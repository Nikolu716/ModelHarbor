"""Mock 中转站 - 端到端测试用。

模拟 OpenAI 兼容上游，可配置故障行为:

    # 正常站点
    python tests/mock_upstream.py --port 9101 --name good
    # 慢站点（响应延迟 3 秒）
    python tests/mock_upstream.py --port 9102 --name slow --delay 3
    # 卡死站点（永不响应，测首字节超时）
    python tests/mock_upstream.py --port 9103 --name stuck --stuck
    # 401 站点
    python tests/mock_upstream.py --port 9104 --name bad401 --error 401
    # 容量不足站点
    python tests/mock_upstream.py --port 9105 --name busy --capacity
    # 余额不足站点（402，模拟余额制耗尽）
    python tests/mock_upstream.py --port 9106 --name noquota --error 402
    # 订阅到期站点（403 + 到期提示，模拟订阅制失效）
    python tests/mock_upstream.py --port 9107 --name subexpired --sub-expired
    # 随机失败率 50%
    python tests/mock_upstream.py --port 9108 --name flaky --fail-rate 0.5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

MODEL = "mock-model"


def make_app(name: str, error: int | None, capacity: bool, delay: float,
             stuck: bool, fail_rate: float, stream_delay: float,
             sub_expired: bool = False, rl_requests: bool = False,
             balance: float | None = None, fake_model: str | None = None):
    app = FastAPI()

    def maybe_error(probe: bool = False):
        """返回故障响应或 None。probe=True 表示健康探测请求。"""
        if stuck:
            return None  # 卡死模式单独处理
        if rl_requests and not probe:
            # 探测正常，但真实请求一律 429（模拟订阅制限流）
            return JSONResponse(status_code=429, content={"error": {
                "message": "Rate limit reached, too many requests.", "type": "rate_limit_error",
                "param": None, "code": 429}})
        if capacity:
            return JSONResponse(status_code=503, content={"error": {
                "message": "The server is currently overloaded with other requests. "
                           "No capacity available, please try again later.",
                "type": "server_error", "param": None, "code": "insufficient_capacity"}})
        if sub_expired:
            return JSONResponse(status_code=403, content={"error": {
                "message": "Your subscription has expired, please renew your plan.",
                "type": "access_denied", "param": None, "code": "subscription_expired"}})
        if error:
            msgs = {401: "Incorrect API key provided.", 402: "Insufficient quota: "
                           "your account balance is not enough.",
                    403: "Access forbidden.", 404: "The model does not exist.",
                    429: "Rate limit reached.", 500: "Internal server error.",
                    503: "Service unavailable.", 504: "Gateway timeout."}
            return JSONResponse(status_code=error, content={"error": {
                "message": msgs.get(error, "mock error"), "type": "server_error",
                "param": None, "code": error}})
        if fail_rate and random.random() < fail_rate:
            return JSONResponse(status_code=502, content={"error": {
                "message": "Bad gateway (mock random fail)", "type": "server_error",
                "param": None, "code": 502}})
        return None

    @app.get("/v1/models")
    async def models():
        err = maybe_error(probe=True)
        if err:
            return err
        return {"object": "list", "data": [
            {"id": MODEL, "object": "model", "created": 0, "owned_by": name}]}

    @app.get("/v1/dashboard/billing/credit_grants")
    async def credit_grants():
        if balance is None:
            return JSONResponse(status_code=404, content={"error": {"message": "not found"}})
        return {"object": "list", "total_granted": balance * 2,
                "total_used": balance, "total_available": balance, "grants": []}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        err = maybe_error()
        if err:
            return err
        if stuck:
            await asyncio.sleep(3600)  # 永不响应（网关测首字节超时）
        if delay:
            await asyncio.sleep(delay)
        if body.get("stream"):
            return _stream(name, stream_delay, body.get("model", MODEL))
        if fake_model:
            content = f"42 37 61 05 此处为[{name}]的返回，特征前缀为数字流" + str(len(fake_model))
        else:
            content = f"你好，我是 [{name}] 中转站。"
        return {"id": f"chatcmpl-{name}", "object": "chat.completion",
                "created": int(time.time()), "model": fake_model or body.get("model", MODEL),
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                          "total_tokens": 110,
                          "prompt_tokens_details": {"cached_tokens": 60}}}

    @app.post("/v1/completions")
    async def completions(request: Request):
        body = await request.json()
        err = maybe_error()
        if err:
            return err
        if stuck:
            await asyncio.sleep(3600)
        if delay:
            await asyncio.sleep(delay)
        if body.get("stream"):
            return _stream(name, stream_delay, body.get("model", MODEL), legacy=True)
        return {"id": f"cmpl-{name}", "object": "text_completion",
                "created": int(time.time()), "model": body.get("model", MODEL),
                "choices": [{"index": 0, "text": f"[{name}] legacy 完成",
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5,
                          "total_tokens": 10}}

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        body = await request.json()
        err = maybe_error()
        if err:
            return err
        if stuck:
            await asyncio.sleep(3600)
        if delay:
            await asyncio.sleep(delay)
        return {"object": "list", "data": [
            {"object": "embedding", "index": 0,
             "embedding": [0.1, 0.2, 0.3], "model": body.get("model", MODEL)}],
            "model": body.get("model", MODEL), "usage": {"prompt_tokens": 3,
                                                         "total_tokens": 3}}

    return app


def _stream(name: str, delay: float, model: str, legacy: bool = False):
    async def gen():
        base_id = f"chatcmpl-{name}" if not legacy else f"cmpl-{name}"
        # 前置事件：created（模拟中转站先应答、后推理的场景）
        lead = {"id": base_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": None}]}
        yield "data: " + json.dumps(lead, ensure_ascii=False) + "\n\n"

        await asyncio.sleep(2.0)  # 模拟推理等待 2 秒（首字时间应反映这段）
        chunks = ["你", "好", "，", "我", "是", f"[{name}]", "中", "转", "站", "。"]
        for i, c in enumerate(chunks):
            obj = {"id": base_id, "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": model,
                   "choices": [{"index": 0, "delta": {"content": c},
                                "finish_reason": None}]}
            if legacy:
                obj = {"id": base_id, "object": "text_completion",
                       "created": int(time.time()), "model": model,
                       "choices": [{"index": 0, "text": c,
                                    "finish_reason": None}]}
            yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
            await asyncio.sleep(delay)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def main():
    parser = argparse.ArgumentParser(description="Mock 中转站")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", default="mock")
    parser.add_argument("--error", type=int, choices=[401, 402, 403, 404, 429, 500, 502, 503, 504],
                        help="固定返回该错误码")
    parser.add_argument("--capacity", action="store_true", help="返回 503 容量不足")
    parser.add_argument("--sub-expired", action="store_true",
                        help="返回 403 订阅到期（模拟订阅制失效）")
    parser.add_argument("--rl-requests", action="store_true",
                        help="探测正常，但真实请求返回 429（模拟订阅制限流）")
    parser.add_argument("--balance", type=float, default=None,
                        help="余额接口返回的余额（缺省则 404 模拟不支持）")
    parser.add_argument("--fake-model", type=str, default=None,
                        help="响应 model 用假名（模拟掺水站）")
    parser.add_argument("--delay", type=float, default=0, help="响应延迟秒数")
    parser.add_argument("--stuck", action="store_true", help="永不响应（卡死）")
    parser.add_argument("--fail-rate", type=float, default=0, help="随机失败率 0~1")
    parser.add_argument("--stream-delay", type=float, default=0.05, help="流式块间隔秒数")
    args = parser.parse_args()

    app = make_app(args.name, args.error, args.capacity, args.delay,
                   args.stuck, args.fail_rate, args.stream_delay,
                   sub_expired=args.sub_expired, rl_requests=args.rl_requests,
                   balance=args.balance, fake_model=args.fake_model)
    print(f"[mock:{args.name}] 监听 :{args.port} "
          f"(error={args.error} capacity={args.capacity} sub_expired={args.sub_expired} "
          f"rl_requests={args.rl_requests} balance={args.balance} delay={args.delay} "
          f"stuck={args.stuck} fail_rate={args.fail_rate})")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
