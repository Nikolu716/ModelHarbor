"""Quota metadata is read-only, cached, durable and isolated from routing health."""
import asyncio
import dataclasses
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from app.config import AppConfig, GatewayConfig, StationConfig
from app.go_usage import normalize_usage, URL
from app.health import HealthMonitor
from app.state import Registry, StationState
from gateway import build_app

PAYLOAD = {"usage": {key: {"percent": percent, "status": "ok", "resetsAt": "2026-11-09T00:00:00Z"}
                     for key, percent in (("rolling", 40), ("weekly", 16), ("monthly", 9))}}


class GoUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = StationConfig.from_dict("Go", {"provider": "opencode", "opencode_plan": "go",
                    "base_url": "https://opencode.ai", "api_key": "private-fixture", "models": ["model"], "probe_models": False})
        self.app = build_app(AppConfig(gateway=GatewayConfig(data_dir=self.tmp.name, allowed_ips=["127.0.0.1"]), stations=[cfg]))
        self.runtime = self.app.state.accounts.active
        self.registry = self.runtime.registry
        self.st = self.registry.get("Go")
        self.monitor = self.runtime.monitor
        self.requests = []
        self.response = httpx.Response(200, json=PAYLOAD)
        async def handle(request):
            self.requests.append(request)
            await asyncio.sleep(.01)
            return self.response
        if self.st.client:
            await self.st.client.aclose()
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://local")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.registry.close_clients()
        await self.runtime.notifier.close()
        self.tmp.cleanup()

    async def test_caching_singleflight_and_read_only_credentials(self):
        before = (self.st.calls, self.st.failures, self.st.health_score, self.st.status)
        await asyncio.gather(*(self.monitor.refresh_go_usage(self.st, manual=True) for _ in range(5)))
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), URL)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.headers["Authorization"], "Bearer private-fixture")
        self.assertNotIn("x-opencode-session", request.headers)
        self.assertEqual(self.st.go_usage_view()["windows"]["rolling"]["percent"], 40)
        self.assertEqual(before, (self.st.calls, self.st.failures, self.st.health_score, self.st.status))
        self.assertFalse(self.st.go_usage_view()["stale"])
        self.assertGreater(self.st.go_usage_next_at, time.time() + 895)

    async def test_errors_retain_cache_and_never_trip_breaker(self):
        await self.monitor.refresh_go_usage(self.st)
        self.st.go_usage_checked_at = 0
        self.response = httpx.Response(429, headers={"Retry-After": "3600"})
        await self.monitor.refresh_go_usage(self.st, manual=True)
        self.assertEqual(self.st.go_usage["monthly"]["percent"], 9)
        self.assertEqual(self.st.go_usage_status, "rate_limited")
        self.assertTrue(self.st.go_usage_view()["stale"])
        self.assertGreater(self.st.go_usage_retry_at, time.time() + 3595)
        await self.monitor.refresh_go_usage(self.st, manual=True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.st.rate_limit_until, 0)
        self.assertEqual(self.st.failures, 0)

    async def test_manual_route_and_disabled_station(self):
        response = await self.client.post("/admin/station/Go/go-usage/refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["windows"]["weekly"]["percent"], 16)
        snapshot = (await self.client.get("/admin/status")).json()
        self.assertEqual(snapshot["stations"][0]["go_usage"]["windows"]["monthly"]["percent"], 9)
        self.st.enabled = False
        self.assertEqual((await self.client.post("/admin/station/Go/go-usage/refresh")).status_code, 400)
        self.assertEqual(len(self.requests), 1)

    async def test_cache_survives_sqlite_restart_but_not_new_credentials(self):
        await self.monitor.refresh_go_usage(self.st)
        from app.stats import EventLog
        log = EventLog(self.tmp.name)
        log.load()
        new = Registry(self.runtime.gateway, [self.st.cfg])
        new.bind_statistics(log)
        self.assertEqual(new.get("Go").go_usage["rolling"]["percent"], 40)
        new.update_station("Go", {"api_key": "other-key"})
        self.assertEqual(new.get("Go").go_usage, {})
        self.assertEqual(new.get("Go").go_usage_status, "pending")
        await new.close_clients()
        other = StationState(dataclasses.replace(self.st.cfg, api_key="other-account"))
        other.restore_durable(self.st.durable_view())
        self.assertEqual(other.go_usage, {})

    async def test_connection_change_during_fetch_drops_old_snapshot(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(request):
            started.set(); await release.wait()
            return httpx.Response(200, json=PAYLOAD)
        await self.st.client.aclose()
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(delayed))
        task = asyncio.create_task(self.monitor.refresh_go_usage(self.st))
        await started.wait()
        self.st.cfg = dataclasses.replace(self.st.cfg, api_key="changed")
        release.set(); await task
        self.assertEqual(self.st.go_usage, {})

    async def test_invalid_or_redirect_responses_never_replace_good_snapshot(self):
        await self.monitor.refresh_go_usage(self.st)
        for response in [httpx.Response(200, json={"usage": {"rolling": {"percent": True}}}),
                         httpx.Response(302, headers={"Location": "https://untrusted.invalid"}),
                         httpx.Response(200, content=b"x" * 65537)]:
            self.st.go_usage_checked_at = 0; self.response = response
            await self.monitor.refresh_go_usage(self.st, manual=True)
            self.assertEqual(self.st.go_usage["rolling"]["percent"], 40)
            self.assertEqual(self.st.go_usage_status, "error")
        self.assertEqual(len(self.requests), 4)

    def test_missing_windows_and_zero_are_not_fabricated(self):
        data = normalize_usage({"usage": {"rolling": {"percent": 0, "status": "ok"}}})
        self.assertEqual(data["rolling"]["percent"], 0)
        self.assertIsNone(data["monthly"]["percent"])
        with self.assertRaises(ValueError): normalize_usage({"usage": {"rolling": {"percent": float("nan")}}})
