"""Automatic quota recovery probing."""
from __future__ import annotations

import asyncio
import tempfile
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
            quota_auto_recover=True,
            quota_probe_interval=60,
            quota_probe_max_interval=300,
            cool_down=60,
        )
        self.registry = Registry(self.gateway, [make_station()])
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
        self.assertEqual(self.station.quota_probe_backoff, 120)

        recovered = asyncio.run(self.monitor.probe_quota_recovery(self.station))
        self.assertFalse(recovered)
        self.assertEqual(self.station.quota_probe_backoff, 240)

        recovered = asyncio.run(self.monitor.probe_quota_recovery(self.station))
        self.assertEqual(self.station.quota_probe_backoff, 300)

    def test_models_source_recovers_when_models_ok(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="models")
        self.monitor.probe = AsyncMock(side_effect=lambda st: setattr(st, "last_probe_ok", True))
        recovered = asyncio.run(self.monitor.probe_quota_recovery(self.station))
        self.assertTrue(recovered)
        self.assertFalse(self.station.quota_exhausted)

    def test_recovery_is_due_after_interval_even_when_cooling(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="request")
        self.station.status = STATUS_DOWN
        self.station.down_since = self.station.quota_at
        self.station.quota_probe_at = self.station.quota_at
        now = self.station.quota_at + 61
        self.assertTrue(self.monitor._quota_recovery_due(self.station, now))

    def test_first_mark_waits_one_interval(self) -> None:
        self.station.mark_quota_exhausted(self.quota_error, source="request")
        self.assertFalse(self.monitor._quota_recovery_due(
            self.station, self.station.quota_at + 10))
        self.assertTrue(self.monitor._quota_recovery_due(
            self.station, self.station.quota_at + 60))


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


if __name__ == "__main__":
    unittest.main()
