"""Encrypted Responses history errors must never trigger a lossy retry."""
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.config import GatewayConfig, StationConfig
from app.errors import classify_error
from app.proxy import OpenAIProxy, _RouteChoice
from app.state import Registry
from app.stats import EventLog


CODE = 'invalid_encrypted_content'
MESSAGE = 'Encrypted function output content could not be decrypted or decoded.'
HISTORY = [{'type': 'reasoning', 'encrypted_content': 'reason'},
           {'type': 'function_call', 'encrypted_function_args': 'args'},
           {'type': 'function_call_output', 'output': [{'type': 'encrypted_content', 'encrypted_content': 'output'}]},
           {'type': 'compaction', 'encrypted_content': 'compact'}]


def frame(obj):
    return b'data: ' + json.dumps(obj).encode() + b'\n\n'


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class EncryptedHistoryTests(unittest.IsolatedAsyncioTestCase):
    def test_generic_encryption_errors_keep_normal_classification(self):
        for code, message in [('encrypted_service_error', 'TLS key could not be decrypted'),
                              ('server_error', 'failed to decrypt'), ('server_error', 'invalid_encrypted_content')]:
            err = classify_error('a', 503, json.dumps({'error': {'code': code, 'message': message}}))
            self.assertTrue(err.retryable)
            self.assertNotEqual(err.code, CODE)

    async def exercise(self, status, chunks, *, stream=False, subscription=False, committed=False):
        with tempfile.TemporaryDirectory() as tmp:
            gateway = GatewayConfig(data_dir=tmp, max_attempts=2, max_rounds=2, retry_backoff=0)
            registry = Registry(gateway, [StationConfig(name=n, base_url=f'https://{n}.invalid/v1',
                                                        models=['m'], api_key='test') for n in ('a', 'b')])
            try:
                events = EventLog(tmp)
                registry.bind_statistics(events)
                proxy = OpenAIProxy(registry, gateway, events)
                stations = list(registry.states.values())
                if subscription:
                    stations[0].cfg.provider = 'chatgpt'
                proxy._route_choices = Mock(return_value=[_RouteChoice(st, 'm') for st in stations])
                registry.note_origin_failure = Mock()
                sent = []

                def upstream(req):
                    sent.append(json.loads(req.content))
                    return httpx.Response(status, stream=Chunks(chunks))

                for st in stations:
                    st.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
                request = Mock(json=AsyncMock(return_value={'model': 'm', 'stream': stream, 'input': HISTORY,
                                   'reasoning': {'effort': 'high'}, 'include': ['reasoning.encrypted_content']}),
                               is_disconnected=AsyncMock(return_value=False), client=None, headers={})
                with patch('app.chatgpt.authorized_headers', new=AsyncMock(return_value={})):
                    response = await proxy.responses(request)
                if committed:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(b''.join([c async for c in response.body_iterator]), b''.join(chunks))
                    self.assertEqual(events.recent(1)[0]['outcome'], 'interrupted')
                else:
                    self.assertEqual(response.status_code, 400)
                    error = json.loads(response.body)['error']
                    self.assertEqual((error['code'], error['type']), (CODE, 'invalid_request_error'))
                    self.assertIn('start a new conversation', error['message'])
                    self.assertEqual(response._zg_attempts, 1)
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]['input'], HISTORY)
                self.assertEqual(sent[0]['include'], ['reasoning.encrypted_content'])
                registry.note_origin_failure.assert_not_called()
                self.assertEqual(stations[0].failures, 0)
                self.assertFalse(stations[0].quota_exhausted)
                self.assertEqual(registry.route_activity_snapshot()['inflight'], 0)
            finally:
                await registry.close_clients()
                await registry.chatgpt.close()

    async def test_http_json(self):
        for stream in (False, True):
            for status in (200, 400, 429):
                with self.subTest(stream=stream, status=status):
                    await self.exercise(status, [json.dumps({'code': CODE, 'message': 'unsupported reasoning'}).encode()], stream=stream)

    async def test_buffered_subscription(self):
        for payload in [json.dumps({'error': {'code': CODE}}).encode(),
                        frame({'type': 'response.failed', 'response': {'error': {'message': MESSAGE}}})]:
            await self.exercise(200, [payload], subscription=True)

    async def test_early_and_committed_sse(self):
        for error in [frame({'type': 'error', 'code': CODE, 'message': 'unsupported reasoning'}),
                      frame({'type': 'response.failed', 'response': {'error': {'message': MESSAGE}}})]:
            await self.exercise(200, [b': keepalive\n\n', error[:20], error[20:]], stream=True)
            await self.exercise(200, [frame({'type': 'response.output_text.delta', 'delta': 'hello'}),
                                      error[:20], error[20:]], stream=True, committed=True)
