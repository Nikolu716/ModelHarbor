"""Dynamic station-management API contract tests."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import GatewayConfig, StationConfig
from app.state import Registry
from app.stations import StationStore
from app.stats import EventLog
from app.web.admin import create_admin_router


class RecordingMonitor:
    def __init__(self) -> None:
        self.probes: list[str] = []

    async def probe(self, station) -> None:
        self.probes.append(station.cfg.name)

    async def probe_quota_recovery(self, station) -> bool:
        self.probes.append(station.cfg.name)
        station.reset_quota()
        return True


class StationManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(data_dir=self.tempdir.name)
        self.alpha = StationConfig(
            name="alpha",
            base_url="https://alpha.invalid/v1",
            api_key="secret-alpha",
            models=["configured-model"],
            priority=1,
            probe_models=True,
            probe_chat=True,
        )
        self.registry = Registry(self.gateway, [self.alpha])
        self.store = StationStore(self.tempdir.name)
        self.monitor = RecordingMonitor()
        app = FastAPI()
        app.include_router(create_admin_router(
            registry=self.registry,
            gateway=self.gateway,
            monitor=self.monitor,
            eventlog=EventLog(self.tempdir.name),
            store=self.store,
        ))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        asyncio.run(self.registry.close_clients())
        self.tempdir.cleanup()

    @staticmethod
    def station_body(name: str = "beta") -> dict:
        return {
            "name": name,
            "base_url": "https://beta.invalid/v1/",
            "api_key": "secret-beta",
            "models": ["model-b", "model-b"],
            "priority": 2,
            "billing": "subscription",
            "probe_models": True,
            "probe_chat": False,
            "enabled": True,
        }

    def test_list_returns_editable_config_without_secrets(self) -> None:
        self.registry.get("alpha").probed_models.add("detected-model")

        response = self.client.get("/admin/stations")

        self.assertEqual(response.status_code, 200)
        station = response.json()[0]
        self.assertEqual(station["models"], ["configured-model"])
        self.assertEqual(station["detected_models"], ["detected-model"])
        self.assertTrue(station["probe_models"])
        self.assertTrue(station["probe_chat"])
        self.assertTrue(station["has_api_key"])
        self.assertNotIn("api_key", station)
        self.assertNotIn("secret-alpha", response.text)

    def test_add_is_immediate_persisted_and_scheduled_for_probe(self) -> None:
        response = self.client.post("/admin/stations", json=self.station_body())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {
            "name": "beta",
            "ok": True,
            "persisted": True,
            "probe_scheduled": True,
        })
        self.assertEqual(self.monitor.probes, [])
        self.assertIsNotNone(self.registry.get("beta"))
        self.assertEqual(self.registry.get("beta").cfg.models, ["model-b"])
        self.assertEqual(
            [station.name for station in self.store.load()],
            ["alpha", "beta"],
        )

        duplicate = self.client.post(
            "/admin/stations", json=self.station_body("BETA"))
        self.assertEqual(duplicate.status_code, 409)

    def test_update_preserves_key_and_syncs_runtime_enabled_state(self) -> None:
        self.client.post("/admin/stations", json=self.station_body())

        response = self.client.put("/admin/stations/beta", json={
            "base_url": "https://new-beta.invalid",
            "enabled": False,
            "probe_chat": True,
        })

        self.assertEqual(response.status_code, 200)
        station = self.registry.get("beta")
        self.assertFalse(station.enabled)
        self.assertFalse(station.cfg.enabled)
        self.assertEqual(station.cfg.api_key, "secret-beta")
        self.assertEqual(station.cfg.base_url, "https://new-beta.invalid")
        self.assertTrue(station.cfg.probe_chat)
        self.assertEqual(self.monitor.probes, [])
        stored = {item.name: item for item in self.store.load()}
        self.assertFalse(stored["beta"].enabled)
        self.assertEqual(stored["beta"].api_key, "secret-beta")

    def test_invalid_updates_do_not_change_runtime_configuration(self) -> None:
        before = self.registry.get("alpha").cfg
        cases = [
            {"base_url": ""},
            {"models": "not-a-list"},
            {"max_concurrency": 0},
            {"timeout": -1},
            {"enabled": "false"},
            {"unexpected": True},
            {"base_url": "https://user:pass@example.invalid"},
            {"base_url": "https://alpha.invalid/v1?token=leak"},
            {"api_key": "bad\nkey"},
        ]

        for body in cases:
            with self.subTest(body=body):
                response = self.client.put("/admin/stations/alpha", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.registry.get("alpha").cfg, before)

    def test_update_transport_fields_reject_active_requests(self) -> None:
        self.client.post("/admin/stations", json=self.station_body())
        route_id = self.registry.begin_route("beta", "model-b")
        try:
            for body in (
                {"base_url": "https://changed.invalid"},
                {"max_concurrency": 4},
                {"api_key": "new-secret"},
            ):
                with self.subTest(body=body):
                    response = self.client.put("/admin/stations/beta", json=body)
                    self.assertEqual(response.status_code, 409)
            priority = self.client.put("/admin/stations/beta", json={"priority": 4})
            self.assertEqual(priority.status_code, 200)
        finally:
            self.registry.end_route(route_id, "success")

    def test_store_load_rejects_malformed_or_empty_files(self) -> None:
        malformed = [
            [],
            {},
            {"stations": []},
            {"stations": [None]},
            {"stations": [{
                "name": "alpha", "base_url": "https://a.invalid",
                "api_key": "a", "max_concurrency": 0,
            }]},
            {"stations": [
                {"name": "alpha", "base_url": "https://a.invalid", "api_key": "a"},
                {"name": "ALPHA", "base_url": "https://b.invalid", "api_key": "b"},
            ]},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                self.store.path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(self.store.load())

    def test_runtime_add_failure_rolls_back_persisted_file(self) -> None:
        original = self.registry.ensure_client

        def fail_client(_station):
            raise RuntimeError("client init failed")

        self.registry.ensure_client = fail_client
        try:
            response = self.client.post("/admin/stations", json=self.station_body())
        finally:
            self.registry.ensure_client = original
        self.assertEqual(response.status_code, 500)
        self.assertEqual([item.name for item in self.store.load()], ["alpha"])
        self.assertIsNone(self.registry.get("beta"))

    def test_toggle_is_persisted(self) -> None:
        response = self.client.post("/admin/station/alpha/toggle")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.registry.get("alpha").enabled)
        self.assertFalse(self.registry.get("alpha").cfg.enabled)
        self.assertFalse(self.store.load()[0].enabled)

    def test_delete_persists_but_rejects_active_or_last_station(self) -> None:
        self.client.post("/admin/stations", json=self.station_body())
        route_id = self.registry.begin_route("beta", "model-b")
        active = self.client.delete("/admin/stations/beta")
        self.assertEqual(active.status_code, 409)
        self.registry.end_route(route_id, "success")

        deleted = self.client.delete("/admin/stations/beta")
        self.assertEqual(deleted.status_code, 200)
        self.assertIsNone(self.registry.get("beta"))
        self.assertEqual([item.name for item in self.store.load()], ["alpha"])

        last = self.client.delete("/admin/stations/alpha")
        self.assertEqual(last.status_code, 409)
        self.assertIsNotNone(self.registry.get("alpha"))

    def test_persistence_failure_never_reports_success_or_mutates_runtime(self) -> None:
        def fail_save(_stations) -> None:
            raise OSError("disk full")

        self.store.save = fail_save

        added = self.client.post("/admin/stations", json=self.station_body())
        self.assertEqual(added.status_code, 500)
        self.assertIsNone(self.registry.get("beta"))

        updated = self.client.put("/admin/stations/alpha", json={"enabled": False})
        self.assertEqual(updated.status_code, 500)
        self.assertTrue(self.registry.get("alpha").enabled)
        self.assertTrue(self.registry.get("alpha").cfg.enabled)


if __name__ == "__main__":
    unittest.main()
