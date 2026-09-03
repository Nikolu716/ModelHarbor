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
  let logsFixture = {
    events: Array.from({ length: 20 }, (_, index) => ({
      ts: now - index,
      station: index % 2 ? "beta" : maliciousStation,
      model: "shared-model",
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

  function jsonResponse(payload, status = 200) {
    return {
      status,
      ok: status >= 200 && status < 300,
      json: async () => payload,
    };
  }

  const html = fs.readFileSync(path.join(ROOT, "app", "web", "static", "index.html"), "utf8");
  const script = fs.readFileSync(path.join(ROOT, "app", "web", "static", "dashboard.js"), "utf8");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://127.0.0.1:8000/dashboard" });
  const { window } = dom;
  window.requestAnimationFrame = (callback) => callback();
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
    const stationActionMatch = url.match(/^\/admin\/station\/([^?]+)\/(toggle|probe|reset)$/);
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
      station.quota_exhausted = false;
      return jsonResponse({ name: stationName, quota_exhausted: false });
    }
    if (url.includes("/stats/daily")) return jsonResponse(statsFixture);
    if (url.includes("/admin/logs")) return jsonResponse(logsFixture);
    return jsonResponse(statusFixture);
  };
  if (!window.HTMLDialogElement.prototype.showModal) {
    window.HTMLDialogElement.prototype.showModal = function showModal() { this.open = true; };
  }
  if (!window.HTMLDialogElement.prototype.close) {
    window.HTMLDialogElement.prototype.close = function close() { this.open = false; };
  }
  window.eval(`${script}\n;globalThis.__testConsumeActivityEvent = consumeActivityEvent;globalThis.__testDrainActivityBuffer = drainActivityBuffer;globalThis.__testApi = api;`);
  await settle();

  const document = window.document;
  assert.equal(document.getElementById("routeLabel").textContent, "正在使用");
  assert.match(document.getElementById("routeStation").textContent, /\+1$/);
  assert.equal(document.getElementById("routeMeta").textContent, "2 路进行中");
  assert.equal(window.pwned, undefined);

  window.__testConsumeActivityEvent('event: activity\ndata: {"route_activity":{"revision":99,"active":true,"inflight":1,"active_routes":[{"station":"beta","model":"instant-model"}],"active_stations":[{"station":"beta","models":[{"model":"instant-model"}]}]}}');
  assert.equal(document.getElementById("routeLabel").textContent, "正在使用");
  assert.equal(document.getElementById("routeStation").textContent, "beta");
  assert.match(document.getElementById("routeModel").textContent, /instant-model/);
  let splitFrame = 'event: activity\r';
  splitFrame = window.__testDrainActivityBuffer(splitFrame);
  assert.equal(document.getElementById("routeStation").textContent, "beta");
  splitFrame += '\ndata: {"route_activity":{"revision":100,"active":true,"inflight":1,"active_routes":[{"station":"alpha","model":"split-model"}],"active_stations":[{"station":"alpha","models":[{"model":"split-model"}]}]}}\r\n\r\n';
  splitFrame = window.__testDrainActivityBuffer(splitFrame);
  assert.equal(splitFrame, "");
  assert.equal(document.getElementById("routeStation").textContent, "alpha");
  assert.match(document.getElementById("routeModel").textContent, /split-model/);
  let mixedFrame = 'event: activity\r\ndata: {"route_activity":{"revision":101,"active":true,"inflight":1,"active_routes":[{"station":"beta","model":"mixed-model"}],"active_stations":[{"station":"beta","models":[{"model":"mixed-model"}]}]}}\n\r\n';
  assert.equal(window.__testDrainActivityBuffer(mixedFrame), "");
  assert.match(document.getElementById("routeModel").textContent, /mixed-model/);

  document.getElementById("tab-stats").click();
  await settle();

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
  assert.match(document.getElementById("brandMark").getAttribute("src"), /brand-mark\.svg/);
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
  assert.equal(document.querySelectorAll("#log-body tr").length, 20);
  assert.equal(document.getElementById("eventPageInfo").textContent, "第 1 / 3 页");
  assert.equal(document.getElementById("eventPrev").disabled, true);
  assert.equal(document.getElementById("eventNext").disabled, false);
  logsFixture = { ...logsFixture, page: 2 };
  document.getElementById("eventNext").click();
  await settle();
  assert.equal(document.getElementById("eventPageInfo").textContent, "第 2 / 3 页");
  assert.equal(document.getElementById("eventPrev").disabled, false);

  document.getElementById("tab-retry").click();
  await settle();
  assert.equal(document.getElementById("retry-wrap").hidden, false);
  document.getElementById("tab-retry").focus();
  document.getElementById("tab-retry").dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "ArrowRight", bubbles: true,
  }));
  assert.equal(document.getElementById("tab-events").getAttribute("aria-selected"), "true");
  document.getElementById("tab-events").dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "ArrowLeft", bubbles: true,
  }));
  assert.equal(document.getElementById("tab-retry").getAttribute("aria-selected"), "true");
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

  document.getElementById("tab-stations").click();
  await settle();
  assert.equal(document.querySelectorAll("#stations-body tr").length, 2);
  assert.equal(document.querySelectorAll("#stations-body img, #stations-body script").length, 0);
  assert.equal(window.pwned, undefined);

  let maliciousManagedRow = [...document.querySelectorAll("[data-station-row]")]
    .find((row) => row.dataset.stationRow === maliciousStation);
  maliciousManagedRow.querySelector('[data-manage-action="edit"]').click();
  assert.equal(document.getElementById("sf-name").disabled, true);
  assert.equal(document.getElementById("sf-api-key").required, false);
  assert.equal(document.getElementById("sf-api-key").value, "");
  document.getElementById("sf-base-url").value = "https://edited-alpha.invalid/v1";
  document.getElementById("station-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await settle();
  assert.equal(managedStations[0].base_url, "https://edited-alpha.invalid/v1");
  const editCall = fetchCalls.filter((call) => call.method === "PUT").at(-1);
  assert.equal(editCall.body.api_key, undefined);
  assert.match(document.getElementById("stationFormStatus").textContent, /已保存/);

  document.getElementById("stations-add").click();
  document.getElementById("sf-name").value = "gamma";
  document.getElementById("sf-base-url").value = "https://gamma.invalid/v1";
  document.getElementById("sf-api-key").value = "secret-gamma";
  document.getElementById("sf-models").value = "model-g1, model-g2";
  document.getElementById("station-form").dispatchEvent(new window.Event("submit", {
    bubbles: true, cancelable: true,
  }));
  await settle();
  assert.ok(managedStations.some((station) => station.name === "gamma"));
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
  assert.equal(document.getElementById("routeStation").textContent, "beta");
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

  dom.window.close();
  clearTimeout(watchdog);
  console.log("dashboard interaction checks passed");
}

main().catch((error) => {
  clearTimeout(watchdog);
  console.error(error);
  process.exit(1);
});
