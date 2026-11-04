import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.chatgpt import ChatGPTAccounts
from app.codex_account import CodexAccount
import httpx
from fastapi import FastAPI
from app.config import GatewayConfig
from app.state import Registry
from app.stats import EventLog
from app.stations import StationStore
from app.web.admin import create_admin_router


class FakeCodexAccount(CodexAccount):
    instances = []

    def __init__(self, directory, expected_email):
        super().__init__(directory, expected_email)
        self.closed = False
        self.account = {"email": f"codex-{len(self.instances) + 1}@example.test", "planType": "plus"}
        self.instances.append(self)

    async def begin_login(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.login = {"status": "pending", "user_code": "ABCD-EFGH",
                      "verification_url": "https://auth.openai.com/codex/device",
                      "login_id": "login-id", "expires_at": time.time() + 900}
        return self.login

    async def _account(self):
        return self.account

    async def cancel_login(self):
        self.login = {"status": "cancelled"}
        await self.close()

    async def close(self):
        self.closed = True

    def complete(self):
        self.login["status"] = "completed"
        (self.directory / "auth.json").write_text("{}", encoding="utf-8")


class CodexBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        FakeCodexAccount.instances = []
        self.patch = patch("app.codex_bindings.CodexAccount", FakeCodexAccount)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addAsyncCleanup(self._close)
        self.accounts = ChatGPTAccounts(self.temp.name)

    async def _close(self):
        await self.accounts.close()
        self.temp.cleanup()

    async def test_each_binding_is_an_independent_native_account_without_shared_oauth(self):
        first = await self.accounts.bindings.start()
        second = await self.accounts.bindings.start()
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["verification_url"], "https://attacker.invalid")
        first_bridge, second_bridge = FakeCodexAccount.instances
        self.assertNotEqual(first_bridge.directory, second_bridge.directory)

        first_bridge.complete()
        completed = await self.accounts.bindings.status(first["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["account"]["codex_connected"])
        self.assertEqual(completed["account"]["email"], first_bridge.account["email"])
        record = self.accounts.store.read(completed["profile"])
        self.assertEqual(record["auth_type"], "codex")
        self.assertNotIn("access_token", record)
        self.assertNotIn("refresh_token", record)
        self.assertNotIn("scopes", record)
        self.assertEqual((await self.accounts.bindings.status(second["id"]))["status"], "pending")

        native = self.accounts.profile_view(completed["profile"], "codex_relay")
        sharing = self.accounts.profile_view(completed["profile"], "shared")
        self.assertTrue(native["connected"])
        self.assertFalse(sharing["connected"])
        self.assertFalse(native["shared_connected"])
        self.assertEqual(native["plan"], "plus")
        restored = ChatGPTAccounts(self.temp.name)
        try:
            self.assertTrue(restored.profile_view(completed["profile"], "codex_cli")["connected"])
            self.assertFalse(restored.profile_view(completed["profile"], "shared")["connected"])
        finally:
            await restored.close()

    async def test_saved_binding_unchanged_when_replacement_cancelled(self):
        first = await self.accounts.bindings.start()
        old = FakeCodexAccount.instances[-1]
        old.complete()
        saved = await self.accounts.bindings.status(first["id"])
        profile = saved["profile"]
        before = self.accounts.store.path(profile).read_bytes()
        replacement = await self.accounts.bindings.start()
        await self.accounts.bindings.cancel(replacement["id"])
        self.assertEqual(self.accounts.store.path(profile).read_bytes(), before)
        self.assertIs(self.accounts.codex_account(profile), old)
        self.assertFalse(old.closed)
        self.assertTrue(self.accounts.profile_view(profile, "codex_relay")["connected"])

    async def test_discarded_completed_binding_is_removed_but_claimed_binding_survives(self):
        abandoned = await self.accounts.bindings.start()
        abandoned_bridge = FakeCodexAccount.instances[-1]
        abandoned_bridge.complete()
        abandoned_result = await self.accounts.bindings.status(abandoned["id"])
        abandoned_profile = abandoned_result["profile"]
        await self.accounts.bindings.cancel(abandoned["id"])
        self.assertFalse(self.accounts.store.path(abandoned_profile).exists())
        self.assertFalse((self.accounts.store.directory / ("codex-" + abandoned_profile)).exists())

        saved = await self.accounts.bindings.start()
        saved_bridge = FakeCodexAccount.instances[-1]
        saved_bridge.complete()
        saved_result = await self.accounts.bindings.status(saved["id"])
        self.accounts.bindings.claim_profile(saved_result["profile"])
        await self.accounts.bindings.cancel(saved["id"])
        self.assertEqual((await self.accounts.bindings.status(saved["id"]))["status"], "completed")
        self.assertTrue(self.accounts.store.path(saved_result["profile"]).exists())

    async def test_failed_or_incomplete_login_cannot_create_connected_profile(self):
        started = await self.accounts.bindings.start()
        bridge = FakeCodexAccount.instances[-1]
        bridge.login["status"] = "completed"
        result = await self.accounts.bindings.status(started["id"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(bridge.closed)
        self.assertNotIn("profile", result)
        self.assertEqual(self.accounts.codex, {})
        self.assertEqual(list(self.accounts.store.directory.glob("*.credential")), [])

    async def test_shutdown_cancels_all_pending_attempts(self):
        await self.accounts.bindings.start()
        await self.accounts.bindings.start()
        await self.accounts.bindings.close()
        self.assertTrue(all(bridge.closed for bridge in FakeCodexAccount.instances))
        self.assertTrue(all(attempt["status"] == "cancelled" for attempt in self.accounts.bindings.attempts.values()))

    async def test_cancel_and_expiry_close_only_their_own_login(self):
        first = await self.accounts.bindings.start()
        second = await self.accounts.bindings.start()
        first_bridge, second_bridge = FakeCodexAccount.instances
        cancelled = await self.accounts.bindings.cancel(first["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(first_bridge.closed)
        self.assertFalse(second_bridge.closed)
        self.assertEqual((await self.accounts.bindings.status(second["id"]))["status"], "pending")

        attempt = self.accounts.bindings.attempts[second["id"]]
        attempt["expires_at"] = time.time() - 1
        expired = await self.accounts.bindings.status(second["id"])
        self.assertEqual(expired["status"], "expired")
        self.assertTrue(second_bridge.closed)
        self.assertFalse(self.accounts.store.path(attempt["profile"]).exists())

    async def test_binding_limit_stops_unbounded_pending_cli_processes(self):
        for _ in range(4):
            await self.accounts.bindings.start()
        with self.assertRaisesRegex(ValueError, "最多同时进行 4 个"):
            await self.accounts.bindings.start()

    async def test_fast_completion_ignores_stale_login_notification(self):
        for received_id, expected in (("new-login", "completed"), ("old-login", "pending")):
            bridge = CodexAccount(Path(self.temp.name) / "rpc", "")
            async def rpc(method, params):
                self.assertEqual(method, "account/login/start")
                reader = asyncio.StreamReader()
                bridge.process = SimpleNamespace(stdout=reader)
                reader.feed_data((json.dumps({"method": "account/login/completed",
                    "params": {"loginId": received_id, "success": True}}) + "\n").encode())
                reader.feed_eof()
                await bridge._read()
                bridge.process = None
                return {"verificationUrl": "https://auth.openai.com/codex/device",
                        "userCode": "TEST-1234", "loginId": "new-login"}
            bridge.rpc = rpc
            self.assertEqual((await bridge.begin_login())["status"], expected)

    async def test_api_saves_native_binding_only_when_channel_is_saved(self):
        gateway = GatewayConfig(data_dir=self.temp.name)
        registry = Registry(gateway, [])
        registry.chatgpt = self.accounts
        monitor = SimpleNamespace(wake=lambda: None)
        store = StationStore(self.temp.name)
        app = FastAPI()
        app.include_router(create_admin_router(registry, gateway, monitor, EventLog(self.temp.name), store=store))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                invalid = await client.post("/admin/chatgpt/codex/login", json={"profile": "existing"})
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual((await client.get("/admin/chatgpt/codex/login/missing")).status_code, 404)
                started = (await client.post("/admin/chatgpt/codex/login", json={})).json()
                FakeCodexAccount.instances[-1].complete()
                completed = (await client.get("/admin/chatgpt/codex/login/" + started["id"])).json()
                self.assertEqual(registry.stations, [])
                body = {"name": "Codex One", "provider": "chatgpt", "chatgpt_mode": "codex_relay",
                        "chatgpt_account": completed["profile"]}
                self.assertEqual((await client.post("/admin/stations", json={**body, "chatgpt_account": "f" * 32})).status_code, 400)
                self.assertEqual((await client.post("/admin/stations", json={**body, "chatgpt_mode": "shared"})).status_code, 400)
                added = await client.post("/admin/stations", json=body)
                self.assertEqual(added.status_code, 201, added.text)
                before = store.path.read_bytes()
                replacement = (await client.post("/admin/chatgpt/codex/login", json={})).json()
                FakeCodexAccount.instances[-1].complete()
                next_account = (await client.get("/admin/chatgpt/codex/login/" + replacement["id"])).json()
                self.assertEqual(store.path.read_bytes(), before)
                self.assertEqual(registry.get("Codex One").cfg.chatgpt_account, completed["profile"])
                changed = await client.put("/admin/stations/Codex%20One", json={"chatgpt_account": next_account["profile"]})
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertEqual(registry.get("Codex One").cfg.chatgpt_account, next_account["profile"])
                self.assertEqual(store.load()[0].chatgpt_account, next_account["profile"])
                self.assertEqual((await client.delete("/admin/chatgpt/codex/login/" + replacement["id"])).status_code, 200)
                self.assertTrue(self.accounts.store.path(next_account["profile"]).exists())
        finally:
            await registry.close_clients()


if __name__ == "__main__":
    unittest.main()
