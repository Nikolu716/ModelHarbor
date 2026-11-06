"""Scheduling regressions; all upstream traffic stays in HTTPX MockTransport."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from app.config import GatewayConfig, StationConfig
from app.proxy import OpenAIProxy, _RequestBody
from app.state import Registry, STATUS_DOWN
from app.stats import EventLog
from app.wire import UnsupportedWireRequest, prepare_request


class Request:
    def __init__(self, stream=False):
        self.stream = stream

    async def json(self):
        return {"model": "m", "input": "hello", "stream": self.stream}

    async def is_disconnected(self):
        return False


class BodyStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if index:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


CONTENT = b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
DONE = b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
METADATA = b'data: {"type":"response.created","response":{"id":"r"}}\n\n'


class RetrySchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(data_dir=self.temp.name, max_attempts=2, max_rounds=1,
                                     retry_backoff=0, max_retry_seconds=.6,
                                     first_byte_timeout=2, first_token_timeout=2)
        self.registry = Registry(self.gateway, [StationConfig(
            name=name, base_url=f"https://{name}.invalid/v1", api_key="test-key",
            models=["m"], max_concurrency=2) for name in ("a", "b", "c")])
        self.stations = list(self.registry.states.values())
        self.events = EventLog(self.temp.name)
        self.proxy = OpenAIProxy(self.registry, self.gateway, self.events)
        self.calls = []
        self.streams = []
        self.responses = {}

        async def upstream(req):
            name = req.url.host.split('.')[0]
            self.calls.append(name)
            if name in self.responses:
                return await self.responses[name](req)
            if json.loads(req.content).get('stream'):
                body = BodyStream([CONTENT, DONE])
                self.streams.append(body)
                return httpx.Response(200, stream=body)
            return httpx.Response(200, json={"id": name, "output": [], "model": "m"})

        for st in self.stations:
            st.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.pick = patch.object(self.proxy.router, 'pick', return_value=self.stations[:2])
        self.pick.start()

    async def asyncTearDown(self):
        self.pick.stop()
        await self.registry.close_clients()
        await self.registry.chatgpt.close()
        self.temp.cleanup()

    async def forward(self, stream):
        body = _RequestBody({'model': 'm', 'input': 'hello', 'stream': stream})
        method = self.proxy._forward_stream if stream else self.proxy._forward_json
        result, transient = await method('/responses', body, 'm')
        if result.status_code == 200 and stream:
            async for _ in result.body_iterator:
                pass
        return result, transient

    def assert_released(self):
        self.assertEqual(self.registry.route_activity_snapshot()['inflight'], 0)
        for st in self.stations:
            self.assertEqual(self.registry.ensure_semaphore(st)._value, st.cfg.max_concurrency)
            self.assertFalse(st.recovery_inflight)
        self.assertTrue(all(stream.closed for stream in self.streams))

    async def test_concurrency_skip_does_not_spend_one_attempt_or_count_as_switch(self):
        self.gateway.max_attempts = 1
        a = self.stations[0]
        sem = self.registry.ensure_semaphore(a)
        await sem.acquire()
        await sem.acquire()
        try:
            for stream in (False, True):
                with self.subTest(stream=stream):
                    self.calls.clear()
                    result, _ = await self.forward(stream)
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(self.calls, ['b'])
                    self.assertEqual(self.events.recent(1)[0]['attempts'], 1)
                    self.assertEqual(self.stations[1].switch_in, 0)
                    self.assertEqual(a.failures, 0)
        finally:
            sem.release()
            sem.release()

    async def test_local_protocol_skip_does_not_spend_one_attempt(self):
        self.gateway.max_attempts = 1

        def prepare(st, *args, **kwargs):
            if st.cfg.name == 'a':
                raise UnsupportedWireRequest('unsupported test input')
            return prepare_request(st, *args, **kwargs)

        with patch('app.proxy.prepare_request', side_effect=prepare):
            for stream in (False, True):
                self.calls.clear()
                result, _ = await self.forward(stream)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(self.calls, ['b'])
                self.assertEqual(self.stations[0].failures, 0)

    async def test_recovery_skip_reports_recovery_without_releasing_another_permit(self):
        a = self.stations[0]
        a.status = STATUS_DOWN
        a.down_since = time.time() - 1000
        permit = await self.proxy.router.acquire(a)
        self.assertIsNotNone(permit)
        self.proxy.router.pick.return_value = [a]
        try:
            for stream in (False, True):
                result, transient = await self.forward(stream)
                self.assertEqual(result.status_code, 502)
                self.assertIn('恢复验证', result.body.decode())
                self.assertNotIn('并发已满', result.body.decode())
                self.assertEqual(getattr(result, '_zg_attempts', -1), 0)
                self.assertTrue(transient)
                self.assertTrue(a.recovery_inflight)
                self.assertEqual(a.semaphore._value, 1)
                self.assertEqual(self.calls, [])
        finally:
            self.proxy.router.release(a, permit)

    async def test_real_upstream_failure_still_spends_attempt_cap(self):
        self.gateway.max_attempts = 1

        async def fail(req):
            return httpx.Response(503, json={'error': {'message': 'temporarily unavailable'}})

        self.responses['a'] = fail
        for stream in (False, True):
            self.calls.clear()
            result, _ = await self.forward(stream)
            self.assertEqual(result.status_code, 502)
            self.assertEqual(self.calls, ['a'])
            self.assertEqual(result._zg_attempts, 1)

    async def test_already_attempted_subscription_does_not_hide_fallback(self):
        self.gateway.max_attempts = 1
        a = self.stations[0]
        a.cfg.provider = 'chatgpt'
        a.cfg.chatgpt_account = 'test-binding'
        for streaming in (False, True):
            self.calls.clear()
            body = _RequestBody({'model': 'm', 'input': 'hello', 'stream': streaming})
            body.subscription_attempts.add('test-binding')
            method = self.proxy._forward_stream if streaming else self.proxy._forward_json
            result, _ = await method('/responses', body, 'm')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.calls, ['b'])
            if streaming:
                async for _ in result.body_iterator:
                    pass

    async def test_preparation_timeout_has_no_upstream_attempt_or_health_penalty(self):
        from app.chatgpt import authorized_headers

        async def authorize(registry, cfg, headers):
            if cfg.name == 'a':
                await asyncio.sleep(2)
            return await authorized_headers(registry, cfg, headers)

        for streaming in (False, True):
            self.calls.clear()
            with patch('app.chatgpt.authorized_headers', side_effect=authorize):
                result = await self.proxy._dispatch(Request(streaming), '/responses')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.calls, ['b'])
            if streaming:
                async for _ in result.body_iterator:
                    pass
            self.assertEqual(self.events.recent(1)[0]['attempts'], 1)
            self.assertEqual(self.stations[0].failures, 0)
            self.assert_released()

    async def test_state_changes_during_preparation_are_rechecked_before_send(self):
        from app.chatgpt import authorized_headers

        async def authorize(registry, cfg, headers):
            if cfg.name == 'a':
                self.stations[0].enabled = False
            return await authorized_headers(registry, cfg, headers)

        self.gateway.max_attempts = 1
        for streaming in (False, True):
            self.calls.clear()
            self.stations[0].enabled = True
            with patch('app.chatgpt.authorized_headers', side_effect=authorize):
                result, _ = await self.forward(streaming)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.calls, ['b'])
            self.assertEqual(self.events.recent(1)[0]['attempts'], 1)

    async def test_recovery_skip_still_allows_fallback_with_attempt_limit_one(self):
        self.gateway.max_attempts = 1
        a = self.stations[0]
        a.status = STATUS_DOWN
        a.down_since = time.time() - 1000
        await a.generation_probe_lock.acquire()
        try:
            for streaming in (False, True):
                self.calls.clear()
                result, _ = await self.forward(streaming)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(self.calls, ['b'])
                self.assertTrue(a.generation_probe_lock.locked())
                self.assertEqual(a.failures, 0)
        finally:
            a.generation_probe_lock.release()

    async def test_slow_headers_json_body_metadata_and_error_body_leave_fallback_time(self):
        for stage, streaming in [('headers', False), ('json', False),
                                 ('metadata', True), ('error', True)]:
            with self.subTest(stage=stage):
                self.calls.clear()

                async def slow(req):
                    if stage == 'headers':
                        await asyncio.sleep(2)
                    body = BodyStream([METADATA if streaming else b'{', b'never'], delay=2)
                    self.streams.append(body)
                    return httpx.Response(503 if stage == 'error' else 200, stream=body)

                self.responses['a'] = slow
                started = time.monotonic()
                result = await self.proxy._dispatch(Request(streaming), '/responses')
                self.assertEqual(result.status_code, 200, getattr(result, 'body', b''))
                self.assertEqual(self.calls, ['a', 'b'])
                self.assertLess(time.monotonic() - started, self.gateway.max_retry_seconds)
                if streaming:
                    async for _ in result.body_iterator:
                        pass
                self.assert_released()
                # A local scheduling cut-off is not proof of an upstream fault.
                self.assertEqual(self.stations[0].failures, 0)

    async def test_single_usable_channel_keeps_whole_budget(self):
        for skip in ('busy', 'unsupported', 'attempt_cap'):
            with self.subTest(skip=skip):
                self.calls.clear()
                self.gateway.max_attempts = 1 if skip == 'attempt_cap' else 2
                b = self.stations[1]
                sem = self.registry.ensure_semaphore(b)
                if skip == 'busy':
                    await sem.acquire()
                    await sem.acquire()

                async def slow(req):
                    await asyncio.sleep(.4)
                    return httpx.Response(200, json={'output': []})

                def prepare(st, *args, **kwargs):
                    if st is b and skip == 'unsupported':
                        raise UnsupportedWireRequest('unsupported test input')
                    return prepare_request(st, *args, **kwargs)

                self.responses['a'] = slow
                try:
                    with patch('app.proxy.prepare_request', side_effect=prepare):
                        result = await self.proxy._dispatch(Request(), '/responses')
                    self.assertEqual(result.status_code, 200, result.body)
                    self.assertEqual(self.calls, ['a'])
                finally:
                    if skip == 'busy':
                        sem.release()
                        sem.release()

    async def test_three_channels_and_backoff_share_total_deadline(self):
        self.gateway.max_attempts = 3
        self.gateway.retry_backoff = .04
        self.proxy.router.pick.return_value = self.stations

        async def slow(req):
            await asyncio.sleep(2)
            return httpx.Response(200, json={})

        self.responses.update(a=slow, b=slow)
        result = await self.proxy._dispatch(Request(), '/responses')
        self.assertEqual(result.status_code, 200, result.body)
        self.assertEqual(self.calls, ['a', 'b', 'c'])
        self.assertEqual(self.events.recent(1)[0]['attempts'], 3)
        self.assert_released()

    async def test_global_timeout_counts_sent_attempts_and_releases_recovery(self):
        for streaming in (False, True):
            self.calls.clear()
            a = self.stations[0]
            a.status = STATUS_DOWN
            a.down_since = time.time() - 1000
            a.recovery_attempt_at = 0

            async def slow(req):
                await asyncio.sleep(2)
                return httpx.Response(200, json={})

            self.responses.update(a=slow, b=slow)
            result = await self.proxy._dispatch(Request(streaming), '/responses')
            self.assertEqual(result.status_code, 502)
            self.assertIn('总时限', result.body.decode())
            self.assertEqual(self.calls, ['a', 'b'])
            self.assertEqual(self.events.recent(1)[0]['attempts'], 2)
            self.assertEqual(a.status, STATUS_DOWN)
            self.assertEqual(a.failures, 0)
            self.assert_released()

    async def test_configured_upstream_timeout_still_counts_health_failure(self):
        self.gateway.first_byte_timeout = .03

        async def slow(req):
            await asyncio.sleep(2)
            return httpx.Response(200, json={})

        self.responses['a'] = slow
        for streaming in (False, True):
            before = self.stations[0].failures
            result = await self.proxy._dispatch(Request(streaming), '/responses')
            self.assertEqual(result.status_code, 200)
            if streaming:
                async for _ in result.body_iterator:
                    pass
            self.assertEqual(self.stations[0].failures, before + 1)
            self.assert_released()

    async def test_unlimited_retry_time_does_not_add_a_station_time_cap(self):
        self.gateway.max_retry_seconds = 0

        async def slow(req):
            await asyncio.sleep(.4)
            return httpx.Response(200, json={'output': []})

        self.responses['a'] = slow
        result = await self.proxy._dispatch(Request(), '/responses')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.calls, ['a'])
        self.assert_released()

    async def test_fallback_budget_uses_real_client_session_and_distinct_bindings(self):
        body = _RequestBody({'model': 'm', 'input': 'hello'})
        body.retry_budget.deadline = time.monotonic() + 1
        b = self.stations[1]
        b.cfg.provider = 'opencode'
        b.cfg.opencode_plan = 'go'
        b.model_api_paths = {'m': ['/responses']}
        self.assertIsNone(self.proxy._attempt_deadline(body, '/responses', [b], 0))
        deadline = self.proxy._attempt_deadline(
            body, '/responses', [b], 0, client_headers={'x-session-id': 'test-conversation'})
        self.assertIsNotNone(deadline)
        self.assertLess(deadline, body.retry_budget.deadline)
        self.assertEqual(self.calls, [])

        b.cfg.provider = 'chatgpt'
        b.cfg.chatgpt_account = 'same-binding'
        self.assertIsNone(self.proxy._attempt_deadline(
            body, '/responses', [b], 0, current_subscription='same-binding'))

    async def test_committed_stream_can_outlive_retry_budget(self):
        async def committed(req):
            body = BodyStream([CONTENT, DONE], delay=.7)
            self.streams.append(body)
            return httpx.Response(200, stream=body)

        self.responses['a'] = committed
        result = await self.proxy._dispatch(Request(True), '/responses')
        self.assertEqual(result.status_code, 200)
        async for _ in result.body_iterator:
            pass
        self.assertEqual(self.calls, ['a'])
        self.assert_released()


if __name__ == '__main__':
    unittest.main()
