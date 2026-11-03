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

from .config import GatewayConfig
from .errors import (
    STUCK_MSG,
    UpstreamError,
    classify_error,
    connect_error,
    first_byte_error,
    timeout_error,
)
from .router import Router, RoutePermit
from .http_cleanup import close_resource, protect_response, CLOSE_TIMEOUT
from .state import Registry, StationState
from .stats import EventLog
from .wire import ChatStreamToResponsesTranslator, NamespaceMemory, chat_to_responses, prepare_request
from .health import _retry_after_seconds
from .protocols import MESSAGES, GENERATE, STREAM_GENERATE, GEMINI_PATHS, inference_url, native_text, error_body
from .providers import MissingOpenCodeSession, inference_headers, request_context
from .wire import UnsupportedWireRequest

from .juice import response_model, response_text

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _shielded_close(resp, registry: Registry) -> None:
    if resp is not None:
        await asyncio.shield(registry.start_cleanup(close_resource(resp)))


class _StreamRouteLease:
    """Idempotently release a committed stream route on every exit path."""

    def __init__(self, registry: Registry, router: Router, st: StationState,
                 route_id: int, resp: httpx.Response, stream_iter, permit: RoutePermit) -> None:
        self.registry = registry
        self.router = router
        self.st = st
        self.route_id = route_id
        self.resp = resp
        self.stream_iter = stream_iter
        self.permit = permit
        self.closed = False
        self.pending_read: asyncio.Future | None = None
        self._close_task: asyncio.Task | None = None

    def start_close(self, outcome: str = "cancelled",
                    error: str = "client_stream_closed") -> asyncio.Task:
        """Schedule cleanup on an independent task and return it.

        Cancellation of the calling ASGI task must not abort the upstream
        socket close, so the task is created directly on the loop rather than
        inside the response's (possibly cancelled) task group.
        """
        if self._close_task is None:
            self._close_task = self.registry.start_cleanup(
                self._close_now(outcome, error))
        return self._close_task

    async def _close_now(self, outcome: str, error: str) -> None:
        try:
            pending = self.pending_read
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                try:
                    async with asyncio.timeout(CLOSE_TIMEOUT):
                        await asyncio.gather(pending, return_exceptions=True)
                except TimeoutError:
                    pass
            await close_resource(self.stream_iter)
        finally:
            try:
                await close_resource(self.resp)
            finally:
                self.closed = True
                self.registry.end_route(self.route_id, outcome, error)
                self.router.release(self.st, self.permit)

    async def close(self, outcome: str = "cancelled",
                    error: str = "client_stream_closed") -> None:
        task = self.start_close(outcome, error)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise

    def abandon(self, loop: asyncio.AbstractEventLoop) -> None:
        """Best-effort safety net when a response is dropped before ASGI starts it."""
        if self._close_task is not None or loop.is_closed():
            return

        def schedule() -> None:
            if self._close_task is None:
                self._close_task = self.registry.start_cleanup(
                    self._close_now("cancelled", "response_dropped"))

        try:
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            pass


class _LeasedStreamingResponse(StreamingResponse):
    """Release the route even if ASGI send fails before/after yielding a chunk."""

    def __init__(self, *args, lease, **kwargs):
        super().__init__(*args, **kwargs)
        self.lease = lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.lease.close()


def _error_response(status: int, message: str, err_type: str = "upstream_error") -> JSONResponse:
    """OpenAI 兼容的错误响应格式。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": status}},
    )


def _error_sse(message: str, path="") -> bytes:
    """SSE 流中途出错的回补事件。"""
    obj = error_body(502, message, path)
    prefix = "event: error\n" if path == MESSAGES else ""
    return f"{prefix}data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


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


def _build_headers(st: StationState, client_headers=None, path=None) -> dict:
    return inference_headers(st.cfg, client_headers, path=path)


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
        if obj.get("error") or obj.get("type") == "error":
            return True
        if obj.get("type") == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("text") or delta.get("partial_json") or delta.get("thinking"):
                return True
        if obj.get("type") == "content_block_start" and (obj.get("content_block") or {}).get("type") in ("tool_use", "server_tool_use"):
            return True
        if obj.get("candidates"):
            candidate = obj["candidates"][0]
            if candidate.get("content", {}).get("parts") or candidate.get("finishReason"):
                return True
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
        native = native_text(obj)
        if native:
            return native
        if obj.get("type") == "content_block_delta":
            return obj.get("delta", {}).get("text", "")
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
    if s.startswith(("event:", "data:")):
        return any(_looks_like_error(line[5:].strip().encode()) for line in s.splitlines() if line.startswith("data:"))
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
    if isinstance(obj, list):
        return next((usage for item in reversed(obj) if (usage := _usage_from_payload(item)) is not None), None)
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("usageMetadata"), dict):
        meta = obj["usageMetadata"]
        inp = _first_usage_int(meta.get("promptTokenCount"))
        out = _first_usage_int(meta.get("candidatesTokenCount"))
        thoughts = _first_usage_int(meta.get("thoughtsTokenCount"))
        if thoughts is not None:
            out = (out or 0) + thoughts
        return UsageBreakdown(tokens=_first_usage_int(meta.get("totalTokenCount")),
                              input_tokens=inp, output_tokens=out,
                              cache_read=_first_usage_int(meta.get("cachedContentTokenCount")))
    if obj.get("type") == "message_start":
        obj = obj.get("message") or {}
    if obj.get("type") in ("message", "message_delta") and isinstance(obj.get("usage"), dict):
        raw = obj["usage"]
        usage = _parse_usage(raw)
        if usage is not None:
            # Anthropic input_tokens excludes both cache reads and cache writes.
            if usage.input_tokens is not None:
                usage.input_tokens += (usage.cache_read or 0) + (usage.cache_write or 0)
            usage.tokens = (usage.input_tokens + usage.output_tokens
                            if usage.input_tokens is not None and usage.output_tokens is not None else None)
        return usage
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
        self.error = ""
        self._anthropic_usage: dict = {}
        self.messages_finished = False
        self.gemini_candidates: set[int] = set()
        self.gemini_finished: set[int] = set()

    def _finish_event(self) -> int | None:
        if not self._data_lines:
            return None
        payload = "\n".join(self._data_lines)
        self._data_lines.clear()
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            obj = {}
        if isinstance(obj, dict) and obj.get("error"):
            err = obj["error"]
            self.error = str(err.get("message", "上游流式错误") if isinstance(err, dict) else err)[:500]
        if isinstance(obj, dict):
            kind = obj.get("type")
            if kind == "message_stop":
                self.messages_finished = True
            for position, candidate in enumerate(obj.get("candidates") or []):
                index = candidate.get("index", position)
                self.gemini_candidates.add(index)
                if candidate.get("finishReason"):
                    self.gemini_finished.add(index)
            if kind in ("message_start", "message_delta"):
                source = (obj.get("message") or {}) if kind == "message_start" else obj
                raw = source.get("usage")
                if isinstance(raw, dict):
                    self._anthropic_usage.update({key: value for key, value in raw.items() if value is not None})
                # Merge cumulative raw fields before adding cached input tokens.
                usage = _usage_from_payload({"type": "message", "usage": self._anthropic_usage})
            else:
                usage = _usage_from_payload(obj)
        else:
            usage = _usage_from_payload(obj)
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


class _NamespaceStreamScanner(_StreamUsageParser):
    """Learn tool namespaces from complete SSE frames without changing bytes."""
    def __init__(self, memory):
        super().__init__()
        self.memory = memory

    def _finish_event(self):
        payload = '\n'.join(self._data_lines)
        self._data_lines.clear()
        try:
            self.memory.remember_sse(json.loads(payload))
        except (ValueError, TypeError):
            pass
        return None

    def feed(self, chunk):
        super().feed(chunk)
        if len(self._buffer) + sum(map(len, self._data_lines)) > 2 * 1024 * 1024:
            self._buffer = ''
            self._data_lines.clear()


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
    scan = bytearray()
    while True:
        try:
            chunk = await asyncio.wait_for(stream_iter.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return buffered, False  # 上游直接结束且无内容
        except BaseException:
            raise
        buffered.append(chunk)
        scan.extend(chunk)
        if _chunk_has_content(bytes(scan)):
            return buffered, True
        # Retain only the partial event when the upstream fragments SSE frames.
        boundary = scan.rfind(b"\n\n")
        if boundary >= 0:
            del scan[:boundary + 2]
        if len(scan) > 2_000_000:
            raise httpx.StreamError("Upstream SSE event exceeds the buffer limit")
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
        self.namespace_memory = NamespaceMemory()
        self.api = APIRouter(prefix="/v1")
        self.api.add_api_route("/chat/completions", self.chat_completions, methods=["POST"])
        self.api.add_api_route("/completions", self.completions, methods=["POST"])
        self.api.add_api_route("/embeddings", self.embeddings, methods=["POST"])
        self.api.add_api_route("/responses", self.responses, methods=["POST"])
        self.api.add_api_route("/models", self.list_models, methods=["GET"])
        self.api.add_api_route("/messages", self.messages, methods=["POST"])
        self.google_api = APIRouter(prefix="/v1beta")
        self.google_api.add_api_route("/models", self.google_models, methods=["GET"])
        for api in (self.api, self.google_api):
            api.add_api_route("/models/{model}:generateContent", self.generate_content, methods=["POST"])
            api.add_api_route("/models/{model}:streamGenerateContent", self.stream_generate_content, methods=["POST"])

    async def _native_dispatch(self, request, path, model=None):
        result = await self._dispatch(request, path, model_override=model)
        if isinstance(result, JSONResponse) and result.status_code >= 400:
            payload = json.loads(result.body)
            message = (payload.get("error") or {}).get("message", "请求失败")
            return JSONResponse(status_code=result.status_code, content=error_body(result.status_code, message, path))
        return result

    async def messages(self, request: Request):
        return await self._native_dispatch(request, MESSAGES)

    async def generate_content(self, model: str, request: Request):
        return await self._native_dispatch(request, GENERATE, model)

    async def stream_generate_content(self, model: str, request: Request):
        alt = request.query_params.get("alt", "json")
        if alt not in ("json", "sse"):
            return JSONResponse(status_code=400, content=error_body(400, "alt 必须为 sse 或 json", GENERATE))
        path = STREAM_GENERATE + ("?alt=sse" if alt == "sse" else "")
        return await self._native_dispatch(request, path, model)

    async def google_models(self):
        models = sorted({model for st in self.registry.states.values()
                         for model in st.effective_models if st.supports_model(model, GENERATE)})
        return {"models": [{"name": "models/" + model, "displayName": model,
                             "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                            for model in models]}

    def _has_retryable_station(self, model: str, path: str | None = None) -> bool:
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
            if (self._station_matches_model(st, model, path)
                    or self._model_discovery_unresolved(st)):
                return True
        return False

    @staticmethod
    def _station_matches_model(st: StationState, model: str, path: str | None = None) -> bool:
        return st.supports_model(model, path)

    @staticmethod
    def _model_discovery_unresolved(st: StationState) -> bool:
        """Model discovery has not produced a trustworthy list yet."""
        return (
            st.cfg.probe_models
            and not st.probed_models
            and st.last_probe_ok is not True
        )

    def _routing_precondition_error(self, model: str, path: str | None = None) -> JSONResponse | None:
        """Classify deterministic no-route states before starting a round."""
        matching = [
            st for st in self.registry.states.values()
            if self._station_matches_model(st, model, path)
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
            f"模型 {model} 没有支持 {path or '当前协议'} 的站点，请检查模型目录与路由范围",
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
                          attempts: int, client_ip: str | None = None) -> JSONResponse:
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
            client_ip=client_ip,
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

    async def _dispatch(self, request: Request, path: str, model_override: str | None = None):
        peer = getattr(request, 'client', None)
        client_ip = peer.host if peer else None
        client_headers = request_context(getattr(request, 'headers', None))

        def failure(model, response, attempts):
            return self._terminal_failure(model, response, attempts, client_ip)

        try:
            body = await request.json()
        except Exception:
            return _error_response(400, "请求体不是合法的 JSON", "invalid_request_error")
        if not isinstance(body, dict):
            return _error_response(400, "请求体必须是 JSON 对象", "invalid_request_error")
        if model_override is not None:
            if (not model_override or any(ch in model_override for ch in "/\\?#%")
                    or model_override in (".", "..")):
                return _error_response(400, "Gemini 模型 ID 无效", "invalid_request_error")
            body["model"] = model_override
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            return _error_response(400, "model 必须是非空字符串", "invalid_request_error")
        if path == '/responses':
            self.namespace_memory.restore(body)

        stream = path != "/embeddings" and body.get("stream")
        if path in GEMINI_PATHS:
            stream = path.endswith("?alt=sse")
        max_retry_seconds = self.gateway.max_retry_seconds
        deadline = (time.monotonic() + max_retry_seconds) if max_retry_seconds else None
        round_index = 0
        total_attempts = 0
        # 仅瞬时失败继续下一轮；确定性错误立即返回。所有客户端均按
        # 网关显式重试配置执行，不再根据其私有重试头缩减重试预算。
        while self.gateway.max_rounds == 0 or round_index < self.gateway.max_rounds:
            if await request.is_disconnected():
                return self._client_disconnected_response()
            precondition_error = self._routing_precondition_error(model, path)
            if precondition_error is not None:
                return failure(model, precondition_error, total_attempts)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return failure(
                    model, self._retry_timeout_response(max_retry_seconds), total_attempts)
            try:
                forward = (self._forward_stream(path, body, model, client_ip, client_headers) if stream
                           else self._forward_json(path, body, model, client_ip, client_headers))
                if remaining is None:
                    result, has_transient = await forward
                else:
                    result, has_transient = await asyncio.wait_for(forward, remaining)
            except asyncio.TimeoutError:
                return failure(
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
                                return failure(
                                    model,
                                    self._retry_timeout_response(max_retry_seconds),
                                    total_attempts,
                                )
                            forward = self._forward_json(path, degraded, model, client_ip, client_headers)
                            if remaining is None:
                                result, _ = await forward
                            else:
                                result, _ = await asyncio.wait_for(forward, remaining)
                        except asyncio.TimeoutError:
                            return failure(
                                model,
                                self._retry_timeout_response(max_retry_seconds),
                                total_attempts,
                            )
                        if getattr(result, "status_code", 0) != 400:
                            return result
                return result
            if not has_transient:
                return failure(
                    model, result, total_attempts)  # 确定性失败：快速返回
            round_index += 1
            if self.gateway.max_rounds and round_index >= self.gateway.max_rounds:
                return failure(model, result, total_attempts)
            delay = self._round_delay(round_index - 1)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return failure(
                        model, self._retry_timeout_response(max_retry_seconds), total_attempts)
                delay = min(delay, remaining)
            if not await self._wait_for_retry(request, delay):
                return self._client_disconnected_response()
        return failure(
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
    def _local_pool_error(self, st: StationState, client: httpx.AsyncClient) -> UpstreamError:
        """PoolTimeout is local saturation, not evidence against the upstream.

        Rebuild the wedged client, but do not charge this to the station's
        breaker or success-rate statistics: the request never reached upstream.
        """
        self.registry.reset_client(st, expected_client=client)
        return UpstreamError(
            st.cfg.name, 0,
            "网关本地连接池已重置，本次请求未计入站点故障，请重试",
            retryable=True,
        )

    def _record_request_failure(self, st: StationState, err: UpstreamError):
        st.record_failure(err)
        if err.retryable and not err.deterministic and not err.quota and err.status_code != 429:
            st.mark_transient_failure(err, self.gateway, source='request')

    def _handle_failure(self, st: StationState, err: UpstreamError, retry_after: str | None = None) -> UpstreamError:
        """记录失败并按计费策略处理，返回 err 供调用方判断是否继续换站/重试。

        - quota 错误（余额不足/订阅到期）→ 标记 quota_exhausted，从路由排除，
          后台按来源自动探测恢复（/models 成功不视为有额度）
        - 429 限流 → 短冷却（rate_limit_cooldown 秒内不路由该站），
          订阅制站点尤其重要：避免重试风暴烧掉订阅配额
        """
        self._record_request_failure(st, err)
        if err.quota:
            st.mark_quota_exhausted(err)
            if self.notifier is not None:
                asyncio.create_task(self.notifier.send(
                    "station_quota", st.cfg.name,
                    f"配额问题: {err.message[:120]}（请检查对应计费入口与额度，恢复方式以站点策略为准）"))
        if err.status_code == 429:
            wait = _retry_after_seconds(retry_after)
            st.mark_rate_limited(max(self.gateway.rate_limit_cooldown, wait), reason=str(err),
                                source='request', wait_source='upstream' if wait > self.gateway.rate_limit_cooldown else 'gateway')
        return err

    # ------------------------------------------------------------------
    # 非流式转发（完整缓冲，可无缝切换）
    # ------------------------------------------------------------------
    async def _forward_json(self, path: str, body: dict, model: str, client_ip: str | None = None, client_headers=None):
        """非流式转发：完整缓冲 + 站级无缝切换。

        返回 (response, has_transient)：has_transient=True 表示本轮存在瞬时失败，
        值得整体重试一轮（fail-fast 判断依据）。
        """
        attempts: list[str] = []
        has_transient = False
        local_errors: list[str] = []
        candidates = self.router.pick(model, path)
        # No candidates can mean all matching stations are in a temporary
        # cooldown/rate-limit window.  Preserve that signal so _dispatch can
        # apply the configured round retry budget instead of returning 502
        # immediately (which otherwise leaves retries solely to the client).
        if not candidates:
            has_transient = self._has_retryable_station(model, path)

        for idx, st in enumerate(candidates):
            if idx >= self.gateway.max_attempts:
                break
            try:
                target_path, target_body, adapter = prepare_request(st, path, body)
                headers = _build_headers(st, client_headers, target_path)
                url = inference_url(st.cfg, target_path, model)
            except (MissingOpenCodeSession, UnsupportedWireRequest, ValueError) as exc:
                local_errors.append(str(exc))
                continue  # No upstream I/O, semaphore, or station-health failure.
            if attempts:
                await asyncio.sleep(self.gateway.retry_backoff)
            permit = await self.router.acquire(st)
            if permit is None:
                attempts.append(f"[{st.cfg.name}] 并发已满")
                has_transient = True  # 下一轮可能不忙
                continue
            route_id = self.registry.begin_route(st.cfg.name, model, client_ip)
            route_outcome = "failed"
            route_error = ""
            resp = None
            try:
                client = self.registry.ensure_client(st)
                timeout = httpx.Timeout(
                    connect=self.gateway.connect_timeout,
                    read=st.cfg.timeout,
                    write=30.0, pool=min(10.0, self.gateway.first_byte_timeout * .8),
                )
                t0 = time.perf_counter()
                req = client.build_request("POST", url, json=target_body,
                                           headers=headers, timeout=timeout)
                # send 会一直等到响应头返回：用首字节超时兜底卡死站点
                # （卡死 = 响应头都不回，超时后取消并切换下一站）
                resp = await asyncio.wait_for(
                    client.send(req, stream=True, follow_redirects=False),
                    timeout=self.gateway.first_byte_timeout,
                )

                protect_response(resp, self.registry.start_cleanup)
                if resp.status_code in (200, 201):
                    # 等首字节（卡死检测），之后缓冲剩余；顺带测量首字时间
                    t_first = time.perf_counter()
                    try:
                        stream_iter, first = await _read_first(
                            resp, timeout=self.gateway.first_byte_timeout)
                    except (asyncio.TimeoutError, httpx.TimeoutException):
                        await _shielded_close(resp, self.registry)
                        err = first_byte_error(st.cfg.name)
                        self._record_request_failure(st, err)
                        route_error = str(err)
                        attempts.append(f"[{st.cfg.name}] {STUCK_MSG}")
                        has_transient = True
                        continue
                    except httpx.TransportError as e:
                        await _shielded_close(resp, self.registry)
                        err = connect_error(st.cfg.name, type(e).__name__)
                        self._record_request_failure(st, err)
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
                        await _shielded_close(resp, self.registry)
                        err = timeout_error(st.cfg.name,
                                            f"读取中断: {type(e).__name__}")
                        self._record_request_failure(st, err)
                        route_error = str(err)
                        attempts.append(str(err))
                        has_transient = True
                        continue
                    await _shielded_close(resp, self.registry)

                    latency = (time.perf_counter() - t0) * 1000
                    # 非流式不记录 TTFT（响应体和响应头同时到达，无"首字"体感含义）
                    switched = bool(attempts)
                    st.record_success(latency / 1000, switched=switched)
                    detail = "成功" + (f" (经{len(attempts)}次切换)" if switched else "")
                    body_bytes = b"".join(chunks)
                    if target_path == '/responses':
                        try:
                            self.namespace_memory.remember_response(json.loads(body_bytes))
                        except (ValueError, UnicodeDecodeError):
                            pass
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
                                      client_ip=client_ip, route_id=route_id,
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
                await _shielded_close(resp, self.registry)
                err = classify_error(st.cfg.name, resp.status_code, payload)
                err = self._handle_failure(st, err, resp.headers.get("retry-after"))
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
                self._record_request_failure(st, err)
                route_error = str(err)
                attempts.append(f"[{st.cfg.name}] {STUCK_MSG}")
                has_transient = True
            except httpx.TimeoutException as e:
                if isinstance(e, httpx.PoolTimeout):
                    err = self._local_pool_error(st, client)
                else:
                    err = timeout_error(st.cfg.name)
                    self._record_request_failure(st, err)
                route_error = str(err)
                attempts.append(str(err))
                has_transient = True
            except httpx.TransportError as e:
                err = connect_error(st.cfg.name, type(e).__name__)
                self._record_request_failure(st, err)
                route_error = str(err)
                attempts.append(str(err))
                has_transient = True
            finally:
                if route_outcome == "failed" and not route_error:
                    route_error = "request_aborted"
                try:
                    await _shielded_close(resp, self.registry)
                finally:
                    self.registry.end_route(route_id, route_outcome, route_error)
                    self.router.release(st, permit)

        if local_errors and not attempts and not has_transient:
            return (_error_response(400, local_errors[0], "invalid_request_error"), False)
        msg = _all_failed_message(attempts)
        response = _annotate_attempts(_error_response(502, msg), len(attempts))
        return (response, has_transient)

    # ------------------------------------------------------------------
    # 流式转发（SSE 透传，首字节前可切换）
    # ------------------------------------------------------------------
    async def _forward_stream(self, path: str, body: dict, model: str, client_ip: str | None = None, client_headers=None):
        """流式转发：SSE 透传 + 首字节前无缝切换 + 长流保活。

        返回 (response, has_transient)，语义同 _forward_json。
        """
        attempts: list[str] = []
        has_transient = False
        local_errors: list[str] = []
        candidates = self.router.pick(model, path)
        if not candidates:
            has_transient = self._has_retryable_station(model, path)

        for idx, st in enumerate(candidates):
            if idx >= self.gateway.max_attempts:
                break
            try:
                target_path, target_body, adapter = prepare_request(st, path, body)
                headers = _build_headers(st, client_headers, target_path)
                url = inference_url(st.cfg, target_path, model)
            except (MissingOpenCodeSession, UnsupportedWireRequest, ValueError) as exc:
                local_errors.append(str(exc))
                continue  # No upstream I/O, semaphore, or station-health failure.
            if attempts:
                await asyncio.sleep(self.gateway.retry_backoff)
            permit = await self.router.acquire(st)
            if permit is None:
                attempts.append(f"[{st.cfg.name}] 并发已满")
                has_transient = True
                continue

            route_id = self.registry.begin_route(st.cfg.name, model, client_ip)
            route_outcome = "failed"
            route_error = ""
            transferred = False
            resp = None
            try:
                client = self.registry.ensure_client(st)
                timeout = httpx.Timeout(
                    connect=self.gateway.connect_timeout,
                    read=self.gateway.read_timeout,
                    write=30.0, pool=min(10.0, self.gateway.first_byte_timeout * .8),
                )
                t0 = time.perf_counter()
                req = client.build_request("POST", url, json=target_body,
                                           headers=headers, timeout=timeout)
                try:
                    # send 会一直等到响应头返回：用首字节超时兜底卡死站点
                    resp = await asyncio.wait_for(
                        client.send(req, stream=True, follow_redirects=False),
                        timeout=self.gateway.first_byte_timeout,
                    )
                except asyncio.TimeoutError:
                    err = first_byte_error(st.cfg.name)
                    self._record_request_failure(st, err)
                    route_error = str(err)
                    attempts.append(f"[{st.cfg.name}] {STUCK_MSG}")
                    has_transient = True
                    continue
                except httpx.TimeoutException as e:
                    if isinstance(e, httpx.PoolTimeout):
                        err = self._local_pool_error(st, client)
                    else:
                        err = timeout_error(st.cfg.name)
                        self._record_request_failure(st, err)
                    route_error = str(err)
                    attempts.append(str(err))
                    has_transient = True
                    continue
                except httpx.TransportError as e:
                    err = connect_error(st.cfg.name, type(e).__name__)
                    self._record_request_failure(st, err)
                    route_error = str(err)
                    attempts.append(str(err))
                    has_transient = True
                    continue

                protect_response(resp, self.registry.start_cleanup)
                # ---- 状态码检查（首字节之前，可切换） ----
                if resp.status_code != 200:
                    payload = await resp.aread()
                    err = classify_error(st.cfg.name, resp.status_code, payload)
                    err = self._handle_failure(st, err, resp.headers.get("retry-after"))
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
                    self._record_request_failure(st, err)
                    route_error = str(err)
                    attempts.append(f"[{st.cfg.name}] {STUCK_MSG}")
                    has_transient = True
                    continue
                except httpx.TransportError as e:
                    await stream_iter.aclose()
                    err = connect_error(st.cfg.name, type(e).__name__)
                    self._record_request_failure(st, err)
                    route_error = str(err)
                    attempts.append(str(err))
                    has_transient = True
                    continue
                first = buffered[0] if buffered else b""

                # ---- 首块校验（200 + error JSON 的情况） ----
                if _looks_like_error(b"".join(buffered)):
                    raw_error = b"".join(buffered)
                    for line in raw_error.splitlines():
                        if line.startswith(b"data:") and _looks_like_error(line[5:].strip()):
                            raw_error = line[5:].strip()
                            break
                    err = classify_error(st.cfg.name, 200, raw_error)
                    err = self._handle_failure(st, err, resp.headers.get("retry-after"))
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
                # A half-open trial must finish before reopening the station.
                # One content chunk followed by a broken stream is not recovery.
                if not permit.recovery:
                    st.record_success(latency / 1000, switched=switched)
                detail = "流式成功" + (f" (经{len(attempts)}次切换)" if switched else "")
                event_time = self.eventlog.add(
                    st.cfg.name, model, True, latency,
                    detail, attempts=len(attempts) + 1,
                    ttft_ms=ttft_ms, client_ip=client_ip, route_id=route_id)
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
                    registry, router, st, route_id, resp, stream_iter, permit)

                async def gen(resp=resp, st=st, prefetch=buffered,
                              stream_iter=stream_iter, route_id=route_id,
                              adapter=adapter, model=model):
                    translator = (ChatStreamToResponsesTranslator(model)
                                  if adapter == ("r2c",) else None)
                    stream_tokens = None
                    usage_parser = _StreamUsageParser()
                    namespace_scanner = _NamespaceStreamScanner(self.namespace_memory) if target_path == '/responses' else None
                    stream_outcome = "cancelled"
                    stream_error = "client_stream_closed"
                    pending_read = None
                    try:
                        for chunk in prefetch:
                            if namespace_scanner:
                                namespace_scanner.feed(chunk)
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
                                if pending_read is None:
                                    pending_read = asyncio.ensure_future(stream_iter.__anext__())
                                    lease.pending_read = pending_read
                                chunk = await asyncio.wait_for(
                                    asyncio.shield(pending_read), timeout=keepalive or None)
                                pending_read = None
                                lease.pending_read = None
                                if namespace_scanner:
                                    namespace_scanner.feed(chunk)
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
                                pending_read = None
                                lease.pending_read = None
                                if namespace_scanner:
                                    namespace_scanner.finish()
                                parsed_tokens = usage_parser.finish()
                                if parsed_tokens is not None:
                                    stream_tokens = parsed_tokens
                                incomplete = (
                                    target_path == MESSAGES and not usage_parser.messages_finished
                                    or target_path in GEMINI_PATHS and (
                                        not usage_parser.gemini_candidates
                                        or not usage_parser.gemini_candidates <= usage_parser.gemini_finished))
                                if incomplete and not usage_parser.error:
                                    usage_parser.error = "上游流提前结束，未收到完整结束事件"
                                    yield _error_sse(usage_parser.error, path)
                                stream_outcome = "stream_error" if usage_parser.error else "success"
                                stream_error = usage_parser.error
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
                            f"上游 {st.cfg.name} 流中断: {type(e).__name__}", path)
                    finally:
                        if permit.recovery:
                            if stream_outcome == "success":
                                st.record_success(latency / 1000, switched=switched)
                            elif stream_outcome == "stream_error":
                                self._record_request_failure(st, connect_error(st.cfg.name, stream_error))
                        # Bookkeeping only: the upstream socket must be closed
                        # from an independent task, because this generator may
                        # be running inside Starlette's cancelled task group.
                        usage = usage_parser.last
                        if usage is not None and usage.tokens is not None:
                            eventlog.add_tokens(
                                st.cfg.name, route_model, usage.tokens,
                                occurred_at=route_event_time,
                                input_tokens=usage.input_tokens,
                                cache_read=usage.cache_read,
                                cache_write=usage.cache_write, route_id=route_id)
                        elif stream_tokens is not None:
                            eventlog.add_tokens(
                                st.cfg.name, route_model, stream_tokens,
                                occurred_at=route_event_time, route_id=route_id)
                        # close() moves the socket close onto an independent
                        # task, so a cancelled caller cannot abort it; normal
                        # completion still waits for the route to be released.
                        await lease.close(stream_outcome, stream_error)

                response = _LeasedStreamingResponse(
                    gen(), media_type="text/event-stream", headers=dict(SSE_HEADERS), lease=lease)
                response._route_lease_finalizer = weakref.finalize(
                    response, lease.abandon, asyncio.get_running_loop())
                transferred = True
                return (response, False)
            except asyncio.CancelledError:
                route_outcome = "cancelled"
                route_error = "client_cancelled"
                raise
            except httpx.TimeoutException as e:
                err = (self._local_pool_error(st, client) if isinstance(e, httpx.PoolTimeout)
                       else timeout_error(st.cfg.name))
                if not isinstance(e, httpx.PoolTimeout):
                    self._record_request_failure(st, err)
                route_error = str(err)
                attempts.append(str(err))
                has_transient = True
            except httpx.TransportError as e:
                err = connect_error(st.cfg.name, type(e).__name__)
                self._record_request_failure(st, err)
                route_error = str(err)
                attempts.append(str(err))
                has_transient = True
            finally:
                if not transferred:
                    try:
                        await _shielded_close(resp, self.registry)
                    finally:
                        if route_outcome == "failed" and not route_error:
                            route_error = "request_aborted"
                        self.registry.end_route(route_id, route_outcome, route_error)
                        self.router.release(st, permit)

        # ---- 全部失败 ----
        if local_errors and not attempts and not has_transient:
            return (_error_response(400, local_errors[0], "invalid_request_error"), False)
        msg = _all_failed_message(attempts)
        response = _annotate_attempts(_error_response(502, msg), len(attempts))
        return (response, has_transient)
