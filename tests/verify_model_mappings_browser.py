"""Isolated mapping editor and viewport checks. No credentials or upstream calls."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import AppConfig, GatewayConfig, StationConfig
from gateway import build_app

OUT = ROOT / 'data/modelharbor-qa/mappings'
OUT.mkdir(parents=True, exist_ok=True)
errors, external, checks = [], [], []
with tempfile.TemporaryDirectory() as directory:
    app = build_app(AppConfig(gateway=GatewayConfig(data_dir=directory, allowed_ips=['127.0.0.1']), stations=[
        StationConfig(name='OpenCode Go', base_url='https://mock.invalid/v1', api_key='fixture', models=['deepseek-v4.1-flash', 'glm-5.3']),
        StationConfig(name='DeepSeek Backup', base_url='https://mock.invalid/v1', api_key='fixture', models=['deepseek-chat'])]))
    async def quiet_health():
        await asyncio.Event().wait()
    app.state.accounts.active.monitor.run = quiet_health
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
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
            page = browser.new_page(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce')
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda req: external.append(req.url) if req.url.startswith(('http:', 'https:')) and not req.url.startswith(base + '/') else None)
            page.goto(base + '/dashboard')
            page.locator('#tab-stations').click()
            page.locator('#stations-tab-mappings').click()
            page.locator('#mapping-empty-add').wait_for()
            page.locator('#mapping-empty-add').click()
            page.locator('#mapping-name').fill('GPT 请求交给 DeepSeek')
            page.locator('#mapping-model').fill('gpt-6-sol')

            def choose(select, label):
                select.locator('..').locator('button').click()
                page.locator('.mh-select-popup:not([hidden])').get_by_role('option', name=label, exact=True).click()

            choose(page.locator('[data-target-station]').first, 'OpenCode Go')
            choose(page.locator('[data-target-model]').first, 'deepseek-v4.1-flash')
            page.locator('#mapping-target-add').click()
            choose(page.locator('[data-target-station]').nth(1), 'DeepSeek Backup')
            choose(page.locator('[data-target-model]').nth(1), 'deepseek-chat')
            page.locator('#mapping-save').click()
            page.wait_for_function('!document.querySelector("#mapping-dialog").open')
            assert page.locator('.mapping-rule').count() == 1
            assert 'deepseek-v4.1-flash' in page.locator('.mapping-rule').inner_text()
            # Reload proves the editor saved, not merely rendered an optimistic row.
            page.reload()
            page.locator('#tab-stations').click()
            page.locator('#stations-tab-mappings').click()
            page.locator('.mapping-rule').wait_for()
            assert app.state.accounts.active.registry.model_mappings.rules[0]['source_model'] == 'gpt-6-sol'
            # Add a more specific rule using the same source model.
            page.locator('#mapping-add').click()
            page.locator('#mapping-name').fill('指定客户端')
            page.locator('#mapping-model').fill('gpt-6-sol')
            choose(page.locator('#mapping-scope'), '指定 IP')
            page.locator('#mapping-ips').fill('192.0.2.10\n192.0.2.11')
            choose(page.locator('[data-target-station]'), 'OpenCode Go')
            choose(page.locator('[data-target-model]'), 'deepseek-v4.1-flash')
            page.locator('#mapping-save').click()
            page.wait_for_function('!document.querySelector("#mapping-dialog").open')
            assert page.locator('.mapping-rule').count() == 2
            for width, height in [(1920,1080),(1440,900),(1366,768),(1280,720),(1024,768),(768,1024),(390,844),(360,640)]:
                page.set_viewport_size({'width': width, 'height': height})
                page.wait_for_timeout(180)
                size = page.evaluate('({width:innerWidth,height:innerHeight,scrollW:document.documentElement.scrollWidth,scrollH:document.documentElement.scrollHeight})')
                assert size['scrollW'] <= width + 1, size
                assert size['scrollH'] <= height + 1, size
                page.screenshot(path=str(OUT / f'list-{width}.png'))
                page.locator('[data-mapping-action="edit"]').first.click()
                dialog = page.locator('#mapping-dialog').bounding_box()
                assert dialog['x'] >= 0 and dialog['y'] >= 0 and dialog['x'] + dialog['width'] <= width + 1 and dialog['y'] + dialog['height'] <= height + 1, dialog
                save = page.locator('#mapping-save').bounding_box()
                assert save['y'] + save['height'] <= height, save
                page.screenshot(path=str(OUT / f'editor-{width}.png'))
                page.locator('#mapping-close').click()
                checks.append([width, height, 'pass'])
            page.set_viewport_size({'width': 1440, 'height': 900})
            page.locator('[data-mapping-action="toggle"]').first.click()
            page.wait_for_function('document.querySelector(".mapping-state").textContent === "已停用"')
            page.locator('[data-mapping-action="edit"]').first.click()
            page.on('dialog', lambda dialog: dialog.accept())
            page.locator('#mapping-delete').click()
            page.wait_for_function('!document.querySelector("#mapping-dialog").open')
            assert page.locator('.mapping-rule').count() == 1
            assert not errors, errors
            assert not external, external
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
print(json.dumps({'viewports': checks, 'page_errors': errors, 'external_requests': external}))
