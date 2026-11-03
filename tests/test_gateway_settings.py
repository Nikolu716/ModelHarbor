"""Gateway retry settings API and persistence contract tests."""
from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import GatewayConfig, StationConfig
from app.gateway_settings import (
    GatewaySettingsStore,
    probe_settings_view,
    retry_settings_view,
    validate_scoring_settings,
)
from app.service_settings import ServiceSettingsStore
from app.state import Registry
from app.stats import EventLog
from app.web.admin import create_admin_router


class RecordingMonitor:
    async def probe(self, station) -> None:
        return None

    async def probe_quota_recovery(self, station, *, manual=False) -> bool:
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
        self.service_store = ServiceSettingsStore(self.tempdir.name)
        self.restart_requests = []
        self.eventlog = EventLog(self.tempdir.name, maxlen=200)
        app = FastAPI()
        app.include_router(create_admin_router(
            registry=self.registry,
            gateway=self.gateway,
            monitor=RecordingMonitor(),
            eventlog=self.eventlog,
            settings_store=self.store,
            service_store=self.service_store,
            restart_service=lambda: self.restart_requests.append(True) is None,
        ))
        self.app = app
        self.client = TestClient(app, client=("127.0.0.1", 12345))

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
        self.store.save_probe({
            "generation_probe_enabled": False,
            "generation_probe_interval": 900,
        })
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
        self.assertEqual(set(payload), {"retry", "probe"})
        self.assertEqual(payload["probe"], probe_settings_view(self.gateway))

    def test_scoring_weight_is_live_persisted_and_preserves_evidence(self):
        self.store.save_probe({"generation_probe_enabled": False, "generation_probe_interval": 900})
        self.store.save_logs(700)
        self.assertEqual(self.client.get('/admin/settings/scoring').json(), {'scoring_speed_weight': .15})
        st = self.registry.get('alpha')
        for _ in range(30):
            st.record_success(30, model='model-a', path='/responses', stream=True)
        original_score = st.health_score
        evidence = list(st.health_samples)
        counters = (st.calls, st.failures)
        response = self.client.put('/admin/settings/scoring', json={'scoring_speed_weight': 0})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['persisted'])
        self.assertEqual(self.gateway.scoring_speed_weight, 0)
        self.assertGreater(st.health_score, original_score)
        self.assertEqual(st.health_score, st.score_breakdown['reliability_score'])
        self.assertEqual(st.routing_evidence('model-a', '/responses', True)['speed_weight'], 0)
        self.assertEqual(list(st.health_samples), evidence)
        self.assertEqual((st.calls, st.failures), counters)
        self.assertEqual(self.store.load_scoring(), {'scoring_speed_weight': 0})
        self.assertEqual(self.store.load_logs(), 700)
        self.assertEqual(self.store.load_probe(self.gateway), probe_settings_view(self.gateway))
        self.store.save(retry_settings_view(self.gateway))
        self.assertEqual(self.store.load_scoring(), {'scoring_speed_weight': 0})
        new = self.registry.add_station(StationConfig('new', 'https://new.invalid', 'fixture'))
        new.record_success(60)
        self.assertEqual(new.health_score, new.score_breakdown['reliability_score'])
        self.assertEqual(self.registry.snapshot()['gateway']['scoring_speed_weight'], 0)

    def test_invalid_scoring_weight_does_not_change_runtime_or_disk(self):
        for body in [{}, {'unknown': .2}, {'scoring_speed_weight': True},
                     {'scoring_speed_weight': '0.2'}, {'scoring_speed_weight': -.1},
                     {'scoring_speed_weight': 1.01}, {'scoring_speed_weight': None},
                     {'scoring_speed_weight': .2, 'admin_key': 'forbidden'}]:
            with self.subTest(body=body):
                response = self.client.put('/admin/settings/scoring', json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.gateway.scoring_speed_weight, .15)
                self.assertFalse(self.store.path.exists())
        for value in [float('nan'), float('inf'), -float('inf')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_scoring_settings({'scoring_speed_weight': value})

    def test_scoring_save_failure_preserves_runtime_and_invalid_saved_values_fall_back(self):
        st = self.registry.get('alpha')
        st.record_success(30)
        before = st.health_score
        with patch.object(self.store, 'save_scoring', side_effect=OSError('mock disk failure')):
            response = self.client.put('/admin/settings/scoring', json={'scoring_speed_weight': .4})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.gateway.scoring_speed_weight, .15)
        self.assertEqual(st.health_score, before)
        self.store.path.write_text('{"scoring":{"scoring_speed_weight":2}}', encoding='utf-8')
        self.assertIsNone(self.store.load_scoring())
        self.assertEqual(GatewayConfig.from_dict({'scoring_speed_weight': 1}).scoring_speed_weight, 1)
        with self.assertRaises(ValueError):
            GatewayConfig.from_dict({'scoring_speed_weight': True})

    def test_probe_settings_are_live_persisted_and_loopback_endpoint_uses_public_url(self) -> None:
        before = self.client.get("/admin/settings/probe")
        self.assertEqual(before.status_code, 200)
        self.assertFalse(before.json()["generation_probe_enabled"])

        response = self.client.put("/admin/settings/probe", json={
            "generation_probe_enabled": True,
            "generation_probe_interval": 1800,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.gateway.generation_probe_enabled)
        self.assertEqual(self.store.load_probe(GatewayConfig()), {
            "generation_probe_enabled": True,
            "generation_probe_interval": 1800.0,
        })

        # Auto-discovery intentionally accepts RFC1918 LAN addresses, not TEST-NET.
        with patch("app.service_settings.deployment_addresses", return_value={"127.0.0.1", "192.168.254.10", "198.18.0.1"}):
            endpoint = self.client.get(
                "/admin/settings/endpoint", headers={"host": "127.0.0.1:8000"})
        self.assertEqual(endpoint.status_code, 200)
        self.assertEqual(endpoint.json()["endpoint"], "http://192.168.254.10:8000/v1")

    def test_explicit_public_url_port_is_preserved(self) -> None:
        self.gateway.public_url = "https://gateway.example:9443"
        response = self.client.get("/admin/settings/endpoint")

        self.assertEqual(response.json()["endpoint"], "https://gateway.example:9443/v1")

    def test_endpoint_explicit_proxy_origin_and_path_are_exact(self):
        for root, expected in [("https://gateway.example", "https://gateway.example/v1"),
                               ("https://gateway.example/api/v1/", "https://gateway.example/api/v1"),
                               ("http://192.0.2.40:8765", "http://192.0.2.40:8765/v1"),
                               ("http://[fd00::1]:8765/v1", "http://[fd00::1]:8765/v1")]:
            response = self.client.put("/admin/settings/endpoint", json={"public_url": root})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["endpoint"], expected)
            self.service_store.save_port(9001)
            self.assertEqual(self.client.get("/admin/settings/endpoint").json()["endpoint"], expected)
            self.assertEqual(self.service_store.load_port(8000), 9001)

    def test_endpoint_auto_uses_current_origin_not_saved_next_port(self):
        self.service_store.save_port(9001)
        for host, expected in [("192.0.2.40:8000", "http://192.0.2.40:8000/v1"),
                               ("gateway.example", "http://gateway.example/v1")]:
            response = self.client.get("/admin/settings/endpoint", headers={"host": host})
            self.assertEqual(response.json()["endpoint"], expected)

    def test_endpoint_ambiguous_local_addresses_require_configuration(self):
        for addresses in [{"127.0.0.1", "198.18.0.1"}, {"10.1.1.1", "192.168.1.1"}]:
            with patch("app.service_settings.deployment_addresses", return_value=addresses):
                result = self.client.get("/admin/settings/endpoint", headers={"host": "localhost:8000"}).json()
                self.assertEqual(result["endpoint"], "")
                self.assertEqual(result["source"], "needs_configuration")

    def test_endpoint_validation_and_remote_write_protection(self):
        for value in ["http://127.0.0.1:8000", "http://[::1]:8000", "http://0.0.0.0", "javascript:alert(1)",
                      "https://user:secret@example.test", "https://example.test?key=secret", "https://example.test:99999", "https://exa mple.test", 1]:
            self.assertEqual(self.client.put("/admin/settings/endpoint", json={"public_url": value}).status_code, 400)
        with TestClient(self.app, client=("192.0.2.123", 1234)) as remote:
            response = remote.put("/admin/settings/endpoint", json={"public_url": "https://example.test"},
                                  headers={"x-forwarded-for": "127.0.0.1"})
            self.assertEqual(response.status_code, 403)

    def test_endpoint_clear_overrides_yaml_and_preserves_port(self):
        self.gateway.public_url = "https://old.example"
        self.service_store.save_port(9001)
        result = self.client.put("/admin/settings/endpoint", json={"public_url": ""},
                                 headers={"host": "192.0.2.40:8000"}).json()
        self.assertEqual(result["endpoint"], "http://192.0.2.40:8000/v1")
        restored = ServiceSettingsStore(self.tempdir.name)
        self.assertEqual(restored.load_public_url("https://old.example"), "")
        self.assertEqual(restored.load_port(8000), 9001)

    def test_ip_usage_is_retained_filtered_and_distinguishes_missing_from_zero(self):
        self.eventlog.set_limit(3)
        self.eventlog.add("alpha", "gone", True, 1, tokens=99999, client_ip="192.0.2.99")
        self.eventlog.add("alpha", "model-a", True, 1, tokens=123, client_ip="192.0.2.1")
        self.eventlog.add("alpha", "model-a", False, 1, tokens=None, client_ip="192.0.2.1")
        self.eventlog.add("alpha", "model-b", True, 1, tokens=0, client_ip=None)
        result = self.eventlog.log_analytics()
        self.assertEqual((result["calls"], result["tokens"], result["usage_reports"], result["ip_reports"]), (3, 123, 2, 2))
        self.assertEqual(len(result["ip_models"]), 2)
        filtered = self.eventlog.log_analytics(ip="192.0.2.1", model="model-a")
        self.assertEqual((filtered["calls"], filtered["tokens"], filtered["usage_reports"]), (2, 123, 1))
        self.assertEqual(filtered["ip_models"][0]["failures"], 1)
        self.assertEqual(sum(r["tokens"] for r in result["breakdown"]), 123)
        self.assertEqual(sum(r["tokens"] for r in result["daily"]), 123)

    def test_service_port_persists_for_next_start_without_changing_active_port(self) -> None:
        before = self.client.get("/admin/settings/service")
        self.assertEqual(before.json(), {"port": 8000, "active_port": 8000})

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind((self.gateway.host, 0))
            port = candidate.getsockname()[1]
        response = self.client.put("/admin/settings/service", json={"port": port})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "port": port, "active_port": 8000,
            "restart_required": True, "persisted": True,
        })
        self.assertEqual(self.gateway.port, 8000)
        self.assertEqual(self.service_store.load_port(8000), port)

    def test_service_port_rejects_a_port_that_is_already_bound(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind((self.gateway.host, 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            response = self.client.put("/admin/settings/service", json={"port": port})

        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.service_store.path.exists())

    def test_service_controls_reject_remote_clients_and_forwarded_spoof(self) -> None:
        remote = TestClient(self.app, client=("198.51.100.92", 54321))
        try:
            self.gateway.allowed_ips = ["198.51.100.92"]
            remote.headers.update({'X-Forwarded-For': '127.0.0.1', 'X-Real-IP': '127.0.0.1'})
            self.assertEqual(remote.get('/admin/capabilities').json(), {'can_manage_service': False})
            get_response = remote.get("/admin/settings/service")
            update_response = remote.put("/admin/settings/service", json={"port": 9123})
            restart_response = remote.post("/admin/service/restart")
        finally:
            remote.close()

        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(update_response.status_code, 403)
        self.assertEqual(restart_response.status_code, 403)
        self.assertFalse(self.service_store.path.exists())
        self.assertEqual(self.restart_requests, [])

    def test_service_controls_allow_the_deployment_machine_network_address(self) -> None:
        with patch('app.service_settings.deployment_addresses', return_value=frozenset({'127.0.0.1', '192.0.2.40'})):
            with TestClient(self.app, client=('192.0.2.40', 12345)) as local:
                self.assertTrue(local.get('/admin/capabilities').json()['can_manage_service'])
                self.assertEqual(local.get('/admin/settings/service').status_code, 200)
                self.assertEqual(local.put('/admin/settings/service', json={'port': 8000}).status_code, 200)
                self.assertEqual(local.post('/admin/service/restart').status_code, 202)

    def test_invalid_service_ports_are_rejected_without_persisting(self) -> None:
        for value in (0, 65536, True, 8000.5, "8000"):
            with self.subTest(value=value):
                response = self.client.put("/admin/settings/service", json={"port": value})
                self.assertEqual(response.status_code, 400)
        self.assertFalse(self.service_store.path.exists())

    def test_service_restart_is_scheduled_by_the_process_supervisor(self) -> None:
        response = self.client.post("/admin/service/restart")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"scheduled": True})
        self.assertEqual(self.restart_requests, [True])

    def test_invalid_probe_settings_do_not_change_runtime_or_disk(self) -> None:
        response = self.client.put("/admin/settings/probe", json={
            "generation_probe_enabled": True,
            "generation_probe_interval": 60,
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.gateway.generation_probe_enabled)
        self.assertFalse(self.store.path.exists())

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

    def test_log_retention_is_persisted_live_and_applies_to_analytics_scope(self) -> None:
        for _ in range(150):
            self.eventlog.add("alpha", "model-a", True, 1.0, client_ip="192.0.2.10")

        before = self.client.get("/admin/settings/logs")
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json()["event_retention"], 200)

        response = self.client.put("/admin/settings/logs", json={"event_retention": 100})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["persisted"])
        self.assertEqual(self.eventlog.maxlen, 100)
        self.assertEqual(len(self.eventlog.events), 100)
        self.assertEqual(self.store.load_logs(), 100)
        stats = self.eventlog.log_analytics(group_by="ip")
        self.assertEqual(stats["retained_events"], 100)
        self.assertEqual(stats["max_events"], 100)

        restored = EventLog(self.tempdir.name, maxlen=self.store.load_logs())
        restored.load()
        self.assertEqual(len(restored.events), 100)
        self.assertEqual(restored.events[0]["client_ip"], "192.0.2.10")

    def test_log_retention_rejects_out_of_range_values(self) -> None:
        for value in (True, 99, 10_001):
            response = self.client.put(
                "/admin/settings/logs", json={"event_retention": value})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.eventlog.maxlen, 200)
        self.assertFalse(self.store.path.exists())


if __name__ == "__main__":
    unittest.main()
