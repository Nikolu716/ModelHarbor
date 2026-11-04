"""Opt-in, single-request visual tests, independent of health monitoring."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

from .health import _retry_after_seconds
from .protocols import inference_url, native_text

MAX_OUTPUT_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PROMPT_LENGTH = 12000
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
BUILTIN = {
    "id": "pelican-svg", "name": "鹈鹕骑自行车 · SVG", "builtin": True,
    "prompt": "Generate an SVG of a pelican riding a bicycle.",
}
PREVIEW_CSP = (
    "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
    "img-src data:; font-src 'none'; connect-src 'none'; media-src 'none'; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)


class _StaticMarkup(HTMLParser):
    """Conservative markup filter; iframe sandbox + CSP are mandatory too.

    HTMLParser is not a browser security boundary. The output is only ever
    embedded in an opaque-origin, scriptless iframe with a deny-by-default CSP.
    No active document, forms, external assets, or nested browsing contexts.
    """
    tags = set("div span p br hr h1 h2 h3 h4 h5 h6 main section article header footer "
               "aside nav figure figcaption pre code b strong i em small blockquote "
               "ul ol li table thead tbody tfoot tr th td caption colgroup col "
               "dl dt dd img style svg g path rect circle ellipse line polyline "
               "polygon text tspan defs lineargradient radialgradient stop "
               "clippath mask pattern title desc symbol use".split())
    drop = set("script iframe frame frameset object embed foreignobject template "
               "noscript xmp plaintext textarea noembed noframes".split())
    void = {"br", "hr", "img", "col"}
    attrs = set("id class style role aria-label width height viewbox xmlns d x y x1 "
                "y1 x2 y2 cx cy r rx ry points fill fill-rule fill-opacity stroke "
                "stroke-width stroke-linecap stroke-linejoin stroke-dasharray "
                "stroke-dashoffset stroke-opacity opacity transform font-family "
                "font-size font-weight text-anchor dominant-baseline dx dy "
                "offset stop-color stop-opacity gradientunits gradienttransform "
                "spreadmethod patternunits patterncontentunits patterntransform "
                "clippathunits maskunits maskcontentunits clip-path mask "
                "preserveaspectratio vector-effect color colspan rowspan alt".split())
    canonical = {s.lower(): s for s in (
        "viewBox", "preserveAspectRatio", "linearGradient", "radialGradient",
        "clipPath", "clipPathUnits", "gradientUnits", "gradientTransform",
        "spreadMethod", "patternUnits", "patternContentUnits", "patternTransform",
        "maskUnits", "maskContentUnits")}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output, self.stack, self.blocked = [], [], []

    def handle_starttag(self, tag, attrs):
        if self.blocked:
            if tag in self.drop:
                self.blocked.append(tag)
            return
        if tag in self.drop:
            self.blocked.append(tag)
            return
        if tag not in self.tags:
            return
        safe = []
        seen = set()
        for key, value in attrs:
            if value is None or key in seen:
                continue
            seen.add(key)
            if key in {"href", "xlink:href"} and tag == "use":
                if not re.fullmatch(r"#[\w.-]+", value):
                    continue
            elif key == "src" and tag == "img":
                if not re.fullmatch(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+", value):
                    continue
            elif key not in self.attrs:
                continue
            # Fragment paint references are useful for gradients; external CSS
            # resources are blocked by CSP, regardless of CSS escaping tricks.
            if key == "xmlns" and value != "http://www.w3.org/2000/svg":
                continue
            safe.append(f' {self.canonical.get(key, key)}="{html.escape(value, quote=True)}"')
        self.output.append(f'<{self.canonical.get(tag, tag)}{"".join(safe)}>')
        if tag not in self.void:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.pop()
            return
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.output.append(f'</{self.canonical.get(current, current)}>')
                if current == tag:
                    break

    def handle_data(self, data):
        if not self.blocked:
            self.output.append(data if self.stack and self.stack[-1] == "style"
                               else html.escape(data))

    def finish(self):
        while self.stack:
            self.output.append(f'</{self.canonical.get(self.stack[-1], self.stack[-1])}>')
            self.stack.pop()
        return "".join(self.output)


def extract_artifact(text: str) -> tuple[str, str]:
    """Prefer a complete HTML document; otherwise SVG or a fenced HTML fragment."""
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return "", ""
    fences = re.findall(r"```(?:html|svg|xml)?\s*\n([\s\S]*?)```", text, re.I)
    for candidate in [*fences, text]:
        match = re.search(r"(?:<!doctype\s+html[^>]*>\s*)?<html\b[\s\S]*?</html\s*>", candidate, re.I)
        if match:
            return match.group(), "html"
        match = re.search(r"<svg\b[\s\S]*?</svg\s*>", candidate, re.I)
        if match:
            return match.group(), "svg"
        stripped = candidate.strip()
        if candidate in fences and re.match(r"<(?:div|main|section|article|style|h[1-6])\b", stripped, re.I):
            return stripped, "html"
    return "", ""


def preview_document(artifact: str) -> str:
    parser = _StaticMarkup()
    parser.feed(artifact)
    parser.close()
    safe = parser.finish()
    return ('<!doctype html><html><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{PREVIEW_CSP}">'
            '<meta name="referrer" content="no-referrer">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>html,body{margin:0;min-height:100%;background:#fff;color:#222}'
            'body{box-sizing:border-box;padding:16px;font-family:system-ui,sans-serif}'
            'body>svg{display:block;width:100%;height:auto;max-height:calc(100vh - 32px)}'
            'img{max-width:100%}*{box-sizing:border-box}</style></head>'
            f'<body>{safe}</body></html>')


def station_signature(st):
    cfg = st.cfg
    data = [cfg.base_url, cfg.api_key, cfg.headers, cfg.wire_api, cfg.probe_chat_path, cfg.inject, cfg.provider, cfg.opencode_plan]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class VisualTestStore:
    """One SQLite database per account, with at most one result per station."""
    def __init__(self, directory):
        self.path = Path(directory) / "visual-tests.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS prompts (id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS results (station TEXT PRIMARY KEY, signature TEXT NOT NULL, summary TEXT NOT NULL, result TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS schedules (station TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def connect(self):
        return closing(sqlite3.connect(self.path, timeout=5))

    def prompts(self):
        with self.connect() as db:
            return [dict(BUILTIN), *[dict(id=row[0], name=row[1], prompt=row[2], builtin=False)
                                    for row in db.execute("SELECT id,name,prompt FROM prompts ORDER BY rowid")]]

    def save_prompt(self, body, prompt_id=None):
        if prompt_id == BUILTIN["id"]:
            raise HTTPException(409, "内置模板不可修改，可另存为自定义模板")
        if set(body) - {"name", "prompt"}:
            raise HTTPException(400, "不支持的模板字段")
        name, prompt = body.get("name"), body.get("prompt")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise HTTPException(400, "模板名称需为 1–60 个字符")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= MAX_PROMPT_LENGTH:
            raise HTTPException(400, "提示词需为 1–12000 个字符")
        with self.connect() as db, db:
            if prompt_id and not db.execute("SELECT 1 FROM prompts WHERE id=?", (prompt_id,)).fetchone():
                raise HTTPException(404, "模板不存在")
            if not prompt_id and db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] >= 32:
                raise HTTPException(409, "每个账号最多保存 32 个自定义模板")
            prompt_id = prompt_id or uuid.uuid4().hex
            db.execute("INSERT INTO prompts VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,prompt=excluded.prompt",
                       (prompt_id, name.strip(), prompt.strip()))
        return dict(id=prompt_id, name=name.strip(), prompt=prompt.strip(), builtin=False)

    def delete_prompt(self, prompt_id):
        if prompt_id == BUILTIN["id"]:
            raise HTTPException(409, "内置模板不可删除")
        with self.connect() as db, db:
            if not db.execute("DELETE FROM prompts WHERE id=?", (prompt_id,)).rowcount:
                raise HTTPException(404, "模板不存在")

    def save_result(self, st, result):
        summary = {key: result[key] for key in ("id", "station", "model", "created_at", "ok", "kind", "latency_ms", "usage", "http_status")}
        summary["preview_available"] = bool(result["artifact"])
        with self.connect() as db, db:
            db.execute("INSERT INTO results VALUES (?,?,?,?) ON CONFLICT(station) DO UPDATE SET signature=excluded.signature,summary=excluded.summary,result=excluded.result",
                       (st.cfg.name, station_signature(st), json.dumps(summary), json.dumps(result)))

    def summaries(self, registry):
        with self.connect() as db:
            # A result is historical evidence, not a connectivity cache. Keep it
            # across configuration changes, upgrades and restarts until replaced.
            return {name: json.loads(data) | {"configuration_changed": signature != station_signature(st)}
                    for name, signature, data in db.execute("SELECT station,signature,summary FROM results")
                    if (st := registry.get(name))}

    def result(self, st):
        with self.connect() as db:
            row = db.execute("SELECT signature,result FROM results WHERE station=?", (st.cfg.name,)).fetchone()
        if not row:
            return None
        result = json.loads(row[1])
        result["configuration_changed"] = row[0] != station_signature(st)
        # Always rebuild with the current sanitizer, including after upgrades.
        result["preview_html"] = preview_document(result["artifact"]) if result["artifact"] else ""
        return result

    def delete_result(self, name):
        with self.connect() as db, db:
            db.execute("DELETE FROM results WHERE station=?", (name,))

    def schedules(self):
        with self.connect() as db:
            return {name: json.loads(data) for name, data in db.execute("SELECT station,data FROM schedules")}

    def save_schedule(self, name, data):
        with self.connect() as db, db:
            db.execute("INSERT INTO schedules VALUES (?,?) ON CONFLICT(station) DO UPDATE SET data=excluded.data",
                       (name, json.dumps(data)))

    def delete_schedule(self, name):
        with self.connect() as db, db:
            db.execute("DELETE FROM schedules WHERE station=?", (name,))


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(p.get("text", "") for p in value if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _response_text(data):
    native = native_text(data)
    if native:
        return native
    if data.get("choices"):
        return _text((data["choices"][0].get("message") or {}).get("content"))
    return _text(data.get("output_text")) or "".join(
        _text(item.get("content")) for item in data.get("output", []) if isinstance(item, dict) and item.get("type") == "message")


def _usage(data):
    if data.get("type") in ("message", "message_start", "message_delta") or "usageMetadata" in data:
        from .proxy import _usage_from_payload
        usage = _usage_from_payload(data)
        return dict(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, total_tokens=usage.tokens) if usage else None
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    result = {}
    for target, keys in {"input_tokens": ("input_tokens", "prompt_tokens"),
                         "output_tokens": ("output_tokens", "completion_tokens"),
                         "total_tokens": ("total_tokens",)}.items():
        result[target] = next((usage[k] for k in keys if isinstance(usage.get(k), int)
                               and not isinstance(usage[k], bool) and usage[k] >= 0), None)
    if result["total_tokens"] is None and all(result[k] is not None for k in ("input_tokens", "output_tokens")):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result if any(v is not None for v in result.values()) else None


def decode_response(raw: bytes, content_type: str):
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" not in content_type and not text.lstrip().startswith(("data:", "event:")):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("上游返回的 JSON 不是对象")
        error = "上游返回错误" if data.get("error") or data.get("status") == "failed" else ""
        truncated = data.get("status") == "incomplete" or data.get("stop_reason") == "max_tokens" or any(
            x.get("finish_reason") == "length" for x in data.get("choices", [])) or any(
            x.get("finishReason") == "MAX_TOKENS" for x in data.get("candidates", []))
        if data.get("promptFeedback", {}).get("blockReason"):
            error = "上游内容过滤阻止了生成"
        return _response_text(data), _usage(data), truncated, error
    parts, final_text, usage, truncated, error, finished = [], "", None, False, "", False
    from .proxy import _StreamUsageParser
    native_usage = _StreamUsageParser()
    native_usage.feed(raw)
    native_usage.finish()
    for event in re.split(r"\r?\n\r?\n", text):
        payload = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
        if payload == "[DONE]":
            finished = True
            continue
        if not payload:
            continue
        data = json.loads(payload)
        if not isinstance(data, dict):
            continue
        kind = data.get("type", "")
        if kind == "content_block_delta" and data.get("delta", {}).get("type") == "text_delta":
            parts.append(_text(data["delta"].get("text")))
        if kind == "message_delta":
            truncated = data.get("delta", {}).get("stop_reason") == "max_tokens"
        if kind == "message_stop":
            finished = True
        for candidate in data.get("candidates", []):
            parts.append(native_text({"candidates": [candidate]}))
            if candidate.get("finishReason"):
                finished = True
                truncated = candidate["finishReason"] == "MAX_TOKENS"
        if kind == "response.output_text.delta":
            parts.append(_text(data.get("delta")))
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            response = data.get("response") or {}
            final_text = _response_text(response)
            usage = _usage(response) or usage
            truncated = kind == "response.incomplete"
            error = "上游生成失败" if kind == "response.failed" else error
            finished = True
        for choice in data.get("choices", []):
            parts.append(_text((choice.get("delta") or {}).get("content")))
            if choice.get("finish_reason"):
                finished = True
                truncated = choice["finish_reason"] == "length"
        usage = _usage(data) or usage
        if kind == "error" or data.get("error"):
            error = "上游返回流式错误"
    if not finished and not error:
        error = "上游流提前结束，结果可能不完整"
    if native_usage.last:
        usage = dict(input_tokens=native_usage.last.input_tokens, output_tokens=native_usage.last.output_tokens,
                     total_tokens=native_usage.last.tokens)
    return final_text or "".join(parts), usage, truncated, error


def response_diagnostics(raw: bytes, content_type: str):
    """Retain explicit termination and reasoning evidence without inferring it from totals."""
    text = raw.decode("utf-8", errors="replace")
    objects = []
    if "text/event-stream" in content_type or text.lstrip().startswith(("data:", "event:")):
        for event in re.split(r"\r?\n\r?\n", text):
            payload = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            try:
                objects.append(json.loads(payload))
            except ValueError:
                pass
    else:
        try:
            objects.append(json.loads(text))
        except ValueError:
            pass
    reasons, reasoning_tokens, reasoning_observed = [], None, False
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        obj = obj.get("response") if isinstance(obj.get("response"), dict) else obj
        usage = obj.get("usage") or obj.get("usageMetadata") or {}
        if isinstance(usage, dict):
            details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
            extra = usage.get("opencode") or {}
            for value in [usage.get("thoughtsTokenCount"), details.get("reasoning_tokens") if isinstance(details, dict) else None,
                          (extra.get("tokens") or {}).get("reasoning") if isinstance(extra, dict) else None]:
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    reasoning_tokens = value
        delta = obj.get("delta") or {}
        reasoning_observed |= isinstance(delta, dict) and delta.get("type") == "thinking_delta"
        if isinstance(delta, dict) and delta.get("stop_reason"):
            reasons.append(delta["stop_reason"])
        if obj.get("stop_reason"):
            reasons.append(obj["stop_reason"])
        if obj.get("status") == "incomplete":
            reasons.append((obj.get("incomplete_details") or {}).get("reason", "incomplete"))
        for choice in obj.get("choices", []):
            if choice.get("finish_reason"):
                reasons.append(choice["finish_reason"])
            message = choice.get("delta") or choice.get("message") or {}
            reasoning_observed |= bool(message.get("reasoning_content") or message.get("reasoning"))
        for candidate in obj.get("candidates", []):
            if candidate.get("finishReason"):
                reasons.append(candidate["finishReason"])
            reasoning_observed |= any(part.get("thought") for part in candidate.get("content", {}).get("parts", []))
    return {"finish_reasons": list(dict.fromkeys(str(reason)[:80] for reason in reasons)),
            "reasoning_tokens": reasoning_tokens, "reasoning_observed": bool(reasoning_observed)}


class VisualTestRunner:
    def __init__(self, registry, gateway, store):
        self.registry, self.gateway, self.store = registry, gateway, store
        self.busy = set()
        self.next_allowed = {}
        self.tasks = {}
        self.jobs = {}
        self.accepting = True

    def job(self, name, *, summary=False):
        job = self.jobs.get(name)
        if job and summary:
            return {key: value for key, value in job.items() if key != "settings"} | {"model": job["settings"]["model"]}
        return job

    def cancel_pending(self):
        self.accepting = False
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        return tasks

    def submit(self, st, body, client_ip, on_complete):
        """Reserve before returning 202; browser lifetime does not own the job."""
        settings = self.validate(st, body)
        self._reserve(st)
        name = st.cfg.name
        cfg = st.cfg
        job = dict(id=uuid.uuid4().hex, state="running", started_at=time.time(),
                   settings=settings, error="")
        self.jobs[name] = job
        started = False

        async def execute():
            nonlocal started
            started = True
            try:
                if self.registry.get(name) is not st or st.cfg is not cfg:
                    raise HTTPException(409, "提交后站点配置已变更，请确认后重新测试")
                result = await self.run(st, settings, client_ip, _reserved=True)
                job.update(state="completed" if result["ok"] else "failed", result_id=result["id"], error=result["error"])
                on_complete(name)
            except asyncio.CancelledError:
                job.update(state="cancelled", error="账号切换或服务停止，测试已结束")
                raise
            except Exception as exc:
                self.busy.discard(name)
                job.update(state="failed", error=str(exc.detail) if isinstance(exc, HTTPException) else "测试执行异常，请重新查看结果")

        task = asyncio.create_task(execute(), name=f"visual-test-{name}")
        self.tasks[name] = task

        def finished(done):
            # Also release reservations if cancelled before execute() starts.
            if self.tasks.get(name) is done:
                self.tasks.pop(name, None)
            if not started:
                self.busy.discard(name)
            if done.cancelled():
                job.update(state="cancelled", error="账号切换或服务停止，测试已结束")
            job["finished_at"] = time.time()

        task.add_done_callback(finished)
        return dict(running=True, job=job)

    def validate(self, st, body):
        if set(body) - {"model", "prompt", "template_name", "max_output_tokens", "protocol", "reasoning_effort"}:
            raise HTTPException(400, "不支持的测试字段")
        prompt = body.get("prompt", BUILTIN["prompt"])
        model = body.get("model") or st.selected_probe_model
        limit = None if st.cfg.provider == "chatgpt" else body.get("max_output_tokens", 12000)
        protocol = body.get("protocol", "auto")
        effort = body.get("reasoning_effort", "auto")
        template_name = body.get("template_name", "临时提示词")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= MAX_PROMPT_LENGTH:
            raise HTTPException(400, "提示词需为 1–12000 个字符")
        if not isinstance(model, str) or not model or len(model) > 200 or any(ord(c) < 32 for c in model):
            raise HTTPException(400, "请先读取站点模型，或在站点配置中补充模型")
        if model not in st.probe_model_candidates:
            raise HTTPException(400, "所选模型不在本站模型目录中，请刷新目录或手动补充")
        if st.cfg.provider != "chatgpt" and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
            raise HTTPException(400, "输出 Token 上限需为正整数，模型支持范围由上游校验")
        if protocol not in ("auto", "responses", "chat_completions", "chat_completions_legacy", "messages", "generate_content"):
            raise HTTPException(400, "不支持的测试协议")
        if st.cfg.provider in ("opencode", "anthropic", "gemini", "chatgpt") and protocol != "auto":
            path = {"responses": "/responses", "messages": "/messages", "generate_content": "/generateContent"}.get(protocol, "/chat/completions")
            if not st.supports_model(model, path):
                raise HTTPException(400, "所选协议与此模型不符，请使用自动协议")
        effective_protocol = protocol if protocol != "auto" else {"/messages": "messages", "/generateContent": "generate_content"}.get(st.generation_path(model), "auto")
        if effective_protocol in ("messages", "generate_content") and effort not in ("auto", "omit"):
            raise HTTPException(400, "原生协议的推理参数因模型而异，请选择上游默认或跟随站点配置")
        if not isinstance(effort, str) or effort not in REASONING_EFFORTS | {"auto", "omit"}:
            raise HTTPException(400, "不支持的推理深度")
        if not isinstance(template_name, str) or len(template_name) > 60:
            raise HTTPException(400, "模板名称过长")
        return dict(model=model, prompt=prompt.strip(), template_name=template_name,
                    max_output_tokens=limit, protocol=protocol, reasoning_effort=effort)

    def _reserve(self, st, *, quota_retry=False):
        now, name = time.time(), st.cfg.name
        if st.native_usage_limited:
            st.reconcile_usage_wait(self.registry.chatgpt.usage_view(st.cfg.chatgpt_account), now)
        if not self.accepting:
            raise HTTPException(409, "账号已切换，请刷新页面")
        if not st.enabled:
            raise HTTPException(409, "站点已停用，未发送请求")
        if st.quota_exhausted and not (quota_retry and st.cfg.provider == "chatgpt"):
            raise HTTPException(409, "站点额度不可用，请先确认额度恢复")
        last_retry = max(st.quota_at, st.quota_probe_at) + 60 if quota_retry else 0
        wait = max(st.rate_limit_until, st.quota_resume_at if st.native_usage_limited else 0,
                   self.next_allowed.get(name, 0), last_retry) - now
        if wait > 0:
            raise HTTPException(429, f"请等待 {math.ceil(wait)} 秒再测试", headers={"Retry-After": str(math.ceil(wait))})
        if st.status == "down" and now - st.down_since < self.gateway.cool_down:
            raise HTTPException(409, "站点仍在熔断冷却中，请稍后测试")
        sem = self.registry.ensure_semaphore(st)
        if (name in self.busy or len(self.busy) >= 2 or sem.locked() or st.recovery_inflight
                or st.generation_probe_lock.locked()
                or st.native_usage_limited and self.registry.subscription_recovery_busy(st)):
            raise HTTPException(409, "测试正在进行或站点并发已满，请稍后重试")
        self.busy.add(name)
        self.next_allowed[name] = now + 15

    async def retry_subscription(self, st, client_ip):
        """One explicit recovery request; keep the last visual artifact intact."""
        if st.cfg.provider != "chatgpt" or not st.quota_exhausted:
            raise HTTPException(409, "仅对接入受限的 ChatGPT 订阅执行手动验证")
        settings = self.validate(st, {"prompt": "Reply with only OK.", "template_name": "手动验证订阅接入"})
        self._reserve(st, quota_retry=True)
        name, task = st.cfg.name, asyncio.current_task()
        self.tasks[name] = task
        st.quota_probe_at = time.time()
        native_recovery = st.native_usage_limited
        if native_recovery:
            st.recovery_inflight = True
            st.quota_resume_at = max(st.quota_resume_at, st.quota_probe_at + st.native_retry_interval())
            st.quota_wait_source = "gateway"
        st._persist_state()
        try:
            result = await self.run(st, settings, client_ip, _reserved=True, _save_result=False)
            if st.quota_exhausted:
                st.quota_probe_at = time.time()
                st.quota_probe_error = result["error"]
                st._persist_state()
        finally:
            if native_recovery:
                st.recovery_inflight = False
            self.busy.discard(name)
            if self.tasks.get(name) is task:
                self.tasks.pop(name, None)
        return {key: result.get(key) for key in ("ok", "model", "http_status", "error", "error_code", "request_id")} | {
            "recovered": not st.quota_exhausted, "quota_exhausted": st.quota_exhausted,
            "quota_error": st.quota_error, "quota_at": st.quota_at,
            "next_allowed_at": max(self.next_allowed.get(st.cfg.name, 0), st.rate_limit_until, st.quota_resume_at),
            "protection": st.protection_view(self.gateway, time.time()),
        }

    async def run(self, st, body, client_ip, *, source="manual", _reserved=False, _save_result=True):
        if st.cfg.provider == "chatgpt" and source != "manual":
            raise HTTPException(400, "ChatGPT 订阅仅支持手动智商测试，不发送后台定时生成请求")
        settings = self.validate(st, body)
        if not _reserved:
            self._reserve(st)
        model, prompt, template_name, limit, protocol, effort = (
            settings[k] for k in ("model", "prompt", "template_name", "max_output_tokens", "protocol", "reasoning_effort"))
        now, name = time.time(), st.cfg.name
        sem = self.registry.ensure_semaphore(st)
        quota_revision = st.quota_revision
        cfg = st.cfg
        if protocol == "auto":
            protocol = cfg.wire_api if cfg.wire_api in ("responses", "chat_completions") else (
                {"/responses": "responses", "/messages": "messages", "/generateContent": "generate_content"}.get(st.generation_path(model), "chat_completions"))
        if protocol == "responses":
            path = "/responses"
            payload = {"model": model, "input": prompt, "max_output_tokens": limit, "store": False, "stream": True}
        elif protocol == "messages":
            path = "/messages"
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": limit, "stream": True}
        elif protocol == "generate_content":
            path = "/streamGenerateContent?alt=sse"
            payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": limit}}
        else:
            path = "/chat/completions"
            token_key = "max_tokens" if protocol == "chat_completions_legacy" else "max_completion_tokens"
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                       token_key: limit, "stream": True, "stream_options": {"include_usage": True}, "store": False}
        # Carry tuning parameters only; never inherit tools, a second prompt or
        # a different model/output limit from arbitrary station injection.
        for key in ("temperature", "top_p"):
            if key in cfg.inject and protocol != "generate_content":
                payload[key] = cfg.inject[key]
        reasoning = cfg.inject.get("reasoning")
        reasoning = dict(reasoning) if isinstance(reasoning, dict) else {}
        inherited = reasoning.get("effort", cfg.inject.get("reasoning_effort"))
        effective_effort = inherited if effort == "auto" else None if effort == "omit" else effort
        if protocol == "responses":
            if effort == "omit":
                reasoning = {}
            elif effective_effort is not None:
                reasoning["effort"] = effective_effort
            if reasoning:
                payload["reasoning"] = reasoning
        elif protocol in ("chat_completions", "chat_completions_legacy") and effective_effort is not None:
            payload["reasoning_effort"] = effective_effort
        elif protocol in ("messages", "generate_content"):
            effective_effort = None
            if effort == "auto":
                if protocol == "messages" and isinstance(cfg.inject.get("thinking"), dict):
                    payload["thinking"] = cfg.inject["thinking"]
                if protocol == "generate_content" and isinstance(cfg.inject.get("generationConfig"), dict):
                    allowed = ("temperature", "topP", "topK", "thinkingConfig")
                    payload["generationConfig"].update({k: v for k, v in cfg.inject["generationConfig"].items() if k in allowed})
        result = dict(id=uuid.uuid4().hex, station=name, model=model, protocol=protocol,
                      reasoning_effort=effort, effective_reasoning_effort=effective_effort,
                      prompt=prompt, template_name=template_name, max_output_tokens=limit,
                      created_at=now, client_ip=client_ip, source=source, ok=False, http_status=None,
                      usage=None, raw_text="", artifact="", kind="", error="", truncated=False,
                      error_code="", request_id="")
        started = time.perf_counter()
        cancelled = False
        try:
            async with sem, asyncio.timeout(120):
                client = self.registry.ensure_client(st)
                from .providers import inference_headers
                headers = inference_headers(cfg, test_session=f"modelharbor-visual-{result['id']}", path=path)
                if cfg.provider == "chatgpt":
                    from .chatgpt import subscription_request, authorized_headers
                    payload.pop("max_output_tokens", None)
                    if cfg.chatgpt_mode == "codex_cli":
                        from .codex_inference import text_request
                        payload = text_request(payload)
                    elif cfg.chatgpt_mode == "codex_relay":
                        from .codex_relay import relay_request
                        payload = relay_request(payload)
                    else:
                        payload = subscription_request(payload)
                    headers = await authorized_headers(self.registry, cfg, headers)
                async with client.stream("POST", inference_url(cfg, path, model), json=payload, headers=headers,
                                         follow_redirects=False, timeout=httpx.Timeout(connect=self.gateway.connect_timeout,
                                         read=120, write=15, pool=5)) as response:
                    result["http_status"] = response.status_code
                    result["request_id"] = response.headers.get("x-request-id", "")
                    if response.status_code == 429:
                        wait = max(60, _retry_after_seconds(response.headers.get("retry-after")))
                        self.next_allowed[name] = time.time() + wait
                        if cfg.provider == "chatgpt":
                            st.mark_rate_limited(wait, reason="手动智商测试收到上游限流", source="visual_test",
                                                 wait_source="upstream" if response.headers.get("retry-after") else "gateway")
                    if not 200 <= response.status_code < 300:
                        result["error"] = f"上游返回 HTTP {response.status_code}；未重试或切换线路"
                        error_raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            error_raw.extend(chunk[:max(0, 16384 - len(error_raw))])
                            if len(error_raw) >= 16384:
                                break
                        try:
                            error_data = json.loads(error_raw)
                            error_data = error_data.get("error", error_data)
                            detail = error_data.get("message", "") if isinstance(error_data, dict) else ""
                            if cfg.provider == "chatgpt" and isinstance(error_data, dict):
                                result["error_code"] = error_data.get("code") or error_data.get("type") or ""
                                from .errors import classify_error
                                upstream_error = classify_error(name, response.status_code, json.dumps({"error": error_data}))
                                upstream_error.retry_after_s = _retry_after_seconds(response.headers.get("retry-after"))
                                if upstream_error.quota:
                                    st.mark_quota_exhausted(upstream_error)
                            if isinstance(detail, str) and detail:
                                for secret in [cfg.api_key, *cfg.headers.values()]:
                                    if secret:
                                        detail = detail.replace(secret, "[redacted]")
                                result["error"] += "：" + detail[:500]
                        except (ValueError, TypeError, AttributeError):
                            pass
                    else:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise ValueError("上游响应超过 2 MB，已停止读取")
                            raw.extend(chunk)
                        content, usage, truncated, error = decode_response(bytes(raw), response.headers.get("content-type", ""))
                        if cfg.provider == "chatgpt":
                            from .chatgpt import completed_response
                            completed, subscription_error = completed_response(bytes(raw))
                            if subscription_error:
                                result["error_code"] = subscription_error.get("code") or subscription_error.get("type") or ""
                                error = subscription_error.get("message") or subscription_error.get("code") or "ChatGPT 响应未完成"
                                from .errors import classify_error
                                classified = classify_error(name, 200, json.dumps({"error": subscription_error}))
                                classified.retry_after_s = _retry_after_seconds(response.headers.get("retry-after"))
                                if classified.quota:
                                    st.mark_quota_exhausted(classified)
                        result["diagnostics"] = response_diagnostics(bytes(raw), response.headers.get("content-type", ""))
                        raw_response = raw.decode("utf-8", errors="replace")
                        for secret in [cfg.api_key, *cfg.headers.values()]:
                            if secret:
                                raw_response = raw_response.replace(secret, "[redacted]")
                        result["raw_response"] = raw_response[:262144]
                        result["raw_response_truncated"] = len(raw_response) > 262144
                        if len(content.encode()) > MAX_OUTPUT_BYTES:
                            raise ValueError("生成内容超过 512 KB，已停止预览")
                        result.update(raw_text=content, usage=usage, truncated=truncated, error=error)
                        artifact, kind = extract_artifact(content)
                        result.update(artifact=artifact, kind=kind, ok=bool(content) and not error)
                        if not content and not error:
                            result["error"] = ("输出达到 Token 上限，但上游未返回可见正文；本次没有可预览的 HTML / SVG"
                                               if truncated else "请求已结束，但上游未返回可见正文；本次没有可预览的 HTML / SVG")
        except asyncio.CancelledError:
            cancelled = True
            result["error"] = "账号切换或服务停止，测试已结束；上游可能已计费"
        except (TimeoutError, httpx.TimeoutException) as exc:
            if isinstance(exc, httpx.PoolTimeout):
                self.registry.reset_client(st, expected_client=client)
                result["error"] = "网关本地连接池等待超时，已重置；本次未发送到上游，请稍后手动重试"
            else:
                result["error"] = "测试超时，未自动重试；上游可能已计费"
        except httpx.TransportError:
            result["error"] = "上游连接中断，未自动重试；上游可能已计费"
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            result["error"] = str(exc) if type(exc) is ValueError else "上游返回格式无法解析"
        finally:
            self.busy.discard(name)
            self.next_allowed[name] = max(self.next_allowed.get(name, 0), time.time() + (60 if cfg.provider == "chatgpt" else 15))
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if self.registry.get(name) is not st or st.cfg is not cfg:
            raise HTTPException(409, "测试期间站点配置已变更，旧结果未保存；上游可能已计费")
        # A successful metadata read, an HTTP 200, or a partial SSE response is
        # not proof of recovery. Subscription success requires response.completed.
        # Do not clear a newer failure from a concurrent business request.
        if cfg.provider == "chatgpt" and result["ok"] and st.quota_exhausted and st.quota_revision == quota_revision:
            st.reset_quota()
        if _save_result:
            self.store.save_result(st, result)
        if cancelled:
            raise asyncio.CancelledError
        result["preview_html"] = preview_document(result["artifact"]) if result["artifact"] else ""
        return result


class VisualTestScheduler:
    """Account-scoped, serial scheduler. Saving/activating never sends a request.

    Completion-based intervals prevent catch-up bursts. Config revisions prevent
    a finished request from overwriting a newer plan. Switching accounts cancels
    in-flight automated tests, which may already have incurred upstream charges.
    """
    def __init__(self, registry, runner, store, *, clock=time.time):
        self.registry, self.runner, self.store, self.clock = registry, runner, store, clock
        self.active = False
        self.epoch = 0

    def view(self, name):
        plan = self.store.schedules().get(name)
        if not plan:
            return dict(enabled=False, interval_seconds=3600, settings=None, state="disabled", next_run_at=None)
        st = self.registry.get(name)
        state = ("disabled" if not plan["enabled"] else "paused" if plan.get("paused_reason")
                 else "inactive" if not self.active else "waiting" if st is None or not st.enabled
                 else "running" if name in self.runner.busy else "scheduled")
        public = {key: value for key, value in plan.items() if key not in {"signature", "revision"}}
        return {**public, "state": state, "next_run_at": plan.get("next_run_at") if state in {"scheduled", "running"} else None}

    def configure(self, st, body):
        if set(body) - {"enabled", "interval_seconds", "settings"} or type(body.get("enabled")) is not bool:
            raise HTTPException(400, "请明确设置是否开启自动测试")
        old = self.store.schedules().get(st.cfg.name, {})
        enabled = body["enabled"]
        if enabled and st.cfg.provider == "chatgpt":
            raise HTTPException(400, "ChatGPT 订阅仅支持手动智商测试，不开启定时生成")
        interval = body.get("interval_seconds", old.get("interval_seconds", 3600))
        if type(interval) is not int or not 300 <= interval <= 604800:
            raise HTTPException(400, "自动测试间隔需为 300–604800 秒（5 分钟至 7 天）")
        settings = body.get("settings", old.get("settings"))
        if enabled:
            if not isinstance(settings, dict):
                raise HTTPException(400, "开启自动测试前请保存模型和提示词")
            settings = self.runner.validate(st, settings)
        elif not old:
            settings = None
        else:
            settings = old.get("settings")
        plan = dict(enabled=enabled, interval_seconds=interval, settings=settings,
                    revision=uuid.uuid4().hex, signature=station_signature(st), failures=0,
                    paused_reason="", last_error="", last_run_at=old.get("last_run_at"),
                    next_run_at=self.clock() + interval if enabled else None)
        self.store.save_schedule(st.cfg.name, plan)
        return self.view(st.cfg.name)

    def activate(self):
        self.active = True
        self.runner.accepting = True
        self.epoch += 1
        for name, plan in self.store.schedules().items():
            if plan["enabled"] and not plan.get("paused_reason"):
                plan["next_run_at"] = self.clock() + plan["interval_seconds"]
                self.store.save_schedule(name, plan)

    def deactivate(self):
        self.active = False
        self.epoch += 1

    def defer_after_manual(self, name):
        plan = self.store.schedules().get(name)
        if plan and plan["enabled"] and not plan.get("paused_reason"):
            plan["next_run_at"] = max(self.clock() + plan["interval_seconds"], self.runner.next_allowed.get(name, 0))
            self.store.save_schedule(name, plan)

    async def tick(self):
        """At most one due station per tick; safe to drive with a fake clock."""
        epoch = self.epoch
        if not self.active:
            return False
        for name, plan in sorted(self.store.schedules().items(), key=lambda item: item[1].get("next_run_at") or 0):
            if not plan["enabled"] or plan.get("paused_reason") or (plan.get("next_run_at") or 0) > self.clock():
                continue
            st = self.registry.get(name)
            if st is None:
                self.store.delete_schedule(name)
                continue
            if st.cfg.provider == "chatgpt":
                plan["paused_reason"] = "ChatGPT 订阅仅手动测试，自动计划已暂停"
                self.store.save_schedule(name, plan)
                continue
            if station_signature(st) != plan["signature"]:
                plan["paused_reason"] = "站点连接或协议配置已变更，请确认参数并重新保存自动测试计划"
                self.store.save_schedule(name, plan)
                continue
            if not st.enabled or st.quota_exhausted or name in self.runner.busy:
                plan["next_run_at"] = self.clock() + plan["interval_seconds"]
                self.store.save_schedule(name, plan)
                continue
            # Persist the reservation before I/O; no immediate catch-up after a crash.
            plan["next_run_at"] = self.clock() + plan["interval_seconds"]
            self.store.save_schedule(name, plan)
            try:
                result = await self.runner.run(st, plan["settings"], None, source="automatic")
                plan["last_run_at"] = result["created_at"]
                code = result["http_status"]
                success = result["ok"] and not result["truncated"]
                error = result["error"] or ("输出不完整，请调整提示词或 Token 上限" if not success else "")
            except HTTPException as exc:
                code, success, error = exc.status_code, False, str(exc.detail)
                # Local cooling/capacity guards did not contact the upstream.
                if code in (409, 429):
                    plan["next_run_at"] = max(self.clock() + plan["interval_seconds"], self.runner.next_allowed.get(name, 0), st.rate_limit_until)
                    plan["last_error"] = error
                    current = self.store.schedules().get(name)
                    if self.epoch == epoch and current and current["revision"] == plan["revision"]:
                        self.store.save_schedule(name, plan)
                    return False
            except asyncio.CancelledError:
                raise
            except Exception:
                code, success, error = None, False, "自动测试执行异常，请查看服务日志或重新保存计划"
            current = self.store.schedules().get(name)
            if self.epoch != epoch or not current or current["revision"] != plan["revision"]:
                return True
            plan["last_error"] = error
            plan["failures"] = 0 if success else plan["failures"] + 1
            if not success and code is not None and 300 <= code < 500 and code not in (408, 409, 425, 429):
                plan["paused_reason"] = "上游拒绝了请求；请检查模型、协议、推理参数或鉴权后重新保存计划"
            elif plan["failures"] >= 3:
                plan["paused_reason"] = "连续 3 次未完成，已暂停以避免持续消耗；请检查结果后重新保存计划"
            delay = plan["interval_seconds"] * (2 ** min(plan["failures"], 3))
            plan["next_run_at"] = max(self.clock() + delay, self.runner.next_allowed.get(name, 0))
            self.store.save_schedule(name, plan)
            return True
        return False

    async def run(self):
        epoch = self.epoch
        while self.active and self.epoch == epoch:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A damaged/unavailable visual-test store must not stop routing
                # or the independent health monitor. Never log secrets/prompts.
                print("[visual-tests] 调度存储暂不可用，本轮跳过")
                await asyncio.sleep(45)
            # Global serial dispatch with spacing, including across due stations.
            await asyncio.sleep(15)


def create_visual_test_router(registry, gateway, store, runner, scheduler=None):
    api = APIRouter()
    scheduler = scheduler or VisualTestScheduler(registry, runner, store)

    def station(name):
        st = registry.get(name)
        if st is None:
            raise HTTPException(404, "站点不存在")
        return st

    @api.get("/visual-tests/prompts")
    async def prompts():
        return store.prompts()

    @api.post("/visual-tests/prompts", status_code=201)
    async def add_prompt(body: dict):
        return store.save_prompt(body)

    @api.put("/visual-tests/prompts/{prompt_id}")
    async def edit_prompt(prompt_id: str, body: dict):
        return store.save_prompt(body, prompt_id)

    @api.delete("/visual-tests/prompts/{prompt_id}")
    async def delete_prompt(prompt_id: str):
        store.delete_prompt(prompt_id)
        return {"ok": True}

    @api.get("/station/{name}/visual-test")
    async def result(name: str):
        st = station(name)
        return {"result": store.result(st), "running": name in runner.busy,
                "job": runner.job(name),
                "next_allowed_at": max(runner.next_allowed.get(name, 0), st.rate_limit_until),
                "schedule": scheduler.view(name)}

    @api.put("/station/{name}/visual-test/schedule")
    async def schedule(name: str, body: dict):
        return scheduler.configure(station(name), body)

    @api.get("/station/{name}/visual-test/schedule")
    async def get_schedule(name: str):
        station(name)
        return scheduler.view(name)

    @api.post("/station/{name}/visual-test", status_code=202)
    async def run(name: str, body: dict, request: Request):
        return runner.submit(station(name), body, request.client.host if request.client else None,
                             scheduler.defer_after_manual)

    @api.delete("/station/{name}/visual-test")
    async def clear(name: str):
        station(name)
        if name in runner.busy:
            raise HTTPException(409, "测试正在进行，请完成后再清除")
        store.delete_result(name)
        return {"ok": True}

    return api
