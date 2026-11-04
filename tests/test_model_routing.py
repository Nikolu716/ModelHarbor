"""Provider-aware routing and Go session safety, without live inference."""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

from app import __version__
from app.config import GatewayConfig, StationConfig
from app.errors import UpstreamError
from app.health import HealthMonitor
from app.providers import inference_headers, opencode_capabilities
from app.proxy import OpenAIProxy
from app.state import Registry
from app.stats import EventLog


class Request:
    client = None

    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}

    async def json(self):
        return self.body

    async def is_disconnected(self):
        return False


class GoRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(max_rounds=1, retry_backoff=0, first_byte_timeout=2,
                                     first_token_timeout=2, sse_keepalive_interval=0)
        go = StationConfig.from_dict("Go", {
            "provider": "opencode", "opencode_plan": "go",
            "base_url": "https://opencode.ai", "api_key": "fake-provider-key",
            "headers": {"x-opencode-session": "must-not-reuse"},
        })
        self.registry = Registry(self.gateway, [go])
        self.station = self.registry.get("Go")
        self.station.probed_models = {"gpt-test", "glm-test"}
        self.station.probed_model_order = ["gpt-test", "glm-test"]
        self.station.model_api_paths = {"gpt-test": ["/responses", "/chat/completions"], "glm-test": ["/chat/completions", "/responses"]}
        self.station.last_probe_ok = True
        self.requests = []
        self.http_status = 200

        async def upstream(request):
            self.requests.append(request)
            await asyncio.sleep(0)
            body = json.loads(request.content)
            if self.http_status != 200:
                return httpx.Response(self.http_status, json={"error": {"message": "Insufficient account funds"}})
            if body.get("stream"):
                content = (b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                           b'data: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":1}}}\n\n')
                return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                                "usage": {"prompt_tokens": 2, "completion_tokens": 1}})
            return httpx.Response(200, json={"object": "response", "output": [], "usage": {"input_tokens": 2, "output_tokens": 1}})

        self.station.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.proxy = OpenAIProxy(self.registry, self.gateway, EventLog(self.tmp.name))

    async def asyncTearDown(self):
        await self.registry.close_clients()
        self.tmp.cleanup()

    async def test_json_go_endpoint_keeps_real_session_and_only_context_headers(self):
        request = Request({"model": "gpt-test", "input": "Fix a sorting bug"}, {
            "session-id": "conversation-a", "thread-id": "thread-a",
            "user-agent": "CodingClient/2.0", "authorization": "Bearer gateway-private",
            "cookie": "must-not-forward", "x-forwarded-for": "203.0.113.9",
        })
        response = await self.proxy._dispatch(request, "/responses")
        self.assertEqual(response.status_code, 200)
        sent = self.requests[0]
        self.assertEqual(sent.url.path, "/inference/go/openai/v1/responses")
        self.assertEqual(sent.headers["session-id"], "conversation-a")
        self.assertEqual(sent.headers["user-agent"], "CodingClient/2.0")
        self.assertEqual(sent.headers["authorization"], "Bearer fake-provider-key")
        for header in ("x-opencode-session", "cookie", "x-forwarded-for"):
            self.assertNotIn(header, sent.headers)

    async def test_stream_go_session_is_preserved(self):
        response = await self.proxy._dispatch(Request({"model": "gpt-test", "input": "Write a function", "stream": True},
                                                       {"x-opencode-session": "conversation-stream"}), "/responses")
        self.assertEqual(response.status_code, 200)
        content = b"".join([part async for part in response.body_iterator])
        self.assertIn(b"response.completed", content)
        self.assertEqual(self.requests[0].headers["x-opencode-session"], "conversation-stream")
        self.assertEqual(self.registry.route_activity_snapshot()["inflight"], 0)

    async def test_concurrent_sessions_never_share_context(self):
        await asyncio.gather(*(self.proxy._dispatch(
            Request({"model": "gpt-test", "input": str(i)}, {"session-id": "s-" + str(i)}),
            "/responses") for i in range(8)))
        self.assertEqual(len(self.requests), 8)
        for request in self.requests:
            self.assertEqual(request.headers["session-id"], "s-" + json.loads(request.content)["input"])

    async def test_missing_session_is_local_for_json_and_stream_with_no_health_penalty(self):
        for stream in (False, True):
            response = await self.proxy._dispatch(Request({"model": "gpt-test", "input": "test", "stream": stream}), "/responses")
            self.assertEqual(response.status_code, 400)
            self.assertIn("session", response.body.decode())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.station.calls, 0)
        self.assertEqual(self.station.failures, 0)
        self.assertFalse(self.station.quota_exhausted)
        self.assertEqual(self.registry.route_activity_snapshot()["inflight"], 0)

    async def test_native_chat_and_responses_keep_client_protocol_and_body(self):
        for path, body in [("/chat/completions", {"messages": [{"role": "user", "content": "Write a sort"}]}),
                           ("/responses", {"input": "Explain this sort"})]:
            response = await self.proxy._dispatch(Request({"model": "glm-test", **body},
                                                          {"session-id": "sort-session"}), path)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(self.requests[-1].url.path.endswith("/go/openai/v1" + path))
            self.assertEqual(json.loads(self.requests[-1].content), {"model": "glm-test", **body})

    async def test_unsupported_protocol_is_local_but_responses_tools_are_preserved(self):
        response = await self.proxy._dispatch(Request({"model": "gpt-test", "input": "text"}, {"session-id": "s"}), "/embeddings")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.requests, [])
        body = {"model": "glm-test", "input": [{"type": "function_call_output", "call_id": "c1", "output": "code"}],
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}]}
        response = await self.proxy._dispatch(Request(body, {"session-id": "s"}), "/responses")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.requests[0].content), body)
        self.assertTrue(self.requests[0].url.path.endswith('/responses'))

    async def test_go_quota_error_never_switches_to_balance_endpoint(self):
        self.http_status = 402
        await self.proxy._dispatch(Request({"model": "gpt-test", "input": "code"}, {"session-id": "s"}), "/responses")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("/inference/go/", self.requests[0].url.path)
        self.assertTrue(self.station.quota_exhausted)
        self.assertEqual(self.station.cfg.opencode_plan, "go")

    async def test_go_generic_generation_probe_is_skipped(self):
        monitor = HealthMonitor(self.registry, self.gateway)
        await monitor._generation_probe(self.station)
        self.assertEqual(self.requests, [])

    def test_catalog_and_explicit_route_restrictions(self):
        catalog = [{"id": "gpt", "type": "chat", "api": {"responses": "/not-followed"}},
                   {"id": "glm", "type": "chat", "api": {"chat-completions": "/not-followed"}},
                   {"id": "native", "type": "chat", "api": {"messages": "/not-followed"}},
                   {"id": "client-free", "type": "chat", "api": {"chat-completions": "/not-followed"}}]
        paths, unavailable = opencode_capabilities(catalog)
        self.assertEqual(set(paths), {"gpt", "glm", "native"})
        self.assertEqual(paths["native"], ["/messages"])
        self.assertEqual(set(unavailable), {"client-free"})
        go_paths, go_unavailable = opencode_capabilities(catalog, "go")
        self.assertFalse(go_unavailable)
        for model in ("gpt", "glm", "native", "client-free"):
            self.assertEqual(set(go_paths[model]), {"/responses", "/chat/completions", "/messages", "/generateContent"})
        self.station.cfg.route_models = ["gpt-*"]
        self.assertEqual(self.station.effective_models, {"gpt-test"})
        self.assertEqual(self.proxy.router.pick("glm-test", "/chat/completions"), [])

    def test_go_configuration_and_honest_gateway_identity(self):
        self.assertEqual(self.station.cfg.models_url, "https://opencode.ai/inference/go/v1/models")
        self.assertEqual(self.station.cfg.billing, "subscription")
        self.assertFalse(self.station.cfg.probe_chat)
        self.assertFalse(self.station.cfg.probe_balance)
        headers = inference_headers(self.station.cfg, {"user-agent": "python-httpx/1", "session-id": "s"})
        self.assertEqual(headers["user-agent"], "ModelHarbor/" + __version__ + " via python-httpx/1")
