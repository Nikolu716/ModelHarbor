"""Metadata discovery must never generate, flood, or contaminate routing health."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import httpx

from app.config import GatewayConfig, StationConfig
from app.errors import UpstreamError
from app.health import HealthMonitor, MODEL_REFRESH_INTERVAL
from app.router import Router
from app.state import Registry, StationState, STATUS_DOWN


class ModelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = GatewayConfig()
        self.registry = Registry(self.gateway, [StationConfig(
            'alpha', 'https://upstream.invalid', 'private-key',
            probe_models=True, probe_balance=False, priority_auto=True)])
        self.st = self.registry.get('alpha')
        self.monitor = HealthMonitor(self.registry, self.gateway)
        self.requests = []
        self.response = httpx.Response(200, json={'data': [{'id': 'model-a'}, 'model-b']})

        async def handle(request):
            self.requests.append(request)
            return self.response
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def asyncTearDown(self):
        await self.registry.close_clients()

    def expire(self):
        self.st.models_checked_at = 0
        self.st.models_next_refresh_at = 0
        self.st.models_retry_after_until = 0

    async def test_discovery_and_polling_share_cache_and_never_generate(self):
        score = self.st.health_score
        await self.monitor._probe_all()
        self.st.last_probe_at = 0
        await self.monitor._probe_all()
        await self.monitor.discover_models(self.st, manual=True)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].method, 'GET')
        self.assertEqual(self.requests[0].url.path, '/v1/models')
        self.assertEqual(self.requests[0].headers['authorization'], 'Bearer private-key')
        self.assertEqual(self.st.probed_models, {'model-a', 'model-b'})
        self.assertEqual(self.st.health_score, score)
        self.assertIsNone(self.st.ema_latency)
        self.assertEqual(len(self.st.window_results), 0)
        self.assertEqual(self.st.calls, 0)
        self.assertGreater(self.st.models_next_refresh_at, time.time() + MODEL_REFRESH_INTERVAL - 2)
        self.assertEqual(self.registry.candidates_for('model-a'), [self.st])
        self.assertEqual(self.registry.candidates_for('missing'), [])

    async def test_success_replaces_removed_models_and_empty_is_authoritative(self):
        self.st.cfg = dataclasses.replace(self.st.cfg, models=['manual'])
        await self.monitor.discover_models(self.st)
        self.expire()
        self.response = httpx.Response(200, json={'data': ['model-c']})
        await self.monitor.discover_models(self.st)
        self.assertEqual(self.st.effective_models, {'manual', 'model-c'})
        self.expire()
        self.response = httpx.Response(200, json={'data': []})
        await self.monitor.discover_models(self.st)
        self.assertEqual(self.st.probed_models, set())
        self.assertEqual(self.st.effective_models, {'manual'})
        self.assertFalse(self.st.accepts_any_model)

    async def test_transient_error_retains_catalog_and_backs_off(self):
        await self.monitor.discover_models(self.st)
        before = self.st.models_discovered_at
        self.expire()
        self.response = httpx.Response(503, text='secret details must not leak')
        await self.monitor.discover_models(self.st)
        await self.monitor.discover_models(self.st)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.st.probed_models, {'model-a', 'model-b'})
        self.assertEqual(self.st.models_discovered_at, before)
        self.assertGreater(self.st.models_next_refresh_at, time.time() + 299)
        self.assertNotIn('secret', self.st.models_discovery_error)
        self.assertEqual(self.st.consecutive_failures, 0)
        self.expire()
        await self.monitor.discover_models(self.st)
        self.assertGreater(self.st.models_next_refresh_at, time.time() + 599)

    async def test_deterministic_failure_stops_until_manual_retry(self):
        for status, expected in [(401, 'auth_error'), (403, 'auth_error'),
                                 (404, 'unsupported'), (405, 'unsupported')]:
            self.expire()
            self.st.models_discovery_status = 'pending'
            self.response = httpx.Response(status)
            count = len(self.requests)
            await self.monitor.discover_models(self.st)
            self.assertEqual(self.st.models_discovery_status, expected)
            self.expire()
            await self.monitor.discover_models(self.st)
            self.assertEqual(len(self.requests), count + 1)
            await self.monitor.discover_models(self.st, manual=True)
            self.assertEqual(len(self.requests), count + 2)

    async def test_retry_after_is_honored_for_manual_requests_and_dates(self):
        for status, header in [(429, '1200'), (503, '1200'),
                               (429, format_datetime(datetime.fromtimestamp(
                                   time.time() + 1200, timezone.utc), usegmt=True))]:
            self.expire()
            self.st.models_discovery_status = 'pending'
            self.response = httpx.Response(status, headers={'Retry-After': header})
            count = len(self.requests)
            await self.monitor.discover_models(self.st)
            self.st.models_checked_at = time.time() - 120
            await self.monitor.discover_models(self.st, manual=True)
            self.assertEqual(len(self.requests), count + 1)
            self.assertGreater(self.st.models_retry_after_until, time.time() + 1198)

    async def test_invalid_partial_and_redirect_responses_keep_last_catalog(self):
        await self.monitor.discover_models(self.st)
        for response in [httpx.Response(200, text='<html>login</html>'),
                         httpx.Response(200, json={'data': [None]}),
                         httpx.Response(200, json={'data': ['partial'], 'has_more': True}),
                         httpx.Response(302, headers={'Location': 'https://other.invalid'})]:
            self.expire()
            self.st.models_discovery_status = 'pending'
            self.response = response
            await self.monitor.discover_models(self.st)
            self.assertEqual(self.st.probed_models, {'model-a', 'model-b'})
            self.assertIn(self.st.models_discovery_status, {'invalid_response', 'unsupported'})
        self.assertEqual(len(self.requests), 5)

    async def test_success_does_not_clear_generation_quota_or_circuit_breaker(self):
        self.st.mark_quota_exhausted(UpstreamError('alpha', 402, 'quota', quota=True), source='request')
        self.st.status = STATUS_DOWN
        self.st.down_since = time.time()
        await self.monitor.discover_models(self.st)
        self.assertTrue(self.st.quota_exhausted)
        self.assertEqual(self.st.status, STATUS_DOWN)
        self.assertEqual(self.st.models_discovery_status, 'ready')

    async def test_generation_probe_skips_without_any_model(self):
        status, latency, error = await self.monitor._generation_probe(self.st)
        self.assertEqual(status, 'skip')
        self.assertIsNone(latency)
        self.assertIn('暂无可用探测模型', error.message)
        self.assertEqual(self.requests, [])

    async def test_auto_generation_uses_upstream_order_and_never_fans_out(self):
        self.response = httpx.Response(200, json={'data': ['z-first', 'a-second']})
        await self.monitor.discover_models(self.st)
        self.response = httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})
        result = await self.monitor.test_generation(self.st)
        self.assertTrue(result['ok'])
        self.assertEqual(result['model'], 'z-first')
        self.assertEqual(json.loads(self.requests[-1].content)['model'], 'z-first')
        self.assertEqual([r.method for r in self.requests], ['GET', 'POST'])
        self.assertFalse(self.gateway.generation_probe_enabled)
        self.assertEqual(self.st.calls, 0)
        again = await self.monitor.test_generation(self.st)
        self.assertTrue(again['skipped'])
        self.assertEqual(len(self.requests), 2)

    async def test_generation_probe_uses_configured_wire_instead_of_stale_probe_path(self):
        for wire, old_path, expected_path, input_key in (
                ('responses', '/chat/completions', '/v1/responses', 'input'),
                ('chat_completions', '/responses', '/v1/chat/completions', 'messages')):
            with self.subTest(wire=wire):
                self.st.cfg = dataclasses.replace(self.st.cfg, models=['model-a'],
                    wire_api=wire, probe_chat_path=old_path)
                self.st.probe_chat_at = 0
                self.response = httpx.Response(200, json={'output': []})
                before = len(self.requests)
                result = await self.monitor.test_generation(self.st)
                self.assertTrue(result['ok'])
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1].url.path, expected_path)
                self.assertIn(input_key, json.loads(self.requests[-1].content))

    async def test_manual_model_survives_catalog_reordering_and_missing_model_never_falls_back(self):
        self.st.cfg = dataclasses.replace(self.st.cfg, probe_chat_model='model-b')
        await self.monitor.discover_models(self.st)
        self.assertEqual(self.st.selected_probe_model, 'model-b')
        self.expire()
        self.response = httpx.Response(200, json={'data': ['new-first', 'model-b', 'model-a']})
        await self.monitor.discover_models(self.st)
        self.assertEqual(self.st.selected_probe_model, 'model-b')
        self.expire()
        self.response = httpx.Response(200, json={'data': ['other-model']})
        await self.monitor.discover_models(self.st)
        before = len(self.requests)
        result = await self.monitor.test_generation(self.st)
        self.assertTrue(result['skipped'])
        self.assertEqual(len(self.requests), before)
        self.assertEqual(self.st.cfg.probe_chat_model, 'model-b')

    async def test_manual_generation_recovers_breaker_without_business_samples(self):
        self.st.cfg = dataclasses.replace(self.st.cfg, models=['fallback-model'])
        self.st.status = STATUS_DOWN
        self.st.down_since = time.time() - self.gateway.cool_down - 1
        self.st.consecutive_failures = 3
        result = await self.monitor.test_generation(self.st)
        self.assertTrue(result['ok'])
        self.assertEqual(self.st.status, 'up')
        self.assertEqual(self.st.consecutive_failures, 0)
        self.assertEqual(len(self.st.window_results), 0)

    async def test_generation_does_not_send_during_rate_or_breaker_cooldown(self):
        self.st.cfg = dataclasses.replace(self.st.cfg, models=['model-a'])
        self.st.mark_rate_limited(120)
        result = await self.monitor.test_generation(self.st)
        self.assertTrue(result['skipped'])
        self.st.rate_limit_until = 0
        self.st.status = STATUS_DOWN
        self.st.down_since = time.time()
        result = await self.monitor.test_generation(self.st)
        self.assertTrue(result['skipped'])
        self.assertEqual(self.requests, [])

    async def test_generation_probe_honors_retry_after_without_circuit_breaking(self):
        self.st.cfg = dataclasses.replace(self.st.cfg, probe_chat_model='model-a')
        self.response = httpx.Response(
            429, headers={'Retry-After': '120'}, json={'error': {'message': 'slow down'}})
        status, _, _ = await self.monitor._generation_probe(self.st)
        self.assertEqual(status, 'client_error')
        self.assertGreater(self.st.rate_limit_until, time.time() + 119)
        self.assertEqual(self.st.consecutive_failures, 0)
        self.assertEqual(self.requests[0].method, 'POST')
        self.assertEqual(json.loads(self.requests[0].content)['model'], 'model-a')

    async def test_background_generation_probe_stays_idle_when_globally_disabled(self):
        self.st.cfg = dataclasses.replace(
            self.st.cfg, probe_models=False, probe_chat=True,
            probe_chat_model='model-a', probe_balance=False)
        self.st.last_probe_at = time.time()
        await self.monitor._probe_all()
        self.assertEqual(self.requests, [])

    async def test_cached_success_cannot_recover_quota(self):
        await self.monitor.discover_models(self.st)
        self.st.mark_quota_exhausted(UpstreamError('alpha', 402, 'quota', quota=True), source='models')
        self.assertFalse(await self.monitor.probe_quota_recovery(self.st))
        self.assertTrue(self.st.quota_exhausted)
        self.assertEqual(len(self.requests), 1)

    async def test_cached_catalog_can_recover_only_from_positive_balance(self):
        await self.monitor.discover_models(self.st)
        self.st.cfg = dataclasses.replace(self.st.cfg, billing='balance', probe_balance=True)
        self.st.mark_quota_exhausted(UpstreamError('alpha', 402, 'quota', quota=True), source='models')
        self.monitor._probe_balance = AsyncMock(side_effect=lambda st: setattr(st, 'balance', 4.0))
        recovered = await self.monitor.probe_quota_recovery(self.st)
        self.assertTrue(recovered)
        self.assertFalse(self.st.quota_exhausted)
        self.assertEqual(len(self.requests), 1)
        self.monitor._probe_balance.assert_awaited_once_with(self.st)

    async def test_disabled_stopped_and_busy_stations_do_not_send_requests(self):
        self.st.enabled = False
        await self.monitor.discover_models(self.st, manual=True)
        self.st.enabled = True
        self.st.semaphore = asyncio.Semaphore(0)
        await self.monitor.discover_models(self.st)
        self.st.semaphore = None
        await self.monitor.stop()
        await self.monitor.discover_models(self.st, manual=True)
        self.assertEqual(self.requests, [])

    async def test_single_flight_and_stale_credentials(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def slow(request):
            self.requests.append(request)
            started.set()
            await finish.wait()
            return httpx.Response(200, json={'data': ['old-key-model']})
        await self.st.client.aclose()
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
        task = asyncio.create_task(self.monitor.discover_models(self.st))
        await started.wait()
        await self.monitor.discover_models(self.st, manual=True)
        self.registry.update_station('alpha', {'api_key': 'new-key'})
        finish.set()
        await task
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.st.probed_models, set())
        self.assertEqual(self.st.models_discovery_status, 'pending')

    async def test_cancellation_restores_state_and_releases_resources(self):
        started = asyncio.Event()
        async def pending(request):
            started.set()
            await asyncio.Future()
        await self.st.client.aclose()
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(pending))
        task = asyncio.create_task(self.monitor.discover_models(self.st))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.st.models_discovery_status, 'pending')
        self.assertFalse(self.st.models_discovery_lock.locked())
        self.assertEqual(self.st.semaphore._value, self.st.cfg.max_concurrency)


class PriorityScoringTests(unittest.TestCase):
    def station(self, **kw):
        return StationState(StationConfig('alpha', 'https://a.invalid', 'key', **kw))

    def test_manual_priority_does_not_change_health(self):
        scores = []
        for priority, bias in [(1, 15), (2, 12), (5, 3), (6, 0), (999, 0)]:
            st = self.station(priority=priority)
            self.assertIsNone(st.health_score)
            st.record_success(90.0)
            self.assertAlmostEqual(st.score_breakdown['reliability_score'], 93.333333, places=4)
            scores.append(st.health_score)
            self.assertEqual(st.priority_bias, bias)
        self.assertLess(max(scores) - min(scores), .0001)

    def test_auto_ignores_priority_and_updates_on_real_results(self):
        first = self.station(priority=1, priority_auto=True)
        last = self.station(priority=10000, priority_auto=True)
        self.assertEqual(first.health_score, last.health_score)
        first.record_success(1.0)
        self.assertAlmostEqual(first.score_breakdown['reliability_score'], 93.333333, places=4)
        self.assertGreater(first.health_score, 85)
        self.assertEqual(first.priority_bias, 0)

    def test_scores_control_order_with_randomization_only_inside_band(self):
        cfgs = [StationConfig(name, 'https://a.invalid', 'key', priority_auto=True, model_routing='passthrough')
                for name in ['fast', 'near', 'slow']]
        registry = Registry(GatewayConfig(), cfgs)
        # Equal timestamps keep tied evidence deterministic before weighted choice.
        with patch('app.state.time.time', return_value=time.time()):
            for name, latency in [('fast', 1), ('near', 2), ('slow', 6)]:
                registry.get(name).record_success(latency, model='any')
        from app.errors import UpstreamError
        registry.get('slow').record_failure(UpstreamError('slow', 503, 'busy'), model='any')
        with patch('app.router.random.random', return_value=0.99):
            picks = Router(registry, registry.gateway).pick('any')
        self.assertEqual([st.cfg.name for st in picks], ['near', 'fast', 'slow'])
        registry.get('fast').enabled = False
        snapshot = {st['name']: st for st in registry.snapshot()['stations']}
        self.assertIsNone(snapshot['fast']['rank'])
        self.assertEqual(snapshot['near']['rank'], 1)

    def test_priority_only_edit_preserves_discovered_models(self):
        cfg = StationConfig('alpha', 'https://a.invalid', 'key', probe_models=True)
        registry = Registry(GatewayConfig(), [cfg])
        st = registry.get('alpha')
        st.probed_models = {'known'}
        registry.update_station('alpha', {'priority_auto': True, 'models': ['extra']})
        self.assertEqual(st.effective_models, {'known', 'extra'})
        self.assertIsNone(st.health_score)

    def test_disabled_station_has_zero_health_score(self):
        st = self.station(enabled=False, priority=1)
        self.assertEqual(st.health_score, 0)


if __name__ == '__main__':
    unittest.main()
