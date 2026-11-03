"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const ROOT = path.resolve(__dirname, "..");
const watchdog = setTimeout(() => {
  console.error("dashboard UI test timed out");
  process.exit(2);
}, 15000);

const maliciousStation = '<img src=x onerror="globalThis.pwned=1"> & alpha';
const maliciousModel = 'model-a"><script>globalThis.pwned=2</script>';

function baseStatus(routeActivity) {
  return {
    gateway: { max_attempts: 3, probe_interval: 30, balance_alert_threshold: 5 },
    summary: { total: 2, up: 2 },
    stations: [
      {
        name: maliciousStation,
        base_url: "https://alpha.invalid/v1",
        enabled: true,
        status: "up",
        billing: "subscription",
        health_score: 82,
        window_success_rate: 1,
        calls: 4,
        failures: 0,
        switch_in: 0,
        switch_out: 0,
        priority: 1,
        rank: 1,
        models: [maliciousModel, "shared-model"],
        history_hour: { ok: [], fail: [] },
      },
      {
        name: "beta",
        base_url: "https://beta.invalid/v1",
        enabled: true,
        status: "up",
        billing: "balance",
        health_score: 76,
        window_success_rate: 0.8,
        calls: 3,
        failures: 1,
        switch_in: 0,
        switch_out: 1,
        priority: 2,
        rank: 2,
        models: ["shared-model"],
        history_hour: { ok: [], fail: [] },
      },
    ],
    events: [],
    route_activity: routeActivity,
    active_routes: routeActivity.active_routes,
    active_stations: routeActivity.active_stations,
    last_success: routeActivity.last_success,
  };
}

function usageFixture() {
  return {
    date: "2026-08-17",
    total_calls: 7,
    total_failures: 1,
    total_tokens: 1000,
    usage_reports: 5,
    token_coverage: 5 / 7,
    token_coverage_known: true,
    stations: [
      {
        name: maliciousStation,
        calls: 4,
        failures: 0,
        tokens: 600,
        usage_reports: 3,
        token_coverage_known: true,
        models: [
          { model: maliciousModel, calls: 2, tokens: 400, usage_reports: 2 },
          { model: "shared-model", calls: 2, tokens: 100, usage_reports: 1 },
        ],
      },
      {
        name: "beta",
        calls: 3,
        failures: 1,
        tokens: 400,
        usage_reports: 2,
        token_coverage_known: true,
        models: [
          { model: "shared-model", calls: 3, tokens: 400, usage_reports: 2 },
        ],
      },
    ],
    timeline: { [maliciousStation]: Array(24).fill(0), beta: Array(24).fill(0) },
    current_hour: 12,
  };
}

async function settle() {
  for (let index = 0; index < 5; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

async function main() {
  const now = Date.now() / 1000;
  let statusFixture = baseStatus({
    active: true,
    inflight: 2,
    active_routes: [
      { id: 1, station: maliciousStation, model: maliciousModel, started_at: now - 2 },
      { id: 2, station: "beta", model: "shared-model", started_at: now - 1 },
    ],
    active_stations: [
      { station: maliciousStation, inflight: 1, models: [{ model: maliciousModel, inflight: 1 }] },
      { station: "beta", inflight: 1, models: [{ model: "shared-model", inflight: 1 }] },
    ],
    last_attempt: null,
    last_success: null,
  });
  let statsFixture = usageFixture();
  let eventRetention = 500;
  let logsFixture = {
    events: Array.from({ length: 20 }, (_, index) => ({
      ts: now - index,
      station: index % 2 ? "beta" : maliciousStation,
      model: "shared-model",
      client_ip: index % 2 ? "192.0.2.2" : null,
      ok: index % 3 !== 0,
      latency_ms: 1200 + index,
      detail: `event-${index}`,
      attempts: 1,
    })),
    total: 45,
    page: 1,
    page_size: 20,
    pages: 3,
    max_events: 500,
  };
  let retrySettings = {
    max_attempts: 3,
    retry_backoff: 0.3,
    max_rounds: 3,
    round_backoff: 1,
    max_round_backoff: 10,
    max_retry_seconds: 120,
  };
  let managedStations = [
    {
      name: maliciousStation,
      base_url: "https://alpha.invalid/v1",
      models: [maliciousModel, "shared-model"],
      priority: 1,
      billing: "subscription",
      enabled: true,
      status: "up",
      quota_exhausted: false,
      probe_chat: true,
      probe_models: true,
      detected_models: [maliciousModel, ...Array.from({ length: 30 }, (_, i) => `found-${i}`)],
      model_discovery: { status: "ready", updated_at: now - 120, checked_at: now - 120 },
      has_api_key: true,
    },
    {
      name: "beta",
      base_url: "https://beta.invalid/v1",
      models: ["shared-model"],
      priority: 2,
      billing: "balance",
      enabled: true,
      status: "up",
      quota_exhausted: false,
      probe_chat: false,
      probe_models: false,
      has_api_key: true,
    },
  ];
  const fetchCalls = [];
  let challengeNextStationWrite = false;
  let stationWriteRejected = false;
  let delayedStationList = null;
  let delayedRetrySave = null;
  let failNextRetrySave = false;
  let scoringWeightFixture = .15;
  let failNextScoringSave = false;
  let allowedIps = ["192.0.2.40", "198.51.100.92", "203.0.113.27", "203.0.113.23"];
  let failNextAccessSave = false;
  let servicePort = 8000;
  let canManageService = true;
  const activeServicePort = 8000;
  let endpointFixture = "http://192.0.2.40:8000/v1";
  let failEndpointRead = false;

  function jsonResponse(payload, status = 200) {
    return {
      status,
      ok: status >= 200 && status < 300,
      json: async () => payload,
    };
  }

  const html = fs.readFileSync(path.join(ROOT, "app", "web", "static", "index.html"), "utf8");
  const script = fs.readFileSync(path.join(ROOT, "app", "web", "static", "dashboard.js"), "utf8");
  const analyticsScript = fs.readFileSync(path.join(ROOT, "app", "web", "static", "analytics.js"), "utf8");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://127.0.0.1:8000/dashboard" });
  const { window } = dom;
  window.requestAnimationFrame = (callback) => callback();
  const chartInstances = new Map();
  window.echarts = { init(element) {
    element.replaceChildren();
    const chart = { element, option: null, resizeCount: 0,
      setOption(option) { this.option = option; },
      resize() { this.resizeCount += 1; } };
    chartInstances.set(element.id, chart);
    return chart;
  } };
  const drawnBars = [];
  const chartContext = { scale() {}, clearRect() { drawnBars.length = 0; }, setLineDash() {},
    beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fillText() {},
    fillRect(...args) { drawnBars.push(args); } };
  window.HTMLCanvasElement.prototype.getContext = () => chartContext;
  const trendCanvas = window.document.getElementById("usageTrendChart");
  Object.defineProperty(trendCanvas, "clientWidth", { value: 800 });
  Object.defineProperty(trendCanvas, "clientHeight", { value: 250 });
  window.Element.prototype.scrollIntoView = function scrollIntoView() {};
  window.confirm = () => true;
  window.fetch = async (input, options = {}) => {
    const url = String(input);
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : null;
    fetchCalls.push({ url, method, body, headers: options.headers || {} });

    if (challengeNextStationWrite && ["POST", "PUT"].includes(method)
        && url.startsWith("/admin/stations")
        && !options.headers?.Authorization && !stationWriteRejected) {
      stationWriteRejected = true;
      return jsonResponse({ detail: "Invalid admin key." }, 401);
    }
    if (url === "/admin/settings/access") {
      if (method === "PUT") {
        if (failNextAccessSave) {
          failNextAccessSave = false;
          return jsonResponse({ detail: "invalid IP" }, 400);
        }
        allowedIps = body.allowed_ips;
      }
      return jsonResponse({ allowed_ips: allowedIps, client_ip: "127.0.0.1" });
    }
    if (url === "/admin/settings/endpoint") {
      if (failEndpointRead) return jsonResponse({ detail: "endpoint temporarily unavailable" }, 503);
      if (method === "PUT") endpointFixture = body.public_url.replace(/\/v1\/?$/, "").replace(/\/$/, "") + "/v1";
      return jsonResponse({
        public_url: endpointFixture.slice(0, -3),
        endpoint: endpointFixture,
        port: activeServicePort,
      });
    }
    if (url === "/admin/capabilities") return jsonResponse({can_manage_service: canManageService});
    if (url === "/admin/settings/service") {
      if (method === "PUT") servicePort = body.port;
      return jsonResponse({
        port: servicePort,
        active_port: activeServicePort,
        ...(method === "PUT" ? {
          restart_required: servicePort !== activeServicePort,
          persisted: true,
        } : {}),
      });
    }
    if (url === "/admin/service/restart" && method === "POST") {
      return jsonResponse({ scheduled: true }, 202);
    }
    if (url === "/admin/settings/scoring") {
      if (method === "PUT") {
        if (failNextScoringSave) {
          failNextScoringSave = false;
          return jsonResponse({ detail: "mock scoring save failure" }, 500);
        }
        scoringWeightFixture = body.scoring_speed_weight;
        statusFixture.gateway.scoring_speed_weight = scoringWeightFixture;
      }
      return jsonResponse({ scoring_speed_weight: scoringWeightFixture, persisted: method === "PUT" });
    }
    if (url === "/admin/settings/retry" && method === "GET") {
      return jsonResponse({ settings: { ...retrySettings } });
    }
    if (url === "/admin/settings/retry" && method === "PUT") {
      if (failNextRetrySave) {
        failNextRetrySave = false;
        return jsonResponse({ detail: "test retry save failure" }, 400);
      }
      if (delayedRetrySave) {
        const pending = delayedRetrySave;
        delayedRetrySave = null;
        return new Promise((resolve) => {
          pending.resolve = () => {
            retrySettings = { ...body };
            resolve(jsonResponse({ settings: { ...retrySettings } }));
          };
        });
      }
      retrySettings = { ...body };
      return jsonResponse({ settings: { ...retrySettings } });
    }
    if (url === "/admin/stations" && method === "GET") {
      const snapshot = managedStations.map((station) => ({ ...station }));
      if (delayedStationList) {
        const pending = delayedStationList;
        delayedStationList = null;
        return new Promise((resolve) => {
          pending.resolve = () => resolve(jsonResponse(snapshot));
        });
      }
      return jsonResponse(snapshot);
    }
    if (url === "/admin/stations" && method === "POST") {
      managedStations.push({
        ...body,
        base_url: body.base_url.replace(/\/$/, ""),
        status: "up",
        quota_exhausted: false,
        has_api_key: true,
      });
      return jsonResponse({
        name: body.name, ok: true, persisted: true,
        probe_scheduled: body.enabled,
      }, 201);
    }
    const stationItemMatch = url.match(/^\/admin\/stations\/([^?]+)$/);
    if (stationItemMatch) {
      const stationName = decodeURIComponent(stationItemMatch[1]);
      const stationIndex = managedStations.findIndex((station) => station.name === stationName);
      if (method === "PUT") {
        managedStations[stationIndex] = { ...managedStations[stationIndex], ...body };
        return jsonResponse({
          name: stationName, ok: true, persisted: true,
          probe_scheduled: managedStations[stationIndex].enabled,
        });
      }
      if (method === "DELETE") {
        managedStations.splice(stationIndex, 1);
        return jsonResponse({ name: stationName, ok: true, persisted: true });
      }
    }
    if (url.endsWith("/models/refresh")) {
      const name = decodeURIComponent(url.slice("/admin/station/".length, -"/models/refresh".length));
      const station = managedStations.find(item => item.name === name);
      station.detected_models = ["newly-found"];
      station.model_discovery = { status: "ready", updated_at: now, checked_at: now };
      return jsonResponse(station);
    }
    const stationActionMatch = url.match(/^\/admin\/station\/([^?]+)\/(toggle|probe|reset|generation-probe)$/);
    if (stationActionMatch) {
      const stationName = decodeURIComponent(stationActionMatch[1]);
      const action = stationActionMatch[2];
      const station = managedStations.find((item) => item.name === stationName);
      if (action === "toggle") {
        station.enabled = !station.enabled;
        return jsonResponse({ name: stationName, enabled: station.enabled });
      }
      if (action === "probe") {
        return jsonResponse({ name: stationName, ok: true, latency_ms: 42 });
      }
      if (action === "generation-probe") {
        station.generation_probe = { mode: "manual", model: station.probe_chat_model,
          last_at: now, ok: true, error: "" };
        return jsonResponse({ name: stationName, model: station.probe_chat_model,
          ok: true, skipped: false, error: "", latency_ms: 42 });
      }
      station.quota_exhausted = false;
      return jsonResponse({ name: stationName, quota_exhausted: false });
    }
    if (url.includes("/stats/history")) {
      const last = new Date("2026-08-17T12:00:00Z");
      const daily = Array.from({length:365}, (_, index) => {
        const date = new Date(last); date.setUTCDate(date.getUTCDate() - 364 + index);
        return {date:date.toISOString().slice(0,10), calls:index === 364 ? 7 : 0,
          failures:index === 364 ? 1 : 0, tokens:index === 364 ? 1000 : 0,
          usage_reports:index === 364 ? 5 : 0, cache_read:0, cache_input:0};
      });
      return jsonResponse({from:daily[0].date, to:"2026-08-17", available_from:"2026-08-17", daily,
        models:[{date:"2026-08-17", station:maliciousStation, model:maliciousModel, calls:4, failures:0, tokens:600},
          {date:"2026-08-17", station:"beta", model:"shared-model", calls:2, failures:0, tokens:400}]});
    }
    if (url.includes("/stats/daily")) return jsonResponse(statsFixture);
    if (url === "/admin/settings/logs") {
      if (method === "PUT") eventRetention = body.event_retention;
      return jsonResponse({ event_retention: eventRetention, min_event_retention: 100, max_event_retention: 10000 });
    }
    if (url.startsWith("/admin/logs/analytics")) {
      return jsonResponse({
        date_from: "2026-08-11", date_to: "2026-08-17", group_by: "ip",
        calls: 20, failures: 2, retained_events: 20, max_events: eventRetention,
        tokens: 1200, usage_reports: 12, ip_reports: 12,
        ip_models: [{ip: "192.0.2.2", model: "shared-model", calls: 12, failures: 1, tokens: 1200, usage_reports: 12}, {ip: "unknown", model: maliciousModel, calls: 8, failures: 1, tokens: 0, usage_reports: 0}],
        available_from: "2026-08-17", available_to: "2026-08-17",
        ips: [{ value: "192.0.2.2", label: "192.0.2.2" }, { value: "unknown", label: "未记录 IP" }],
        models: [{ value: "shared-model", label: "shared-model" }],
        daily: Array.from({ length: 7 }, (_, index) => ({ date: `2026-08-${String(11 + index).padStart(2, "0")}`, calls: index ? 3 : 2, failures: 0 })),
        breakdown: [{ value: "192.0.2.2", label: "192.0.2.2", calls: 12, failures: 1, tokens: 1200, usage_reports: 12 }, { value: "unknown", label: "未记录 IP", calls: 8, failures: 1, tokens: 0, usage_reports: 0 }],
      });
    }
    if (url.includes("/admin/logs")) return jsonResponse(logsFixture);
    return jsonResponse(statusFixture);
  };
  if (!window.HTMLDialogElement.prototype.showModal) {
    window.HTMLDialogElement.prototype.showModal = function showModal() { this.open = true; };
  }
  if (!window.HTMLDialogElement.prototype.close) {
    window.HTMLDialogElement.prototype.close = function close() { this.open = false; };
  }
  window.eval(`${script}\n${analyticsScript}\n;globalThis.__testFormatters = {finiteNumber, formatBalance, formatLatency};globalThis.__testConsumeActivityEvent = consumeActivityEvent;globalThis.__testDrainActivityBuffer = drainActivityBuffer;globalThis.__testApi = api;globalThis.__testProtectionMarkup = protectionMarkup;globalThis.__testRefreshCapabilities = refreshCapabilities;`);
  await settle();

  const document = window.document;
  document.querySelector('#list-wrap [data-score-station]').click();
  assert.equal(document.getElementById('healthScoreDialog').open, true);
  assert.equal(document.getElementById('scoreStationName').textContent, maliciousStation);
  assert.ok(document.getElementById('scoreSpeedNote').textContent.includes('0.15'));
  assert.equal(document.querySelectorAll('#healthScoreDialog script, #healthScoreDialog img').length, 0);
  document.getElementById('scoreClose').click();
  assert.equal(document.getElementById('healthScoreDialog').open, false);
  document.getElementById('tab-stations').click();
  document.getElementById('stations-tab-scoring').click();
  await settle();
  const speedInput = document.getElementById('scoring-speed-weight');
  const scoringForm = document.getElementById('scoring-form');
  assert.equal(speedInput.value, '15');
  speedInput.value = '35';
  speedInput.dispatchEvent(new window.Event('input', { bubbles: true }));
  assert.match(document.getElementById('scoring-preview').textContent, /65–100/);
  scoringForm.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(scoringWeightFixture, .35);
  assert.match(document.getElementById('scoringSettingsStatus').textContent, /即时生效/);
  document.querySelector('#list-wrap [data-score-station]').click();
  assert.match(document.getElementById('scorePolicyNote').textContent, /35%/);
  assert.match(document.getElementById('scoreSpeedNote').textContent, /0.35/);
  document.getElementById('scoreClose').click();
  speedInput.value = '';
  scoringForm.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(scoringWeightFixture, .35);
  assert.match(document.getElementById('scoringSettingsStatus').textContent, /0–100%/);
  failNextScoringSave = true;
  speedInput.value = '50';
  scoringForm.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(scoringWeightFixture, .35);
  assert.match(document.getElementById('scoringSettingsStatus').textContent, /保存失败/);
  speedInput.value = '0';
  scoringForm.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(scoringWeightFixture, 0);
  document.querySelector('#list-wrap [data-score-station]').click();
  assert.match(document.getElementById('scoreSpeedNote').textContent, /不按速度扣分/);
  document.getElementById('scoreClose').click();
  document.getElementById('scoring-reset').click();
  scoringForm.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(scoringWeightFixture, .15);
  document.getElementById('tab-monitor').click();
  const protectionStation = { name: maliciousStation, enabled: true, status: "down", protection: {
    kind: "breaker", until: Date.now() / 1000 + 60, failure_count: 3, status_code: 503,
    source: "request", reason: maliciousModel,
  } };
  const protectionContainer = document.createElement("div");
  protectionContainer.innerHTML = window.__testProtectionMarkup(protectionStation);
  assert.match(protectionContainer.textContent, /熔断 · \d+ 秒/);
  assert.match(protectionContainer.textContent, /连续 3 次故障 · 最近 HTTP 503/);
  assert.equal(protectionContainer.querySelectorAll("script, img").length, 0);
  protectionStation.protection.until = Date.now() / 1000 - 1;
  protectionContainer.innerHTML = window.__testProtectionMarkup(protectionStation);
  assert.match(protectionContainer.textContent, /待恢复验证/);
  protectionStation.status = "up";
  protectionStation.protection = {kind: "rate_limited", until: Date.now() / 1000 + 120,
    source: "request", wait_source: "upstream"};
  protectionContainer.innerHTML = window.__testProtectionMarkup(protectionStation);
  assert.match(protectionContainer.textContent, /限流冷却 · \d+ 秒/);
  assert.match(protectionContainer.textContent, /上游 429 · 遵守上游等待时间/);
  // Subsections and viewport pagination preserve data and form drafts.
  document.getElementById("monitor-tab-history").click();
  assert.equal(document.getElementById("monitor-history").hidden, false);
  assert.equal(document.getElementById("monitor-stations").hidden, true);
  assert.ok(document.querySelector("#monitor-history .probe-block"));
  assert.equal(document.querySelector("#list-wrap .probe-block"), null);
  document.getElementById("monitor-tab-history").dispatchEvent(new window.KeyboardEvent("keydown", {key: "Home", bubbles: true}));
  assert.equal(document.activeElement.id, "monitor-tab-stations");
  assert.equal(document.getElementById("monitor-stations").hidden, false);
  const viewport = document.getElementById("monitorViewport");
  Object.defineProperty(viewport, "clientHeight", {configurable: true, value: 120});
  const callsBeforePagination=fetchCalls.length;
  window.dispatchEvent(new window.Event("resize"));
  await new Promise(resolve => setTimeout(resolve, 70));
  assert.equal(document.querySelectorAll("#list-wrap tbody tr:not([hidden])").length, 1);
  const secondPage=document.querySelector('#monitorPagination [data-page-step="1"]');
  assert.equal(secondPage.disabled, false);
  secondPage.click();
  assert.equal(document.querySelector("#list-wrap tbody tr:not([hidden]) .station-name-text").textContent, "beta");
  assert.match(document.querySelector("#monitorPagination .page-info").textContent, /2 [/] 2/);
  assert.equal(fetchCalls.length, callsBeforePagination);
  document.querySelector('#monitorPagination [data-page-step="-1"]').click();
  delete viewport.clientHeight;
  document.querySelectorAll("#list-wrap tbody tr").forEach(el => el.hidden=false);
  document.getElementById("btn-card").click();
  assert.equal(document.getElementById("cardSections").hidden, false);
  assert.equal(document.querySelectorAll(".card-content .card-actions").length, 0);
  document.querySelector('[data-card-section="details"]:not(#grid)').click();
  assert.equal(document.getElementById("grid").dataset.cardSection, "details");
  document.querySelector('[data-card-section="overview"]:not(#grid)').click();
  document.getElementById("btn-list").click();
  assert.equal(document.getElementById("cardSections").hidden, true);
  document.getElementById("tab-stats").click();
  document.getElementById("stats-tab-cache").click();
  assert.equal(document.getElementById("stats-cache").hidden, false);
  assert.equal(document.getElementById("stats-models").hidden, true);
  document.getElementById("stats-tab-models").click();
  document.getElementById("tab-stations").click();
  await settle();
  assert.equal(document.getElementById("stations-editor").hidden, true);
  document.getElementById("stations-add").click();
  assert.equal(document.getElementById("stations-editor").hidden, false);
  document.getElementById("sf-name").value="draft-kept-across-tabs";
  document.getElementById("editor-tab-routing").click();
  assert.equal(document.getElementById("editor-routing").hidden, false);
  document.getElementById("sf-base-url").dispatchEvent(new window.Event("invalid", {cancelable:true}));
  assert.equal(document.getElementById("editor-connection").hidden, false);

  document.getElementById("stations-tab-list").click();
  document.getElementById("stations-tab-editor").click();
  assert.equal(document.getElementById("sf-name").value, "draft-kept-across-tabs");
  document.getElementById("sf-name").value="";
  document.getElementById("stations-tab-list").click();

  document.getElementById("tab-stations").click();
  document.getElementById("stations-tab-retry").click();
  await settle();
  document.getElementById("retry-tab-duration").click();
  assert.equal(document.getElementById("retry-section-duration").hidden, false);
  document.getElementById("retry-max-rounds").dispatchEvent(new window.Event("invalid", {cancelable: true}));
  assert.equal(document.getElementById("retry-section-rounds").hidden, false);
  document.getElementById("retry-tab-switch").click();
  document.getElementById("tab-monitor").click();
  await settle();

  // Dashboard redesign interactions must preserve read-only monitoring.
  assert.equal(document.getElementById("pageTitle").textContent, "运行概览");
  assert.equal(document.getElementById("summaryStrip").hidden, false);
  assert.equal(document.getElementById("monitor-count").textContent, "2 / 2");
  const callsBeforeFilter = fetchCalls.length;
  const search = document.getElementById("station-search");
  search.value = "BETA";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(document.querySelectorAll("#list-wrap tbody tr").length, 1);
  assert.equal(document.querySelector("#list-wrap .station-name-text").textContent, "beta");
  assert.equal(document.getElementById("monitor-count").textContent, "1 / 2");
  assert.equal(document.getElementById("s-calls").textContent, "7");
  search.value = "shared-model";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  assert.equal(document.querySelectorAll("#list-wrap tbody tr").length, 2);
  const filter = document.getElementById("station-filter");
  document.getElementById("station-scope-disabled").click();
  assert.equal(document.getElementById("station-disabled-count").textContent, "0");
  assert.equal(document.getElementById("stationHealthFilter").hidden, true);
  assert.equal(document.getElementById("monitor-count").textContent, "0 / 0");
  assert.equal(document.querySelector("#list-wrap .empty-cell").colSpan, 17);
  document.getElementById("btn-card").click();
  assert.equal(document.getElementById("grid").hidden, false);
  assert.equal(document.getElementById("detailToggle").hidden, true);
  assert.ok(document.querySelector("#grid .empty-cell"));
  document.getElementById("station-scope-disabled").dispatchEvent(new window.KeyboardEvent("keydown", {key: "Home", bubbles: true}));
  assert.equal(document.activeElement.id, "station-scope-enabled");
  assert.equal(document.getElementById("stationHealthFilter").hidden, false);
  assert.equal(document.querySelectorAll("#grid .station-card").length, 2);
  search.value = "";
  search.dispatchEvent(new window.Event("input", { bubbles: true }));
  document.getElementById("btn-list").click();
  assert.equal(document.getElementById("detailToggle").hidden, false);
  const details = document.getElementById("show-details");
  details.click();
  assert.equal(document.getElementById("monitor-wrap").classList.contains("show-details"), true);
  details.click();
  assert.equal(document.getElementById("monitor-wrap").classList.contains("show-details"), false);
  assert.equal(fetchCalls.length, callsBeforeFilter, "local filters must not call upstream");
  const refreshButton = document.getElementById("refreshDashboard");
  refreshButton.click();
  await settle();
  assert.equal(refreshButton.disabled, false);
  assert.equal(refreshButton.getAttribute("aria-busy"), "false");
  assert.ok(fetchCalls.slice(callsBeforeFilter).some(call => call.url === "/admin/status"));
  assert.ok(fetchCalls.slice(callsBeforeFilter).every(call => call.method === "GET"));
  let copiedEndpoint;
  document.execCommand = command => {
    assert.equal(command, "copy");
    copiedEndpoint = document.querySelector(".clipboard-helper").value;
    return true;
  };
  document.getElementById("copyEndpoint").click();
  await settle();
  assert.equal(copiedEndpoint, "http://192.0.2.40:8000/v1");
  assert.equal(document.querySelector(".clipboard-helper"), null);
  assert.match(document.getElementById("toast").textContent, /已复制/);
  endpointFixture = "https://gateway.example/api/v1";
  document.getElementById("copyEndpoint").click();
  await settle();
  assert.equal(copiedEndpoint, endpointFixture, "every click reads the current endpoint");
  copiedEndpoint = null;
  failEndpointRead = true;
  document.getElementById("copyEndpoint").click();
  await settle();
  assert.equal(copiedEndpoint, null, "a failed read must not copy a stale URL");
  assert.match(document.getElementById("toast").textContent, /endpoint temporarily unavailable/);
  failEndpointRead = false;
  document.execCommand = () => false;
  document.getElementById("copyEndpoint").click();
  await settle();
  assert.match(document.getElementById("toast").textContent, /请手动复制/);
  document.getElementById("tab-monitor").dispatchEvent(new window.KeyboardEvent("keydown", {key:"End",bubbles:true}));
  assert.equal(document.getElementById("tab-access").getAttribute("aria-selected"), "true");
  assert.equal(document.getElementById("summaryStrip").hidden, true);
  assert.equal(document.getElementById("routingPanel").hidden, true);
  document.getElementById("tab-access").dispatchEvent(new window.KeyboardEvent("keydown", {key:"Home",bubbles:true}));
  assert.equal(document.activeElement.id, "tab-monitor");
  document.getElementById("tab-monitor").dispatchEvent(new window.KeyboardEvent("keydown", {key:"ArrowDown",bubbles:true}));
  assert.equal(document.activeElement.id, "tab-stats");
  assert.equal(document.getElementById("summaryStrip").hidden, false);
  assert.equal(document.getElementById("routingPanel").hidden, true);
  document.getElementById("tab-stats").dispatchEvent(new window.KeyboardEvent("keydown", {key:"ArrowUp",bubbles:true}));
  assert.equal(document.getElementById("routingPanel").hidden, false);
  await settle();

  assert.equal(document.getElementById("routeLabel").textContent, "正在使用");
  assert.equal(document.querySelectorAll("#routeList .route-item").length, 2);
  assert.match(document.querySelector("#routeList .route-item-station").textContent, /alpha/);
  assert.equal(document.querySelector("#routeList .route-item-model").textContent, maliciousModel);
  assert.equal(document.querySelectorAll("#routeList .route-phase")[0].textContent, "处理中");
  assert.equal(document.getElementById("routeMeta").textContent, "2 个请求 · 2 组线路与模型");
  assert.equal(window.pwned, undefined);

  window.__testConsumeActivityEvent('event: activity\ndata: {"route_activity":{"revision":98,"active":true,"inflight":3,"active_routes":[{"station":"alpha","model":"model-x","phase":"receiving"},{"station":"alpha","model":"model-x","phase":"streaming"},{"station":"beta","model":"model-y","phase":"connecting"}]}}');
  assert.equal(document.querySelectorAll("#routeList .route-item").length, 2);
  assert.equal(document.querySelector("#routeList .route-count").textContent, "× 2");
  assert.equal(document.querySelector("#routeList .route-phase").textContent, "多阶段");
  assert.equal(document.getElementById("routeMeta").textContent, "3 个请求 · 2 组线路与模型");

  window.__testConsumeActivityEvent('event: activity\ndata: {"route_activity":{"revision":99,"active":true,"inflight":1,"active_routes":[{"station":"beta","model":"instant-model"}],"active_stations":[{"station":"beta","models":[{"model":"instant-model"}]}]}}');
  assert.equal(document.getElementById("routeLabel").textContent, "正在使用");
  assert.equal(document.querySelector("#routeList .route-item-station").textContent, "beta");
  assert.match(document.querySelector("#routeList .route-item-model").textContent, /instant-model/);
  let splitFrame = 'event: activity\r';
  splitFrame = window.__testDrainActivityBuffer(splitFrame);
  assert.equal(document.querySelector("#routeList .route-item-station").textContent, "beta");
  splitFrame += '\ndata: {"route_activity":{"revision":100,"active":true,"inflight":1,"active_routes":[{"station":"alpha","model":"split-model"}],"active_stations":[{"station":"alpha","models":[{"model":"split-model"}]}]}}\r\n\r\n';
  splitFrame = window.__testDrainActivityBuffer(splitFrame);
  assert.equal(splitFrame, "");
  assert.equal(document.querySelector("#routeList .route-item-station").textContent, "alpha");
  assert.match(document.querySelector("#routeList .route-item-model").textContent, /split-model/);
  let mixedFrame = 'event: activity\r\ndata: {"route_activity":{"revision":101,"active":true,"inflight":1,"active_routes":[{"station":"beta","model":"mixed-model"}],"active_stations":[{"station":"beta","models":[{"model":"mixed-model"}]}]}}\n\r\n';
  assert.equal(window.__testDrainActivityBuffer(mixedFrame), "");
  assert.match(document.querySelector("#routeList .route-item-model").textContent, /mixed-model/);

  document.getElementById("tab-stats").click();
  await settle();

  document.getElementById("stats-tab-trend").click();
  assert.equal(document.getElementById("stats-trend").hidden, false);
  assert.equal(document.querySelectorAll("#usageHeatmap button[data-heat-date]").length, 365);
  assert.match(document.getElementById("trend-kpis").textContent, /1K/);
  assert.match(document.getElementById("trend-legend").textContent, /600/);
  assert.ok(drawnBars.length >= 2);
  assert.equal(document.querySelectorAll("#stats-trend script, #stats-trend img").length, 0);
  const callsBeforeTrend = fetchCalls.length;
  document.querySelector('[data-trend-metric="calls"]').click();
  assert.match(document.getElementById("trend-legend").textContent, /未归类/);
  document.querySelector('[data-trend-group="station"]').click();
  assert.match(document.getElementById("trend-legend").textContent, /beta/);
  document.querySelector('[data-trend-period="7"]').click();
  assert.equal(document.getElementById("trend-from").value, "2026-08-11");
  document.querySelector('#usageHeatmap [data-heat-date="2026-08-17"]').click();
  assert.equal(document.getElementById("trend-from").value, "2026-08-17");
  assert.equal(document.getElementById("trend-to").value, "2026-08-17");
  document.querySelector('[data-trend-metric="tokens"]').click();
  document.querySelector('[data-trend-period="30"]').click();
  assert.equal(fetchCalls.length, callsBeforeTrend);
  document.getElementById("stats-tab-models").click();

  assert.equal(document.querySelectorAll(".usage-tab").length, 2);
  assert.equal(document.querySelectorAll(".donut-segment.is-station").length, 0);
  assert.equal(document.querySelectorAll(".donut-segment.is-model").length, 3);
  assert.equal(document.querySelectorAll('.donut-segment[data-model="__unclassified__"]').length, 1);
  assert.equal(document.querySelectorAll(".model-swatch").length, 3);
  assert.match(document.querySelector(".model-row-value").textContent, /Token.*次/);
  assert.equal(document.querySelectorAll(".station-usage-row").length, 2);
  assert.equal(document.getElementById("donutValue").textContent, "600");
  assert.match(document.getElementById("chartInspector").textContent, /3\/4 次已回传/);
  assert.equal(document.querySelectorAll("img:not(#brandMark), script:not([src])").length, 0);
  assert.match(document.getElementById("brandMark").getAttribute("src"), /brand-logo-source\.png/);
  assert.equal(window.pwned, undefined);

  const firstUsageTab = document.querySelector(".usage-tab");
  firstUsageTab.focus();
  firstUsageTab.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
  assert.equal(document.querySelector('.usage-tab[aria-selected="true"]').dataset.usageStation, "beta");
  document.querySelector('.usage-tab[aria-selected="true"]')
    .dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
  assert.equal(document.querySelector('.usage-tab[aria-selected="true"]').dataset.usageStation, maliciousStation);

  const geometryBefore = [...document.querySelectorAll(".donut-segment")]
    .map((segment) => [segment.getAttribute("stroke-dasharray"), segment.getAttribute("stroke-dashoffset")]);
  const firstModel = document.querySelector(".model-row");
  firstModel.click();
  const geometryAfter = [...document.querySelectorAll(".donut-segment")]
    .map((segment) => [segment.getAttribute("stroke-dasharray"), segment.getAttribute("stroke-dashoffset")]);
  assert.deepEqual(geometryAfter, geometryBefore, "selection must not rescale the chart");
  assert.equal(document.querySelector(".model-row").classList.contains("is-selected"), true);
  assert.equal(document.getElementById("modelBreakdown").hidden, false);
  assert.equal(document.querySelectorAll(".model-row").length, 3);
  assert.match(document.getElementById("chartInspector").textContent, /400 Token/);
  assert.ok(document.querySelectorAll(".donut-segment.is-dimmed").length > 0);

  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(document.querySelectorAll(".model-row.is-selected").length, 0);

  const betaTab = [...document.querySelectorAll(".usage-tab")]
    .find((button) => button.dataset.usageStation === "beta");
  betaTab.click();
  assert.equal(document.querySelectorAll(".donut-segment.is-model").length, 1);
  assert.equal(document.getElementById("donutValue").textContent, "400");

  document.getElementById("tab-events").click();
  await settle();
  assert.equal(document.getElementById("events-records").hidden, false);
  assert.equal(document.getElementById("events-analytics").hidden, true);
  document.getElementById("eventFiltersToggle").click();
  assert.equal(document.getElementById("eventFilters").hidden, false);
  assert.equal(document.getElementById("eventFiltersToggle").getAttribute("aria-expanded"), "true");
  document.getElementById("events-tab-analytics").click();
  assert.equal(document.getElementById("events-records").hidden, true);
  assert.equal(document.getElementById("events-analytics").hidden, false);
  document.getElementById("events-tab-analytics").dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "ArrowLeft", bubbles: true,
  }));
  assert.equal(document.getElementById("events-records").hidden, false);
  document.getElementById("eventFiltersToggle").click();
  assert.equal(document.getElementById("eventFilters").hidden, true);
  assert.equal(document.querySelectorAll("#log-body tr").length, 20);
  assert.equal(document.querySelector("#log-body .event-ip").textContent, "未记录 IP");
  assert.equal(document.getElementById("eventBreakdownTitle").textContent, "IP 分布");
  assert.equal(document.getElementById("eventBreakdownTotal").textContent, "20");
  assert.equal(document.getElementById("eventDonutCenter").textContent, "20\n请求");
  document.getElementById("eventMetric").value = "tokens";
  document.getElementById("eventMetric").dispatchEvent(new window.Event("change"));
  assert.equal(chartInstances.get("eventTrendChart").option.series[0].type, "bar");
  assert.equal(document.getElementById("eventDonutCenter").textContent, "1,200\nToken");
  assert.match(document.getElementById("eventBreakdownLegend").textContent, /未上报/);
  document.getElementById("eventMetric").value = "calls";
  document.getElementById("eventMetric").dispatchEvent(new window.Event("change"));
  document.getElementById("events-tab-sources").click();
  await settle();
  assert.equal(document.getElementById("events-sources").hidden, false);
  assert.equal(document.querySelectorAll("#eventSourceRows img, #eventSourceRows script").length, 0);
  assert.match(document.getElementById("eventSourceSummary").textContent, /60.0%/);
  document.getElementById("events-tab-records").click();
  assert.equal(document.getElementById("eventDonutCenter").parentElement.className, "event-donut-wrap");
  assert.equal(document.getElementById("eventBreakdownDonut").querySelector("#eventDonutCenter"), null);
  const trendOption = chartInstances.get("eventTrendChart").option;
  const breakdownOption = chartInstances.get("eventBreakdownDonut").option;
  assert.equal(trendOption.series[0].type, "line");
  assert.equal(trendOption.series[0].name, "\u8bf7\u6c42");
  assert.equal(trendOption.series[1].name, "\u5931\u8d25");
  assert.equal(trendOption.xAxis.data.length, 7);
  assert.equal(breakdownOption.series[0].type, "pie");
  assert.equal(breakdownOption.series[0].data[1].name, "\u672a\u8bb0\u5f55 IP");
  assert.equal(document.querySelector("#log-body .event-ip").title, "\u6b64\u8bb0\u5f55\u672a\u4fdd\u5b58\u5ba2\u6237\u7aef\u5730\u5740\uff0c\u65e0\u6cd5\u8fd8\u539f\u6765\u6e90");
  assert.equal(document.getElementById("eventRetentionLimit").value, "500");
  assert.equal(document.getElementById("eventPageInfo").textContent, "第 1 / 3 页");
  assert.equal(document.getElementById("eventPrev").disabled, true);
  assert.equal(document.getElementById("eventNext").disabled, false);
  logsFixture = { ...logsFixture, page: 2 };
  document.getElementById("eventNext").click();
  await settle();
  assert.equal(document.getElementById("eventPageInfo").textContent, "第 2 / 3 页");
  assert.equal(document.getElementById("eventPrev").disabled, false);
  document.getElementById("eventGroupBy").value = "model";
  document.getElementById("eventGroupBy").dispatchEvent(new window.Event("change", { bubbles: true }));
  await settle();
  assert.equal(document.getElementById("eventBreakdownTitle").textContent, "模型分布");
  assert.equal(document.getElementById("eventDonutCenter").textContent, "20\n请求");
  document.getElementById("eventRetentionLimit").value = "1000";
  document.getElementById("eventRetentionSave").click();
  await settle();
  assert.equal(eventRetention, 1000);
  assert.ok(fetchCalls.some(call => call.url === "/admin/settings/logs" && call.method === "PUT" && call.body.event_retention === 1000));

  document.getElementById("tab-stations").click();
  document.getElementById("stations-tab-retry").click();
  await settle();
  assert.equal(document.getElementById("retry-wrap").hidden, false);
  assert.equal(document.getElementById("retry-wrap").closest(".tab-panel").id, "stations-wrap");
  assert.equal(document.getElementById("probe-wrap").closest(".tab-panel").id, "stations-wrap");
  assert.equal(document.getElementById("tab-retry"), null);
  document.getElementById("stations-tab-retry").focus();
  document.getElementById("stations-tab-retry").dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "ArrowRight", bubbles: true,
  }));
  await settle();
  assert.equal(document.getElementById("stations-tab-probe").getAttribute("aria-selected"), "true");
  assert.equal(document.getElementById("probe-wrap").hidden, false);
  assert.equal(document.getElementById("retry-wrap").hidden, true);
  document.getElementById("stations-tab-probe").dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "ArrowLeft", bubbles: true,
  }));
  assert.equal(document.getElementById("retry-wrap").hidden, false);
  document.getElementById("tab-stations").focus();
  document.getElementById("tab-stations").dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "ArrowRight", bubbles: true,
  }));
  assert.equal(document.getElementById("tab-events").getAttribute("aria-selected"), "true");
  document.getElementById("tab-events").dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "ArrowLeft", bubbles: true,
  }));
  assert.equal(document.getElementById("tab-stations").getAttribute("aria-selected"), "true");
  assert.equal(document.getElementById("retry-max-attempts").value, "3");
  assert.equal(document.getElementById("retry-backoff").value, "0.3");
  assert.equal(document.getElementById("retry-infinite-rounds").checked, false);
  assert.equal(document.getElementById("retry-unlimited-duration").checked, false);
  assert.equal(document.getElementById("retry-max-rounds").disabled, false);
  assert.equal(document.getElementById("retry-max-seconds").disabled, false);

  document.getElementById("retry-max-attempts").value = "8";
  document.getElementById("retry-backoff").value = "1.5";
  document.getElementById("retry-round-backoff").value = "2";
  document.getElementById("retry-max-round-backoff").value = "30";
  document.getElementById("retry-infinite-rounds").checked = true;
  document.getElementById("retry-infinite-rounds")
    .dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.equal(document.getElementById("retry-max-rounds").disabled, true);
  assert.equal(document.getElementById("retry-unlimited-duration").disabled, true);
  assert.equal(document.getElementById("retry-max-seconds").disabled, false);

  const pendingRetrySave = {};
  delayedRetrySave = pendingRetrySave;
  document.getElementById("retry-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(document.getElementById("retry-save").disabled, true);
  assert.equal(document.getElementById("retry-save").textContent, "保存中...");
  assert.equal(typeof pendingRetrySave.resolve, "function");
  pendingRetrySave.resolve();
  await settle();
  assert.equal(retrySettings.max_attempts, 8);
  assert.equal(retrySettings.max_rounds, 0);
  assert.equal(retrySettings.max_retry_seconds, 120);
  assert.equal(retrySettings.round_backoff, 2);
  assert.equal(document.getElementById("retry-save").disabled, false);
  assert.match(document.getElementById("retrySettingsStatus").textContent, /已保存/);
  const retryWrite = fetchCalls.filter((call) => call.url === "/admin/settings/retry"
    && call.method === "PUT").at(-1);
  assert.deepEqual(retryWrite.body, retrySettings);

  retrySettings = {
    max_attempts: 4,
    retry_backoff: 0.5,
    max_rounds: 6,
    round_backoff: 1.5,
    max_round_backoff: 20,
    max_retry_seconds: 90,
  };
  document.getElementById("retry-reload").click();
  await settle();
  assert.equal(document.getElementById("retry-max-attempts").value, "4");
  assert.equal(document.getElementById("retry-max-rounds").value, "6");
  assert.equal(document.getElementById("retry-max-seconds").value, "90");
  assert.equal(document.getElementById("retry-infinite-rounds").checked, false);
  assert.equal(document.getElementById("retry-unlimited-duration").checked, false);
  assert.match(document.getElementById("retrySettingsStatus").textContent, /已加载/);

  failNextRetrySave = true;
  document.getElementById("retry-max-attempts").value = "5";
  document.getElementById("retry-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await settle();
  assert.match(document.getElementById("retrySettingsStatus").textContent, /保存失败.*test retry save failure/);
  assert.equal(document.getElementById("retrySettingsStatus").classList.contains("is-error"), true);
  assert.equal(document.getElementById("retry-save").disabled, false);

  document.getElementById("tab-access").click();
  await settle();
  assert.equal(document.getElementById("access-wrap").hidden, false);
  assert.equal(document.getElementById("access-ips").value, allowedIps.join("\n"));
  assert.match(document.getElementById("access-client").textContent, /127\.0\.0\.1/);
  document.getElementById("access-ips").value = "192.0.2.40,198.51.100.92\n192.0.2.40";
  document.getElementById("access-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  assert.equal(document.getElementById("access-save").disabled, true);
  await settle();
  assert.deepEqual(allowedIps, ["192.0.2.40", "198.51.100.92"]);
  assert.match(document.getElementById("accessSettingsStatus").textContent, /已保存/);
  failNextAccessSave = true;
  document.getElementById("access-ips").value = "bad";
  document.getElementById("access-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await settle();
  assert.match(document.getElementById("accessSettingsStatus").textContent, /保存失败.*invalid IP/);
  assert.equal(document.getElementById("access-ips").value, "bad");
  assert.equal(document.getElementById("access-save").disabled, false);
  document.getElementById("access-reload").click();
  await settle();
  assert.equal(document.getElementById("access-ips").value, allowedIps.join("\n"));

  assert.equal(document.getElementById("tab-service").hidden, true);
  document.getElementById("tab-service").click();
  assert.equal(document.getElementById("service-wrap").hidden, true);
  await window.__testRefreshCapabilities();
  assert.equal(document.getElementById("tab-service").hidden, false);
  document.getElementById("tab-service").click();
  await settle();
  assert.equal(document.getElementById("service-wrap").hidden, false);
  assert.equal(document.getElementById("service-port").value, "8000");
  document.getElementById("service-port").value = "9123";
  document.getElementById("service-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await settle();
  assert.equal(servicePort, 9123);
  assert.match(document.getElementById("serviceSettingsStatus").textContent, /重启服务后生效/);
  assert.ok(fetchCalls.some(call => call.url === "/admin/settings/service"
    && call.method === "PUT" && call.body.port === 9123));

  canManageService = false;
  await window.__testRefreshCapabilities();
  assert.equal(document.getElementById("tab-service").hidden, true);
  assert.equal(document.getElementById("service-wrap").hidden, true);
  const serviceReads = fetchCalls.filter(call => call.url === "/admin/settings/service").length;
  document.getElementById("tab-service").click();
  await settle();
  assert.equal(document.getElementById("service-wrap").hidden, true);
  assert.equal(fetchCalls.filter(call => call.url === "/admin/settings/service").length, serviceReads);

  document.getElementById("tab-stations").click();
  await settle();
  assert.equal(document.querySelectorAll("#stations-body tr").length, 2);
  assert.equal(document.querySelectorAll("#stations-body img, #stations-body script").length, 0);
  assert.equal(window.pwned, undefined);

  let maliciousManagedRow = [...document.querySelectorAll("[data-station-row]")]
    .find((row) => row.dataset.stationRow === maliciousStation);
  const callsBeforeCatalog = fetchCalls.length;
  maliciousManagedRow.querySelector('[data-manage-action="models"]').click();
  assert.equal(document.getElementById("editor-models").hidden, false);
  assert.ok(fetchCalls.slice(callsBeforeCatalog).every(call => call.method === "GET" && call.url === "/admin/settings/probe"));
  assert.equal(document.querySelectorAll("#sf-model-list img, #sf-model-list script").length, 0);
  assert.equal(document.querySelector("#sf-model-list .catalog-model").textContent, maliciousModel);
  const catalogButton = document.querySelector("#sf-model-list .catalog-model");
  assert.equal(catalogButton.tagName, "BUTTON");
  assert.equal(document.getElementById("sf-probe-model-auto").checked, true);
  assert.equal(catalogButton.getAttribute("aria-pressed"), "true");
  const beforeSelect = fetchCalls.length;
  catalogButton.click();
  assert.equal(document.getElementById("sf-probe-model-auto").checked, false);
  assert.equal(document.getElementById("sf-probe-chat-model").value, maliciousModel);
  assert.equal(document.getElementById("sf-probe-current").textContent, maliciousModel);
  assert.equal(fetchCalls.length, beforeSelect);
  const autoProbe = document.getElementById("sf-probe-model-auto");
  autoProbe.checked = true;
  autoProbe.dispatchEvent(new window.Event("change"));
  assert.equal(document.getElementById("sf-probe-chat-model").value, "");
  assert.equal(document.getElementById("sf-probe-current").textContent, maliciousModel);
  assert.equal(document.querySelectorAll("#sf-model-list .catalog-model").length, 32);
  document.getElementById("sf-model-search").value = "found-29";
  document.getElementById("sf-model-search").dispatchEvent(new window.Event("input"));
  assert.equal(document.querySelectorAll("#sf-model-list .catalog-model").length, 1);
  assert.equal(document.querySelector("#sf-model-list .catalog-model").textContent, "found-29");
  document.getElementById("sf-model-search").value = "";
  document.getElementById("sf-model-search").dispatchEvent(new window.Event("input"));
  document.getElementById("sf-models-refresh").click();
  await settle();
  assert.match(document.getElementById("sf-model-list").textContent, /newly-found/);
  assert.equal(document.getElementById("sf-models-refresh").disabled, true);
  assert.equal(fetchCalls.filter(call => call.url.endsWith("/models/refresh")).length, 1);
  document.getElementById("sf-cancel").click();
  maliciousManagedRow = [...document.querySelectorAll("[data-station-row]")]
    .find((row) => row.dataset.stationRow === maliciousStation);
  maliciousManagedRow.querySelector('[data-manage-action="edit"]').click();
  assert.equal(document.getElementById("sf-name").disabled, true);
  assert.equal(document.getElementById("sf-api-key").required, false);
  assert.equal(document.getElementById("sf-api-key").value, "");
  document.getElementById("editor-tab-models").click();
  assert.equal(document.getElementById("editor-models").hidden, false);
  const probeModelSelect = document.getElementById("sf-probe-chat-model");
  assert.equal(probeModelSelect.tagName, "SELECT");
  assert.equal(probeModelSelect.value, "");
  assert.match(probeModelSelect.options[0].textContent, /自动.*newly-found/);
  assert.equal(document.querySelectorAll("#editor-models script, #editor-models img").length, 0);
  assert.equal(document.getElementById("editor-models").hidden, false);
  [...document.querySelectorAll("#sf-model-list [data-probe-model]")]
    .find(button => button.dataset.probeModel === "shared-model").click();
  assert.equal(probeModelSelect.value, "shared-model");
  assert.equal(document.getElementById("sf-probe-model-auto").checked, false);
  assert.match(document.getElementById("sf-probe-save-state").textContent, /待保存/);
  assert.equal(document.getElementById("editor-models").hidden, false);
  assert.match(document.getElementById("sf-probe-current").textContent, /shared-model/);
  assert.equal(document.getElementById("sf-generation-test").disabled, true);
  assert.match(document.getElementById("sf-generation-result").textContent, /请先保存/);
  document.getElementById("sf-base-url").value = "https://edited-alpha.invalid/v1";
  document.getElementById("station-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await settle();
  assert.equal(managedStations[0].base_url, "https://edited-alpha.invalid/v1");
  const editCall = fetchCalls.filter((call) => call.method === "PUT").at(-1);
  assert.equal(editCall.body.api_key, undefined);
  assert.equal(editCall.body.probe_chat_model, "shared-model");
  assert.match(document.getElementById("stationFormStatus").textContent, /已保存/);

  maliciousManagedRow = [...document.querySelectorAll("[data-station-row]")]
    .find((row) => row.dataset.stationRow === maliciousStation);
  maliciousManagedRow.querySelector('[data-manage-action="edit"]').click();
  document.getElementById("editor-tab-models").click();
  assert.equal(probeModelSelect.value, "shared-model");
  const generationButton = document.getElementById("sf-generation-test");
  assert.equal(generationButton.disabled, false);
  const settingsWritesBeforeTest = fetchCalls.filter(call => call.url.includes("/settings/") && call.method !== "GET").length;
  generationButton.click();
  generationButton.click();
  await settle();
  assert.equal(fetchCalls.filter(call => call.url.endsWith("/generation-probe")).length, 1);
  assert.match(document.getElementById("sf-generation-result").textContent, /shared-model 生成成功/);
  assert.equal(fetchCalls.filter(call => call.url.includes("/settings/") && call.method !== "GET").length, settingsWritesBeforeTest);
  document.getElementById("sf-cancel").click();

  document.getElementById("stations-add").click();
  const providerSelect = document.getElementById("sf-provider");
  providerSelect.value = "opencode";
  providerSelect.dispatchEvent(new window.Event("change"));
  assert.equal(document.getElementById("sf-base-url").value, "https://opencode.ai/inference/go/openai/v1");
  assert.equal(document.getElementById("sf-base-url").readOnly, true);
  assert.equal(document.getElementById("sf-model-routing").value, "catalog");
  assert.equal(document.getElementById("sf-model-routing").disabled, true);
  assert.equal(document.getElementById("sf-probe-models").checked, true);
  assert.equal(document.getElementById("sf-probe-models").disabled, true);
  assert.equal(document.getElementById("sf-probe-chat").checked, false);
  assert.equal(document.getElementById("sf-probe-interval").value, "3600");
  assert.equal(document.getElementById("sf-models").closest('details').hidden, true);
  const planSelect = document.getElementById("sf-opencode-plan");
  assert.equal(document.getElementById("sf-opencode-plan-field").hidden, false);
  planSelect.value = 'go';
  planSelect.dispatchEvent(new window.Event('change'));
  assert.equal(document.getElementById("sf-base-url").value, 'https://opencode.ai/inference/go/openai/v1');
  assert.equal(document.getElementById("sf-billing").value, 'subscription');
  assert.equal(document.getElementById("sf-billing").disabled, true);
  assert.equal(document.getElementById("sf-probe-chat").disabled, true);
  assert.equal(document.getElementById("sf-generation-test").disabled, true);
  assert.match(document.getElementById("sf-provider-note").textContent, /\$0.*不代表订阅不可用/);
  planSelect.value = 'balance';
  planSelect.dispatchEvent(new window.Event('change'));
  assert.equal(document.getElementById("sf-base-url").value, 'https://opencode.ai/inference/openai/v1');
  assert.equal(document.getElementById("sf-billing").value, 'balance');
  assert.equal(document.getElementById("sf-probe-chat").disabled, false);
  document.getElementById("sf-cancel").click();
  document.getElementById("stations-add").click();
  assert.equal(providerSelect.value, "openai");
  assert.equal(planSelect.value, 'go');
  assert.equal(document.getElementById("sf-opencode-plan-field").hidden, true);
  assert.equal(document.getElementById("sf-billing").disabled, false);
  assert.equal(document.getElementById("sf-base-url").readOnly, false);
  assert.equal(document.getElementById("sf-model-routing").disabled, false);
  assert.equal(document.getElementById("sf-models").closest('details').hidden, false);
  document.getElementById("sf-name").value = "gamma";
  assert.equal(document.getElementById("sf-priority-auto").checked, true);
  assert.equal(document.getElementById("sf-priority").disabled, true);
  assert.equal(document.getElementById("sf-probe-models").checked, true);
  assert.equal(document.getElementById("sf-probe-chat").checked, false);
  document.getElementById("sf-priority-auto").checked = false;
  document.getElementById("sf-priority-auto").dispatchEvent(new window.Event("change"));
  assert.equal(document.getElementById("sf-priority").disabled, false);
  document.getElementById("sf-priority-auto").checked = true;
  document.getElementById("sf-priority-auto").dispatchEvent(new window.Event("change"));
  document.getElementById("sf-base-url").value = "https://gamma.invalid/v1";
  document.getElementById("sf-api-key").value = "secret-gamma";
  document.getElementById("sf-models").value = "model-g1, model-g2";
  document.getElementById("sf-route-models").value = "model-g1";
  document.getElementById("sf-route-models").dispatchEvent(new window.Event("input"));
  assert.ok(document.getElementById("sf-routing-note").textContent.includes('1 / 2'));
  document.getElementById("station-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await settle();
  assert.ok(managedStations.some((station) => station.name === "gamma"));
  assert.equal(managedStations.find(station => station.name === "gamma").priority_auto, true);
  assert.equal(managedStations.find(station => station.name === "gamma").model_routing, "catalog");
  assert.deepEqual(managedStations.find(station => station.name === "gamma").route_models, ["model-g1"]);
  assert.match(document.getElementById("stationFormStatus").textContent, /已添加并保存/);
  assert.equal(document.getElementById("sf-submit").disabled, false);

  let gammaRow = [...document.querySelectorAll("[data-station-row]")]
    .find((row) => row.dataset.stationRow === "gamma");
  gammaRow.querySelector('[data-manage-action="toggle"]').click();
  await settle();
  assert.equal(managedStations.find((station) => station.name === "gamma").enabled, false);
  assert.match(document.getElementById("stationListStatus").textContent, /已停用并保存/);

  gammaRow = [...document.querySelectorAll("[data-station-row]")]
    .find((row) => row.dataset.stationRow === "gamma");
  gammaRow.querySelector('[data-manage-action="delete"]').click();
  await settle();
  assert.equal(managedStations.some((station) => station.name === "gamma"), false);
  assert.match(document.getElementById("stationListStatus").textContent, /已删除并保存/);

  const pendingList = {};
  delayedStationList = pendingList;
  document.getElementById("stations-refresh").click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(typeof pendingList.resolve, "function");
  document.getElementById("stations-add").click();
  document.getElementById("sf-name").value = "queued";
  document.getElementById("sf-base-url").value = "https://queued.invalid/v1";
  document.getElementById("sf-api-key").value = "secret-queued";
  document.getElementById("station-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  pendingList.resolve();
  await settle();
  assert.ok([...document.querySelectorAll("[data-station-row]")]
    .some((row) => row.dataset.stationRow === "queued"));

  window.localStorage.removeItem("zg_admin_key");
  challengeNextStationWrite = true;
  stationWriteRejected = false;
  const retryBody = {
    name: "auth-site",
    base_url: "https://auth-site.invalid/v1",
    api_key: "secret-auth",
    models: [],
    priority: 5,
    billing: "balance",
    probe_chat: false,
    probe_models: false,
    enabled: true,
  };
  const authenticatedWrite = window.__testApi("/admin/stations", "POST", true, retryBody);
  await settle();
  assert.equal(document.getElementById("authDialog").open, true);
  document.getElementById("adminKeyInput").value = "admin-test-key";
  document.getElementById("authForm").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await authenticatedWrite;
  const authCalls = fetchCalls.filter((call) => call.method === "POST"
    && call.url === "/admin/stations" && call.body?.name === "auth-site");
  assert.equal(authCalls.length, 2);
  assert.equal(authCalls[1].body.api_key, "secret-auth");
  assert.equal(authCalls[1].headers.Authorization, "Bearer admin-test-key");

  document.getElementById("tab-stats").click();

  statusFixture = baseStatus({
    active: false,
    inflight: 0,
    active_routes: [],
    active_stations: [],
    last_attempt: null,
    last_success: { station: "beta", model: "shared-model", ts: now - 12 },
  });
  await new Promise((resolve) => setTimeout(resolve, 3100));
  await settle();
  assert.equal(document.getElementById("routeLabel").textContent, "最近使用");
  assert.equal(document.querySelector("#routeList .route-item-station").textContent, "beta");
  assert.equal(document.querySelector("#routeList .route-item-model").textContent, "shared-model");
  assert.match(document.getElementById("routeMeta").textContent, /秒前|刚刚/);

  statsFixture = {
    date: "2026-08-17",
    total_calls: 3,
    total_failures: 0,
    total_tokens: 0,
    usage_reports: 0,
    token_coverage: 0,
    token_coverage_known: true,
    stations: [{
      name: "zero",
      calls: 3,
      failures: 0,
      tokens: 0,
      usage_reports: 0,
      token_coverage_known: true,
      models: [{ model: "known-model", calls: 3, tokens: 0, usage_reports: 0 }],
    }],
    timeline: { zero: Array(24).fill(0) },
  };
  const realNow = window.Date.now;
  window.Date.now = () => realNow() + 31000;
  document.getElementById("tab-monitor").click();
  document.getElementById("tab-stats").click();
  window.Date.now = realNow;
  await settle();
  assert.equal(document.querySelectorAll(".donut-segment").length, 0);
  assert.equal(document.querySelectorAll(".usage-tab").length, 1);
  assert.match(document.getElementById("chartInspector").textContent, /3 次调用尚无可统计 Token/);

  statsFixture = {
    ...statsFixture,
    total_calls: 1,
    total_tokens: 100,
    usage_reports: 1,
    token_coverage: 1,
    stations: [{
      name: "only",
      calls: 1,
      failures: 0,
      tokens: 100,
      usage_reports: 1,
      token_coverage_known: true,
      models: [{ model: "only-model", calls: 1, tokens: 100, usage_reports: 1 }],
    }],
    timeline: { only: Array(24).fill(0) },
  };
  window.Date.now = () => realNow() + 62000;
  document.getElementById("tab-monitor").click();
  document.getElementById("tab-stats").click();
  window.Date.now = realNow;
  await settle();
  assert.equal(document.querySelectorAll(".donut-segment.is-station").length, 0);
  assert.equal(document.querySelectorAll(".donut-segment.is-model").length, 1);
  document.querySelectorAll(".donut-segment").forEach((segment) => {
    assert.match(segment.getAttribute("stroke-dasharray"), / 0\.000$/);
  });

  assert.equal(window.__testFormatters.finiteNumber(null, Number.NaN).toString(), 'NaN');
  assert.doesNotMatch(window.__testFormatters.formatBalance(null, 5), /\$0/);
  assert.match(window.__testFormatters.formatBalance(0, 5), /\$0\.00/);
  assert.match(window.__testFormatters.formatBalance(null, 5, {provider: 'opencode', opencode_plan: 'go'}), /Go 订阅/);
  assert.doesNotMatch(window.__testFormatters.formatLatency(null), /0ms/);
  dom.window.close();
  clearTimeout(watchdog);
  console.log("dashboard interaction checks passed");
}

main().catch((error) => {
  clearTimeout(watchdog);
  console.error(error);
  process.exit(1);
});
