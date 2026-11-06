"""Optional official Codex app-server bridge for account limits and reset credits.

This app-server process owns native authentication and reads account metadata.
The optional protocol relay and CLI text adapter use this profile's protected
CODEX_HOME. Neither path reads the developer's unrelated Codex credentials.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .chatgpt import ChatGPTAuthError, _private_directory, _atomic

USAGE_INTERVAL = 900
USAGE_TIMEOUT = 45
USAGE_LOCK_TIMEOUT = 3
RPC_TIMEOUT = 30


class CodexAccountError(ChatGPTAuthError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def codex_environment(directory, *, account_metadata=False):
    """Isolate credentials; opt only account metadata into the service proxy."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("CODEX_") and key not in {
                       "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ACCESS_TOKEN",
                       "MODELHARBOR_CODEX_PROXY"}}
    environment["CODEX_HOME"] = str(directory)
    proxy = os.environ.get("MODELHARBOR_CODEX_PROXY", "").strip() if account_metadata else ""
    if proxy:
        try:
            parsed = urlsplit(proxy)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and parsed.port and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise CodexAccountError("Codex 代理地址无效，请检查 MODELHARBOR_CODEX_PROXY", "codex_proxy_invalid")
        # Lowercase variants take precedence in some HTTP stacks.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            environment[name] = proxy
    return environment


def _rpc_error(error):
    """Classify known CLI failures without exposing its raw response or tokens."""
    message = str(error.get("message", "")).lower()
    if error.get("code") == -32601:
        return CodexAccountError("当前 Codex CLI 尚不支持此账号接口，请升级 CLI", "codex_method_unsupported")
    if "workspace routing discovery timed out" in message:
        return CodexAccountError("Codex 工作空间连接超时，请检查部署主机到 OpenAI 的网络或代理；无需重新授权", "codex_workspace_timeout")
    if any(value in message for value in ("error sending request", "connection refused", "dns error", "timed out")):
        return CodexAccountError("Codex 连接官方服务失败，请检查部署主机的网络或代理后刷新；无需重新授权", "codex_network_error")
    if any(value in message for value in ("401", "unauthorized", "refresh_token_reused", "refresh_token_expired", "invalid_grant")):
        return CodexAccountError("Codex 账号授权已失效，请重新授权当前绑定的账号", "codex_auth_required")
    return CodexAccountError("Codex 账号接口暂时读取失败，请稍后刷新，暂不需要重新授权", "codex_rpc_error")


def codex_executable():
    configured = os.environ.get("MODELHARBOR_CODEX_PATH")
    candidates = [configured, shutil.which("codex.exe"), shutil.which("codex")]
    launcher = shutil.which("codex.cmd")
    if launcher:
        root = Path(launcher).parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
        candidates.extend(str(path) for path in root.glob("codex-win32-*/vendor/*/bin/codex.exe"))
    for candidate in candidates:
        if not candidate:
            continue
        executable = Path(candidate).resolve()
        if not executable.is_file() or executable.suffix.lower() in {".cmd", ".ps1", ".bat"}:
            continue
        if os.name == "nt":
            if executable.suffix.lower() != ".exe":
                continue
            if not _is_openai_signed(executable):
                continue
        return str(executable)
    raise ChatGPTAuthError(
        "此功能需要受信任的官方 Codex CLI。请将 OpenAI 签名的 codex.exe 安装到 Program Files，"
        "并设置 MODELHARBOR_CODEX_PATH 指向该文件。")


def _is_openai_signed(executable: Path) -> bool:
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        return False
    environment = {**os.environ, "MODELHARBOR_CODEX_VERIFY_PATH": str(executable)}
    script = r"""
$ErrorActionPreference='Stop'
$file=Get-Item -LiteralPath $env:MODELHARBOR_CODEX_VERIFY_PATH
$root=[Environment]::GetFolderPath('ProgramFiles').TrimEnd('\')
if (-not $file.FullName.StartsWith($root+'\', [StringComparison]::OrdinalIgnoreCase)) { exit 1 }
$s=Get-AuthenticodeSignature -LiteralPath $file.FullName
$publisher=if ($s.SignerCertificate) { $s.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false) } else { '' }
if ($s.Status -ne 'Valid' -or $publisher -notin @('OpenAI','OpenAI OpCo, LLC')) { exit 1 }
$trusted=@('S-1-5-18','S-1-5-32-544','S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
$writeMask=2 -bor 4 -bor 16 -bor 64 -bor 256 -bor 65536 -bor 262144 -bor 524288
$current=$file.FullName
while ($current) {
  $acl=Get-Acl -LiteralPath $current
  if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) { exit 1 }
  foreach ($rule in $acl.Access) {
    if ($rule.AccessControlType -ne 'Allow' -or ($rule.PropagationFlags -band 2)) { continue }
    if (-not ($rule.FileSystemRights -band $writeMask)) { continue }
    $sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($sid -notin $trusted) { exit 1 }
  }
  if ($current.TrimEnd('\') -eq $root) { exit 0 }
  $current=Split-Path -Path $current -Parent
}
exit 1
"""
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=environment, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class CodexAccount:
    def __init__(self, directory, expected_email):
        self.directory = Path(directory).resolve()
        self.expected_email = expected_email.strip().casefold()
        self.process = None
        self.reader = None
        self.counter = 0
        self.pending = {}
        self.start_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.cache = {"status": "disconnected", "reset_credits": None, "windows": [], "updated_at": 0}
        self.login = None
        self.next_refresh_at = 0
        self.failures = 0
        try:
            cached = json.loads((self.directory / "usage.json").read_text(encoding="utf-8"))
            if (isinstance(cached, dict) and isinstance(cached.get("windows"), list)
                    and str(cached.get("email", "")).casefold() == self.expected_email):
                self.cache.update(cached)
        except (OSError, ValueError, TypeError):
            pass

    def view(self):
        now = time.time()
        return {**self.cache, "stale": bool(self.cache.get("updated_at")) and (
            self.cache["status"] != "connected" or now - self.cache["updated_at"] >= USAGE_INTERVAL * 2),
            "manual_refresh_at": max(self.cache.get("checked_at", 0), self.cache.get("failed_at", 0)) + 60,
            "next_refresh_at": self.next_refresh_at}

    async def refresh_background(self):
        # An explicit device-code login opts this profile in. Never start a
        # process for a subscription that has not connected quota metadata.
        if (self.login and self.login.get("status") == "pending"
                or not (self.directory / "auth.json").is_file()
                or time.time() < self.next_refresh_at or self.operation_lock.locked()):
            return
        self.next_refresh_at = time.time() + USAGE_INTERVAL
        try:
            await self.refresh()
        except (ChatGPTAuthError, OSError) as exc:
            self.failures += 1
            self.next_refresh_at = time.time() + min(14400, USAGE_INTERVAL * 2 ** min(self.failures, 4))
            self._usage_failed(exc)

    async def start(self):
        async with self.start_lock:
            if (self.process and self.process.returncode is None
                    and self.reader and not self.reader.done()):
                return
            if self.process or self.reader:
                await self.close()
            _private_directory(self.directory)
            environment = codex_environment(self.directory, account_metadata=True)
            executable = await asyncio.to_thread(codex_executable)
            self.process = await asyncio.create_subprocess_exec(
                executable, "app-server", "--listen", "stdio://",
                "-c", 'cli_auth_credentials_store="file"',
                "-c", 'model_provider="openai"',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=self.directory, env=environment,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            self.reader = asyncio.create_task(self._read())
            try:
                await self.rpc("initialize", {"clientInfo": {"name": "ModelHarbor", "title": "ModelHarbor", "version": __version__},
                                              "capabilities": {"experimentalApi": True}}, start=False)
                await self._write({"method": "initialized"})
            except BaseException:
                await self.close()
                raise

    async def _write(self, message):
        async with self.write_lock:
            self.process.stdin.write(json.dumps(message).encode() + b"\n")
            await self.process.stdin.drain()

    async def rpc(self, method, params=None, *, start=True):
        if start:
            await self.start()
        self.counter += 1
        identifier = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self._write({"id": identifier, "method": method, "params": params or {}})
            result = await asyncio.wait_for(future, RPC_TIMEOUT)
            if not isinstance(result, dict):
                raise ChatGPTAuthError("Codex 账户接口返回了无效响应")
            return result
        except (TimeoutError, BrokenPipeError, ConnectionError) as exc:
            await self.close()
            raise CodexAccountError("Codex 账号连接超时或中断，请稍后刷新；无需重新授权", "codex_connection_error") from exc
        finally:
            self.pending.pop(identifier, None)

    async def _read(self):
        try:
            while raw := await self.process.stdout.readline():
                message = json.loads(raw)
                if "id" in message and message["id"] in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(_rpc_error(message.get("error") or {}))
                        else:
                            future.set_result(message.get("result") or {})
                elif message.get("method") == "account/login/completed":
                    params = message.get("params") or {}
                    if (self.login and self.login.get("status") in ("starting", "pending")
                            and (not self.login.get("login_id") or not params.get("loginId")
                                 or self.login['login_id'] == params['loginId'])):
                        self.login.update(status="completed" if params.get("success") else "failed",
                                          login_id=params.get("loginId") or self.login.get("login_id"))
        except (ValueError, OSError, asyncio.CancelledError):
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ChatGPTAuthError("Codex 账号连接已关闭"))

    async def _account(self):
        response = await self.rpc("account/read", {"refreshToken": False})
        account = response.get("account") or {}
        if account.get("type") != "chatgpt" or not isinstance(account.get("email"), str) or not account["email"].strip():
            raise ChatGPTAuthError("请先绑定 Codex 账号")
        if self.expected_email and account["email"].strip().casefold() != self.expected_email:
            await self.rpc("account/logout")
            raise ChatGPTAuthError("Codex 登录账号与本站 ChatGPT 账号不同，请使用同一个账号")
        return account

    async def inference_credentials(self):
        """Read this profile's native token; only official Codex may refresh it."""
        async with self.operation_lock:
            await self._account()
            def read():
                try:
                    record = json.loads((self.directory / "auth.json").read_text(encoding="utf-8"))
                    tokens = record.get("tokens") or {}
                    token, account_id = tokens.get("access_token"), tokens.get("account_id")
                    if (record.get("auth_mode") not in (None, "chatgpt") or not isinstance(token, str)
                            or not token or not isinstance(account_id, str) or not account_id):
                        raise ValueError()
                    import base64
                    payload = token.split(".")[1]
                    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                    expiry = claims.get("exp", 0)
                    import math
                    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                        raise ValueError()
                    return token, account_id, expiry
                except (OSError, ValueError, TypeError, IndexError, AttributeError):
                    raise ChatGPTAuthError("Codex 登录凭据不可用，请在用量面板检查官方登录") from None
            token, account_id, expiry = await asyncio.to_thread(read)
            if expiry <= time.time() + 90:
                # Never implement a second refresh-token lifecycle alongside CLI.
                await self.rpc("account/read", {"refreshToken": True})
                token, account_id, expiry = await asyncio.to_thread(read)
                if expiry <= time.time():
                    raise ChatGPTAuthError("Codex 授权已过期，请在用量面板重新授权")
            return token, account_id

    async def begin_login(self):
        async with self.operation_lock:
            if self.login and self.login.get("status") == "pending" and self.login.get("expires_at", 0) > time.time():
                return self.login
            if self.login and self.login.get("status") == "pending" and self.login.get("login_id"):
                await self.rpc("account/login/cancel", {"loginId": self.login['login_id']})
            self.login = {"status": "starting"}
            try:
                result = await self.rpc("account/login/start", {"type": "chatgptDeviceCode"})
                if (result.get("verificationUrl") != "https://auth.openai.com/codex/device"
                        or not isinstance(result.get("userCode"), str) or not result['userCode'].strip()
                        or not isinstance(result.get("loginId"), str) or not result['loginId']):
                    raise ChatGPTAuthError("官方未返回有效的设备验证码，请检查 Codex CLI 后重试")
            except BaseException:
                self.login = None
                raise
            completed = self.login.get('status') if (self.login.get('status') in ('completed', 'failed')
                and self.login.get('login_id') == result['loginId']) else 'pending'
            self.login = {"status": completed, "verification_url": result['verificationUrl'],
                          "user_code": result['userCode'], "login_id": result.get('loginId'),
                          "expires_at": time.time() + 900}
            return self.login

    async def cancel_login(self):
        async with self.operation_lock:
            if self.login and self.login.get('status') in ('starting', 'pending') and self.login.get('login_id'):
                try:
                    await asyncio.wait_for(self.rpc('account/login/cancel', {'loginId': self.login['login_id']}), 5)
                except (ChatGPTAuthError, OSError, TimeoutError):
                    pass
            self.login = {'status': 'cancelled'}
            await self.close()

    async def refresh(self):
        # CLI text generation owns this lock for its entire stream. A quota read
        # must not wait behind it indefinitely or terminate the active request.
        try:
            await asyncio.wait_for(self.operation_lock.acquire(), USAGE_LOCK_TIMEOUT)
        except TimeoutError as exc:
            raise CodexAccountError("此 Codex 账号正在处理其他操作，请稍后刷新用量", "codex_account_busy") from exc
        try:
            if self.login and self.login.get("status") == "failed":
                return self.login
            if time.time() - max(self.cache.get("checked_at", 0), self.cache.get("failed_at", 0)) < 60:
                return self.view()
            try:
                async with asyncio.timeout(USAGE_TIMEOUT):
                    return await self._refresh_usage()
            except TimeoutError as exc:
                await self.close()
                error = CodexAccountError("读取 Codex 用量超时，请检查部署主机的网络或代理后刷新；无需重新授权", "codex_usage_timeout")
                self._usage_failed(error)
                raise error from exc
            except (ChatGPTAuthError, OSError) as exc:
                self._usage_failed(exc)
                raise
        finally:
            self.operation_lock.release()

    def _usage_failed(self, error):
        self.cache.update(status="error", failed_at=time.time(),
                          error=str(error) if isinstance(error, ChatGPTAuthError) else "读取 Codex 用量失败，请检查本机 CLI 和网络后刷新",
                          error_code=getattr(error, "code", "codex_usage_error"))

    async def _refresh_usage(self):
        account = await self._account()
        result = await self.rpc("account/rateLimits/read")
        buckets = result.get("rateLimitsByLimitId") or {"codex": result.get("rateLimits") or {}}
        windows, limits = [], []
        for name, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            limit_id = bucket.get("limitId") or name
            limits.append({"limit_id": limit_id, "reached_type": bucket.get("rateLimitReachedType")})
            for key in ("primary", "secondary"):
                value = bucket.get(key)
                if isinstance(value, dict):
                    windows.append({"name": bucket.get("limitName") or name, "limit_id": limit_id, "window": key,
                                    "used_percent": value.get("usedPercent"), "duration_mins": value.get("windowDurationMins"),
                                    "resets_at": value.get("resetsAt")})
        credits = result.get("rateLimitResetCredits")
        self.cache.update(status="connected", plan=account.get("planType"), email=account.get("email"),
                          windows=windows, limits=limits, updated_at=time.time(), reset_credits=credits if isinstance(credits, dict) else None)
        self.cache["checked_at"] = time.time()
        self.cache.pop("error", None)
        self.cache.pop("error_code", None)
        self.cache.pop("failed_at", None)
        self.failures = 0
        self.next_refresh_at = time.time() + USAGE_INTERVAL
        _private_directory(self.directory)
        _atomic(self.directory / "usage.json", json.dumps(self.cache).encode())
        return self.view()

    async def consume(self, credit_id, idempotency_key):
        async with self.operation_lock:
            await self._account()
            result = await self.rpc("account/rateLimitResetCredit/consume", {"idempotencyKey": idempotency_key,
                **({"creditId": credit_id} if credit_id else {})})
            self.cache["checked_at"] = 0
            self.cache.pop("failed_at", None)
        # Refresh even when nothing was consumed; never infer a new quota window.
        try:
            await self.refresh()
        except ChatGPTAuthError as exc:
            return {"outcome": result.get("outcome"), "usage": self.view(), "refresh_error": str(exc)}
        return {"outcome": result.get("outcome"), "usage": self.view()}

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        self.process = None
        self.reader = None
