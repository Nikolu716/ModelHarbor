"use strict";

const $ = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat("zh-CN");
const percentFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const compactNumberFormat = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});
const CHART_TONES = 6;

let statusData = null;
let statsData = null;
let currentView = ["list", "card"].includes(localStorage.getItem("zg_view"))
  ? localStorage.getItem("zg_view")
  : "list";
let currentTab = ["monitor", "stats", "events", "stations", "retry"].includes(localStorage.getItem("zg_tab"))
  ? localStorage.getItem("zg_tab")
  : "monitor";
let refreshTimer = null;
let refreshRunning = false;
let refreshRequested = false;
let statsRunning = false;
let statsUpdatedAt = 0;
let eventsData = null;
let eventsRunning = false;
let eventsUpdatedAt = 0;
let stationsUpdatedAt = 0;
let retrySettingsUpdatedAt = 0;
let retrySettingsLoading = false;
let retrySettingsSaving = false;
let retrySettingsData = null;
let eventPage = 1;
const EVENT_PAGE_SIZE = 20;
let renderFingerprint = "";
let toastTimer = null;
let authPending = null;
let authResolve = null;
let usageStation = "";
let cacheWindow = ["hour", "best_hour", "today", "d7", "d31"].includes(localStorage.getItem("zg_cache_window"))
  ? localStorage.getItem("zg_cache_window")
  : "today";
const CACHE_WINDOWS = [
  { id: "hour", label: "本小时" },
  { id: "best_hour", label: "最佳1小时" },
  { id: "today", label: "今日" },
  { id: "d7", label: "近7天" },
  { id: "d31", label: "近31天" },
];
let chartSelection = { station: "", model: "" };
let chartPreview = null;
let activityRevision = -1;
let activityStreamController = null;
let activityStreamRetryTimer = null;
let activityStreamRetryDelay = 1000;
const pendingActions = new Set();

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  })[char]);
}

function finiteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, finiteNumber(value)));
}

function applyDataWidths(container) {
  container.querySelectorAll("[data-width]").forEach((bar) => {
    bar.style.width = `${clamp(bar.dataset.width, 0, 100)}%`;
  });
}

function formatCount(value) {
  return numberFormat.format(Math.max(0, finiteNumber(value)));
}

function formatCompactCount(value) {
  const count = Math.max(0, finiteNumber(value));
  return count < 10000 ? formatCount(count) : compactNumberFormat.format(count);
}

function formatShare(value) {
  return `${percentFormat.format(clamp(value, 0, 1) * 100)}%`;
}

function formatHitRate(rate, known = true) {
  if (known === false || rate == null || !Number.isFinite(Number(rate))) return "未回传";
  return `${percentFormat.format(clamp(rate, 0, 1) * 100)}%`;
}

function quotaRecoveryHint(station, gateway = {}) {
  if (!station.quota_exhausted) return "";
  const auto = gateway.quota_auto_recover !== false;
  const error = station.quota_probe_error || station.quota_error || "";
  if (!auto) {
    return `配额问题: ${error || "请充值或续费后重置"}`;
  }
  const lastAt = finiteNumber(station.quota_probe_at) || finiteNumber(station.quota_at);
  const interval = finiteNumber(station.quota_probe_backoff)
    || finiteNumber(gateway.quota_probe_interval, 60);
  const last = lastAt > 0 ? formatAge(lastAt) : "";
  let next = "等待首次恢复探测";
  if (lastAt > 0 && interval > 0) {
    const remain = Math.max(0, Math.floor(lastAt + interval - Date.now() / 1000));
    next = remain <= 0 ? "即将再探" : `${remain} 秒后再探`;
  }
  const lastText = last ? `上次探测 ${last}` : "尚未恢复探测";
  return `配额耗尽，正在自动探测恢复（${lastText}，${next}）。仍无额度将继续探测，无需手动重置。${error ? ` ${error}` : ""}`;
}

function formatTime(timestamp) {
  const seconds = finiteNumber(timestamp, Number.NaN);
  if (!Number.isFinite(seconds) || seconds <= 0) return "-";
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function formatAge(timestamp) {
  const seconds = finiteNumber(timestamp, Number.NaN);
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  const age = Math.max(0, Math.floor(Date.now() / 1000 - seconds));
  if (age < 5) return "刚刚";
  if (age < 60) return `${age} 秒前`;
  if (age < 3600) return `${Math.floor(age / 60)} 分钟前`;
  if (age < 86400) return `${Math.floor(age / 3600)} 小时前`;
  return `${Math.floor(age / 86400)} 天前`;
}

function formatLatency(seconds) {
  const value = finiteNumber(seconds, Number.NaN);
  if (!Number.isFinite(value) || value < 0) return '<span class="muted">-</span>';
  const className = value < 2 ? "is-fast" : (value < 10 ? "is-slow" : "is-bad");
  // 亚秒级用毫秒显示，避免 0.00s 吞掉有效数据（如 0.3ms）
  const text = value < 1 ? `${(value * 1000).toFixed(0)}ms` : `${value.toFixed(2)}s`;
  return `<span class="latency-value ${className}">${text}</span>`;
}

function formatBalance(balance, threshold) {
  const value = finiteNumber(balance, Number.NaN);
  if (!Number.isFinite(value)) return '<span class="muted">-</span>';
  const limit = finiteNumber(threshold, Number.NaN);
  const className = Number.isFinite(limit) ? (value < limit ? "is-low" : "is-ok") : "";
  return `<span class="balance-value ${className}">$${value.toFixed(2)}</span>`;
}

function scoreState(score) {
  const value = clamp(score, 0, 100);
  if (value >= 70) return "is-good";
  if (value >= 40) return "is-warn";
  return "is-bad";
}

function statusInfo(station) {
  if (!station.enabled) return { className: "unknown", label: "已禁用" };
  if (station.quota_exhausted) return { className: "quota", label: "配额耗尽" };
  switch (station.status) {
    case "up": return { className: "up", label: "在线" };
    case "down": return { className: "down", label: "熔断" };
    case "cooling": return { className: "cooling", label: "冷却中" };
    default: return { className: "unknown", label: "未知" };
  }
}

function sortedStations(stations) {
  return [...(stations || [])].sort((left, right) => {
    const leftRank = finiteNumber(left.rank, Number.MAX_SAFE_INTEGER);
    const rightRank = finiteNumber(right.rank, Number.MAX_SAFE_INTEGER);
    return leftRank - rightRank || String(left.name).localeCompare(String(right.name), "zh-CN");
  });
}

function setSyncState(kind, message) {
  const element = $("syncState");
  element.classList.toggle("is-loading", kind === "loading");
  element.classList.toggle("is-online", kind === "online");
  element.classList.toggle("is-error", kind === "error");
  $("syncText").textContent = message;
}

function showToast(message, isError = false) {
  const toast = $("toast");
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.hidden = false;
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, isError ? 5200 : 2800);
}

function settleAuth(accepted) {
  if (!authPending || !authResolve) return;
  const resolve = authResolve;
  authPending = null;
  authResolve = null;
  if ($("authDialog").open) $("authDialog").close();
  resolve(accepted);
}

function requestAdminKey() {
  if (authPending) return authPending;
  authPending = new Promise((resolve) => {
    authResolve = resolve;
  });
  $("adminKeyInput").value = "";
  $("authDialog").showModal();
  requestAnimationFrame(() => $("adminKeyInput").focus());
  return authPending;
}

async function api(path, method = "GET", allowAuthRetry = true, body = null) {
  const headers = {};
  const key = localStorage.getItem("zg_admin_key");
  if (key) headers.Authorization = `Bearer ${key}`;
  if (body) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    method, headers, cache: "no-store",
    body: body ? JSON.stringify(body) : undefined,
  });
  if (response.status === 401 && path.startsWith("/admin/") && allowAuthRetry) {
    localStorage.removeItem("zg_admin_key");
    if (await requestAdminKey()) return api(path, method, false, body);
    throw new Error("需要管理密钥");
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // Some upstream failures have no JSON body.
  }
  if (!response.ok) {
    const detail = payload?.detail || payload?.error?.message;
    throw new Error(detail || `${method} ${path} 返回 ${response.status}`);
  }
  return payload;
}

function applyActivitySnapshot(activity) {
  if (!activity || typeof activity !== "object") return;
  const revision = finiteNumber(activity.revision, -1);
  if (revision >= 0 && revision < activityRevision) return;
  if (revision >= 0) activityRevision = revision;
  if (!statusData) return;
  statusData = {
    ...statusData,
    route_activity: activity,
    active_routes: activity.active_routes || [],
    active_stations: activity.active_stations || [],
    last_attempt: activity.last_attempt || null,
    last_success: activity.last_success || null,
  };
  renderRouteStatus(statusData);
}

function consumeActivityEvent(block) {
  let eventType = "message";
  const dataLines = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) eventType = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  });
  if (eventType !== "activity" || !dataLines.length) return false;
  try {
    const payload = JSON.parse(dataLines.join("\n"));
    applyActivitySnapshot(payload.route_activity);
    activityStreamRetryDelay = 1000;
    return true;
  } catch {
    // Ignore one malformed frame; the status poll remains the fallback.
    return false;
  }
}

function drainActivityBuffer(buffer) {
  let boundary = buffer.match(/\r\n\r\n|\r\n\n|\n\r\n|\n\n|\r\r/);
  while (boundary && boundary.index != null) {
    const block = buffer.slice(0, boundary.index).replace(/\r\n?|\n/g, "\n");
    consumeActivityEvent(block);
    buffer = buffer.slice(boundary.index + boundary[0].length);
    boundary = buffer.match(/\r\n\r\n|\r\n\n|\n\r\n|\n\n|\r\r/);
  }
  return buffer;
}

function scheduleActivityStreamRetry() {
  if (activityStreamRetryTimer || document.visibilityState === "hidden") return;
  const delay = activityStreamRetryDelay;
  activityStreamRetryDelay = Math.min(activityStreamRetryDelay * 2, 30000);
  activityStreamRetryTimer = setTimeout(() => {
    activityStreamRetryTimer = null;
    connectActivityStream();
  }, delay);
}

async function connectActivityStream() {
  if (activityStreamController) return;
  const controller = new AbortController();
  activityStreamController = controller;
  let shouldRetry = false;
  try {
    const headers = { Accept: "text/event-stream" };
    const key = localStorage.getItem("zg_admin_key");
    if (key) headers.Authorization = `Bearer ${key}`;
    const response = await fetch("/admin/activity/stream", {
      headers,
      cache: "no-store",
      signal: controller.signal,
    });
    if (response.status === 401) return;
    if (!response.ok) throw new Error(`activity stream returned ${response.status}`);
    if (!response.body?.getReader) return;

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          buffer += decoder.decode();
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        buffer = drainActivityBuffer(buffer);
      }
      buffer = drainActivityBuffer(buffer);
      if (buffer.trim()) consumeActivityEvent(buffer.replace(/\r\n?|\n/g, "\n"));
    } finally {
      reader.releaseLock();
    }
    shouldRetry = true;
  } catch (error) {
    shouldRetry = error?.name !== "AbortError";
  } finally {
    if (activityStreamController === controller) activityStreamController = null;
    if (shouldRetry) scheduleActivityStreamRetry();
  }
}

function ensureActivityStream() {
  if (!activityStreamController && !activityStreamRetryTimer) connectActivityStream();
}

function mergeStatusActivity(nextStatus) {
  const nextActivity = nextStatus?.route_activity;
  const nextRevision = finiteNumber(nextActivity?.revision, -1);
  if (statusData?.route_activity && nextRevision >= 0 && nextRevision < activityRevision) {
    return { ...nextStatus, route_activity: statusData.route_activity };
  }
  if (nextRevision >= 0) activityRevision = nextRevision;
  return nextStatus;
}

function applyTab() {
  const monitorActive = currentTab === "monitor";
  const statsActive = currentTab === "stats";
  const eventsActive = currentTab === "events";
  const stationsActive = currentTab === "stations";
  const retryActive = currentTab === "retry";
  ["monitor", "stats", "stations", "retry", "events"].forEach(k => {
    $("tab-" + k).classList.toggle("is-active", currentTab === k);
    $("tab-" + k).setAttribute("aria-selected", String(currentTab === k));
    $("tab-" + k).setAttribute("tabindex", currentTab === k ? "0" : "-1");
  });
  $("monitor-wrap").hidden = !monitorActive;
  $("stats-wrap").hidden = !statsActive;
  $("events-wrap").hidden = !eventsActive;
  $("stations-wrap").hidden = !stationsActive;
  $("retry-wrap").hidden = !retryActive;
  $("viewSwitch").hidden = !monitorActive;
}

function setTab(tab) {
  if (!["monitor", "stats", "events", "stations", "retry"].includes(tab)) return;
  currentTab = tab;
  localStorage.setItem("zg_tab", tab);
  applyTab();
  if (tab === "stats" && Date.now() - statsUpdatedAt > 30000) refreshStats();
  if (tab === "events" && Date.now() - eventsUpdatedAt > 5000) refreshEvents();
  if (tab === "stations" && Date.now() - stationsUpdatedAt > 10000) {
    refreshStations({ notifyError: true });
  }
  if (tab === "retry" && Date.now() - retrySettingsUpdatedAt > 30000) {
    refreshRetrySettings({ notify: true });
  }
}

function applyView() {
  const listActive = currentView === "list";
  $("btn-list").classList.toggle("is-active", listActive);
  $("btn-card").classList.toggle("is-active", !listActive);
  $("btn-list").setAttribute("aria-pressed", String(listActive));
  $("btn-card").setAttribute("aria-pressed", String(!listActive));
  $("list-wrap").hidden = !listActive;
  $("grid").hidden = listActive;
}

function setView(view) {
  if (!["list", "card"].includes(view)) return;
  currentView = view;
  localStorage.setItem("zg_view", view);
  applyView();
}

function historySlots(history) {
  const targetLength = 60;
  const ok = Array.isArray(history?.ok) ? history.ok.slice(-targetLength) : [];
  const fail = Array.isArray(history?.fail) ? history.fail.slice(-targetLength) : [];
  while (ok.length < targetLength) ok.unshift(0);
  while (fail.length < targetLength) fail.unshift(0);
  return { ok, fail };
}

function probeHtml(history) {
  const slots = historySlots(history);
  let hasData = false;
  const cells = slots.ok.map((okCount, index) => {
    const failCount = finiteNumber(slots.fail[index]);
    const successCount = finiteNumber(okCount);
    let state = "is-empty";
    if (successCount > 0 && failCount === 0) state = "is-ok";
    else if (successCount > 0) state = "is-warn";
    else if (failCount > 0) state = "is-fail";
    if (successCount > 0 || failCount > 0) hasData = true;
    const minutesAgo = 59 - index;
    const timeLabel = minutesAgo === 0 ? "当前分钟" : `${minutesAgo} 分钟前`;
    const title = `${timeLabel}: 成功 ${successCount} 次 / 失败 ${failCount} 次`;
    return `<span class="probe-cell ${state}" title="${escapeHtml(title)}"></span>`;
  }).join("");
  const label = hasData ? "最近 60 分钟可用性" : "最近 60 分钟暂无数据";
  return `<div class="probe" role="img" aria-label="${label}">${cells}</div>`;
}

const PROBE_LEGEND = `
  <div class="probe-legend" aria-label="探针图例">
    <span class="probe-legend-item"><span class="legend-swatch is-ok"></span>正常</span>
    <span class="probe-legend-item"><span class="legend-swatch is-warn"></span>部分异常</span>
    <span class="probe-legend-item"><span class="legend-swatch is-fail"></span>故障</span>
    <span class="probe-legend-item"><span class="legend-swatch is-empty"></span>无数据</span>
  </div>`;

function actionButton(action, station, label, options = {}) {
  const key = `${action}:${station.name}`;
  const pending = pendingActions.has(key);
  const disabled = pending || options.disabled;
  const className = `action-button${options.destructive ? " is-destructive" : ""}`;
  const buttonLabel = pending ? "处理中" : label;
  return `<button class="${className}" type="button" data-action="${action}"
                  data-station="${escapeHtml(station.name)}" ${disabled ? "disabled" : ""}>${buttonLabel}</button>`;
}

function stationActions(station) {
  return [
    actionButton("toggle", station, station.enabled ? "禁用" : "启用", { destructive: station.enabled }),
    actionButton("probe", station, "探测", { disabled: !station.enabled }),
    station.quota_exhausted ? actionButton("reset", station, "立即探测恢复") : "",
  ].join("");
}

function renderRouteStatus(data) {
  const activity = data.route_activity || {};
  const activeRoutes = Array.isArray(activity.active_routes)
    ? activity.active_routes
    : (Array.isArray(activity.routes)
      ? activity.routes
      : (Array.isArray(data.active_routes) ? data.active_routes : []));
  let activeStations = Array.isArray(activity.active_stations)
    ? activity.active_stations
    : (Array.isArray(activity.stations)
      ? activity.stations
      : (Array.isArray(data.active_stations) ? data.active_stations : []));

  if (!activeStations.length && activeRoutes.length) {
    const grouped = new Map();
    activeRoutes.forEach((route) => {
      const station = String(route.station || route.name || "").trim();
      if (!station) return;
      const current = grouped.get(station) || { station, inflight: 0, models: [] };
      current.inflight += 1;
      const model = String(route.model || "").trim();
      if (model && !current.models.includes(model)) current.models.push(model);
      grouped.set(station, current);
    });
    activeStations = [...grouped.values()];
  }

  const inflight = Math.max(0, finiteNumber(
    activity.inflight ?? data.inflight ?? activeRoutes.length,
  ));
  const stationNames = activeStations
    .map((station) => String(station.station || station.name || "").trim())
    .filter(Boolean);
  const routeModels = activeRoutes.map((route) => String(route.model || "").trim());
  activeStations.forEach((station) => {
    const models = Array.isArray(station.models) ? station.models : [];
    models.forEach((model) => {
      const name = typeof model === "object" ? model?.model : model;
      routeModels.push(String(name || "").trim());
    });
  });
  const models = [...new Set(routeModels.filter(Boolean))];
  const isActive = inflight > 0 || stationNames.length > 0;
  const routeLive = $("routeLive");
  routeLive.classList.toggle("is-active", isActive);

  if (isActive) {
    const visibleStation = stationNames.length > 1
      ? `${stationNames[0]} +${stationNames.length - 1}`
      : (stationNames[0] || "路由处理中");
    $("routeLabel").textContent = "正在使用";
    $("routeStation").textContent = visibleStation;
    $("routeModel").textContent = models.length === 1 ? `· ${models[0]}` : (models.length > 1 ? `· ${models.length} 个模型` : "");
    $("routeMeta").textContent = `${formatCount(inflight || activeRoutes.length)} 路进行中`;
    routeLive.title = [stationNames.join("、"), models.join("、")].filter(Boolean).join(" · ");
    return;
  }

  const lastSuccess = activity.last_success || data.last_success || data.last_routed || {};
  const lastAttempt = activity.last_attempt || data.last_attempt || {};
  const recent = lastSuccess.station ? lastSuccess : lastAttempt;
  if (recent.station) {
    $("routeLabel").textContent = lastSuccess.station ? "最近使用" : "最近尝试";
    $("routeStation").textContent = String(recent.station);
    $("routeModel").textContent = recent.model ? `· ${recent.model}` : "";
    $("routeMeta").textContent = formatAge(recent.finished_at || recent.ts || recent.started_at);
    routeLive.title = [recent.station, recent.model].filter(Boolean).join(" · ");
    return;
  }

  $("routeLabel").textContent = "当前路由";
  $("routeStation").textContent = "等待首次路由";
  $("routeModel").textContent = "";
  $("routeMeta").textContent = "";
  routeLive.removeAttribute("title");
}

function renderSummary(data) {
  const stations = data.stations || [];
  const totalCalls = stations.reduce((sum, station) => sum + finiteNumber(station.calls), 0);
  const totalFailures = stations.reduce((sum, station) => sum + finiteNumber(station.failures), 0);
  const totalSwitches = stations.reduce((sum, station) => sum + finiteNumber(station.switch_out), 0);
  const successRate = totalCalls > 0 ? (totalCalls - totalFailures) / totalCalls * 100 : null;
  $("s-online").textContent = `${finiteNumber(data.summary?.up)}/${finiteNumber(data.summary?.total)}`;
  $("s-calls").textContent = formatCount(totalCalls);
  $("s-switch").textContent = formatCount(totalSwitches);
  $("s-rate").textContent = successRate == null ? "-" : `${percentFormat.format(successRate)}%`;

  const interval = finiteNumber(data.gateway?.probe_interval);
  const rounds = finiteNumber(data.gateway?.max_rounds ?? data.gateway?.max_attempts);
  const seconds = finiteNumber(data.gateway?.max_retry_seconds);
  const roundPolicy = rounds === 0 ? "不限轮次" : `最多 ${formatCount(rounds)} 轮`;
  const timePolicy = seconds === 0 ? "不限总时长" : `最长 ${formatCount(seconds)} 秒`;
  $("sub").textContent = `自动托管 · ${formatCount(interval)} 秒探测 · ${roundPolicy} · ${timePolicy}`;
  renderRouteStatus(data);
}

function renderCards(data) {
  const threshold = data.gateway?.balance_alert_threshold;
  $("grid").innerHTML = sortedStations(data.stations).map((station) => {
    const state = statusInfo(station);
    const score = clamp(station.health_score, 0, 100);
    const scoreClass = scoreState(score);
    const rate = clamp(finiteNumber(station.window_success_rate) * 100, 0, 100);
    const billing = station.billing === "subscription" ? "订阅制" : "余额制";
    const rateLimited = finiteNumber(station.rate_limit_until) > Date.now() / 1000;
    const models = station.accepts_any_model
      ? ['<span class="model-tag">接受任意模型</span>']
      : (station.models || []).slice(0, 6).map((model) => `<span class="model-tag" title="${escapeHtml(model)}">${escapeHtml(model)}</span>`);
    if (!station.accepts_any_model && (station.models || []).length > 6) {
      models.push(`<span class="model-tag">+${(station.models || []).length - 6}</span>`);
    }
    const recoveryHint = quotaRecoveryHint(station, data.gateway);
    const alert = recoveryHint
      ? `<p class="card-alert">${escapeHtml(recoveryHint)}</p>`
      : (station.last_error ? `<p class="card-alert">最近错误: ${escapeHtml(station.last_error)}</p>` : "");
    const probeValue = station.last_probe_ok == null
      ? "未探测"
      : (station.last_probe_ok ? "正常" : "失败");

    return `<article class="station-card ${station.enabled ? "" : "is-disabled"}">
      <header class="card-header">
        <div class="card-title-wrap">
          <div class="card-title"><span class="status-dot ${state.className}" aria-hidden="true"></span><span>${escapeHtml(station.name)}</span></div>
          <div class="card-meta">
            <span class="status-tag">${state.label}</span>
            <span class="status-tag">${billing}</span>
            <span class="status-tag">优先级 ${escapeHtml(station.priority)}</span>
            ${rateLimited ? '<span class="status-tag is-warn">限流中</span>' : ""}
            ${station.quota_exhausted ? '<span class="status-tag is-alert">配额耗尽</span>' : ""}
            ${station.quota_exhausted ? '<span class="status-tag is-warn">自动探测中</span>' : ""}
          </div>
        </div>
        <span class="card-rank">#${escapeHtml(station.rank || "-")}</span>
      </header>

      <div class="health-row">
        <span>健康评分</span>
        <div class="health-track" aria-hidden="true"><div class="health-bar ${scoreClass}" data-width="${score}"></div></div>
        <span class="health-number">${score.toFixed(1)}</span>
      </div>
      <div class="station-url" title="${escapeHtml(station.base_url)}">${escapeHtml(station.base_url)}</div>

      <div class="card-stats">
        <div class="card-stat"><span class="card-stat-label">延迟 EMA</span><span class="card-stat-value">${formatLatency(station.ema_latency)}</span></div>
        <div class="card-stat"><span class="card-stat-label">首字 TTFT</span><span class="card-stat-value">${formatLatency(station.ttft_ema)}</span></div>
        <div class="card-stat"><span class="card-stat-label">近 100 次成功率</span><span class="card-stat-value">${percentFormat.format(rate)}%</span></div>
        <div class="card-stat"><span class="card-stat-label">调用 / 失败</span><span class="card-stat-value">${formatCount(station.calls)} / ${formatCount(station.failures)}</span></div>
        <div class="card-stat"><span class="card-stat-label">额度</span><span class="card-stat-value">${formatBalance(station.balance, threshold)}</span></div>
        <div class="card-stat"><span class="card-stat-label">最近探测</span><span class="card-stat-value">${probeValue}</span></div>
        <div class="card-stat"><span class="card-stat-label">故障切换</span><span class="card-stat-value">${formatCount(station.switch_in)} 入 / ${formatCount(station.switch_out)} 出</span></div>
      </div>

      <div class="card-probe">${probeHtml(station.history_hour)}</div>
      <div class="models">${models.join("")}</div>
      ${alert}
      <div class="card-actions">${stationActions(station)}</div>
    </article>`;
  }).join("") || '<div class="empty-cell">暂无站点</div>';

  applyDataWidths($("grid"));
}

function renderList(data) {
  const threshold = data.gateway?.balance_alert_threshold;
  const rows = sortedStations(data.stations).map((station) => {
    const state = statusInfo(station);
    const score = clamp(station.health_score, 0, 100);
    const rate = clamp(finiteNumber(station.window_success_rate) * 100, 0, 100);
    const billing = station.billing === "subscription" ? "订阅" : "余额";
    const probeLabel = station.last_probe_ok == null ? "未探测" : (station.last_probe_ok ? "正常" : "失败");
    const probeClass = station.last_probe_ok == null ? "muted" : (station.last_probe_ok ? "text-ok" : "text-danger");
    const error = station.last_error || station.last_probe_error || "-";
    const rateLimited = finiteNumber(station.rate_limit_until) > Date.now() / 1000;

    return `<tr class="${station.enabled ? "" : "is-disabled"}">
      <td class="col-rank muted rank-value">#${escapeHtml(station.rank || "-")}</td>
      <td class="col-station">
        <div class="station-name" title="${escapeHtml(station.base_url)}">
          <span class="status-dot ${state.className}" aria-hidden="true"></span>
          <span class="station-name-text">${escapeHtml(station.name)}</span>
        </div>
        <div class="tag-row">
          ${station.quota_exhausted ? '<span class="status-tag is-alert">配额耗尽</span>' : ""}
          ${station.quota_exhausted ? '<span class="status-tag is-warn">自动探测中</span>' : ""}
          ${rateLimited ? '<span class="status-tag is-warn">限流中</span>' : ""}
        </div>
      </td>
      <td class="col-billing muted">${billing}</td>
      <td class="col-score"><span class="score-value ${scoreState(score)}">${score.toFixed(1)}</span></td>
      <td class="col-latency">${formatLatency(station.ema_latency)}</td>
      <td class="col-ttft">${formatLatency(station.ttft_ema ?? (station.avg_ttft_ms != null ? station.avg_ttft_ms / 1000 : null))}</td>
      <td class="col-cache">${station.cache_hit_rate == null ? '<span class="muted">-</span>' : (station.cache_hit_rate >= 0.8 ? `<span class="ok">${(station.cache_hit_rate * 100).toFixed(1)}%</span>` : `<span class="warn">${(station.cache_hit_rate * 100).toFixed(1)}%</span>`)}</td>
      <td class="col-juice" title="${juiceDetail(station)}">${station.juice_score == null ? '<span class="muted">-</span>' : (station.juice_score >= 90 ? `<span class="ok">${station.juice_score}</span>` : (station.juice_score >= 60 ? `<span class="warn">${station.juice_score}</span>` : `<span class="bad">${station.juice_score}</span>`))}</td>
      <td class="col-rate number-value">${percentFormat.format(rate)}%</td>
      <td class="col-calls number-value">${formatCount(station.calls)} <span class="text-danger">/ ${formatCount(station.failures)}</span></td>
      <td class="col-balance">${formatBalance(station.balance, threshold)}</td>
      <td class="col-probe"><span class="${probeClass}">${probeLabel}</span></td>
      <td class="col-checked muted">${formatTime(station.last_probe_at)}</td>
      <td class="col-error error-cell" title="${escapeHtml(error)}">${escapeHtml(error)}</td>
      <td class="col-actions"><div class="table-actions">${stationActions(station)}</div></td>
    </tr>`;
  }).join("");

  const probes = sortedStations(data.stations).map((station) => {
    const state = statusInfo(station);
    return `<div class="probe-row">
      <div class="probe-name"><span class="status-dot ${state.className}" aria-hidden="true"></span><span>${escapeHtml(station.name)}</span></div>
      ${probeHtml(station.history_hour)}
      <span class="probe-now">现在</span>
    </div>`;
  }).join("");

  $("list-wrap").innerHTML = `
    <div class="table-shell monitor-shell">
      <table class="monitor-table">
        <caption class="sr-only">中转站实时状态</caption>
        <thead><tr>
          <th class="col-rank">排名</th><th class="col-station">站点</th><th class="col-billing">计费</th>
          <th class="col-score">评分</th><th class="col-latency">延迟 EMA</th><th class="col-ttft">首字</th><th class="col-cache">缓存命中</th><th class="col-juice">Juice</th><th class="col-rate">成功率</th>
          <th class="col-calls">调用 / 失败</th><th class="col-balance">额度</th><th class="col-probe">探测</th>
          <th class="col-checked">最后检查</th><th class="col-error">最近错误</th><th class="col-actions">操作</th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="13" class="empty-cell">暂无站点</td></tr>'}</tbody>
      </table>
    </div>
    <section class="probe-block" aria-labelledby="probe-heading">
      <div class="probe-header">
        <span class="probe-title" id="probe-heading">60 分钟可用性</span>
        <span class="probe-range">60 分钟前 → 现在 · 每格 1 分钟</span>
      </div>
      <div class="probe-scroll"><div class="probe-rows">${probes}</div></div>
      ${PROBE_LEGEND}
    </section>`;
}

function juiceDetail(station) {
  const parts = [];
  if (station.model_mismatch > 0) parts.push(`model 回显不一致 ${station.model_mismatch}次`);
  const fams = station.upstream_families || {};
  for (const f of ["anthropic", "bedrock", "vertex"]) {
    if (fams[f] > 0) parts.push(`${f} 上游头 ${fams[f]}次`);
  }
  const fps = station.fingerprints || [];
  if (fps.length) {
    for (const fp of fps.slice(0, 3)) {
      const flagTxt = fp.flag === "mismatch" ? "⚠疑似不一致" : (fp.flag === "no_peer" ? "无对比对象" : (fp.flag === "collecting" ? "采集中" : "匹配"));
      parts.push(`${fp.model}: ${fp.jsd == null ? "-" : "JSD " + fp.jsd} ${flagTxt} (${fp.samples}样本)`);
    }
  }
  parts.push("Juice=模型匹配度(头家族+回显+指纹JSD)");
  return parts.join(String.fromCharCode(10));
}

function renderEvents(data) {
  const events = data.events || [];
  const total = Math.max(0, finiteNumber(data.total));
  const pages = Math.max(1, finiteNumber(data.pages, 1));
  const page = clamp(data.page || 1, 1, pages);
  eventPage = page;
  $("eventCount").textContent = `共 ${formatCount(total)} 条 · 最多保留 ${formatCount(data.max_events || 500)} 条`;
  $("log-body").innerHTML = events.map((event) => {
    const latency = finiteNumber(event.latency_ms, Number.NaN);
    const detail = event.detail || "-";
    const attempt = finiteNumber(event.attempts, 1);
    return `<tr>
      <td class="event-time muted">${formatTime(event.ts)}</td>
      <td class="event-station">${escapeHtml(event.station || "-")}</td>
      <td class="event-model">${escapeHtml(event.model || "-")}</td>
      <td class="event-result ${event.ok ? "text-ok" : "text-danger"}">${event.ok ? "成功" : "失败"}</td>
      <td class="event-latency">${Number.isFinite(latency) && latency > 0 ? `${(latency / 1000).toFixed(2)}s` : "-"}</td>
      <td class="event-detail" title="${escapeHtml(detail)}">${escapeHtml(detail)}${attempt > 1 ? ` <span class="attempt-count">(${attempt} 次尝试)</span>` : ""}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="6" class="empty-cell">暂无事件</td></tr>';
  $("eventPageInfo").textContent = `第 ${formatCount(page)} / ${formatCount(pages)} 页`;
  $("eventPrev").disabled = page <= 1 || eventsRunning;
  $("eventNext").disabled = page >= pages || eventsRunning;
}

function captureActionFocus() {
  const active = document.activeElement;
  if (!(active instanceof HTMLButtonElement) || !active.dataset.action) return null;
  return {
    action: active.dataset.action,
    station: active.dataset.station,
    container: active.closest("#grid") ? "grid" : "list-wrap",
  };
}

function restoreActionFocus(target) {
  if (!target) return;
  const buttons = $(target.container).querySelectorAll("button[data-action][data-station]");
  for (const button of buttons) {
    if (button.dataset.action === target.action && button.dataset.station === target.station && !button.disabled) {
      button.focus({ preventScroll: true });
      break;
    }
  }
}

function render(force = false) {
  if (!statusData) return;
  renderSummary(statusData);
  const fingerprint = JSON.stringify([statusData, [...pendingActions].sort()]);
  if (force || fingerprint !== renderFingerprint) {
    const focusTarget = captureActionFocus();
    renderCards(statusData);
    renderList(statusData);
    renderFingerprint = fingerprint;
    applyView();
    restoreActionFocus(focusTarget);
  }
}

function chartStations() {
  return (statsData?.stations || []).map((station, stationIndex) => ({
    name: String(station.name || ""),
    tokens: Math.max(0, finiteNumber(station.tokens)),
    calls: Math.max(0, finiteNumber(station.calls)),
    failures: Math.max(0, finiteNumber(station.failures)),
    usageReports: Math.max(0, finiteNumber(station.usage_reports)),
    coverageKnown: station.token_coverage_known !== false,
    cacheRead: Math.max(0, finiteNumber(station.cache_read)),
    cacheWrite: Math.max(0, finiteNumber(station.cache_write)),
    cacheHitRate: station.cache_hit_rate,
    cacheKnown: station.cache_known === true,
    tone: stationIndex % CHART_TONES,
    models: (station.models || []).map((model, modelIndex) => ({
      name: String(model.model || ""),
      tokens: Math.max(0, finiteNumber(model.tokens)),
      calls: Math.max(0, finiteNumber(model.calls)),
      failures: Math.max(0, finiteNumber(model.failures)),
      usageReports: Math.max(0, finiteNumber(model.usage_reports)),
      cacheRead: Math.max(0, finiteNumber(model.cache_read)),
      cacheHitRate: model.cache_hit_rate,
      cacheKnown: model.cache_known === true,
      tone: (stationIndex + modelIndex + 1) % CHART_TONES,
    })),
  }));
}

function chartItem(kind, stationName, modelName = "") {
  const station = chartStations().find((item) => item.name === stationName);
  if (!station) return null;
  if (kind !== "model") return { kind: "station", station };
  if (modelName === "__unclassified__") {
    const classified = station.models.reduce((sum, model) => sum + model.tokens, 0);
    const tokens = Math.max(0, station.tokens - classified);
    if (tokens <= 0) return null;
    return {
      kind: "model",
      station,
      model: {
        name: "未归类模型",
        tokens,
        calls: 0,
        usageReports: 0,
        unclassified: true,
      },
    };
  }
  const model = station.models.find((item) => item.name === modelName);
  return model ? { kind: "model", station, model } : null;
}

function ringSegment({ kind, station, model = "", radius, fraction, offset, tone, label }) {
  if (fraction <= 0) return "";
  const circumference = 2 * Math.PI * radius;
  const length = clamp(fraction, 0, 1) * circumference;
  const gap = fraction >= 0.9995 ? 0 : Math.min(1.6, length * 0.16);
  const visibleLength = Math.max(0.2, length - gap);
  const modelAttribute = kind === "model" ? ` data-model="${escapeHtml(model)}"` : "";
  return `<circle class="donut-segment is-${kind} chart-stroke-${tone}" cx="90" cy="90" r="${radius}"
      stroke-dasharray="${visibleLength.toFixed(3)} ${(circumference - visibleLength).toFixed(3)}"
      stroke-dashoffset="${(-offset * circumference).toFixed(3)}" tabindex="0" role="button"
      data-chart-kind="${kind}" data-station="${escapeHtml(station)}"${modelAttribute}
      aria-label="${escapeHtml(label)}"><title>${escapeHtml(label)}</title></circle>`;
}

function ensureUsageStation() {
  const stations = chartStations();
  if (!stations.some((station) => station.name === usageStation)) {
    usageStation = stations[0]?.name || "";
  }
  chartSelection.station = usageStation;
  if (chartSelection.model && !chartItem("model", usageStation, chartSelection.model)) {
    chartSelection.model = "";
  }
  return stations.find((station) => station.name === usageStation) || null;
}

function selectedChartItem() {
  if (!chartSelection.station) return null;
  return chartItem(
    chartSelection.model ? "model" : "station",
    chartSelection.station,
    chartSelection.model,
  );
}

function updateChartInspector(item = null) {
  const data = statsData || {};
  const totalTokens = Math.max(0, finiteNumber(data.total_tokens));
  const totalCalls = Math.max(0, finiteNumber(data.total_calls));
  const stations = chartStations();
  const inspector = $("chartInspector");
  const selectedStation = ensureUsageStation();
  let centerValue = selectedStation?.tokens || 0;
  let centerLabel = "站点 Token";

  if (item?.kind === "model") {
    const stationShare = item.station.tokens > 0 ? item.model.tokens / item.station.tokens : 0;
    const globalShare = totalTokens > 0 ? item.model.tokens / totalTokens : 0;
    centerValue = item.model.tokens;
    centerLabel = "模型 Token";
    inspector.innerHTML = `
      <span class="inspector-kicker">${escapeHtml(item.station.name)}</span>
      <strong class="inspector-name" title="${escapeHtml(item.model.name)}">${escapeHtml(item.model.name)}</strong>
      <span class="inspector-metrics"><strong>${formatCount(item.model.tokens)} Token</strong>
        <span>站内 ${formatShare(stationShare)}</span><span>全局 ${formatShare(globalShare)}</span>
        ${item.model.calls ? `<span>${formatCount(item.model.calls)} 次调用</span>` : ""}
        ${item.model.cacheKnown ? `<span>缓存命中 ${formatHitRate(item.model.cacheHitRate)}</span>` : ""}</span>`;
  } else if (item?.kind === "station") {
    const share = totalTokens > 0 ? item.station.tokens / totalTokens : 0;
    centerValue = item.station.tokens;
    centerLabel = "站点 Token";
    const coverage = item.station.coverageKnown
      ? `${formatCount(item.station.usageReports)}/${formatCount(item.station.calls)} 次已回传`
      : "覆盖率从本版本起统计";
    inspector.innerHTML = `
      <span class="inspector-kicker">站点</span>
      <strong class="inspector-name" title="${escapeHtml(item.station.name)}">${escapeHtml(item.station.name)}</strong>
      <span class="inspector-metrics"><strong>${formatCount(item.station.tokens)} Token</strong>
        <span>全局 ${formatShare(share)}</span><span>${formatCount(item.station.models.length)} 个模型</span>
        <span>${coverage}</span>
        <span>缓存命中 ${formatHitRate(item.station.cacheHitRate, item.station.cacheKnown)}</span></span>
      ${item.station.tokens === 0 ? `<span class="chart-empty">${formatCount(item.station.calls)} 次调用尚无可统计 Token</span>` : ""}`;
  } else if (selectedStation) {
    const share = totalTokens > 0 ? selectedStation.tokens / totalTokens : 0;
    const coverage = selectedStation.coverageKnown
      ? `${formatCount(selectedStation.usageReports)}/${formatCount(selectedStation.calls)} 次已回传`
      : "覆盖率从本版本起统计";
    inspector.innerHTML = `
      <span class="inspector-kicker">所选站点</span>
      <strong class="inspector-name" title="${escapeHtml(selectedStation.name)}">${escapeHtml(selectedStation.name)}</strong>
      <span class="inspector-metrics"><strong>${formatCount(selectedStation.tokens)} Token</strong>
        <span>全局 ${formatShare(share)}</span><span>${formatCount(selectedStation.models.length)} 个模型</span>
        <span>${coverage}</span>
        <span>缓存命中 ${formatHitRate(selectedStation.cacheHitRate, selectedStation.cacheKnown)}</span></span>
      ${selectedStation.tokens === 0 ? `<span class="chart-empty">${formatCount(selectedStation.calls)} 次调用尚无可统计 Token</span>` : ""}`;
  } else {
    const modelCount = stations.reduce((sum, station) => sum + station.models.length, 0);
    let coverage = "";
    if (data.token_coverage_known !== false) {
      coverage = `${formatCount(data.usage_reports)}/${formatCount(totalCalls)} 次已回传 Token`;
    } else {
      coverage = "Token 覆盖率从本版本起统计";
    }
    const empty = totalTokens === 0
      ? `<span class="chart-empty">${totalCalls ? `${formatCount(totalCalls)} 次调用尚无可统计 Token` : "今日暂无调用"}</span>`
      : "";
    inspector.innerHTML = `
      <span class="inspector-kicker">全部站点</span>
      <strong class="inspector-name">${formatCount(totalTokens)} 已统计 Token</strong>
      <span class="inspector-metrics"><span>${formatCount(stations.length)} 个站点</span>
        <span>${formatCount(modelCount)} 个模型</span><span>${coverage}</span>
        <span>缓存命中 ${formatHitRate(data.cache_hit_rate, data.cache_known)}</span></span>${empty}`;
  }

  $("donutValue").textContent = formatCompactCount(centerValue);
  $("donutValue").title = `${formatCount(centerValue)} Token`;
  $("donutLabel").textContent = centerLabel;
}

function updateChartInteraction() {
  const activeItem = chartPreview
    || (chartSelection.model ? selectedChartItem() : chartItem("station", usageStation));
  const donut = $("donut");
  donut.querySelectorAll(".donut-segment").forEach((segment) => {
    const sameStation = activeItem && segment.dataset.station === activeItem.station.name;
    const sameModel = activeItem?.kind === "model"
      && segment.dataset.chartKind === "model"
      && segment.dataset.model === (activeItem.model.unclassified ? "__unclassified__" : activeItem.model.name);
    const primary = activeItem?.kind === "station"
      ? sameStation && segment.dataset.chartKind === "station"
      : sameModel;
    const related = Boolean(activeItem && sameStation && !primary);
    segment.classList.toggle("is-hovered", Boolean(primary));
    segment.classList.toggle("is-related", related);
    segment.classList.toggle("is-dimmed", Boolean(activeItem?.kind === "model" && !primary));
  });

  $("modelBreakdown").querySelectorAll(".model-row").forEach((row) => {
    const selectedModel = chartSelection.station === row.dataset.station
      && chartSelection.model === row.dataset.model;
    row.classList.toggle("is-selected", Boolean(selectedModel));
    row.setAttribute("aria-pressed", String(Boolean(selectedModel)));
  });
  updateChartInspector(activeItem);
}

function renderModelBreakdown() {
  const breakdown = $("modelBreakdown");
  const station = ensureUsageStation();
  if (!station) {
    breakdown.hidden = true;
    breakdown.innerHTML = "";
    return;
  }

  const items = station.models.map((model) => ({ ...model, key: model.name }));
  const classified = station.models.reduce((sum, model) => sum + model.tokens, 0);
  const unclassified = Math.max(0, station.tokens - classified);
  if (unclassified > 0) {
    items.push({
      name: "未归类模型",
      key: "__unclassified__",
      tokens: unclassified,
      calls: 0,
      tone: station.tone,
    });
  }
  breakdown.innerHTML = items.map((model) => {
    const share = station.tokens > 0 ? model.tokens / station.tokens : 0;
    return `<button class="model-row" type="button" data-chart-kind="model"
        data-station="${escapeHtml(station.name)}" data-model="${escapeHtml(model.key)}"
        aria-pressed="false" aria-label="${escapeHtml(model.name)}, ${formatCount(model.tokens)} Token, 站内 ${formatShare(share)}">
      <span class="model-row-key">
        <span class="model-swatch chart-fill-${model.tone}" aria-hidden="true"></span>
        <span class="model-row-name" title="${escapeHtml(model.name)}">${escapeHtml(model.name)}</span>
      </span>
      <span class="model-row-value">${formatCompactCount(model.tokens)} Token · ${formatShare(share)} · ${formatCount(model.calls)} 次${model.cacheKnown ? ` · 命中 ${formatHitRate(model.cacheHitRate)}` : ""}</span>
    </button>`;
  }).join("") || '<span class="chart-empty">该站有调用，但上游尚未返回模型 Token</span>';
  breakdown.hidden = false;
}

function renderTokenChart() {
  const data = statsData || {};
  const stations = chartStations();
  const totalTokens = Math.max(0, finiteNumber(data.total_tokens));
  const selectedStation = ensureUsageStation();
  $("usageTabs").innerHTML = stations.map((station, index) => `
    <button id="usage-tab-${index}" class="usage-tab ${station.name === usageStation ? "is-active" : ""}" type="button"
      role="tab" aria-selected="${station.name === usageStation}" data-usage-station="${escapeHtml(station.name)}"
      aria-controls="usageChartPanel" tabindex="${station.name === usageStation ? "0" : "-1"}"
      title="${escapeHtml(station.name)} · ${formatCount(station.tokens)} Token">
      <span class="usage-tab-name">${escapeHtml(station.name)}</span>
      <span class="usage-tab-value">${formatCompactCount(station.tokens)}</span>
    </button>`).join("")
    || '<span class="muted">今日暂无站点</span>';
  const selectedIndex = stations.findIndex((station) => station.name === usageStation);
  if (selectedIndex >= 0) {
    $("usageChartPanel").setAttribute("aria-labelledby", `usage-tab-${selectedIndex}`);
  } else {
    $("usageChartPanel").removeAttribute("aria-labelledby");
  }

  let modelOffset = 0;
  const modelSegments = [];
  if (selectedStation) {
    const station = selectedStation;
    const classifiedTokens = station.models.reduce((sum, model) => sum + model.tokens, 0);
    const scale = classifiedTokens > station.tokens && classifiedTokens > 0
      ? station.tokens / classifiedTokens
      : 1;
    station.models.forEach((model) => {
      const drawnTokens = model.tokens * scale;
      const fraction = station.tokens > 0 ? drawnTokens / station.tokens : 0;
      const stationShare = station.tokens > 0 ? model.tokens / station.tokens : 0;
      modelSegments.push(ringSegment({
        kind: "model",
        station: station.name,
        model: model.name,
        radius: 62,
        fraction,
        offset: modelOffset,
        tone: model.tone,
        label: `${station.name} / ${model.name}: ${formatCount(model.tokens)} Token, 站内 ${formatShare(stationShare)}`,
      }));
      modelOffset += fraction;
    });
    const unclassified = Math.max(0, station.tokens - classifiedTokens);
    if (unclassified > 0) {
      const fraction = station.tokens > 0 ? unclassified / station.tokens : 0;
      modelSegments.push(ringSegment({
        kind: "model",
        station: station.name,
        model: "__unclassified__",
        radius: 62,
        fraction,
        offset: modelOffset,
        tone: station.tone,
        label: `${station.name} / 未归类模型: ${formatCount(unclassified)} Token`,
      }));
      modelOffset += fraction;
    }
  }

  $("donut").innerHTML = `<title>${escapeHtml(selectedStation?.name || "站点")} 模型 Token 用量</title>
    <desc>单层饼图展示所选站点内各模型已返回的 Token。</desc>
    <circle class="donut-track" cx="90" cy="90" r="62"></circle>
    ${modelSegments.join("")}`;
  $("stationUsageList").innerHTML = stations.map((station) => {
    const share = totalTokens > 0 ? station.tokens / totalTokens : 0;
    return `<div class="station-usage-row">
      <span class="station-usage-name" title="${escapeHtml(station.name)}">${escapeHtml(station.name)}</span>
      <span class="station-usage-track" aria-hidden="true"><span class="station-usage-bar" data-width="${clamp(share * 100, 0, 100)}"></span></span>
      <span class="station-usage-value">${formatCompactCount(station.tokens)} · ${formatShare(share)}${station.cacheKnown ? ` · 命中 ${formatHitRate(station.cacheHitRate)}` : ""}</span>
    </div>`;
  }).join("") || '<span class="chart-empty">今日暂无站点用量</span>';
  applyDataWidths($("stationUsageList"));
  renderModelBreakdown();
  updateChartInteraction();
}

function selectUsageStation(stationName, shouldFocus = false) {
  if (!chartStations().some((station) => station.name === stationName)) return;
  usageStation = stationName;
  chartSelection = { station: usageStation, model: "" };
  chartPreview = null;
  renderTokenChart();
  if (!shouldFocus) return;
  const selectedTab = [...$("usageTabs").querySelectorAll("button[data-usage-station]")]
    .find((button) => button.dataset.usageStation === usageStation);
  selectedTab?.focus();
  selectedTab?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
}

function selectCacheWindow(windowId) {
  if (!CACHE_WINDOWS.some((item) => item.id === windowId)) return;
  cacheWindow = windowId;
  localStorage.setItem("zg_cache_window", windowId);
  renderCacheCompare();
}

function renderCacheCompare() {
  const compare = statsData?.cache_compare || {};
  const windows = compare.windows || {};
  const rows = Array.isArray(windows[cacheWindow]) ? windows[cacheWindow] : [];
  const notes = {
    hour: `本小时 ${String(finiteNumber(compare.current_hour)).padStart(2, "0")}:00 起`,
    best_hour: "近 24 小时里各站命中率最高的一小时",
    today: "今日累计",
    d7: "近 7 天累计",
    d31: "近 31 天累计",
  };
  $("cacheWindowNote").textContent = notes[cacheWindow] || "按命中率排序";
  $("cacheWindowTabs").innerHTML = CACHE_WINDOWS.map((item) => {
    const best = compare.best?.[item.id];
    return `<button class="cache-window-tab ${item.id === cacheWindow ? "is-active" : ""}" type="button"
        role="tab" aria-selected="${item.id === cacheWindow}" data-cache-window="${item.id}"
        tabindex="${item.id === cacheWindow ? "0" : "-1"}">
      <span class="usage-tab-name">${item.label}</span>
      ${best ? `<span class="usage-tab-value">${escapeHtml(best)}</span>` : ""}
    </button>`;
  }).join("");

  $("cacheRankList").innerHTML = rows.map((station) => {
    const known = station.cache_known === true;
    const rate = known ? clamp(station.cache_hit_rate, 0, 1) : 0;
    const label = known ? formatHitRate(station.cache_hit_rate) : "未回传";
    const volume = known
      ? `${formatCompactCount(station.cache_read)} / ${formatCompactCount(station.cache_input)}`
      : "暂无缓存字段";
    const when = station.window_label ? ` · ${station.window_label}` : "";
    const bestMark = station.is_best ? " · 最高" : "";
    return `<div class="cache-rank-row ${station.is_best ? "is-best" : ""} ${known ? "" : "is-empty"}">
      <span class="cache-rank-index">#${formatCount(station.rank || 0)}</span>
      <span class="cache-rank-name" title="${escapeHtml(station.name)}">${escapeHtml(station.name)}</span>
      <span class="cache-rank-track" aria-hidden="true"><span class="cache-rank-bar" data-width="${clamp(rate * 100, 0, 100)}"></span></span>
      <span class="cache-rank-value">${label}${bestMark} · ${volume}${when}</span>
    </div>`;
  }).join("") || '<span class="chart-empty">还没有可对比的缓存回传。新请求开始统计后，这里会按站点排出命中率。</span>';
  applyDataWidths($("cacheRankList"));
}

function renderStats() {
  if (!statsData) return;
  const data = statsData;
  const totalCalls = finiteNumber(data.total_calls);
  const failures = finiteNumber(data.total_failures);
  const successes = Math.max(0, totalCalls - failures);
  const successRate = totalCalls > 0 ? successes / totalCalls * 100 : null;
  const coverage = data.token_coverage_known === false
    ? "更新后统计"
    : `${formatCount(data.usage_reports)}/${formatCount(totalCalls)}`;

  $("stats-summary").innerHTML = `
    <div class="daily-metric"><span class="daily-label">总调用</span><strong class="daily-value">${formatCount(totalCalls)}</strong></div>
    <div class="daily-metric"><span class="daily-label">成功</span><strong class="daily-value text-ok">${formatCount(successes)}</strong></div>
    <div class="daily-metric"><span class="daily-label">失败</span><strong class="daily-value ${failures ? "text-danger" : ""}">${formatCount(failures)}</strong></div>
    <div class="daily-metric"><span class="daily-label">成功率</span><strong class="daily-value">${successRate == null ? "-" : `${percentFormat.format(successRate)}%`}</strong></div>
    <div class="daily-metric"><span class="daily-label">已统计 Token</span><strong class="daily-value">${formatCount(data.total_tokens)}</strong></div>
    <div class="daily-metric"><span class="daily-label">Token 回传调用</span><strong class="daily-value">${coverage}</strong></div>
    <div class="daily-metric"><span class="daily-label">缓存命中率</span><strong class="daily-value">${formatHitRate(data.cache_hit_rate, data.cache_known)}</strong></div>
    <div class="daily-metric"><span class="daily-label">缓存 Token</span><strong class="daily-value">${data.cache_known ? formatCount(data.cache_read) : "未回传"}</strong></div>`;

  renderTokenChart();
  renderCacheCompare();

  const names = Object.keys(data.timeline || {});
  $("gantt").innerHTML = names.map((name) => {
    const cells = (data.timeline[name] || []).slice(0, 24).map((count, hour) => {
      const value = finiteNumber(count);
      const level = value === 0 ? "" : (value < 5 ? "level-1" : (value < 20 ? "level-2" : "level-3"));
      return `<span class="gantt-cell ${level}" title="${hour}:00 - ${hour + 1}:00 · ${formatCount(value)} 次调用"></span>`;
    }).join("");
    return `<div class="gantt-row"><span class="gantt-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span><div class="gantt-track">${cells}</div></div>`;
  }).join("") || '<div class="empty-cell">今日暂无调用</div>';
}

async function refreshStats() {
  if (statsRunning) return;
  statsRunning = true;
  try {
    statsData = await api("/admin/stats/daily");
    statsUpdatedAt = Date.now();
    renderStats();
  } catch (error) {
    showToast(`统计读取失败: ${error.message}`, true);
  } finally {
    statsRunning = false;
  }
}

async function refreshEvents(page = eventPage) {
  if (eventsRunning) return;
  eventsRunning = true;
  $("eventPrev").disabled = true;
  $("eventNext").disabled = true;
  try {
    eventsData = await api(`/admin/logs?page=${Math.max(1, finiteNumber(page, 1))}&page_size=${EVENT_PAGE_SIZE}`);
    eventsUpdatedAt = Date.now();
    renderEvents(eventsData);
  } catch (error) {
    showToast(`事件读取失败: ${error.message}`, true);
    if (!eventsData) {
      renderEvents({ events: [], total: 0, page: 1, pages: 1, max_events: 500 });
    }
  } finally {
    eventsRunning = false;
    if (eventsData) renderEvents(eventsData);
  }
}

const RETRY_SETTING_KEYS = [
  "max_attempts",
  "retry_backoff",
  "max_rounds",
  "round_backoff",
  "max_round_backoff",
  "max_retry_seconds",
];

const RETRY_SETTING_RULES = {
  max_attempts: { minimum: 1, maximum: 100, integer: true },
  retry_backoff: { minimum: 0, maximum: 60 },
  max_rounds: { minimum: 0, maximum: 10000, integer: true },
  round_backoff: { minimum: 0.1, maximum: 300 },
  max_round_backoff: { minimum: 0.1, maximum: 600 },
  max_retry_seconds: { minimum: 0, maximum: 86400 },
};

function setRetrySettingsStatus(message = "", state = "") {
  const status = $("retrySettingsStatus");
  status.textContent = message;
  status.classList.toggle("is-error", state === "error");
  status.classList.toggle("is-success", state === "success");
}

function retrySettingsSource(payload) {
  const candidates = [payload?.settings, payload?.retry, payload];
  return candidates.find((candidate) => candidate && typeof candidate === "object"
    && RETRY_SETTING_KEYS.every((key) => Object.prototype.hasOwnProperty.call(candidate, key)));
}

function normalizeRetrySettings(payload, fallback = null) {
  const source = retrySettingsSource(payload) || retrySettingsSource(fallback);
  if (!source) throw new Error("接口未返回完整的重试设置");
  const normalized = {};
  RETRY_SETTING_KEYS.forEach((key) => {
    const rule = RETRY_SETTING_RULES[key];
    const value = Number(source[key]);
    if (!Number.isFinite(value)
        || value < rule.minimum
        || value > rule.maximum
        || (rule.integer && !Number.isInteger(value))) {
      throw new Error(`接口返回了无效的 ${key}`);
    }
    normalized[key] = value;
  });
  return normalized;
}

function setPositiveRetryValue(input, value, fallback) {
  if (value > 0) {
    input.value = String(value);
    input.dataset.lastValue = String(value);
    return;
  }
  const previous = Number(input.dataset.lastValue);
  input.value = Number.isFinite(previous) && previous > 0 ? String(previous) : String(fallback);
}

function syncRetryLimitControls() {
  const busy = retrySettingsLoading || retrySettingsSaving;
  const infiniteRounds = $("retry-infinite-rounds").checked;
  const unlimitedDuration = $("retry-unlimited-duration").checked;
  $("retry-infinite-rounds").disabled = busy || unlimitedDuration;
  $("retry-unlimited-duration").disabled = busy || infiniteRounds;
  $("retry-max-rounds").disabled = busy || infiniteRounds;
  $("retry-max-seconds").disabled = busy || unlimitedDuration;
  $("retry-max-rounds-field").classList.toggle("is-disabled", infiniteRounds);
  $("retry-max-seconds-field").classList.toggle("is-disabled", unlimitedDuration);
}

function setRetrySettingsBusy() {
  const busy = retrySettingsLoading || retrySettingsSaving;
  $("retry-form").setAttribute("aria-busy", String(busy));
  $("retry-form").querySelectorAll("input").forEach((input) => {
    input.disabled = busy;
  });
  $("retry-save").disabled = busy;
  $("retry-reload").disabled = busy;
  $("retry-save").textContent = retrySettingsSaving ? "保存中..." : "保存设置";
  $("retry-reload").textContent = retrySettingsLoading ? "加载中..." : "重新加载";
  syncRetryLimitControls();
}

function applyRetrySettings(payload, fallback = null) {
  const settings = normalizeRetrySettings(payload, fallback);
  retrySettingsData = settings;
  $("retry-max-attempts").value = String(settings.max_attempts);
  $("retry-backoff").value = String(settings.retry_backoff);
  $("retry-infinite-rounds").checked = settings.max_rounds === 0;
  setPositiveRetryValue($("retry-max-rounds"), settings.max_rounds, 3);
  $("retry-round-backoff").value = String(settings.round_backoff);
  $("retry-max-round-backoff").value = String(settings.max_round_backoff);
  $("retry-unlimited-duration").checked = settings.max_retry_seconds === 0;
  setPositiveRetryValue($("retry-max-seconds"), settings.max_retry_seconds, 60);
  syncRetryLimitControls();
}

function readRetryNumber(id, label, minimum, maximum = null, integer = false) {
  const input = $(id);
  const value = Number(input.value);
  if (!Number.isFinite(value) || value < minimum
      || (maximum != null && value > maximum)
      || (integer && !Number.isInteger(value))) {
    input.focus({ preventScroll: true });
    const bounds = maximum == null
      ? `不能小于 ${minimum}`
      : `必须在 ${minimum} 到 ${maximum} 之间`;
    throw new Error(`${label}${bounds}${integer ? "，且必须为整数" : ""}`);
  }
  return value;
}

function readRetrySettingsForm() {
  const settings = {
    max_attempts: readRetryNumber("retry-max-attempts", "单次最多尝试站点", 1, 100, true),
    retry_backoff: readRetryNumber("retry-backoff", "站点切换等待", 0, 60),
    max_rounds: $("retry-infinite-rounds").checked
      ? 0
      : readRetryNumber("retry-max-rounds", "最大重试轮次", 1, 10000, true),
    round_backoff: readRetryNumber("retry-round-backoff", "轮间基础等待", 0.1, 300),
    max_round_backoff: readRetryNumber("retry-max-round-backoff", "轮间最长等待", 0.1, 600),
    max_retry_seconds: $("retry-unlimited-duration").checked
      ? 0
      : readRetryNumber("retry-max-seconds", "总重试时长上限", 1, 86400),
  };
  if (settings.max_rounds === 0 && settings.max_retry_seconds === 0) {
    $("retry-unlimited-duration").focus({ preventScroll: true });
    throw new Error("重试轮次和总时长不能同时不限");
  }
  return settings;
}

async function refreshRetrySettings({ notify = false } = {}) {
  if (retrySettingsLoading || retrySettingsSaving) return;
  retrySettingsLoading = true;
  setRetrySettingsStatus("正在读取设置...");
  setRetrySettingsBusy();
  try {
    const payload = await api("/admin/settings/retry");
    applyRetrySettings(payload);
    retrySettingsUpdatedAt = Date.now();
    setRetrySettingsStatus(notify ? "已加载最新设置" : "", notify ? "success" : "");
  } catch (error) {
    const message = `读取失败: ${error.message}`;
    setRetrySettingsStatus(message, "error");
    if (notify) showToast(message, true);
  } finally {
    retrySettingsLoading = false;
    setRetrySettingsBusy();
  }
}

async function saveRetrySettings() {
  if (retrySettingsLoading || retrySettingsSaving) return;
  let body;
  try {
    body = readRetrySettingsForm();
  } catch (error) {
    setRetrySettingsStatus(error.message, "error");
    return;
  }
  retrySettingsSaving = true;
  setRetrySettingsStatus("正在保存设置...");
  setRetrySettingsBusy();
  try {
    const result = await api("/admin/settings/retry", "PUT", true, body);
    applyRetrySettings(result, body);
    retrySettingsUpdatedAt = Date.now();
    setRetrySettingsStatus("重试设置已保存", "success");
    showToast("重试设置已保存");
  } catch (error) {
    const message = `保存失败: ${error.message}`;
    setRetrySettingsStatus(message, "error");
    showToast(message, true);
  } finally {
    retrySettingsSaving = false;
    setRetrySettingsBusy();
  }
}

function scheduleRefresh(delay = 3000) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, delay);
}

async function refresh(force = false) {
  if (refreshRunning) {
    refreshRequested = true;
    return;
  }
  refreshRunning = true;
  if (!statusData) setSyncState("loading", "正在同步");
  try {
    statusData = mergeStatusActivity(await api("/admin/status"));
    render(force);
    ensureActivityStream();
    const now = new Date();
    setSyncState("online", `已更新 ${now.toLocaleTimeString("zh-CN", { hour12: false })}`);
    if (currentTab === "stats" && Date.now() - statsUpdatedAt > 30000) refreshStats();
    if (currentTab === "events" && eventPage === 1
        && Date.now() - eventsUpdatedAt > 5000) refreshEvents(1);
  } catch (error) {
    setSyncState("error", "连接异常");
    $("sub").textContent = `无法连接网关: ${error.message}`;
  } finally {
    refreshRunning = false;
    if (refreshRequested) {
      refreshRequested = false;
      queueMicrotask(() => refresh(true));
    } else {
      const routeActive = Boolean(statusData?.route_activity?.active
        || (statusData?.active_routes || []).length);
      scheduleRefresh(routeActive ? 750 : 1500);
    }
  }
}

async function runStationAction(action, stationName) {
  const key = `${action}:${stationName}`;
  if (pendingActions.has(key)) return;
  pendingActions.add(key);
  render(true);
  const labels = { toggle: "站点状态已更新", probe: "探测已完成", reset: "额度探测已完成" };
  try {
    const encodedName = encodeURIComponent(stationName);
    const result = await api(`/admin/station/${encodedName}/${action}`, "POST");
    let message = labels[action] || "操作已完成";
    if (action === "reset") {
      message = result.recovered || !result.quota_exhausted
        ? `${stationName} 额度已恢复`
        : `${stationName} 仍无额度，将继续自动探测`;
    }
    showToast(message, action === "reset" && result.quota_exhausted);
    await refresh(true);
  } catch (error) {
    showToast(`操作失败: ${error.message}`, true);
  } finally {
    pendingActions.delete(key);
    render(true);
  }
}

function chartControlFromTarget(target) {
  if (!(target instanceof Element)) return null;
  const control = target.closest("[data-chart-kind][data-station]");
  if (!control || (!$("donut").contains(control)
      && !$("modelBreakdown").contains(control))) return null;
  return control;
}

function itemFromChartControl(control) {
  if (!control) return null;
  return chartItem(control.dataset.chartKind, control.dataset.station, control.dataset.model || "");
}

function lockChartItem(item) {
  if (!item) {
    chartSelection = { station: usageStation, model: "" };
  } else {
    const model = item.kind === "model"
      ? (item.model.unclassified ? "__unclassified__" : item.model.name)
      : "";
    const isSame = chartSelection.station === item.station.name && chartSelection.model === model;
    chartSelection = isSame
      ? { station: usageStation, model: "" }
      : { station: item.station.name, model };
  }
  chartPreview = null;
  renderModelBreakdown();
  updateChartInteraction();
}

function setChartPreviewFromEvent(event) {
  const control = chartControlFromTarget(event.target);
  if (!control) return;
  chartPreview = itemFromChartControl(control);
  updateChartInteraction();
}

function clearChartPreviewFromEvent(event) {
  const control = chartControlFromTarget(event.target);
  if (!control) return;
  const relatedControl = chartControlFromTarget(event.relatedTarget);
  if (relatedControl === control) return;
  chartPreview = null;
  updateChartInteraction();
}

[$("donut"), $("modelBreakdown")].forEach((container) => {
  container.addEventListener("pointerover", setChartPreviewFromEvent);
  container.addEventListener("pointerout", clearChartPreviewFromEvent);
  container.addEventListener("focusin", setChartPreviewFromEvent);
  container.addEventListener("focusout", clearChartPreviewFromEvent);
  container.addEventListener("click", (event) => {
    const control = chartControlFromTarget(event.target);
    if (control) {
      lockChartItem(itemFromChartControl(control));
    } else if (container === $("donut")) {
      lockChartItem(null);
    }
  });
});

$("donut").addEventListener("keydown", (event) => {
  const control = chartControlFromTarget(event.target);
  if (!control || !["Enter", " "].includes(event.key)) return;
  event.preventDefault();
  lockChartItem(itemFromChartControl(control));
});

document.addEventListener("click", (event) => {
  const tabButton = event.target.closest("button[data-tab]");
  if (tabButton) {
    setTab(tabButton.dataset.tab);
    return;
  }
  const usageTab = event.target.closest("button[data-usage-station]");
  if (usageTab) {
    selectUsageStation(usageTab.dataset.usageStation);
    return;
  }
  const cacheTab = event.target.closest("button[data-cache-window]");
  if (cacheTab) {
    selectCacheWindow(cacheTab.dataset.cacheWindow);
    return;
  }
  const viewButton = event.target.closest("button[data-view]");
  if (viewButton) {
    setView(viewButton.dataset.view);
    return;
  }
  const actionButtonElement = event.target.closest("button[data-action][data-station]");
  if (actionButtonElement && !actionButtonElement.disabled) {
    runStationAction(actionButtonElement.dataset.action, actionButtonElement.dataset.station);
  }
});

$("eventPrev").addEventListener("click", () => {
  if (!eventsRunning && eventPage > 1) refreshEvents(eventPage - 1);
});

$("eventNext").addEventListener("click", () => {
  const pages = Math.max(1, finiteNumber(eventsData?.pages, 1));
  if (!eventsRunning && eventPage < pages) refreshEvents(eventPage + 1);
});

$("retry-infinite-rounds").addEventListener("change", syncRetryLimitControls);
$("retry-unlimited-duration").addEventListener("change", syncRetryLimitControls);

$("retry-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveRetrySettings();
});

$("retry-reload").addEventListener("click", () => {
  refreshRetrySettings({ notify: true });
});

$("usageTabs").addEventListener("keydown", (event) => {
  const current = event.target.closest("button[data-usage-station]");
  if (!current || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...$("usageTabs").querySelectorAll("button[data-usage-station]")];
  if (!tabs.length) return;
  event.preventDefault();
  let index = tabs.indexOf(current);
  if (event.key === "Home") index = 0;
  else if (event.key === "End") index = tabs.length - 1;
  else index = (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  selectUsageStation(tabs[index].dataset.usageStation, true);
});

$("authForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = $("adminKeyInput").value.trim();
  if (!value) return;
  localStorage.setItem("zg_admin_key", value);
  settleAuth(true);
});

$("authCancel").addEventListener("click", () => settleAuth(false));
$("authDialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  settleAuth(false);
});

$("tab-monitor").parentElement.addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  const tabs = ["monitor", "stats", "stations", "retry", "events"];
  const direction = event.key === "ArrowRight" ? 1 : -1;
  const nextTab = tabs[(tabs.indexOf(currentTab) + direction + tabs.length) % tabs.length];
  setTab(nextTab);
  $(`tab-${nextTab}`).focus();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    clearTimeout(activityStreamRetryTimer);
    activityStreamRetryTimer = null;
    ensureActivityStream();
    refresh(true);
  }
});

window.addEventListener("beforeunload", () => {
  clearTimeout(activityStreamRetryTimer);
  activityStreamController?.abort();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && chartSelection.model && !$("authDialog").open) {
    lockChartItem(null);
  }
});


/* ---------------- 站点管理（动态增删改，无需重启） ---------------- */
let STATIONS = [];
let editingStation = null;
let stationsLoading = false;
let stationsError = "";
let stationSubmitting = false;
const stationPendingActions = new Set();
let stationsRequest = null;
let stationsRefreshAgain = false;
let stationsRefreshNotify = false;

function setStationFormStatus(message = "", isError = false) {
  const status = $("stationFormStatus");
  status.textContent = message;
  status.classList.toggle("is-error", isError);
  status.classList.toggle(
    "is-success",
    !isError && /已(?:添加|保存)/.test(message),
  );
}

function setStationListStatus(message = "", isError = false) {
  const status = $("stationListStatus");
  status.textContent = message;
  status.classList.toggle("is-error", isError);
}

async function refreshStationsOnce(notifyError) {
  stationsLoading = true;
  stationsError = "";
  $("stationsTableShell").setAttribute("aria-busy", "true");
  setStationListStatus("正在读取站点...");
  renderStations();
  try {
    const stations = await api("/admin/stations");
    if (!Array.isArray(stations)) throw new Error("站点列表响应格式错误");
    STATIONS = stations;
    stationsUpdatedAt = Date.now();
    setStationListStatus(`已同步 ${STATIONS.length} 个站点`);
    return true;
  } catch (error) {
    stationsError = error.message || "读取失败";
    setStationListStatus(`站点列表读取失败: ${stationsError}`, true);
    if (notifyError) showToast(`站点列表读取失败: ${stationsError}`, true);
    return false;
  } finally {
    stationsLoading = false;
    $("stationsTableShell").removeAttribute("aria-busy");
    renderStations();
  }
}

function refreshStations({ notifyError = false, force = false } = {}) {
  if (stationsRequest) {
    if (force) {
      stationsRefreshAgain = true;
      stationsRefreshNotify = stationsRefreshNotify || notifyError;
    }
    return stationsRequest;
  }
  stationsRequest = (async () => {
    let shouldNotify = notifyError;
    let result = false;
    do {
      stationsRefreshAgain = false;
      result = await refreshStationsOnce(shouldNotify);
      shouldNotify = shouldNotify || stationsRefreshNotify;
      stationsRefreshNotify = false;
    } while (stationsRefreshAgain);
    return result;
  })().finally(() => {
    stationsRequest = null;
  });
  return stationsRequest;
}

function renderStations() {
  $("stationsCount").textContent = `(${STATIONS.length})`;
  if (stationsLoading && !STATIONS.length) {
    $("stations-body").innerHTML = '<tr><td colspan="7" class="empty-cell">正在读取站点...</td></tr>';
    return;
  }
  if (stationsError && !STATIONS.length) {
    $("stations-body").innerHTML = '<tr><td colspan="7" class="empty-cell">站点列表暂不可用，请点击刷新重试</td></tr>';
    return;
  }

  $("stations-body").innerHTML = STATIONS.map((station) => {
    const name = String(station.name || "");
    const state = statusInfo(station);
    const models = Array.isArray(station.models) ? station.models : [];
    const modelsLabel = models.length ? models.join(", ") : "任意模型";
    const actions = ["toggle", "probe", "reset", "delete"];
    const rowBusy = actions.some((action) => stationPendingActions.has(`${action}:${name}`));
    const toggleBusy = stationPendingActions.has(`toggle:${name}`);
    const probeBusy = stationPendingActions.has(`probe:${name}`);
    const resetBusy = stationPendingActions.has(`reset:${name}`);
    const deleteBusy = stationPendingActions.has(`delete:${name}`);
    const disabled = rowBusy ? " disabled" : "";
    const busy = (value) => value ? ' aria-busy="true"' : "";
    return `
      <tr class="${station.enabled ? "" : "is-disabled"}" data-station-row="${escapeHtml(name)}">
        <td><span class="managed-station-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
          ${station.quota_exhausted ? '<span class="station-warning">配额耗尽 · 自动探测中</span>' : ""}</td>
        <td class="station-url-cell" title="${escapeHtml(station.base_url || "")}">${escapeHtml(station.base_url || "-")}</td>
        <td class="station-models-cell" title="${escapeHtml(modelsLabel)}">${escapeHtml(modelsLabel)}</td>
        <td>${formatCount(station.priority)}</td>
        <td class="muted">${station.billing === "subscription" ? "订阅" : "余额"}</td>
        <td><span class="managed-status"><span class="status-dot ${state.className}" aria-hidden="true"></span>${escapeHtml(state.label)}</span></td>
        <td><div class="station-manage-actions">
          <button class="button station-action" type="button" data-manage-action="edit" data-station="${escapeHtml(name)}"${disabled}>编辑</button>
          <button class="button station-action" type="button" data-manage-action="toggle" data-station="${escapeHtml(name)}"${disabled}${busy(toggleBusy)}>${toggleBusy ? "处理中..." : (station.enabled ? "停用" : "启用")}</button>
          <button class="button station-action" type="button" data-manage-action="probe" data-station="${escapeHtml(name)}"${disabled}${busy(probeBusy)}>${probeBusy ? "探测中..." : "探测"}</button>
          ${station.quota_exhausted ? `<button class="button station-action" type="button" data-manage-action="reset" data-station="${escapeHtml(name)}"${disabled}${busy(resetBusy)}>${resetBusy ? "探测中..." : "立即探测"}</button>` : ""}
          <button class="button station-action is-destructive" type="button" data-manage-action="delete" data-station="${escapeHtml(name)}"${disabled}${busy(deleteBusy)}>${deleteBusy ? "删除中..." : "删除"}</button>
        </div></td>
      </tr>`;
  }).join("") || '<tr><td colspan="7" class="empty-cell">暂无站点，点击“添加站点”开始配置</td></tr>';
}

function fillStationForm(station) {
  if (stationSubmitting) return;
  editingStation = station.name;
  $("sf-name").value = station.name;
  $("sf-name").disabled = true;
  $("sf-base-url").value = station.base_url;
  $("sf-api-key").value = "";
  $("sf-api-key").required = false;
  $("sf-api-key").placeholder = "已配置，留空保持不变";
  $("sf-models").value = (station.models || []).join(", ");
  $("sf-priority").value = station.priority;
  $("sf-billing").value = station.billing;
  $("sf-probe-chat").checked = Boolean(station.probe_chat);
  $("sf-probe-models").checked = Boolean(station.probe_models);
  $("sf-enabled").checked = station.enabled;
  $("sf-heading").textContent = "编辑站点: " + station.name;
  $("sf-submit").textContent = "保存修改";
  $("sf-cancel").hidden = false;
  setStationFormStatus(`正在编辑 ${station.name}，API Key 留空将保持不变`);
  $("stations-wrap").scrollIntoView({ behavior: "smooth" });
  $("sf-base-url").focus({ preventScroll: true });
}

function resetStationForm({ keepStatus = false } = {}) {
  editingStation = null;
  $("sf-name").value = "";
  $("sf-name").disabled = false;
  $("sf-base-url").value = "";
  $("sf-api-key").value = "";
  $("sf-api-key").required = true;
  $("sf-api-key").placeholder = "your-upstream-key";
  $("sf-models").value = "";
  $("sf-priority").value = "5";
  $("sf-billing").value = "balance";
  $("sf-probe-chat").checked = false;
  $("sf-probe-models").checked = false;
  $("sf-enabled").checked = true;
  $("sf-heading").textContent = "添加 / 编辑站点";
  $("sf-submit").textContent = "添加站点";
  $("sf-cancel").hidden = true;
  if (!keepStatus) setStationFormStatus();
}

function setStationSubmitting(busy) {
  stationSubmitting = busy;
  $("station-form").setAttribute("aria-busy", String(busy));
  $("sf-submit").disabled = busy;
  $("sf-cancel").disabled = busy;
  if (busy) {
    $("sf-submit").textContent = editingStation ? "正在保存..." : "正在添加...";
  } else {
    $("sf-submit").textContent = editingStation ? "保存修改" : "添加站点";
  }
}

$("station-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (stationSubmitting) return;
  if (!event.currentTarget.checkValidity()) {
    event.currentTarget.reportValidity();
    return;
  }
  const body = {
    name: $("sf-name").value.trim(),
    base_url: $("sf-base-url").value.trim(),
    api_key: $("sf-api-key").value.trim(),
    models: $("sf-models").value.split(/[,，]/).map(x => x.trim()).filter(Boolean),
    priority: parseInt($("sf-priority").value) || 5,
    billing: $("sf-billing").value,
    probe_chat: $("sf-probe-chat").checked,
    probe_models: $("sf-probe-models").checked,
    enabled: $("sf-enabled").checked,
  };
  if (!body.name || !body.base_url || (!body.api_key && !editingStation)) {
    setStationFormStatus("名称、Base URL 和 API Key 必填", true);
    return;
  }
  const wasEditing = Boolean(editingStation);
  const stationName = editingStation || body.name;
  setStationSubmitting(true);
  setStationFormStatus(wasEditing ? "正在保存站点..." : "正在添加站点...");
  try {
    let result;
    if (wasEditing) {
      delete body.name;
      if (!body.api_key) delete body.api_key;
      result = await api(
        "/admin/stations/" + encodeURIComponent(stationName),
        "PUT", true, body,
      );
    } else {
      result = await api("/admin/stations", "POST", true, body);
    }
    const probeNote = result?.probe_scheduled ? "，连接检查已在后台排队" : "";
    const message = wasEditing
      ? `${stationName} 已保存${probeNote}`
      : `${stationName} 已添加并保存${probeNote}`;
    const listRefreshed = await refreshStations({ force: true });
    const finalMessage = listRefreshed
      ? message
      : `${message}，但列表刷新失败，请点击“刷新”重试`;
    resetStationForm({ keepStatus: true });
    setStationFormStatus(finalMessage, !listRefreshed);
    showToast(finalMessage, !listRefreshed);
    await refresh(true);
  } catch (error) {
    const prefix = wasEditing ? "更新失败" : "添加失败";
    setStationFormStatus(`${prefix}: ${error.message}`, true);
    showToast(`${prefix}: ${error.message}`, true);
  } finally {
    setStationSubmitting(false);
  }
});

async function runManagedStationAction(action, stationName) {
  const key = `${action}:${stationName}`;
  if (stationPendingActions.has(key)) return;
  stationPendingActions.add(key);
  renderStations();
  const labels = {
    toggle: "正在更新启用状态...",
    probe: "正在探测连接...",
    reset: "正在探测额度是否恢复...",
    delete: "正在删除站点...",
  };
  setStationListStatus(`${stationName}: ${labels[action] || "正在处理..."}`);
  try {
    const encodedName = encodeURIComponent(stationName);
    const result = action === "delete"
      ? await api(`/admin/stations/${encodedName}`, "DELETE")
      : await api(`/admin/station/${encodedName}/${action}`, "POST");
    const listRefreshed = await refreshStations({ force: true });
    await refresh(true);

    let message = `${stationName} 操作已完成`;
    let isError = false;
    if (action === "toggle") message = `${stationName} 已${result.enabled ? "启用" : "停用"}并保存`;
    if (action === "delete") message = `${stationName} 已删除并保存`;
    if (action === "reset") {
      message = result.recovered || !result.quota_exhausted
        ? `${stationName} 额度已恢复`
        : `${stationName} 仍无额度，将继续自动探测`;
      isError = Boolean(result.quota_exhausted);
    }
    if (action === "probe") {
      isError = result.ok === false;
      message = result.ok
        ? `${stationName} 探测成功${result.latency_ms == null ? "" : `，${result.latency_ms} ms`}`
        : `${stationName} 探测未通过${result.error ? `: ${result.error}` : ""}`;
    }
    if (!listRefreshed) {
      message += "，但列表刷新失败，请点击“刷新”重试";
      isError = true;
    }
    if (action === "delete" && editingStation === stationName) resetStationForm();
    setStationListStatus(message, isError);
    showToast(message, isError);
  } catch (error) {
    const message = `${stationName} 操作失败: ${error.message}`;
    setStationListStatus(message, true);
    showToast(message, true);
  } finally {
    stationPendingActions.delete(key);
    renderStations();
  }
}

$("stations-body").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-manage-action][data-station]");
  if (!button || button.disabled) return;
  const action = button.dataset.manageAction;
  const stationName = button.dataset.station;
  if (action === "edit") {
    const station = STATIONS.find((item) => item.name === stationName);
    if (station) fillStationForm(station);
    return;
  }
  if (action === "delete"
      && !window.confirm(`确定删除站点 ${stationName}？该操作会立即停止后续路由。`)) return;
  runManagedStationAction(action, stationName);
});

$("stations-add").addEventListener("click", () => {
  if (stationSubmitting) return;
  resetStationForm();
  $("stations-wrap").scrollIntoView({ behavior: "smooth" });
  $("sf-name").focus({ preventScroll: true });
});

$("stations-refresh").addEventListener("click", () => {
  refreshStations({ notifyError: true });
});

$("sf-cancel").addEventListener("click", () => resetStationForm());

applyTab();
applyView();
if (currentTab === "stations") refreshStations({ notifyError: true });
if (currentTab === "retry") refreshRetrySettings({ notify: true });
refresh();
