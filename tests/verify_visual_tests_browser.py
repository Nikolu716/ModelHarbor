"""Isolated visual-test workflow and responsive checks; all upstream I/O is mocked."""
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
from gateway import build_app

OUT = ROOT / 'data/modelharbor-qa/visual-tests'
OUT.mkdir(parents=True, exist_ok=True)
ARTIFACT = '''<!doctype html><html><head><style>body{background:#f3f5f6} .remote{background:url(https://blocked.invalid/image)}</style></head><body>
<script>parent.visualUnsafe=true;fetch('https://blocked.invalid/script')</script>
<img src="https://blocked.invalid/pixel" onerror="parent.visualUnsafe=true">
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 280"><rect width="400" height="280" fill="#f3f5f6"/><g fill="none" stroke="#35565b" stroke-width="5"><circle cx="110" cy="195" r="53"/><circle cx="300" cy="195" r="53"/><path d="m110 195 70-102 44 102H110l100-75h55l35 75"/></g><path d="M180 103c-23-68 35-96 67-54l64 12-76 18c-4 38-37 44-55 24Z" fill="#e7ba76"/><circle cx="229" cy="54" r="4" fill="#35565b"/><text x="200" y="265" text-anchor="middle" fill="#657579" font-size="12">Isolated preview fixture</text></svg>
</body></html>'''
errors, external, calls = [], [], []
release_generation = threading.Event()

with tempfile.TemporaryDirectory() as directory:
    app = build_app(AppConfig(gateway=GatewayConfig(data_dir=directory, allowed_ips=['127.0.0.1']),
        stations=[StationConfig(name='Visual QA', base_url='https://mock.invalid/v1', api_key='fixture-only',
                                models=['first-model', 'second-model'] + [f'catalog-model-{i:02}' for i in range(40)])] +
                 [StationConfig(name=name, base_url='https://mock.invalid/v1', api_key='fixture-only', models=['first-model'])
                  for name in ['Atlas', 'Cedar', 'Harbor', 'Quartz', 'Willow']]))
    runtime = app.state.accounts.active
    # A low-score station awaiting recovery exercises the widest primary action.
    recovery_station = runtime.registry.get('Cedar')
    recovery_station.status = 'down'
    recovery_station.down_since = time.time() - runtime.gateway.cool_down - 1
    recovery_station.consecutive_failures = 3
    loop_holder = []
    async def quiet_health():
        loop_holder.append(asyncio.get_running_loop())
        await asyncio.Event().wait()
    runtime.monitor.run = quiet_health
    async def upstream(request):
        assert request.url.host == 'mock.invalid'
        calls.append(json.loads(request.content))
        while not release_generation.is_set():
            await asyncio.sleep(.05)
        return httpx.Response(200, json={'choices': [{'message': {'content': ARTIFACT}}],
                                        'usage': {'prompt_tokens': 20, 'completion_tokens': 200, 'total_tokens': 220}})
    runtime.registry.get('Visual QA').client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    for name in ['Atlas', 'Cedar', 'Harbor', 'Quartz', 'Willow']:
        runtime.visual_scheduler.store.save_result(runtime.registry.get(name), dict(id=name, station=name, model='first-model', http_status=200,
            created_at=time.time(), artifact=ARTIFACT.replace('#e7ba76', '#a1bca5'), kind='html', ok=True,
            error='', truncated=False, latency_ms=3200, usage={'total_tokens': 220}, raw_text=ARTIFACT))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
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
            context = browser.new_context(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce')
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            def inspect_request(request):
                if request.url.startswith(('http:', 'https:')) and not request.url.startswith(f'http://127.0.0.1:{port}/'):
                    external.append(request.url)
            page.on('request', inspect_request)
            page.goto(f'http://127.0.0.1:{port}/dashboard')
            page.locator('[data-channel-models="Visual QA"]').first.wait_for()
            page.locator('#tab-stations').click()
            page.locator('#stations-tab-scoring').click()
            page.wait_for_function('!document.querySelector("#scoring-speed-weight").disabled')
            assert page.locator('#scoring-speed-weight').input_value() == '15'
            page.locator('#scoring-speed-weight').fill('35')
            assert '65–100' in page.locator('#scoring-preview').inner_text()
            page.locator('#scoring-save').click()
            page.wait_for_function('document.querySelector("#scoringSettingsStatus").textContent.includes("即时生效")')
            assert runtime.gateway.scoring_speed_weight == .35
            page.reload()
            page.locator('#stations-tab-scoring').click()
            page.wait_for_function('!document.querySelector("#scoring-speed-weight").disabled')
            assert page.locator('#scoring-speed-weight').input_value() == '35'
            page.locator('#scoring-reset').click()
            page.locator('#scoring-save').click()
            page.wait_for_function('document.querySelector("#scoringSettingsStatus").textContent.includes("即时生效")')
            assert runtime.gateway.scoring_speed_weight == .15
            assert len(calls) == 0
            page.locator('#tab-monitor').click()
            assert page.locator('#monitor-tab-visual').inner_text() == '智商测试'
            assert page.locator('#list-wrap th.col-visual').inner_text() == '智商测试'
            page.locator('#station-search').fill('Atlas')
            table_preview = page.locator('[data-table-visual="Atlas"] button')
            table_preview.locator('iframe').wait_for()
            assert table_preview.locator('iframe').get_attribute('sandbox') == ''
            table_preview.frame_locator('iframe').locator('svg').wait_for()
            page.evaluate('''() => {
                window.qaThumbnail = document.querySelector('[data-table-visual="Atlas"] iframe');
                window.qaThumbnailLoads = 0;
                qaThumbnail.addEventListener('load', () => window.qaThumbnailLoads++);
            }''')
            for count in range(1, 5):
                page.locator('#refreshDashboard').click()
                page.wait_for_function('!document.querySelector("#refreshDashboard").disabled')
                # Exercise changed metrics, not just an identical response fingerprint.
                page.evaluate('''count => {
                    statusData.stations.find(s => s.name === 'Atlas').calls = count;
                    render(true);
                }''', count)
                assert page.locator('[data-monitor-station="Atlas"] .col-calls').inner_text().startswith(str(count))
                assert page.evaluate('qaThumbnail === document.querySelector(\'[data-table-visual="Atlas"] iframe\')')
            # Metadata polling and running-state badges must not reload the last result.
            page.evaluate('''() => {
                const item = STATIONS.find(s => s.name === 'Atlas');
                item.visual_running = true;
                ModelHarborVisualTests.syncTablePreviews();
                item.visual_running = false;
                ModelHarborVisualTests.syncTablePreviews();
            }''')
            page.wait_for_timeout(7000)
            assert page.evaluate('qaThumbnail === document.querySelector(\'[data-table-visual="Atlas"] iframe\')')
            assert page.evaluate('qaThumbnailLoads') == 0, 'unchanged thumbnail reloaded during polling'
            table_preview.click()
            page.frame_locator('#visualPreviewFrame').locator('svg').wait_for()
            assert page.locator('#visualTestTitle').inner_text() == '智商测试'
            assert page.locator('#visualTestStation').inner_text() == 'Atlas'
            page.locator('#visualTestClose').click()
            page.locator('#station-search').fill('Visual QA')
            page.locator('[data-table-visual="Visual QA"] button').click()
            page.wait_for_function('!document.querySelector("#visualTestRun").disabled')
            assert page.locator('#visualTestDialog').get_attribute('data-view') == 'config'
            assert calls == []
            page.locator('#visualTestClose').click()
            page.locator('#station-search').fill('')
            for thumb in page.locator('[data-table-visual]:visible button[data-visual-thumbnail]').all():
                thumb.frame_locator('iframe').locator('svg').wait_for()
            page.screenshot(path=str(OUT / '1440-model-column.png'))
            page.locator('[data-channel-models="Visual QA"]').first.click()
            assert '42 / 42' in page.locator('#channelModelsCount').inner_text()
            page.screenshot(path=str(OUT / '1440-model-catalog.png'))
            page.locator('#channelModelsSearch').fill('catalog-model-39')
            assert page.locator('#channelModelsList>div').count() == 1
            page.locator('#channelModelsClose').click()
            page.locator('#monitor-tab-visual').click()
            page.locator('#visualGallerySearch').fill('Visual QA')
            entry = page.locator('.visual-gallery-card [data-visual-open="config"]').first
            entry.click()
            page.wait_for_function('!document.querySelector("#visualTestRun").disabled')
            assert page.locator('#visualTestModel').input_value() == 'first-model'
            assert not page.locator('#visualScheduleEnabled').is_checked()
            assert calls == []
            page.locator('#visualTestModel').select_option('second-model')
            page.locator('[data-visual-config="options"]').click()
            page.locator('#visualTestReasoning').select_option('low')
            page.locator('#visualTestLimit').fill('4096')
            page.locator('[data-visual-config="content"]').click()
            page.locator('.visual-template-library>summary').click()
            page.locator('#visualTemplateName').fill('Custom card')
            page.locator('#visualTestPrompt').fill('Draw a pelican riding a bicycle as a complete HTML document.')
            page.locator('#visualTemplateSave').click()
            page.wait_for_function('document.querySelector("#visualTemplateStatus").textContent.includes("已保存")')
            assert calls == []
            page.locator('#visualTestRun').click()
            page.wait_for_function('document.querySelector("#visualTestStatus").textContent.includes("后台生成中")')
            assert not page.locator('#visualTestClose').is_disabled()
            page.locator('#visualTestClose').click()
            page.locator('#tab-stats').click()
            page.locator('#tab-monitor').click()
            entry.click()
            page.wait_for_function('document.querySelector("#visualTestStatus").textContent.includes("后台生成中")')
            assert page.locator('#visualTestRun').is_disabled()
            page.keyboard.press('Escape')
            page.reload()
            page.locator('#monitor-tab-visual').click()
            page.locator('#visualGallerySearch').fill('Visual QA')
            page.locator('.visual-gallery-card [data-visual-open="result"]').click()
            page.wait_for_function('document.querySelector("#visualTestStatus").textContent.includes("后台生成中")')
            release_generation.set()
            page.wait_for_function('document.querySelector("#visualTestStatus").textContent.includes("测试完成")')
            assert len(calls) == 1 and calls[0]['model'] == 'second-model'
            assert calls[0]['reasoning_effort'] == 'low'
            assert page.locator('#visualPreviewFrame').is_visible()
            assert page.locator('#visualPreviewFrame').get_attribute('sandbox') == ''
            page.frame_locator('#visualPreviewFrame').locator('svg').wait_for()
            assert page.frame_locator('#visualPreviewFrame').locator('script').count() == 0
            assert page.evaluate('window.visualUnsafe === undefined')
            # A completed job appears in the home table without re-running it.
            page.locator('#visualTestClose').click()
            page.locator('#monitor-tab-stations').click()
            page.locator('#station-search').fill('Visual QA')
            page.locator('[data-table-visual="Visual QA"] iframe').wait_for()
            table_result = page.locator('[data-table-visual="Visual QA"] button')
            latest_id = runtime.visual_scheduler.store.result(runtime.registry.get('Visual QA'))['id']
            assert table_result.get_attribute('data-visual-thumbnail') == latest_id
            table_result.click()
            page.frame_locator('#visualPreviewFrame').locator('svg').wait_for()
            assert len(calls) == 1
            page.locator('#visualTestClose').click()
            page.locator('#monitor-tab-visual').click()
            page.locator('.visual-gallery-card [data-visual-open="result"]').click()
            page.frame_locator('#visualPreviewFrame').locator('svg').wait_for()
            page.locator('[data-visual-tab="source"]').click()
            assert '<svg' in page.locator('#visual-panel-source').inner_text()
            page.locator('[data-visual-tab="raw"]').click()
            assert '<script>' in page.locator('#visual-panel-raw').inner_text()
            page.locator('[data-visual-tab="preview"]').click()
            page.locator('[data-visual-workspace="config"]').click()
            page.locator('[data-visual-config="schedule"]').click()
            page.locator('#visualScheduleEnabled').check()
            page.locator('#visualScheduleInterval').fill('60')
            page.locator('#visualScheduleSave').click()
            page.wait_for_function('document.querySelector("#visualScheduleFeedback").textContent.includes("已保存")')
            assert len(calls) == 1
            assert 'second-model' in page.locator('#visualScheduleSummary').inner_text()
            assert '下次执行不早于' in page.locator('#visualScheduleStatus').inner_text()
            page.screenshot(path=str(OUT / '1440-schedule.png'))
            page.locator('#visualTestClose').click()
            entry.click()
            page.wait_for_function('!document.querySelector("#visualTestRun").disabled')
            assert page.locator('#visualTestModel').input_value() == 'second-model'
            assert page.locator('#visualTestPrompt').input_value().startswith('Draw a pelican')
            assert page.locator('#visualScheduleEnabled').is_checked()
            assert len(calls) == 1
            # Drive a due plan on the server loop, against MockTransport only.
            async def run_due():
                scheduler = runtime.visual_scheduler
                plan = scheduler.store.schedules()['Visual QA']
                plan['next_run_at'] = 0
                scheduler.store.save_schedule('Visual QA', plan)
                scheduler.runner.next_allowed.clear()
                await scheduler.tick()
            asyncio.run_coroutine_threadsafe(run_due(), loop_holder[0]).result(5)
            assert len(calls) == 2
            page.locator('#visualTestClose').click()
            entry.click()
            page.wait_for_function('!document.querySelector("#visualTestRun").disabled')
            assert '自动' in page.locator('#visualResultContext').inner_text()
            page.locator('[data-visual-config="schedule"]').click()
            page.locator('#visualScheduleEnabled').uncheck()
            page.locator('#visualScheduleSave').click()
            page.wait_for_function('document.querySelector("#visualScheduleFeedback").textContent.includes("已关闭")')
            page.locator('[data-visual-config="content"]').click()
            for width, height in [(1920, 1080), (1366, 768), (1280, 720), (1024, 768), (768, 1024), (390, 844), (360, 640), (844, 390)]:
                page.locator('#visualTestClose').click()
                page.set_viewport_size({'width': width, 'height': height})
                page.locator('#tab-stations').click()
                page.locator('#stations-tab-scoring').click()
                page.wait_for_function('!document.querySelector("#scoring-speed-weight").disabled')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1 && document.documentElement.scrollHeight <= innerHeight + 1'), (width, height, 'scoring page overflow')
                save = page.locator('#scoring-save').bounding_box()
                assert save and save['y'] >= 0 and save['y'] + save['height'] <= height + 1, (width, height, 'scoring save visibility')
                assert page.locator('#scoring-wrap').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
                if width in (1366, 390):
                    page.screenshot(path=str(OUT / f'{width}-{height}-scoring-policy.png'))
                page.locator('#tab-monitor').click()
                page.locator('#monitor-tab-stations').click()
                page.locator('#station-search').fill('')
                page.wait_for_timeout(150)
                previous_page = page.locator('#monitorPagination [data-page-step="-1"]')
                while previous_page.is_enabled():
                    previous_page.click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1 && document.documentElement.scrollHeight <= innerHeight + 1'), (width, height, 'table page overflow')
                page.locator('[data-table-visual]:visible button').first.scroll_into_view_if_needed()
                for thumb in page.locator('[data-table-visual]:visible button[data-visual-thumbnail]').all():
                    thumb.frame_locator('iframe').locator('svg').wait_for()
                page.screenshot(path=str(OUT / f'{width}-{height}-table.png'))
                score_button = page.locator('#list-wrap [data-score-station]:visible').first
                score_button.scroll_into_view_if_needed()
                score_button.click()
                assert page.locator('#scoreSummary').inner_text() == '待评估'
                assert '0 / 100' in page.locator('#scoreFacts').inner_text()
                bounds = page.locator('#healthScoreDialog').bounding_box()
                assert bounds['x'] >= 0 and bounds['y'] >= 0 and bounds['x'] + bounds['width'] <= width + 1 and bounds['y'] + bounds['height'] <= height + 1
                assert page.locator('#healthScoreDialog').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
                if width in (1366, 390):
                    page.screenshot(path=str(OUT / f'{width}-{height}-health-score.png'))
                page.locator('#scoreClose').click()
                assert page.locator('#list-wrap .table-actions:visible').evaluate_all('''nodes => nodes.every(node => {
                    const buttons = [...node.querySelectorAll('button')].map(b => b.getBoundingClientRect());
                    const cell = node.closest('td').getBoundingClientRect();
                    return buttons.length === 2 && Math.abs(buttons[0].top - buttons[1].top) < 1
                        && buttons[1].right <= cell.right;
                })'''), (width, height, 'station actions wrapped or overflowed')
                first_rows = page.locator('[data-table-visual]:visible').evaluate_all('nodes => nodes.map(n => n.dataset.tableVisual)')
                next_page = page.locator('#monitorPagination [data-page-step="1"]')
                if next_page.is_enabled():
                    next_page.click()
                    next_rows = page.locator('[data-table-visual]:visible').evaluate_all('nodes => nodes.map(n => n.dataset.tableVisual)')
                    assert next_rows and set(first_rows).isdisjoint(next_rows), (width, height, 'table pagination')
                page.locator('#monitor-tab-visual').click()
                page.locator('#visualGallerySearch').fill('')
                page.wait_for_timeout(150)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1 && document.documentElement.scrollHeight <= innerHeight + 1'), (width, height, 'page overflow')
                assert page.locator('#visualGalleryGrid').evaluate('e => e.scrollHeight <= e.clientHeight + 1'), (width, height, 'gallery overflow')
                assert page.locator('.visual-gallery-card').count() <= 6
                page.screenshot(path=str(OUT / f'{width}-{height}-gallery.png'))
                first_page = page.locator('.visual-gallery-card h3').all_text_contents()
                if page.locator('#visualGalleryNext').is_enabled():
                    page.locator('#visualGalleryNext').click()
                    second_page = page.locator('.visual-gallery-card h3').all_text_contents()
                    assert second_page and set(first_page).isdisjoint(second_page), (width, height, 'pagination')
                    page.locator('#visualGalleryPrev').click()
                    assert page.locator('.visual-gallery-card h3').all_text_contents() == first_page
                page.locator('#visualGallerySearch').fill('Visual QA')
                page.locator('.visual-gallery-card [data-channel-models]').click()
                assert '42 / 42' in page.locator('#channelModelsCount').inner_text()
                catalog = page.locator('#channelModelsDialog').bounding_box()
                assert catalog['x'] >= -1 and catalog['y'] >= -1 and catalog['x'] + catalog['width'] <= width + 1 and catalog['y'] + catalog['height'] <= height + 1
                assert page.locator('#channelModelsList').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
                page.locator('#channelModelsList>div').last.scroll_into_view_if_needed()
                assert page.locator('#channelModelsList>div').count() == 42
                if width == 390:
                    page.screenshot(path=str(OUT / f'{width}-{height}-model-catalog.png'))
                page.locator('#channelModelsClose').click()
                entry.click()
                page.wait_for_function('!document.querySelector("#visualTestRun").disabled')
                bounds = page.locator('#visualTestDialog').bounding_box()
                assert bounds['x'] >= -1 and bounds['y'] >= -1 and bounds['width'] <= width + 1 and bounds['height'] <= height + 1, (width, height, bounds)
                fits = page.locator('#visualTestDialog').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1')
                assert fits, (width, height, 'dialog overflow')
                assert page.locator('#visualTestClose').evaluate('''button => {
                    const box = button.getBoundingClientRect();
                    const icon = button.querySelector('svg').getBoundingClientRect();
                    return box.width >= 36 && box.height >= 36
                        && Math.abs(box.x + box.width / 2 - icon.x - icon.width / 2) < 1
                        && Math.abs(box.y + box.height / 2 - icon.y - icon.height / 2) < 1;
                }'''), (width, height, 'close icon alignment')
                run = page.locator('#visualTestRun').bounding_box()
                assert run and run['y'] + run['height'] <= height + 1
                page.screenshot(path=str(OUT / f'{width}-{height}-config.png'))
                page.locator('[data-visual-workspace="result"]').click()
                preview = page.locator('#visualPreviewFrame').bounding_box()
                assert preview and preview['width'] > 200 and preview['height'] > 100, (width, height, preview)
                page.screenshot(path=str(OUT / f'{width}-{height}-preview.png'))
            assert errors == [], errors
            assert external == [], external
            assert len(calls) == 2
            browser.close()
    finally:
        release_generation.set()
        server.should_exit = True
        thread.join(10)
        sock.close()
    assert not thread.is_alive()
print('visual-test browser workflow passed; 8 viewports; 2 mocked generations; 0 external requests')
