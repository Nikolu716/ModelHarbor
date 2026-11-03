"""Account isolation, persistence, authorization and live stream switching."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.responses import StreamingResponse

from app.accounts import AccountStore
from app.access_control import AccessControlStore
from app.config import AppConfig, GatewayConfig, StationConfig
from app.health import HealthMonitor
from app.stations import StationStore
from gateway import build_app


class AccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = AppConfig(gateway=GatewayConfig(
            data_dir=self.temp.name, allowed_ips=['10.1.1.1'],
            api_keys=['test-api'], admin_key='test-admin'), stations=[
                StationConfig(name='legacy', base_url='https://old.invalid/v1',
                              api_key='private-upstream-key', models=['model-old'])])
        self.app = build_app(self.cfg)
        self.manager = self.app.state.accounts

    async def asyncTearDown(self):
        for runtime in self.manager.runtimes.values():
            await runtime.registry.close_clients()
            await runtime.notifier.close()

    async def request(self, path, method='GET', body=None, host='10.1.1.1',
                      app=None, headers=None):
        auth = {'Authorization': 'Bearer test-admin'} if path.startswith('/admin/') else {'Authorization': 'Bearer test-api'}
        auth.update(headers or {})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
                app=app or self.app, client=(host, 12345), raise_app_exceptions=False),
                base_url='http://gateway') as client:
            return await client.request(method, path, json=body, headers=auth)

    async def create(self, **changes):
        body = {'username': 'alice', 'display_name': 'Alice', 'allowed_ips': ['10.1.1.1']}
        body.update(changes)
        response = await self.request('/admin/accounts', 'POST', body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    async def switch(self, account_id, body=None, **kwargs):
        return await self.request('/admin/accounts/' + account_id + '/switch',
                                  'POST', {} if body is None else body, **kwargs)

    async def test_legacy_data_is_preserved_without_copying_secrets(self):
        self.assertEqual(self.manager.active.id, 'default')
        self.assertEqual(self.manager.active.registry.stations, self.cfg.stations)
        self.assertEqual(self.manager.active.gateway.allowed_ips, ['10.1.1.1'])
        response = await self.request('/admin/accounts')
        self.assertNotIn('private-upstream-key', response.text)
        self.assertNotIn('password_hash', response.text)
        self.assertEqual(response.json()['scope'], 'gateway')

    async def test_station_allowlist_usage_and_settings_are_isolated(self):
        account_id = await self.create(allowed_ips=['10.1.1.1', '10.2.2.2'])
        self.assertEqual((await self.switch(account_id)).status_code, 200)
        self.assertEqual((await self.request('/admin/stations')).json(), [])
        new_station = {'name': 'personal', 'base_url': 'https://new.invalid/v1',
                       'api_key': 'new-private-key', 'models': ['model-new'], 'probe_chat': False}
        self.assertEqual((await self.request('/admin/stations', 'POST', new_station)).status_code, 201)
        self.assertEqual((await self.request('/admin/settings/access')).json()['allowed_ips'], ['10.1.1.1', '10.2.2.2'])
        self.assertEqual((await self.request('/healthz', host='127.0.0.1')).status_code, 403)
        self.assertEqual((await self.request('/healthz', host='10.2.2.2')).status_code, 200)
        self.assertEqual((await self.request('/admin/settings/retry', 'PUT', {'max_attempts': 6})).status_code, 200)
        self.assertNotEqual(self.manager.active.eventlog.path, self.manager.runtimes['default'].eventlog.path)
        self.manager.active.eventlog.daily_totals['2026-10-21'] = {'calls': 17}
        self.assertEqual((await self.switch('default')).status_code, 200)
        self.assertEqual((await self.request('/admin/stations')).json()[0]['name'], 'legacy')
        self.assertEqual(self.manager.active.gateway.allowed_ips, ['10.1.1.1'])
        self.assertEqual(self.manager.active.gateway.max_attempts, 3)
        self.assertNotIn('2026-10-21', self.manager.active.eventlog.daily_totals)
        self.assertEqual((await self.switch(account_id)).status_code, 200)
        self.assertEqual(self.manager.active.eventlog.daily_totals['2026-10-21']['calls'], 17)
        self.assertEqual(self.manager.active.gateway.max_attempts, 6)

    async def test_scoring_policy_is_account_scoped_and_survives_restart(self):
        self.assertEqual((await self.request('/admin/settings/scoring', 'PUT', {'scoring_speed_weight': .05})).status_code, 200)
        account_id = await self.create()
        await self.switch(account_id)
        self.assertEqual((await self.request('/admin/settings/scoring')).json()['scoring_speed_weight'], .15)
        await self.request('/admin/settings/scoring', 'PUT', {'scoring_speed_weight': .45})
        await self.switch('default')
        self.assertEqual((await self.request('/admin/settings/scoring')).json()['scoring_speed_weight'], .05)
        # Ignore in-memory values: a fresh app must read each account's saved file.
        self.cfg.gateway.scoring_speed_weight = .15
        restored = build_app(self.cfg)
        try:
            self.assertEqual((await self.request('/admin/settings/scoring', app=restored)).json()['scoring_speed_weight'], .05)
            response = await self.request('/admin/accounts/' + account_id + '/switch', 'POST', {}, app=restored)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual((await self.request('/admin/settings/scoring', app=restored)).json()['scoring_speed_weight'], .45)
        finally:
            for runtime in restored.state.accounts.runtimes.values():
                await runtime.registry.close_clients()
                await runtime.notifier.close()

    async def test_discovered_models_and_priority_mode_follow_active_account(self):
        default = self.manager.active
        default.registry.get('legacy').probed_models = {'private-old-model'}
        account_id = await self.create()
        await self.switch(account_id)
        await self.request('/admin/stations', 'POST', {
            'name': 'personal', 'base_url': 'https://new.invalid', 'api_key': 'private-new'})
        current = self.manager.active

        async def discover(st, *, manual=False):
            st.probed_models = {'private-new-model'}
            st.models_discovery_status = 'ready'

        with patch.object(current.monitor, 'discover_models', side_effect=discover):
            response = await self.request('/admin/station/personal/models/refresh', 'POST', {})
        self.assertEqual(response.json()['detected_models'], ['private-new-model'])
        station = (await self.request('/admin/stations')).json()[0]
        self.assertTrue(station['priority_auto'])
        self.assertNotIn('private-old-model', str(station))
        self.assertEqual(default.registry.get('legacy').probed_models, {'private-old-model'})
        await self.switch('default')
        self.assertEqual((await self.request(
            '/admin/station/personal/models/refresh', 'POST', {})).status_code, 404)
        self.assertFalse((await self.request('/admin/stations')).json()[0]['priority_auto'])

    async def test_optional_password_and_hash_storage(self):
        account_id = await self.create(password='correct-password', require_password=True)
        row = self.manager.store.get(account_id)
        self.assertTrue(row['password_hash'].startswith('pbkdf2_sha256$'))
        self.assertNotIn('correct-password', row['password_hash'])
        self.assertEqual((await self.switch(account_id)).status_code, 403)
        self.assertEqual((await self.switch(account_id, {'username': 'alice', 'password': 'wrong'})).status_code, 403)
        self.assertEqual(self.manager.active.id, 'default')
        self.assertEqual((await self.switch(account_id, {'username': 'ALICE', 'password': 'correct-password'})).status_code, 200)
        self.assertEqual((await self.request('/admin/accounts/' + account_id, 'PUT', {'require_password': False})).status_code, 200)
        await self.switch('default')
        self.assertEqual((await self.switch(account_id)).status_code, 200)

    async def test_password_failures_are_rate_limited(self):
        account_id = await self.create(password='valid-password', require_password=True)
        for _ in range(5):
            self.assertEqual((await self.switch(account_id, {'username': 'alice', 'password': 'wrong'})).status_code, 403)
        response = await self.switch(account_id, {'username': 'alice', 'password': 'valid-password'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '300')

    async def test_validation_duplicate_user_and_admin_security(self):
        await self.create()
        for body in ({'username': 'ALICE', 'display_name': 'Other'},
                     {'username': '../bad', 'display_name': 'Bad'},
                     {'username': 'other', 'display_name': 'Other', 'require_password': True},
                     {'username': 'other', 'display_name': 'Other', 'allowed_ips': None}):
            self.assertIn((await self.request('/admin/accounts', 'POST', body)).status_code, [400, 409])
        for path in ['/admin/accounts', '/admin/accounts/default', '/admin/accounts/default/switch']:
            response = await self.request(path, 'POST' if path.endswith('switch') else 'GET', {}, headers={'Authorization': 'Bearer wrong'})
            self.assertEqual(response.status_code, 401)

    async def test_switch_rejects_remote_lockout_and_forwarded_spoof(self):
        account_id = await self.create(allowed_ips=['10.2.2.2'])
        response = await self.switch(account_id, host='10.1.1.1', headers={'X-Forwarded-For': '10.2.2.2'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.manager.active.id, 'default')

    async def test_default_policy_is_not_inherited_and_empty_target_cannot_activate(self):
        response = await self.request('/admin/accounts', 'POST', {
            'username': 'empty', 'display_name': 'Empty'})
        self.assertEqual(response.status_code, 201)
        account_id = response.json()['id']
        policy = await self.request('/admin/accounts/' + account_id)
        self.assertEqual(policy.json()['allowed_ips'], [])
        self.assertEqual((await self.switch(account_id)).status_code, 400)
        self.assertEqual(self.manager.active.id, 'default')

    async def test_default_and_local_ips_do_not_bypass_active_account_policy(self):
        self.manager.active.gateway.allowed_ips = ['10.1.1.1', '10.2.2.2', '127.0.0.1', '::1']
        account_id = await self.create(allowed_ips=['10.2.2.2'])
        self.assertEqual((await self.switch(account_id, host='10.2.2.2')).status_code, 200)
        for host in ['10.1.1.1', '127.0.0.1', '::1', '::ffff:127.0.0.1']:
            for path in ['/healthz', '/dashboard', '/v1/models', '/admin/logs', '/admin/settings/access']:
                self.assertEqual((await self.request(path, host=host)).status_code, 403)
        self.assertEqual((await self.request('/healthz', host='10.2.2.2')).status_code, 200)

    async def test_log_records_analytics_and_retention_remain_account_local(self):
        account_id = await self.create()
        first = self.manager.active
        first.eventlog.add('legacy', 'default-model', True, 10, client_ip='192.0.2.1')
        self.assertEqual((await self.switch(account_id)).status_code, 200)
        second = self.manager.active
        self.assertEqual((await self.request('/admin/logs')).json()['total'], 0)
        second.eventlog.add('personal', 'private-model', False, 20, client_ip='192.0.2.2')
        self.assertEqual((await self.request('/admin/settings/logs', 'PUT', {'event_retention': 100})).status_code, 200)
        for path in ['/admin/logs', '/admin/logs/analytics?group_by=ip']:
            data = (await self.request(path)).text
            self.assertIn('192.0.2.2', data)
            self.assertNotIn('192.0.2.1', data)
        await self.switch('default')
        for path in ['/admin/logs', '/admin/logs/analytics?group_by=ip']:
            data = (await self.request(path)).text
            self.assertIn('192.0.2.1', data)
            self.assertNotIn('192.0.2.2', data)
        self.assertEqual((await self.request('/admin/settings/logs')).json()['event_retention'], 500)
        self.assertEqual(second.eventlog.maxlen, 100)

    async def test_sqlite_activation_failure_preserves_live_account(self):
        account_id = await self.create()
        with patch.object(AccountStore, 'activate', side_effect=sqlite3.OperationalError('test failure')):
            self.assertEqual((await self.switch(account_id)).status_code, 503)
        self.assertEqual(self.manager.active.id, 'default')
        self.assertEqual(self.manager.store.active_id(), 'default')

    async def test_allowlist_failure_rolls_back_account_metadata(self):
        account_id = await self.create()
        original = self.manager.store.get(account_id)
        with patch.object(AccessControlStore, 'save', side_effect=OSError('disk full')):
            response = await self.request('/admin/accounts/' + account_id, 'PUT', {
                'display_name': 'Unexpected', 'allowed_ips': ['10.9.9.9']})
            self.assertEqual(response.status_code, 500)
            response = await self.request('/admin/accounts', 'POST', {
                'username': 'failed', 'display_name': 'Failed', 'allowed_ips': []})
            self.assertEqual(response.status_code, 500)
        self.assertEqual(self.manager.store.get(account_id), original)
        self.assertEqual(len(self.manager.store.list()), 2)
        self.assertEqual((await self.request('/admin/accounts/' + account_id)).json()['allowed_ips'], ['10.1.1.1'])

    async def test_corrupt_target_cannot_replace_current_runtime(self):
        account_id = await self.create()
        path = self.manager.store.directory(account_id) / 'stations.json'
        path.write_text('{broken', encoding='utf-8')
        self.assertEqual((await self.switch(account_id)).status_code, 503)
        self.assertEqual(self.manager.active.id, 'default')

    async def test_active_selection_and_empty_stations_survive_restart(self):
        account_id = await self.create()
        await self.switch(account_id)
        StationStore(str(self.manager.store.directory(account_id))).save([])
        restarted = build_app(AppConfig(gateway=GatewayConfig(data_dir=self.temp.name, admin_key='test-admin')))
        self.assertEqual(restarted.state.accounts.active.id, account_id)
        self.assertEqual((await self.request('/admin/stations', app=restarted)).json(), [])
        self.assertEqual((await self.request('/healthz', host='10.1.1.1', app=restarted)).status_code, 200)

    async def test_stale_editor_cannot_modify_new_account_even_after_switch_back(self):
        revision = self.manager.revision
        account_id = await self.create()
        await self.switch(account_id)
        await self.switch('default')
        response = await self.request('/admin/settings/access', 'PUT', {'allowed_ips': []},
            headers={'X-ModelHarbor-Account': 'default', 'X-ModelHarbor-Revision': revision})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'account_changed')
        self.assertEqual(self.manager.active.gateway.allowed_ips, ['10.1.1.1'])

    async def test_delete_rules_and_no_key_leak(self):
        account_id = await self.create(password='hidden-password')
        self.assertEqual((await self.request('/admin/accounts/default', 'DELETE')).status_code, 409)
        await self.switch(account_id)
        self.assertEqual((await self.request('/admin/accounts/' + account_id, 'DELETE')).status_code, 409)
        await self.switch('default')
        self.assertEqual((await self.request('/admin/accounts/' + account_id, 'DELETE')).status_code, 200)
        self.assertEqual((await self.switch(account_id)).status_code, 404)

    async def test_stream_stays_on_old_runtime_and_inactive_monitor_stops(self):
        account_id = await self.create()
        first = self.manager.active
        second = self.manager.runtime(account_id)
        begin, finish = asyncio.Event(), asyncio.Event()

        async def stream():
            begin.set()
            yield b'first-account-start\n'
            await finish.wait()
            yield b'first-account-end\n'

        @first.app.get('/v1/test-stream')
        async def endpoint():
            return StreamingResponse(stream())

        with patch.object(HealthMonitor, '_probe_all', new_callable=AsyncMock), \
                patch.object(first.registry, 'close_clients', new_callable=AsyncMock) as close:
            await self.manager.start()
            try:
                task = asyncio.create_task(self.request('/v1/test-stream'))
                await asyncio.wait_for(begin.wait(), 2)
                monitor_task = first.monitor_task
                self.assertEqual((await self.switch(account_id)).status_code, 200)
                await asyncio.sleep(0.05)
                self.assertTrue(monitor_task.done())
                self.assertIsNotNone(second.monitor_task)
                self.assertEqual(first.requests, 1)
                close.assert_not_awaited()
                self.assertEqual((await self.request('/v1/models')).headers['x-modelharbor-account'], account_id)
                finish.set()
                response = await asyncio.wait_for(task, 2)
                self.assertEqual(response.text, 'first-account-start\nfirst-account-end\n')
                self.assertEqual(response.headers['x-modelharbor-account'], 'default')
                await asyncio.sleep(0.05)
                close.assert_awaited_once()
                self.assertEqual(first.requests, 0)
            finally:
                finish.set()
                await self.manager.stop()

    async def test_openapi_keeps_proxy_and_admin_paths(self):
        paths = self.app.openapi()['paths']
        self.assertIn('/v1/models', paths)
        self.assertIn('/admin/stations', paths)
        self.assertIn('/admin/accounts/{account_id}/switch', paths)

    async def test_rapid_switch_preserves_old_activation_and_single_monitor(self):
        account_id = await self.create()
        first = self.manager.active
        with patch.object(HealthMonitor, '_probe_all', new_callable=AsyncMock):
            await self.manager.start()
            try:
                activation = first.inactive
                original_monitor = first.monitor_task
                responses = await asyncio.gather(self.switch(account_id), self.switch('default'))
                self.assertEqual([r.status_code for r in responses], [200, 200])
                await asyncio.sleep(.05)
                self.assertEqual(self.manager.active.id, 'default')
                self.assertTrue(activation.is_set())
                self.assertFalse(first.inactive.is_set())
                self.assertTrue(original_monitor.done())
                self.assertFalse(first.monitor_task.done())
                self.assertIsNone(self.manager.runtimes[account_id].monitor_task)
                self.assertEqual(self.manager.store.active_id(), 'default')
            finally:
                await self.manager.stop()


if __name__ == '__main__':
    unittest.main()
