"""Optional official Codex app-server bridge for account limits and reset credits.

This process never runs a model turn. It uses its own protected CODEX_HOME and
never reads, changes, or logs the developer's existing Codex credentials.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import __version__
from .chatgpt import ChatGPTAuthError, _private_directory, _atomic

USAGE_INTERVAL = 900


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
        "读取重置卡需要受信任的官方 Codex CLI。请将 OpenAI 签名的 codex.exe 安装到 Program Files，"
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
        self.expected_email = expected_email.casefold()
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
            "manual_refresh_at": self.cache.get("checked_at", 0) + 60,
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
        except (ChatGPTAuthError, OSError):
            self.failures += 1
            self.next_refresh_at = time.time() + min(14400, USAGE_INTERVAL * 2 ** min(self.failures, 4))
            self.cache.update(status="error", error="暂时无法读取订阅用量，保留上次数据；可在详情中重新连接")

    async def start(self):
        async with self.start_lock:
            if self.process and self.process.returncode is None:
                return
            _private_directory(self.directory)
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ACCESS_TOKEN",
                                          "CODEX_API_KEY", "CODEX_REMOTE_TOKEN"}}
            environment["CODEX_HOME"] = str(self.directory)
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
            result = await asyncio.wait_for(future, 30)
            if not isinstance(result, dict):
                raise ChatGPTAuthError("Codex 账户接口返回了无效响应")
            return result
        except (TimeoutError, BrokenPipeError, ConnectionError) as exc:
            raise ChatGPTAuthError("Codex 账号接口暂不可用，请稍后重试") from exc
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
                            code = (message.get("error") or {}).get("code")
                            future.set_exception(ChatGPTAuthError(
                                "当前 Codex CLI 尚不支持此账号接口，请升级 CLI" if code == -32601 else "Codex 账号接口调用失败，请检查登录或稍后重试"))
                        else:
                            future.set_result(message.get("result") or {})
                elif message.get("method") == "account/login/completed":
                    success = (message.get("params") or {}).get("success", False)
                    self.login = {"status": "completed" if success else "failed"}
        except (ValueError, OSError, asyncio.CancelledError):
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ChatGPTAuthError("Codex 账号连接已关闭"))

    async def _account(self):
        response = await self.rpc("account/read", {"refreshToken": False})
        account = response.get("account") or {}
        if account.get("type") != "chatgpt" or not account.get("email"):
            raise ChatGPTAuthError("请先连接 Codex 用量与重置卡")
        if account["email"].casefold() != self.expected_email:
            await self.rpc("account/logout")
            raise ChatGPTAuthError("Codex 登录账号与本站 ChatGPT 账号不同，请使用同一个账号")
        return account

    async def begin_login(self):
        async with self.operation_lock:
            if self.login and self.login.get("status") == "pending" and self.login.get("expires_at", 0) > time.time():
                return self.login
            result = await self.rpc("account/login/start", {"type": "chatgptDeviceCode"})
            self.login = {"status": "pending", "verification_url": result.get("verificationUrl"),
                          "user_code": result.get("userCode"), "expires_at": time.time() + 900}
            return self.login

    async def refresh(self):
        async with self.operation_lock:
            if self.login and self.login.get("status") == "failed":
                return self.login
            if time.time() - self.cache.get("checked_at", 0) < 60:
                return self.view()
            account = await self._account()
            result = await self.rpc("account/rateLimits/read")
            buckets = result.get("rateLimitsByLimitId") or {"codex": result.get("rateLimits") or {}}
            windows = []
            for name, bucket in buckets.items():
                if not isinstance(bucket, dict):
                    continue
                for key in ("primary", "secondary"):
                    value = bucket.get(key)
                    if isinstance(value, dict):
                        windows.append({"name": bucket.get("limitName") or name, "window": key,
                                        "used_percent": value.get("usedPercent"), "duration_mins": value.get("windowDurationMins"),
                                        "resets_at": value.get("resetsAt")})
            credits = result.get("rateLimitResetCredits")
            self.cache.update(status="connected", plan=account.get("planType"), email=account.get("email"),
                              windows=windows, updated_at=time.time(), reset_credits=credits if isinstance(credits, dict) else None)
            self.cache["checked_at"] = time.time()
            self.cache.pop("error", None)
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
