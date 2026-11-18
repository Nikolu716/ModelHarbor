"""Terminal SSE frames must finish recovery even when the peer closes at once."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest

import httpx

from app.config import GatewayConfig, StationConfig
from app.proxy import OpenAIProxy
from app.state import Registry, STATUS_DOWN
from app.stats import EventLog


CONTENT = b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
DONE = (b'data: {"type":"response.completed","response":{"status":"completed",'
        b'"output":[],"usage":{"input_tokens":7,"output_tokens":3,"total_tokens":10}}}\n\n')


class HeldOpenStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.read_past_terminal = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        self.read_past_terminal = True
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class StreamCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(data_dir=self.directory.name, max_rounds=1, max_attempts=1)
        self.registry = Registry(self.gateway, [StationConfig(
            name='a', base_url='https://a.invalid/v1', api_key='test', models=['m'], max_concurrency=1)])
        self.st = self.registry.get('a')
        self.events = EventLog(self.directory.name)
        self.registry.bind_statistics(self.events)
        self.proxy = OpenAIProxy(self.registry, self.gateway, self.events)

    async def asyncTearDown(self):
        await self.registry.close_clients()
        await self.registry.chatgpt.close()
        self.directory.cleanup()

    async def response(self, chunks, path='/responses'):
        if self.st.client:
            await self.st.client.aclose()
        self.st.cfg.provider = 'anthropic' if path == '/messages' else 'openai'
        self.st.status = STATUS_DOWN
        self.st.consecutive_failures = 3
        self.st.down_since = time.time() - self.gateway.cool_down - 1
        self.st.recovery_attempt_at = 0
        upstream = HeldOpenStream(chunks)
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, stream=upstream)))
        response, _ = await self.proxy._forward_stream(path, {'model': 'm', 'stream': True}, 'm')
        self.assertEqual(response.status_code, 200)
        return response, upstream

    def assert_released(self, upstream):
        self.assertTrue(upstream.closed)
        self.assertFalse(self.st.recovery_inflight)
        self.assertEqual(self.st.semaphore._value, 1)
        self.assertEqual(self.registry.route_activity_snapshot()['inflight'], 0)

    def assert_success(self, upstream):
        self.assert_released(upstream)
        self.assertEqual(self.st.status, 'up')
        self.assertEqual(self.st.consecutive_failures, 0)
        self.assertEqual(self.events.recent(1)[0]['outcome'], 'success')

    async def test_terminal_frame_finishes_without_transport_eof(self):
        for chunks, path in (
            ([CONTENT, DONE[:36], DONE[36:]], '/responses'),
            ([CONTENT + DONE], '/responses'),
            ([b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
              b'data: [DONE]\n\n'], '/chat/completions'),
            ([b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n',
              b'data: {"type":"message_stop"}\n\n'], '/messages'),
        ):
            with self.subTest(path=path, chunks=len(chunks)):
                response, upstream = await self.response(chunks, path)
                async def consume():
                    return b''.join([chunk async for chunk in response.body_iterator])
                payload = await asyncio.wait_for(consume(), .5)
                self.assertEqual(payload, b''.join(chunks))
                self.assertFalse(upstream.read_past_terminal)
                self.assert_success(upstream)
        self.assertEqual(self.st.calls, 4)
        self.assertEqual(self.st.failures, 0)

    async def test_client_closes_immediately_after_fragmented_completed_frame(self):
        response, upstream = await self.response([CONTENT, DONE[:40], DONE[40:]])
        for _ in range(3):
            await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
        self.assert_success(upstream)
        self.assertEqual(self.events.recent(1)[0]['tokens'], 10)
        self.assertEqual((self.st.calls, self.st.failures), (1, 0))
        self.assertEqual(self.st.score_breakdown['business_samples'], 1)

    async def test_asgi_disconnect_or_send_error_after_terminal_preserves_success(self):
        for version in ('2.3', '2.4'):
            for prefetched in (False, True):
                with self.subTest(version=version, prefetched=prefetched):
                    response, upstream = await self.response([CONTENT + DONE] if prefetched else [CONTENT, DONE])
                    terminal_sent = asyncio.Event()
                    async def send(message):
                        if DONE in message.get('body', b''):
                            terminal_sent.set()
                            if version == '2.4':
                                raise OSError('client closed after terminal frame')
                            await asyncio.Event().wait()
                    async def receive():
                        await terminal_sent.wait()
                        return {'type': 'http.disconnect'}
                    try:
                        await asyncio.wait_for(response(
                            {'type': 'http', 'asgi': {'spec_version': version}}, receive, send), 1)
                    except Exception as exc:
                        if version != '2.4' or isinstance(exc, TimeoutError):
                            raise
                    self.assert_success(upstream)
                    self.assertEqual(self.events.recent(1)[0]['tokens'], 10)
        self.assertEqual(self.st.calls, 4)
        self.assertEqual(self.st.score_breakdown['business_samples'], 4)
        self.assertEqual(self.events.daily_stats()['total_failures'], 0)

    async def test_usage_without_terminal_does_not_recover_cancelled_stream(self):
        usage = b'data: {"usage":{"total_tokens":10}}\n\n'
        response, upstream = await self.response([CONTENT + usage, DONE])
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
        self.assert_released(upstream)
        self.assertEqual(self.st.status, STATUS_DOWN)
        self.assertEqual(self.st.failures, 0)
        self.assertIsNone(self.st.health_score)
        self.assertEqual(self.events.recent(1)[0]['outcome'], 'interrupted')
        self.assertEqual(self.events.recent(1)[0]['tokens'], 10)

    async def test_concurrent_requests_reuse_one_slot_after_terminal_disconnect(self):
        self.gateway.max_retry_seconds = 5
        self.gateway.round_backoff = .01
        streams = []
        async def upstream(req):
            await asyncio.sleep(.01)
            stream = HeldOpenStream([CONTENT, DONE[:30], DONE[30:]])
            streams.append(stream)
            return httpx.Response(200, stream=stream)
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        class Request:
            async def json(inner):
                return {'model': 'm', 'input': 'fixture', 'stream': True}
            async def is_disconnected(inner):
                return False
        async def run():
            response = await self.proxy._dispatch(Request(), '/responses')
            self.assertEqual(response.status_code, 200)
            for _ in range(3):
                await anext(response.body_iterator)
            await response.body_iterator.aclose()
        await asyncio.wait_for(asyncio.gather(*(run() for _ in range(16))), 6)
        self.assertEqual(len(streams), 16)
        self.assertTrue(all(s.closed and not s.read_past_terminal for s in streams))
        self.assertEqual((self.st.calls, self.st.failures), (16, 0))
        self.assertEqual(self.events.daily_stats()['total_failures'], 0)
        self.assertTrue(all(e['outcome'] == 'success' and e['tokens'] == 10
                            for e in self.events.recent(20)))
        self.assert_released(streams[-1])

    async def test_failed_or_unexpected_incomplete_frame_cannot_recover(self):
        for kind in ('response.failed', 'response.incomplete'):
            with self.subTest(kind=kind):
                payload = {'type': kind, 'response': {'incomplete_details': {'reason': 'server_error'}}}
                end = b'data: ' + json.dumps(payload).encode() + b'\n\ndata: [DONE]\n\n'
                response, upstream = await self.response([CONTENT, end])
                await response.body_iterator.__anext__()
                await response.body_iterator.__anext__()
                await response.body_iterator.aclose()
                self.assert_released(upstream)
                self.assertEqual(self.st.status, STATUS_DOWN)
                self.assertEqual(self.events.recent(1)[0]['outcome'], 'interrupted')
        self.assertEqual(self.st.failures, 2)


if __name__ == '__main__':
    unittest.main()
