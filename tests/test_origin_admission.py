"""Regression tests for shared upstream admission across stations."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest

from app.config import GatewayConfig, StationConfig
from app.errors import UpstreamError
from app.router import Router
from app.state import Registry, STATUS_DOWN


class OriginAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(
            data_dir=self.temp.name,
            origin_max_concurrency=2,
            cool_down=0.01,
        )
        self.registry = Registry(self.gateway, [
            StationConfig("key-a", "https://provider.example/v1", "a", models=["m"], max_concurrency=4),
            StationConfig("key-b", "https://provider.example/v1", "b", models=["m"], max_concurrency=4),
        ])
        self.router = Router(self.registry, self.gateway)

    async def asyncTearDown(self) -> None:
        await self.registry.close_clients()
        self.temp.cleanup()

    async def test_same_origin_has_one_total_concurrency_budget(self) -> None:
        a = self.registry.get("key-a")
        b = self.registry.get("key-b")
        permits = await asyncio.gather(*(
            self.router.acquire(st) for st in (a, b, a, b)
        ))

        held = [permit for permit in permits if permit is not None]
        self.assertEqual(len(held), 2)
        limiter = self.registry.ensure_origin_limiter(a)
        self.assertEqual(limiter.active, 2)
        self.assertEqual(limiter.semaphore._value, 0)

        for station, permit in zip((a, b, a, b), permits):
            if permit is not None:
                self.router.release(station, permit)
        self.assertEqual(limiter.active, 0)
        self.assertEqual(limiter.semaphore._value, 2)
        self.assertFalse(self.registry.ensure_semaphore(a).locked())
        self.assertFalse(self.registry.ensure_semaphore(b).locked())

    async def test_origin_full_is_local_capacity_and_does_not_count_failure(self) -> None:
        self.gateway.origin_max_concurrency = 1
        a = self.registry.get("key-a")
        b = self.registry.get("key-b")
        permit = await self.router.acquire(a)
        self.assertIsNotNone(permit)
        before = (a.failures, b.failures)
        self.assertEqual(self.router.unavailable_reason(b), "共享上游并发已满")
        self.assertIsNone(await self.router.acquire(b))
        self.assertEqual((a.failures, b.failures), before)
        self.router.release(a, permit)

    async def test_recovery_validation_is_single_flight_per_origin(self) -> None:
        a = self.registry.get("key-a")
        b = self.registry.get("key-b")
        for station in (a, b):
            station.status = STATUS_DOWN
            station.down_since = 0.0
            station.recovery_attempt_at = 0.0
        first = await self.router.acquire(a)
        self.assertIsNotNone(first)
        self.assertTrue(self.registry.ensure_origin_limiter(a).recovery_lock.locked())
        self.assertEqual(
            self.router.unavailable_reason(b),
            "共享上游正在恢复验证",
        )
        self.assertIsNone(await self.router.acquire(b))
        self.router.release(a, first, "failed", "upstream_down")
        self.assertFalse(self.registry.ensure_origin_limiter(a).recovery_lock.locked())

    async def test_transient_failures_trip_one_shared_breaker_and_probe_once(self) -> None:
        a = self.registry.get("key-a")
        b = self.registry.get("key-b")
        error = UpstreamError("key-a", 503, "provider unavailable", retryable=True)
        self.registry.note_origin_failure(a, error)
        self.registry.note_origin_failure(b, error)
        self.registry.note_origin_failure(a, error)
        limiter = self.registry.ensure_origin_limiter(a)
        self.assertTrue(limiter.recovery_required)
        self.assertGreater(limiter.cooldown_until, time.time())
        self.assertEqual(self.router.unavailable_reason(a), "共享上游正在冷却")
        limiter.cooldown_until = time.time() - 1

        first = await self.router.acquire(a)
        self.assertIsNotNone(first)
        self.assertTrue(first.origin_recovery)
        self.assertEqual(
            self.router.unavailable_reason(b),
            "共享上游正在恢复验证",
        )
        self.assertIsNone(await self.router.acquire(b))
        self.router.release(a, first, "success")
        self.assertFalse(limiter.recovery_required)
        self.assertEqual(limiter.failure_streak, 0)


if __name__ == "__main__":
    unittest.main()
