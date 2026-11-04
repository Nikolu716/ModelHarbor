"""Codex protocol forwarding: tools, native login and stream ownership."""
import asyncio
import base64
import gzip
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from app.chatgpt import ChatGPTAuthError, authorized_headers
from app.codex_account import CodexAccount
from app.codex_relay import CODEX_RESPONSES, CodexRelayTransport, relay_request
from app.config import GatewayConfig, StationConfig
from app.providers import inference_headers, request_context
from app.proxy import OpenAIProxy
from app.state import Registry
from app.stats import EventLog
from app.wire import UnsupportedWireRequest


class Stream(httpx.AsyncByteStream):
    def __init__(self, events, block=False):
        self.events, self.block = events, block
        self.closed = 0

    async def __aiter__(self):
        for event in self.events:
            yield ("data: " + json.dumps(event) + "\n\n").encode()
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed += 1


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = StationConfig.from_dict("Relay", {"provider": "chatgpt", "chatgpt_mode": "codex_relay",
            "chatgpt_account": "0123456789abcdef0123456789abcdef"})
        self.bridge = SimpleNamespace(inference_credentials=AsyncMock(return_value=("native-secret", "native-account")))
        self.accounts = SimpleNamespace(codex_account=Mock(return_value=self.bridge), access_token=AsyncMock())
        self.seen, self.streams = [], []
        self.code, self.block = 200, False
        self.events = [{"type": "response.output_item.added", "item": {"type": "function_call", "name": "test"}},
            {"type": "response.completed", "response": {"id": "resp_relay", "object": "response", "status": "completed",
                "model": "test-model", "output": [{"type": "function_call", "name": "test", "call_id": "call_1",
                    "arguments": "{}"}], "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}}]
        async def upstream(request):
            self.seen.append(request)
            stream = Stream(self.events, self.block)
            self.streams.append(stream)
            return httpx.Response(self.code, stream=stream, headers={"content-type": "text/event-stream", "x-request-id": "req_native",
                **({"location": "https://elsewhere.example/steal"} if self.code == 307 else {})})
        self.transport = CodexRelayTransport(self.accounts, self.cfg, transport=httpx.MockTransport(upstream))
        self.client = httpx.AsyncClient(transport=self.transport)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def test_native_credentials_fixed_origin_and_session_isolation(self):
        gateway = SimpleNamespace(chatgpt=self.accounts)
        raw = {"authorization": "Bearer client-secret", "cookie": "private", "x-session-id": "conversation-1",
               "thread-id": "thread-1", "x-codex-turn-state": "real-turn", "chatgpt-account-id": "untrusted"}
        headers = await authorized_headers(gateway, self.cfg, inference_headers(self.cfg, request_context(raw)))
        self.assertNotIn("Authorization", headers)
        response = await self.client.post("https://api.openai.com/v1/responses", headers=headers,
                                          json={"model": "test-model", "input": "hello"})
        self.assertEqual(response.status_code, 200)
        request = self.seen[0]
        self.assertEqual(str(request.url), CODEX_RESPONSES)
        self.assertEqual(request.headers["authorization"], "Bearer native-secret")
        self.assertEqual(request.headers["chatgpt-account-id"], "native-account")
        self.assertEqual(request.headers["session-id"], "conversation-1")
        self.assertEqual(request.headers["thread-id"], "thread-1")
        self.assertEqual(request.headers["x-codex-turn-state"], "real-turn")
        self.assertNotIn("cookie", request.headers)
        self.assertNotIn("x-forwarded-for", request.headers)
        self.accounts.access_token.assert_not_awaited()
        self.assertEqual(self.streams[0].closed, 1)
        self.assertEqual(response.headers["x-request-id"], "req_native")

    async def test_proxy_preserves_tool_only_stream_and_tool_results(self):
        gateway = GatewayConfig(data_dir=self.temp.name, max_attempts=1, max_rounds=1)
        registry = Registry(gateway, [self.cfg])
        station = registry.get("Relay")
        station.probed_models = {"test-model"}
        station.model_api_paths = {"test-model": ["/responses"]}
        station.client = self.client
        proxy = OpenAIProxy(registry, gateway, EventLog(self.temp.name))
        class Request:
            client = None
            headers = {"x-session-id": "real-session"}
            async def json(self):
                return {"model": "test-model", "input": [{"type": "function_call", "name": "test",
                    "call_id": "call_old", "arguments": "{}"}, {"type": "function_call_output", "call_id": "call_old", "output": "42"}],
                    "tools": [{"type": "function", "name": "test", "parameters": {"type": "object"}}],
                    "tool_choice": "required", "stream": True}
            async def is_disconnected(self):
                return False
        try:
            response = await proxy._dispatch(Request(), "/responses")
            self.assertEqual(response.status_code, 200)
            content = b"".join([chunk async for chunk in response.body_iterator])
            self.assertIn(b"function_call", content)
            self.assertIn(b"resp_relay", content)
            sent = json.loads(self.seen[0].content)
            self.assertEqual(sent["input"][1]["output"], "42")
            self.assertEqual(sent["tools"][0]["name"], "test")
            self.assertEqual(sent["tool_choice"], "required")
            self.assertEqual(self.seen[0].headers["session-id"], "real-session")
            self.assertEqual((station.calls, station.failures), (1, 0))
            self.assertTrue(self.streams[0].closed)
            self.assertFalse(registry.route_activity_snapshot()["active_routes"])
        finally:
            await registry.close_clients()
            await registry.chatgpt.close()

    async def test_auth_failure_does_not_contact_upstream_or_sharing_endpoint(self):
        self.bridge.inference_credentials.side_effect = ChatGPTAuthError("login required")
        response = await self.client.post("https://api.openai.com/v1/responses", json={"model": "test-model", "input": "hello"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "codex_auth_required")
        self.assertEqual(self.seen, [])
        self.accounts.access_token.assert_not_awaited()

    async def test_redirect_is_returned_without_forwarding_native_credentials(self):
        self.code = 307
        response = await self.client.post("https://api.openai.com/v1/responses", json={"model": "test-model", "input": "hello"}, follow_redirects=True)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "codex_redirect_refused")
        self.assertEqual(len(self.seen), 1)

    async def test_disconnect_closes_upstream_once(self):
        self.block = True
        async with self.client.stream("POST", "https://api.openai.com/v1/responses", json={"model": "test-model", "input": "hello"}) as response:
            iterator = response.aiter_bytes()
            await anext(iterator)
        self.assertEqual(self.streams[0].closed, 1)

    async def test_cancellation_mid_read_closes_upstream_once(self):
        self.block = True
        async with self.client.stream("POST", "https://api.openai.com/v1/responses",
                                      json={"model": "test-model", "input": "hello"}) as response:
            iterator = response.aiter_bytes()
            for _ in self.events:
                await anext(iterator)
            pending = asyncio.create_task(anext(iterator))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertEqual(self.streams[0].closed, 1)

    async def test_compressed_stream_is_decoded_only_by_outer_client(self):
        payload = b''.join(("data: " + json.dumps(event) + "\n\n").encode() for event in self.events)

        async def compressed(request):
            return httpx.Response(200, stream=httpx.ByteStream(gzip.compress(payload)),
                                  headers={"content-type": "text/event-stream", "content-encoding": "gzip"})

        async with httpx.AsyncClient(transport=CodexRelayTransport(
                self.accounts, self.cfg, transport=httpx.MockTransport(compressed))) as client:
            response = await client.post("https://api.openai.com/v1/responses",
                                         json={"model": "test-model", "input": "hello"})
        self.assertEqual(response.content, payload)

    async def test_invalid_request_is_rejected_before_native_login(self):
        for body in ([], {"model": "test-model", "input": []},
                     {"model": "test-model", "input": "hello", "instructions": 123},
                     {"model": "test-model", "input": "hello", "previous_response_id": "old"}):
            with self.subTest(body=body):
                response = await self.client.post("https://api.openai.com/v1/responses", json=body)
                self.assertEqual(response.status_code, 400)
        self.bridge.inference_credentials.assert_not_awaited()
        self.assertFalse(self.seen)

    async def test_rate_limit_is_passed_through_without_retry(self):
        self.code = 429
        response = await self.client.post("https://api.openai.com/v1/responses", json={"model": "test-model", "input": "hello"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(self.seen), 1)


class ProfileSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_switching_profile_replaces_transport_and_uses_new_native_account(self):
        for mode in ("codex_relay", "codex_cli"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                first_profile, second_profile = "1" * 32, "2" * 32
                cfg = StationConfig.from_dict("Native", {"provider": "chatgpt", "chatgpt_mode": mode,
                                                         "chatgpt_account": first_profile})
                registry = Registry(GatewayConfig(data_dir=directory), [cfg])
                bridges = {profile: SimpleNamespace(operation_lock=asyncio.Lock(), _account=AsyncMock(),
                           rpc=AsyncMock(return_value={"data": [{"model": profile}]}), close=AsyncMock())
                           for profile in (first_profile, second_profile)}
                registry.chatgpt.codex_account = Mock(side_effect=bridges.__getitem__)
                station = registry.get("Native")
                try:
                    first_client = registry.ensure_client(station)
                    first = await first_client.get("https://api.openai.com/v1/models")
                    self.assertEqual(first.json()["models"][0]["slug"], first_profile)
                    registry.update_station("Native", {"chatgpt_account": second_profile})
                    second_client = registry.ensure_client(station)
                    self.assertIsNot(first_client, second_client)
                    second = await second_client.get("https://api.openai.com/v1/models")
                    self.assertEqual(second.json()["models"][0]["slug"], second_profile)
                    bridges[second_profile]._account.assert_awaited_once()
                finally:
                    await registry.close_clients()
                    await registry.chatgpt.close()
                self.assertTrue(first_client.is_closed)
                self.assertTrue(second_client.is_closed)


class NativeCredentialsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_official_bridge_refreshes_expiring_native_token(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = CodexAccount(directory, "own@example.test")
            bridge._account = AsyncMock()
            path = Path(directory) / "auth.json"
            def write(expiry):
                payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
                token = "header." + payload + ".signature"
                path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": token,
                    "account_id": "own", "refresh_token": "never-manually-used"}}), encoding="utf-8")
                return token
            first = write(time.time() + 7200)
            bridge.rpc = AsyncMock()
            self.assertEqual(await bridge.inference_credentials(), (first, "own"))
            bridge.rpc.assert_not_awaited()
            write(time.time() - 1)
            async def refresh(*args):
                write(time.time() + 7200)
                return {}
            bridge.rpc.side_effect = refresh
            token, account = await bridge.inference_credentials()
            self.assertEqual(account, "own")
            bridge.rpc.assert_awaited_once_with("account/read", {"refreshToken": True})
            path.write_text('{"OPENAI_API_KEY":"sharing-must-not-be-used"}', encoding="utf-8")
            with self.assertRaises(ChatGPTAuthError):
                await bridge.inference_credentials()


class ContractTests(unittest.TestCase):
    def test_preserves_structured_output_namespaces_and_encrypted_history(self):
        source = {"model": "test", "input": [{"type": "reasoning", "encrypted_content": "opaque"},
            {"type": "custom_tool_call_output", "call_id": "c", "output": "ok"}],
            "tools": [{"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "patch"}]}],
            "text": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
            "include": ["reasoning.encrypted_content"], "reasoning": {"effort": "high"}, "max_output_tokens": 12000}
        target = relay_request(source)
        for key in ("input", "tools", "text", "include", "reasoning"):
            self.assertEqual(target[key], source[key])
        self.assertNotIn("max_output_tokens", target)
        self.assertEqual(relay_request(target), target)
        self.assertEqual(source["max_output_tokens"], 12000)
        with self.assertRaises(UnsupportedWireRequest):
            relay_request({"model": "test", "input": "hello", "previous_response_id": "old"})
