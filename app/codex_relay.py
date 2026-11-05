"""Opt-in Codex protocol relay. Authentication remains owned by official Codex.

Only the fixed Codex Responses endpoint is reachable through this transport.
Client tools are forwarded to the model; the gateway never executes them.
"""
from __future__ import annotations

import asyncio
import copy
import json

import httpx

from . import __version__
from .chatgpt import ChatGPTAuthError
from .wire import UnsupportedWireRequest

CODEX_RESPONSES = "https://chatgpt.com/backend-api/codex/responses"


def _sanitize_input_items(items):
    """Drop client-only IDs from Responses message history.

    Codex accepts server-issued message IDs (``msg_...``). Some clients add
    local timing/TTFT IDs while restoring a conversation. Those IDs are not
    valid upstream message IDs, but removing them is safe because the message
    content remains intact. Other item IDs and tool ``call_id`` values must be
    preserved for Responses history and function-call correlation.
    """
    sanitized = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "message" and "id" in item:
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier.startswith("msg_"):
                item = dict(item)
                item.pop("id", None)
        sanitized.append(item)
    return sanitized


def relay_request(body):
    """Keep Responses tools, history and encrypted reasoning intact."""
    if not isinstance(body, dict):
        raise UnsupportedWireRequest("Codex 协议转发需要 JSON 对象")
    for key in ("previous_response_id", "conversation", "background"):
        if body.get(key):
            raise UnsupportedWireRequest("Codex 协议转发需要完整 input 历史，不支持服务端会话或后台任务")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip() or any(ord(c) < 32 for c in model):
        raise UnsupportedWireRequest("Codex 模型 ID 无效")
    value = body.get("input")
    if not isinstance(value, (str, list)) or not value:
        raise UnsupportedWireRequest("Codex 协议转发需要非空 input")
    if body.get("instructions") is not None and not isinstance(body["instructions"], str):
        raise UnsupportedWireRequest("instructions 必须是文本")
    # This route does not accept sampling/output-budget hints. Preserve semantic
    # contracts (tools, text.format, input items, reasoning); let upstream validate.
    omitted = {"max_output_tokens", "max_completion_tokens", "temperature", "top_p", "top_logprobs",
               "prompt_cache_retention", "safety_identifier", "metadata", "user",
               "previous_response_id", "conversation", "background"}
    target = {key: copy.deepcopy(value) for key, value in body.items() if key not in omitted}
    if isinstance(value, str):
        target["input"] = [{"role": "user", "content": value}]
    else:
        target["input"] = _sanitize_input_items(target["input"])
        target["input"] = [({**item, "role": "developer"} if isinstance(item, dict)
                            and item.get("role") == "system" else item) for item in target["input"]]
    target.update(instructions=target.get("instructions") or "", store=False, stream=True)
    return target


class _RelayStream(httpx.AsyncByteStream):
    def __init__(self, response):
        self.response = response
        self.close_task = None

    async def __aiter__(self):
        try:
            async for chunk in self.response.aiter_raw():
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self.response.aclose())
        await asyncio.shield(self.close_task)


class CodexRelayTransport(httpx.AsyncBaseTransport):
    def __init__(self, accounts, cfg, *, transport=None):
        self.accounts, self.cfg = accounts, cfg
        self.client = httpx.AsyncClient(transport=transport, follow_redirects=False,
            timeout=httpx.Timeout(cfg.timeout, connect=10, pool=10),
            limits=httpx.Limits(max_connections=max(10, cfg.max_concurrency * 2),
                               max_keepalive_connections=cfg.max_concurrency))

    async def handle_async_request(self, request):
        if request.method == "GET" and request.url.path == "/v1/models":
            from .codex_inference import CodexCLITransport
            return await CodexCLITransport(self.accounts, self.cfg).handle_async_request(request)
        if request.method != "POST" or request.url.path != "/v1/responses":
            return httpx.Response(400, json={"error": {"message": "Codex 协议转发仅支持 /v1/responses"}})
        try:
            body = relay_request(json.loads(request.content))
            bridge = self.accounts.codex_account(self.cfg.chatgpt_account)
            token, account_id = await bridge.inference_credentials()
        except ChatGPTAuthError as exc:
            return httpx.Response(401, json={"error": {"code": "codex_auth_required", "message": str(exc)}})
        except (ValueError, TypeError) as exc:
            return httpx.Response(400, json={"error": {"message": str(exc)}})
        headers = {"Authorization": "Bearer " + token, "ChatGPT-Account-Id": account_id,
                   "Content-Type": "application/json", "Accept": "text/event-stream",
                   "User-Agent": "ModelHarbor/" + __version__}
        # Carry only real client session identifiers. Do not invent a shared
        # identity or forward client credentials, cookies, account IDs or IPs.
        for name in ("session-id", "thread-id", "x-codex-turn-state", "x-codex-turn-metadata"):
            if request.headers.get(name):
                headers[name] = request.headers[name]
        if "session-id" not in headers and request.headers.get("x-session-id"):
            headers["session-id"] = request.headers["x-session-id"]
        outgoing = self.client.build_request("POST", CODEX_RESPONSES, json=body, headers=headers)
        if "timeout" in request.extensions:
            outgoing.extensions["timeout"] = request.extensions["timeout"]
        response = await self.client.send(outgoing, stream=True)
        if response.is_redirect:
            await asyncio.shield(response.aclose())
            return httpx.Response(502, json={"error": {"code": "codex_redirect_refused",
                "message": f"Codex 上游返回 HTTP {response.status_code} 重定向，未跟随或重发请求"}},
                headers={key: response.headers[key] for key in ("x-request-id",) if key in response.headers})
        # The outer client performs decoding. Keep original bytes and headers.
        return httpx.Response(response.status_code, headers=response.headers, stream=_RelayStream(response))

    async def aclose(self):
        await self.client.aclose()
