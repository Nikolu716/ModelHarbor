"""Exercise shared selectors in the real browser; isolated data, no upstream I/O."""
import asyncio
import os
import socket
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

OUT = ROOT / "data/modelharbor-qa/selects"
OUT.mkdir(parents=True, exist_ok=True)
errors, external = [], []

with tempfile.TemporaryDirectory() as directory:
    app = build_app(AppConfig(
        gateway=GatewayConfig(data_dir=directory, allowed_ips=["127.0.0.1"]),
        stations=[StationConfig(name="Selector QA", base_url="https://mock.invalid",
                                api_key="fixture-only", models=["model-" + str(i) for i in range(40)]),
                  StationConfig(name="Paused QA", base_url="https://paused.invalid",
                                api_key="fixture-only", models=["paused-model"], enabled=False, probe_models=False)]))
    async def quiet_health():
        await asyncio.Event().wait()
    app.state.accounts.active.monitor.run = quiet_health
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = "http://127.0.0.1:" + str(port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)
    assert server.started
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get("MODELHARBOR_TEST_BROWSER", "msedge"))
            context = browser.new_context(viewport={"width":1440, "height":900}, reduced_motion="reduce")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            def guard(route):
                if route.request.url.startswith(origin + "/"):
                    route.continue_()
                else:
                    external.append(route.request.url)
                    route.abort()
            page.route("**/*", guard)
            page.goto(origin + "/dashboard")
            page.wait_for_function("Boolean(window.ModelHarborSelects)")
            page.wait_for_function("document.querySelector('#station-enabled-count').textContent === '1'")
            for width, height in [(1920,1080),(1440,900),(1366,768),(1280,720),(1024,768),(768,1024),(390,844)]:
                page.set_viewport_size({"width":width, "height":height})
                page.locator("#station-scope-disabled").click()
                assert page.locator("#list-wrap .station-name-text").all_text_contents() == ["Paused QA"]
                assert page.locator("#stationHealthFilter").is_hidden()
                assert page.locator("#station-disabled-count").inner_text() == "1"
                page.locator("#station-search").fill("no-match")
                assert page.locator("#list-wrap .empty-cell").is_visible()
                page.locator("#station-search").fill("paused-model")
                assert page.locator("#list-wrap .station-name-text").all_text_contents() == ["Paused QA"]
                page.locator("#station-search").fill("")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                assert page.locator("#stationScopeTabs").evaluate("el => {const r=el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight;}")
                page.screenshot(path=str(OUT / (str(width) + "-paused-stations.png")))
                page.locator("#station-scope-disabled").press("ArrowLeft")
                assert page.locator("#list-wrap .station-name-text").all_text_contents() == ["Selector QA"]
                assert page.locator("#stationHealthFilter").is_visible()
            page.set_viewport_size({"width":1440,"height":900})
            page.locator("#station-scope-disabled").click()
            page.locator('#list-wrap [data-action="toggle"]').click()
            page.wait_for_function("document.querySelector('#station-disabled-count').textContent === '0'")
            assert page.locator("#list-wrap .empty-cell").is_visible()
            page.locator("#station-scope-enabled").click()
            assert page.locator("#list-wrap .station-name-text").count() == 2
            page.locator('#list-wrap [data-action="toggle"][data-station="Paused QA"]').click()
            page.wait_for_function("document.querySelector('#station-disabled-count').textContent === '1'")
            assert page.locator("#list-wrap .station-name-text").all_text_contents() == ["Selector QA"]
            page.locator("#tab-stations").click()
            page.locator("#stations-add").click()

            def button(selector):
                return page.locator(selector).locator("..").locator(".mh-select-trigger")

            def assert_bounds():
                result = page.locator(".mh-select-popup:visible").evaluate("""el => {
                    const r = el.getBoundingClientRect();
                    return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,w:innerWidth,h:innerHeight};
                }""")
                assert result["left"] >= 0 and result["right"] <= result["w"] + 1, result
                assert result["top"] >= 0 and result["bottom"] <= result["h"] + 1, result

            for width, height in [(1920,1080),(1440,900),(1366,768),(1280,720),(1024,768),(768,1024),(390,844)]:
                page.set_viewport_size({"width":width, "height":height})
                button("#sf-provider").click()
                assert_bounds()
                page.screenshot(path=str(OUT / (str(width) + "-provider.png")))
                page.locator(".mh-select-popup:visible [role=option]").filter(has_text="OpenCode Go").click()
                assert page.locator("#sf-opencode-plan").input_value() == "go"
                assert page.locator("#sf-base-url").input_value() == "https://opencode.ai/inference/go/openai/v1"
                assert button("#sf-opencode-plan").inner_text() == "Go 订阅"
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                button("#sf-opencode-plan").click()
                assert_bounds()
                page.keyboard.press("Escape")
                assert page.locator(".mh-select-popup:visible").count() == 0
                assert button("#sf-opencode-plan").evaluate("el => el === document.activeElement")
                # Keyboard selection must propagate the form change exactly once.
                button("#sf-provider").press("ArrowUp")
                page.keyboard.press("Home")
                page.keyboard.press("Enter")
                assert page.locator("#sf-provider").input_value() == "openai"
                assert page.locator("#sf-opencode-plan-field").is_hidden()

            page.set_viewport_size({"width":1366, "height":768})
            for value, label, address in [("anthropic", "Anthropic Messages", "https://api.anthropic.com/v1"),
                                           ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta")]:
                button("#sf-provider").click()
                page.locator(".mh-select-popup:visible [role=option]").filter(has_text=label).click()
                assert page.locator("#sf-provider").input_value() == value
                assert page.locator("#sf-base-url").input_value() == address
                assert not page.locator("#sf-base-url").get_attribute("readonly")
                assert page.locator("#sf-provider-note").is_visible()
                page.screenshot(path=str(OUT / (value + "-editor.png")))
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#tab-monitor").click()
            page.locator('[data-table-visual="Selector QA"] button').click()
            page.wait_for_function("!document.querySelector('#visualTestRun').disabled")
            button("#visualTestModel").click()
            search = page.locator(".mh-select-popup:visible .mh-select-search")
            search.fill("model-39")
            assert page.locator(".mh-select-popup:visible [role=option]").count() == 1
            assert_bounds()
            page.screenshot(path=str(OUT / "1366-search-model.png"))
            search.press("Enter")
            assert page.locator("#visualTestModel").input_value() == "model-39"
            assert button("#visualTestModel").inner_text() == "model-39"
            button("#visualTestModel").click()
            search.fill("no-such-option")
            assert page.locator(".mh-select-empty").inner_text() == "没有匹配的选项"
            page.keyboard.press("Escape")
            assert page.locator("#visualTestDialog").is_visible()
            page.evaluate("document.querySelector('#visualTestModel').value = 'model-7'")
            page.wait_for_function("document.querySelector('#visualTestModel').nextElementSibling.textContent === 'model-7'")
            page.evaluate("document.querySelector('#visualTestModel').disabled = true")
            page.wait_for_function("document.querySelector('#visualTestModel').nextElementSibling.disabled")
            assert button("#visualTestModel").is_disabled()
            page.locator("#visualTestClose").click()
            assert not errors, errors
            assert not external, external
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
print("shared dropdown browser checks passed; 7 viewports; keyboard/search/dialog/state; 0 external requests")
