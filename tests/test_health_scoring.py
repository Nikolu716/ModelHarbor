"""Health combines completed reliability with a bounded, passive speed weight."""
import io
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from app.config import GatewayConfig, StationConfig
from app.errors import UpstreamError, classify_error
from app.proxy import OpenAIProxy
from app.router import Router
from app.state import Registry, StationState
from app.stats import EventLog
from app.scoring import speed_score


def station(name='a', **kw):
    return StationState(StationConfig(name, 'https://mock.invalid/v1', 'fixture-only',
                                     models=['m', 'other'], priority_auto=True, **kw))


class HealthScoringTests(unittest.TestCase):
    def test_cli_handles_unassessed_and_disabled_channels(self):
        import cli
        from rich.console import Console

        registry = Registry(GatewayConfig(), [station('fresh').cfg, station('off').cfg,
                                             station('measured').cfg])
        registry.get('off').enabled = False
        registry.get('measured').record_success(60)
        output = io.StringIO()
        with httpx.Client(base_url='http://mock.invalid', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=registry.snapshot())
        )) as client, patch.object(cli, 'console', Console(file=output, width=200, color_system=None)):
            cli.cmd_status(client)
        rendered = output.getvalue()
        self.assertIn('待评估', rendered)
        self.assertIn('已停用', rendered)
        self.assertIn('健康度', rendered)

    def test_speed_changes_score_but_cannot_overwhelm_reliability(self):
        slow, fast = station('slow'), station('fast')
        with patch('app.state.time.time', return_value=time.time()):
            for _ in range(100):
                slow.record_success(90, model='m', path='/responses', stream=True)
                fast.record_success(.1, model='m', path='/responses', stream=True)
        self.assertGreater(slow.health_score, 84)
        self.assertGreater(fast.health_score, 98)
        self.assertGreater(fast.health_score, slow.health_score)
        self.assertLess(fast.health_score - slow.health_score, 15)
        self.assertAlmostEqual(slow.score_breakdown['reliability_score'],
                               fast.score_breakdown['reliability_score'], places=4)
        self.assertGreaterEqual(slow.health_score, .85 * slow.score_breakdown['reliability_score'])
        self.assertLessEqual(fast.health_score, fast.score_breakdown['reliability_score'])
        self.assertEqual(slow.score_breakdown['confidence'], 'high')

    def test_speed_curve_is_monotonic_and_capped_for_each_mode(self):
        for stream, fast, slow in [(True, 2, 30), (False, 10, 120)]:
            with self.subTest(stream=stream):
                self.assertEqual(speed_score(0, stream), 100)
                self.assertEqual(speed_score(fast, stream), 100)
                self.assertAlmostEqual(speed_score((fast * slow) ** .5, stream), 50)
                self.assertEqual(speed_score(slow, stream), 0)
                self.assertEqual(speed_score(10**9, stream), 0)
                self.assertGreater(speed_score(fast * 2, stream), speed_score(fast * 3, stream))

    def test_configurable_weight_scales_penalty_and_routes_using_the_same_policy(self):
        gateway = GatewayConfig(scoring_speed_weight=0)
        registry = Registry(gateway, [station('fast', model_routing='passthrough').cfg,
                                      station('slow', model_routing='passthrough').cfg])
        fast, slow = registry.get('fast'), registry.get('slow')
        with patch('app.state.time.time', return_value=time.time()):
            for _ in range(50):
                fast.record_success(1, model='m', path='/responses', stream=True)
                slow.record_success(30, model='m', path='/responses', stream=True)
            for weight in (0, .15, .3, .5, 1):
                gateway.scoring_speed_weight = weight
                for st in (fast, slow):
                    st._update_score()
                    b = st.routing_evidence('m', '/responses', True)
                    self.assertAlmostEqual(st.health_score, b['score'])
                    self.assertAlmostEqual(b['score'], b['reliability_score'] *
                                           (1 - weight + weight * b['speed_score'] / 100))
                    self.assertGreaterEqual(b['score'], b['reliability_score'] * (1 - weight))
                    self.assertLessEqual(b['score'], b['reliability_score'])
            router = Router(registry, gateway)
            with patch('app.router.random.random', return_value=.99):
                self.assertEqual(router.pick('m', '/responses', stream=True)[0].cfg.name, 'fast')
                gateway.scoring_speed_weight = 0
                self.assertEqual(router.pick('m', '/responses', stream=True)[0].cfg.name, 'slow')

    def test_one_extreme_latency_is_bounded_before_aggregation(self):
        capped, extreme = station('capped'), station('extreme')
        with patch('app.state.time.time', return_value=time.time()):
            for _ in range(99):
                capped.record_success(1, stream=True)
                extreme.record_success(1, stream=True)
            before = extreme.health_score
            capped.record_success(30, stream=True)
            extreme.record_success(10**9, stream=True)
        self.assertAlmostEqual(capped.health_score, extreme.health_score)
        self.assertLess(before - extreme.health_score, 1)

    def test_missing_speed_uses_neutral_value_and_respects_request_mode(self):
        st = station()
        for _ in range(30):
            st.record_success(1, model='m', path='/responses', stream=True)
            st.record_success(120, model='m', path='/responses', stream=False)
        streaming = st.routing_evidence('m', '/responses', True)
        nonstream = st.routing_evidence('m', '/responses', False)
        self.assertAlmostEqual(streaming['reliability_score'], nonstream['reliability_score'], places=4)
        self.assertGreater(streaming['score'], nonstream['score'])
        missing = station()
        missing.record_failure(UpstreamError('a', 503, 'bad'), model='m', path='/responses')
        evidence = missing.routing_evidence('m', '/responses', True)
        self.assertIsNone(evidence['speed_score'])
        self.assertEqual(evidence['speed_samples'], 0)
        self.assertAlmostEqual(evidence['score'], evidence['reliability_score'] * .925)

    def test_routing_counts_speed_once_and_keeps_manual_weight(self):
        registry = Registry(GatewayConfig(), [station('a').cfg, station('b').cfg])
        a, b = registry.get('a'), registry.get('b')
        for _ in range(50):
            a.record_success(2, model='m', path='/responses', stream=True)
            b.record_success(4, model='m', path='/responses', stream=True)
        router = Router(registry, registry.gateway)
        with patch('app.router._weighted_choice', return_value=a) as choose:
            router.pick('m', '/responses', stream=True)
        self.assertEqual(choose.call_args.args[1], [.5, .5])
        registry.update_station('a', {'priority_auto': False, 'priority': 1})
        with patch('app.router._weighted_choice', return_value=a) as choose:
            router.pick('m', '/responses', stream=True)
        self.assertEqual(choose.call_args.args[1], [2 / 3, 1 / 3])

    def test_unknown_disabled_and_sparse_data_are_distinct(self):
        st = station()
        self.assertIsNone(st.health_score)
        self.assertIsNone(st.window_success_rate)
        st.record_success(1)
        self.assertEqual(st.score_breakdown['confidence'], 'low')
        self.assertLess(st.health_score, 100)
        st.enabled = False
        st._update_score()
        self.assertEqual(st.health_score, 0)

    def test_failures_are_attributed_without_counting_client_or_capacity_policy(self):
        for code, message in [(400, 'invalid request'), (413, 'payload too large'),
                              (422, 'invalid parameter'), (429, 'rate limit'), (402, 'payment required')]:
            with self.subTest(code=code):
                st = station()
                st.record_failure(classify_error('a', code, message))
                self.assertIsNone(st.health_score)
                self.assertEqual(st.failures, 1)  # Accounting remains truthful.
        for code in (401, 403, 404, 503):
            st = station()
            st.record_failure(classify_error('a', code, 'unavailable'))
            self.assertLess(st.health_score, 70)

    def test_recent_recovery_outweighs_old_failures_and_samples_expire(self):
        st = station()
        now = time.time()
        with patch('app.state.time.time', return_value=now - 12 * 3600):
            for _ in range(50):
                st.record_failure(UpstreamError('a', 503, 'unavailable'))
        before = st.health_score
        with patch('app.state.time.time', return_value=now):
            for _ in range(30):
                st.record_success(30)
        self.assertGreater(st.score_breakdown['reliability_score'], 85)
        self.assertGreater(st.health_score, 75)
        self.assertGreater(st.health_score, before)
        with patch('app.state.time.time', return_value=now + 25 * 3600):
            st._update_score()
            self.assertIsNone(st.health_score)
            self.assertFalse(st.health_samples)

    def test_persistence_keeps_event_time_and_does_not_import_legacy_booleans(self):
        st = station()
        st.record_success(5, model='m', path='/responses', stream=True)
        data = st.durable_view()
        restored = station()
        restored.restore_durable(data)
        self.assertEqual(list(restored.health_samples), list(st.health_samples))
        self.assertEqual(restored.calls, 1)
        legacy = {**data, 'window_results': [True] * 50}
        legacy.pop('score_version')
        restored = station()
        restored.restore_durable(legacy)
        self.assertEqual(restored.calls, 1)
        self.assertIsNone(restored.health_score)
        self.assertEqual(restored.score_breakdown['legacy_samples_ignored'], 50)
        with patch('app.state.time.time', return_value=time.time() + 25 * 3600):
            expired = station()
            expired.restore_durable({**data, 'saved_at': time.time()})
            self.assertIsNone(expired.health_score)

    def test_probes_and_operating_protection_do_not_invent_health_samples(self):
        st = station()
        st.record_success(80)
        before = st.health_score
        st.record_probe(.1)
        st.mark_rate_limited(600)
        self.assertAlmostEqual(st.health_score, before, places=4)
        self.assertEqual(st.ema_latency, 80)
        self.assertEqual(st.score_breakdown['business_samples'], 1)

    def test_routing_uses_model_protocol_and_stream_specific_evidence(self):
        registry = Registry(GatewayConfig(), [station('a').cfg, station('b').cfg])
        a, b = registry.get('a'), registry.get('b')
        for _ in range(40):
            a.record_success(90, model='m', path='/responses', stream=True)
            b.record_failure(UpstreamError('b', 503, 'bad'), model='m', path='/responses', stream=True)
            a.record_failure(UpstreamError('a', 503, 'bad'), model='other', path='/responses', stream=True)
            b.record_success(.1, model='other', path='/responses', stream=True)
        router = Router(registry, registry.gateway)
        self.assertEqual(router.pick('m', '/responses', stream=True)[0].cfg.name, 'a')
        self.assertEqual(router.pick('other', '/responses', stream=True)[0].cfg.name, 'b')
        self.assertIsNone(a.routing_evidence('m', '/chat/completions')['score'])
        self.assertIsNone(a.routing_evidence('m', '/responses', False)['latency'])
        self.assertAlmostEqual(a.routing_evidence('m', '/responses', True)['latency'], 90)

    def test_changing_connection_clears_only_scoring_evidence(self):
        registry = Registry(GatewayConfig(), [station().cfg])
        st = registry.get('a')
        st.record_success(2)
        registry.update_station('a', {'headers': dict(st.cfg.headers), 'priority': 2})
        self.assertIsNotNone(st.health_score)
        registry.update_station('a', {'wire_api': 'responses'})
        self.assertIsNone(st.health_score)
        st.record_success(2)
        registry.update_station('a', {'api_key': 'changed-fixture'})
        self.assertIsNone(st.health_score)
        self.assertEqual(st.calls, 2)
        old_signature = st.connection_signature()
        registry.update_station('a', {'api_key': 'another-fixture'})
        st.record_success(1, signature=old_signature)
        self.assertIsNone(st.health_score)
        self.assertEqual(st.calls, 3)

    def test_output_limit_is_not_an_upstream_outage(self):
        from app.proxy import _StreamUsageParser

        for reason in ('max_output_tokens', 'content_filter'):
            with self.subTest(reason=reason):
                parser = _StreamUsageParser()
                parser.feed(('data: {"type":"response.incomplete","response":'
                             '{"incomplete_details":{"reason":"' + reason + '"}}}\n\n').encode())
                parser.finish()
                self.assertFalse(parser.error)

    def test_runtime_save_uses_timestamps_and_polling_never_renews_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(directory)
            registry = Registry(GatewayConfig(), [station().cfg])
            registry.bind_statistics(log)
            st = registry.get('a')
            now = time.time()
            with patch('app.state.time.time', return_value=now):
                st.record_success(60, model='m', path='/responses')
            revision = log.store.revision
            with patch('app.state.time.time', return_value=now + 12 * 3600):
                registry.snapshot()
            self.assertEqual(log.store.revision, revision)
            saved = log._runtime['a']['health_samples'][0]
            self.assertEqual(saved['at'], now)
            with patch('app.state.time.time', return_value=now + 25 * 3600):
                fresh = Registry(GatewayConfig(), [station().cfg])
                fresh.bind_statistics(log)
                self.assertIsNone(fresh.get('a').health_score)


class StreamingHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(data_dir=self.directory.name, max_rounds=1, max_attempts=1)
        self.registry = Registry(self.gateway, [station().cfg])
        self.st = self.registry.get('a')
        self.proxy = OpenAIProxy(self.registry, self.gateway, EventLog(self.directory.name))

    async def asyncTearDown(self):
        await self.registry.close_clients()
        self.directory.cleanup()

    async def stream(self, ending):
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                if isinstance(ending, Exception):
                    raise ending
                yield ending
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, stream=Stream())))
        return (await self.proxy._forward_stream('/chat/completions', {'model': 'm'}, 'm'))[0]

    async def test_only_completed_stream_success_updates_health_once(self):
        response = await self.stream(b'data: [DONE]\n\n')
        self.assertIsNone(self.st.health_score)
        self.assertEqual(self.st.calls, 1)
        _ = [chunk async for chunk in response.body_iterator]
        self.assertEqual(self.st.calls, 1)
        self.assertEqual(self.st.failures, 0)
        self.assertEqual(self.st.score_breakdown['business_samples'], 1)
        self.assertGreater(self.st.health_score, 85)

    async def test_stream_error_is_failure_and_cancellation_is_neutral(self):
        response = await self.stream(httpx.ReadError('lost'))
        _ = [chunk async for chunk in response.body_iterator]
        self.assertEqual((self.st.calls, self.st.failures), (1, 1))
        self.assertEqual(self.st.score_breakdown['success_rate'], 0)
        await self.st.client.aclose()
        before = len(self.st.health_samples)
        response = await self.stream(b'data: [DONE]\n\n')
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
        self.assertEqual(len(self.st.health_samples), before)
        self.assertEqual((self.st.calls, self.st.failures), (2, 1))

    async def test_midstream_quota_keeps_accounting_but_not_health_failure(self):
        response = await self.stream(b'data: {"error":{"message":"insufficient_quota"}}\n\n')
        _ = [chunk async for chunk in response.body_iterator]
        self.assertTrue(self.st.quota_exhausted)
        self.assertEqual(self.st.failures, 1)
        self.assertIsNone(self.st.health_score)

    async def test_http_200_error_body_is_not_scored_as_success(self):
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={'error': {'message': 'server overloaded'}})))
        response, _ = await self.proxy._forward_json('/chat/completions', {'model': 'm'}, 'm')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.st.calls, 1)
        self.assertLess(self.st.health_score, 70)
