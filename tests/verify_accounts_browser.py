"""Isolated real-browser account flow and responsive layout checks. No upstream traffic."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import AppConfig, GatewayConfig, StationConfig
from gateway import build_app

OUT = ROOT / 'data/modelharbor-qa'
OUT.mkdir(parents=True, exist_ok=True)
errors = []
checks = []
failures = []

with tempfile.TemporaryDirectory() as directory:
    cfg = AppConfig(gateway=GatewayConfig(data_dir=directory,
                    allowed_ips=['127.0.0.1', '10.1.1.1', '10.2.2.2']), stations=[
        StationConfig(name='QA station', base_url='https://never-contact.invalid/v1',
                      api_key='fixture-only', models=['model-b', 'model-a'] +
                      [f'catalog-model-{i:02}' for i in range(36)], enabled=False),
        StationConfig(name='Compact station', base_url='https://never-contact.invalid/v1',
                      api_key='fixture-only', models=['first-model', 'second-model'], enabled=False)])
    app = build_app(cfg)
    log = app.state.accounts.active.eventlog
    for i in range(36):
        log.add('QA station', 'model-a' if i < 8 else 'model-b', True, 300,
                tokens=None if i % 5 == 0 else 10000 + i*250, input_tokens=8000, cache_read=6000,
                client_ip='10.1.1.1' if i < 12 else f'10.2.2.{i}')
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=18082, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)
    assert server.started
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get('MODELHARBOR_TEST_BROWSER'))
            ctx = browser.new_context(viewport={'width':1366,'height':768}, reduced_motion='reduce')
            page = ctx.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto('http://127.0.0.1:18082/dashboard')
            page.wait_for_function('!document.querySelector("#accountMenuToggle").disabled')
            assert page.locator('#tab-accounts').count() == 0
            assert page.locator('#tab-service').is_visible()

            def open_menu():
                if page.locator('#accountMenu').is_hidden():
                    page.locator('#accountMenuToggle').click()

            def select_account(account_id):
                open_menu()
                page.locator(f'[data-account-action="switch"][data-id="{account_id}"]').click()

            def wait_account(account_id):
                page.wait_for_function('id => window.modelHarborAccount?.id === id && !document.querySelector("#accountMenuToggle").disabled', arg=account_id)

            def edit_account(account_id):
                open_menu()
                page.locator(f'[data-account-action="edit"][data-id="{account_id}"]').click()
                page.locator('#accountEditorDialog').wait_for(state='visible')

            open_menu()
            page.keyboard.press('Escape')
            assert page.locator('#accountMenu').is_hidden()
            page.locator('#accountMenuToggle').press('ArrowDown')
            assert page.locator('#accountMenu').is_visible()
            page.locator('#pageTitle').click()
            assert page.locator('#accountMenu').is_hidden()
            page.locator('#tab-stations').click()
            page.locator('[data-station-row="Compact station"] [data-manage-action="models"]').click()
            page.wait_for_function('document.querySelector("#sf-model-count").textContent === "2"')
            assert page.locator('#sf-probe-current').inner_text() == 'first-model'
            # A short catalog should use only its content height.
            assert page.locator('#sf-model-list').evaluate('(el) => el.clientHeight') < 95
            page.locator('[data-probe-model="second-model"]').click()
            assert not page.locator('#sf-probe-model-auto').is_checked()
            assert page.locator('#sf-probe-current').inner_text() == 'second-model'
            page.locator('#sf-submit').click()
            page.wait_for_function('document.querySelector("#stations-list").hidden === false')
            page.locator('[data-station-row="Compact station"] [data-manage-action="models"]').click()
            assert page.locator('#sf-probe-current').inner_text() == 'second-model'
            page.locator('#sf-probe-model-auto').check()
            assert page.locator('#sf-probe-current').inner_text() == 'first-model'
            page.locator('#sf-submit').click()
            page.wait_for_function('document.querySelector("#stations-list").hidden === false')
            page.locator('[data-station-row="QA station"] [data-manage-action="models"]').click()
            assert page.locator('#sf-probe-current').inner_text() == 'model-b'
            page.screenshot(path=str(OUT/'1366-station-models.png'))
            page.locator('#sf-cancel').click()
            # Save and reload an OpenCode preset without enabling external traffic.
            page.locator('#stations-add').click()
            page.locator('#sf-provider').select_option('opencode')
            page.locator('#sf-api-key').fill('fixture-opencode-key')
            assert page.locator('#sf-base-url').input_value() == 'https://opencode.ai/inference/go/openai/v1'
            page.locator('#editor-tab-routing').click()
            page.locator('#sf-enabled').uncheck()
            page.locator('#sf-route-models').fill('gpt-*, deepseek-*')
            page.locator('#sf-submit').click()
            page.wait_for_function('document.querySelector("#stations-list").hidden === false')
            saved = next(st for st in page.request.get('http://127.0.0.1:18082/admin/stations').json() if st['name'] == 'OpenCode Go')
            assert saved['provider'] == 'opencode' and saved['model_routing'] == 'catalog'
            assert saved['route_models'] == ['gpt-*', 'deepseek-*']
            assert saved['probe_interval'] == 3600 and not saved['probe_chat'] and not saved['enabled']
            page.locator('[data-station-row="OpenCode Go"] [data-manage-action="edit"]').click()
            assert page.locator('#sf-provider').input_value() == 'opencode'
            assert page.locator('#sf-api-key').input_value() == ''
            page.locator('#editor-tab-routing').click()
            assert page.locator('#sf-route-models').input_value() == 'gpt-*, deepseek-*'
            page.locator('#sf-cancel').click()
            assert page.request.delete('http://127.0.0.1:18082/admin/stations/OpenCode%20Go').status == 200
            page.locator('#stations-refresh').click()
            open_menu()
            page.locator('#accountAdd').click()
            assert page.locator('#accountIps').input_value() == ''
            page.locator('#accountDisplay').fill('Design Team')
            page.locator('#accountUsername').fill('design-team')
            page.locator('#accountPassword').fill('fixture-password')
            page.locator('#accountRequirePassword').check()
            page.locator('#accountIps').fill('10.1.1.1\n10.2.2.2')
            page.locator('#accountSave').click()
            page.locator('#accountEditorDialog').wait_for(state='hidden')
            page.wait_for_function('document.querySelectorAll(".account-row").length===2')
            target = page.locator('[data-account-action="switch"]').nth(1).get_attribute('data-id')
            assert target
            select_account(target)
            page.locator('#switchPassword').fill('wrong-password')
            page.locator('#switchConfirm').click()
            page.wait_for_function('document.querySelector("#switchAccountError").textContent.length>0')
            assert page.evaluate('window.modelHarborAccount.id') == 'default'
            assert not page.locator('#authDialog').is_visible()
            page.locator('#switchPassword').fill('fixture-password')
            page.locator('#switchConfirm').click()
            page.wait_for_function('document.querySelector("#switchAccountError").textContent.includes("白名单")')
            assert page.evaluate('window.modelHarborAccount.id') == 'default'
            page.locator('#switchCancel').click()
            edit_account(target)
            assert page.locator('#accountIps').input_value() == '10.1.1.1\n10.2.2.2'
            page.locator('#accountIps').fill('127.0.0.1\n10.2.2.2')
            page.locator('#accountSave').click()
            page.locator('#accountEditorDialog').wait_for(state='hidden')
            select_account(target)
            page.locator('#switchPassword').fill('fixture-password')
            page.locator('#switchConfirm').click()
            wait_account(target)
            page.locator('#tab-stations').click()
            page.wait_for_function('document.querySelector("#stations-body").textContent.includes("暂无") || document.querySelector("#stations-body").textContent.includes("没有")')
            page.locator('#tab-access').click()
            page.wait_for_function('document.querySelector("#access-ips").value === "127.0.0.1\\n10.2.2.2"')
            page.locator('#tab-events').click()
            page.wait_for_function('document.querySelector("#log-body").textContent.includes("暂无事件")')
            assert '10.1.1.1' not in page.locator('#log-body').inner_text()
            select_account('default')
            wait_account('default')
            page.locator('#tab-events').click()
            page.wait_for_function('document.querySelector("#log-body").textContent.includes("10.2.2.35")')
            page.locator('#tab-access').click()
            page.wait_for_function('document.querySelector("#access-ips").value.includes("10.1.1.1")')
            edit_account(target)
            assert page.locator('#accountIps').input_value() == '127.0.0.1\n10.2.2.2'
            assert page.locator('#accountPassword').input_value() == ''
            page.locator('#accountRequirePassword').uncheck()
            page.locator('#accountSave').click()
            page.locator('#accountEditorDialog').wait_for(state='hidden')
            select_account(target)
            wait_account(target)
            assert not page.locator('#accountSwitchDialog').is_visible()
            select_account('default')
            wait_account('default')

            page.locator('#tab-service').click()
            page.wait_for_function('!document.querySelector("#service-port").disabled')
            page.locator('#endpoint-public-url').fill('https://gateway.example/api/v1')
            assert page.locator('#endpoint-save').is_enabled()
            page.locator('#endpoint-save').click()
            page.wait_for_timeout(500)
            assert page.locator('#endpoint-preview').inner_text() == 'https://gateway.example/api/v1', page.locator('#endpoint-status').inner_text()
            assert page.request.get('http://127.0.0.1:18082/admin/settings/endpoint').json()['public_url'] == 'https://gateway.example/api'
            select_account(target)
            wait_account(target)
            assert page.request.get('http://127.0.0.1:18082/admin/settings/endpoint').json()['endpoint'] == 'https://gateway.example/api/v1'
            select_account('default')
            wait_account('default')
            page.locator('#tab-events').click()
            page.locator('#events-tab-sources').click()
            page.wait_for_function('document.querySelector("#eventSourceRows").textContent.includes("10.1.1.1")')
            assert page.locator('#events-sources').is_visible()
            assert page.locator('#events-records').is_hidden()
            assert '10.1.1.1' in page.locator('#eventSourceRows').inner_text()
            first_rows = page.locator('#eventSourceRows').inner_text()
            page.locator('#eventSourceNext').click()
            assert page.locator('#eventSourceRows').inner_text() != first_rows
            page.locator('#eventIpFilter').select_option('10.1.1.1')
            page.wait_for_function('document.querySelector("#eventSourceRows").children.length === 2')
            assert page.locator('#eventSourceNext').is_disabled()
            page.locator('#eventIpFilter').select_option('')
            page.locator('#eventMetric').select_option('tokens')
            page.locator('#events-tab-analytics').click()
            page.wait_for_function('document.querySelector("#eventTrendTitle").textContent.includes("Token")')

            pages = {'monitor':['stations','history'], 'stats':['trend','models','today','cache','timeline'],
                     'stations':['list','editor','models','routing','retry:switch','retry:rounds','retry:duration','defaults'],
                     'access':[None], 'service':[None],
                     'events':['records','records:filtered','analytics','analytics:filtered','sources']}
            viewports = [(2560,1440),(1920,1080),(1440,900),(1366,768),(1280,720),(1024,768),(768,1024),(390,844),(375,667),(320,568),(844,390)]
            if os.environ.get('MODELHARBOR_QA_VIEWPORTS'):
                viewports = [tuple(map(int, size.split('x'))) for size in os.environ['MODELHARBOR_QA_VIEWPORTS'].split(',')]
            for width,height in viewports:
                page.set_viewport_size({'width':width,'height':height})
                for tab,sections in pages.items():
                    page.locator('#tab-'+tab).click()
                    for section in sections:
                        if section:
                            if tab == 'events':
                                event_section, _, filtered = section.partition(':')
                                page.wait_for_timeout(70)
                                page.locator('#events-tab-'+event_section).click()
                                if page.locator('#events-wrap').get_attribute('data-layout') != 'combined':
                                    expanded = page.locator('#eventFiltersToggle').get_attribute('aria-expanded') == 'true'
                                    if expanded != bool(filtered):
                                        page.locator('#eventFiltersToggle').click()
                            elif section.startswith('retry:'):
                                page.locator('#stations-tab-retry').click()
                                page.locator('#retry-tab-'+section.split(':')[1]).click()
                            elif section == 'defaults':
                                page.locator('#stations-tab-probe').click()
                            elif tab == 'stations' and section == 'editor':
                                page.locator('#stations-tab-list').click()
                                page.wait_for_timeout(150)
                                if not page.locator('[data-station-row="QA station"]').is_visible():
                                    page.locator('#stationsPagination [data-page-step="1"]').click()
                                page.locator('[data-station-row="QA station"] [data-manage-action="edit"]').click()
                            else:
                                editor_section = tab == 'stations' and section in ('models','routing')
                                section_id = ('editor' if editor_section else tab) + '-tab-' + section
                                page.locator('#'+section_id).click()
                        page.wait_for_timeout(90)
                        data=page.evaluate('''() => {
                          const root=document.documentElement;
                          const panel=document.querySelector('.tab-panel:not([hidden])');
                          const clipped=[...panel.querySelectorAll('*')].filter(el=>{
                            if(!el.getClientRects().length)return false;
                            const s=getComputedStyle(el);
                            return s.overflowY==='hidden' && el.clientHeight>2 && el.scrollHeight>el.clientHeight+3 && !['station-grid','monitor-viewport'].some(c=>el.classList.contains(c));
                          }).map(el=>({id:el.id,cls:el.className,height:el.clientHeight,scroll:el.scrollHeight}));
                          const actions=['accountSave','sf-submit','retry-save','access-save','eventNext'].filter(id=>{
                            const el=document.getElementById(id);if(!el?.getClientRects().length)return false;
                            const r=el.getBoundingClientRect();return r.top<0||r.bottom>innerHeight+1;
                          });
                          const rect=panel.getBoundingClientRect();
                          const donut=document.querySelector('.donut-stage').getBoundingClientRect();
                          const tabs=document.querySelector('#usageTabs').getBoundingClientRect();
                          const trend=document.querySelector('#stats-trend');
                          return {scrollHeight:root.scrollHeight,scrollWidth:root.scrollWidth,
                                  panelHeight:rect.height,panelBottom:rect.bottom,clipped,actions,chartGap:donut.top-tabs.bottom,
                                  trendScroll:trend.scrollHeight-trend.clientHeight};
                        }''')
                        item={'viewport':[width,height],'tab':tab,'section':section,**data}
                        checks.append(item)
                        if data['scrollHeight']>height+1 or data['scrollWidth']>width+1 or data['panelHeight']<60 or data['panelBottom']>height+1 or data['clipped'] or data['actions']:
                            failures.append(item)
                        if tab=='stats' and section=='models' and data['chartGap']>65:
                            failures.append(item)
                        if tab=='stats' and section=='trend' and width>=1100 and height>=700 and data['trendScroll']>2:
                            failures.append(item)
                        if ((width,height) in [(1920,1080),(1366,768),(390,844),(320,568),(844,390)]
                                and ((tab == 'stats' and section in ('trend','models'))
                                     or (tab == 'stations' and section in ('models', 'editor', 'routing'))
                                     or tab in ('events', 'access', 'service'))):
                            page.screenshot(path=str(OUT/f'{width}x{height}-{tab}-{(section or "settings").replace(":", "-")}.png'))
                for editor_id in (None, 'default'):
                    open_menu()
                    page.screenshot(path=str(OUT/f'{width}x{height}-account-menu.png'))
                    if editor_id:
                        page.locator('#accountEditCurrent').click()
                    else:
                        page.locator('#accountAdd').click()
                    page.locator('#accountEditorDialog').wait_for(state='visible')
                    assert page.locator('#accountIps').is_disabled() == bool(editor_id)
                    data = page.locator('#accountEditorDialog').evaluate('''el => {
                        const r = el.getBoundingClientRect();
                        const save = document.querySelector('#accountSave').getBoundingClientRect();
                        return {left:r.left, right:r.right, top:r.top, bottom:r.bottom,
                                saveBottom:save.bottom, scroll:el.scrollHeight, height:el.clientHeight};
                    }''')
                    item = {'viewport':[width,height], 'tab':'account-dialog', 'section':editor_id, **data}
                    checks.append(item)
                    if data['left'] < 0 or data['right'] > width+1 or data['top'] < 0 or data['bottom'] > height+1 or data['saveBottom'] > height or data['scroll'] > data['height']+2:
                        failures.append(item)
                    if not editor_id:
                        page.screenshot(path=str(OUT/f'{width}x{height}-account-editor.png'))
                    page.locator('#accountCancel').click()
            browser.close()
    finally:
        server.should_exit=True
        thread.join(timeout=10)
    report={'checks':checks,'failures':failures,'errors':errors}
    (OUT/'layout.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'checks':len(checks),'failures':len(failures),'errors':errors}))
    if failures or errors:
        raise SystemExit(1)
