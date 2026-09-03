"""Gateway retry settings API and persistence contract tests."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import GatewayConfig, StationConfig
from app.gateway_settings import GatewaySettingsStore, retry_settings_view
from app.state import Registry
from app.stats import EventLog
from app.web.admin import create_admin_router


class RecordingMonitor:
    async def probe(self, station) -> None:
        return None

    async def probe_quota_recovery(self, station) -> bool:
        station.reset_quota()
        return True


class GatewaySettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(data_dir=self.tempdir.name)
        station = StationConfig(
            name="alpha",
            base_url="https://alpha.invalid/v1",
            api_key="secret-alpha",
            models=["model-a"],
        )
        self.registry = Registry(self.gateway, [station])
        self.store = GatewaySettingsStore(self.tempdir.name)
        app = FastAPI()
        app.include_router(create_admin_router(
            registry=self.registry,
            gateway=self.gateway,
            monitor=RecordingMonitor(),
            eventlog=EventLog(self.tempdir.name),
            settings_store=self.store,
        ))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        asyncio.run(self.registry.close_clients())
        self.tempdir.cleanup()

    def test_get_exposes_only_retry_policy(self) -> None:
        response = self.client.get("/admin/settings/retry")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), retry_settings_view(self.gateway))
        self.assertNotIn("secret-alpha", response.text)
        self.assertNotIn("api_keys", response.text)
        self.assertNotIn("admin_key", response.text)

    def test_partial_update_is_live_and_persisted(self) -> None:
        response = self.client.put("/admin/settings/retry", json={
            "max_rounds": 0,
            "max_retry_seconds": 240,
            "max_round_backoff": 15,
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["persisted"])
        self.assertEqual(self.gateway.max_rounds, 0)
        self.assertEqual(self.gateway.max_retry_seconds, 240.0)
        self.assertEqual(self.gateway.max_round_backoff, 15.0)
        loaded = self.store.load(GatewayConfig(data_dir=self.tempdir.name))
        self.assertEqual(loaded, retry_settings_view(self.gateway))
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"retry"})

    def test_invalid_update_does_not_change_runtime_or_disk(self) -> None:
        before = retry_settings_view(self.gateway)

        response = self.client.put("/admin/settings/retry", json={
            "round_backoff": 20,
            "max_round_backoff": 10,
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(retry_settings_view(self.gateway), before)
        self.assertFalse(self.store.path.exists())

    def test_persistence_failure_does_not_change_runtime(self) -> None:
        before = retry_settings_view(self.gateway)

        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            response = self.client.put(
                "/admin/settings/retry",
                json={"max_attempts": 9},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(retry_settings_view(self.gateway), before)

    def test_unknown_and_busy_loop_values_are_rejected(self) -> None:
        unknown = self.client.put(
            "/admin/settings/retry",
            json={"api_keys": ["leak"]},
        )
        zero_delay = self.client.put(
            "/admin/settings/retry",
            json={"round_backoff": 0},
        )

        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(zero_delay.status_code, 400)

    def test_rounds_and_duration_cannot_both_be_unlimited(self) -> None:
        response = self.client.put("/admin/settings/retry", json={
            "max_rounds": 0,
            "max_retry_seconds": 0,
        })

        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(self.gateway.max_retry_seconds, 0.0)


if __name__ == "__main__":
    unittest.main()
