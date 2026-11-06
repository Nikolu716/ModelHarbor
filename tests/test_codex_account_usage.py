"""Quota metadata failures, native process recovery and service networking."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.codex_account import CodexAccount, CodexAccountError, codex_environment, _rpc_error


class CodexUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = CodexAccount(Path(self.temp.name), "own@example.test")
        self.bridge.cache.update(status="connected", email="own@example.test", updated_at=time.time() - 300,
                                 windows=[{"used_percent": 49, "duration_mins": 300}])

    async def asyncTearDown(self):
        await self.bridge.close()
        self.temp.cleanup()

    async def test_explicit_proxy_is_scoped_to_metadata_child_and_keeps_auth_isolation(self):
        environment = {"MODELHARBOR_CODEX_PROXY": "http://127.0.0.1:9999",
                       "https_proxy": "http://old.invalid:80", "OPENAI_API_KEY": "private",
                       "OPENAI_BASE_URL": "https://unrelated.invalid", "CODEX_HOME": "other-profile",
                       "CODEX_REMOTE_TOKEN": "private", "PATH": "system-path"}
        with patch.dict(os.environ, environment, clear=True):
            result = codex_environment(self.bridge.directory, account_metadata=True)
            self.assertEqual(os.environ["https_proxy"], "http://old.invalid:80")
        self.assertEqual(result["CODEX_HOME"], str(self.bridge.directory))
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(result[key], "http://127.0.0.1:9999")
        self.assertNotIn("OPENAI_API_KEY", result)
        self.assertNotIn("OPENAI_BASE_URL", result)
        self.assertNotIn("CODEX_REMOTE_TOKEN", result)
        self.assertNotIn("MODELHARBOR_CODEX_PROXY", result)
        self.assertEqual(result["PATH"], "system-path")

    async def test_invalid_proxy_does_not_expose_its_credentials(self):
        with patch.dict(os.environ, {"MODELHARBOR_CODEX_PROXY": "http://user:secret@host:bad"}):
            with self.assertRaises(CodexAccountError) as caught:
                codex_environment(self.bridge.directory, account_metadata=True)
        self.assertEqual(caught.exception.code, "codex_proxy_invalid")
        self.assertNotIn("secret", str(caught.exception))

    async def test_known_cli_errors_have_specific_safe_messages(self):
        cases = [("workspace routing discovery timed out", "codex_workspace_timeout"),
                 ("failed to fetch codex rate limits: error sending request for url (private)", "codex_network_error"),
                 ("workspace routing discovery unauthorized (401)", "codex_auth_required"),
                 ("unknown upstream error private-token", "codex_rpc_error")]
        for message, code in cases:
            error = _rpc_error({"code": -32603, "message": message})
            self.assertEqual(error.code, code)
            self.assertNotIn("private", str(error))
        self.assertEqual(_rpc_error({"code": -32601}).code, "codex_method_unsupported")

    async def test_failed_read_preserves_data_throttles_repeats_and_can_recover(self):
        b = self.bridge
        original_time = b.cache["updated_at"]
        b._account = AsyncMock(side_effect=_rpc_error({"message": "workspace routing discovery timed out"}))
        with self.assertRaises(CodexAccountError):
            await b.refresh()
        failed = b.view()
        self.assertEqual(failed["error_code"], "codex_workspace_timeout")
        self.assertEqual(failed["updated_at"], original_time)
        self.assertEqual(failed["windows"][0]["used_percent"], 49)
        self.assertTrue(failed["stale"])
        self.assertNotIn("checked_at", failed)
        self.assertGreater(failed["manual_refresh_at"], time.time())
        self.assertEqual((await b.refresh())["status"], "error")
        b._account.assert_awaited_once()
        b.cache["failed_at"] = time.time() - 61
        b._account = AsyncMock(return_value={"email": "own@example.test", "planType": "plus"})
        b.rpc = AsyncMock(return_value={"rateLimits": {"primary": {"usedPercent": 50, "windowDurationMins": 300}}})
        recovered = await b.refresh()
        self.assertEqual(recovered["status"], "connected")
        self.assertEqual(recovered["windows"][0]["used_percent"], 50)
        self.assertNotIn("error", recovered)
        self.assertNotIn("failed_at", recovered)
        b.rpc.assert_awaited_once_with("account/rateLimits/read")

    async def test_background_keeps_specific_failure_and_backs_off(self):
        b = self.bridge
        (b.directory / "auth.json").write_text("{}", encoding="utf-8")
        b._account = AsyncMock(side_effect=_rpc_error({"message": "workspace routing discovery timed out"}))
        await b.refresh_background()
        self.assertEqual(b.view()["error_code"], "codex_workspace_timeout")
        self.assertIn("工作空间连接超时", b.view()["error"])
        self.assertGreater(b.next_refresh_at, time.time() + 1700)
        await b.refresh_background()
        b._account.assert_awaited_once()

    async def test_busy_generation_does_not_hold_quota_dialog_or_get_cancelled(self):
        b = self.bridge
        await b.operation_lock.acquire()
        b._account = AsyncMock()
        with patch("app.codex_account.USAGE_LOCK_TIMEOUT", .01), patch.object(b, "close", new_callable=AsyncMock) as close:
            with self.assertRaises(CodexAccountError) as caught:
                await b.refresh()
            self.assertEqual(caught.exception.code, "codex_account_busy")
            self.assertTrue(b.operation_lock.locked())
            b._account.assert_not_awaited()
            close.assert_not_awaited()
        b.operation_lock.release()

    async def test_total_read_timeout_preserves_cache_and_releases_lock(self):
        b = self.bridge
        async def stalled():
            await asyncio.Event().wait()
        b._account = stalled
        with patch("app.codex_account.USAGE_TIMEOUT", .01), patch.object(b, "close", new_callable=AsyncMock) as close:
            with self.assertRaises(CodexAccountError) as caught:
                await b.refresh()
            self.assertEqual(caught.exception.code, "codex_usage_timeout")
            close.assert_awaited_once()
        self.assertFalse(b.operation_lock.locked())
        self.assertEqual(b.view()["windows"][0]["used_percent"], 49)

    async def test_dead_reader_is_replaced_even_when_child_is_alive(self):
        b = self.bridge
        b.process = SimpleNamespace(returncode=None)
        b.reader = asyncio.create_task(asyncio.sleep(0))
        await b.reader
        async def close_old():
            b.process = None
            b.reader = None
        with patch.dict(os.environ, {"MODELHARBOR_CODEX_PROXY": "http://127.0.0.1:9999"}), \
                patch.object(b, "close", side_effect=close_old) as close, \
                patch("app.codex_account.codex_executable", return_value="trusted-codex"), \
                patch("app.codex_account.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn, \
                patch.object(b, "rpc", new_callable=AsyncMock), patch.object(b, "_write", new_callable=AsyncMock), \
                patch.object(b, "_read", new_callable=AsyncMock):
            await b.start()
            close.assert_awaited_once()
            spawn.assert_awaited_once()
            self.assertEqual(spawn.call_args.kwargs["env"]["CODEX_HOME"], str(b.directory))
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                self.assertEqual(spawn.call_args.kwargs["env"][key], "http://127.0.0.1:9999")
            await b.reader
        b.process = None

    async def test_rpc_timeout_closes_child_and_removes_pending_request(self):
        b = self.bridge
        with patch("app.codex_account.RPC_TIMEOUT", .01), \
                patch.object(b, "_write", new_callable=AsyncMock), patch.object(b, "close", new_callable=AsyncMock) as close:
            with self.assertRaises(CodexAccountError) as caught:
                await b.rpc("account/read", start=False)
            self.assertEqual(caught.exception.code, "codex_connection_error")
            close.assert_awaited_once()
        self.assertEqual(b.pending, {})

    async def test_reader_retains_actionable_error_category(self):
        b = self.bridge
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({"id": 1, "error": {"code": -32603,
                         "message": "workspace routing discovery timed out"}}).encode() + b"\n")
        reader.feed_eof()
        b.process = SimpleNamespace(stdout=reader, returncode=0)
        future = asyncio.get_running_loop().create_future()
        b.pending[1] = future
        await b._read()
        with self.assertRaises(CodexAccountError) as caught:
            await future
        self.assertEqual(caught.exception.code, "codex_workspace_timeout")
        b.pending.clear()


if __name__ == "__main__":
    unittest.main()
