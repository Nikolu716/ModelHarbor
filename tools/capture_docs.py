"""Capture README screenshots from isolated synthetic data; no provider requests.

Requires Playwright + Chromium, or MODELHARBOR_TEST_BROWSER=msedge/chrome.
No local configuration or production data is read.
"""
from __future__ import annotations

import asyncio
import os
import random
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import AppConfig, GatewayConfig, StationConfig
from app.go_usage import normalize_usage
from gateway import build_app

OUT = ROOT / 'docs/screenshots'
CHANNELS = [
    ('Harbor OpenAI', 'openai', ['demo-coder', 'demo-chat']),
    ('OpenCode Go', 'opencode', ['demo-coder', 'demo-reasoner']),
    ('Cedar Anthropic', 'anthropic', ['demo-sonnet', 'demo-haiku']),
    ('Atlas Gemini', 'gemini', ['demo-pro', 'demo-flash']),
]


def artifact(color: str) -> str:
    return '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 520">
<rect width="800" height="520" fill="#f4f2ec"/>
<circle cx="625" cy="115" r="58" fill="#e7cf99"/>
<path d="M0 414Q180 344 355 402T800 385V520H0Z" fill="#e1e8df"/>
<path d="M125 429H679" stroke="#aebdb3" stroke-width="3"/>
<g fill="none" stroke="#345d58" stroke-width="7">
<circle cx="240" cy="350" r="77"/><circle cx="563" cy="350" r="77"/>
<path d="m240 350 100-146 68 146H240l144-106h107l72 106M332 204h54m96 40-17-35h42"/>
</g><path d="M340 213c-50-96 24-160 95-109l98 15-98 27c15 61-43 100-95 67Z" fill="COLOR"/>
<path d="m435 104 98 15-98 27" fill="#d59c52"/>
<path d="M341 211q42 35 72-17" fill="none" stroke="#ffffff" stroke-width="7"/>
<circle cx="420" cy="111" r="5" fill="#263d38"/>
<path d="m383 245 26 45-12 49h31" fill="none" stroke="#d59c52" stroke-width="8" stroke-linecap="round"/>
<text x="60" y="72" font-family="sans-serif" font-size="16" letter-spacing="4" fill="#61746a">A LITTLE BALANCE</text>
<text x="60" y="479" font-family="sans-serif" font-size="13" fill="#7b887e">ModelHarbor · synthetic preview</text>
</svg>'''.replace('COLOR', color)


def capture() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    errors, external, upstream = [], [], []
    with tempfile.TemporaryDirectory(prefix='modelharbor-docs-') as directory:
        stations = [StationConfig.from_dict(name, {
            'provider': provider, 'opencode_plan': 'go',
            'base_url': 'https://opencode.ai' if provider == 'opencode' else 'https://demo.invalid/v1',
            'api_key': 'fixture-only',
            'models': models, 'probe_models': True, 'probe_chat': False,
            'priority_auto': True,
        }) for name, provider, models in CHANNELS]
        app = build_app(AppConfig(gateway=GatewayConfig(
            data_dir=directory, allowed_ips=['127.0.0.1'],
            public_url='https://gateway.example.com', generation_probe_enabled=False),
            stations=stations))
        runtime = app.state.accounts.active

        async def quiet_health():
            await asyncio.Event().wait()

        async def deny_upstream(request):
            upstream.append(request.url.host)
            raise AssertionError('Documentation capture must not call providers')

        runtime.monitor.run = quiet_health
        now = time.time()
        rng = random.Random(17)
        colors = ['#94ada3', '#e1c58d', '#a9b9d0', '#c6b3c9']
        for i, (name, _, models) in enumerate(CHANNELS):
            st = runtime.registry.get(name)
            st.client = httpx.AsyncClient(transport=httpx.MockTransport(deny_upstream))
            st.calls = 216 + i * 37
            st.failures = i + 1
            st.ema_latency = 1.2 + i * .35
            st.ttft_ema = .38 + i * .15
            st.health_samples.extend(dict(at=now - (100 - j) * 60, ok=j != 97,
                model=models[j % 2], path='/responses', stream=True, latency=1.2 + i * .35)
                for j in range(100))
            st.last_used_at = now - i * 8
            st.probed_models = set(models)
            st.probed_model_order = models[:]
            st.model_api_paths = {model: ['/chat/completions', '/responses'] for model in models}
            st.models_discovery_status = 'ready'
            st.models_discovered_at = st.models_checked_at = now
            st.history_minute = int(now // 60)
            st.history_ok = [rng.randint(1, 7) for _ in range(60)]
            st.history_fail = [0] * 60
            st._update_score()
            svg = artifact(colors[i])
            runtime.visual_scheduler.store.save_result(st, dict(
                id=f'docs-{i}', station=name, model=models[0], protocol='chat_completions',
                created_at=now - 60 * i, artifact=svg, raw_text=svg, kind='svg',
                ok=True, truncated=False, error='', http_status=200,
                latency_ms=4200 + i * 180, usage={'input_tokens': 64, 'output_tokens': 920, 'total_tokens': 984},
                prompt='Draw a pelican riding a bicycle as SVG.', template_name='演示：骑车的鹈鹕',
                max_output_tokens=4096, reasoning_effort='auto'))
        go = runtime.registry.get('OpenCode Go')
        reset = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        go.go_usage = normalize_usage({'usage': {key: {
            'percent': value, 'status': 'ok', 'resetsAt': reset,
        } for key, value in [('rolling', 32), ('weekly', 18), ('monthly', 9)]}})
        go.go_usage_status = 'ready'
        go.go_usage_updated_at = now
        for day in range(6, -1, -1):
            for n in range(28 + (6 - day) * 5):
                idx = rng.choices(range(4), weights=[5, 3, 2, 2])[0]
                name, _, models = CHANNELS[idx]
                runtime.eventlog.add(name, models[n % 2], n % 31 != 0, 1100 + rng.randint(0, 1600),
                    tokens=1500 + n * 83, input_tokens=900 + n * 12,
                    cache_read=360, client_ip=['192.0.2.10', '198.51.100.20', '203.0.113.30'][n % 3],
                    occurred_at=now - day * 86400 - (60 - n) * 60)
        route = runtime.registry.begin_route('Harbor OpenAI', 'demo-coder', '192.0.2.10')
        runtime.registry.update_route_phase(route, 'streaming')
        route = runtime.registry.begin_route('OpenCode Go', 'demo-reasoner', '198.51.100.20')
        runtime.registry.update_route_phase(route, 'streaming')
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
        assert server.started, 'Demo server failed to start'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, channel=os.environ.get('MODELHARBOR_TEST_BROWSER'))
                page = browser.new_page(viewport={'width': 1440, 'height': 960},
                                        device_scale_factor=1, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))

                def guard(route):
                    if route.request.url.startswith(origin + '/'):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()

                page.route('**/*', guard)
                page.goto(origin + '/dashboard')
                page.locator('[data-channel-models="Harbor OpenAI"]').first.wait_for()
                page.wait_for_timeout(1200)
                page.screenshot(path=str(OUT / 'overview.png'))
                page.locator('#tab-stats').click()
                page.locator('#stats-tab-trend').click()
                page.locator('#stats-trend canvas').first.wait_for()
                page.wait_for_timeout(800)
                page.screenshot(path=str(OUT / 'usage.png'))
                page.locator('#tab-events').click()
                page.locator('#events-tab-analytics').click()
                page.locator('#eventBreakdownLegend').get_by_text('192.0.2.10', exact=False).wait_for()
                page.wait_for_timeout(800)
                page.screenshot(path=str(OUT / 'requests.png'))
                page.locator('#tab-monitor').click()
                page.locator('#monitor-tab-visual').click()
                page.locator('.visual-gallery-card').first.wait_for()
                page.wait_for_timeout(1000)
                page.screenshot(path=str(OUT / 'visual-tests.png'))
                assert not errors, errors
                assert not external, external
                assert not upstream, upstream
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    for name in ('overview.png', 'usage.png', 'requests.png', 'visual-tests.png'):
        path = OUT / name
        with Image.open(path) as screenshot:
            clean = Image.new('RGB', screenshot.size)
            clean.paste(screenshot)
            clean.save(path, optimize=True)
    print('Captured 4 synthetic screenshots; 0 provider calls; 0 external browser requests.')


if __name__ == '__main__':
    capture()
