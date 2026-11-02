"""Read-only quota UI and no-text result diagnosis, using isolated mock data."""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import AppConfig, GatewayConfig, StationConfig
from app.go_usage import normalize_usage
from gateway import build_app

OUT = ROOT / 'data/modelharbor-qa/go-usage'
OUT.mkdir(parents=True, exist_ok=True)
payload = {'usage': {key: {'percent': percent, 'status': 'ok', 'resetsAt': '2026-11-09T00:00:00Z'}
                     for key, percent in [('rolling', 40), ('weekly', 16), ('monthly', 9)]}}
errors, external, upstream = [], [], []
with tempfile.TemporaryDirectory() as directory:
    app = build_app(AppConfig(gateway=GatewayConfig(data_dir=directory, allowed_ips=['127.0.0.1']),
        stations=[StationConfig.from_dict('Go QA', {'provider': 'opencode', 'opencode_plan': 'go',
            'base_url': 'https://opencode.ai', 'api_key': 'fixture-only', 'models': ['test-model'], 'probe_models': False})]))
    runtime = app.state.accounts.active
    async def quiet_health():
        await asyncio.Event().wait()
    runtime.monitor.run = quiet_health
    st = runtime.registry.get('Go QA')
    st.go_usage = normalize_usage(payload)
    st.go_usage_status = 'ready'
    st.go_usage_updated_at = time.time()
    st.model_api_paths = {'test-model': ['/chat/completions', '/responses', '/messages', '/generateContent']}
    async def handle(request):
        upstream.append((request.method, str(request.url)))
        assert request.method == 'GET' and request.url.path == '/inference/go/v1/usage'
        return httpx.Response(200, json=payload)
    st.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    runtime.visual_scheduler.store.save_result(st, dict(id='empty-legacy', station='Go QA', model='test-model', protocol='chat_completions',
        created_at=time.time(), artifact='', raw_text='', kind='', ok=False, truncated=True, error='legacy failure',
        latency_ms=22354, http_status=200, usage={'input_tokens':41,'output_tokens':4096,'total_tokens':4137},
        prompt='Draw SVG', template_name='Fixture', max_output_tokens=4096, reasoning_effort='auto'))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    origin = 'http://127.0.0.1:' + str(port)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    for _ in range(100):
        if server.started: break
        time.sleep(.05)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get('MODELHARBOR_TEST_BROWSER', 'msedge'))
            page = browser.new_page(viewport={'width':1440, 'height':900})
            page.on('pageerror', lambda e: errors.append(str(e)))
            def guard(route):
                if route.request.url.startswith(origin + '/'):
                    route.continue_()
                else:
                    external.append(route.request.url); route.abort()
            page.route('**/*', guard)
            page.goto(origin + '/dashboard')
            entry = page.locator('#list-wrap [data-go-usage="Go QA"]')
            entry.wait_for()
            assert '40%' in entry.inner_text() and '16%' in entry.inner_text() and '9%' in entry.inner_text()
            for width,height in [(1920,1080),(1440,900),(1366,768),(1024,768),(768,1024),(390,844),(360,640)]:
                page.set_viewport_size({'width':width,'height':height})
                entry.click()
                dialog = page.locator('#goUsageDialog')
                assert dialog.evaluate('e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight}')
                assert dialog.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                assert '本周期' in dialog.inner_text() and '重置于' in dialog.inner_text()
                page.screenshot(path=str(OUT / (str(width)+'-quota.png')))
                page.locator('#goUsageClose').click()
            assert not upstream, upstream
            page.set_viewport_size({'width':1440,'height':900})
            entry.click(); page.locator('#goUsageRefresh').click()
            page.wait_for_function("document.querySelector('#goUsageRefresh').textContent.includes('秒后')")
            assert len(upstream)==1
            assert page.locator('#goUsageRefresh').is_disabled()
            page.locator('#goUsageClose').click()
            page.locator('#list-wrap [data-visual-test="Go QA"]').click()
            page.wait_for_function("document.querySelector('#visualTestForm').getAttribute('aria-busy') === 'false'")
            assert page.locator('#visualResultDiagnosis').is_visible()
            assert '4096' in page.locator('#visualDiagnosisText').inner_text()
            assert '无法确认' in page.locator('#visualDiagnosisText').inner_text()
            assert '历史记录未保存' in page.locator('#visual-panel-raw').inner_text()
            for width,height in [(1440,900),(390,844),(360,640)]:
                page.set_viewport_size({'width':width,'height':height})
                assert page.locator('#visualTestDialog').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                page.screenshot(path=str(OUT/(str(width)+'-diagnosis.png')))
            page.locator('#visualAdjustSettings').click()
            page.locator('#visualTestLimit').fill('20000')
            assert page.locator('#visualTestLimit').get_attribute('max') is None
            assert page.locator('#visualTestLimit').evaluate('e=>e.checkValidity()')
            assert page.locator('#visualTestLimit').input_value()=='20000'
            page.locator('[data-visual-config="schedule"]').click()
            page.locator('#visualScheduleEnabled').check()
            page.locator('#visualScheduleSave').click()
            page.wait_for_function("document.querySelector('#visualScheduleFeedback').textContent.includes('已保存')")
            saved=runtime.visual_scheduler.store.schedules()['Go QA']
            assert saved['settings']['max_output_tokens']==20000
            assert saved['enabled']
            page.locator('#visualScheduleEnabled').uncheck()
            page.locator('#visualScheduleSave').click()
            page.wait_for_function("document.querySelector('#visualScheduleFeedback').textContent.includes('已关闭')")
            assert len(upstream)==1, upstream
            assert not errors,errors
            assert not external,external
            browser.close()
    finally:
        server.should_exit=True; thread.join(timeout=10)
print('Go quota/diagnosis/browser checks passed; 7 viewports; 20000 Token saved; 0 generation requests')
