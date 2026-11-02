"""Native Anthropic/Gemini integration: fixtures only, no external requests."""
import asyncio
import json
import tempfile
import unittest

import httpx

from app.config import AppConfig, GatewayConfig, StationConfig
from app.health import HealthMonitor
from app.providers import inference_headers, opencode_capabilities
from app.protocols import inference_url
from app.proxy import _StreamUsageParser, _extract_usage
from app.visual_tests import decode_response, VisualTestStore, VisualTestRunner
from gateway import build_app


ANTHROPIC = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": "anth-test",
             "content": [{"type": "text", "text": "<svg></svg>"}], "stop_reason": "end_turn",
             "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5}}
GEMINI = {"candidates": [{"content": {"role": "model", "parts": [
    {"functionCall": {"name": "inspect", "args": {"path": "src"}}, "thoughtSignature": "opaque-signature"},
    {"text": "<svg></svg>"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "thoughtsTokenCount": 3,
                      "totalTokenCount": 15, "cachedContentTokenCount": 4}}


def sse(data):
    return ("data: " + json.dumps(data) + "\n\n").encode()


ANTH_STREAM = (sse({"type": "message_start", "message": {**ANTHROPIC, "content": [], "usage": {**ANTHROPIC["usage"], "output_tokens": 0}}})
               + sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool-1", "name": "inspect", "input": {}}})
               + sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":"src"}'}})
               + sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}})
               + sse({"type": "message_stop"}))


class Fragments(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.closed = False

    async def __aiter__(self):
        for i in range(0, len(self.data), 11):
            yield self.data[i:i + 11]

    async def aclose(self):
        self.closed = True


class NativeProtocolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = GatewayConfig(data_dir=self.tmp.name, allowed_ips=["127.0.0.1"], api_keys=["gateway-key"],
                                 max_rounds=1, retry_backoff=0, sse_keepalive_interval=0)
        stations = [StationConfig.from_dict(name, {"base_url": root, "api_key": secret,
                    "provider": provider, "models": [model], "probe_models": False})
                    for name, provider, root, secret, model in (
                        ("Anth", "anthropic", "https://anth.invalid", "anth-upstream", "anth-test"),
                        ("Gem", "gemini", "https://gem.invalid", "gem-upstream", "gem-test"))]
        self.app = build_app(AppConfig(gateway=self.cfg, stations=stations))
        self.runtime = self.app.state.accounts.active
        self.registry = self.runtime.registry
        self.sent = []
        self.error_status = None
        self.streams = []

        async def handle(request):
            self.sent.append(request)
            if self.error_status:
                return httpx.Response(self.error_status, json={"error": {"message": "fixture bad argument"}})
            body = json.loads(request.content)
            anth = request.url.host == "anth.invalid"
            if body.get("stream") or request.url.params.get("alt") == "sse":
                stream = Fragments(ANTH_STREAM if anth else sse(GEMINI))
                self.streams.append(stream)
                return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
            if request.url.path.endswith(":streamGenerateContent"):
                return httpx.Response(200, json=[GEMINI])
            return httpx.Response(200, json=ANTHROPIC if anth else GEMINI)

        for st in self.registry.states.values():
            st.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 12345)), base_url="http://gateway.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.registry.close_clients()
        await self.runtime.notifier.close()
        self.tmp.cleanup()

    async def test_anthropic_full_body_and_native_headers(self):
        body = {"model": "anth-test", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "inspect"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "fixture"}}]}],
            "system": [{"type": "text", "text": "code", "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": "inspect", "input_schema": {"type": "object"}}], "max_tokens": 64}
        r = await self.client.post("/v1/messages", headers={"x-api-key": "gateway-key", "anthropic-version": "2023-06-01", "anthropic-beta": "fixture-beta"}, json=body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), ANTHROPIC)
        sent = self.sent[0]
        self.assertEqual(str(sent.url), "https://anth.invalid/v1/messages")
        self.assertEqual(json.loads(sent.content), body)
        self.assertEqual(sent.headers["x-api-key"], "anth-upstream")
        self.assertEqual(sent.headers["anthropic-beta"], "fixture-beta")
        self.assertNotIn("authorization", sent.headers)
        logs = (await self.client.get("/admin/logs")).json()["events"]
        self.assertEqual(logs[0]["client_ip"], "127.0.0.1")
        self.assertEqual(logs[0]["tokens"], 38)

    async def test_gemini_body_and_signature_and_both_entry_prefixes(self):
        body = {"contents": [{"role": "model", "parts": [GEMINI["candidates"][0]["content"]["parts"][0]]},
                             {"role": "user", "parts": [{"functionResponse": {"name": "inspect", "response": {"ok": True}}}]}],
                "generationConfig": {"maxOutputTokens": 64, "thinkingConfig": {"thinkingBudget": 16}},
                "tools": [{"functionDeclarations": [{"name": "inspect", "parameters": {"type": "OBJECT"}}]}]}
        for prefix in ("/v1", "/v1beta"):
            r = await self.client.post(prefix + "/models/gem-test:generateContent",
                                       headers={"x-goog-api-key": "gateway-key"}, json=body)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), GEMINI)
            sent = self.sent[-1]
            self.assertEqual(str(sent.url), "https://gem.invalid/v1beta/models/gem-test:generateContent")
            self.assertEqual(sent.headers["x-goog-api-key"], "gem-upstream")
            self.assertNotIn("authorization", sent.headers)
            self.assertEqual(json.loads(sent.content), body)

    async def test_streams_preserve_frames_tools_and_cumulative_usage(self):
        r = await self.client.post("/v1/messages", headers={"x-api-key": "gateway-key"},
                                  json={"model": "anth-test", "messages": [], "max_tokens": 64, "stream": True})
        self.assertEqual(r.content, ANTH_STREAM)
        r = await self.client.post("/v1beta/models/gem-test:streamGenerateContent?alt=sse",
                                  headers={"x-goog-api-key": "gateway-key"}, json={"contents": []})
        self.assertEqual(r.content, sse(GEMINI))
        logs = (await self.client.get("/admin/logs")).json()["events"]
        self.assertEqual({item["station"]: item["tokens"] for item in logs}, {"Anth": 42, "Gem": 15})
        self.assertTrue(all(stream.closed for stream in self.streams))
        self.assertEqual(self.registry.route_activity_snapshot()["inflight"], 0)

    async def test_google_stream_default_json_array(self):
        r = await self.client.post("/v1beta/models/gem-test:streamGenerateContent",
                                  headers={"x-goog-api-key": "gateway-key"}, json={"contents": []})
        self.assertEqual(r.json(), [GEMINI])
        self.assertEqual((await self.client.get("/admin/logs")).json()["events"][0]["tokens"], 15)

    async def test_model_lists_auth_and_wrong_protocol(self):
        r = await self.client.get("/v1beta/models")
        self.assertEqual(r.status_code, 401)
        r = await self.client.get("/v1beta/models", headers={"x-goog-api-key": "gateway-key"})
        self.assertEqual([model["name"] for model in r.json()["models"]], ["models/gem-test"])
        for path, body in [("/v1/messages", {"model": "gem-test"}), ("/v1/responses", {"model": "anth-test"})]:
            r = await self.client.post(path, headers={"Authorization": "Bearer gateway-key"}, json=body)
            self.assertEqual(r.status_code, 400)
        self.assertFalse(self.sent)

    async def test_ip_policy_covers_native_google_routes(self):
        self.runtime.gateway.allowed_ips = ["10.1.1.1"]
        r = await self.client.post("/v1beta/models/gem-test:generateContent",
                                  headers={"x-goog-api-key": "gateway-key"}, json={"contents": []})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.sent)

    async def test_errors_keep_native_shape_without_retry(self):
        self.error_status = 400
        r = await self.client.post("/v1/messages", headers={"x-api-key": "gateway-key"}, json={"model": "anth-test"})
        self.assertEqual(r.json()["type"], "error")
        r = await self.client.post("/v1beta/models/gem-test:generateContent",
                                  headers={"x-goog-api-key": "gateway-key"}, json={"contents": []})
        self.assertEqual(r.json()["error"]["status"], "INVALID_ARGUMENT")
        self.assertEqual(len(self.sent), 2)

    async def test_native_visual_tests_use_matching_payloads(self):
        runner = VisualTestRunner(self.registry, self.cfg, VisualTestStore(self.tmp.name))
        for name, model, protocol in (("Anth", "anth-test", "messages"), ("Gem", "gem-test", "generate_content")):
            result = await runner.run(self.registry.get(name), {"model": model, "prompt": "Draw SVG",
                "protocol": "auto", "max_output_tokens": 20000, "reasoning_effort": "auto"}, "127.0.0.1")
            self.assertEqual(result["protocol"], protocol)
            sent = self.sent[-1]
            payload = json.loads(sent.content)
            if name == "Anth":
                self.assertEqual(payload["max_tokens"], 20000)
                self.assertNotIn("stream_options", payload)
            else:
                self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 20000)
                self.assertNotIn("model", payload)
                self.assertIn("<svg>", result["raw_text"])

    async def test_native_catalog_pagination_and_credentials(self):
        for provider, name in (("anthropic", "Anth"), ("gemini", "Gem")):
            st = self.registry.get(name)
            st.cfg.probe_models = True
            await st.client.aclose()
            requests = []
            def handle(request):
                requests.append(request)
                if provider == "anthropic":
                    return httpx.Response(200, json={"data": [{"id": "a" if len(requests) == 1 else "b"}],
                        "has_more": len(requests) == 1, "last_id": "a"})
                return httpx.Response(200, json={"models": [{"name": "models/a" if len(requests) == 1 else "models/b",
                    "supportedGenerationMethods": ["generateContent"]}], **({"nextPageToken": "next-page"} if len(requests) == 1 else {})})
            st.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            await self.runtime.monitor.discover_models(st, manual=True)
            self.assertEqual(st.probed_models, {"a", "b"})
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1].url.params["after_id" if provider == "anthropic" else "pageToken"], "a" if provider == "anthropic" else "next-page")
            self.assertIn("x-api-key" if provider == "anthropic" else "x-goog-api-key", requests[0].headers)

    async def test_go_messages_and_gemini_use_go_host_and_real_session(self):
        cfg = StationConfig.from_dict("Go", {"provider": "opencode", "opencode_plan": "go", "base_url": "https://opencode.ai", "api_key": "go-key"})
        st = self.registry.add_station(cfg)
        st.probed_models = {"qwen-native", "gemini-native"}
        st.model_api_paths = {"qwen-native": ["/messages"], "gemini-native": ["/generateContent"]}
        await st.client.aclose()
        sent = []
        def handle(request):
            sent.append(request)
            return httpx.Response(200, json=ANTHROPIC if request.url.path.endswith("/messages") else GEMINI)
        st.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        for path, body, suffix in [("/v1/messages", {"model": "qwen-native", "messages": [], "max_tokens": 10}, "/anthropic/v1/messages"),
                                  ("/v1beta/models/gemini-native:generateContent", {"contents": []}, "/google/v1beta/models/gemini-native:generateContent")]:
            r = await self.client.post(path, headers={"Authorization": "Bearer gateway-key", "session-id": "real-conversation"}, json=body)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(str(sent[-1].url), "https://opencode.ai/inference/go" + suffix)
            self.assertEqual(sent[-1].headers["session-id"], "real-conversation")
            self.assertEqual(sent[-1].headers["authorization"], "Bearer go-key")

    async def test_midstream_error_does_not_become_success(self):
        st = self.registry.get("Anth")
        await st.client.aclose()
        content = (sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}})
                   + sse({"type": "error", "error": {"type": "overloaded_error", "message": "overloaded fixture"}}))
        st.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Fragments(content), headers={"content-type": "text/event-stream"})))
        r = await self.client.post("/v1/messages", headers={"x-api-key": "gateway-key"}, json={"model": "anth-test", "messages": [], "max_tokens": 10, "stream": True})
        self.assertEqual(r.content, content)
        last = self.registry.route_activity_snapshot()["last_attempt"]
        self.assertEqual(last["outcome"], "stream_error")
        self.assertEqual(self.registry.route_activity_snapshot()["inflight"], 0)

    async def test_incomplete_native_streams_are_recorded_as_failures(self):
        cases = [
            ("Anth", "/v1/messages", {"model": "anth-test", "stream": True},
             {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}}),
            ("Gem", "/v1beta/models/gem-test:streamGenerateContent?alt=sse", {"contents": []},
             {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}),
        ]
        for station, path, body, payload in cases:
            st = self.registry.get(station)
            await st.client.aclose()
            st.client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Fragments(sse(payload)), headers={"content-type": "text/event-stream"})))
            response = await self.client.post(path, headers={"Authorization": "Bearer gateway-key"}, json=body)
            self.assertIn(b'"error"', response.content)
            self.assertEqual(self.registry.route_activity_snapshot()["last_attempt"]["outcome"], "stream_error")
            logs = (await self.client.get("/admin/logs")).json()["events"]
            self.assertFalse(logs[0]["ok"])
        self.assertEqual(self.registry.route_activity_snapshot()["inflight"], 0)

    async def test_keepalive_does_not_cancel_slow_native_stream(self):
        class SlowStream(httpx.AsyncByteStream):
            closed = False
            cancelled = False
            async def __aiter__(self):
                yield sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "start"}})
                try:
                    await asyncio.sleep(.08)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                yield sse({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "end"}})
                yield sse({"type": "message_stop"})
            async def aclose(self):
                self.closed = True
        stream = SlowStream()
        self.runtime.gateway.sse_keepalive_interval = .01
        st = self.registry.get("Anth")
        await st.client.aclose()
        st.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})))
        response = await self.client.post("/v1/messages", headers={"x-api-key": "gateway-key"}, json={"model": "anth-test", "stream": True})
        self.assertIn(b': keep-alive', response.content)
        self.assertIn(b'message_stop', response.content)
        self.assertFalse(stream.cancelled)
        self.assertTrue(stream.closed)
        self.assertEqual(self.registry.route_activity_snapshot()["last_attempt"]["outcome"], "success")

    async def test_catalog_invalid_second_page_preserves_previous_models(self):
        st = self.registry.get("Gem")
        st.probed_models = {"previous"}
        st.cfg.probe_models = True
        await st.client.aclose()
        st.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "models": [{"name": "models/new", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "same-token"})))
        await self.runtime.monitor.discover_models(st, manual=True)
        self.assertEqual(st.probed_models, {"previous"})
        self.assertEqual(st.models_discovery_status, "invalid_response")


class NativeUnitTests(unittest.TestCase):
    def test_anthropic_partial_usage_retains_cached_input(self):
        parser = _StreamUsageParser()
        parser.feed(sse({"type": "message_start", "message": ANTHROPIC}))
        parser.feed(sse({"type": "message_delta", "usage": {"input_tokens": 12, "output_tokens": 8}}))
        self.assertEqual((parser.last.input_tokens, parser.last.tokens), (37, 45))
        parser.feed(sse({"type": "message_delta", "usage": {"cache_read_input_tokens": 0, "output_tokens": 9}}))
        self.assertEqual((parser.last.input_tokens, parser.last.tokens), (17, 26))

    def test_go_native_paths_and_endpoint_lock(self):
        cfg = StationConfig.from_dict("Go", {"provider": "opencode", "opencode_plan": "go", "base_url": "https://opencode.ai", "api_key": "fixture"})
        self.assertEqual(inference_url(cfg, "/messages", "qwen"), "https://opencode.ai/inference/go/anthropic/v1/messages")
        self.assertEqual(inference_url(cfg, "/streamGenerateContent?alt=sse", "gemini"), "https://opencode.ai/inference/go/google/v1beta/models/gemini:streamGenerateContent?alt=sse")
        with self.assertRaises(ValueError):
            inference_url(cfg, "/generateContent", "../other")
        headers = inference_headers(cfg, {"session-id": "real-session", "anthropic-version": "2023-06-01"}, path="/messages")
        self.assertEqual(headers["session-id"], "real-session")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        paths, unavailable = opencode_capabilities([{"id": "qwen", "type": "chat", "api": {"messages": "https://evil.invalid"}}], "balance")
        self.assertEqual(paths["qwen"], ["/messages"])
        self.assertFalse(unavailable)

    def test_native_usage_and_visual_content(self):
        usage = _extract_usage(json.dumps(ANTHROPIC).encode())
        self.assertEqual((usage.tokens, usage.input_tokens, usage.cache_read, usage.cache_write), (38, 35, 20, 5))
        usage = _extract_usage(json.dumps(GEMINI).encode())
        self.assertEqual((usage.tokens, usage.input_tokens, usage.output_tokens), (15, 10, 5))
        parser = _StreamUsageParser()
        for byte in ANTH_STREAM:
            parser.feed(bytes([byte]))
        parser.finish()
        self.assertEqual(parser.last.tokens, 42)
        text, usage, truncated, error = decode_response(json.dumps(ANTHROPIC).encode(), "application/json")
        self.assertEqual(text, "<svg></svg>")
        self.assertEqual(usage["total_tokens"], 38)
        self.assertFalse(truncated or error)
        text, usage, truncated, error = decode_response(sse(GEMINI), "text/event-stream")
        self.assertEqual(text, "<svg></svg>")
        self.assertEqual(usage["total_tokens"], 15)
        self.assertFalse(truncated or error)
