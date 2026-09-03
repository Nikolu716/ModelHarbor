"""Backend tests for accurate current and recent route activity."""
from __future__ import annotations

import asyncio
import gc
import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from starlette.responses import Response


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import GatewayConfig, StationConfig
from app.proxy import (
    OpenAIProxy,
    _extract_stream_tokens,
    _extract_tokens,
    _StreamUsageParser,
)
from app.state import Registry, STATUS_DOWN
from app.stats import EventLog


def make_registry() -> tuple[Registry, GatewayConfig]:
    gateway = GatewayConfig(
        max_attempts=2,
        max_rounds=1,
        first_byte_timeout=2.0,
        read_timeout=2.0,
        sse_keepalive_interval=0.0,
    )
    stations = [
        StationConfig(
            name="alpha",
            base_url="http://alpha.test",
            api_key="test-alpha",
            models=["model-a", "model-b"],
            max_concurrency=20,
        ),
        StationConfig(
            name="beta",
            base_url="http://beta.test",
            api_key="test-beta",
            models=["model-a"],
            max_concurrency=20,
        ),
    ]
    return Registry(gateway, stations), gateway


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, release: asyncio.Event):
        self.release = release
        self.closed = False

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        await self.release.wait()
        yield b'data: {"type":"response.completed","response":{"us'
        yield b'age":{"input_tokens":8,"output_tokens":3}}}\n\n'
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


class DispatchRequest:
    """Small Request-compatible double for retry-loop tests."""

    def __init__(self, body: dict, disconnect_on_check: int | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {}
        self.disconnect_on_check = disconnect_on_check
        self.disconnect_checks = 0

    async def json(self) -> dict:
        return self.body

    async def is_disconnected(self) -> bool:
        self.disconnect_checks += 1
        return (self.disconnect_on_check is not None
                and self.disconnect_checks >= self.disconnect_on_check)


class RegistryActivityTests(unittest.TestCase):
    def test_retry_configuration_validation_and_status_snapshot(self) -> None:
        cfg = GatewayConfig.from_dict({
            "max_rounds": 0,
            "round_backoff": 0.25,
            "max_round_backoff": 4,
            "max_retry_seconds": 120,
        })
        self.assertEqual(cfg.max_rounds, 0)
        self.assertEqual(cfg.max_round_backoff, 4.0)
        self.assertEqual(cfg.max_retry_seconds, 120.0)
        with self.assertRaises(ValueError):
            GatewayConfig.from_dict({"max_rounds": -1})
        with self.assertRaises(ValueError):
            GatewayConfig.from_dict({"max_round_backoff": 0})
        with self.assertRaises(ValueError):
            GatewayConfig.from_dict({
                "round_backoff": 2,
                "max_round_backoff": 1,
            })
        with self.assertRaises(ValueError):
            GatewayConfig.from_dict({
                "max_rounds": 0,
                "max_retry_seconds": 0,
                "round_backoff": 1,
            })

        registry, gateway = make_registry()
        gateway.max_rounds = 0
        gateway.max_round_backoff = 6.0
        gateway.max_retry_seconds = 90.0
        status = registry.snapshot()["gateway"]
        self.assertEqual(status["max_rounds"], 0)
        self.assertEqual(status["max_round_backoff"], 6.0)
        self.assertEqual(status["max_retry_seconds"], 90.0)

    def test_activity_subscription_pushes_route_transitions(self) -> None:
        async def scenario() -> None:
            registry, _ = make_registry()
            subscriber_id, queue = registry.subscribe_route_activity()
            try:
                initial = await asyncio.wait_for(queue.get(), timeout=0.5)
                self.assertFalse(initial["active"])

                route_id = registry.begin_route("alpha", "model-a")
                started = await asyncio.wait_for(queue.get(), timeout=0.5)
                self.assertTrue(started["active"])
                self.assertEqual(
                    started["active_routes"][0]["phase"], "connecting")

                registry.update_route_phase(route_id, "streaming")
                streaming = await asyncio.wait_for(queue.get(), timeout=0.5)
                self.assertEqual(
                    streaming["active_routes"][0]["phase"], "streaming")

                registry.end_route(route_id, "success")
                ended = await asyncio.wait_for(queue.get(), timeout=0.5)
                self.assertFalse(ended["active"])
                self.assertEqual(ended["last_attempt"]["outcome"], "success")
                self.assertGreater(ended["revision"], initial["revision"])
            finally:
                registry.unsubscribe_route_activity(subscriber_id)

        asyncio.run(scenario())

    def test_non_stream_responses_nested_usage_is_counted(self) -> None:
        payload = json.dumps({
            "response": {"usage": {"input_tokens": 5, "output_tokens": 2}},
        }).encode()
        self.assertEqual(_extract_tokens(payload), 7)

    def test_responses_completed_nested_usage_is_counted(self) -> None:
        chunk = (
            b'data: {"usage":{"total_tokens":4}}\n\n'
            b'data: {"type":"response.completed","response":{"usage":'
            b'{"input_tokens":8,"output_tokens":3}}}\n\n'
        )
        self.assertEqual(_extract_stream_tokens(chunk), 11)

    def test_stream_usage_parser_handles_split_json_and_multiple_events(self) -> None:
        payload = (
            b'data: {"usage":{"total_tokens":4}}\n\n'
            b'event: response.completed\n'
            b'data: {"type":"response.completed","response":{"usage":'
            b'{"input_tokens":13,"output_tokens":8}}}\n\n'
            b'data: [DONE]\n\n'
        )
        # Split inside field names, numbers, braces and SSE delimiters.
        split_at = [1, 7, 28, 42, 57, 74, 91, 108, 125, len(payload) - 1]
        parser = _StreamUsageParser()
        observed = []
        start = 0
        for end in split_at + [len(payload)]:
            value = parser.feed(payload[start:end])
            if value is not None:
                observed.append(value)
            start = end
        tail = parser.finish()
        if tail is not None:
            observed.append(tail)
        self.assertEqual(observed, [4, 21])

    def test_stream_usage_parser_preserves_reported_zero(self) -> None:
        parser = _StreamUsageParser()
        self.assertEqual(
            parser.feed(b'data: {"usage":{"total_tokens":0}}\n\n'), 0)
        self.assertIsNone(parser.finish())

    def test_concurrent_routes_are_grouped_without_losing_model_detail(self) -> None:
        registry, _ = make_registry()
        first = registry.begin_route("alpha", "model-a")
        second = registry.begin_route("alpha", "model-b")
        third = registry.begin_route("beta", "model-a")
        registry.update_route_phase(second, "receiving")

        snapshot = registry.snapshot()
        activity = snapshot["route_activity"]
        self.assertTrue(activity["active"])
        self.assertEqual(activity["inflight"], 3)
        self.assertEqual(snapshot["active_routes"], activity["active_routes"])
        self.assertEqual(snapshot["active_stations"], activity["active_stations"])

        stations = {item["station"]: item for item in activity["active_stations"]}
        self.assertEqual(stations["alpha"]["inflight"], 2)
        self.assertEqual(
            stations["alpha"]["models"],
            [
                {"model": "model-a", "inflight": 1},
                {"model": "model-b", "inflight": 1},
            ],
        )
        self.assertEqual(stations["beta"]["inflight"], 1)
        self.assertEqual(
            {item["id"]: item["phase"] for item in activity["active_routes"]},
            {first: "connecting", second: "receiving", third: "connecting"},
        )

        # An older attempt finishing later must not overwrite the newest attempt.
        registry.end_route(second, "failed", "upstream_error")
        self.assertEqual(registry.snapshot()["last_attempt"]["id"], third)
        registry.end_route(third, "success")
        registry.end_route(first, "failed", "timeout")
        final = registry.snapshot()
        self.assertFalse(final["route_activity"]["active"])
        self.assertEqual(final["route_activity"]["inflight"], 0)
        self.assertEqual(final["last_attempt"]["id"], third)
        self.assertEqual(final["last_attempt"]["outcome"], "success")

        registry.mark_routed("beta", "model-a", 12.34)
        success = registry.snapshot()
        self.assertEqual(success["last_success"]["station"], "beta")
        self.assertEqual(success["last_routed"], success["last_success"])

    def test_activity_methods_are_thread_safe_and_route_ids_are_unique(self) -> None:
        registry, _ = make_registry()

        def run(index: int) -> int:
            route_id = registry.begin_route("alpha", f"model-{index % 3}")
            registry.update_route_phase(route_id, "receiving")
            registry.end_route(route_id, "success")
            return route_id

        with ThreadPoolExecutor(max_workers=12) as executor:
            route_ids = list(executor.map(run, range(200)))

        self.assertEqual(len(set(route_ids)), 200)
        activity = registry.route_activity_snapshot()
        self.assertFalse(activity["active"])
        self.assertEqual(activity["inflight"], 0)
        self.assertEqual(activity["last_attempt"]["outcome"], "success")


class ProxyActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_candidates_during_cooldown_are_retryable(self) -> None:
        registry, gateway = make_registry()
        alpha = registry.get("alpha")
        beta = registry.get("beta")
        assert alpha is not None and beta is not None
        # Keep the model configured, but make every matching station
        # temporarily unavailable.  This is the state that previously
        # returned ``has_transient=False`` and skipped gateway retries.
        alpha.status = STATUS_DOWN
        alpha.down_since = time.time()
        beta.enabled = False

        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            response, transient = await proxy._forward_json(
                "/responses", {"model": "model-a"}, "model-a")

        self.assertEqual(response.status_code, 502)
        self.assertTrue(transient)
        await registry.close_clients()

    async def test_empty_candidates_for_unknown_model_are_not_retryable(self) -> None:
        registry, gateway = make_registry()
        for station in registry.states.values():
            station.enabled = True

        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            response, transient = await proxy._forward_json(
                "/responses", {"model": "model-not-configured"},
                "model-not-configured")

        self.assertEqual(response.status_code, 502)
        self.assertFalse(transient)
        await registry.close_clients()

    async def test_unknown_model_fails_fast_without_triggering_client_retries(self) -> None:
        registry, gateway = make_registry()
        gateway.max_rounds = 0
        gateway.max_retry_seconds = 1.0
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            started = time.monotonic()
            result = await proxy._dispatch(
                DispatchRequest({"model": "model-not-configured"}),
                "/responses")
            elapsed = time.monotonic() - started

        self.assertEqual(result.status_code, 400)
        self.assertLess(elapsed, 0.1)
        await registry.close_clients()

    async def test_non_string_model_is_rejected_as_invalid_request(self) -> None:
        registry, gateway = make_registry()
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            result = await proxy._dispatch(
                DispatchRequest({"model": ["model-a"]}),
                "/responses",
            )

        self.assertEqual(result.status_code, 400)
        await registry.close_clients()

    async def test_model_discovery_startup_window_remains_retryable(self) -> None:
        registry, gateway = make_registry()
        gateway.max_rounds = 2
        gateway.round_backoff = 0.01
        gateway.max_round_backoff = 0.01
        alpha = registry.get("alpha")
        assert alpha is not None
        alpha.cfg.probe_models = True
        calls = 0

        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))

            async def discover_then_forward(path: str, body: dict, model: str):
                nonlocal calls
                calls += 1
                if calls == 1:
                    alpha.probed_models.add(model)
                    alpha.last_probe_ok = True
                    return Response(status_code=502), True
                return Response(status_code=200), False

            proxy._forward_json = discover_then_forward  # type: ignore[method-assign]
            result = await proxy._dispatch(
                DispatchRequest({"model": "newly-discovered-model"}),
                "/responses",
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls, 2)
        await registry.close_clients()

    async def test_model_with_only_disabled_stations_returns_422(self) -> None:
        registry, gateway = make_registry()
        for station in registry.states.values():
            station.enabled = False
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            result = await proxy._dispatch(
                DispatchRequest({"model": "model-a"}),
                "/responses",
            )

        self.assertEqual(result.status_code, 422)
        await registry.close_clients()

    async def test_model_with_only_exhausted_stations_returns_422(self) -> None:
        registry, gateway = make_registry()
        for station in registry.states.values():
            station.quota_exhausted = True
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            result = await proxy._dispatch(
                DispatchRequest({"model": "model-a"}),
                "/responses",
            )

        self.assertEqual(result.status_code, 422)
        await registry.close_clients()

    async def test_dispatch_retries_transient_empty_candidates(self) -> None:
        registry, gateway = make_registry()
        gateway.max_rounds = 2
        gateway.round_backoff = 0.0
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            calls = 0

            async def flaky_forward(path: str, body: dict, model: str):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return Response(status_code=502), True
                return Response(status_code=200), True

            proxy._forward_json = flaky_forward  # type: ignore[method-assign]
            request = DispatchRequest(
                {"model": "model-a"},
                headers={"x-stainless-retry-count": "4"},
            )
            result = await proxy._dispatch(request, "/responses")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls, 2)
        await registry.close_clients()

    async def test_unlimited_rounds_recover_before_total_deadline(self) -> None:
        registry, gateway = make_registry()
        gateway.max_rounds = 0
        gateway.max_retry_seconds = 1.0
        gateway.round_backoff = 0.0
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            calls = 0

            async def recovering_forward(path: str, body: dict, model: str):
                nonlocal calls
                calls += 1
                if calls < 3:
                    return Response(status_code=502), True
                return Response(status_code=200), False

            proxy._forward_json = recovering_forward  # type: ignore[method-assign]
            result = await proxy._dispatch(
                DispatchRequest({"model": "model-a"}), "/responses")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls, 3)
        await registry.close_clients()

    async def test_original_request_recovers_after_station_cooldown(self) -> None:
        registry, gateway = make_registry()
        gateway.max_rounds = 0
        gateway.max_retry_seconds = 1.0
        gateway.round_backoff = 0.01
        gateway.max_round_backoff = 0.02
        gateway.cool_down = 0.05
        alpha = registry.get("alpha")
        beta = registry.get("beta")
        assert alpha is not None and beta is not None
        alpha.status = STATUS_DOWN
        alpha.down_since = time.time()
        beta.enabled = False
        upstream_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            return httpx.Response(200, json={"id": "recovered"})

        alpha.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            proxy = OpenAIProxy(registry, gateway, eventlog)
            started = time.monotonic()
            result = await proxy._dispatch(
                DispatchRequest({"model": "model-a"}),
                "/responses",
            )
            elapsed = time.monotonic() - started
            stats = eventlog.daily_stats()

        self.assertEqual(result.status_code, 200)
        self.assertGreaterEqual(elapsed, gateway.cool_down * 0.75)
        self.assertEqual(upstream_calls, 1)
        self.assertEqual(alpha.status, "up")
        self.assertEqual(len(eventlog.events), 1)
        self.assertEqual(stats["total_calls"], 1)
        self.assertEqual(stats["total_failures"], 0)
        await registry.close_clients()

    async def test_exhausted_retry_rounds_record_one_client_failure(self) -> None:
        registry, gateway = make_registry()
        gateway.max_rounds = 3
        gateway.max_retry_seconds = 1.0
        gateway.round_backoff = 0.0
        calls = 0
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            proxy = OpenAIProxy(registry, gateway, eventlog)

            async def unavailable_forward(path: str, body: dict, model: str):
                nonlocal calls
                calls += 1
                return Response(status_code=502), True

            proxy._forward_json = unavailable_forward  # type: ignore[method-assign]
            result = await proxy._dispatch(
                DispatchRequest({"model": "model-a"}), "/responses")
            stats = eventlog.daily_stats()

        self.assertEqual(result.status_code, 502)
        self.assertEqual(calls, 3)
        self.assertEqual(len(eventlog.events), 1)
        self.assertEqual(stats["total_calls"], 1)
        self.assertEqual(stats["total_failures"], 1)
        await registry.close_clients()

    async def test_total_retry_timeout_includes_backoff_wait(self) -> None:
        registry, gateway = make_registry()
        gateway.max_rounds = 0
        gateway.max_retry_seconds = 0.03
        gateway.round_backoff = 1.0
        gateway.max_round_backoff = 1.0
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            calls = 0

            async def unavailable_forward(path: str, body: dict, model: str):
                nonlocal calls
                calls += 1
                return Response(status_code=502), True

            proxy._forward_json = unavailable_forward  # type: ignore[method-assign]
            started = time.monotonic()
            result = await proxy._dispatch(
                DispatchRequest({"model": "model-a"}), "/responses")
            elapsed = time.monotonic() - started

        self.assertEqual(result.status_code, 502)
        self.assertIn("总时限", result.body.decode("utf-8"))
        self.assertEqual(calls, 1)
        self.assertLess(elapsed, 0.25)
        await registry.close_clients()

    async def test_total_retry_timeout_cancels_inflight_upstream_work(self) -> None:
        registry, gateway = make_registry()
        gateway.max_rounds = 0
        gateway.max_retry_seconds = 0.03
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            cancelled = False

            async def slow_forward(path: str, body: dict, model: str):
                nonlocal cancelled
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                return Response(status_code=200), False

            proxy._forward_json = slow_forward  # type: ignore[method-assign]
            started = time.monotonic()
            result = await proxy._dispatch(
                DispatchRequest({"model": "model-a"}), "/responses")
            elapsed = time.monotonic() - started

        self.assertEqual(result.status_code, 502)
        self.assertTrue(cancelled)
        self.assertLess(elapsed, 0.25)
        await registry.close_clients()

    async def test_disconnect_stops_retry_during_backoff(self) -> None:
        registry, gateway = make_registry()
        gateway.max_rounds = 0
        gateway.max_retry_seconds = 1.0
        gateway.round_backoff = 0.5
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            calls = 0

            async def unavailable_forward(path: str, body: dict, model: str):
                nonlocal calls
                calls += 1
                return Response(status_code=502), True

            proxy._forward_json = unavailable_forward  # type: ignore[method-assign]
            result = await proxy._dispatch(
                DispatchRequest({"model": "model-a"}, disconnect_on_check=2),
                "/responses")

        self.assertEqual(result.status_code, 499)
        self.assertEqual(calls, 1)
        await registry.close_clients()

    async def test_json_route_is_visible_while_upstream_is_inflight(self) -> None:
        registry, gateway = make_registry()
        beta = registry.get("beta")
        assert beta is not None
        beta.enabled = False
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return httpx.Response(
                200,
                json={"id": "response", "usage": {"total_tokens": 7}},
            )

        station = registry.get("alpha")
        assert station is not None
        station.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            task = asyncio.create_task(
                proxy._forward_json("/chat/completions", {"model": "model-a"},
                                    "model-a"))
            try:
                await asyncio.wait_for(started.wait(), timeout=1.0)
                activity = registry.snapshot()["route_activity"]
                self.assertTrue(activity["active"])
                self.assertEqual(activity["inflight"], 1)
                self.assertEqual(activity["active_routes"][0]["station"], "alpha")
                self.assertEqual(activity["active_routes"][0]["model"], "model-a")
                self.assertIsNone(activity["last_success"])
            finally:
                release.set()

            response, transient = await task
            self.assertEqual(response.status_code, 200)
            self.assertFalse(transient)

        final = registry.snapshot()["route_activity"]
        self.assertFalse(final["active"])
        self.assertEqual(final["last_attempt"]["outcome"], "success")
        self.assertEqual(final["last_success"]["station"], "alpha")
        await registry.close_clients()

    async def test_stream_route_stays_active_until_stream_is_closed(self) -> None:
        registry, gateway = make_registry()
        beta = registry.get("beta")
        assert beta is not None
        beta.enabled = False
        release = asyncio.Event()
        controlled = ControlledStream(release)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=controlled)

        station = registry.get("alpha")
        assert station is not None
        station.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            recorded_tokens = []
            real_add_tokens = eventlog.add_tokens

            def record_tokens(station_name: str, model_name: str,
                              tokens: int, occurred_at: float | None = None,
                              **kwargs) -> None:
                recorded_tokens.append((station_name, model_name, tokens))
                real_add_tokens(
                    station_name, model_name, tokens,
                    occurred_at=occurred_at, **kwargs)

            eventlog.add_tokens = record_tokens
            proxy = OpenAIProxy(registry, gateway, eventlog)
            response, transient = await proxy._forward_stream(
                "/chat/completions", {"model": "model-b", "stream": True},
                "model-b")
            self.assertFalse(transient)

            activity = registry.snapshot()["route_activity"]
            self.assertTrue(activity["active"])
            self.assertEqual(activity["active_routes"][0]["phase"], "streaming")
            self.assertEqual(activity["last_attempt"]["outcome"], "inflight")
            self.assertEqual(activity["last_success"]["model"], "model-b")

            async def consume() -> bytes:
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                return b"".join(chunks)

            consume_task = asyncio.create_task(consume())
            await asyncio.sleep(0)
            self.assertTrue(registry.route_activity_snapshot()["active"])
            release.set()
            payload = await asyncio.wait_for(consume_task, timeout=1.0)
            self.assertIn(b"[DONE]", payload)

            final = registry.snapshot()["route_activity"]
            self.assertFalse(final["active"])
            self.assertEqual(final["last_attempt"]["outcome"], "success")
            self.assertTrue(controlled.closed)
            self.assertEqual(recorded_tokens, [("alpha", "model-b", 11)])

        await registry.close_clients()

    async def test_unconsumed_stream_response_releases_route_when_dropped(self) -> None:
        registry, gateway = make_registry()
        beta = registry.get("beta")
        assert beta is not None
        beta.enabled = False
        controlled = ControlledStream(asyncio.Event())

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=controlled)

        station = registry.get("alpha")
        assert station is not None
        station.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            response, _ = await proxy._forward_stream(
                "/chat/completions", {"model": "model-a", "stream": True},
                "model-a")
            self.assertTrue(registry.route_activity_snapshot()["active"])
            del response
            gc.collect()
            for _ in range(10):
                await asyncio.sleep(0)
                if not registry.route_activity_snapshot()["active"]:
                    break
            self.assertFalse(registry.route_activity_snapshot()["active"])
            self.assertTrue(controlled.closed)
            self.assertEqual(
                registry.route_activity_snapshot()["last_attempt"]["outcome"],
                "cancelled")

        await registry.close_clients()

    async def test_stream_tokens_stay_on_request_start_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            started = time.time() - 86400
            eventlog.add("alpha", "model-a", True, 10.0, tokens=None)
            # Move the just-created call to a stable synthetic previous day.
            today = time.strftime("%Y-%m-%d", time.localtime())
            previous = time.strftime("%Y-%m-%d", time.localtime(started))
            eventlog.daily[previous] = eventlog.daily.pop(today)
            eventlog.daily_models[previous] = eventlog.daily_models.pop(today)
            eventlog.add_tokens("alpha", "model-a", 9, occurred_at=started)
            self.assertEqual(eventlog.daily[previous]["alpha"]["tokens"], 9)
            self.assertNotIn(today, eventlog.daily)


if __name__ == "__main__":
    unittest.main()
