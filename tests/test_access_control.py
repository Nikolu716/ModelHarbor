"""Access policy validation, persistence and gateway enforcement tests."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from app.access_control import (
    AccessControlStore, is_ip_allowed, validate_allowed_ips,
)
from app.config import AppConfig, GatewayConfig
from app.gateway_settings import GatewaySettingsStore, retry_settings_view
from app.service_settings import ServiceSettingsStore, local_service_url
from gateway import build_app, main


ALLOWED = ["192.0.2.40", "198.51.100.92", "203.0.113.27", "203.0.113.23"]


class AccessPolicyTests(unittest.TestCase):
    def test_local_tools_use_active_policy_without_inheriting_or_mutating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = GatewayConfig(data_dir=directory, host='0.0.0.0', allowed_ips=['192.0.2.40'])
            with patch('app.service_settings.deployment_addresses', return_value={'127.0.0.1', '192.0.2.40', '10.1.1.1'}):
                self.assertEqual(local_service_url(cfg), 'http://192.0.2.40:8000')
                self.assertFalse((Path(directory) / 'accounts.sqlite3').exists())
                account_id = 'a' * 32
                with closing(sqlite3.connect(Path(directory) / 'accounts.sqlite3')) as db, db:
                    db.execute('CREATE TABLE metadata (key TEXT, value TEXT)')
                    db.execute('INSERT INTO metadata VALUES (?, ?)', ('active_account', account_id))
                store = AccessControlStore(str(Path(directory) / 'accounts' / account_id))
                store.save(['10.1.1.1'])
                before = store.path.read_bytes()
                self.assertEqual(local_service_url(cfg), 'http://10.1.1.1:8000')
                self.assertEqual(store.path.read_bytes(), before)
                store.save([])
                self.assertEqual(local_service_url(cfg), 'http://127.0.0.1:8000')
                self.assertEqual(store.load(['192.0.2.40']), [])

    def test_normalizes_and_deduplicates_ip_literals(self):
        self.assertEqual(validate_allowed_ips([
            " 192.0.2.40 ", "::ffff:192.0.2.40",
            "2001:0DB8:0:0:0:0:0:1", "2001:db8::1",
        ]), ["192.0.2.40", "2001:db8::1"])

    def test_rejects_malformed_and_overbroad_configuration(self):
        for values in (None, "192.0.2.40", {}, True, [None], [123], [True],
                       [""], ["example.com"], ["http://192.0.2.40/"],
                       ["192.0.2.40:8000"], ["10.0.0.0/8"], ["0.0.0.0/0"],
                       ["999.0.0.1"], ["fe80::1%eth0"], ["127.0.0.1"] * 257):
            with self.subTest(values=values), self.assertRaises(ValueError):
                GatewayConfig.from_dict({"allowed_ips": values})

    def test_empty_list_denies_every_client_including_localhost(self):
        for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertFalse(is_ip_allowed(host, []))
            self.assertTrue(is_ip_allowed(host, ["127.0.0.1", "::1"]))
        for host in (None, "testclient", "localhost", "192.0.2.40", "127.0.0.2"):
            self.assertFalse(is_ip_allowed(host, []))

    def test_store_preserves_empty_override_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AccessControlStore(directory)
            self.assertEqual(store.load(ALLOWED), ALLOWED)
            store.save([])
            self.assertEqual(store.load(ALLOWED), [])
            for invalid in ("{", "null", "{}", '{"allowed_ips": ["invalid"]}'):
                store.path.write_text(invalid, encoding="utf-8")
                with self.assertRaises(ValueError):
                    store.load(ALLOWED)

    def test_entrypoint_disables_forwarded_header_rewriting(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = AppConfig(gateway=GatewayConfig(data_dir=directory))
            ServiceSettingsStore(directory).save_port(9123)
            app = SimpleNamespace(state=SimpleNamespace())
            server = Mock()
            with patch("sys.argv", ["gateway.py"]), \
                    patch("gateway.load_config", return_value=cfg), \
                    patch("gateway.build_app", return_value=app), \
                    patch("gateway.uvicorn.Config") as config, \
                    patch("gateway.uvicorn.Server", return_value=server):
                main()
            self.assertIs(config.call_args.kwargs["proxy_headers"], False)
            self.assertEqual(config.call_args.kwargs["port"], 9123)
            self.assertEqual(cfg.gateway.port, 9123)
            self.assertIs(app.state.uvicorn_server, server)
            server.run.assert_called_once_with()


class GatewayAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.cfg = AppConfig(gateway=GatewayConfig(
            data_dir=self.tempdir.name, allowed_ips=list(ALLOWED),
            api_keys=["api-secret"], admin_key="admin-secret",
        ))
        self.app = build_app(self.cfg)
        self.store = AccessControlStore(self.tempdir.name)

    async def request(self, host, path="/healthz", *, method="GET", body=None,
                      headers=None, app=None):
        transport = httpx.ASGITransport(
            app=app or self.app, client=(host, 12345) if host is not None else None,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            return await client.request(method, path, json=body, headers=headers)

    async def save(self, host, ips, **kwargs):
        return await self.request(
            host, "/admin/settings/access", method="PUT",
            body={"allowed_ips": ips}, headers={"Authorization": "Bearer admin-secret"},
            **kwargs,
        )

    async def test_explicit_addresses_can_access(self):
        for host in (*ALLOWED, "::ffff:198.51.100.92"):
            with self.subTest(host=host):
                response = await self.request(host)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["ok"])

    async def test_unlisted_and_unknown_clients_are_blocked_on_all_routes(self):
        for host in ("198.51.100.93", "::ffff:198.51.100.93", "2001:db8::1", "127.0.0.1", "::1", None):
            for path in ("/", "/healthz", "/dashboard", "/static/index.html",
                         "/static/dashboard.js", "/favicon.ico", "/docs",
                         "/openapi.json", "/v1/models", "/v1/responses",
                         "/admin/settings/access", "/admin/status", "/missing"):
                with self.subTest(host=host, path=path):
                    response = await self.request(host, path)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["error"]["code"], "ip_not_allowed")
        for method in ("POST", "PUT", "DELETE", "OPTIONS", "HEAD"):
            self.assertEqual((await self.request("10.0.0.99", method=method)).status_code, 403)

    async def test_forwarded_headers_cannot_bypass_or_override_socket_peer(self):
        headers = {
            "X-Forwarded-For": "192.0.2.40, 127.0.0.1",
            "X-Real-IP": "198.51.100.92", "Forwarded": "for=203.0.113.27",
            "Authorization": "Bearer admin-secret",
        }
        self.assertEqual((await self.request("10.0.0.99", headers=headers)).status_code, 403)
        headers["X-Forwarded-For"] = "10.0.0.99"
        self.assertEqual((await self.request(ALLOWED[0], headers=headers)).status_code, 200)

    async def test_api_and_admin_keys_still_required_for_allowed_ips(self):
        for path in ("/v1/models", "/admin/settings/access"):
            self.assertEqual((await self.request(ALLOWED[0], path)).status_code, 401)
        response = await self.request(ALLOWED[0], "/admin/settings/access", headers={
            "Authorization": "Bearer admin-secret",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["allowed_ips"], ALLOWED)
        self.assertEqual(response.json()["client_ip"], ALLOWED[0])
        self.assertNotIn("admin-secret", response.text)
        self.assertNotIn("api-secret", response.text)
        self.assertEqual((await self.request(ALLOWED[0], "/v1/models", headers={
            "Authorization": "Bearer api-secret",
        })).status_code, 200)

    async def test_live_update_persists_and_applies_after_restart(self):
        new_ips = [ALLOWED[0], "2001:db8::1"]
        response = await self.save(ALLOWED[0], new_ips)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["persisted"])
        self.assertEqual(self.store.load(ALLOWED), new_ips)
        self.assertEqual((await self.request(ALLOWED[1])).status_code, 403)
        self.assertEqual((await self.request("2001:db8::1")).status_code, 200)
        restarted = build_app(AppConfig(gateway=GatewayConfig(
            data_dir=self.tempdir.name, allowed_ips=list(ALLOWED),
        )))
        self.assertEqual((await self.request(ALLOWED[1], app=restarted)).status_code, 403)
        self.assertEqual((await self.request("2001:db8::1", app=restarted)).status_code, 200)

    async def test_cannot_lock_out_current_remote_client(self):
        response = await self.save(ALLOWED[0], [ALLOWED[1]])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.cfg.gateway.allowed_ips, ALLOWED)
        self.assertFalse(self.store.path.exists())

    async def test_empty_policy_cannot_lock_out_current_client(self):
        response = await self.save(ALLOWED[0], [])
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.request(ALLOWED[0])).status_code, 200)
        self.assertEqual((await self.request("::1")).status_code, 403)

    async def test_invalid_policy_cannot_change_live_or_saved_values(self):
        self.store.save(ALLOWED)
        before = self.store.path.read_bytes()
        for ips in (None, "192.0.2.40", ["bad"], ["0.0.0.0/0"]):
            response = await self.save(ALLOWED[0], ips)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.cfg.gateway.allowed_ips, ALLOWED)
            self.assertEqual(self.store.path.read_bytes(), before)

    async def test_unknown_fields_are_rejected(self):
        response = await self.request(
            ALLOWED[0], "/admin/settings/access", method="PUT",
            body={"allowed_ips": [], "admin_key": "oops"},
            headers={"Authorization": "Bearer admin-secret"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.cfg.gateway.admin_key, "admin-secret")

    async def test_disk_failure_does_not_change_active_policy(self):
        with patch.object(AccessControlStore, "save", side_effect=OSError("disk full")):
            response = await self.save(ALLOWED[0], [ALLOWED[0]])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.cfg.gateway.allowed_ips, ALLOWED)

    async def test_retry_persistence_does_not_overwrite_access_policy(self):
        await self.save(ALLOWED[0], [ALLOWED[0]])
        retry_store = GatewaySettingsStore(self.tempdir.name)
        retry_store.save(retry_settings_view(self.cfg.gateway))
        self.assertEqual(self.store.load([]), [ALLOWED[0]])
        self.assertEqual(set(json.loads(retry_store.path.read_text())), {"retry"})

    async def test_corrupt_persisted_policy_prevents_insecure_startup(self):
        self.store.path.write_text('{"allowed_ips": null}', encoding="utf-8")
        with self.assertRaises(ValueError):
            build_app(self.cfg)


if __name__ == "__main__":
    unittest.main()
