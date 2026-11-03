"""Model substitution, account isolation and wire compatibility without paid I/O."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from app.config import AppConfig, GatewayConfig, StationConfig
from app.mapping_wire import ChatStreamToResponsesTranslator, responses_to_chat, chat_to_responses
from app.model_mappings import ModelMappings, validate_rules
from app.proxy import OpenAIProxy
from app.state import Registry
from app.stats import EventLog
from app.wire import UnsupportedWireRequest
from gateway import build_app


def rule(**changes):
    return {'id': 'substitute', 'name': 'GPT via DeepSeek', 'enabled': True,
            'source_model': 'gpt-6-sol', 'source_ips': [],
            'targets': [{'station': 'Go', 'model': 'deepseek-v4.1-flash'}], **changes}


def sse(obj):
    return ('data: ' + json.dumps(obj, ensure_ascii=False) + '\r\n\r\n').encode()


def events(content):
    return [json.loads(line[6:]) for line in content.decode().splitlines() if line.startswith('data: {')]


class Request:
    def __init__(self, body, ip='10.1.1.1', headers=None):
        self.body = body
        self.client = SimpleNamespace(host=ip)
        self.headers = {'session-id': 'mapping-fixture-session', **(headers or {})}

    async def json(self):
        return self.body

    async def is_disconnected(self):
        return False


class Fragments(httpx.AsyncByteStream):
    closed = False

    def __init__(self, payload):
        self.payload = payload

    async def __aiter__(self):
        for i in range(0, len(self.payload), 11):
            yield self.payload[i:i + 11]

    async def aclose(self):
        self.closed = True


class MappingStoreTests(unittest.TestCase):
    def test_ip_precedence_and_snapshot(self):
        store = ModelMappings()
        specific = rule(id='ip', source_ips=['::ffff:10.1.1.1'])
        store.save([rule(), specific], None)
        self.assertEqual(store.match('gpt-6-sol', '10.1.1.1', '/responses')['id'], 'ip')
        self.assertEqual(store.match('gpt-6-sol', '10.2.2.2', '/responses')['id'], 'substitute')
        snapshot = store.match('gpt-6-sol', None, '/responses')
        snapshot['targets'][0]['model'] = 'changed'
        self.assertEqual(store.rules[0]['targets'][0]['model'], 'deepseek-v4.1-flash')
        self.assertIsNone(store.match('gpt-6-sol-extra', None, '/responses'))
        self.assertIsNone(store.match('gpt-6-sol', None, '/embeddings'))

    def test_validation_conflicts_and_limits(self):
        invalid = [[rule(), rule(id='another')], [rule(source_ips=['10.1.1.1/24'])],
                   [rule(targets=[])], [rule(source_model='gpt*')], [rule(enabled='true')],
                   [rule(targets=rule()['targets'] * 2)], [rule(source_ips='10.1.1.1')],
                   [rule(id=str(i)) for i in range(65)],
                   [rule(id='ip1', source_ips=['10.1.1.1']), rule(id='ip2', source_ips=['::ffff:10.1.1.1'])]]
        for values in invalid:
            with self.subTest(values=values[:2]), self.assertRaises(ValueError):
                validate_rules(values)

    def test_durable_save_failure_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ModelMappings(directory)
            store.save([rule()], None)
            self.assertEqual(ModelMappings(directory).rules, store.rules)
            with patch.object(Path, 'replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    store.save([], None)
            self.assertEqual(store.rules, [rule()])
            self.assertEqual(ModelMappings(directory).rules, [rule()])
            store.path.write_text('{broken', encoding='utf-8')
            broken = ModelMappings(directory)
            with self.assertRaises(ValueError):
                broken.match('any-model', '10.1.1.1', '/responses')
            broken.save([], None)
            self.assertIsNone(broken.match('any-model', None, '/responses'))


class WireMappingTests(unittest.TestCase):
    def test_function_round_trip_and_incomplete_result(self):
        result = chat_to_responses({'choices': [{'message': {'tool_calls': [
            {'id': 'c1', 'type': 'function', 'function': {'name': 'inspect', 'arguments': '{"path":"src"}'}}]},
            'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}}, 'actual')
        inputs = result['output'] + [{'type': 'function_call_output', 'call_id': 'c1', 'output': 'file list'}]
        converted = responses_to_chat({'model': 'actual', 'input': inputs,
            'tools': [{'type': 'function', 'name': 'inspect', 'parameters': {'type': 'object'}, 'strict': True}],
            'tool_choice': {'type': 'function', 'name': 'inspect'}, 'reasoning': {'effort': 'low'}})
        self.assertEqual(converted['messages'][0]['tool_calls'][0]['id'], 'c1')
        self.assertEqual(converted['messages'][1], {'role': 'tool', 'tool_call_id': 'c1', 'content': 'file list'})
        self.assertTrue(converted['tools'][0]['function']['strict'])
        self.assertEqual(converted['reasoning_effort'], 'low')
        self.assertEqual(result['model'], 'actual')
        limited = chat_to_responses({'choices': [{'message': {'content': 'partial'}, 'finish_reason': 'length'}]}, 'actual')
        self.assertEqual(limited['status'], 'incomplete')
        self.assertNotIn('usage', limited)

    def test_unsupported_semantics_are_rejected(self):
        for extra in [{'previous_response_id': 'r1'}, {'conversation': 'c'}, {'background': True},
                      {'tools': [{'type': 'web_search'}]}, {'tools': [{'type': 'custom', 'name': 'shell'}]},
                      {'input': [{'type': 'reasoning', 'encrypted_content': 'opaque'}]},
                      {'input': [{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'private'}]}]},
                      {'tool_choice': {'type': 'allowed_tools'}}, {'reasoning': {'summary': 'auto'}}]:
            with self.subTest(extra=extra), self.assertRaises(UnsupportedWireRequest):
                responses_to_chat({'input': 'text', **extra})

    def test_fragmented_stream_lifecycle_tools_usage_and_unicode(self):
        payload = sse({'choices': [{'delta': {'content': '中文'}}]})
        payload += sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function', 'function': {'name': 'inspect', 'arguments': '{"p":'}}]}}]})
        payload += sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"src"}'}}]}, 'finish_reason': 'tool_calls'}]})
        payload += sse({'choices': [], 'usage': {'prompt_tokens': 4, 'completion_tokens': 6, 'total_tokens': 10}}) + b'data: [DONE]\n\n'
        translator = ChatStreamToResponsesTranslator('actual')
        result = b''.join(translator.feed(bytes([byte])) for byte in payload) + translator.finish()
        parsed = events(result)
        self.assertEqual([e['sequence_number'] for e in parsed], list(range(len(parsed))))
        done = parsed[-1]
        self.assertEqual(done['type'], 'response.completed')
        self.assertEqual(done['response']['output'][0]['content'][0]['text'], '中文')
        self.assertEqual(done['response']['output'][1]['arguments'], '{"p":"src"}')
        self.assertEqual(done['response']['usage']['total_tokens'], 10)
        self.assertEqual(sum(e['type'] == 'response.output_item.added' for e in parsed), 2)

    def test_truncation_and_upstream_error_never_claim_completion(self):
        for end in (b'', sse({'error': {'message': 'failed'}})):
            translator = ChatStreamToResponsesTranslator('actual')
            result = translator.feed(sse({'choices': [{'delta': {'content': 'partial'}}]}) + end) + translator.finish()
            self.assertTrue(translator.error)
            self.assertEqual(events(result)[-1]['type'], 'response.failed')
            self.assertNotIn(b'response.completed', result)


class MappingProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(data_dir=self.tmp.name, max_rounds=1, retry_backoff=0, sse_keepalive_interval=0)
        configs = [StationConfig.from_dict('Go', {'provider': 'opencode', 'opencode_plan': 'go', 'base_url': 'https://opencode.ai', 'api_key': 'fixture'}),
                   StationConfig(name='Backup', base_url='https://backup.invalid/v1', api_key='fixture', models=['backup-model'], wire_api='chat_completions'),
                   StationConfig(name='Outside', base_url='https://outside.invalid/v1', api_key='fixture', models=['gpt-6-sol'])]
        self.registry = Registry(self.gateway, configs)
        self.go = self.registry.get('Go')
        self.go.model_api_paths = {'deepseek-v4.1-flash': ['/chat/completions']}
        self.go.probed_models = {'deepseek-v4.1-flash'}
        self.registry.model_mappings.save([rule()], self.registry)
        self.log = EventLog(self.tmp.name)
        self.registry.bind_statistics(self.log)
        self.proxy = OpenAIProxy(self.registry, self.gateway, self.log)
        self.sent, self.streams = [], []
        self.status = 200
        self.tail = b'data: [DONE]\n\n'
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.block = False

        async def upstream(request):
            body = json.loads(request.content)
            self.sent.append((request, body))
            if self.block:
                self.entered.set()
                await self.release.wait()
            if self.status != 200 and request.url.host == 'opencode.ai':
                return httpx.Response(self.status, json={'error': {'message': 'temporarily unavailable'}})
            if body.get('stream'):
                stream = Fragments(sse({'model': body['model'], 'choices': [{'delta': {'content': 'answer'}}]}) + self.tail)
                self.streams.append(stream)
                return httpx.Response(200, stream=stream, headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, json={'model': body['model'], 'choices': [{'message': {'content': 'answer'}, 'finish_reason': 'stop'}],
                                            'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})
        for station in self.registry.states.values():
            station.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))

    async def asyncTearDown(self):
        self.release.set()
        await self.registry.close_clients()
        self.tmp.cleanup()

    async def dispatch(self, **changes):
        return await self.proxy._dispatch(Request({'model': 'gpt-6-sol', 'input': 'text', **changes}), '/responses')

    async def test_json_mapping_records_actual_model_and_persists_both_names(self):
        result = await self.dispatch()
        self.assertEqual(result.status_code, 200, result.body)
        self.assertEqual(json.loads(result.body)['model'], 'deepseek-v4.1-flash')
        request, body = self.sent[0]
        self.assertEqual(request.url.path, '/inference/go/openai/v1/chat/completions')
        self.assertEqual(request.headers['session-id'], 'mapping-fixture-session')
        self.assertEqual(body['model'], 'deepseek-v4.1-flash')
        self.assertEqual(body['messages'][0]['content'], 'text')
        event = self.log.events[-1]
        self.assertEqual((event['model'], event['requested_model'], event['mapping_id']), ('deepseek-v4.1-flash', 'gpt-6-sol', 'substitute'))
        restored = EventLog(self.tmp.name)
        restored.load()
        self.assertEqual(restored.events[-1]['requested_model'], 'gpt-6-sol')
        self.assertEqual(self.registry.route_activity_snapshot()['inflight'], 0)
        self.assertEqual(self.go.semaphore._value, self.go.cfg.max_concurrency)

    async def test_native_chat_tools_unchanged_and_unmatched_routing_unchanged(self):
        body = {'model': 'gpt-6-sol', 'messages': [{'role': 'user', 'content': 'text'}],
                'tools': [{'type': 'function', 'function': {'name': 'inspect', 'parameters': {'type': 'object'}}}]}
        result = await self.proxy._dispatch(Request(body), '/chat/completions')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.sent[0][1], {**body, 'model': 'deepseek-v4.1-flash'})
        self.registry.model_mappings.save([], self.registry)
        result = await self.dispatch()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.sent[-1][0].url.host, 'outside.invalid')

    async def test_ip_rule_does_not_trust_forwarded_header(self):
        self.registry.model_mappings.save([rule(source_ips=['10.2.2.2'])], self.registry)
        await self.proxy._dispatch(Request({'model': 'gpt-6-sol', 'input': 'text'}, headers={'x-forwarded-for': '10.2.2.2'}), '/responses')
        self.assertEqual(self.sent[0][0].url.host, 'outside.invalid')

    async def test_catalog_alias_is_scoped_to_peer_and_go_native_body_is_preserved(self):
        self.registry.model_mappings.save([rule(source_model='mapped-alias', source_ips=['10.2.2.2'])], self.registry)
        allowed = await self.proxy.list_models(Request({}, ip='10.2.2.2'))
        other = await self.proxy.list_models(Request({}, ip='10.1.1.1'))
        self.assertIn('mapped-alias', [m['id'] for m in allowed['data']])
        self.assertNotIn('mapped-alias', [m['id'] for m in other['data']])
        self.registry.model_mappings.save([rule()], self.registry)
        self.go.model_api_paths['deepseek-v4.1-flash'].append('/responses')
        body = {'model': 'gpt-6-sol', 'input': 'code', 'tools': [{'type': 'function', 'name': 'inspect', 'parameters': {'type': 'object'}}]}
        result = await self.proxy._dispatch(Request(body), '/responses')
        self.assertEqual(result.status_code, 200)
        self.assertTrue(self.sent[-1][0].url.path.endswith('/responses'))
        self.assertEqual(self.sent[-1][1], {**body, 'model': 'deepseek-v4.1-flash'})

    async def test_unconvertible_is_local_and_preserves_station_health(self):
        result = await self.dispatch(tools=[{'type': 'web_search'}])
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.go.failures, 0)

    async def test_local_incompatible_target_does_not_consume_send_budget(self):
        self.gateway.max_attempts = 1
        self.registry.model_mappings.save([rule(targets=rule()['targets'] + [{'station': 'Outside', 'model': 'gpt-6-sol'}])], self.registry)
        with patch.object(self.proxy.router, 'pick', return_value=[self.go, self.registry.get('Outside')]):
            result = await self.dispatch(tools=[{'type': 'web_search'}])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0].url.host, 'outside.invalid')

    async def test_target_only_failover_with_different_actual_models(self):
        self.registry.model_mappings.save([rule(targets=rule()['targets'] + [{'station': 'Backup', 'model': 'backup-model'}])], self.registry)
        for code in (503, 429):
            self.status = code
            self.sent.clear()
            with patch.object(self.proxy.router, 'pick', return_value=[self.go, self.registry.get('Backup')]):
                result = await self.dispatch()
            self.assertEqual(result.status_code, 200, result.body)
            self.assertEqual([body['model'] for _, body in self.sent], ['deepseek-v4.1-flash', 'backup-model'])
            self.assertEqual(json.loads(result.body)['model'], 'backup-model')
            self.assertEqual(self.go.semaphore._value, self.go.cfg.max_concurrency)

    async def test_disabled_quota_deleted_and_restricted_targets_never_escape(self):
        for attribute, value in [('enabled', False), ('quota_exhausted', True)]:
            old = getattr(self.go, attribute)
            setattr(self.go, attribute, value)
            result = await self.dispatch()
            self.assertEqual(result.status_code, 422)
            setattr(self.go, attribute, old)
        self.go.cfg.route_models = ['another-model']
        self.assertEqual((await self.dispatch()).status_code, 422)
        self.go.cfg.route_models = []
        del self.registry.states['Go']
        self.assertEqual((await self.dispatch()).status_code, 422)
        self.assertEqual(self.sent, [])
        await self.go.client.aclose()

    async def test_stream_completion_failure_and_cleanup(self):
        for tail, expected in [(b'data: [DONE]\n\n', b'response.completed'), (b'', b'response.failed')]:
            self.tail = tail
            result = await self.dispatch(stream=True)
            self.assertEqual(result.status_code, 200)
            content = b''.join([chunk async for chunk in result.body_iterator])
            self.assertIn(expected, content)
            self.assertTrue(self.streams[-1].closed)
            self.assertEqual(self.go.semaphore._value, self.go.cfg.max_concurrency)
            self.assertEqual(self.registry.route_activity_snapshot()['inflight'], 0)
        self.assertFalse(self.log.events[-1]['ok'])

    async def test_circuit_cooldown_and_business_recovery_are_kept(self):
        self.go.status = 'down'
        self.go.down_since = time.time()
        result = await self.dispatch()
        self.assertEqual(result.status_code, 502)
        self.assertEqual(self.sent, [])
        self.go.down_since = time.time() - self.gateway.cool_down - 2
        result = await self.dispatch()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.go.status, 'up')
        self.assertFalse(self.go.recovery_inflight)

    async def test_restart_interrupted_request_retains_mapping_context(self):
        self.log.start_route(100, 'Go', 'deepseek-v4.1-flash', '10.1.1.1', requested_model='gpt-6-sol', mapping_id='substitute')
        restarted = EventLog(self.tmp.name)
        restarted.load()
        event = restarted.events[-1]
        self.assertFalse(event['ok'])
        self.assertEqual(event['requested_model'], 'gpt-6-sol')
        self.assertEqual(event['mapping_id'], 'substitute')

    async def test_inflight_request_keeps_snapshot_new_request_uses_new_rule(self):
        self.block = True
        task = asyncio.create_task(self.dispatch())
        await asyncio.wait_for(self.entered.wait(), 2)
        self.registry.model_mappings.save([rule(targets=[{'station': 'Backup', 'model': 'backup-model'}])], self.registry)
        self.release.set()
        result = await task
        self.assertEqual(json.loads(result.body)['model'], 'deepseek-v4.1-flash')
        self.block = False
        result = await self.dispatch()
        self.assertEqual(json.loads(result.body)['model'], 'backup-model')


class MappingAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_isolation_authorization_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = AppConfig(gateway=GatewayConfig(data_dir=directory, allowed_ips=['127.0.0.1'], admin_key='admin'),
                            stations=[StationConfig(name='Go', base_url='https://fixture.invalid', api_key='fixture', models=['deepseek-v4.1-flash'])])
            app = build_app(cfg)
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test', headers={'Authorization': 'Bearer admin'})
            try:
                self.assertEqual((await client.put('/admin/model-mappings', json={'rules': [rule()]})).status_code, 200)
                account = (await client.post('/admin/accounts', json={'username': 'other', 'display_name': 'Other', 'allowed_ips': ['127.0.0.1']})).json()['id']
                self.assertEqual((await client.post(f'/admin/accounts/{account}/switch', json={})).status_code, 200)
                self.assertEqual((await client.get('/admin/model-mappings')).json()['rules'], [])
                self.assertEqual((await client.put('/admin/model-mappings', json={'rules': [rule()]})).status_code, 400)
                self.assertEqual((await client.get('/admin/model-mappings', headers={'Authorization': 'Bearer wrong'})).status_code, 401)
                await client.post('/admin/accounts/default/switch', json={})
                self.assertEqual((await client.get('/admin/model-mappings')).json()['rules'], [rule()])
                restored = build_app(copy.deepcopy(cfg))
                try:
                    self.assertEqual(restored.state.accounts.active.registry.model_mappings.rules, [rule()])
                    self.assertEqual(restored.state.accounts.runtime(account).registry.model_mappings.rules, [])
                finally:
                    for runtime in restored.state.accounts.runtimes.values():
                        await runtime.registry.close_clients()
                        await runtime.notifier.close()
            finally:
                await client.aclose()
                for runtime in app.state.accounts.runtimes.values():
                    await runtime.registry.close_clients()
                    await runtime.notifier.close()


if __name__ == '__main__':
    unittest.main()
