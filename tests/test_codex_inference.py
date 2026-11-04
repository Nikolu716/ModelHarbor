"""Native CLI integration: protocol bounds, auth isolation and child cleanup."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.chatgpt import authorized_headers, completed_response
from app.codex_inference import CodexCLITransport, text_request
from app.config import GatewayConfig, StationConfig
from app.errors import classify_error
from app.health import HealthMonitor
from app.proxy import OpenAIProxy
from app.state import Registry
from app.stations import dataclass_to_dict
from app.stats import EventLog
from app.visual_tests import VisualTestRunner, VisualTestStore
from app.wire import UnsupportedWireRequest


class FakeProcess:
    def __init__(self, events, *, eof=True):
        self.returncode = None
        self.pid = 314159
        self.stdin = Mock(drain=AsyncMock())
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        for event in events:
            self.stdout.feed_data(json.dumps(event).encode() + b"\n")
        if eof:
            self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.killed = False

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        if not self.stdout.at_eof():
            self.stdout.feed_eof()


class CodexCLIProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = StationConfig.from_dict("Native", {"provider": "chatgpt", "chatgpt_mode": "codex_cli",
            "chatgpt_account": "0123456789abcdef0123456789abcdef"})
        self.bridge = SimpleNamespace(directory=Path(self.temp.name), operation_lock=asyncio.Lock(),
            _account=AsyncMock(return_value={"type": "chatgpt", "email": "own@example.test"}),
            close=AsyncMock(), rpc=AsyncMock(return_value={"data": [
                {"id": "native-model", "model": "native-model", "displayName": "Native Model"},
                {"id": "hidden", "hidden": True}]}))
        self.accounts = SimpleNamespace(codex_account=Mock(return_value=self.bridge))
        self.events = [{"type": "thread.started", "thread_id": "test"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "<svg></svg>"}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5, "cached_input_tokens": 10}}]
        self.process = FakeProcess(self.events)
        self.spawned = []

        async def launch(*args, **kwargs):
            self.spawned.append((args, kwargs))
            if Path(args[0]).name.lower() == "taskkill.exe":
                self.process.kill()
                return SimpleNamespace(wait=AsyncMock(return_value=0))
            return self.process

        self.spawn_patch = patch("app.codex_inference.asyncio.create_subprocess_exec", side_effect=launch)
        self.spawn_patch.start()
        self.path_patch = patch("app.codex_account.codex_executable", return_value="trusted-codex")
        self.path_patch.start()
        self.client = httpx.AsyncClient(transport=CodexCLITransport(self.accounts, self.cfg))

    async def asyncTearDown(self):
        await self.client.aclose()
        self.spawn_patch.stop()
        self.path_patch.stop()
        self.temp.cleanup()

    async def test_catalog_and_generation_use_native_login_without_oauth_bearer(self):
        token = AsyncMock()
        registry = SimpleNamespace(chatgpt=SimpleNamespace(access_token=token))
        headers = await authorized_headers(registry, self.cfg, {"Authorization": "Bearer gateway-key"})
        self.assertNotIn("Authorization", headers)
        token.assert_not_awaited()
        response = await self.client.get("https://api.openai.com/v1/models")
        self.assertEqual([m["slug"] for m in response.json()["models"]], ["native-model"])
        self.assertEqual(self.spawned, [])
        self.bridge.rpc.assert_awaited_once_with("model/list", {"limit": 100})
        response = await self.client.post("https://api.openai.com/v1/responses", json={
            "model": "native-model", "input": "Draw SVG", "reasoning": {"effort": "low"}})
        result, error = completed_response(response.content)
        self.assertIsNone(error)
        self.assertEqual(result["model"], "native-model")
        self.assertEqual(result["usage"]["total_tokens"], 25)
        self.assertEqual(result["usage"]["input_tokens_details"]["cached_tokens"], 10)
        self.assertEqual(result["output"][0]["content"][0]["text"], "<svg></svg>")
        args, kwargs = self.spawned[0]
        self.assertIn("--ephemeral", args)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("read-only", args)
        for feature in ("shell_tool", "plugins", "multi_agent", "computer_use"):
            self.assertIn(feature, args)
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.bridge.directory))
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertFalse(list(self.bridge.directory.glob("text-*")), "request scratch must be removed")

    async def test_first_cli_error_stops_retries_and_never_reports_completed(self):
        self.process = FakeProcess([{"type": "error", "message": json.dumps({"error": {
            "code": "usage_limit_reached", "message": "usage limit reached"}})}], eof=False)
        response = await self.client.post("https://api.openai.com/v1/responses", json={"model": "native-model", "input": "hello"})
        result, error = completed_response(response.content)
        self.assertIsNone(result)
        self.assertEqual(error["code"], "usage_limit_reached")
        classified = classify_error("Native", response.status_code, json.dumps({"error": error}))
        self.assertTrue(classified.quota)
        self.assertTrue(classified.deterministic)
        self.assertEqual(classified.status_code, 200)
        self.assertTrue(self.process.killed)
        self.assertEqual(sum(args[0] == "trusted-codex" for args, _ in self.spawned), 1)

    async def test_incomplete_cli_exit_is_failure(self):
        self.process = FakeProcess(self.events[:2])
        response = await self.client.post("https://api.openai.com/v1/responses", json={"model": "native-model", "input": "hello"})
        result, error = completed_response(response.content)
        self.assertIsNone(result)
        self.assertEqual(error["code"], "codex_cli_error")

    async def test_disconnect_kills_child_and_releases_account(self):
        self.process = FakeProcess([], eof=False)
        async with self.client.stream("POST", "https://api.openai.com/v1/responses", json={"model": "native-model", "input": "hello"}) as response:
            iterator = response.aiter_bytes()
            self.assertIn(b"response.created", await anext(iterator))
            pending = asyncio.create_task(anext(iterator))
            for _ in range(50):
                await asyncio.sleep(0)
                if self.spawned:
                    break
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertTrue(self.process.killed)
        self.assertFalse(self.bridge.operation_lock.locked())
        self.assertFalse(list(self.bridge.directory.glob("text-*")))

    async def test_native_mode_integrates_with_discovery_proxy_and_visual_tests(self):
        gateway = GatewayConfig(data_dir=self.temp.name, max_attempts=1, max_rounds=1)
        registry = Registry(gateway, [self.cfg])
        registry.chatgpt.codex_account = self.accounts.codex_account
        station = registry.get("Native")
        try:
            await HealthMonitor(registry, gateway).discover_models(station, manual=True)
            self.assertEqual(station.probed_models, {"native-model"})
            self.assertEqual(station.model_api_paths, {"native-model": ["/responses"]})
            class Request:
                client, headers = None, {}
                async def json(self):
                    return {"model": "native-model", "input": "hello"}
                async def is_disconnected(self):
                    return False
            result = await OpenAIProxy(registry, gateway, EventLog(self.temp.name))._dispatch(Request(), "/responses")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(json.loads(result.body)["output"][0]["content"][0]["text"], "<svg></svg>")
            self.process = FakeProcess(self.events)
            runner = VisualTestRunner(registry, gateway, VisualTestStore(self.temp.name))
            result = await runner.run(station, {"model": "native-model", "prompt": "Draw SVG"}, None)
            self.assertTrue(result["ok"])
            self.assertEqual(result["kind"], "svg")
            self.assertEqual(result["usage"]["input_tokens"], 20)
        finally:
            await registry.close_clients()
            await registry.chatgpt.close()

    async def test_native_only_gateway_rejects_tools_before_login_or_generation(self):
        gateway = GatewayConfig(data_dir=self.temp.name, max_attempts=1, max_rounds=1)
        registry = Registry(gateway, [self.cfg])
        registry.chatgpt.codex_account = self.accounts.codex_account
        station = registry.get("Native")
        station.probed_models = {"native-model"}
        station.model_api_paths = {"native-model": ["/responses"]}
        proxy = OpenAIProxy(registry, gateway, EventLog(self.temp.name))
        try:
            for streaming in (False, True):
                class Request:
                    client, headers = None, {}
                    async def json(self):
                        return {"model": "native-model", "input": "hello", "stream": streaming,
                                "tools": [{"type": "function", "name": "local_test",
                                           "parameters": {"type": "object", "properties": {}}}]}
                    async def is_disconnected(self):
                        return False
                result = await proxy._dispatch(Request(), "/responses")
                self.assertEqual(result.status_code, 400)
                self.assertIn("tools", json.loads(result.body)["error"]["message"])
            self.assertEqual(self.spawned, [])
            self.accounts.codex_account.assert_not_called()
            self.bridge._account.assert_not_awaited()
            self.assertIsNone(station.client)
            self.assertEqual((station.calls, station.failures), (0, 0))
        finally:
            await registry.close_clients()
            await registry.chatgpt.close()

    async def test_tool_request_can_use_another_channel_without_losing_tools(self):
        gateway = GatewayConfig(data_dir=self.temp.name, max_attempts=2, max_rounds=1)
        raw = StationConfig.from_dict("Raw", {"base_url": "https://raw.example.test", "api_key": "test-key",
                                              "models": ["native-model"]})
        registry = Registry(gateway, [self.cfg, raw])
        registry.chatgpt.codex_account = self.accounts.codex_account
        native, compatible = registry.get("Native"), registry.get("Raw")
        native.probed_models = {"native-model"}
        native.model_api_paths = {"native-model": ["/responses"]}
        sent = []
        tools = [{"type": "function", "name": "local_test",
                  "parameters": {"type": "object", "properties": {}}}]
        async def upstream(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "resp_raw", "object": "response", "status": "completed",
                "model": "native-model", "output": [{"type": "function_call", "name": "local_test",
                "call_id": "call_test", "arguments": "{}"}]})
        compatible.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        proxy = OpenAIProxy(registry, gateway, EventLog(self.temp.name))
        class Request:
            client, headers = None, {}
            async def json(self):
                return {"model": "native-model", "input": "hello", "tools": tools, "tool_choice": "required"}
            async def is_disconnected(self):
                return False
        try:
            with patch.object(proxy.router, "pick", return_value=[native, compatible]):
                result = await proxy._dispatch(Request(), "/responses")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(json.loads(result.body)["id"], "resp_raw")
            self.assertEqual(sent[0]["tools"], tools)
            self.assertEqual(sent[0]["tool_choice"], "required")
            self.assertEqual(self.spawned, [])
            self.assertEqual((native.calls, native.failures), (0, 0))
            self.assertEqual(compatible.calls, 1)
        finally:
            await registry.close_clients()
            await registry.chatgpt.close()


class CodexCLIContractTests(unittest.TestCase):
    def test_rejects_lossy_tools_and_multimodal_conversion(self):
        for extra in ({"tools": [{"type": "function", "name": "run"}]}, {"previous_response_id": "old"},
                      {"input": [{"type": "function_call_output", "output": "secret"}]},
                      {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "data:..."}]}]},
                      {"text": {"format": {"type": "json_schema"}}}):
            with self.subTest(extra=extra), self.assertRaises(UnsupportedWireRequest):
                text_request({"model": "native-model", "input": "hello", **extra})

    def test_native_mode_roundtrip_and_connection_switch_invalidate_old_limits(self):
        cfg = StationConfig.from_dict("Native", {"provider": "chatgpt", "chatgpt_mode": "codex_cli"})
        self.assertEqual(StationConfig.from_dict("Native", dataclass_to_dict(cfg)).chatgpt_mode, "codex_cli")
        with self.assertRaises(ValueError):
            StationConfig.from_dict("Native", {"provider": "chatgpt", "chatgpt_mode": "guess"})
        with tempfile.TemporaryDirectory() as directory:
            shared = StationConfig.from_dict("Native", {"provider": "chatgpt"})
            registry = Registry(GatewayConfig(data_dir=directory), [shared])
            station = registry.get("Native")
            station.calls, station.failures = 40, 2
            station.quota_exhausted, station.quota_error = True, "shared endpoint limit"
            station.probed_models = {"old-shared-model"}
            old_signature = station.connection_signature()
            registry.update_station("Native", {"chatgpt_mode": "codex_cli"})
            self.assertNotEqual(station.connection_signature(), old_signature)
            self.assertFalse(station.quota_exhausted)
            self.assertEqual((station.calls, station.failures), (40, 2))
            self.assertEqual(station.probed_models, set())
            self.assertFalse(station.supports_model("old-shared-model", "/responses"))
