"""Automatic quota recovery probing."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from app.config import GatewayConfig, StationConfig
from app.errors import UpstreamError
from app.health import HealthMonitor
from app.state import Registry, STATUS_DOWN
from app.stats import EventLog


def make_station(name: str = "alpha", **kwargs) -> StationConfig:
    values = {
        "name": name,
        "base_url": "https://alpha.invalid/v1",
        "api_key": "secret",
        "models": ["gpt-test"],
    }
    values.update(kwargs)
    return StationConfig(**values)


class QuotaRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = GatewayConfig(
            generation_probe_enabled=True,
            generation_probe_interval=300,
            quota_auto_recover=True,
            quota_probe_interval=60,
            quota_probe_max_interval=300,
            cool_down=60,
        )
        self.registry = Registry(self.gateway, [make_station(
            probe_chat=True, probe_chat_model="gpt-test")])
        self.monitor = HealthMonitor(self.registry, self.gateway)
        self.station = self.registry.get("alpha")
        self.quota_error = UpstreamError(
            "alpha", 402, "Insufficient quota", quota=True)

    def test_models_success_does_not_clear_quota(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="request")
        self.station.last_probe_ok = True
        self.station.last_probe_error = ""
        self.assertTrue(self.station.quota_exhausted)
        self.assertFalse(self.registry.is_routeable(self.station))

    def test_generation_ok_clears_quota(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="request")

        async def fake_generation(_st):
            return "ok", 0.2, None

        self.monitor._generation_probe = fake_generation
        recovered = asyncio.run(self.monitor.probe_quota_recovery(self.station))
        self.assertTrue(recovered)
        self.assertFalse(self.station.quota_exhausted)
        self.assertTrue(self.registry.is_routeable(self.station))

    def test_generation_quota_keeps_flag_and_backs_off(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="request")

        async def fake_generation(_st):
            return "quota", 0.1, self.quota_error

        self.monitor._generation_probe = fake_generation
        recovered = asyncio.run(self.monitor.probe_quota_recovery(self.station))
        self.assertFalse(recovered)
        self.assertTrue(self.station.quota_exhausted)
        self.assertEqual(self.station.quota_probe_backoff, 900)

        recovered = asyncio.run(self.monitor.probe_quota_recovery(self.station))
        self.assertFalse(recovered)
        self.assertEqual(self.station.quota_probe_backoff, 900)

        recovered = asyncio.run(self.monitor.probe_quota_recovery(self.station))
        self.assertEqual(self.station.quota_probe_backoff, 900)

    def test_models_source_does_not_treat_catalog_as_generation_or_quota_proof(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="models")
        def fresh_success(st):
            st.last_probe_ok = True
            st.last_probe_at += 1
        self.monitor.probe = AsyncMock(side_effect=fresh_success)
        recovered = asyncio.run(self.monitor.probe_quota_recovery(self.station))
        self.assertFalse(recovered)
        self.assertTrue(self.station.quota_exhausted)

    def test_manual_models_recovery_sends_one_explicit_generation_check(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="models")
        self.monitor._generation_probe = AsyncMock(return_value=("ok", 0.1, None))
        recovered = asyncio.run(self.monitor.probe_quota_recovery(self.station, manual=True))
        self.assertTrue(recovered)
        self.assertFalse(self.station.quota_exhausted)
        self.monitor._generation_probe.assert_awaited_once_with(self.station)

    def test_recovery_is_due_after_interval_even_when_cooling(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="request")
        self.station.status = STATUS_DOWN
        self.station.down_since = self.station.quota_at
        self.station.quota_probe_at = self.station.quota_at
        now = self.station.quota_at + 901
        self.assertTrue(self.monitor._quota_recovery_due(self.station, now))

    def test_first_mark_waits_one_interval(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="request")
        self.assertFalse(self.monitor._quota_recovery_due(
            self.station, self.station.quota_at + 10))
        self.assertFalse(self.monitor._quota_recovery_due(
            self.station, self.station.quota_at + 60))
        self.assertTrue(self.monitor._quota_recovery_due(
            self.station, self.station.quota_at + 900))

    def test_disabled_global_generation_probe_never_runs_in_background(self) -> None:
        self.gateway.generation_probe_enabled = False
        self.station.mark_quota_exhausted(self.quota_error, source="request")
        self.assertFalse(self.monitor._quota_recovery_due(
            self.station, self.station.quota_at + 3600))
        self.assertFalse(asyncio.run(self.monitor.probe_quota_recovery(self.station)))

    def test_rate_limited_generation_cannot_confirm_quota_recovery(self):
        self.station.mark_quota_exhausted(self.quota_error, source="request")
        self.monitor._generation_probe = AsyncMock(return_value=(
            "client_error", 0.1, UpstreamError("alpha", 429, "slow down")))
        self.assertFalse(asyncio.run(self.monitor.probe_quota_recovery(self.station)))
        self.assertTrue(self.station.quota_exhausted)

    def test_due_generation_probe_runs_separately_from_other_checks(self) -> None:
        self.gateway.generation_probe_interval = 300
        self.station.cfg.probe_models = True
        self.station.cfg.probe_path = "/health"
        self.station.last_used_at = 0
        self.monitor.discover_models = AsyncMock()
        self.monitor.probe = AsyncMock()
        self.monitor._probe_chat = AsyncMock()
        self.monitor._probe_balance = AsyncMock()
        self.monitor._notify_changes = AsyncMock()

        asyncio.run(self.monitor._probe_all())

        self.monitor._probe_chat.assert_awaited_once_with(self.station)
        self.monitor.discover_models.assert_not_awaited()
        self.monitor.probe.assert_not_awaited()
        self.monitor._probe_balance.assert_not_awaited()


class QuotaHandleFailureTests(unittest.TestCase):
    def test_request_quota_marks_request_source(self) -> None:
        from app.proxy import OpenAIProxy

        gateway = GatewayConfig()
        registry = Registry(gateway, [make_station()])
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            station = registry.get("alpha")
            err = UpstreamError("alpha", 402, "Insufficient quota", quota=True)
            proxy._handle_failure(station, err)
            self.assertTrue(station.quota_exhausted)
            self.assertEqual(station.quota_source, "request")

    def test_business_5xx_reaches_breaker_and_updates_score(self) -> None:
        from app.proxy import OpenAIProxy

        gateway = GatewayConfig(fail_threshold=2)
        registry = Registry(gateway, [make_station()])
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            station = registry.get("alpha")
            error = UpstreamError("alpha", 503, "upstream unavailable")

            proxy._record_request_failure(station, error)
            self.assertEqual(station.consecutive_failures, 1)
            self.assertEqual(station.score_breakdown["failure_streak_penalty"], 5)
            proxy._record_request_failure(station, error)

            self.assertEqual(station.status, "down")
            self.assertEqual(station.score_breakdown["breaker_penalty"], 25)

    def test_rate_limit_uses_retry_after_without_opening_breaker(self) -> None:
        from app.proxy import OpenAIProxy

        gateway = GatewayConfig(fail_threshold=1)
        registry = Registry(gateway, [make_station()])
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            station = registry.get("alpha")
            error = UpstreamError("alpha", 429, "rate limited")

            proxy._handle_failure(station, error, "120")

            self.assertEqual(station.consecutive_failures, 0)
            self.assertEqual(station.status, "up")
            self.assertGreater(station.rate_limit_until, time.time() + 119)
            self.assertEqual(station.score_breakdown["rate_limit_penalty"], 35)
            protection = registry.snapshot()['stations'][0]['protection']
            self.assertEqual(protection['kind'], 'rate_limited')
            self.assertEqual(protection['source'], 'request')
            self.assertEqual(protection['wait_source'], 'upstream')
            self.assertGreater(protection['remaining_seconds'], 118)
            self.assertIn('429', protection['reason'])

    def test_breaker_wait_and_recovery_state_match_routing(self):
        from app.proxy import OpenAIProxy

        gateway = GatewayConfig(fail_threshold=3, cool_down=60)
        registry = Registry(gateway, [make_station()])
        with tempfile.TemporaryDirectory() as directory:
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))
            station = registry.get('alpha')
            error = UpstreamError('alpha', 503, 'model unavailable')
            for _ in range(3):
                proxy._handle_failure(station, error)
            protection = registry.snapshot()['stations'][0]['protection']
            self.assertEqual(protection['kind'], 'breaker')
            self.assertEqual(protection['failure_count'], 3)
            self.assertEqual(protection['status_code'], 503)
            self.assertIn('model unavailable', protection['reason'])
            self.assertGreater(protection['remaining_seconds'], 58)
            self.assertFalse(registry.is_routeable(station))
            station.down_since -= 61
            protection = registry.snapshot()['stations'][0]['protection']
            self.assertEqual(protection['kind'], 'recovering')
            self.assertEqual(protection['remaining_seconds'], 0)
            self.assertTrue(registry.is_routeable(station))
            station.record_success(0.2)
            self.assertEqual(registry.snapshot()['stations'][0]['protection']['kind'], 'normal')

    def test_invalid_retry_after_keeps_finite_gateway_cooldown(self):
        from app.proxy import OpenAIProxy

        for header in ('inf', 'NaN', '-1', 'not-a-date'):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                gateway = GatewayConfig(rate_limit_cooldown=30)
                registry = Registry(gateway, [make_station()])
                proxy = OpenAIProxy(registry, gateway, EventLog(directory))
                station = registry.get('alpha')
                proxy._handle_failure(station, UpstreamError('alpha', 429, 'slow down'), header)
                protection = registry.snapshot()['stations'][0]['protection']
                self.assertEqual(protection['wait_source'], 'gateway')
                self.assertGreater(protection['remaining_seconds'], 28)
                self.assertLessEqual(protection['remaining_seconds'], 30)


if __name__ == "__main__":
    unittest.main()
