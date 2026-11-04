"""Visual tests use only MockTransport; these tests must never call an upstream."""
import asyncio
import dataclasses
import json
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException

from app.config import AppConfig, GatewayConfig, StationConfig
from app.state import Registry
from app.stats import EventLog
from app.visual_tests import (BUILTIN, MAX_RESPONSE_BYTES, VisualTestRunner,
                              VisualTestStore, VisualTestScheduler, decode_response, extract_artifact,
                              preview_document)
from app.web.admin import create_admin_router
from gateway import build_app

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80"><circle cx="35" cy="40" r="20" fill="teal"/></svg>'


class ArtifactTests(unittest.TestCase):
    def test_extract_complete_artifacts_and_reject_partial(self):
        for raw in (SVG, f'Here is the image:\n```svg\n{SVG}\n```', f'```xml\n{SVG}\n```'):
            with self.subTest(raw=raw):
                self.assertEqual(extract_artifact(raw), (SVG, 'svg'))
        doc = '<html><body>' + SVG + '</body></html>'
        self.assertEqual(extract_artifact(doc), (doc, 'html'))
        self.assertEqual(extract_artifact('```html\n<div>Hello</div>\n```')[1], 'html')
        self.assertEqual(extract_artifact('<svg><path d="M0 0"'), ('', ''))
        self.assertEqual(extract_artifact('No picture'), ('', ''))
        self.assertEqual(extract_artifact('x' * (512 * 1024 + 1)), ('', ''))

    def test_preview_strips_active_elements_and_preserves_svg_case(self):
        raw = '''<html><head><base href="https://evil.invalid"><meta http-equiv="refresh" content="0;url=https://evil.invalid"></head>
        <body onload="alert(1)"><script>alert(1)</script><iframe src="https://evil.invalid"></iframe>
        <form action="https://evil.invalid"><input name="secret"></form><img src="https://evil.invalid/a" onerror="alert(2)">
        <a href="javascript:alert(3)">text</a><svg viewBox="0 0 200 100"><defs><linearGradient id="g"><stop offset="0" stop-color="red"/></linearGradient></defs>
        <foreignObject><iframe></iframe><p>unsafe</p></foreignObject><use href="https://evil.invalid/a.svg#x"/>
        <rect fill="url(#g)" width="100" height="100"/></svg></body></html>'''
        result = preview_document(raw)
        for value in ('<script', '<iframe', '<form', '<input', 'onload', 'onerror', 'javascript:', '<base', '<foreignObject', 'http-equiv="refresh"', 'https://evil.invalid'):
            self.assertNotIn(value, result)
        self.assertIn('viewBox="0 0 200 100"', result)
        self.assertIn('<linearGradient', result)
        self.assertIn("default-src 'none'", result)
        self.assertIn("script-src 'none'", result)
        self.assertIn('fill="url(#g)"', result)

    def test_stream_parses_chat_and_responses_without_duplicate_text(self):
        chat = '\n\n'.join('data: ' + json.dumps(x) for x in [
            {'choices': [{'delta': {'content': SVG}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}]) + '\n\ndata: [DONE]\n\n'
        self.assertEqual(decode_response(chat.encode(), 'text/event-stream'), (SVG, {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}, False, ''))
        responses = '\n\n'.join('data: ' + json.dumps(x) for x in [
            {'type': 'response.output_text.delta', 'delta': SVG},
            {'type': 'response.completed', 'response': {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': SVG}]}], 'usage': {'input_tokens': 10, 'output_tokens': 20}}}])
        text, usage, truncated, error = decode_response(responses.encode(), 'text/event-stream')
        self.assertEqual(text, SVG)
        self.assertEqual(usage['total_tokens'], 30)
        self.assertFalse(truncated)
        self.assertEqual(error, '')

    def test_missing_usage_is_not_reported_as_zero_and_broken_stream_is_flagged(self):
        data = {'choices': [{'message': {'content': SVG}, 'finish_reason': 'length'}]}
        self.assertEqual(decode_response(json.dumps(data).encode(), 'application/json'), (SVG, None, True, ''))
        stream = 'data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': SVG}) + '\n\n'
        self.assertTrue(decode_response(stream.encode(), 'text/event-stream')[3])


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(data_dir=self.temp.name)
        self.registry = Registry(self.gateway, [StationConfig(name='alpha', base_url='https://fake.invalid/v1',
                                   api_key='test-secret-key', models=['first', 'second'])])
        self.st = self.registry.get('alpha')
        self.store = VisualTestStore(self.temp.name)
        self.runner = VisualTestRunner(self.registry, self.gateway, self.store)
        self.calls = []
        self.reply = httpx.Response(200, json={'choices': [{'message': {'content': SVG}}], 'usage': {'total_tokens': 100}})
        async def handler(request):
            self.calls.append(request)
            return self.reply
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.log = EventLog(self.temp.name)
        app = FastAPI()
        scheduler = VisualTestScheduler(self.registry, self.runner, self.store)
        app.include_router(create_admin_router(self.registry, self.gateway, None, self.log, visual_scheduler=scheduler))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://local')

    async def asyncTearDown(self):
        await asyncio.gather(*self.runner.cancel_pending(), return_exceptions=True)
        await self.client.aclose()
        await self.registry.close_clients()
        self.temp.cleanup()

    async def test_no_calls_from_reading_templates_results_or_station_lists(self):
        for route in ('/admin/visual-tests/prompts', '/admin/station/alpha/visual-test', '/admin/stations'):
            response = await self.client.get(route)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls, [])

    async def test_post_returns_accepted_before_generation_and_reconnect_reads_job(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            self.calls.append(request)
            started.set()
            await release.wait()
            return self.reply
        await self.st.client.aclose()
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        response = await asyncio.wait_for(self.client.post('/admin/station/alpha/visual-test', json={'model': 'second'}), 1)
        self.assertEqual(response.status_code, 202)
        job_id = response.json()['job']['id']
        await asyncio.wait_for(started.wait(), 1)
        duplicate = await self.client.post('/admin/station/alpha/visual-test', json={})
        self.assertIn(duplicate.status_code, (409, 429))
        await self.client.aclose()
        async with httpx.AsyncClient(transport=self.client._transport, base_url='http://local') as reconnected:
            current = (await reconnected.get('/admin/station/alpha/visual-test')).json()
            self.assertTrue(current['running'])
            self.assertEqual(current['job']['id'], job_id)
            self.assertEqual(current['job']['settings']['model'], 'second')
            listing = (await reconnected.get('/admin/stations')).json()
            self.assertTrue(listing[0]['visual_running'])
            task = self.runner.tasks['alpha']
            release.set()
            await asyncio.wait_for(task, 1)
            current = (await reconnected.get('/admin/station/alpha/visual-test')).json()
            self.assertFalse(current['running'])
            self.assertEqual(current['job']['state'], 'completed')
            self.assertEqual(current['result']['model'], 'second')
            self.assertEqual(len(self.calls), 1)

    async def test_cancellation_before_task_starts_releases_reservation(self):
        self.runner.submit(self.st, {}, None, lambda name: None)
        await asyncio.gather(*self.runner.cancel_pending(), return_exceptions=True)
        self.assertEqual(self.runner.busy, set())
        self.assertEqual(self.runner.tasks, {})
        self.assertEqual(self.runner.job('alpha')['state'], 'cancelled')
        self.assertEqual(self.calls, [])
        with self.assertRaises(HTTPException):
            self.runner.submit(self.st, {}, None, lambda name: None)

    async def test_single_call_selected_model_persisted_without_changing_health_or_usage(self):
        before = (self.st.health_score, self.st.last_probe_at, self.st.probe_chat_at, self.st.status, self.st.calls)
        result = await self.runner.run(self.st, {'model': 'second', 'prompt': 'Draw SVG'}, '10.2.3.4')
        self.assertEqual(len(self.calls), 1)
        payload = json.loads(self.calls[0].content)
        self.assertEqual(payload['model'], 'second')
        self.assertEqual(payload['max_completion_tokens'], 12000)
        self.assertTrue(payload['stream'])
        self.assertFalse(payload['store'])
        self.assertTrue(result['ok'])
        self.assertEqual(result['artifact'], SVG)
        self.assertEqual(result['client_ip'], '10.2.3.4')
        self.assertEqual(before, (self.st.health_score, self.st.last_probe_at, self.st.probe_chat_at, self.st.status, self.st.calls))
        self.assertEqual(VisualTestStore(self.temp.name).result(self.st)['id'], result['id'])
        self.assertEqual(self.store.summaries(self.registry)['alpha']['model'], 'second')

    async def test_user_token_limit_is_not_clamped_and_upstream_validates(self):
        for protocol, key in [('responses', 'max_output_tokens'), ('chat_completions', 'max_completion_tokens'), ('chat_completions_legacy', 'max_tokens')]:
            self.runner.next_allowed.clear()
            result = await self.runner.run(self.st, {'protocol': protocol, 'max_output_tokens': 20000}, None)
            self.assertEqual(json.loads(self.calls[-1].content)[key], 20000)
            self.assertEqual(result['max_output_tokens'], 20000)
        self.runner.next_allowed.clear()
        self.reply = httpx.Response(400, json={'error': {'message': 'model supports at most 16000 output tokens'}})
        count = len(self.calls)
        result = await self.runner.run(self.st, {'max_output_tokens': 20000}, None)
        self.assertEqual(len(self.calls), count + 1)
        self.assertIn('16000', result['error'])
        self.assertEqual(self.runner.validate(self.st, {'max_output_tokens': 1})['max_output_tokens'], 1)

    async def test_reasoning_only_response_has_evidence_and_redacted_raw_response(self):
        self.reply = httpx.Response(200, json={'choices': [{'message': {'content': '', 'reasoning_content': 'private reasoning test-secret-key'}, 'finish_reason': 'length'}],
            'usage': {'prompt_tokens': 41, 'completion_tokens': 4096, 'total_tokens': 4137, 'completion_tokens_details': {'reasoning_tokens': 4096}}})
        result = await self.runner.run(self.st, {}, None)
        self.assertFalse(result['ok'])
        self.assertEqual(result['raw_text'], '')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['diagnostics']['reasoning_tokens'], 4096)
        self.assertTrue(result['diagnostics']['reasoning_observed'])
        self.assertEqual(result['diagnostics']['finish_reasons'], ['length'])
        self.assertNotIn('test-secret-key', result['raw_response'])
        self.assertIn('[redacted]', result['raw_response'])

    async def test_reasoning_maps_to_correct_protocol_and_override_wins(self):
        self.st.cfg.inject = {'reasoning': {'effort': 'high'}, 'model': 'wrong', 'max_output_tokens': 999999, 'tools': [{'type': 'web_search'}]}
        for protocol, expected_key in [('responses', 'reasoning'), ('chat_completions', 'reasoning_effort')]:
            with self.subTest(protocol=protocol):
                self.runner.next_allowed.clear()
                result = await self.runner.run(self.st, {'protocol': protocol, 'reasoning_effort': 'low', 'max_output_tokens': 512}, None)
                payload = json.loads(self.calls[-1].content)
                self.assertEqual(payload[expected_key], {'effort': 'low'} if protocol == 'responses' else 'low')
                self.assertEqual(result['effective_reasoning_effort'], 'low')
                self.assertEqual(payload['model'], 'first')
                self.assertNotIn('tools', payload)
                self.assertNotIn('reasoning_effort' if protocol == 'responses' else 'reasoning', payload)

    async def test_reasoning_auto_and_omit_and_legacy_chat(self):
        self.st.cfg.inject = {'reasoning_effort': 'high'}
        result = await self.runner.run(self.st, {'protocol': 'responses'}, None)
        self.assertEqual(result['effective_reasoning_effort'], 'high')
        self.assertEqual(json.loads(self.calls[-1].content)['reasoning']['effort'], 'high')
        self.runner.next_allowed.clear()
        await self.runner.run(self.st, {'protocol': 'chat_completions_legacy', 'reasoning_effort': 'omit'}, None)
        payload = json.loads(self.calls[-1].content)
        self.assertNotIn('reasoning_effort', payload)
        self.assertNotIn('max_completion_tokens', payload)
        self.assertEqual(payload['max_tokens'], 12000)

    async def test_blocked_and_invalid_requests_never_call_upstream(self):
        for changes in ({'model': 'missing'}, {'model': 7}, {'reasoning_effort': 'invalid'},
                        {'prompt': ''}, {'prompt': 'x' * 12001}, {'max_output_tokens': True},
                        {'max_output_tokens': 0}, {'max_output_tokens': -1}, {'max_output_tokens': 1.5}, {'protocol': 'invalid'}, {'unexpected': 1}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                await self.runner.run(self.st, changes, None)
        for key, value in [('enabled', False), ('quota_exhausted', True), ('rate_limit_until', time.time() + 60)]:
            original = getattr(self.st, key)
            setattr(self.st, key, value)
            with self.subTest(key=key), self.assertRaises(HTTPException):
                await self.runner.run(self.st, {}, None)
            setattr(self.st, key, original)
        self.runner.busy.add('alpha')
        with self.assertRaises(HTTPException):
            await self.runner.run(self.st, {}, None)
        self.assertEqual(self.calls, [])

    async def test_rate_limit_is_respected_without_retries_or_health_mutation(self):
        self.reply = httpx.Response(429, headers={'Retry-After': '240'}, json={'error': {'message': 'limited'}})
        result = await self.runner.run(self.st, {}, None)
        self.assertFalse(result['ok'])
        self.assertGreater(self.runner.next_allowed['alpha'], time.time() + 230)
        self.assertEqual(self.st.rate_limit_until, 0)
        with self.assertRaises(HTTPException):
            await self.runner.run(self.st, {}, None)
        self.assertEqual(len(self.calls), 1)

    async def test_errors_are_useful_and_secret_redacted_and_no_redirects(self):
        for code in (400, 302, 500):
            self.runner.next_allowed.clear()
            self.reply = httpx.Response(code, headers={'Location': 'https://never.invalid'}, json={'error': {'message': 'unsupported reasoning test-secret-key'}})
            result = await self.runner.run(self.st, {}, None)
            self.assertNotIn('test-secret-key', result['error'])
            self.assertIn('unsupported reasoning', result['error'])
        self.assertEqual(len(self.calls), 3)

    async def test_result_retained_after_connection_change(self):
        original = await self.runner.run(self.st, {}, None)
        self.st.cfg = dataclasses.replace(self.st.cfg, api_key='changed')
        restored = VisualTestStore(self.temp.name).result(self.st)
        self.assertEqual(restored['id'], original['id'])
        self.assertEqual(restored['artifact'], SVG)
        self.assertTrue(restored['configuration_changed'])
        self.assertTrue(self.store.summaries(self.registry)['alpha']['preview_available'])
        with patch('app.visual_tests.time.time', return_value=time.time() + 400 * 86400):
            self.assertEqual(self.store.result(self.st)['artifact'], SVG)

    async def test_result_replaces_previous_and_can_be_cleared(self):
        one = await self.runner.run(self.st, {}, None)
        self.runner.next_allowed.clear()
        two = await self.runner.run(self.st, {'model': 'second'}, None)
        self.assertNotEqual(one['id'], two['id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0], 1)
        self.store.delete_result('alpha')
        self.assertIsNone(self.store.result(self.st))

    async def test_response_size_limit_and_transport_failure(self):
        self.reply = httpx.Response(200, content=b'x' * (MAX_RESPONSE_BYTES + 1))
        result = await self.runner.run(self.st, {}, None)
        self.assertFalse(result['ok'])
        self.assertIn('2 MB', result['error'])
        self.runner.next_allowed.clear()
        with patch.object(self.st.client, 'stream', side_effect=httpx.ConnectError('secret URL')):
            result = await self.runner.run(self.st, {}, None)
        self.assertIn('未自动重试', result['error'])
        self.assertNotIn('secret URL', result['error'])
        self.assertEqual(self.runner.busy, set())

    async def test_prompt_crud_and_builtin_protection(self):
        created = await self.client.post('/admin/visual-tests/prompts', json={'name': 'Card', 'prompt': 'Make HTML'})
        self.assertEqual(created.status_code, 201)
        pid = created.json()['id']
        self.assertEqual((await self.client.put('/admin/visual-tests/prompts/' + pid, json={'name': 'Card 2', 'prompt': 'Draw SVG'})).status_code, 200)
        self.assertEqual((await self.client.delete('/admin/visual-tests/prompts/' + BUILTIN['id'])).status_code, 409)
        self.assertEqual((await self.client.put('/admin/visual-tests/prompts/' + BUILTIN['id'], json={})).status_code, 409)
        self.assertEqual((await self.client.delete('/admin/visual-tests/prompts/' + pid)).status_code, 200)
        self.assertEqual(len((await self.client.get('/admin/visual-tests/prompts')).json()), 1)
        self.assertEqual(self.calls, [])


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        RunnerTests.setUp(self)
        self.now = time.time()
        self.scheduler = VisualTestScheduler(self.registry, self.runner, self.store, clock=lambda: self.now)

    asyncTearDown = RunnerTests.asyncTearDown

    def configure(self, **changes):
        return self.scheduler.configure(self.st, {'enabled': True, 'interval_seconds': 600,
                                                  'settings': {'model': 'second', 'reasoning_effort': 'low'}, **changes})

    def due(self):
        self.now = self.store.schedules()['alpha']['next_run_at']
        self.runner.next_allowed.clear()

    async def test_default_off_save_without_call_snapshot_and_full_interval(self):
        self.scheduler.activate()
        self.assertEqual(self.scheduler.view('alpha')['state'], 'disabled')
        await self.scheduler.tick()
        plan = self.configure()
        self.assertEqual(plan['settings']['model'], 'second')
        self.assertEqual(plan['settings']['prompt'], BUILTIN['prompt'])
        self.assertEqual(plan['next_run_at'], self.now + 600)
        await self.scheduler.tick()
        self.assertEqual(self.calls, [])
        self.due()
        await self.scheduler.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads(self.calls[0].content)['reasoning_effort'], 'low')
        self.assertEqual(self.store.result(self.st)['source'], 'automatic')
        self.assertEqual(VisualTestStore(self.temp.name).schedules()['alpha']['settings'], plan['settings'])
        await self.scheduler.tick()
        self.assertEqual(len(self.calls), 1)

    async def test_inactive_and_reactivation_does_not_catch_up(self):
        self.configure()
        self.due()
        await self.scheduler.tick()
        self.assertEqual(self.calls, [])
        self.now += 86400
        self.scheduler.activate()
        self.assertEqual(self.scheduler.view('alpha')['next_run_at'], self.now + 600)
        await self.scheduler.tick()
        self.assertEqual(self.calls, [])
        self.scheduler.deactivate()
        self.due()
        await self.scheduler.tick()
        self.assertEqual(self.calls, [])

    async def test_validation_and_disabling_need_no_upstream(self):
        for body in ({}, {'enabled': 'true'}, {'enabled': True, 'interval_seconds': 60, 'settings': {}},
                     {'enabled': True, 'interval_seconds': True, 'settings': {}},
                     {'enabled': True, 'settings': {'model': 'absent'}}):
            with self.subTest(body=body), self.assertRaises(HTTPException):
                self.scheduler.configure(self.st, body)
        self.configure()
        plan = self.scheduler.configure(self.st, {'enabled': False})
        self.assertFalse(plan['enabled'])
        self.assertIsNone(plan['next_run_at'])
        self.assertEqual(self.calls, [])

    async def test_auth_and_parameter_errors_pause_without_retry(self):
        for code in (400, 401, 403, 404, 422, 302):
            with self.subTest(code=code):
                self.scheduler.activate()
                self.configure()
                self.due()
                self.reply = httpx.Response(code, json={'error': {'message': 'invalid'}})
                before = len(self.calls)
                await self.scheduler.tick()
                self.assertEqual(len(self.calls), before + 1)
                self.assertEqual(self.scheduler.view('alpha')['state'], 'paused')
                self.now += 86400
                self.scheduler.activate()
                await self.scheduler.tick()
                self.assertEqual(len(self.calls), before + 1)

    async def test_failures_backoff_and_pause_after_three(self):
        self.scheduler.activate()
        self.configure()
        self.reply = httpx.Response(503, json={'error': {'message': 'unavailable'}})
        for failures in (1, 2, 3):
            self.due()
            await self.scheduler.tick()
            plan = self.store.schedules()['alpha']
            self.assertEqual(plan['failures'], failures)
            self.assertEqual(plan['next_run_at'], self.now + 600 * 2 ** failures)
        self.assertEqual(self.scheduler.view('alpha')['state'], 'paused')
        self.assertEqual(len(self.calls), 3)

    async def test_rate_limit_retry_after_and_manual_deferral(self):
        self.scheduler.activate()
        self.configure()
        self.due()
        self.reply = httpx.Response(429, headers={'Retry-After': '86400'})
        await self.scheduler.tick()
        self.assertGreaterEqual(self.store.schedules()['alpha']['next_run_at'], self.runner.next_allowed['alpha'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.scheduler.view('alpha')['state'], 'scheduled')
        self.scheduler.defer_after_manual('alpha')
        self.assertGreaterEqual(self.store.schedules()['alpha']['next_run_at'], self.runner.next_allowed['alpha'])

    async def test_disabled_quota_cooling_and_changed_connection_skip(self):
        self.scheduler.activate()
        self.configure()
        for key, value in [('enabled', False), ('quota_exhausted', True), ('rate_limit_until', time.time() + 9999)]:
            self.due()
            old = getattr(self.st, key)
            setattr(self.st, key, value)
            await self.scheduler.tick()
            setattr(self.st, key, old)
            self.assertEqual(self.calls, [])
        self.due()
        self.st.cfg = dataclasses.replace(self.st.cfg, api_key='changed')
        await self.scheduler.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.scheduler.view('alpha')['state'], 'paused')

    async def test_changed_plan_is_not_overwritten_by_inflight_request(self):
        self.scheduler.activate()
        self.configure()
        self.due()
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            entered.set()
            await release.wait()
            return self.reply
        await self.st.client.aclose()
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        task = asyncio.create_task(self.scheduler.tick())
        await asyncio.wait_for(entered.wait(), 2)
        self.scheduler.configure(self.st, {'enabled': False})
        release.set()
        await task
        self.assertEqual(self.scheduler.view('alpha')['state'], 'disabled')

    async def test_tick_serializes_due_sites(self):
        self.registry.add_station(StationConfig(name='beta', base_url='https://fake.invalid', api_key='key', models=['first']))
        second = self.registry.get('beta')
        second.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.reply))
        self.scheduler.activate()
        self.configure()
        self.scheduler.configure(second, {'enabled': True, 'interval_seconds': 600, 'settings': {}})
        self.due()
        await self.scheduler.tick()
        self.assertIsNotNone(self.store.result(self.st))
        self.assertIsNone(self.store.result(second))


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_stop_drains_detached_manual_jobs_before_closing_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            app = build_app(AppConfig(gateway=GatewayConfig(data_dir=directory, allowed_ips=['127.0.0.1']),
                stations=[StationConfig(name='alpha', base_url='https://fake.invalid', api_key='fixture', models=['first'])]))
            manager, started = app.state.accounts, asyncio.Event()
            runtime = manager.active
            st = runtime.registry.get('alpha')
            async def handler(request):
                started.set()
                await asyncio.Event().wait()
            async def quiet_health():
                await asyncio.Event().wait()
            st.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            client = st.client
            with patch.object(runtime.monitor, 'run', quiet_health):
                await manager.start()
                runner = runtime.visual_scheduler.runner
                runner.submit(st, {}, '127.0.0.1', runtime.visual_scheduler.defer_after_manual)
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.wait_for(manager.stop(), 2)
                self.assertEqual(runner.busy, set())
                self.assertEqual(runner.tasks, {})
                self.assertEqual(runner.job('alpha')['state'], 'cancelled')
                self.assertIn('账号切换或服务停止', runner.store.result(st)['error'])
                self.assertTrue(client.is_closed)

    async def test_account_pause_cancels_automated_request_and_releases_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            app = build_app(AppConfig(gateway=GatewayConfig(data_dir=directory, allowed_ips=['127.0.0.1']),
                stations=[StationConfig(name='alpha', base_url='https://fake.invalid', api_key='fixture', models=['first'])]))
            manager, started, cancelled = app.state.accounts, asyncio.Event(), asyncio.Event()
            runtime = manager.active
            st = runtime.registry.get('alpha')
            async def handler(request):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            st.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            async def quiet_health():
                await asyncio.Event().wait()
            with patch.object(runtime.monitor, 'run', quiet_health):
                runtime.visual_scheduler.configure(st, {'enabled': True, 'settings': {}})
                runtime.start()
                plan = runtime.visual_scheduler.store.schedules()['alpha']
                plan['next_run_at'] = 0
                runtime.visual_scheduler.store.save_schedule('alpha', plan)
                await asyncio.wait_for(started.wait(), 2)
                task = runtime.pause()
                await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(cancelled.is_set())
                self.assertFalse(runtime.visual_scheduler.active)
                self.assertEqual(runtime.visual_scheduler.runner.busy, set())
                self.assertIn('账号切换', runtime.visual_scheduler.store.result(st)['error'])
                runtime.start()
                await asyncio.sleep(.02)
                self.assertGreater(runtime.visual_scheduler.view('alpha')['next_run_at'], time.time() + 3500)
                await asyncio.gather(runtime.pause(), return_exceptions=True)
            await runtime.registry.close_clients()
            await runtime.notifier.close()

    async def test_accounts_isolate_prompts_results_and_reject_stale_or_unauthorized_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = AppConfig(gateway=GatewayConfig(data_dir=directory, allowed_ips=['10.1.1.1'], admin_key='admin-test'),
                            stations=[StationConfig(name='alpha', base_url='https://fake.invalid', api_key='key', models=['model'])])
            app = build_app(cfg)
            manager = app.state.accounts
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=('10.1.1.1', 123)), base_url='http://local', headers={'Authorization': 'Bearer admin-test'}) as client:
                await client.post('/admin/visual-tests/prompts', json={'name': 'Private', 'prompt': 'secret prompt'})
                saved = await client.put('/admin/station/alpha/visual-test/schedule', json={'enabled': True, 'interval_seconds': 600, 'settings': {}})
                self.assertEqual(saved.status_code, 200)
                old_revision = manager.revision
                created = await client.post('/admin/accounts', json={'username': 'another', 'display_name': 'Another', 'allowed_ips': ['10.1.1.1']})
                account_id = created.json()['id']
                await client.post(f'/admin/accounts/{account_id}/switch', json={})
                self.assertEqual((await client.get('/admin/visual-tests/prompts')).json(), [BUILTIN])
                self.assertEqual((await client.get('/admin/station/alpha/visual-test')).status_code, 404)
                self.assertEqual(manager.active.visual_scheduler.store.schedules(), {})
                stale = await client.post('/admin/station/alpha/visual-test', headers={'X-ModelHarbor-Revision': old_revision}, json={})
                self.assertEqual(stale.status_code, 409)
                self.assertEqual((await client.post('/admin/station/alpha/visual-test', headers={'Authorization': 'Bearer bad'}, json={})).status_code, 401)
                await client.post('/admin/accounts/default/switch', json={})
                self.assertEqual(len((await client.get('/admin/visual-tests/prompts')).json()), 2)
                self.assertTrue((await client.get('/admin/station/alpha/visual-test/schedule')).json()['enabled'])
            for runtime in manager.runtimes.values():
                await runtime.registry.close_clients()
                await runtime.notifier.close()


if __name__ == '__main__':
    unittest.main()
