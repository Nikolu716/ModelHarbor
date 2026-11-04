"""协议适配层：OpenAI Responses API 与 Chat Completions API 之间翻译。

Codex 等新版客户端走 Responses 协议，而部分中转站只实现
chat/completions（或反之）。这里负责：

- 请求翻译：把入口请求体翻译成目标站能接受的协议
- 响应翻译：把目标站返回翻译回入口协议（非流式 + 流式 SSE）
- 参数注入：按站注入缺省参数（如 reasoning effort=max）

全部为纯函数 / 轻量对象，便于单测。
"""
from __future__ import annotations

import codecs
import json

RESPONSES = "responses"
CHAT = "chat_completions"
AUTO = "auto"


class UnsupportedWireRequest(ValueError):
    """A request cannot be translated without discarding client semantics."""


def validate_text_adapter(body: dict) -> None:
    # The legacy adapter handles stateless text only. Never silently discard
    # tool calls, server-side conversation IDs, images, or structured output.
    unsupported = ("tools", "tool_choice", "previous_response_id", "conversation",
                   "text", "background", "include")
    if any(body.get(key) for key in unsupported):
        raise UnsupportedWireRequest(
            "此模型使用 Chat Completions；Responses 文本适配不支持工具、服务端会话或结构化输出。"
            "请使用 /v1/chat/completions 原生接口，或选择支持 Responses 的模型。未向上游发送请求。")
    items = body.get("input")
    if isinstance(items, dict):
        items = [items]
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or item.get("type", "message") != "message":
                raise UnsupportedWireRequest("Responses 文本适配无法无损转发此输入项，请使用模型原生接口。")
            content = item.get("content", "")
            if isinstance(content, list) and any(
                    not isinstance(part, dict) or part.get("type") not in _TEXT_TYPES for part in content):
                raise UnsupportedWireRequest("Responses 文本适配不支持多模态输入，请使用模型原生接口。")

# 文本内容块类型：Responses 用 input_text/output_text，Anthropic 用 text
_TEXT_TYPES = ("input_text", "output_text", "text")


def inject_params(body: dict, inject: dict) -> dict:
    """把 inject 中的默认参数缺省注入到请求体（不覆盖用户显式值）。

    对 dict 值做浅层深度合并：用户已传 reasoning 时只补缺失子键，
    未传时整体注入（例如 reasoning: {effort: max}）。
    """
    if not inject or not isinstance(body, dict):
        return body
    merged = dict(body)
    for key, value in inject.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(value, dict) and isinstance(merged[key], dict):
            d = dict(value)
            d.update(merged[key])  # 用户值优先
            merged[key] = d
    return merged


def _content_list_to_text(parts) -> str:
    """Responses 的 content 列表 → 纯文本（忽略图片/工具等非文本块）。"""
    texts = []
    if not isinstance(parts, list):
        return ""
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("type") in _TEXT_TYPES:
            texts.append(p.get("text") or "")
    return "".join(texts)


def _input_to_messages(inp) -> list[dict]:
    """把 Responses 的 input 字段转成 chat 的 messages 列表。"""
    messages: list[dict] = []
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, dict):
        role = inp.get("role", "user")
        content = inp.get("content", "")
        if isinstance(content, list):
            content = _content_list_to_text(content)
        if content:
            messages.append({"role": role, "content": content})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            content = item.get("content", "")
            if isinstance(content, list):
                content = _content_list_to_text(content)
            if isinstance(content, str) and content:
                messages.append({"role": role, "content": content})
    return messages


def responses_to_chat(body: dict) -> dict:
    """Responses 请求体 → chat/completions 请求体。"""
    messages: list[dict] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    messages.extend(_input_to_messages(body.get("input")))

    chat: dict = {"model": body.get("model"), "messages": messages}
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    for key in ("temperature", "top_p", "stop", "stream",
                "reasoning", "tools", "tool_choice", "response_format"):
        if key in body:
            chat[key] = body[key]
    return chat


def chat_to_responses(obj: dict, model: str | None = None) -> dict:
    """chat/completions 非流式响应 → Responses 非流式响应。"""
    model = model or obj.get("model")
    choices = obj.get("choices") or []
    text = ""
    if choices:
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
    usage = obj.get("usage") or {}
    output = []
    if text:
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    return {
        "id": obj.get("id", "resp_translated"),
        "object": "response",
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def _sse_event(event: str, payload: dict) -> bytes:
    return ("event: %s\ndata: %s\n\n"
            % (event, json.dumps(payload, ensure_ascii=False))).encode("utf-8")


class ChatStreamToResponsesTranslator:
    """把 chat/completions 的 SSE 字节流增量翻译成 Responses 的 SSE 字节流。

    chat 流事件（data: {choices:[{delta:{content}}]} ... data: [DONE]）
    → Responses 流事件（response.created / output_text.delta / response.completed）。
    """

    def __init__(self, model: str):
        self.model = model
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._data_lines: list[str] = []
        self._started = False
        self._finished = False
        self._text_parts: list[str] = []

    def _emit_created(self) -> bytes:
        resp = {"id": "resp_" + self.model, "object": "response",
                "status": "in_progress", "model": self.model, "output": []}
        return _sse_event("response.created", {"type": "response.created", "response": resp})

    def _emit_delta(self, text: str) -> bytes:
        return _sse_event("response.output_text.delta",
                          {"type": "response.output_text.delta", "delta": text})

    def _emit_completed(self) -> bytes:
        output = []
        if self._text_parts:
            text = "".join(self._text_parts)
            output.append({"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": text, "annotations": []}]})
        resp = {"id": "resp_" + self.model, "object": "response",
                "status": "completed", "model": self.model, "output": output}
        return _sse_event("response.completed",
                          {"type": "response.completed", "response": resp})

    def _handle_data(self, data: str) -> bytes:
        data = data.strip()
        if data == "[DONE]":
            if not self._finished:
                self._finished = True
                return self._emit_completed()
            return b""
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return b""
        delta_text = ""
        choices = obj.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            delta_text = delta.get("content") or ""
        out = b""
        if not self._started:
            self._started = True
            out += self._emit_created()
        if delta_text:
            self._text_parts.append(delta_text)
            out += self._emit_delta(delta_text)
        return out

    def feed(self, chunk: bytes) -> bytes:
        self._buffer += self._decoder.decode(chunk, final=False)
        out = b""
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line == "":
                if self._data_lines:
                    out += self._handle_data("".join(self._data_lines))
                    self._data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                self._data_lines.append(line[5:].lstrip(" "))
        return out

    def finish(self) -> bytes:
        self._buffer += self._decoder.decode(b"", final=True)
        if self._buffer.strip():
            rest = self._buffer
            self._buffer = ""
            for line in rest.split("\n"):
                line = line.rstrip("\r")
                if line.startswith("data:"):
                    self._data_lines.append(line[5:].lstrip(" "))
        out = b""
        if self._data_lines:
            out += self._handle_data("".join(self._data_lines))
            self._data_lines = []
        if not self._finished:
            self._finished = True
            out += self._emit_completed()
        return out


# 回放输入项中携带 provider namespace 的调用类型（cockpit-tools #1856：
# 客户端回放时剥离 namespace，上游按默认 namespace 查找失败返回 HTTP 400）
_CALL_ITEM_TYPES = ("function_call", "custom_tool_call", "tool_call", "mcp_tool_call")


class NamespaceMemory:
    """记录上游响应中 call_id -> namespace，回放时补回被客户端剥离的 namespace。

    Codex 类客户端（cockpit-tools <= v1.3.16）在多轮对话回放
    function_call 输入项时会删除 namespace 字段，导致上游报
    "Missing namespace for function_call 'x'"（HTTP 400）。
    网关同时看到上游响应（含 namespace）与后续请求（不含 namespace），
    因此用 call_id 关联即可无损补回。
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._max = max_entries
        self._map: dict[str, str] = {}

    def remember(self, call_id: str, namespace: str) -> None:
        if not call_id or not namespace:
            return
        if call_id in self._map:
            return  # 首次观测为准：同一 call_id 的 namespace 不会变化
        if len(self._map) >= self._max:
            self._map.pop(next(iter(self._map)))  # dict 保持插入序，淘汰最旧
        self._map[call_id] = namespace

    def namespace_for(self, call_id: str) -> str | None:
        return self._map.get(call_id)

    def remember_item(self, item) -> None:
        if not isinstance(item, dict):
            return
        if item.get("type") not in _CALL_ITEM_TYPES:
            return
        ns = item.get("namespace")
        cid = item.get("call_id")
        if isinstance(ns, str) and isinstance(cid, str):
            self.remember(cid, ns)

    def remember_response(self, obj) -> None:
        """从非流式 Responses 响应对象中学习 namespace。"""
        if not isinstance(obj, dict):
            return
        output = obj.get("output")
        if isinstance(output, list):
            for item in output:
                self.remember_item(item)
        resp = obj.get("response")
        if isinstance(resp, dict):
            self.remember_response(resp)

    def remember_sse(self, payload) -> None:
        """从一条 SSE data 载荷中学习 namespace。"""
        if not isinstance(payload, dict):
            return
        if payload.get("type") in ("response.output_item.added",
                                   "response.output_item.done"):
            self.remember_item(payload.get("item"))
            return
        if payload.get("type") == "response.completed":
            self.remember_response(payload.get("response"))
            return
        self.remember_item(payload)

    def restore(self, body: dict) -> bool:
        """给回放的调用项补回缺失的 namespace，返回是否有改动。"""
        if not isinstance(body, dict):
            return False
        inp = body.get("input")
        if not isinstance(inp, list):
            return False
        changed = False
        for item in inp:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in _CALL_ITEM_TYPES:
                continue
            if item.get("namespace") is not None:
                continue
            cid = item.get("call_id")
            if not isinstance(cid, str):
                continue
            ns = self._map.get(cid)
            if ns:
                item["namespace"] = ns
                changed = True
        return changed


def prepare_request(st, path: str, body: dict, *, allow_tools: bool = False):
    """决定发往某站的目标路径 / 请求体 / 响应适配器。

    返回 (target_path, target_body, adapter)：
    - adapter == ("r2c",) 表示需要把 chat 响应翻译回 responses
    - adapter is None 表示直接透传
    """
    from .protocols import MESSAGES, GEMINI_PATHS, capability_path
    if st.cfg.provider == "chatgpt":
        if path != "/responses":
            raise UnsupportedWireRequest("ChatGPT 订阅站点请使用 /v1/responses 接口。")
        from .chatgpt import subscription_request
        return path, subscription_request(inject_params(body, st.cfg.inject)), None
    if path == MESSAGES or path in GEMINI_PATHS:
        if not st.supports_model(body.get("model", ""), capability_path(path)):
            raise UnsupportedWireRequest("此站点不支持当前原生协议")
        target = inject_params(body, getattr(st.cfg, "inject", None))
        if path in GEMINI_PATHS:
            target = {key: value for key, value in target.items() if key not in ("model", "stream")}
        return path, target, None
    entry = RESPONSES if path == "/responses" else CHAT
    wire = getattr(st.cfg, "wire_api", None) or AUTO
    if st.cfg.provider == "opencode":
        paths = st.model_api_paths.get(body.get("model"), [])
        if path in paths:
            wire = entry
        elif allow_tools and entry == RESPONSES and '/chat/completions' in paths:
            wire = CHAT
        else:
            raise UnsupportedWireRequest("此模型不支持当前请求协议，请按模型目录选择原生接口。")
    if wire == AUTO:
        wire = entry

    target_path, target_body, adapter = path, body, None
    if wire != entry:
        if entry == RESPONSES and wire == CHAT:
            target_path = "/chat/completions"
            if allow_tools:
                from .mapping_wire import responses_to_chat as mapped_responses_to_chat
                target_body = mapped_responses_to_chat(body)
            else:
                validate_text_adapter(body)
                target_body = responses_to_chat(body)
            adapter = ("r2c",)
        elif entry == CHAT and wire == RESPONSES:
            raise UnsupportedWireRequest("此站点使用 Responses，请使用 /v1/responses 接口。")

    target_body = inject_params(target_body, getattr(st.cfg, "inject", None))
    return target_path, target_body, adapter
