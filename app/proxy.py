"""OpenAI 兼容转发 + 故障转移 + SSE 流式透传。

核心策略：
- 非流式请求：完整缓冲响应后再返回 —— 失败可完全无缝切换
- 流式请求：等首字节（first_byte_timeout）确认站点存活后才开始透传；
  首字节之前可切换，首字节之后若卡死/断开只能向客户端补发错误事件
  （SSE 协议下无法中途换站）
- 每站并发信号量限流，超限站点跳过
"""
from __future__ import annotations

import asyncio
import codecs
import json
import math
import time
import weakref

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .config import GatewayConfig
from .errors import (
    STUCK_MSG,
    UpstreamError,
    classify_error,
    connect_error,
    first_byte_error,
    timeout_error,
)
from .router import Router
from .state import Registry, StationState
from .stats import EventLog
from .wire import ChatStreamToResponsesTranslator, chat_to_responses, prepare_request

from .juice import response_model, response_text

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class _StreamRouteLease:
    """Idempotently release a committed stream route on every exit path."""

    def __init__(self, registry: Registry, router: Router, st: StationState,
                 route_id: int, resp: httpx.Response, stream_iter) -> None:
        self.registry = registry
        self.router = router
        self.st = st
        self.route_id = route_id
        self.resp = resp
        self.stream_iter = stream_iter
        self.closed = False

    async def close(self, outcome: str = "cancelled",
                    error: str = "client_stream_closed") -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.stream_iter.aclose()
        finally:
            try:
                await self.resp.aclose()
            finally:
                self.registry.end_route(self.route_id, outcome, error)
                self.router.release(self.st)

    def abandon(self, loop: asyncio.AbstractEventLoop) -> None:
        """Best-effort safety net when a response is dropped before ASGI starts it."""
        if self.closed or loop.is_closed():
            return

        def schedule() -> None:
            if not self.closed:
                loop.create_task(self.close())

        try:
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            pass


def _error_response(status: int, message: str, err_type: str = "upstream_error") -> JSONResponse:
    """OpenAI 兼容的错误响应格式。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": status}},
    )


def _error_sse(message: str) -> bytes:
    """SSE 流中途出错的回补事件。"""
    obj = {"error": {"message": message, "type": "upstream_error", "param": None, "code": 500}}
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _all_failed_message(attempts: list[str]) -> str:
    """Build a useful error when routing found no eligible station."""
    if not attempts:
        return "没有可用中转站（站点可能正在冷却、限流或尚未恢复），请稍后重试"
    return f"所有 {len(attempts)} 个中转站均失败: " + " | ".join(attempts)


def _annotate_attempts(response: Response, attempts: int) -> Response:
    """Attach internal routing metadata without exposing it as an HTTP header."""
    response._zg_attempts = max(0, int(attempts))  # type: ignore[attr-defined]
    return response


def _response_error_message(response: Response) -> str:
    try:
        payload = json.loads(response.body.decode("utf-8", errors="replace"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return "中转站请求失败"


# 参数类错误关键词：不同中转站对同一模型的参数支持不一致（如 reasoning effort），
# 命中时网关可剥离对应参数降级重试，保证跨站兼容
PARAM_ERROR_KEYWORDS = ("invalid value", "unsupported", "not supported",
                        "reasoning", "effort", "参数", "不支持")


def _is_param_error(result) -> bool:
    """判断 400 响应是否为参数类错误（模型参数不被该站支持）。"""
    try:
        body = json.loads(result.body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return False
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return False
    msg = str(err.get("message", "")).lower()
    return any(k in msg for k in PARAM_ERROR_KEYWORDS)


def _strip_reasoning(body: dict):
    """剥离 reasoning 字段降级重试；无该字段返回 None（不降级）。"""
    if "reasoning" in body:
        degraded = dict(body)
        degraded.pop("reasoning", None)
        return degraded
    return None


def _build_headers(st: StationState) -> dict:
    return {
        "Authorization": f"Bearer {st.cfg.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **st.cfg.headers,
    }


def _chunk_has_content(chunk: bytes) -> bool:
    """判断块内是否包含首个内容 token（chat delta.content / responses output_text.delta）。"""
    text = chunk.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # responses 流式事件
        if obj.get("type") == "response.output_text.delta":
            return True
        if obj.get("type") == "response.output_text.done":
            return True
        # chat 流式 chunk
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str) and content:
                    return True
    return False


def stream_text(chunk: bytes) -> str:
    """从 SSE 首个内容块提取文本（delta.content / output_text.delta）。"""
    text = chunk.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for choice in obj.get("choices", []) or []:
            if isinstance(choice, dict):
                delta = choice.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                    return delta["content"]
        if obj.get("type") == "response.output_text.delta":
            return str(obj.get("delta", ""))
    return ""


def _looks_like_error(chunk: bytes) -> bool:
    """部分中转站用 200 + error JSON 表示失败，需识别并触发切换。"""
    s = chunk.decode("utf-8", errors="replace").strip()
    if s.startswith("data:"):
        s = s[5:].strip()
    if not s or s == "[DONE]":
        return False
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return False  # 无法解析，照常透传
    return isinstance(obj, dict) and obj.get("error") is not None


def _usage_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return max(0, int(value))


def _first_usage_int(*values) -> int | None:
    for value in values:
        parsed = _usage_int(value)
        if parsed is not None:
            return parsed
    return None


def _usage_tokens(usage) -> int | None:
    if not isinstance(usage, dict):
        return None
    total = _usage_int(usage.get("total_tokens"))
    if total is not None:
        return total
    # Responses API also exposes input_tokens + output_tokens.
    input_tokens = _usage_int(usage.get("input_tokens"))
    output_tokens = _usage_int(usage.get("output_tokens"))
    if input_tokens is not None and output_tokens is not None:
        return input_tokens + output_tokens
    return None


class UsageBreakdown:
    """Normalized usage from chat/completions or Responses API."""

    __slots__ = ("tokens", "input_tokens", "output_tokens",
                 "cache_read", "cache_write")

    def __init__(self, tokens: int | None = None,
                 input_tokens: int | None = None,
                 output_tokens: int | None = None,
                 cache_read: int | None = None,
                 cache_write: int | None = None) -> None:
        self.tokens = tokens
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read = cache_read
        self.cache_write = cache_write

    def as_kwargs(self) -> dict[str, int | None]:
        return {
            "tokens": self.tokens,
            "input_tokens": self.input_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }


def _parse_usage(usage) -> UsageBreakdown | None:
    if not isinstance(usage, dict):
        return None
    tokens = _usage_tokens(usage)
    input_tokens = _first_usage_int(
        usage.get("prompt_tokens"), usage.get("input_tokens"))
    output_tokens = _first_usage_int(
        usage.get("completion_tokens"), usage.get("output_tokens"))
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        details = {}
    cache_read = _first_usage_int(
        usage.get("cache_read_input_tokens"),
        usage.get("cached_tokens"),
        details.get("cached_tokens"),
        details.get("cache_read_tokens"),
        usage.get("prompt_cache_hit_tokens"),
    )
    cache_write = _first_usage_int(
        usage.get("cache_creation_input_tokens"),
        usage.get("cache_write_input_tokens"),
        details.get("cache_creation_tokens"),
        details.get("cache_write_tokens"),
    )
    if (tokens is None and input_tokens is None and output_tokens is None
            and cache_read is None and cache_write is None):
        return None
    return UsageBreakdown(
        tokens=tokens, input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read=cache_read, cache_write=cache_write,
    )


def _usage_from_payload(obj) -> UsageBreakdown | None:
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    response = obj.get("response")
    if not isinstance(usage, dict) and isinstance(response, dict):
        usage = response.get("usage")
    return _parse_usage(usage)


def _extract_usage(content: bytes) -> UsageBreakdown | None:
    """从完整响应体提取 usage（含缓存命中字段）。"""
    try:
        obj = json.loads(content.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return _usage_from_payload(obj)


def _extract_tokens(content: bytes) -> int | None:
    """从完整响应体中提取 usage.total_tokens（chat/completions/embeddings/responses 兼容）。"""
    usage = _extract_usage(content)
    return None if usage is None else usage.tokens


def _usage_from_sse_data(payload: str) -> UsageBreakdown | None:
    payload = payload.strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return _usage_from_payload(obj)


def _tokens_from_sse_data(payload: str) -> int | None:
    usage = _usage_from_sse_data(payload)
    return None if usage is None else usage.tokens


class _StreamUsageParser:
    """Incrementally parse complete SSE events across arbitrary network chunks."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._data_lines: list[str] = []
        self.last: UsageBreakdown | None = None

    def _finish_event(self) -> int | None:
        if not self._data_lines:
            return None
        payload = "\n".join(self._data_lines)
        self._data_lines.clear()
        usage = _usage_from_sse_data(payload)
        if usage is None:
            return None
        self.last = usage
        return usage.tokens

    def _consume_line(self, line: str) -> int | None:
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            return self._finish_event()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_lines.append(value if separator else "")
        return None

    def feed(self, chunk: bytes) -> int | None:
        """Consume bytes and return the last usage value from completed events."""
        self._buffer += self._decoder.decode(chunk, final=False)
        result = None
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            tokens = self._consume_line(line)
            if tokens is not None:
                result = tokens
        return result

    def finish(self) -> int | None:
        """Flush decoder and a final event even when the stream omitted a blank line."""
        self._buffer += self._decoder.decode(b"", final=True)
        result = None
        if self._buffer:
            tokens = self._consume_line(self._buffer)
            self._buffer = ""
            if tokens is not None:
                result = tokens
        tokens = self._finish_event()
        if tokens is not None:
            result = tokens
        return result


def _extract_stream_tokens(chunk: bytes) -> int | None:
    """Compatibility helper for a complete SSE byte sequence."""
    parser = _StreamUsageParser()
    result = parser.feed(chunk)
    tail = parser.finish()
    return tail if tail is not None else result


async def _read_first(resp: httpx.Response, timeout: float):
    """取首个数据块（首字节）。返回 (stream_iter, first_bytes)。"""
    stream_iter = resp.aiter_bytes()
    try:
        first = await asyncio.wait_for(stream_iter.__anext__(), timeout=timeout)
    except StopAsyncIteration:
        first = b""  # 空流
    except BaseException:
        await stream_iter.aclose()
        raise
    return stream_iter, first


async def _read_until_content(stream_iter, timeout: float):
    """持续读取直至出现首个内容事件。

    返回 (buffered_chunks: list[bytes], content_started: bool)。
    只消费不转发，块由调用方全部缓存用于后续透传；
    超时/异常时由调用方按卡死处理（迭代器可能已断，需 aclose）。
    注意：不做内部 aclose，由调用方在失败路径统一清理。
    """
    buffered: list[bytes] = []
    while True:
        try:
            chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return buffered, False  # 上游直接结束且无内容
        except BaseException:
            raise
        buffered.append(chunk)
        if _chunk_has_content(chunk):
            return buffered, True
        # 无内容事件（created/reasoning/keepalive 等）继续等待
    """取首个数据块（首字节）。返回 (stream_iter, first_bytes)。

    stream_iter 复用于后续迭代，避免对同一响应二次调用
    aiter_bytes() 触发 httpx 的 StreamConsumed。
    超时或异常时内部负责关闭迭代器后重新抛出。
    """
    stream_iter = resp.aiter_bytes()
    try:
        first = await asyncio.wait_for(stream_iter.__anext__(), timeout=timeout)
    except StopAsyncIteration:
        first = b""  # 空流
    except BaseException:
        await stream_iter.aclose()
        raise
    return stream_iter, first


class OpenAIProxy:
    def __init__(self, registry: Registry, gateway: GatewayConfig, eventlog: EventLog,
                 notifier=None):
        self.registry = registry
        self.gateway = gateway
        self.eventlog = eventlog
        self.notifier = notifier
        self.router = Router(registry, gateway)
        self.api = APIRouter(prefix="/v1")
        self.api.add_api_route("/chat/completions", self.chat_completions, methods=["POST"])
        self.api.add_api_route("/completions", self.completions, methods=["POST"])
        self.api.add_api_route("/embeddings", self.embeddings, methods=["POST"])
        self.api.add_api_route("/responses", self.responses, methods=["POST"])
        self.api.add_api_route("/models", self.list_models, methods=["GET"])

    def _has_retryable_station(self, model: str) -> bool:
        """Whether a configured station may become routeable again.

        ``Router.pick`` intentionally filters stations that are cooling down,
        rate-limited, or otherwise unhealthy.  An empty result therefore does
        not necessarily mean that the model is unsupported: it can also mean
        that every eligible station is temporarily unavailable.  Keep quota
        exhausted and explicitly disabled stations out of this check because
        those require operator action rather than blind retries.
        """
        for st in self.registry.states.values():
            if not st.enabled or st.quota_exhausted:
                continue
            if (self._station_matches_model(st, model)
                    or self._model_discovery_unresolved(st)):
                return True
        return False

    @staticmethod
    def _station_matches_model(st: StationState, model: str) -> bool:
        return st.accepts_any_model or model in st.effective_models

    @staticmethod
    def _model_discovery_unresolved(st: StationState) -> bool:
        """Model discovery has not produced a trustworthy list yet."""
        return (
            st.cfg.probe_models
            and not st.probed_models
            and st.last_probe_ok is not True
        )

    def _routing_precondition_error(self, model: str) -> JSONResponse | None:
        """Classify deterministic no-route states before starting a round."""
        matching = [
            st for st in self.registry.states.values()
            if self._station_matches_model(st, model)
        ]
        if any(st.enabled and not st.quota_exhausted for st in matching):
            return None

        # Immediately after startup, probed model lists are empty while the
        # health monitor is fetching them.  Keep the original request waiting
        # instead of misclassifying a discoverable model as invalid.
        if any(
            st.enabled
            and not st.quota_exhausted
            and self._model_discovery_unresolved(st)
            for st in self.registry.states.values()
        ):
            return None

        if matching:
            return _error_response(
                422,
                "该模型对应的中转站均已停用，或需要先重置配额状态",
                "invalid_request_error",
            )
        return _error_response(
            400,
            f"模型 {model} 未在任何中转站配置",
            "invalid_request_error",
        )

    def _round_delay(self, round_index: int) -> float:
        """Return a capped exponential delay before the next retry round."""
        base = self.gateway.round_backoff
        if base <= 0:
            return 0.0
        cap = self.gateway.max_round_backoff
        if cap <= base:
            return cap
        cap_round = math.ceil(math.log2(cap / base))
        if round_index >= cap_round:
            return cap
        return base * (2 ** round_index)

    async def _wait_for_retry(self, request: Request, delay: float) -> bool:
        """Wait in short slices so a disconnected client stops retries promptly."""
        deadline = time.monotonic() + delay
        while True:
            if await request.is_disconnected():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(remaining, 0.1))

    @staticmethod
    def _retry_timeout_response(limit: float) -> JSONResponse:
        return _error_response(
            502,
            f"重试总时限 {limit:g} 秒已到期，所有可用中转站仍未恢复",
        )

    @staticmethod
    def _client_disconnected_response() -> JSONResponse:
        return _error_response(499, "客户端已断开，已停止后台重试")

    def _terminal_failure(self, model: str, response: JSONResponse,
                          attempts: int) -> JSONResponse:
        """Record one client-visible failure, regardless of internal rounds."""
        if getattr(response, "_zg_failure_recorded", False):
            return response
        self.eventlog.add(
            "-",
            model,
            False,
            0,
            _response_error_message(response),
            attempts=max(1, attempts),
        )
        response._zg_failure_recorded = True  # type: ignore[attr-defined]
        return response

    # ------------------------------------------------------------------
    # 入口端点
    # ------------------------------------------------------------------
    async def chat_completions(self, request: Request):
        return await self._dispatch(request, "/chat/completions")

    async def completions(self, request: Request):
        return await self._dispatch(request, "/completions")

    async def embeddings(self, request: Request):
        return await self._dispatch(request, "/embeddings")

    async def responses(self, request: Request):
        """Responses API（Codex/OpenAI 新版客户端使用），透传 + 故障切换。"""
        return await self._dispatch(request, "/responses")

    async def _dispatch(self, request: Request, path: str):
        try:
            body = await request.json()
        except Exception:
            return _error_response(400, "请求体不是合法的 JSON", "invalid_request_error")
        if not isinstance(body, dict):
            return _error_response(400, "请求体必须是 JSON 对象", "invalid_request_error")
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            return _error_response(400, "model 必须是非空字符串", "invalid_request_error")

        stream = path != "/embeddings" and body.get("stream")
        max_retry_seconds = self.gateway.max_retry_seconds
        deadline = (time.monotonic() + max_retry_seconds) if max_retry_seconds else None
        round_index = 0
        total_attempts = 0
        # 仅瞬时失败继续下一轮；确定性错误立即返回。所有客户端均按
        # 网关显式重试配置执行，不再根据其私有重试头缩减重试预算。
        while self.gateway.max_rounds == 0 or round_index < self.gateway.max_rounds:
            if await request.is_disconnected():
                return self._client_disconnected_response()
            precondition_error = self._routing_precondition_error(model)
            if precondition_error is not None:
                return self._terminal_failure(model, precondition_error, total_attempts)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return self._terminal_failure(
                    model, self._retry_timeout_response(max_retry_seconds), total_attempts)
            try:
                forward = (self._forward_stream(path, body, model) if stream
                           else self._forward_json(path, body, model))
                if remaining is None:
                    result, has_transient = await forward
                else:
                    result, has_transient = await asyncio.wait_for(forward, remaining)
            except asyncio.TimeoutError:
                return self._terminal_failure(
                    model, self._retry_timeout_response(max_retry_seconds), total_attempts)
            total_attempts += getattr(result, "_zg_attempts", 0)
            if getattr(result, "status_code", 0) != 502:
                # 参数类 400（如 reasoning effort 不被该站支持）：剥离参数降级重试一次
                if result.status_code == 400 and _is_param_error(result):
                    degraded = _strip_reasoning(body)
                    if degraded is not None:
                        try:
                            remaining = None if deadline is None else deadline - time.monotonic()
                            if remaining is not None and remaining <= 0:
                                return self._terminal_failure(
                                    model,
                                    self._retry_timeout_response(max_retry_seconds),
                                    total_attempts,
                                )
                            forward = self._forward_json(path, degraded, model)
                            if remaining is None:
                                result, _ = await forward
                            else:
                                result, _ = await asyncio.wait_for(forward, remaining)
                        except asyncio.TimeoutError:
                            return self._terminal_failure(
                                model,
                                self._retry_timeout_response(max_retry_seconds),
                                total_attempts,
                            )
                        if getattr(result, "status_code", 0) != 400:
                            return result
                return result
            if not has_transient:
                return self._terminal_failure(
                    model, result, total_attempts)  # 确定性失败：快速返回
            round_index += 1
            if self.gateway.max_rounds and round_index >= self.gateway.max_rounds:
                return self._terminal_failure(model, result, total_attempts)
            delay = self._round_delay(round_index - 1)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._terminal_failure(
                        model, self._retry_timeout_response(max_retry_seconds), total_attempts)
                delay = min(delay, remaining)
            if not await self._wait_for_retry(request, delay):
                return self._client_disconnected_response()
        return self._terminal_failure(
            model, _error_response(502, "重试已停止"), total_attempts)

    async def list_models(self):
        merged: dict[str, list[str]] = {}
        for st in self.registry.states.values():
            for m in st.effective_models:
                merged.setdefault(m, []).append(st.cfg.name)
        data = [
            {"id": m, "object": "model", "created": 0,
             "owned_by": ",".join(sorted(names))}
            for m, names in sorted(merged.items())
        ]
        return {"object": "list", "data": data}

    # ------------------------------------------------------------------
    # 失败处理（计费模式策略）
    # ------------------------------------------------------------------
    def _handle_failure(self, st: StationState, err: UpstreamError) -> UpstreamError:
        """记录失败并按计费策略处理，返回 err 供调用方判断是否继续换站/重试。

        - quota 错误（余额不足/订阅到期）→ 标记 quota_exhausted，从路由排除，
          后台按来源自动探测恢复（/models 成功不视为有额度）
        - 429 限流 → 短冷却（rate_limit_cooldown 秒内不路由该站），
          订阅制站点尤其重要：避免重试风暴烧掉订阅配额
        """
        st.record_failure(err)
        if err.quota:
            st.mark_quota_exhausted(err)
            if self.notifier is not None:
                asyncio.create_task(self.notifier.send(
                    "station_quota", st.cfg.name,
                    f"配额问题: {err.message[:120]}（将自动探测直到额度恢复）"))
        if err.status_code == 429:
            st.mark_rate_limited(self.gateway.rate_limit_cooldown)
        return err

    # ------------------------------------------------------------------
    # 非流式转发（完整缓冲，可无缝切换）
    # ------------------------------------------------------------------
    async def _forward_json(self, path: str, body: dict, model: str):
        """非流式转发：完整缓冲 + 站级无缝切换。

        返回 (response, has_transient)：has_transient=True 表示本轮存在瞬时失败，
        值得整体重试一轮（fail-fast 判断依据）。
        """
        attempts: list[str] = []
        has_transient = False
        candidates = self.router.pick(model)
        # No candidates can mean all matching stations are in a temporary
        # cooldown/rate-limit window.  Preserve that signal so _dispatch can
        # apply the configured round retry budget instead of returning 502
        # immediately (which otherwise leaves retries solely to the client).
        if not candidates:
            has_transient = self._has_retryable_station(model)

        for idx, st in enumerate(candidates):
            if idx >= self.gateway.max_attempts:
                break
            if not await self.router.acquire(st):
                attempts.append(f"[{st.cfg.name}] 并发已满")
                has_transient = True  # 下一轮可能不忙
                continue
            if attempts:  # 之前失败过，切换前稍作退避
                await asyncio.sleep(self.gateway.retry_backoff)
            route_id = self.registry.begin_route(st.cfg.name, model)
            route_outcome = "failed"
            route_error = ""
            target_path, target_body, adapter = prepare_request(st, path, body)
            try:
                client = self.registry.ensure_client(st)
                url = st.cfg.api_base + target_path
                timeout = httpx.Timeout(
                    connect=self.gateway.connect_timeout,
                    read=st.cfg.timeout,
                    write=30.0, pool=10.0,
                )
                t0 = time.perf_counter()
                req = client.build_request("POST", url, json=target_body,
                                           headers=_build_headers(st), timeout=timeout)
                # send 会一直等到响应头返回：用首字节超时兜底卡死站点
                # （卡死 = 响应头都不回，超时后取消并切换下一站）
                resp = await asyncio.wait_for(
                    client.send(req, stream=True),
                    timeout=self.gateway.first_byte_timeout,
                )

                if resp.status_code in (200, 201):
                    # 等首字节（卡死检测），之后缓冲剩余；顺带测量首字时间
                    t_first = time.perf_counter()
                    try:
                        stream_iter, first = await _read_first(
                            resp, timeout=self.gateway.first_byte_timeout)
                    except (asyncio.TimeoutError, httpx.TimeoutException):
                        await resp.aclose()
                        err = first_byte_error(st.cfg.name)
                        st.record_failure(err)
                        route_error = str(err)
                        attempts.append(f"[{st.cfg.name}] {STUCK_MSG}")
                        has_transient = True
                        continue
                    except httpx.TransportError as e:
                        await resp.aclose()
                        err = connect_error(st.cfg.name, type(e).__name__)
                        st.record_failure(err)
                        route_error = str(err)
                        attempts.append(str(err))
                        has_transient = True
                        continue
                    self.registry.update_route_phase(route_id, "receiving")
                    # 缓冲剩余字节（后续读取超时受 station.timeout 约束）
                    try:
                        chunks = [first] if first else []
                        async for chunk in stream_iter:
                            chunks.append(chunk)
                    except (httpx.TimeoutException, httpx.TransportError) as e:
                        await stream_iter.aclose()
                        await resp.aclose()
                        err = timeout_error(st.cfg.name,
                                            f"读取中断: {type(e).__name__}")
                        st.record_failure(err)
                        route_error = str(err)
                        attempts.append(str(err))
                        has_transient = True
                        continue
                    await resp.aclose()

                    latency = (time.perf_counter() - t0) * 1000
                    # 非流式不记录 TTFT（响应体和响应头同时到达，无"首字"体感含义）
                    switched = bool(attempts)
                    st.record_success(latency / 1000, switched=switched)
                    detail = "成功" + (f" (经{len(attempts)}次切换)" if switched else "")
                    body_bytes = b"".join(chunks)
                    usage = _extract_usage(body_bytes)
                    tokens = None if usage is None else usage.tokens
                    # 协议翻译：chat 响应 → responses（Codex 入口 + chat 站）
                    if adapter == ("r2c",):
                        try:
                            obj = json.loads(body_bytes.decode("utf-8", errors="replace"))
                            body_bytes = json.dumps(
                                chat_to_responses(obj, model), ensure_ascii=False).encode("utf-8")
                        except Exception:
                            pass  # 翻译失败降级为透传原响应
                    self.eventlog.add(st.cfg.name, model, True, latency,
                                      detail, attempts=len(attempts) + 1,
                                      **(usage.as_kwargs() if usage else {
                                          "tokens": tokens}))
                    # Juice 被动统计：上游头家族 + model 回显比对 + 真实响应特征指纹
                    resp_model = response_model(body_bytes)
                    st.note_upstream(resp.headers, resp_model, model)
                    self.registry.note_fingerprint(
                        st, model, response_text(body_bytes))
                    self.registry.mark_routed(st.cfg.name, model, latency)
                    route_outcome = "success"
                    return (Response(
                        content=body_bytes,
                        status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"),
                    ), False)
                # 非 2xx：读错误体（该响应尚未被迭代，可安全 aread）
                payload = await resp.aread()
                await resp.aclose()
                err = classify_error(st.cfg.name, resp.status_code, payload)
                err = self._handle_failure(st, err)
                route_error = str(err)
                if not err.retryable:
                    # 客户端问题，无需再试其他站
                    return (_error_response(err.status_code or 400, err.message,
                                            "invalid_request_error"), False)
                attempts.append(str(err))
                has_transient |= not err.deterministic
            except asyncio.CancelledError:
                route_outcome = "cancelled"
                route_error = "client_cancelled"
                raise
            except asyncio.TimeoutError:
                err = first_byte_error(st.cfg.name)
                st.record_failure(err)
                route_error = str(err)
                attempts.append(f"[{st.cfg.name}] {STUCK_MSG}")
                has_transient = True
            except httpx.TimeoutException:
                err = timeout_error(st.cfg.name)
                st.record_failure(err)
                route_error = str(err)
                attempts.append(str(err))
                has_transient = True
            except httpx.TransportError as e:
                err = connect_error(st.cfg.name, type(e).__name__)
                st.record_failure(err)
                route_error = str(err)
                attempts.append(str(err))
                has_transient = True
            finally:
                if route_outcome == "failed" and not route_error:
                    route_error = "request_aborted"
                self.registry.end_route(route_id, route_outcome, route_error)
                self.router.release(st)

        msg = _all_failed_message(attempts)
        response = _annotate_attempts(_error_response(502, msg), len(attempts))
        return (response, has_transient)

    # ------------------------------------------------------------------
    # 流式转发（SSE 透传，首字节前可切换）
    # ------------------------------------------------------------------
    async def _forward_stream(self, path: str, body: dict, model: str):
        """流式转发：SSE 透传 + 首字节前无缝切换 + 长流保活。

        返回 (response, has_transient)，语义同 _forward_json。
        """
        attempts: list[str] = []
        has_transient = False
        candidates = self.router.pick(model)
        if not candidates:
            has_transient = self._has_retryable_station(model)

        for idx, st in enumerate(candidates):
            if idx >= self.gateway.max_attempts:
                break
            if not await self.router.acquire(st):
                attempts.append(f"[{st.cfg.name}] 并发已满")
                has_transient = True
                continue
            if attempts:
                await asyncio.sleep(self.gateway.retry_backoff)

            route_id = self.registry.begin_route(st.cfg.name, model)
            route_outcome = "failed"
            route_error = ""
            transferred = False
            resp = None
            target_path, target_body, adapter = prepare_request(st, path, body)
            try:
                client = self.registry.ensure_client(st)
                url = st.cfg.api_base + target_path
                timeout = httpx.Timeout(
                    connect=self.gateway.connect_timeout,
                    read=self.gateway.read_timeout,
                    write=30.0, pool=10.0,
                )
                t0 = time.perf_counter()
                req = client.build_request("POST", url, json=target_body,
                                           headers=_build_headers(st), timeout=timeout)
                try:
                    # send 会一直等到响应头返回：用首字节超时兜底卡死站点
                    resp = await asyncio.wait_for(
                        client.send(req, stream=True),
                        timeout=self.gateway.first_byte_timeout,
                    )
                except asyncio.TimeoutError:
                    err = first_byte_error(st.cfg.name)
                    st.record_failure(err)
                    route_error = str(err)
                    attempts.append(f"[{st.cfg.name}] {STUCK_MSG}")
                    has_transient = True
                    continue
                except httpx.TimeoutException:
                    err = timeout_error(st.cfg.name)
                    st.record_failure(err)
                    route_error = str(err)
                    attempts.append(str(err))
                    has_transient = True
                    continue
                except httpx.TransportError as e:
                    err = connect_error(st.cfg.name, type(e).__name__)
                    st.record_failure(err)
                    route_error = str(err)
                    attempts.append(str(err))
                    has_transient = True
                    continue

                # ---- 状态码检查（首字节之前，可切换） ----
                if resp.status_code != 200:
                    payload = await resp.aread()
                    err = classify_error(st.cfg.name, resp.status_code, payload)
                    err = self._handle_failure(st, err)
                    route_error = str(err)
                    if not err.retryable:
                        return (_error_response(err.status_code or 400, err.message,
                                                "invalid_request_error"), False)
                    attempts.append(str(err))
                    has_transient |= not err.deterministic
                    continue

                # ---- 等首个内容事件（卡死检测点 + 首字时间测量） ----
                stream_iter = resp.aiter_bytes()
                t_first = time.perf_counter()
                try:
                    buffered, has_content = await _read_until_content(
                        stream_iter, timeout=self.gateway.first_token_timeout)
                except (asyncio.TimeoutError, httpx.TimeoutException):
                    await stream_iter.aclose()
                    err = first_byte_error(st.cfg.name)
                    st.record_failure(err)
                    route_error = str(err)
                    attempts.append(f"[{st.cfg.name}] {STUCK_MSG}")
                    has_transient = True
                    continue
                except httpx.TransportError as e:
                    await stream_iter.aclose()
                    err = connect_error(st.cfg.name, type(e).__name__)
                    st.record_failure(err)
                    route_error = str(err)
                    attempts.append(str(err))
                    has_transient = True
                    continue
                first = buffered[0] if buffered else b""

                # ---- 首块校验（200 + error JSON 的情况） ----
                if _looks_like_error(first):
                    err = classify_error(st.cfg.name, 200, first)
                    err = self._handle_failure(st, err)
                    route_error = str(err)
                    if not err.retryable:
                        return (_error_response(400, err.message,
                                                "invalid_request_error"), False)
                    attempts.append(str(err))
                    has_transient |= not err.deterministic
                    continue

                # ===== 已提交：开始透传（此后不可切换） =====
                latency = (time.perf_counter() - t0) * 1000
                ttft_ms = (time.perf_counter() - t_first) * 1000
                st.record_ttft(ttft_ms / 1000)
                switched = bool(attempts)
                st.record_success(latency / 1000, switched=switched)
                detail = "流式成功" + (f" (经{len(attempts)}次切换)" if switched else "")
                event_time = self.eventlog.add(
                    st.cfg.name, model, True, latency,
                    detail, attempts=len(attempts) + 1,
                    ttft_ms=ttft_ms)
                st.note_upstream(resp.headers, None, model)  # Juice: 上游头家族（流式）
                self.registry.note_fingerprint(st, model, stream_text(first))
                self.registry.mark_routed(st.cfg.name, model, latency)
                self.registry.update_route_phase(route_id, "streaming")

                keepalive = self.gateway.sse_keepalive_interval
                eventlog = self.eventlog
                route_model = model
                route_event_time = event_time
                registry = self.registry
                router = self.router
                lease = _StreamRouteLease(
                    registry, router, st, route_id, resp, stream_iter)

                async def gen(resp=resp, st=st, prefetch=buffered,
                              stream_iter=stream_iter, route_id=route_id,
                              adapter=adapter, model=model):
                    translator = (ChatStreamToResponsesTranslator(model)
                                  if adapter == ("r2c",) else None)
                    stream_tokens = None
                    usage_parser = _StreamUsageParser()
                    stream_outcome = "cancelled"
                    stream_error = "client_stream_closed"
                    try:
                        for chunk in prefetch:
                            parsed_tokens = usage_parser.feed(chunk)
                            if parsed_tokens is not None:
                                stream_tokens = parsed_tokens
                            if translator:
                                out = translator.feed(chunk)
                                if out:
                                    yield out
                            else:
                                yield chunk
                        while True:
                            try:
                                chunk = await asyncio.wait_for(
                                    stream_iter.__anext__(), timeout=keepalive or None)
                                parsed_tokens = usage_parser.feed(chunk)
                                if parsed_tokens is not None:
                                    stream_tokens = parsed_tokens
                                if translator:
                                    out = translator.feed(chunk)
                                    if out:
                                        yield out
                                else:
                                    yield chunk
                            except StopAsyncIteration:
                                parsed_tokens = usage_parser.finish()
                                if parsed_tokens is not None:
                                    stream_tokens = parsed_tokens
                                stream_outcome = "success"
                                stream_error = ""
                                if translator:
                                    tail = translator.finish()
                                    if tail:
                                        yield tail
                                break
                            except asyncio.TimeoutError:
                                # SSE 保活：长流无事件时发注释行，防止客户端空闲超时
                                if keepalive:
                                    yield b": keep-alive\n\n"
                    except asyncio.CancelledError:
                        stream_outcome = "cancelled"
                        stream_error = "client_cancelled"
                        raise
                    except (httpx.TransportError, httpx.StreamError) as e:
                        # 流中途断开：SSE 协议下无法切换，补发错误事件
                        stream_outcome = "stream_error"
                        stream_error = type(e).__name__
                        yield _error_sse(
                            f"上游 {st.cfg.name} 流中断: {type(e).__name__}")
                    finally:
                        usage = usage_parser.last
                        if usage is not None and usage.tokens is not None:
                            eventlog.add_tokens(
                                st.cfg.name, route_model, usage.tokens,
                                occurred_at=route_event_time,
                                input_tokens=usage.input_tokens,
                                cache_read=usage.cache_read,
                                cache_write=usage.cache_write)
                        elif stream_tokens is not None:
                            eventlog.add_tokens(
                                st.cfg.name, route_model, stream_tokens,
                                occurred_at=route_event_time)
                        await lease.close(stream_outcome, stream_error)

                response = StreamingResponse(
                    gen(), media_type="text/event-stream", headers=dict(SSE_HEADERS),
                    background=BackgroundTask(lease.close))
                response._route_lease_finalizer = weakref.finalize(
                    response, lease.abandon, asyncio.get_running_loop())
                transferred = True
                return (response, False)
            except asyncio.CancelledError:
                route_outcome = "cancelled"
                route_error = "client_cancelled"
                raise
            finally:
                if not transferred:
                    try:
                        if resp is not None:
                            await resp.aclose()
                    finally:
                        if route_outcome == "failed" and not route_error:
                            route_error = "request_aborted"
                        self.registry.end_route(route_id, route_outcome, route_error)
                        self.router.release(st)

        # ---- 全部失败 ----
        msg = _all_failed_message(attempts)
        response = _annotate_attempts(_error_response(502, msg), len(attempts))
        return (response, has_transient)
