"""Model catalog layout regression: long names + four protocols, no provider I/O."""
import asyncio
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
from gateway import build_app

OUT = ROOT / 'data/modelharbor-qa/model-catalog'
OUT.mkdir(parents=True, exist_ok=True)
MODELS = ['demo-reasoner-with-a-long-version-identifier-2026-11-02'] + [
    f'catalog-model-{i:02}-preview' for i in range(27)]
PATHS = ['/chat/completions', '/responses', '/messages', '/generateContent']

with tempfile.TemporaryDirectory() as directory:
    app = build_app(AppConfig(gateway=GatewayConfig(data_dir=directory, allowed_ips=['127.0.0.1']),
        stations=[StationConfig.from_dict('Catalog QA', {
            'provider': 'opencode', 'opencode_plan': 'go', 'base_url': 'https://opencode.ai',
            'api_key': 'fixture-only', 'models': MODELS, 'probe_models': True})]))
    runtime = app.state.accounts.active
    calls, errors, external = [], [], []

    async def quiet_health():
        await asyncio.Event().wait()

    async def deny_upstream(request):
        calls.append(request.url.host)
        raise AssertionError('No upstream I/O permitted')

    runtime.monitor.run = quiet_health
    station = runtime.registry.get('Catalog QA')
    station.client = httpx.AsyncClient(transport=httpx.MockTransport(deny_upstream))
    station.probed_models = set(MODELS)
    station.probed_model_order = MODELS[:]
    station.model_api_paths = {model: PATHS[:] for model in MODELS}
    station.models_discovery_status = 'ready'
    station.models_discovered_at = time.time()
    station.model_unavailable[MODELS[-1]] = 'This model is not available for the selected subscription.'
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)
    assert server.started
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get('MODELHARBOR_TEST_BROWSER'))
            page = browser.new_page(viewport={'width':1440,'height':900}, reduced_motion='reduce')
            page.on('pageerror', lambda error: errors.append(str(error)))

            def guard(route):
                if route.request.url.startswith(origin + '/'):
                    route.continue_()
                else:
                    external.append(route.request.url)
                    route.abort()

            page.route('**/*', guard)
            page.goto(origin + '/dashboard')
            page.locator('#tab-stations').click()
            page.locator('[data-station-row="Catalog QA"] [data-manage-action="models"]').click()
            page.wait_for_function('document.querySelectorAll("#sf-model-list button").length === 28')
            viewports = [(1920,1080),(1440,900),(1366,768),(1280,720),(1024,768),
                         (768,1024),(390,844),(320,568),(844,390)]
            for width, height in viewports:
                page.set_viewport_size({'width':width,'height':height})
                page.wait_for_timeout(120)
                result = page.locator('#sf-model-list').evaluate('''list => {
                    const items = [...list.querySelectorAll('button')];
                    const issues = [];
                    for (const item of items) {
                        const box = item.getBoundingClientRect();
                        const name = item.querySelector('span').getBoundingClientRect();
                        const detail = item.querySelector('small').getBoundingClientRect();
                        if (name.bottom > detail.top + 1) issues.push('name/detail overlap');
                        if (name.left < box.left || name.right > box.right + 1 ||
                            name.top < box.top || detail.bottom > box.bottom + 1)
                            issues.push('text outside button');
                        if (item.scrollWidth > item.clientWidth + 1 || item.scrollHeight > item.clientHeight + 1)
                            issues.push('button content clipped');
                    }
                    for (let i=0;i<items.length;i++) for (let j=i+1;j<items.length;j++) {
                        const a=items[i].getBoundingClientRect(), b=items[j].getBoundingClientRect();
                        if (Math.min(a.right,b.right)-Math.max(a.left,b.left)>1 &&
                            Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)
                            issues.push('buttons overlap');
                    }
                    const save=document.querySelector('#sf-submit').getBoundingClientRect();
                    return {issues, horizontalOverflow:list.scrollWidth-list.clientWidth,
                        pageWidth:document.documentElement.scrollWidth,
                        pageHeight:document.documentElement.scrollHeight, saveBottom:save.bottom};
                }''')
                assert not result['issues'], (width, height, result)
                assert result['horizontalOverflow'] <= 1, (width, height, result)
                assert result['pageWidth'] <= width + 1, (width, height, result)
                assert result['pageHeight'] <= height + 1, (width, height, result)
                assert result['saveBottom'] <= height + 1, (width, height, result)
                if width in (1440,1366,390):
                    page.screenshot(path=str(OUT/f'{width}x{height}.png'))
            page.set_viewport_size({'width':1440,'height':900})
            page.locator('#sf-model-search').fill('catalog-model-25')
            assert page.locator('#sf-model-list button').count() == 1
            page.locator('#sf-model-list button').click()
            assert page.locator('#sf-probe-current').inner_text() == MODELS[-2]
            assert page.locator('#sf-model-list button').get_attribute('aria-pressed') == 'true'
            assert not page.locator('#sf-probe-model-auto').is_checked()
            page.locator('#sf-model-search').fill('catalog-model-26')
            assert page.locator('#sf-model-list button').is_disabled()
            assert not calls, calls
            assert not external, external
            assert not errors, errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
print('Model catalog verified: 9 viewports, 28 multi-protocol models; no external calls.')
