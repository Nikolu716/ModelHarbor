"""Usage limits must recover passively without generation probes or races."""
import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.chatgpt import completed_response
from app.codex_account import CodexAccount
from app.config import GatewayConfig, StationConfig
from app.errors import classify_error
from app.health import HealthMonitor
from app.proxy import OpenAIProxy, _StreamUsageParser
from app.router import Router
from app.state import Registry, StationState
from app.stats import EventLog
from app.visual_tests import VisualTestRunner, VisualTestStore


class NativeLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(data_dir=self.temp.name, cool_down=1, max_rounds=1, max_attempts=1)
        self.cfg = StationConfig.from_dict('Native', {'provider': 'chatgpt', 'chatgpt_mode': 'codex_relay',
            'chatgpt_account': '0123456789abcdef0123456789abcdef', 'max_concurrency': 8})
        self.registry = Registry(self.gateway, [self.cfg])
        self.st = self.registry.get('Native')
        self.registry.chatgpt.usage_view = lambda profile: {'status': 'connected', 'windows': []}
        self.router = Router(self.registry, self.gateway)
        self.proxy = OpenAIProxy(self.registry, self.gateway, EventLog(self.temp.name))

    async def asyncTearDown(self):
        await self.registry.close_clients()
        await self.registry.chatgpt.close()
        self.temp.cleanup()

    def restrict(self, **fields):
        error = classify_error('Native', 429, json.dumps({'error': {
            'type': 'usage_limit_reached', 'message': 'The usage limit has been reached', **fields}}))
        self.st.mark_quota_exhausted(error)
        return error

    def due(self):
        self.st.quota_resume_at = time.time() - 1
        self.st.rate_limit_until = 0
        self.st.recovery_attempt_at = 0

    async def test_native_type_reset_and_retry_after_preserved(self):
        reset = time.time() + 300
        err = self.restrict(resets_at=reset, resets_in_seconds=240, limit_id='model-x')
        self.assertEqual(err.code, 'usage_limit_reached')
        self.assertEqual(err.limit_id, 'model-x')
        self.assertEqual(self.st.quota_resume_at, reset)
        self.proxy._handle_failure(self.st, err, retry_after='1200')
        self.assertGreaterEqual(self.st.quota_resume_at, time.time() + 1198)
        view = self.st.protection_view(self.gateway, time.time())
        self.assertEqual(view['wait_source'], 'upstream')
        self.assertEqual(view['recovery_mode'], 'passive')
        self.assertFalse(view['recovery_ready'])
        self.assertFalse(self.registry.is_routeable(self.st))

    async def test_invalid_resets_ignored_unknown_wait_backs_off_and_persists(self):
        self.restrict(resets_at='oops', resets_in_seconds=-20)
        self.assertAlmostEqual(self.st.quota_resume_at - self.st.quota_at, 900)
        saved = self.st.durable_view()
        restored = StationState(self.cfg, gateway=self.gateway)
        restored.restore_durable(saved)
        self.assertEqual(restored.quota_resume_at, self.st.quota_resume_at)
        self.restrict()
        self.assertAlmostEqual(self.st.quota_resume_at - self.st.quota_at, 1800)
        for _ in range(10):
            self.restrict()
        self.assertEqual(self.st.native_retry_interval(), 21600)

    async def test_old_native_pause_migrates_without_postponing_restart(self):
        self.restrict()
        saved = self.st.durable_view()
        for key in ('quota_code', 'quota_limit_id', 'quota_resume_at', 'quota_wait_source', 'quota_recovery_attempts'):
            saved['values'].pop(key)
        saved['values']['quota_at'] = time.time() - 3600
        saved['values']['quota_error'] = '[Native] HTTP 429: The usage limit has been reached'
        restored = StationState(self.cfg, gateway=self.gateway)
        restored.restore_durable(saved)
        self.assertTrue(restored.native_usage_limited)
        self.assertLess(restored.quota_resume_at, time.time())
        self.assertTrue(self.registry.is_routeable(restored))
        again = StationState(self.cfg, gateway=self.gateway)
        again.restore_durable(restored.durable_view())
        self.assertEqual(again.quota_resume_at, restored.quota_resume_at)

    async def test_only_one_real_recovery_request_and_complete_success_clears(self):
        self.restrict()
        self.due()
        permits = await asyncio.gather(*(self.router.acquire(self.st) for _ in range(12)))
        allowed = [p for p in permits if p]
        self.assertEqual(len(allowed), 1)
        permit = allowed[0]
        self.assertTrue(self.st.quota_exhausted)
        self.assertTrue(self.st.recovery_inflight)
        self.st.record_success(.1, quota_revision=permit.quota_revision, signature=self.st.connection_signature())
        self.router.release(self.st, permit)
        self.assertFalse(self.st.quota_exhausted)
        self.assertFalse(self.st.recovery_inflight)
        self.assertTrue(self.registry.is_routeable(self.st))

    async def test_old_success_or_changed_binding_cannot_clear_newer_failure(self):
        self.restrict()
        old = self.st.quota_revision
        self.restrict()
        self.st.record_success(.1, quota_revision=old)
        self.assertTrue(self.st.quota_exhausted)
        self.st.record_success(.1, quota_revision=self.st.quota_revision, signature='old-binding')
        self.assertTrue(self.st.quota_exhausted)
        self.st.record_success(.1)
        self.assertTrue(self.st.quota_exhausted)

    async def test_cancelled_or_failed_trial_releases_gate_but_keeps_wait(self):
        self.restrict()
        self.due()
        permit = await self.router.acquire(self.st)
        until = self.st.quota_resume_at
        self.router.release(self.st, permit)
        self.router.release(self.st, permit)
        self.assertFalse(self.st.recovery_inflight)
        self.assertEqual(self.st.quota_resume_at, until)
        self.assertFalse(self.registry.is_routeable(self.st))
        self.assertIsNone(await self.router.acquire(self.st))
        self.assertEqual(self.registry.ensure_semaphore(self.st)._value, 8)

    async def test_recovery_outcomes_persist_without_extending_wait_on_restart(self):
        for outcome, error, expected in [('cancelled', 'client_cancelled', 'interrupted'),
                                          ('stream_error', 'incomplete SSE response', 'failed')]:
            with self.subTest(outcome=outcome):
                self.restrict()
                self.due()
                permit = await self.router.acquire(self.st)
                until = self.st.quota_resume_at
                self.router.release(self.st, permit, outcome, error)
                self.router.release(self.st, permit, 'failed', 'late cleanup')
                view = self.st.protection_view(self.gateway, time.time())
                self.assertEqual(view['recovery_outcome'], expected)
                self.assertEqual(view['recovery_error'], error)
                self.assertEqual(view['recovery_attempt_at'], permit.recovery_at)
                self.assertGreaterEqual(view['recovery_finished_at'], permit.recovery_at)
                restored = StationState(self.cfg, gateway=self.gateway)
                restored.restore_durable(self.st.durable_view())
                self.assertEqual(restored.quota_recovery_outcome, expected)
                self.assertEqual(restored.quota_resume_at, until)
                self.assertTrue(restored.quota_exhausted)

    async def test_restart_during_recovery_reports_interruption_not_success(self):
        self.restrict()
        self.due()
        permit = await self.router.acquire(self.st)
        saved = self.st.durable_view()
        restored = StationState(self.cfg, gateway=self.gateway)
        restored.restore_durable(saved)
        self.assertEqual(restored.quota_recovery_outcome, 'interrupted')
        self.assertEqual(restored.quota_recovery_error, 'service_restarted')
        self.assertFalse(restored.recovery_inflight)
        self.assertTrue(restored.quota_exhausted)
        self.assertEqual(restored.quota_resume_at, self.st.quota_resume_at)
        self.router.release(self.st, permit)

    async def test_late_cleanup_cannot_replace_new_restriction_or_binding(self):
        for change_binding in (False, True):
            with self.subTest(change_binding=change_binding):
                self.restrict()
                self.due()
                permit = await self.router.acquire(self.st)
                if change_binding:
                    self.registry.update_station('Native', {'chatgpt_account': 'f' * 32})
                self.restrict(resets_in_seconds=2400)
                self.router.release(self.st, permit, 'cancelled', 'client_cancelled')
                self.assertEqual(self.st.quota_recovery_outcome, '')
                self.assertGreater(self.st.quota_resume_at, time.time() + 2398)

    async def test_manual_recovery_records_interruption_and_failure(self):
        runner = VisualTestRunner(self.registry, self.gateway, VisualTestStore(self.temp.name))
        self.st.probed_models = {'test-model'}
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                self.restrict()
                self.due()
                self.st.quota_at = self.st.quota_probe_at = time.time() - 61
                runner.next_allowed.clear()
                runner.run = AsyncMock(side_effect=asyncio.CancelledError() if cancelled else None,
                                       return_value={'ok': False, 'error': 'incomplete_response'})
                if cancelled:
                    with self.assertRaises(asyncio.CancelledError):
                        await runner.retry_subscription(self.st, None)
                else:
                    result = await runner.retry_subscription(self.st, None)
                    self.assertEqual(result['protection']['recovery_outcome'], 'failed')
                self.assertEqual(self.st.quota_recovery_outcome, 'interrupted' if cancelled else 'failed')
                self.assertFalse(self.st.recovery_inflight)
                self.assertTrue(self.st.quota_exhausted)
                self.assertGreater(self.st.quota_resume_at, time.time())
                self.assertNotIn(self.st.cfg.name, runner.busy)

    async def test_same_profile_recovery_is_single_flight(self):
        other = StationState(self.cfg, gateway=self.gateway)
        self.registry.states['Alias'] = other
        self.restrict()
        other.mark_quota_exhausted(classify_error('Alias', 429, '{"error":{"type":"usage_limit_reached"}}'))
        self.due()
        other.quota_resume_at = time.time() - 1
        permit = await self.router.acquire(self.st)
        self.assertIsNotNone(permit)
        self.assertFalse(self.registry.is_routeable(other))
        self.assertIsNone(await self.router.acquire(other))
        self.router.release(self.st, permit)

    async def test_metadata_spare_stale_errors_and_other_buckets_never_clear(self):
        self.restrict(limit_id='codex')
        original = self.st.quota_resume_at
        now = time.time() + .01
        usage = {'status': 'connected', 'updated_at': now, 'windows': [
            {'limit_id': 'codex', 'used_percent': 0, 'resets_at': now + 7200}]}
        self.st.reconcile_usage_wait(usage, now)
        self.assertEqual(self.st.quota_resume_at, original)
        for changes in ({'stale': True}, {'status': 'error'}, {'error': 'offline'}, {'updated_at': self.st.quota_at - 1}):
            blocked = {**usage, 'windows': [{'limit_id': 'codex', 'used_percent': 100, 'resets_at': now + 7200}], **changes}
            self.st.reconcile_usage_wait(blocked, now)
            self.assertEqual(self.st.quota_resume_at, original)
        self.st.reconcile_usage_wait({**usage, 'windows': [{'limit_id': 'other', 'used_percent': 100, 'resets_at': now + 7200}]}, now)
        self.assertEqual(self.st.quota_resume_at, original)
        self.st.reconcile_usage_wait({**usage, 'windows': [{'limit_id': 'codex', 'used_percent': 100, 'resets_at': now + 7200}]}, now)
        self.assertEqual(self.st.quota_resume_at, now + 7200)
        self.assertTrue(self.st.quota_exhausted)

    async def test_shared_restriction_remains_indefinite_in_all_modes(self):
        err = classify_error('Native', 200, '{"error":{"code":"subscription_sharing_usage_limit_exceeded","message":"usage limit"}}')
        self.st.mark_quota_exhausted(err)
        self.assertFalse(self.st.native_usage_limited)
        self.assertEqual(self.st.quota_resume_at, 0)
        self.assertFalse(self.registry.is_routeable(self.st))
        self.assertEqual(self.st.protection_view(self.gateway, time.time())['recovery_mode'], '')

    async def test_no_background_generation_and_manual_retry_obeys_same_wait(self):
        self.restrict()
        self.st.probed_models = {'test-model'}
        self.st.model_api_paths = {'test-model': ['/responses']}
        monitor = HealthMonitor(self.registry, self.gateway)
        self.assertFalse(monitor._quota_recovery_due(self.st, time.time() + 86400))
        monitor._probe_chat = AsyncMock()
        self.registry.chatgpt.refresh_usage = AsyncMock()
        await monitor._probe_all()
        monitor._probe_chat.assert_not_awaited()
        runner = VisualTestRunner(self.registry, self.gateway, VisualTestStore(self.temp.name))
        with self.assertRaises(HTTPException) as raised:
            await runner.retry_subscription(self.st, None)
        self.assertEqual(raised.exception.status_code, 429)

    async def test_http200_stream_and_top_level_error_retain_usage_code(self):
        for event in ({'type': 'response.failed', 'response': {'error': {'code': 'usage_limit_reached', 'message': 'wait', 'resets_at': 2000000000}}},
                      {'type': 'error', 'code': 'usage_limit_reached', 'message': 'wait', 'resets_at': 2000000000}):
            raw = b'data: ' + json.dumps(event).encode() + b'\n\n'
            result, error = completed_response(raw)
            self.assertIsNone(result)
            err = classify_error('Native', 200, json.dumps({'error': error}))
            self.assertEqual(err.code, 'usage_limit_reached')
            parser = _StreamUsageParser()
            parser.feed(raw)
            self.assertTrue(parser.error)
            parsed = classify_error('Native', 200, parser.error_payload)
            self.assertEqual(parsed.resets_at, 2000000000)
            self.proxy._handle_failure(self.st, parsed)
            self.assertEqual(self.st.quota_resume_at, 2000000000)

    async def test_account_metadata_keeps_bucket_identity_and_reached_type(self):
        bridge = CodexAccount(self.temp.name, 'test@example.test')
        bridge._account = AsyncMock(return_value={'email': 'test@example.test', 'planType': 'plus'})
        bridge.rpc = AsyncMock(return_value={'rateLimitsByLimitId': {
            'codex': {'limitId': 'codex', 'primary': {'usedPercent': 0, 'resetsAt': 2000000000}},
            'specific': {'limitId': 'model-x', 'rateLimitReachedType': 'primary', 'primary': {'usedPercent': 100}}}})
        view = await bridge.refresh()
        self.assertEqual([w['limit_id'] for w in view['windows']], ['codex', 'model-x'])
        self.assertEqual(view['limits'][1]['reached_type'], 'primary')

    async def test_proxy_recovers_with_one_completed_real_request_json_and_stream(self):
        events = (b'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
                  b'data: {"type":"response.completed","response":{"id":"resp_test","output":[],"usage":{"input_tokens":2,"output_tokens":1}}}\n\n')
        calls = []
        async def upstream(request):
            calls.append(request)
            return httpx.Response(200, content=events, headers={'content-type': 'text/event-stream'})
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.st.probed_models = {'test-model'}
        self.st.model_api_paths = {'test-model': ['/responses']}
        for streaming in (False, True):
            self.restrict()
            self.due()
            class Request:
                client, headers = None, {}
                async def json(self):
                    return {'model': 'test-model', 'input': 'a real request', 'stream': streaming}
                async def is_disconnected(self):
                    return False
            with patch('app.chatgpt.authorized_headers', new=AsyncMock(return_value={})):
                response = await self.proxy._dispatch(Request(), '/responses')
                self.assertEqual(response.status_code, 200, getattr(response, 'body', b''))
                if streaming:
                    async for _ in response.body_iterator:
                        pass
            self.assertFalse(self.st.quota_exhausted)
            self.assertFalse(self.st.recovery_inflight)
            self.assertEqual(self.st.quota_recovery_outcome, '')
        self.assertEqual(len(calls), 2)

    async def test_proxy_real_trial_restriction_keeps_structured_wait_without_retry(self):
        calls = []
        async def upstream(request):
            calls.append(request)
            return httpx.Response(429, headers={'Retry-After': '1500'}, json={'error': {
                'type': 'usage_limit_reached', 'message': 'The usage limit has been reached'}})
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.st.probed_models = {'test-model'}
        self.st.model_api_paths = {'test-model': ['/responses']}
        self.restrict()
        self.due()
        class Request:
            client, headers = None, {}
            async def json(self):
                return {'model': 'test-model', 'input': 'a real request'}
            async def is_disconnected(self):
                return False
        with patch('app.chatgpt.authorized_headers', new=AsyncMock(return_value={})):
            response = await self.proxy._dispatch(Request(), '/responses')
        self.assertEqual(response.status_code, 429)
        self.assertGreaterEqual(int(response.headers['retry-after']), 1498)
        self.assertEqual(len(calls), 1, getattr(response, 'body', b''))
        self.assertTrue(self.st.quota_exhausted)
        self.assertFalse(self.st.recovery_inflight)
        self.assertGreater(self.st.quota_resume_at, time.time() + 1498)
        self.assertEqual(self.st.quota_code, 'usage_limit_reached')
        self.assertEqual(self.st.quota_recovery_outcome, '')

    async def test_normal_and_mapped_wait_return_429_without_upstream_or_round_retries(self):
        self.st.probed_models = {'test-model'}
        self.st.model_api_paths = {'test-model': ['/responses']}
        self.restrict(resets_in_seconds=1200)
        self.gateway.max_rounds = 0
        self.proxy._wait_for_retry = AsyncMock(side_effect=AssertionError('must not loop'))
        calls = []
        async def upstream(request):
            calls.append(request)
            raise AssertionError('must not generate while waiting')
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        for mapped in (False, True):
            if mapped:
                self.registry.model_mappings.save([{'id': 'native', 'name': 'Native alias',
                    'enabled': True, 'source_model': 'alias', 'source_ips': [],
                    'targets': [{'station': 'Native', 'model': 'test-model'}]}], self.registry)
            class Request:
                client, headers = None, {}
                async def json(self):
                    return {'model': 'alias' if mapped else 'test-model', 'input': 'test'}
                async def is_disconnected(self):
                    return False
            response = await self.proxy._dispatch(Request(), '/responses')
            self.assertEqual(response.status_code, 429, response.body)
            self.assertGreaterEqual(int(response.headers['retry-after']), 1198)
        self.assertEqual(calls, [])
        self.proxy._wait_for_retry.assert_not_awaited()

    async def test_real_recovery_stream_closed_by_client_records_interruption(self):
        events = (b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
                  b'data: {"type":"response.completed","response":{"id":"resp_test","output":[]}}\n\n')
        calls = []
        async def upstream(request):
            calls.append(request)
            return httpx.Response(200, content=events, headers={'content-type': 'text/event-stream'})
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.st.probed_models = {'test-model'}
        self.st.model_api_paths = {'test-model': ['/responses']}
        self.restrict()
        self.due()
        class Request:
            client, headers = None, {}
            async def json(self):
                return {'model': 'test-model', 'input': 'real request', 'stream': True}
            async def is_disconnected(self):
                return False
        with patch('app.chatgpt.authorized_headers', new=AsyncMock(return_value={})):
            response = await self.proxy._dispatch(Request(), '/responses')
            self.assertEqual(response.status_code, 200)
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.st.quota_exhausted)
        self.assertFalse(self.st.recovery_inflight)
        self.assertEqual(self.st.quota_recovery_outcome, 'interrupted')
        self.assertEqual(self.st.quota_recovery_error, 'client_stream_closed')
        self.assertGreater(self.st.quota_resume_at, time.time())

    async def test_mapped_recovery_blocks_parallel_request_and_then_recovers(self):
        self.st.probed_models = {'test-model'}
        self.st.model_api_paths = {'test-model': ['/responses']}
        self.registry.model_mappings.save([{'id': 'native', 'name': 'Native alias',
            'enabled': True, 'source_model': 'alias', 'source_ips': [],
            'targets': [{'station': 'Native', 'model': 'test-model'}]}], self.registry)
        self.restrict()
        self.due()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def upstream(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, content=b'data: {"type":"response.completed","response":{"id":"resp_ok","output":[]}}\n\n')
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        class Request:
            client, headers = None, {}
            async def json(self):
                return {'model': 'alias', 'input': 'test'}
            async def is_disconnected(self):
                return False
        with patch('app.chatgpt.authorized_headers', new=AsyncMock(return_value={})):
            trial = asyncio.create_task(self.proxy._dispatch(Request(), '/responses'))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                blocked = await self.proxy._dispatch(Request(), '/responses')
                self.assertEqual(blocked.status_code, 429, blocked.body)
                self.assertTrue(self.st.quota_exhausted)
            finally:
                release.set()
                result = await trial
        self.assertEqual(result.status_code, 200, result.body)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.st.quota_exhausted)
        self.assertFalse(self.st.recovery_inflight)

    async def test_native_protocol_adapter_retains_retry_header(self):
        self.restrict()
        self.st.probed_models = {'test-model'}
        self.st.model_api_paths = {'test-model': ['/responses']}
        blocked = self.proxy._native_usage_wait_response([self.st])
        with patch.object(self.proxy, '_dispatch', new=AsyncMock(return_value=blocked)):
            response = await self.proxy._native_dispatch(None, '/messages')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], blocked.headers['retry-after'])


if __name__ == '__main__':
    unittest.main()
