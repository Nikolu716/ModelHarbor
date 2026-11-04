"""Official Sign in with ChatGPT for a locally hosted gateway.

OAuth tokens live in protected, account-scoped runtime files, never StationConfig.
The default sharing route uses the public Responses API. Native modes require
an explicit separate Codex login; sharing tokens never cross that boundary.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from . import __version__

AUTHORITY = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
PROFILE_RE = re.compile(r"[a-f0-9]{32}")


class ChatGPTAuthError(ValueError):
    pass


def _private_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        # The interactive administrator and the Windows service share this store.
        # No credentials are granted to other local users through inherited ACLs.
        import subprocess
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r",
             "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise OSError("无法保护 ChatGPT 凭据目录，请检查服务运行权限")
    else:
        path.chmod(0o700)


def _atomic(path: Path, payload: bytes):
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _dpapi(payload: bytes, decrypt=False) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    # Machine binding allows LocalSystem service restarts to retain the session.
    flags = 1 if decrypt else 5
    if not function(ctypes.byref(source), None, None, None, None, flags, ctypes.byref(result)):
        raise OSError("ChatGPT 凭据加解密失败")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


class CredentialStore:
    def __init__(self, directory):
        self.directory = Path(directory) / "chatgpt"
        self._ready = False

    def prepare(self):
        if not self._ready:
            _private_directory(self.directory)
            self._ready = True

    def _crypt(self, value, decrypt=False):
        if os.name == "nt":
            return _dpapi(value, decrypt)
        from cryptography.fernet import Fernet
        self.prepare()
        key = self.directory / "credentials.key"
        if not key.exists():
            _atomic(key, Fernet.generate_key())
        codec = Fernet(key.read_bytes())
        return codec.decrypt(value) if decrypt else codec.encrypt(value)

    def path(self, profile):
        if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
            raise ChatGPTAuthError("ChatGPT 账号引用无效")
        return self.directory / (profile + ".credential")

    def read(self, profile):
        path = self.path(profile)
        if not path.exists():
            raise ChatGPTAuthError("请先通过官方页面连接 ChatGPT 账号")
        try:
            return json.loads(self._crypt(path.read_bytes(), True))
        except (OSError, ValueError, TypeError) as exc:
            raise ChatGPTAuthError("ChatGPT 授权记录不可读，请重新授权") from exc

    def write(self, profile, record):
        self.prepare()
        _atomic(self.path(profile), self._crypt(json.dumps(record).encode()))

    def host_id(self):
        self.prepare()
        path = self.directory / "host-id"
        if not path.exists():
            _atomic(path, ("urn:uuid:" + str(uuid.uuid4())).encode())
        return path.read_text().strip()

    def view(self, profile):
        if not profile:
            return {"connected": False, "status": "disconnected"}
        try:
            record = self.read(profile)
        except ChatGPTAuthError:
            return {"profile": profile, "connected": False, "status": "disconnected"}
        connected = bool(record.get("access_token") and DIRECT_SCOPE in record.get("scopes", []))
        return {"profile": profile, "email": record.get("email", ""),
                "connected": connected, "auth_type": record.get("auth_type", "shared"),
                "status": "native_only" if record.get("auth_type") == "codex" else record.get("status", "connected" if connected else "permission_required"),
                "expires_at": record.get("expires_at"), "usage_url": "https://chatgpt.com/settings/usage"}


class ChatGPTAccounts:
    def __init__(self, directory, *, transport=None):
        self.store = CredentialStore(directory)
        self.transport = transport
        self.attempts = {}
        self.locks = {}
        self.jwks = None
        self.jwks_at = 0
        self.codex = {}
        from .codex_bindings import CodexBindings
        self.bindings = CodexBindings(self)

    def profile_view(self, profile, mode="shared"):
        view = self.store.view(profile)
        empty = {**view, "codex_connected": False, "shared_connected": view['connected']}
        if not profile:
            return empty
        try:
            record = self.store.read(profile)
            native = self.store.directory / ("codex-" + profile) / "auth.json"
            connected = bool(record.get("email") and native.is_file())
            result = {**empty, "codex_connected": connected,
                      "plan": self.usage_view(profile).get("plan") or record.get("plan")}
            if mode != 'shared':
                result.update(connected=connected, status='connected' if connected else 'codex_login_required', auth_type='codex')
            return result
        except (ChatGPTAuthError, OSError):
            return empty if mode == 'shared' else {**empty, "connected": False, "status": "codex_login_required", "auth_type": "codex"}

    def codex_account(self, profile):
        from .codex_account import CodexAccount
        account = self.store.read(profile)
        if not isinstance(account.get("email"), str) or not account["email"].strip():
            raise ChatGPTAuthError("请先绑定 Codex 账号")
        if profile not in self.codex:
            self.store.prepare()
            directory = self.store.directory / ("codex-" + profile)
            self.codex[profile] = CodexAccount(directory, account.get("email", ""))
        return self.codex[profile]

    def usage_view(self, profile):
        empty = {"status": "disconnected", "windows": [], "reset_credits": None, "updated_at": 0}
        if not profile:
            return empty
        try:
            self.store.path(profile)  # Validate before building a profile path.
            directory = self.store.directory / ("codex-" + profile)
            if profile not in self.codex and not (directory / "auth.json").is_file():
                return empty
            return self.codex_account(profile).view()
        except (ChatGPTAuthError, OSError):
            return empty

    async def refresh_usage(self, profile):
        self.usage_view(profile)
        bridge = self.codex.get(profile)
        if bridge:
            await bridge.refresh_background()

    def _client(self):
        return httpx.AsyncClient(transport=self.transport, timeout=25, follow_redirects=False)

    async def _verify_id(self, token, client_id, nonce=None):
        import jwt
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in {"RS256", "ES256"}:
                raise ValueError("unsupported algorithm")
            async with self._client() as client:
                if not self.jwks or time.time() - self.jwks_at > 3600 or not any(
                        key.get("kid") == header.get("kid") for key in self.jwks):
                    response = await client.get(AUTHORITY + "/.well-known/openid-configuration")
                    response.raise_for_status()
                    configuration = response.json()
                    jwks_url = configuration.get("jwks_uri", "")
                    parsed = urlsplit(jwks_url)
                    if configuration.get("issuer") != AUTHORITY or parsed.scheme != "https" or parsed.hostname != "auth.openai.com" or parsed.username or parsed.password:
                        raise ValueError("invalid issuer")
                    response = await client.get(jwks_url)
                    response.raise_for_status()
                    self.jwks = response.json()["keys"]
                    self.jwks_at = time.time()
            key = next(key for key in self.jwks if key.get("kid") == header.get("kid"))
            claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=[header["alg"]],
                                issuer=AUTHORITY, audience=client_id, options={"require": ["exp", "sub", "iss", "aud"]})
            if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
                raise ValueError("nonce mismatch")
            return claims
        except (jwt.PyJWTError, ValueError, KeyError, StopIteration, TypeError) as exc:
            raise ChatGPTAuthError("OpenAI 登录身份验证失败，请重新发起授权") from exc

    async def start_login(self, profile=None):
        # One callback listener per account runtime, so repeated clicks do not leak ports.
        for attempt in self.attempts.values():
            if attempt["status"] == "pending":
                self.cancel(attempt["id"])
        profile = profile or uuid.uuid4().hex
        try:
            existing = self.store.read(profile)
        except ChatGPTAuthError:
            existing = {}
        if existing.get("auth_type") == "codex":
            # Switching to sharing OAuth must not overwrite a live native profile.
            profile, existing = uuid.uuid4().hex, {}
        attempt = dict(id=uuid.uuid4().hex, profile=profile, status="pending", error="",
                       state=secrets.token_urlsafe(32), nonce=secrets.token_urlsafe(32),
                       verifier=secrets.token_urlsafe(64), expires_at=time.time() + 600,
                       client_id=existing.get("client_id"), subject=existing.get("subject"))
        loop = asyncio.get_running_loop()
        manager = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Callback query strings contain one-time credentials.

            def do_GET(self):
                expected_host = "127.0.0.1:" + str(self.server.server_port)
                if self.headers.get("Host") != expected_host or len(self.path) > 16384 or urlsplit(self.path).path != "/auth/callback":
                    self.send_error(400)
                    return
                query = parse_qs(urlsplit(self.path).query)
                try:
                    future = asyncio.run_coroutine_threadsafe(manager.complete(attempt["id"], query), loop)
                    future.result(65)
                    message = "ChatGPT connected. You can return to ModelHarbor."
                    status = 200
                except Exception:
                    message = "Connection was not completed. Return to ModelHarbor for details."
                    status = 400
                payload = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                           "<title>ModelHarbor</title><style>body{font:16px system-ui;max-width:540px;margin:15vh auto;padding:24px;line-height:1.7;color:#243143}h1{font-size:24px}</style>"
                           "<h1>ModelHarbor</h1><p>" + message + "</p>").encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        attempt["server"] = server
        attempt["redirect_uri"] = f"http://127.0.0.1:{server.server_port}/auth/callback"
        parameters = dict(client_id=attempt["client_id"] or "dynamic_agent_client",
                          ext_agent_host_id=self.store.host_id(), response_type="code",
                          redirect_uri=attempt["redirect_uri"], scope=SCOPES, resource=RESOURCE,
                          state=attempt["state"], nonce=attempt["nonce"], code_challenge_method="S256",
                          code_challenge=base64.urlsafe_b64encode(hashlib.sha256(attempt["verifier"].encode()).digest()).decode().rstrip("="))
        if not attempt["client_id"]:
            parameters["agent_name_hint"] = "ModelHarbor"
        elif existing.get("email"):
            parameters["login_hint"] = existing["email"]
        if attempt["client_id"] and DIRECT_SCOPE not in existing.get("scopes", []):
            parameters["prompt"] = "consent"
        self.attempts[attempt["id"]] = attempt
        # Retain only a bounded set of non-secret completion records.
        for identifier in list(self.attempts)[:-16]:
            self.attempts.pop(identifier, None)
        threading.Thread(target=server.serve_forever, daemon=True, name="chatgpt-oauth").start()
        attempt["timer"] = loop.call_later(600, self.cancel, attempt["id"])
        return {**self.login_view(attempt["id"]), "auth_url": AUTHORITY + "/api/accounts/authorize?" + urlencode(parameters)}

    def login_view(self, identifier):
        attempt = self.attempts.get(identifier)
        if not attempt:
            raise ChatGPTAuthError("授权流程不存在或已过期，请重新发起")
        return {key: attempt.get(key) for key in ("id", "profile", "status", "error", "expires_at")}

    def cancel(self, identifier):
        attempt = self.attempts.get(identifier)
        if not attempt:
            return
        if attempt["status"] == "pending":
            attempt["status"] = "cancelled"
        timer = attempt.pop("timer", None)
        if timer:
            timer.cancel()
        server = attempt.pop("server", None)
        if server:
            # shutdown() blocks until serve_forever polls; keep it off the event loop.
            threading.Thread(target=lambda: (server.shutdown(), server.server_close()), daemon=True).start()
        for key in ("state", "nonce", "verifier"):
            attempt.pop(key, None)

    async def complete(self, identifier, query):
        attempt = self.attempts.get(identifier)
        state = query.get("state", [])
        if not attempt or attempt["status"] != "pending" or time.time() >= attempt["expires_at"] or len(state) != 1 or not secrets.compare_digest(state[0], attempt.get("state", "")):
            raise ChatGPTAuthError("授权状态验证失败或已过期")
        attempt["status"] = "exchanging"
        try:
            if query.get("error"):
                raise ChatGPTAuthError("官方授权未完成或已取消，请重新连接并允许使用 ChatGPT 订阅")
            client_ids, codes = query.get("client_id", []), query.get("code", [])
            if len(client_ids) > 1 or len(codes) != 1 or not codes[0]:
                raise ChatGPTAuthError("授权回调不完整")
            client_id = client_ids[0] if client_ids else attempt["client_id"]
            if not client_id or client_id == "dynamic_agent_client" or len(client_id) > 256 or (attempt["client_id"] and client_id != attempt["client_id"]):
                raise ChatGPTAuthError("授权客户端与所选账号不一致")
            try:
                previous = self.store.read(attempt["profile"])
            except ChatGPTAuthError:
                previous = {}
            self.store.write(attempt["profile"], {**previous, "client_id": client_id})
            async with self._client() as client:
                response = await client.post(AUTHORITY + "/api/accounts/oauth/token", data={
                    "grant_type": "authorization_code", "client_id": client_id, "code": codes[0],
                    "code_verifier": attempt["verifier"], "redirect_uri": attempt["redirect_uri"], "resource": RESOURCE})
            if response.status_code != 200:
                raise ChatGPTAuthError(f"OpenAI 授权交换失败（HTTP {response.status_code}），请重新授权")
            record = response.json()
            claims = await self._verify_id(record.get("id_token", ""), client_id, attempt["nonce"])
            if attempt["subject"] and claims["sub"] != attempt["subject"]:
                raise ChatGPTAuthError("登录的账号与原绑定账号不同，请新增站点连接另一个账号")
            record.update(client_id=client_id, subject=claims["sub"], issuer=claims["iss"],
                          email=claims.get("email", ""), scopes=str(record.get("scope", "")).split())
            self._validate_tokens(record)
            self.store.write(attempt["profile"], record)
            attempt["status"] = "completed" if DIRECT_SCOPE in record["scopes"] else "permission_required"
        except Exception as exc:
            attempt["status"] = "failed"
            attempt["error"] = str(exc) if isinstance(exc, ChatGPTAuthError) else "连接 OpenAI 授权服务失败，请稍后重新连接"
            raise ChatGPTAuthError(attempt["error"]) from exc
        finally:
            self.cancel(identifier)

    @staticmethod
    def _validate_tokens(record):
        expires = record.get("expires_in")
        if not isinstance(record.get("access_token"), str) or not record["access_token"] or str(record.get("token_type", "")).lower() != "bearer" or isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires) or not 0 < expires <= 86400:
            raise ChatGPTAuthError("OpenAI 返回了无效的授权凭据")
        record["expires_at"] = time.time() + expires
        record["status"] = "connected" if DIRECT_SCOPE in record.get("scopes", []) else "permission_required"

    async def access_token(self, profile):
        async with self.locks.setdefault(profile, asyncio.Lock()):
            record = self.store.read(profile)
            if DIRECT_SCOPE not in record.get("scopes", []) or not record.get("access_token"):
                raise ChatGPTAuthError("ChatGPT 订阅未授权或登录已失效，请重新连接")
            if record.get("expires_at", 0) > time.time() + 90:
                return record["access_token"]
            if not record.get("refresh_token"):
                raise ChatGPTAuthError("ChatGPT 授权已过期，请重新连接")
            async with self._client() as client:
                response = await client.post(AUTHORITY + "/api/accounts/oauth/token", data={
                    "grant_type": "refresh_token", "client_id": record["client_id"],
                    "refresh_token": record["refresh_token"], "resource": RESOURCE})
            if response.status_code != 200:
                try:
                    error = response.json().get("error")
                    error = error.get("code", error.get("type")) if isinstance(error, dict) else error
                except ValueError:
                    error = None
                if error in {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}:
                    record = {k: v for k, v in record.items() if k not in {"access_token", "refresh_token", "id_token"}}
                    record["status"] = "reauth_required"
                    self.store.write(profile, record)
                raise ChatGPTAuthError(f"ChatGPT 授权续期失败（HTTP {response.status_code}），请稍后重试或重新连接")
            replacement = response.json()
            if replacement.get("id_token"):
                claims = await self._verify_id(replacement["id_token"], record["client_id"])
                if claims["sub"] != record["subject"]:
                    raise ChatGPTAuthError("续期身份不一致，已停止使用新凭据")
            updated = {**record, **replacement, "scopes": str(replacement["scope"]).split() if "scope" in replacement else record["scopes"]}
            self._validate_tokens(updated)
            self.store.write(profile, updated)
            if DIRECT_SCOPE not in updated["scopes"]:
                raise ChatGPTAuthError("ChatGPT 订阅权限已被撤销，请重新授权")
            return updated["access_token"]

    async def logout(self, profile):
        async with self.locks.setdefault(profile, asyncio.Lock()):
            record = self.store.read(profile)
            confirmed = not record.get("refresh_token")
            if record.get("refresh_token"):
                try:
                    async with self._client() as client:
                        discovery = await client.get(AUTHORITY + "/.well-known/openid-configuration")
                        discovery.raise_for_status()
                        endpoint = discovery.json().get("revocation_endpoint", "")
                        if urlsplit(endpoint).scheme == "https" and urlsplit(endpoint).hostname == "auth.openai.com":
                            response = await client.post(endpoint, data={"token": record["refresh_token"], "token_type_hint": "refresh_token", "client_id": record["client_id"]})
                            confirmed = response.status_code == 200
                except (httpx.HTTPError, ValueError):
                    pass
            self.store.write(profile, {k: v for k, v in record.items() if k in {"client_id", "subject", "issuer", "email"}})
            return {"ok": True, "revocation_confirmed": confirmed}

    def pause(self):
        for identifier in list(self.attempts):
            self.cancel(identifier)
        self.bindings.pause()

    async def close(self):
        self.pause()
        await self.bindings.close()
        for bridge in self.codex.values():
            await bridge.close()


async def authorized_headers(registry, cfg, headers):
    if cfg.provider != "chatgpt":
        return headers
    if cfg.chatgpt_mode in ("codex_cli", "codex_relay"):
        context = {key: value for key, value in headers.items()
                   if key.lower() in {"session-id", "thread-id", "x-session-id", "x-codex-turn-state", "x-codex-turn-metadata"}}
        return {**context, "Content-Type": "application/json", "Accept": "text/event-stream"}
    token = await registry.chatgpt.access_token(cfg.chatgpt_account)
    return {"Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream", "User-Agent": "ModelHarbor/" + __version__}


def subscription_request(body):
    """Prepare the documented HTTP/SSE contract without discarding context/tools."""
    from .wire import UnsupportedWireRequest
    unsupported_context = ("previous_response_id", "conversation", "background")
    if any(body.get(key) for key in unsupported_context):
        raise UnsupportedWireRequest("ChatGPT 订阅接口需要客户端携带完整历史，不支持服务端会话或后台任务。")
    unsupported_fields = {"max_output_tokens", "max_tool_calls", "metadata", "moderation", "multi_agent", "prompt",
                          "prompt_cache_retention", "safety_identifier", "temperature", "top_logprobs", "top_p", "truncation", "user"}
    # Common output/sampling hints cannot be honored by this subscription route.
    # Reject semantic capabilities rather than silently deleting them.
    if any(body.get(key) for key in ("moderation", "multi_agent", "prompt")):
        raise UnsupportedWireRequest("ChatGPT 订阅接口不支持请求中的 moderation、multi_agent 或 prompt 功能。")
    target = {key: value for key, value in body.items() if key not in unsupported_fields | set(unsupported_context)}
    inputs = target.get("input", [])
    if isinstance(inputs, str):
        inputs = [{"role": "user", "content": inputs}]
    if isinstance(inputs, dict):
        inputs = [inputs]
    if not isinstance(inputs, list):
        raise UnsupportedWireRequest("ChatGPT 订阅接口的 input 必须是文本或消息数组。")
    target["input"] = [({**item, "role": "developer"} if isinstance(item, dict) and item.get("role") == "system" else item) for item in inputs]
    for tool in target.get("tools", []) or []:
        if not isinstance(tool, dict) or tool.get("type") not in {"namespace", "web_search", "web_search_preview"}:
            raise UnsupportedWireRequest("ChatGPT 订阅接口要求函数/自定义工具放在 namespace 中；不支持此托管工具。")
    target.update(store=False, stream=True)
    return target


def completed_response(payload):
    """Unwrap a forced upstream SSE stream for non-streaming Responses clients."""
    completed = None
    for block in payload.replace(b"\r\n", b"\n").split(b"\n\n"):
        data = b"\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith(b"data:"))
        if not data or data == b"[DONE]":
            continue
        event = json.loads(data)
        if event.get("type") in {"response.failed", "response.incomplete", "error"}:
            response = event.get("response") or event
            return None, response.get("error") or (event if event.get("type") == "error" else
                {"code": "incomplete_response", "message": "ChatGPT 响应未完成"})
        if event.get("type") == "response.completed":
            completed = event.get("response")
    if not isinstance(completed, dict):
        return None, {"code": "incomplete_response", "message": "ChatGPT 流在完成事件前中断"}
    return completed, None
