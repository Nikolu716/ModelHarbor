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


def prepare_request(st, path: str, body: dict):
    """决定发往某站的目标路径 / 请求体 / 响应适配器。

    返回 (target_path, target_body, adapter)：
    - adapter == ("r2c",) 表示需要把 chat 响应翻译回 responses
    - adapter is None 表示直接透传
    """
    entry = RESPONSES if path == "/responses" else CHAT
    wire = getattr(st.cfg, "wire_api", None) or AUTO
    if wire == AUTO:
        wire = entry

    target_path, target_body, adapter = path, body, None
    if wire != entry:
        if entry == RESPONSES and wire == CHAT:
            target_path = "/chat/completions"
            target_body = responses_to_chat(body)
            adapter = ("r2c",)
        elif entry == CHAT and wire == RESPONSES:
            # 反向翻译暂未实现：chat 入口 + responses 站。当前无此场景，
            # 降级为直接透传（目标站若只支持 responses 会失败并切换）。
            target_path = "/responses"
            target_body = body
            adapter = None

    target_body = inject_params(target_body, getattr(st.cfg, "inject", None))
    return target_path, target_body, adapter
