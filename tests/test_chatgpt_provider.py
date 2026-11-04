"""ChatGPT-plan routing, OAuth contracts, and reset-credit consent."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
from app import __version__
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.chatgpt import (
    AUTHORITY, DIRECT_SCOPE, RESOURCE, SCOPES, ChatGPTAccounts,
    completed_response, subscription_request,
)
from app.config import GatewayConfig, StationConfig
from app.errors import classify_error
from app.health import HealthMonitor
from app.proxy import OpenAIProxy
from app.state import Registry
from app.stats import EventLog
from app.wire import UnsupportedWireRequest, prepare_request
from app.visual_tests import VisualTestRunner, VisualTestStore, VisualTestScheduler
from app.codex_account import CodexAccount
from fastapi import HTTPException


class Request:
    client = None

    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}

    async def json(self):
        return self.body

    async def is_disconnected(self):
        return False


class ChatGPTProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.profile = "0123456789abcdef0123456789abcdef"
        self.calls = []
        self.events = (b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
                       b'data: {"type":"response.completed","response":{"id":"resp-test","object":"response","output":[{"type":"message","content":[{"type":"output_text","text":"hello"}]}],"usage":{"input_tokens":4,"output_tokens":2}}}\n\n')

        async def upstream(request):
            self.calls.append(request)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"models": [
                    {"slug": "gpt-test", "display_name": "GPT Test", "visibility": "list"},
                    {"slug": "internal", "display_name": "Hidden", "visibility": "hidden"}]})
            if request.url.path == "/v1/responses":
                return httpx.Response(200, content=self.events, headers={"content-type": "text/event-stream"})
            return httpx.Response(404)

        self.transport = httpx.MockTransport(upstream)
        self.gateway = GatewayConfig(data_dir=self.temp.name, max_rounds=1, max_attempts=1,
                                     retry_backoff=0, first_byte_timeout=2, sse_keepalive_interval=0)
        config = StationConfig.from_dict("Plus", {"provider": "chatgpt", "chatgpt_account": self.profile})
        self.registry = Registry(self.gateway, [config])
        self.registry.chatgpt.transport = self.transport
        self.registry.chatgpt.store.write(self.profile, {
            "client_id": "oaiapp-test", "subject": "account-subject", "email": "plus@example.test",
            "scopes": [DIRECT_SCOPE], "access_token": "private-access-token",
            "refresh_token": "private-refresh-token", "token_type": "Bearer",
            "expires_at": time.time() + 3600,
        })
        self.station = self.registry.get("Plus")
        self.station.client = httpx.AsyncClient(transport=self.transport)
        self.monitor = HealthMonitor(self.registry, self.gateway)
        self.log = EventLog(self.temp.name)
        self.proxy = OpenAIProxy(self.registry, self.gateway, self.log)

    async def asyncTearDown(self):
        await self.registry.close_clients()
        await self.registry.chatgpt.close()
        self.temp.cleanup()

    async def test_subscription_protocol_filters_catalog_and_preserves_display_name(self):
        await self.monitor.discover_models(self.station, manual=True)
        self.assertEqual(self.station.probed_models, {"gpt-test"})
        self.assertEqual(self.station.model_display_names, {"gpt-test": "GPT Test"})
        self.assertEqual(self.station.model_api_paths["gpt-test"], ["/responses"])
        self.assertFalse(self.station.cfg.probe_chat)
        self.assertFalse(self.station.cfg.probe_balance)
        self.assertEqual(self.calls[0].url, RESOURCE + "/models")
        self.assertEqual(self.calls[0].headers["authorization"], "Bearer private-access-token")

    async def test_non_stream_response_consumes_official_sse_to_completion(self):
        await self.monitor.discover_models(self.station, manual=True)
        result = await self.proxy._dispatch(Request({"model": "gpt-test", "input": "hello"}), "/responses")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(json.loads(result.body)["id"], "resp-test")
        sent = self.calls[-1]
        self.assertEqual(sent.url, RESOURCE + "/responses")
        self.assertEqual(sent.headers["authorization"], "Bearer private-access-token")
        self.assertEqual(json.loads(sent.content), {
            "model": "gpt-test", "input": [{"role": "user", "content": "hello"}],
            "store": False, "stream": True,
        })

    async def test_chat_completions_is_rejected_without_upstream_fallback(self):
        await self.monitor.discover_models(self.station, manual=True)
        self.calls.clear()
        result = await self.proxy._dispatch(Request({"model": "gpt-test", "messages": []}), "/chat/completions")
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.calls, [])

    async def test_subscription_body_contract_and_completed_event_validation(self):
        target = subscription_request({
            "model": "gpt-test", "input": [{"role": "system", "content": "rules"}],
            "max_output_tokens": 20, "temperature": 0.1, "store": True, "stream": False,
        })
        self.assertEqual(target, {"model": "gpt-test", "input": [{"role": "developer", "content": "rules"}],
                                  "store": False, "stream": True})
        with self.assertRaises(UnsupportedWireRequest):
            subscription_request({"model": "gpt-test", "previous_response_id": "resp_old"})
        with self.assertRaises(UnsupportedWireRequest):
            prepare_request(self.station, "/chat/completions", {"model": "gpt-test"})
        response, error = completed_response(self.events)
        self.assertEqual(response["id"], "resp-test")
        self.assertIsNone(error)
        response, error = completed_response(b'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}}\n\n')
        self.assertIsNone(response)
        self.assertEqual(error["code"], "subscription_sharing_usage_limit_exceeded")

    async def test_official_usage_limit_error_pauses_this_subscription_route(self):
        err = classify_error("Plus", 429, json.dumps({"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "Limit reached"}}))
        self.assertTrue(err.quota)
        self.assertTrue(err.deterministic)
        self.assertFalse(classify_error("Plus", 503, json.dumps({"error": {
            "code": "subscription_sharing_usage_unavailable", "message": "Try later"}})).deterministic)

    async def test_rotating_refresh_token_is_serialized_and_preserved_on_temporary_failure(self):
        refreshed = 0

        async def token_endpoint(request):
            nonlocal refreshed
            refreshed += 1
            await asyncio.sleep(0.01)
            form = parse_qs(request.content.decode())
            self.assertEqual(form["client_id"], ["oaiapp-test"])
            self.assertEqual(form["refresh_token"], ["private-refresh-token"])
            self.assertNotIn("scope", form)
            return httpx.Response(200, json={"access_token": "new-secret", "refresh_token": "new-refresh",
                                           "token_type": "Bearer", "expires_in": 3600})

        manager = self.registry.chatgpt
        record = manager.store.read(self.profile)
        record["expires_at"] = 0
        manager.store.write(self.profile, record)
        manager.transport = httpx.MockTransport(token_endpoint)
        self.assertEqual(await asyncio.gather(*(manager.access_token(self.profile) for _ in range(4))), ["new-secret"] * 4)
        self.assertEqual(refreshed, 1)
        self.assertEqual(manager.store.read(self.profile)["refresh_token"], "new-refresh")
        record = manager.store.read(self.profile)
        record["expires_at"] = 0
        manager.store.write(self.profile, record)
        manager.transport = httpx.MockTransport(lambda _: httpx.Response(503))
        with self.assertRaises(ValueError):
            await manager.access_token(self.profile)
        self.assertEqual(manager.store.read(self.profile)["refresh_token"], "new-refresh")

    async def test_device_login_reuses_valid_code_and_renews_expired_code(self):
        bridge = self.registry.chatgpt.codex_account(self.profile)
        bridge.rpc = AsyncMock(return_value={"verificationUrl": "https://auth.openai.com/codex/device", "userCode": "TEST-1234", "loginId": "device-test"})
        first = await bridge.begin_login()
        self.assertEqual((await bridge.begin_login())["user_code"], first["user_code"])
        self.assertEqual(bridge.rpc.await_count, 1)
        bridge.login["expires_at"] = time.time() - 1
        bridge.rpc.return_value["userCode"] = "NEW-5678"
        renewed = await bridge.begin_login()
        self.assertEqual(renewed["user_code"], "NEW-5678")
        self.assertEqual(bridge.rpc.await_count, 3)
        self.assertEqual(bridge.rpc.await_args_list[1].args, ("account/login/cancel", {"loginId": "device-test"}))
        self.assertGreater(renewed["expires_at"], time.time())

    async def test_reset_credit_requires_explicit_confirmation_and_idempotency(self):
        from fastapi import FastAPI
        from app.web.admin import create_admin_router
        bridge = self.registry.chatgpt.codex_account(self.profile)
        bridge.consume = AsyncMock(return_value={"outcome": "reset", "usage": {}})
        app = FastAPI()
        app.include_router(create_admin_router(self.registry, self.gateway, self.monitor, self.log))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = f"/admin/chatgpt/profile/{self.profile}/reset-credit"
            self.assertEqual((await client.post(endpoint, json={})).status_code, 400)
            self.assertEqual((await client.post(endpoint, json={"confirmed": True})).status_code, 400)
            bridge.consume.assert_not_awaited()
            key = "01234567-89ab-4def-8123-456789abcdef"
            result = await client.post(endpoint, json={"confirmed": True, "credit_id": "credit-1", "idempotency_key": key})
            self.assertEqual(result.status_code, 200)
            bridge.consume.assert_awaited_once_with("credit-1", key)
            bridge.consume.reset_mock()
            key_without_id = "11234567-89ab-4def-8123-456789abcdef"
            result = await client.post(endpoint, json={"confirmed": True, "idempotency_key": key_without_id})
            self.assertEqual(result.status_code, 200)
            bridge.consume.assert_awaited_once_with(None, key_without_id)

    async def test_codex_usage_failure_does_not_cache_a_false_successful_refresh(self):
        bridge = self.registry.chatgpt.codex_account(self.profile)
        bridge._account = AsyncMock(return_value={"type": "chatgpt", "email": "plus@example.test"})
        bridge.rpc = AsyncMock(side_effect=[ValueError("temporary failure"), {"rateLimits": {}}])
        with self.assertRaises(ValueError):
            await bridge.refresh()
        result = await bridge.refresh()
        self.assertEqual(result["status"], "connected")
        self.assertEqual(bridge.rpc.await_count, 2)

    async def test_quota_cache_survives_restart_and_metadata_reads_never_start_inference(self):
        bridge = self.registry.chatgpt.codex_account(self.profile)
        bridge._account = AsyncMock(return_value={"email": "plus@example.test", "planType": "plus"})
        bridge.rpc = AsyncMock(return_value={"rateLimits": {"primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1900000000},
            "secondary": {"usedPercent": 30, "windowDurationMins": 10080}}, "rateLimitResetCredits": {"availableCount": 2}})
        await bridge.refresh()
        self.assertEqual(self.registry.snapshot()["stations"][0]["chatgpt_usage"]["windows"][0]["used_percent"], 12)
        self.assertEqual(self.calls, [])
        cached = CodexAccount(bridge.directory, "plus@example.test")
        self.assertEqual(cached.view()["reset_credits"]["availableCount"], 2)
        self.assertEqual(cached.view()["windows"][1]["duration_mins"], 10080)
        bridge.refresh = AsyncMock()
        bridge.next_refresh_at = 0
        await bridge.refresh_background()
        bridge.refresh.assert_not_awaited()  # No explicit device-code auth.
        (bridge.directory / "auth.json").write_text("{}", encoding="utf-8")
        await bridge.refresh_background()
        await bridge.refresh_background()
        bridge.refresh.assert_awaited_once()

    async def test_new_station_wakes_model_discovery_without_waiting_for_interval(self):
        initial = asyncio.Event()
        original = self.monitor._probe_all
        async def tick():
            await original()
            initial.set()
        self.monitor._probe_all = tick
        task = asyncio.create_task(self.monitor.run())
        try:
            await asyncio.wait_for(initial.wait(), 2)
            self.calls.clear()
            new = self.registry.add_station(StationConfig.from_dict("Second", {"provider": "chatgpt", "chatgpt_account": self.profile}))
            new.client = httpx.AsyncClient(transport=self.transport)
            self.monitor.wake()
            for _ in range(100):
                if new.probed_models:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(new.probed_models, {"gpt-test"})
            self.assertEqual([r.url.path for r in self.calls], ["/v1/models"])
        finally:
            await self.monitor.stop()
            await asyncio.wait_for(task, 2)

    async def test_subscription_visual_test_omits_output_limit_and_never_retries(self):
        await self.monitor.discover_models(self.station, manual=True)
        self.calls.clear()
        visual = VisualTestStore(self.temp.name)
        runner = VisualTestRunner(self.registry, self.gateway, visual)
        result = await runner.run(self.station, {"model": "gpt-test", "max_output_tokens": 99999, "reasoning_effort": "high"}, None)
        self.assertTrue(result["ok"], result["error"])
        self.assertIsNone(result["max_output_tokens"])
        self.assertEqual(len(self.calls), 1)
        payload = json.loads(self.calls[0].content)
        self.assertNotIn("max_output_tokens", payload)
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertEqual(self.calls[0].headers["user-agent"], "ModelHarbor/" + __version__)
        with self.assertRaises(HTTPException):
            await runner.run(self.station, {}, None)
        scheduler = VisualTestScheduler(self.registry, runner, visual)
        with self.assertRaises(HTTPException):
            scheduler.configure(self.station, {"enabled": True, "settings": {}})
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.monitor._quota_recovery_due(self.station, time.time()))

    async def test_subscription_one_attempt_across_rounds_and_parameter_fallback(self):
        await self.monitor.discover_models(self.station, manual=True)
        self.gateway.max_rounds = 5
        self.gateway.round_backoff = self.gateway.retry_backoff = 0
        for status, error in ((503, {"code": "subscription_sharing_usage_unavailable", "message": "temporary"}),
                              (400, {"code": "subscription_sharing_unsupported_capability", "message": "reasoning effort not supported"})):
            self.calls.clear()
            self.station.status = "up"
            self.station.consecutive_failures = 0
            async def fail(request):
                self.calls.append(request)
                return httpx.Response(status, json={"error": error})
            await self.station.client.aclose()
            self.station.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
            await self.proxy._dispatch(Request({"model": "gpt-test", "input": "hello", "reasoning": {"effort": "high"}}), "/responses")
            self.assertEqual(len(self.calls), 1)

    async def test_visual_subscription_rate_limit_is_shared_with_normal_requests(self):
        await self.monitor.discover_models(self.station, manual=True)
        self.calls.clear()
        async def limited(request):
            self.calls.append(request)
            return httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "180"})
        await self.station.client.aclose()
        self.station.client = httpx.AsyncClient(transport=httpx.MockTransport(limited))
        runner = VisualTestRunner(self.registry, self.gateway, VisualTestStore(self.temp.name))
        result = await runner.run(self.station, {}, None)
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(self.station.rate_limit_until, time.time() + 179)
        with self.assertRaises(HTTPException):
            await runner.run(self.station, {}, None)
        self.assertEqual(len(self.calls), 1)

    async def test_visual_subscription_needs_completion_and_stops_on_usage_limit(self):
        await self.monitor.discover_models(self.station, manual=True)
        runner = VisualTestRunner(self.registry, self.gateway, VisualTestStore(self.temp.name))
        self.events = b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        result = await runner.run(self.station, {}, None)
        self.assertFalse(result["ok"])
        runner.next_allowed.clear()
        self.events = b'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded","message":"usage limit"}}}\n\n'
        result = await runner.run(self.station, {}, None)
        self.assertFalse(result["ok"])
        self.assertTrue(self.station.quota_exhausted)

    async def test_streamed_response_requires_completion_and_closes_route(self):
        await self.monitor.discover_models(self.station, manual=True)
        result = await self.proxy._dispatch(Request({"model": "gpt-test", "input": "hello", "stream": True}), "/responses")
        self.assertEqual(result.status_code, 200)
        payload = b"".join([part async for part in result.body_iterator])
        self.assertIn(b"response.completed", payload)
        self.assertEqual(self.registry.route_activity_snapshot()["inflight"], 0)
        self.events = b'data: {"type":"response.output_text.delta","delta":"broken"}\n\n'
        result = await self.proxy._dispatch(Request({"model": "gpt-test", "input": "hello", "stream": True}), "/responses")
        payload = b"".join([part async for part in result.body_iterator])
        self.assertIn(b"error", payload)
        self.assertEqual(self.station.failures, 1)
        self.assertEqual(self.registry.route_activity_snapshot()["inflight"], 0)

    async def test_subscription_manual_retry_clears_only_on_completion_and_preserves_preview(self):
        await self.monitor.discover_models(self.station, manual=True)
        self.registry.bind_statistics(self.log)
        store = VisualTestStore(self.temp.name)
        runner = VisualTestRunner(self.registry, self.gateway, store)
        previous = await runner.run(self.station, {}, None)
        runner.next_allowed.clear()
        self.station.mark_quota_exhausted(classify_error("Plus", 200, json.dumps({"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "sharing limited"}})))
        self.station.quota_at = self.station.quota_probe_at = time.time() - 61
        self.calls.clear()
        result = await runner.retry_subscription(self.station, "127.0.0.1")
        self.assertTrue(result["ok"])
        self.assertTrue(result["recovered"])
        self.assertFalse(self.station.quota_exhausted)
        self.assertFalse(self.station.durable_view()["values"]["quota_exhausted"])
        self.assertFalse(self.log._runtime["Plus"]["values"]["quota_exhausted"])
        self.assertEqual(len(self.calls), 1)
        payload = json.loads(self.calls[0].content)
        self.assertEqual(payload["input"], [{"role": "user", "content": "Reply with only OK."}])
        self.assertNotIn("max_output_tokens", payload)
        self.assertEqual(store.result(self.station)["id"], previous["id"])

    async def test_subscription_retry_keeps_protection_for_partial_stream_and_limit(self):
        await self.monitor.discover_models(self.station, manual=True)
        self.registry.bind_statistics(self.log)
        runner = VisualTestRunner(self.registry, self.gateway, VisualTestStore(self.temp.name))
        self.station.mark_quota_exhausted(classify_error("Plus", 429, json.dumps({"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "sharing limited"}})))
        for events, code in [
            (b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n', "incomplete_response"),
            (b'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded","message":"still limited"}}}\n\n', "subscription_sharing_usage_limit_exceeded"),
        ]:
            self.events = events
            self.station.quota_at = self.station.quota_probe_at = time.time() - 61
            runner.next_allowed.clear()
            self.calls.clear()
            result = await runner.retry_subscription(self.station, None)
            self.assertFalse(result["recovered"])
            self.assertEqual(result["http_status"], 200)
            self.assertEqual(result["error_code"], code)
            self.assertTrue(self.station.quota_exhausted)
            self.assertTrue(self.log._runtime["Plus"]["values"]["quota_exhausted"])
            self.assertEqual(len(self.calls), 1)
            self.assertIsNone(runner.store.result(self.station))
            with self.assertRaises(HTTPException) as caught:
                await runner.retry_subscription(self.station, None)
            self.assertEqual(caught.exception.status_code, 429)

    async def test_subscription_retry_api_requires_confirmation_enabled_station_and_wait(self):
        from fastapi import FastAPI
        from app.web.admin import create_admin_router
        await self.monitor.discover_models(self.station, manual=True)
        self.station.mark_quota_exhausted(classify_error("Plus", 429, json.dumps({"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "sharing limited"}})))
        app = FastAPI()
        app.include_router(create_admin_router(self.registry, self.gateway, self.monitor, self.log))
        self.calls.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            endpoint = "/admin/station/Plus/subscription-retry"
            for body in ({}, {"confirmed": False}, {"confirmed": 1}, {"confirmed": True, "extra": True}):
                self.assertEqual((await client.post(endpoint, json=body)).status_code, 400)
            self.assertEqual((await client.post(endpoint, json={"confirmed": True})).status_code, 429)
            self.station.enabled = False
            self.assertEqual((await client.post("/admin/station/Plus/reset")).status_code, 409)
            self.assertTrue(self.station.quota_exhausted)
            self.assertEqual((await client.post(endpoint, json={"confirmed": True})).status_code, 409)
            self.station.enabled = True
            self.station.quota_at = self.station.quota_probe_at = time.time() - 61
            self.station.rate_limit_until = time.time() + 180
            self.assertEqual((await client.post(endpoint, json={"confirmed": True})).status_code, 429)
            self.assertEqual(self.calls, [])
            self.station.rate_limit_until = 0
            response = await client.post(endpoint, json={"confirmed": True})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["recovered"])
            self.assertEqual(len(self.calls), 1)

    async def test_subscription_retry_has_one_inflight_and_retains_newer_failure(self):
        await self.monitor.discover_models(self.station, manual=True)
        runner = VisualTestRunner(self.registry, self.gateway, VisualTestStore(self.temp.name))
        err = classify_error("Plus", 429, json.dumps({"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limited"}}))
        self.station.mark_quota_exhausted(err)
        self.station.quota_at = self.station.quota_probe_at = time.time() - 61
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(request):
            self.calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, content=self.events, headers={"content-type": "text/event-stream"})
        await self.station.client.aclose()
        self.station.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        task = asyncio.create_task(runner.retry_subscription(self.station, None))
        await entered.wait()
        self.assertIn("Plus", runner.tasks)
        with self.assertRaises(HTTPException):
            await runner.retry_subscription(self.station, None)
        self.station.mark_quota_exhausted(err)
        release.set()
        result = await task
        self.assertFalse(result["recovered"])
        self.assertTrue(self.station.quota_exhausted)
        self.assertNotIn("Plus", runner.tasks)
        self.assertNotIn("Plus", runner.busy)


class ChatGPTOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_pkce_scope_nonce_and_id_token_validation(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
        public_jwk.update(kid="test-key", alg="RS256", use="sig")
        self.signed_client = "oaiapp-issued-test"
        self.signed_nonce = ""

        async def oauth(request):
            if request.url.path == "/.well-known/openid-configuration":
                return httpx.Response(200, json={"issuer": AUTHORITY, "jwks_uri": AUTHORITY + "/jwks"})
            if request.url.path == "/jwks":
                return httpx.Response(200, json={"keys": [public_jwk]})
            if request.url.path == "/api/accounts/oauth/token":
                form = request.content.decode()
                self.assertIn("code_verifier=", form)
                self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A", form)
                claims = {"iss": AUTHORITY, "aud": self.signed_client, "sub": "subject-1",
                          "email": "plus@example.test", "nonce": self.signed_nonce,
                          "exp": time.time() + 600, "iat": time.time()}
                token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
                return httpx.Response(200, json={"access_token": "access-secret", "refresh_token": "refresh-secret",
                    "id_token": token, "token_type": "Bearer", "expires_in": 3600, "scope": SCOPES})
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as directory:
            manager = ChatGPTAccounts(directory, transport=httpx.MockTransport(oauth))
            login = await manager.start_login()
            params = parse_qs(urlsplit(login["auth_url"]).query)
            self.assertEqual(params["client_id"], ["dynamic_agent_client"])
            self.assertEqual(params["scope"], [SCOPES])
            self.assertEqual(params["resource"], [RESOURCE])
            self.assertTrue(params["redirect_uri"][0].startswith("http://127.0.0.1:"))
            self.assertEqual(params["code_challenge_method"], ["S256"])
            self.assertNotEqual(params["code_challenge"][0], params["state"][0])
            self.assertEqual(params["agent_name_hint"], ["ModelHarbor"])
            self.signed_nonce = manager.attempts[login["id"]]["nonce"]
            await manager.complete(login["id"], {"state": params["state"], "client_id": [self.signed_client], "code": ["one-time-code"]})
            view = manager.store.view(login["profile"])
            self.assertTrue(view["connected"])
            self.assertEqual(view["email"], "plus@example.test")
            stored = manager.store.read(login["profile"])
            self.assertEqual(stored["access_token"], "access-secret")
            self.assertNotIn(b"access-secret", manager.store.path(login["profile"]).read_bytes())

    async def test_bad_callback_state_is_rejected_before_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ChatGPTAccounts(directory, transport=httpx.MockTransport(lambda _: httpx.Response(500)))
            login = await manager.start_login()
            with self.assertRaises(ValueError):
                await manager.complete(login["id"], {"state": ["forged"], "code": ["code"], "client_id": ["oaiapp-test"]})
            manager.cancel(login["id"])
