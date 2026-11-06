"""Explicit, text-only Responses adapter for the official local Codex CLI.

Native authentication stays inside Codex. Never reuse SIWC tokens against a
different endpoint, silently change billing, or execute client-supplied tools.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from .wire import UnsupportedWireRequest


def text_request(body):
    if body.get("tools") or body.get("tool_choice") not in (None, "none"):
        raise UnsupportedWireRequest("官方 Codex CLI 模式目前仅支持文本输入输出，不支持客户端工具调用；请移除 tools 或选择原生共享接口。")
    for key in ("previous_response_id", "conversation", "background", "include", "text", "service_tier"):
        if body.get(key):
            raise UnsupportedWireRequest(f"官方 Codex CLI 文本模式不支持 {key}，请携带完整文本历史。")
    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise UnsupportedWireRequest("instructions 必须是文本")
    value = body.get("input")
    if isinstance(value, str):
        value = [{"role": "user", "content": value}]
    if not isinstance(value, list) or not value:
        raise UnsupportedWireRequest("官方 Codex CLI 需要非空文本 input")
    items = []
    for item in value:
        if (not isinstance(item, dict) or item.get("type", "message") != "message"
                or item.get("role") not in ("user", "assistant", "developer", "system")):
            raise UnsupportedWireRequest("官方 Codex CLI 文本模式不支持工具结果或多模态输入")
        content = item.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get("type") not in ("input_text", "output_text", "text")
                   or not isinstance(part.get("text"), str) for part in content):
                raise UnsupportedWireRequest("官方 Codex CLI 文本模式暂不支持图片、文件或其他非文本内容")
            content = "\n".join(part["text"] for part in content)
        if not isinstance(content, str):
            raise UnsupportedWireRequest("消息 content 必须是文本")
        items.append({"role": item["role"], "content": content})
    reasoning = body.get("reasoning") or {}
    if not isinstance(reasoning, dict) or set(reasoning) - {"effort"}:
        raise UnsupportedWireRequest("官方 Codex CLI 仅支持 reasoning.effort")
    effort = reasoning.get("effort")
    if effort is not None and effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        raise UnsupportedWireRequest("不支持的推理深度")
    model = body.get("model")
    if not isinstance(model, str) or not model or model.startswith("-") or any(ord(c) < 32 for c in model):
        raise UnsupportedWireRequest("官方 Codex CLI 模型 ID 无效")
    return {"model": body.get("model"), "input": items, "instructions": instructions or "",
            "reasoning": reasoning, "store": False, "stream": True}


def _event(kind, **data):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n\n").encode()


def _error(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {"message": value}
    if not isinstance(value, dict):
        value = {"message": "官方 Codex CLI 请求失败"}
    value = value.get("error", value)
    if not isinstance(value, dict):
        value = {"message": str(value)}
    return {"code": value.get("code") or "codex_cli_error", "message": str(value.get("message") or "官方 Codex CLI 请求失败")[:1000]}


class _CLIStream(httpx.AsyncByteStream):
    def __init__(self, accounts, cfg, body):
        self.accounts, self.cfg, self.body = accounts, cfg, body
        self.process = None
        self.stderr_task = None
        self.scratch = None
        self.closed = False
        self.close_task = None

    async def aclose(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        self.closed = True
        process = self.process
        if process and process.returncode is None:
            try:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"),
                        "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    await asyncio.wait_for(killer.wait(), 5)
                else:
                    process.kill()
                await asyncio.wait_for(process.wait(), 5)
            except (OSError, TimeoutError):
                if process.returncode is None:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 5)
        if self.stderr_task:
            try:
                await asyncio.wait_for(self.stderr_task, 3)
            except (TimeoutError, OSError):
                self.stderr_task.cancel()
        if self.scratch:
            self.scratch.cleanup()

    async def __aiter__(self):
        bridge = self.accounts.codex_account(self.cfg.chatgpt_account)
        async with bridge.operation_lock:
            try:
                async for chunk in self._iterate(bridge):
                    yield chunk
            finally:
                # Finish child cleanup before metadata reads or the next request
                # can access this login and its rotating native credentials.
                await self.aclose()

    async def _iterate(self, bridge):
        from .codex_account import codex_executable, codex_environment
        identifier = "resp_codex_" + uuid.uuid4().hex
        response = {"id": identifier, "object": "response", "created_at": int(time.time()),
                    "model": self.body["model"], "status": "in_progress", "output": []}
        yield _event("response.created", response=response)
        try:
            async with asyncio.timeout(min(self.cfg.timeout, 300)):
                await bridge._account()  # Verify the explicitly linked account before inference.
                await bridge.close()  # Prevent two native processes rotating the same login.
                executable = await asyncio.to_thread(codex_executable)
                environment = codex_environment(bridge.directory)
                self.scratch = tempfile.TemporaryDirectory(prefix="text-", dir=bridge.directory)
                scratch = self.scratch.name
                instructions = Path(scratch) / "instructions.txt"
                instructions.write_text("You are a text assistant accessed through ModelHarbor. Follow the supplied conversation and instructions. Return only the requested answer. Do not use tools or access local files.", encoding="utf-8")
                args = [executable, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                        "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
                        "--model", self.body["model"], "-C", scratch, "-c", 'model_provider="openai"',
                        "-c", 'web_search="disabled"', "-c", "model_instructions_file=" + json.dumps(str(instructions))]
                effort = self.body.get("reasoning", {}).get("effort")
                if effort:
                    args.extend(["-c", "model_reasoning_effort=" + json.dumps(effort)])
                for feature in ("shell_tool", "unified_exec", "multi_agent", "multi_agent_v2", "plugins", "apps",
                                "hooks", "computer_use", "browser_use", "browser_use_external", "in_app_browser",
                                "image_generation", "view_image", "code_mode_host", "workspace_dependencies", "skill_search", "tool_suggest"):
                    args.extend(["--disable", feature])
                args.append("-")
                self.process = await asyncio.create_subprocess_exec(
                    *args, env=environment, cwd=scratch, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=2 * 1024 * 1024,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                stderr = bytearray()
                async def drain_stderr():
                    while chunk := await self.process.stderr.read(4096):
                        stderr.extend(chunk)
                        del stderr[:-8192]
                self.stderr_task = asyncio.create_task(drain_stderr())
                prompt = json.dumps({"instructions": self.body["instructions"], "messages": self.body["input"]}, ensure_ascii=False)
                await self.process.stdin.drain()
                self.process.stdin.write(("Respond to the last user message in this conversation. Text output only; do not use tools.\n" + prompt).encode())
                await self.process.stdin.drain()
                self.process.stdin.close()
                error, usage, completed, texts, size = None, None, False, [], 0
                while raw := await self.process.stdout.readline():
                    size += len(raw)
                    if size > 2 * 1024 * 1024:
                        raise ValueError("官方 Codex 输出超过 2 MB，已停止读取")
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    kind = event.get("type")
                    if kind == "item.completed":
                        item = event.get("item") or {}
                        if item.get("type") == "agent_message":
                            text = item.get("text", "")
                            if isinstance(text, str):
                                texts.append(text)
                                yield _event("response.output_text.delta", response_id=identifier,
                                             item_id="msg_" + identifier, output_index=0, content_index=0, delta=text)
                        elif item.get("type") not in {"reasoning", "plan", "error", "warning"}:
                            raise ValueError("官方 Codex 尝试了文本模式不支持的操作，已停止本次请求")
                    elif kind in {"error", "turn.failed"}:
                        error = _error(event.get("error", event.get("message")))
                        response.update(status="failed", error=error)
                        yield _event("response.failed", response=response)
                        return  # Stop CLI retries at the first reported failure.
                    elif kind == "turn.completed":
                        completed, usage = True, event.get("usage") or {}
                code = await self.process.wait()
                await self.stderr_task
                if code != 0 or not completed or error:
                    # CLI diagnostics can include local paths; keep stderr off the API.
                    failure = error or {"code": "codex_cli_error", "message": "官方 Codex 未完成响应，请检查 CLI 版本、账号授权与模型目录"}
                    response.update(status="failed", error=failure)
                    yield _event("response.failed", response=response)
                    return
                output = "\n".join(texts)
                message = {"id": "msg_" + identifier, "type": "message", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": output, "annotations": []}]}
                normalized = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                              "input_tokens_details": {"cached_tokens": usage.get("cached_input_tokens", 0)},
                              "output_tokens_details": {"reasoning_tokens": usage.get("reasoning_output_tokens", 0)}}
                normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
                response.update(status="completed", output=[message], usage=normalized)
                yield _event("response.output_text.done", response_id=identifier, item_id=message["id"], output_index=0, content_index=0, text=output)
                yield _event("response.output_item.done", response_id=identifier, output_index=0, item=message)
                yield _event("response.completed", response=response)
        except (TimeoutError, OSError, ValueError) as exc:
            response.update(status="failed", error={"code": "codex_cli_error", "message": str(exc)[:500]})
            yield _event("response.failed", response=response)
        finally:
            await self.aclose()


class CodexCLITransport(httpx.AsyncBaseTransport):
    def __init__(self, accounts, cfg):
        self.accounts, self.cfg = accounts, cfg

    async def handle_async_request(self, request):
        if request.method == "GET" and request.url.path == "/v1/models":
            bridge = self.accounts.codex_account(self.cfg.chatgpt_account)
            async with bridge.operation_lock:
                await bridge._account()
                result = await bridge.rpc("model/list", {"limit": 100})
            return httpx.Response(200, json={"models": [{"slug": item.get("model") or item["id"],
                "display_name": item.get("displayName"), "visibility": "list"} for item in result.get("data", [])
                if not item.get("hidden") and (item.get("model") or item.get("id"))]})
        if request.method != "POST" or request.url.path != "/v1/responses":
            return httpx.Response(400, json={"error": {"message": "官方 Codex CLI 模式仅支持文本 Responses"}})
        try:
            body = text_request(json.loads(request.content))
        except (ValueError, TypeError) as exc:
            return httpx.Response(400, json={"error": {"message": str(exc)}})
        return httpx.Response(200, stream=_CLIStream(self.accounts, self.cfg, body), headers={"content-type": "text/event-stream"})
