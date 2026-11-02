"""Static and optional live checks for the gateway dashboard."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import tempfile
import urllib.request
from urllib.parse import urlencode, urlsplit
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.stats import EventLog


class DashboardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.inline_handlers: list[str] = []
        self.inline_style_blocks = 0
        self.inline_script_blocks = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"] or "")
        self.inline_handlers.extend(name for name, _ in attrs if name.startswith("on"))
        if tag == "style":
            self.inline_style_blocks += 1
        if tag == "script" and not values.get("src"):
            self.inline_script_blocks += 1


def verify_static() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "dashboard.css").read_text(encoding="utf-8")
    js = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    state = (ROOT / "app" / "state.py").read_text(encoding="utf-8")

    parser = DashboardParser()
    parser.feed(html)
    duplicates = sorted({item for item in parser.ids if parser.ids.count(item) > 1})

    assert not duplicates, f"duplicate HTML ids: {duplicates}"
    assert not parser.inline_handlers, f"inline handlers: {parser.inline_handlers}"
    assert parser.inline_style_blocks == 0, "dashboard CSS must remain external"
    assert parser.inline_script_blocks == 0, "dashboard JS must remain external"
    assert '/static/dashboard.css?v=40' in html
    assert '/static/dashboard.js?v=43' in html
    assert '/static/visual-tests.js?v=11' in html
    assert '/static/visual-tests.css?v=8' in html
    assert '/static/analytics.js?v=8' in html
    assert '/static/brand-logo-source.png?v=4' in html
    assert '/static/favicon.ico?v=4' in html
    assert '/static/brand-mark-180.png?v=4' in html
    assert '统一模型接入与调度' not in html
    assert '<picture class="brand-picture">' in html
    assert 'sidebar-footer' not in html
    assert 'class="table-shell event-shell"' in html
    for required_id in (
        "monitorViewport", "monitorPagination", "monitor-history", "cardSections",
        "stats-models", "stats-today", "stats-cache", "stats-timeline",
        "stations-list", "stations-editor", "stationsPagination",
        "stations-tab-retry", "stations-tab-probe", "sf-probe-model-auto",
        "sf-probe-current", "sf-effective-policy",
        "retry-section-switch", "retry-section-rounds", "retry-section-duration",
        "pageTitle", "summaryStrip", "routingPanel", "station-search", "station-filter",
        "show-details", "monitor-count", "refreshDashboard", "copyEndpoint",
        "brandMark", "routeLive", "routeLabel", "routeList", "routeMeta",
        "donut", "donutValue", "donutLabel", "chartInspector",
        "modelBreakdown", "usageTabs", "stationUsageList",
        "cacheWindowTabs", "cacheRankList", "cacheWindowNote", "cache-heading",
        "events-wrap", "eventPrev", "eventNext", "eventPageInfo",
        "stations-wrap", "station-form", "stationFormStatus",
        "stationListStatus", "stations-add", "stations-refresh", "stations-body",
        "retry-wrap", "retry-form", "retrySettingsStatus", "retry-reload", "retry-save",
        "retry-infinite-rounds", "retry-unlimited-duration", "retry-max-attempts",
        "retry-backoff", "retry-max-rounds", "retry-round-backoff",
        "retry-max-round-backoff", "retry-max-seconds",
        "access-wrap", "access-form", "access-ips", "access-client",
        "access-save", "access-reload", "accessSettingsStatus",
        "service-wrap", "service-form", "service-port", "service-port-state",
        "serviceSettingsStatus", "service-save", "service-reload", "service-restart",
    ):
        assert required_id in parser.ids, f"missing dashboard id: {required_id}"

    assert 'name="color-scheme" content="light"' in html
    assert '???' not in html
    assert "#monitor-wrap:not(.show-details)" in css
    assert "height:100dvh" in css
    assert "function fitViewportContent" in js
    assert "function setSectionTab" in js
    assert "prefers-reduced-motion" in css
    assert "overflow-x: auto" in css
    assert "repeat(60, minmax" in css
    assert "grid-template-columns: repeat(5, 1fr)" in css
    assert "@media (max-width: 620px)" in css
    assert ".donut-segment.is-model" in css
    assert ".usage-tab.is-active" in css
    assert "#events-wrap .event-shell" in css
    assert ".route-live.is-active" in css
    assert ".route-item-model" in css
    assert re.search(r"\.route-list\s*\{[^}]*max-height:\s*78px[^}]*overflow:\s*auto", css)
    assert 'class="event-donut-wrap"><div id="eventBreakdownDonut"' in html
    assert '<span id="eventDonutCenter" class="event-donut-center"' in html
    assert ".station-table" in css
    assert ".station-manage-actions" in css
    assert ".retry-settings-layout" in css
    assert ".settings-toggle input:checked + .toggle-track" in css
    assert ".retry-field-grid" in css
    assert re.search(r"\.station-usage-bar\s*\{[^}]*display:\s*block", css)
    assert ".cache-rank-bar" in css
    assert ".cache-window-tabs" in css
    assert "gradient(" not in css.lower()
    assert not re.search(r"#(?:60a5fa|3b82f6|2563eb|7c3aed|8b5cf6)\b", css, re.I)

    assert "function escapeHtml" in js
    assert "encodeURIComponent(stationName)" in js
    assert "setInterval(" not in js
    assert "pendingActions" in js
    assert "function renderRouteStatus" in js
    assert "function renderTokenChart" in js
    assert "function refreshEvents" in js
    assert "function refreshStations" in js
    assert "function runManagedStationAction" in js
    assert "function refreshRetrySettings" in js
    assert "function saveRetrySettings" in js
    assert 'api("/admin/settings/retry", "PUT", true, body)' in js
    assert '$("retry-infinite-rounds").checked' in js
    assert '$("retry-unlimited-duration").checked' in js
    assert "return api(path, method, false, body)" in js
    assert 'new URLSearchParams({' in js
    assert '/admin/logs?${query.toString()}' in js
    analytics = (STATIC / "analytics.js").read_text(encoding="utf-8")
    assert '/admin/logs/analytics?' in analytics
    assert '/admin/settings/logs' in analytics
    assert 'id="eventTrendChart"' in html
    assert '/static/vendor/echarts.min.js?v=5.6.0' in html
    assert 'id="eventTrendChart" role="img"' in html
    assert 'type: "line"' in analytics
    assert 'type: "pie"' in analytics
    assert "未记录 IP" in analytics and "未记录 IP" in js
    assert 'id="eventBreakdownDonut"' in html
    assert 'id="eventIpFilter"' in html
    assert 'id="eventModelFilter"' in html
    assert 'id="eventRetentionLimit"' in html
    assert "data-chart-kind" in js
    assert "已统计 Token" in js
    assert "缓存命中率" in js
    assert "function formatHitRate" in js
    assert "function renderCacheCompare" in js
    assert "最佳1小时" in js
    assert "function quotaRecoveryHint" in js
    assert "立即探测恢复" in js
    assert "当前使用" not in js
    assert "正在使用" in js and "最近使用" in js
    assert "station.calls / totalCalls" not in js
    assert "balance_alert_threshold" in state
    assert "route_activity" in state

    print("dashboard static checks passed")


def verify_legacy_stats() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    with tempfile.TemporaryDirectory() as directory:
        eventlog = EventLog(directory)
        eventlog.daily[today] = {
            "legacy": {"calls": 1, "failures": 0, "ms": 10.0},
        }
        eventlog.daily_models[today] = {"legacy": {"old-model": 3}}
        eventlog.add("legacy", "model", True, 20.0, tokens=7)
        stats = eventlog.daily_stats()
        legacy = next(item for item in stats["stations"] if item["name"] == "legacy")
        models = {item["model"]: item for item in legacy["models"]}
        assert legacy["tokens"] == 10
        assert stats["total_tokens"] == 10
        assert legacy["token_coverage_known"] is False
        assert stats["token_coverage_known"] is False
        assert models["old-model"]["tokens"] == 3
        assert models["old-model"]["station_share"] == 0.3
        assert models["model"]["calls"] == 1
        assert models["model"]["usage_reports"] == 1
        assert models["model"]["tokens"] == 7
        assert models["model"]["station_share"] == 0.7
    print("legacy stats compatibility check passed")


def verify_model_stats() -> None:
    with tempfile.TemporaryDirectory() as directory:
        eventlog = EventLog(directory)
        eventlog.add("alpha", "model-a", True, 10.0, tokens=None)
        eventlog.add("alpha", "model-a", True, 12.0, tokens=0)
        eventlog.add("alpha", "model-b", True, 15.0, tokens=30)
        eventlog.add("beta", "model-a", False, 20.0, tokens=None)
        stats = eventlog.daily_stats()
        stations = {item["name"]: item for item in stats["stations"]}
        alpha_models = {item["model"]: item for item in stations["alpha"]["models"]}

        assert stats["total_calls"] == 4
        assert stats["total_tokens"] == 30
        assert stats["usage_reports"] == 2
        assert stats["token_coverage"] == 0.5
        assert alpha_models["model-a"]["calls"] == 2
        assert alpha_models["model-a"]["tokens"] == 0
        assert alpha_models["model-a"]["usage_reports"] == 1
        assert alpha_models["model-b"]["station_share"] == 1.0
        assert stations["beta"]["models"][0]["calls"] == 1
    print("station/model usage checks passed")


def live_json(url: str, key: str) -> dict:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as response:
        assert response.status == 200, f"{url}: {response.status}"
        return json.load(response)


def verify_live() -> None:
    from app.config import GatewayConfig
    from app.service_settings import local_service_url

    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    gateway = config.get("gateway", {})
    port = int(gateway.get("port", 8000))
    key = str(gateway.get("admin_key", "") or "")
    settings = GatewayConfig.from_dict(gateway)
    data_dir = Path(settings.data_dir)
    settings.data_dir = str(data_dir if data_dir.is_absolute() else ROOT / data_dir)
    base = local_service_url(settings)

    for asset in (
        "index.html", "dashboard.css?v=39", "dashboard.js?v=43", "analytics.js?v=8",
        "visual-tests.js?v=11", "visual-tests.css?v=8",
        "vendor/echarts.min.js?v=5.6.0",
        "brand-logo-source.png?v=4", "brand-mark-180.png?v=4",
        "brand-mark-512.png?v=4", "favicon.ico?v=4",
    ):
        with urllib.request.urlopen(f"{base}/static/{asset}", timeout=10) as response:
            assert response.status == 200, f"{asset}: {response.status}"
            assert response.read(), f"{asset}: empty response"

    with urllib.request.urlopen(f"{base}/favicon.ico", timeout=10) as response:
        assert response.status == 200
        assert response.headers.get_content_type() in {"image/x-icon", "image/vnd.microsoft.icon"}
        assert response.read(), "favicon.ico: empty response"

    status = live_json(f"{base}/admin/status", key)
    managed_stations = live_json(f"{base}/admin/stations", key)
    stats = live_json(f"{base}/admin/stats/daily", key)
    history = live_json(f"{base}/admin/stats/history?days=365", key)
    log_settings = live_json(f"{base}/admin/settings/logs", key)
    log_analytics = live_json(f"{base}/admin/logs/analytics?group_by=ip", key)
    endpoint = live_json(f"{base}/admin/settings/endpoint", key)
    assert {"public_url", "endpoint", "port", "source", "candidates"} <= set(endpoint)
    if endpoint["endpoint"]:
        parsed_endpoint = urlsplit(endpoint["endpoint"])
        assert parsed_endpoint.scheme in {"http", "https"}
        assert parsed_endpoint.path.endswith("/v1")
        assert parsed_endpoint.hostname not in {"localhost", "0.0.0.0", "::"}
        try:
            address = ipaddress.ip_address(parsed_endpoint.hostname)
        except ValueError:
            pass
        else:
            assert not address.is_loopback and not address.is_unspecified
        if endpoint["source"] == "local_interface":
            assert parsed_endpoint.port == endpoint["port"]
    else:
        assert endpoint["source"] == "needs_configuration"
    assert len(history["daily"]) == 365
    assert history["daily"][-1]["date"] == stats["date"]
    assert isinstance(history["models"], list)
    logs = live_json(f"{base}/admin/logs?page=1&page_size=20", key)
    assert isinstance(status.get("stations"), list)
    assert isinstance(managed_stations, list)
    assert "logs" not in status
    assert {"total", "up"} <= set(status.get("summary", {}))
    assert isinstance(stats.get("timeline"), dict)
    assert isinstance(status.get("route_activity"), dict)
    assert isinstance(status["route_activity"].get("active_routes"), list)
    assert isinstance(status["route_activity"].get("active_stations"), list)
    assert isinstance(stats.get("stations"), list)
    assert "token_coverage_known" in stats
    assert isinstance(stats.get("cache_compare"), dict)
    assert {"hour", "best_hour", "today", "d7", "d31"} <= set(
        (stats.get("cache_compare") or {}).get("windows") or {})
    assert {"events", "total", "page", "page_size", "pages", "max_events",
            "retained_events"} <= set(logs)
    assert logs["page"] == 1 and logs["page_size"] == 20
    assert len(logs["events"]) <= 20
    assert 100 <= log_settings["event_retention"] <= log_settings["max_event_retention"]
    assert logs["max_events"] == log_settings["event_retention"]
    assert log_analytics["retained_events"] == logs["retained_events"]
    assert log_analytics["max_events"] == log_settings["event_retention"]
    assert isinstance(log_analytics.get("daily"), list)
    assert isinstance(log_analytics.get("breakdown"), list)
    assert isinstance(log_analytics.get("ips"), list)
    assert isinstance(log_analytics.get("models"), list)
    assert isinstance(log_analytics.get("ip_models"), list)
    assert 0 <= log_analytics["ip_reports"] <= log_analytics["calls"]
    assert 0 <= log_analytics["usage_reports"] <= log_analytics["calls"]
    for collection in ("daily", "breakdown", "ip_models"):
        for metric in ("calls", "failures", "tokens", "usage_reports"):
            assert sum(row[metric] for row in log_analytics[collection]) == log_analytics[metric]
    for row in log_analytics["ip_models"]:
        assert {"ip", "model", "calls", "failures", "tokens", "usage_reports"} <= set(row)
        assert 0 <= row["usage_reports"] <= row["calls"]
    if log_analytics["ip_models"]:
        sample = log_analytics["ip_models"][0]
        query = urlencode({"ip": sample["ip"], "model": sample["model"], "group_by": "model"})
        filtered = live_json(f"{base}/admin/logs/analytics?{query}", key)
        assert all(row["ip"] == sample["ip"] and row["model"] == sample["model"]
                   for row in filtered["ip_models"])
        assert sum(row["calls"] for row in filtered["ip_models"]) == filtered["calls"]
    assert all("client_ip" in event for event in logs["events"])
    for station in stats["stations"]:
        assert isinstance(station.get("models"), list)
        assert "tokens" in station and "calls" in station
        for model in station["models"]:
            assert {"model", "calls", "tokens", "usage_reports"} <= set(model)
    for station in managed_stations:
        assert {
            "name", "base_url", "models", "priority", "billing", "enabled",
            "status", "probe_models", "probe_chat", "priority_auto",
            "priority_bias", "detected_models", "effective_models",
            "model_discovery", "rank", "has_api_key", "provider", "model_routing",
            "route_models", "model_api_paths", "model_unavailable",
        } <= set(station)
        assert isinstance(station["detected_models"], list)
        assert isinstance(station["model_discovery"], dict)
        assert "api_key" not in station

    stream_headers = {"Accept": "text/event-stream"}
    if key:
        stream_headers["Authorization"] = f"Bearer {key}"
    stream_request = urllib.request.Request(
        f"{base}/admin/activity/stream", headers=stream_headers)
    with urllib.request.urlopen(stream_request, timeout=10) as response:
        assert response.status == 200
        assert response.readline().decode("utf-8").strip() == "event: activity"
        data_line = response.readline().decode("utf-8").strip()
        assert data_line.startswith("data: ")
        activity = json.loads(data_line[6:])["route_activity"]
        assert {"revision", "active", "inflight", "active_routes"} <= set(activity)
    print(f"dashboard live checks passed ({len(status['stations'])} stations)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="also check the running local gateway")
    args = parser.parse_args()
    verify_static()
    verify_legacy_stats()
    verify_model_stats()
    if args.live:
        verify_live()


if __name__ == "__main__":
    main()
