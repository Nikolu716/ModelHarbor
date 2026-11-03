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
const legacyStationSection = ["retry", "probe"].includes(localStorage.getItem("zg_tab")) ? localStorage.getItem("zg_tab") : "";
let currentTab = legacyStationSection ? "stations" : ["monitor", "stats", "events", "stations", "access"].includes(localStorage.getItem("zg_tab"))
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
let probeSettingsUpdatedAt = 0;
let probeSettingsLoading = false;
let probeSettingsSaving = false;
let probeSettingsData = null;
let endpointData = null;
let endpointSettingsBusy = false;
let accessSettingsBusy = false;
let accessSettingsLoaded = false;
let serviceSettingsBusy = false;
let canManageService = false;
let serviceSettingsLoaded = false;
let serviceSettingsUpdatedAt = 0;
let serviceSettingsData = null;
let eventPage = 1;
let EVENT_PAGE_SIZE = 20;
let renderFingerprint = "";
let stationScope = "enabled";
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
  if (value == null || typeof value === "string" && value.trim() === "") return fallback;
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

function scoreBreakdownTitle(breakdown = {}) {
  const labels = [
    ["latency", "延迟"],
    ["business_reliability", "业务可靠性"],
    ["priority_bias", "手动优先级"],
    ["failure_streak_penalty", "连续失败扣分"],
    ["breaker_penalty", "熔断扣分"],
    ["quota_penalty", "配额扣分"],
    ["rate_limit_penalty", "限流扣分"],
  ];
  const parts = labels.map(([key, label]) =>
    `${label} ${finiteNumber(breakdown[key]).toFixed(1)}`);
  if (breakdown.business_samples != null) {
    parts.push(`业务样本 ${formatCount(breakdown.business_samples)}`);
  }
  return `健康评分拆解：${parts.join(" · ")}`;
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
  if (!auto || (!gateway.generation_probe_enabled || !station.probe_chat)
      && station.quota_source !== "models") {
    return `配额保护中，后台生成恢复检查已关闭；恢复额度后可手动生成测试。${error}`;
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

function formatBalance(balance, threshold, station = {}) {
  if (station.provider === "opencode" && station.opencode_plan === "go") {
    return window.ModelHarborGoUsage?.markup(station) || '<span class="subscription-value">Go 订阅 · 读取中</span>';
  }
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

function probeResultLabel(station) {
  if (station.last_probe_ok == null) return "尚未检查";
  const kind = { catalog: "目录", connection: "连接", generation: "生成" }[station.last_probe_kind] || "检查";
  return `${kind}${station.last_probe_ok ? (station.last_probe_kind === "catalog" ? "可达" : "成功") : "失败"}`;
}

function statusInfo(station) {
  const protection = station.protection || {};
  const now = Date.now() / 1000;
  const until = finiteNumber(protection.until);
  const remaining = Math.max(0, Math.ceil(until - now));
  const source = { request: "业务请求", generation_probe: "生成测试", connection_probe: "连接检查" }[protection.source] || "请求";
  const code = protection.status_code ? `HTTP ${protection.status_code}` : "连接失败 / 超时";
  if (!station.enabled) return { className: "unknown", label: "已停用", detail: "管理员停用，不参与路由" };
  if (station.quota_exhausted) return { className: "quota", label: "配额保护", detail: protection.reason || station.quota_error || "上游报告额度不可用" };
  if (protection.kind === "rate_limited" && remaining > 0 || finiteNumber(station.rate_limit_until) > now) {
    const wait = remaining || Math.ceil(station.rate_limit_until - now);
    return { className: "cooling", label: `限流冷却 · ${wait} 秒`,
      detail: `${source}收到上游 429 · ${protection.wait_source === "upstream" ? "遵守上游等待时间" : "网关暂缓路由"}`,
      reason: protection.reason };
  }
  if (protection.kind === "breaker" && remaining > 0 || station.status === "down" && !until) {
    return { className: "down", label: `熔断${remaining ? ` · ${remaining} 秒` : ""}`,
      detail: `网关保护 · 连续 ${protection.failure_count ?? station.consecutive_failures ?? 0} 次故障 · 最近 ${code}（${source}）`,
      reason: protection.reason || station.last_error };
  }
  if (["breaker", "recovering"].includes(protection.kind) || ["cooling", "recovering"].includes(station.status)) {
    return { className: "cooling", label: "待恢复验证", detail: "冷却已结束，将以单个业务请求验证恢复；也可手动验证", reason: protection.reason };
  }
  switch (station.status) {
    case "up": return { className: "up", label: "在线", detail: "允许参与路由" };
    default: return { className: "unknown", label: "未知", detail: "" };
  }
}

function protectionMarkup(station) {
  const state = statusInfo(station);
  const title = [state.detail, state.reason].filter(Boolean).join("\n");
  return `<span class="protection-status" data-protection-name="${escapeHtml(station.name)}" title="${escapeHtml(title)}"><span class="station-state ${state.className}">${escapeHtml(state.label)}</span>${station.enabled && state.className !== "up" ? `<span class="protection-reason">${escapeHtml(state.detail)}</span>` : ""}</span>`;
}

function updateProtectionDisplays() {
  const stations = new Map((statusData?.stations || []).map(station => [station.name, station]));
  document.querySelectorAll("[data-protection-name]").forEach(element => {
    const station = stations.get(element.dataset.protectionName);
    if (station) element.outerHTML = protectionMarkup(station);
  });
}

function sortedStations(stations) {
  return [...(stations || [])].sort((left, right) => {
    const leftRank = left.rank == null ? Number.MAX_SAFE_INTEGER : finiteNumber(left.rank, Number.MAX_SAFE_INTEGER);
    const rightRank = right.rank == null ? Number.MAX_SAFE_INTEGER : finiteNumber(right.rank, Number.MAX_SAFE_INTEGER);
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
  const accountContext = window.modelHarborAccount;
  if (accountContext && path.startsWith("/admin/") && !path.startsWith("/admin/accounts")) {
    headers["X-ModelHarbor-Account"] = accountContext.id;
    headers["X-ModelHarbor-Revision"] = accountContext.revision;
  }
  const key = localStorage.getItem("zg_admin_key");
  if (key) headers.Authorization = `Bearer ${key}`;
  if (body) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    method, headers, cache: "no-store",
    body: body ? JSON.stringify(body) : undefined,
  });
  const responseRevision = response.headers?.get?.("X-ModelHarbor-Revision");
  if (accountContext && !path.startsWith("/admin/accounts") && ((responseRevision && responseRevision !== accountContext.revision) || response.status === 409 && path === "/admin/status")) {
    window.location.reload();
    throw new Error("当前网关账号已切换，正在刷新");
  }
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
  if (eventType === "account_changed") { window.location.reload(); return false; }
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


// Subsections preserve form values and keep navigation within the viewport.
const viewportPages = { monitor: 1, stations: 1 };
let viewportLayoutTimer = null;
function setSectionTab(group, section, { focus = false } = {}) {
  const nav = document.querySelector('[data-tab-group="' + group + '"]');
  if (!nav) return;
  const button = [...nav.querySelectorAll('[role="tab"]')].find(el => el.dataset.section === section);
  if (!button) return;
  nav.querySelectorAll('[role="tab"]').forEach(el => {
    const active = el === button;
    el.setAttribute("aria-selected", String(active));
    el.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('[data-section-panel="' + group + '"]').forEach(el => {
    el.hidden = el.dataset.section !== section;
  });
  if (group === "stations") refreshStationPolicy();
  if (group === "events") updateEventsLayout();
  if (focus) button.focus({ preventScroll: true });
  scheduleViewportLayout();
}
function refreshStationPolicy(force = false) {
  const section = document.querySelector('[data-tab-group="stations"] [aria-selected="true"]')?.dataset.section;
  if (section === "retry" && (force || Date.now() - retrySettingsUpdatedAt > 30000)) return refreshRetrySettings({ notify: true });
  if (["probe", "editor"].includes(section) && (force || Date.now() - probeSettingsUpdatedAt > 30000)) return refreshProbeSettings();
}
function scheduleViewportLayout() {
  clearTimeout(viewportLayoutTimer);
  viewportLayoutTimer = setTimeout(fitViewportContent, 40);
}
function paginateElements(key, elements, capacity, pager) {
  const pages = Math.max(1, Math.ceil(elements.length / capacity));
  viewportPages[key] = clamp(viewportPages[key], 1, pages);
  const start = (viewportPages[key] - 1) * capacity;
  elements.forEach((el, index) => { el.hidden = index < start || index >= start + capacity; });
  pager.querySelector(".page-info").textContent = "第 " + viewportPages[key] + " / " + pages + " 页 · 共 " + elements.length + " 个站点";
  pager.querySelector('[data-page-step="-1"]').disabled = viewportPages[key] <= 1;
  pager.querySelector('[data-page-step="1"]').disabled = viewportPages[key] >= pages;
}
function fitViewportContent() {
  updateEventsLayout();
  if ($("editor-models").getClientRects().length) renderStationModels();
  const viewport = $("monitorViewport");
  if (currentTab === "monitor" && viewport.clientHeight > 0) {
    const cardMode = currentView === "card";
    const rows = [...document.querySelectorAll(cardMode ? "#grid .station-card" : "#list-wrap tbody tr:not(:has(.empty-cell))")];
    rows.forEach(row => { row.hidden = false; });
    let capacity = 1;
    if (cardMode) {
      const columns = getComputedStyle($("grid")).gridTemplateColumns.split(" ").length;
      capacity = Math.max(1, columns);
    } else {
      const head = document.querySelector("#list-wrap thead");
      const headerHeight = head?.getBoundingClientRect().height || 38;
      const rowHeight = Math.max(48, ...rows.map(row => row.getBoundingClientRect().height));
      capacity = Math.max(1, Math.floor((viewport.clientHeight - headerHeight - 18) / rowHeight));
    }
    paginateElements("monitor", rows, capacity, $("monitorPagination"));
  }
  const shell = $("stationsTableShell");
  if (currentTab === "stations" && shell.clientHeight > 0) {
    const rows = [...document.querySelectorAll("#stations-body tr:not(:has(.empty-cell))")];
    rows.forEach(row => { row.hidden = false; });
    const headerHeight = $("stations-table").querySelector("thead").getBoundingClientRect().height;
    const rowHeight = Math.max(48, ...rows.map(row => row.getBoundingClientRect().height));
    const capacity = Math.max(1, Math.floor((shell.clientHeight - headerHeight - 18) / rowHeight));
    paginateElements("stations", rows, capacity, $("stationsPagination"));
  }
  const eventsShell = document.querySelector(".event-shell");
  if (currentTab === "events" && eventsShell.clientHeight > 0) {
    const headerHeight = eventsShell.querySelector("thead").getBoundingClientRect().height || 38;
    const rowHeight = Math.max(44, ...[...$("log-body").querySelectorAll("tr:not(:has(.empty-cell))")].map(row => row.getBoundingClientRect().height));
    const size = clamp(Math.floor((eventsShell.clientHeight - headerHeight - 18) / rowHeight), 1, 50);
    if (size !== EVENT_PAGE_SIZE) {
      if (eventsRunning) { scheduleViewportLayout(); return; }
      EVENT_PAGE_SIZE = size;
      eventPage = 1;
      refreshEvents(1);
    }
  }
}
function updateEventsLayout() {
  const combined = window.innerWidth >= 1100 && window.innerHeight >= 700;
  const panel = $("events-wrap");
  const previous = panel.dataset.layout;
  panel.dataset.layout = combined ? "combined" : "tabs";
  panel.querySelector(".event-toolbar").hidden = false;
  const selected = panel.querySelector('[data-tab-group="events"] [aria-selected="true"]').dataset.section;
  panel.querySelectorAll('[data-section-panel="events"]').forEach(el => {
    el.hidden = selected === "sources" ? el.dataset.section !== "sources" : combined ? el.dataset.section === "sources" : el.dataset.section !== selected;
    el.setAttribute("role", combined && selected !== "sources" ? "region" : "tabpanel");
  });
  if (combined || previous === "combined") {
    $("eventFilters").hidden = !combined;
    $("eventFiltersToggle").setAttribute("aria-expanded", String(combined));
  }
}
function initializeViewportLayout() {
  $("eventFiltersToggle").addEventListener("click", () => {
    const expanded = $("eventFilters").hidden;
    $("eventFilters").hidden = !expanded;
    $("eventFiltersToggle").setAttribute("aria-expanded", String(expanded));
    scheduleViewportLayout();
  });
  $("cardSections").addEventListener("click", event => {
    const button = event.target.closest("[data-card-section]");
    if (!button) return;
    $("grid").dataset.cardSection = button.dataset.cardSection;
    $("cardSections").querySelectorAll("button").forEach(el => el.setAttribute("aria-pressed", String(el === button)));
    scheduleViewportLayout();
  });
  document.querySelectorAll("[data-tab-group]").forEach(nav => {
    nav.addEventListener("click", event => {
      const button = event.target.closest('[role="tab"]');
      if (button) setSectionTab(nav.dataset.tabGroup, button.dataset.section);
    });
    nav.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const tabs = [...nav.querySelectorAll('[role="tab"]')];
      const current = tabs.indexOf(event.target);
      if (current < 0) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      setSectionTab(nav.dataset.tabGroup, tabs[next].dataset.section, { focus: true });
    });
  });
  for (const [key, id] of [["monitor", "monitorPagination"], ["stations", "stationsPagination"]]) {
    $(id).addEventListener("click", event => {
      const button = event.target.closest("[data-page-step]");
      if (!button || button.disabled) return;
      viewportPages[key] += Number(button.dataset.pageStep);
      fitViewportContent();
    });
  }
  $("station-form").addEventListener("invalid", event => {
    const panel = event.target.closest('[data-section-panel="editor"]');
    if (panel) {
      setSectionTab("stations", "editor");
      setSectionTab("editor", panel.dataset.section);
    }
  }, true);
  // Validation can reveal a field inside an inactive settings subsection.
  $("retry-form").addEventListener("invalid", event => {
    const panel = event.target.closest("[data-section-panel]");
    if (panel) {
      setSectionTab("stations", "retry");
      setSectionTab("retry", panel.dataset.section);
    }
  }, true);
  if (window.ResizeObserver) {
    const observer = new ResizeObserver(scheduleViewportLayout);
    [$("monitorViewport"), $("stationsTableShell"), document.querySelector(".event-shell")].forEach(el => observer.observe(el));
  }
  window.addEventListener("resize", scheduleViewportLayout);
}

const PAGE_META = {
  "monitor": [
    "运行概览",
    "掌握站点健康、路由动态与服务表现。",
    "OVERVIEW"
  ],
  "stats": [
    "用量分析",
    "了解模型用量、缓存命中与每小时调用趋势。",
    "ANALYTICS"
  ],
  "stations": [
    "站点管理",
    "管理上游连接、模型与探测，配置站点间的故障切换。",
    "CONNECTIONS"
  ],
  "access": [
    "IP 白名单",
    "管理当前账号允许访问的 IP，独立保存并立即生效。",
    "ACCESS CONTROL"
  ],
  "service": ["服务设置", "配置网关监听端口并管理服务进程。", "GATEWAY SERVICE"],
  "events": [
    "请求日志",
    "查看最近调用记录与错误详情。",
    "ACTIVITY"
  ]
};

function applyTab() {
  const [title, description, eyebrow] = PAGE_META[currentTab];
  $("pageTitle").textContent = title;
  $("pageDescription").textContent = description;
  $("pageEyebrow").textContent = eyebrow;
  $("breadcrumbCurrent").textContent = $("tab-" + currentTab).textContent.trim();
  document.title = `${title} · ModelHarbor`;
  document.querySelector(".page-content").dataset.activeTab = currentTab;
  $("summaryStrip").hidden = !["monitor", "stats"].includes(currentTab);
  $("routingPanel").hidden = currentTab !== "monitor";
  const monitorActive = currentTab === "monitor";
  const statsActive = currentTab === "stats";
  const eventsActive = currentTab === "events";
  const stationsActive = currentTab === "stations";
  ["monitor", "stats", "stations", "access", "service", "events"].forEach(k => {
    $("tab-" + k).classList.toggle("is-active", currentTab === k);
    $("tab-" + k).setAttribute("aria-selected", String(currentTab === k));
    $("tab-" + k).setAttribute("tabindex", currentTab === k ? "0" : "-1");
  });
  $("monitor-wrap").hidden = !monitorActive;
  $("stats-wrap").hidden = !statsActive;
  $("events-wrap").hidden = !eventsActive;
  $("stations-wrap").hidden = !stationsActive;
  $("access-wrap").hidden = currentTab !== "access";
  $("service-wrap").hidden = currentTab !== "service";
  $("viewSwitch").hidden = !monitorActive;
  scheduleViewportLayout();
}

function setTab(tab) {
  if (["retry", "probe"].includes(tab)) { setTab("stations"); setSectionTab("stations", tab); return; }
  if (!["monitor", "stats", "events", "stations", "access", "service"].includes(tab)) return;
  if (tab === "service" && !canManageService) return;
  currentTab = tab;
  localStorage.setItem("zg_tab", tab);
  applyTab();
  if (tab === "stats" && Date.now() - statsUpdatedAt > 30000) refreshStats();
  if (tab === "events" && Date.now() - eventsUpdatedAt > 5000) refreshEvents();
  if (tab === "stations" && Date.now() - stationsUpdatedAt > 10000) {
    refreshStations({ notifyError: true });
  }
  if (tab === "stations") refreshStationPolicy();
  if (tab === "access" && !accessSettingsLoaded) refreshAccessSettings();
  if (tab === "service" && Date.now() - serviceSettingsUpdatedAt > 30000) refreshServiceSettings();
}

function applyView() {
  $("detailToggle").hidden = currentView === "card";
  $("cardSections").hidden = currentView !== "card";
  const listActive = currentView === "list";
  $("btn-list").classList.toggle("is-active", listActive);
  $("btn-card").classList.toggle("is-active", !listActive);
  $("btn-list").setAttribute("aria-pressed", String(listActive));
  $("btn-card").setAttribute("aria-pressed", String(!listActive));
  $("list-wrap").hidden = !listActive;
  $("grid").hidden = listActive;
  fitViewportContent();
}

function setView(view) {
  if (!["list", "card"].includes(view)) return;
  currentView = view;
  viewportPages.monitor = 1;
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
  const className = `action-button${options.destructive ? " is-destructive" : ""}${options.primary ? " is-primary-action" : ""}`;
  const buttonLabel = pending ? "处理中" : label;
  return `<button class="${className}" type="button" data-action="${action}"
                  data-station="${escapeHtml(station.name)}" title="${escapeHtml(options.title || label)}" ${disabled ? "disabled" : ""}>${buttonLabel}</button>`;
}

function stationActions(station) {
  const kind = station.protection?.kind;
  const go = station.provider === "opencode" && station.opencode_plan === "go";
  let action = "probe", label = "连接检查";
  let title = "读取连接状态，不发送生成请求，也不解除生成故障保护";
  if (station.quota_exhausted) {
    action = "reset"; label = "检查额度";
    title = "按站点策略验证额度恢复；生成检查可能产生用量";
  } else if (kind === "recovering" && !go) {
    action = "generation-probe"; label = "验证恢复";
    title = "用站点配置的探测模型发送一次生成请求，可能产生用量；成功后解除熔断";
  }
  return [
    actionButton(action, station, label, { primary: true, title, disabled: !station.enabled }),
    actionButton("toggle", station, station.enabled ? "停用" : "启用", { destructive: station.enabled }),
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

  const inflight = Math.max(0, finiteNumber(
    activity.inflight ?? data.inflight ?? activeRoutes.length,
  ));
  const routeGroups = new Map();
  const addRoute = (stationValue, modelValue, phaseValue = "", count = 1) => {
    const station = String(stationValue || "").trim() || "未知线路";
    const model = String(modelValue || "").trim() || "未识别模型";
    const key = `${station}\u0000${model}`;
    const current = routeGroups.get(key) || { station, model, count: 0, phases: new Set() };
    current.count += Math.max(1, finiteNumber(count));
    const phase = String(phaseValue || "").trim();
    if (phase) current.phases.add(phase);
    routeGroups.set(key, current);
  };
  if (activeRoutes.length) {
    activeRoutes.forEach((route) => addRoute(
      route.station || route.name,
      route.model,
      route.phase,
    ));
  } else {
    activeStations.forEach((station) => {
      const stationName = station.station || station.name;
      (Array.isArray(station.models) ? station.models : []).forEach((entry) => {
        if (entry && typeof entry === "object") {
          addRoute(stationName, entry.model, "", entry.inflight);
        } else {
          addRoute(stationName, entry);
        }
      });
    });
  }
  const groups = [...routeGroups.values()];
  const isActive = inflight > 0 || groups.length > 0;
  const routeLive = $("routeLive");
  routeLive.classList.toggle("is-active", isActive);
  const routeList = $("routeList");
  routeList.replaceChildren();

  const appendRoute = (station, model, phase, count = 0) => {
    const item = document.createElement("div");
    item.className = "route-item";
    item.setAttribute("role", "listitem");
    const stationName = document.createElement("span");
    stationName.className = "route-item-station";
    stationName.textContent = station;
    stationName.title = station;
    const arrow = document.createElement("span");
    arrow.className = "route-item-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "→";
    const modelName = document.createElement("strong");
    modelName.className = "route-item-model";
    modelName.textContent = model;
    modelName.title = model;
    const phaseLabel = document.createElement("span");
    phaseLabel.className = "route-phase";
    phaseLabel.textContent = phase;
    const countLabel = document.createElement("span");
    countLabel.className = "route-count";
    countLabel.textContent = count > 1 ? `× ${formatCount(count)}` : "";
    item.append(stationName, arrow, modelName, phaseLabel, countLabel);
    routeList.append(item);
  };
  const appendPlaceholder = (label) => {
    const placeholder = document.createElement("div");
    placeholder.className = "route-placeholder";
    placeholder.setAttribute("role", "listitem");
    placeholder.textContent = label;
    routeList.append(placeholder);
  };

  if (isActive) {
    $("routeLabel").textContent = "正在使用";
    $("routeMeta").textContent = `${formatCount(inflight || activeRoutes.length)} 个请求 · ${formatCount(groups.length)} 组线路与模型`;
    groups.forEach((group) => {
      const phase = group.phases.size === 1
        ? ({ connecting: "连接中", receiving: "接收中", streaming: "流式响应" }[group.phases.values().next().value] || "处理中")
        : (group.phases.size > 1 ? "多阶段" : "处理中");
      appendRoute(group.station, group.model, phase, group.count);
    });
    if (!groups.length) appendPlaceholder("请求处理中，线路信息暂不可用");
    routeLive.title = "";
    return;
  }

  const lastSuccess = activity.last_success || data.last_success || data.last_routed || {};
  const lastAttempt = activity.last_attempt || data.last_attempt || {};
  const recent = lastSuccess.station ? lastSuccess : lastAttempt;
  if (recent.station) {
    $("routeLabel").textContent = lastSuccess.station ? "最近使用" : "最近尝试";
    appendRoute(String(recent.station), String(recent.model || "未识别模型"), lastSuccess.station ? "最近成功" : "最近尝试");
    $("routeMeta").textContent = formatAge(recent.finished_at || recent.ts || recent.started_at);
    routeLive.title = "";
    return;
  }

  $("routeLabel").textContent = "当前路由";
  $("routeMeta").textContent = "";
  appendPlaceholder("等待首次请求");
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

  $("sub").textContent = "自动刷新 · 显示进行中与最近一次调用";
  renderRouteStatus(data);
}

function filteredMonitorData(data) {
  const query = $("station-search").value.trim().toLocaleLowerCase();
  const filter = $("station-filter").value;
  const all = data.stations || [];
  const scoped = all.filter(station => Boolean(station.enabled) === (stationScope === "enabled"));
  $("station-enabled-count").textContent = all.filter(station => station.enabled).length;
  $("station-disabled-count").textContent = all.filter(station => !station.enabled).length;
  const stations = scoped.filter((station) => {
    const searchable = [station.name, ...(station.models || [])].join(" ").toLocaleLowerCase();
    const state = statusInfo(station);
    const matchesState = stationScope === "disabled" || filter === "all"
      || (filter === "up" && station.enabled && state.className === "up")
      || (filter === "attention" && station.enabled && state.className !== "up");
    return (!query || searchable.includes(query)) && matchesState;
  });
  $("monitor-count").textContent = `${stations.length} / ${scoped.length}`;
  return { ...data, stations };
}

function renderCards(data) {
  const threshold = data.gateway?.balance_alert_threshold;
  $("grid").innerHTML = sortedStations(data.stations).map((station) => {
    const state = statusInfo(station);
    const score = clamp(station.health_score, 0, 100);
    const scoreClass = scoreState(score);
    const rate = clamp(finiteNumber(station.window_success_rate) * 100, 0, 100);
    const billing = station.billing === "subscription" ? "订阅制" : "余额制";
    const models = station.accepts_any_model
      ? [`<span class="model-tag">${station.route_models?.length ? '按范围透传' : '接受任意模型'}</span>`]
      : (station.models || []).slice(0, 6).map((model) => `<span class="model-tag" title="${escapeHtml(model)}">${escapeHtml(model)}</span>`);
    if (!station.accepts_any_model && (station.models || []).length > 6) {
      models.push(`<span class="model-tag">+${(station.models || []).length - 6}</span>`);
    }
    const recoveryHint = quotaRecoveryHint(station, data.gateway);
    const alert = recoveryHint
      ? `<p class="card-alert">${escapeHtml(recoveryHint)}</p>`
      : (station.last_error ? `<p class="card-alert">最近错误: ${escapeHtml(station.last_error)}</p>` : "");
    const probeValue = probeResultLabel(station);
    const scoreTitle = scoreBreakdownTitle(station.health_score_breakdown);

    return `<article class="station-card ${station.enabled ? "" : "is-disabled"}">
      <header class="card-header">
        <div class="card-title-wrap">
          <div class="card-title"><span class="status-dot ${state.className}" aria-hidden="true"></span><span>${escapeHtml(station.name)}</span></div>
          <div class="card-meta">
            ${protectionMarkup(station)}
            <span class="status-tag">${billing}</span>
            <span class="status-tag">${station.priority_auto ? "自动优先级" : `手动 ${escapeHtml(station.priority)}`}</span>
          </div>
        </div>
        <span class="card-rank">#${escapeHtml(station.rank || "-")}</span>
      </header>

      <div class="card-content"><div class="health-row" title="${escapeHtml(scoreTitle)}">
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
        <div class="card-stat"><span class="card-stat-label">额度</span><span class="card-stat-value">${formatBalance(station.balance, threshold, station)}</span></div>
        <div class="card-stat"><span class="card-stat-label">最近检查</span><span class="card-stat-value">${probeValue}</span></div>
        <div class="card-stat"><span class="card-stat-label">故障切换</span><span class="card-stat-value">${formatCount(station.switch_in)} 入 / ${formatCount(station.switch_out)} 出</span></div>
      </div>

      <div class="card-probe">${probeHtml(station.history_hour)}</div>
      <div class="models">${models.join("")}</div>
      ${alert}</div>
      <div class="card-actions">${stationActions(station)}</div>
    </article>`;
  }).join("") || '<div class="empty-cell">没有匹配的站点，请调整搜索或筛选条件</div>';

  applyDataWidths($("grid"));
}

function updateMonitorRows(tbody, markup) {
  const template = document.createElement("template");
  template.innerHTML = `<table><tbody>${markup}</tbody></table>`;
  const nextRows = [...template.content.querySelector("tbody").children];
  const previous = new Map([...tbody.children].map(row => [row.dataset.monitorStation, row]));
  const names = new Set(nextRows.map(row => row.dataset.monitorStation));
  for (const row of [...tbody.children]) if (!names.has(row.dataset.monitorStation)) row.remove();
  let anchor = tbody.firstElementChild;
  for (const next of nextRows) {
    let row = previous.get(next.dataset.monitorStation);
    if (row && next.dataset.monitorStation != null) {
      row.className = next.className;
      for (let i = 0; i < next.cells.length; i++) {
        // A live sandbox must stay attached: replacing its ancestors reloads it.
        if (next.cells[i].classList.contains("col-visual")) continue;
        if (row.cells[i].outerHTML !== next.cells[i].outerHTML) row.cells[i].replaceWith(next.cells[i].cloneNode(true));
      }
    } else if (row) {
      if (row.innerHTML !== next.innerHTML) row.innerHTML = next.innerHTML;
    } else row = next;
    if (row !== anchor) {
      if (row.parentNode === tbody && typeof tbody.moveBefore === "function") tbody.moveBefore(row, anchor);
      else tbody.insertBefore(row, anchor);
    }
    anchor = row.nextElementSibling;
  }
}

function renderList(data) {
  const threshold = data.gateway?.balance_alert_threshold;
  const rows = sortedStations(data.stations).map((station) => {
    const state = statusInfo(station);
    const score = clamp(station.health_score, 0, 100);
    const rate = clamp(finiteNumber(station.window_success_rate) * 100, 0, 100);
    const billing = station.billing === "subscription" ? "订阅" : "余额";
    const probeLabel = probeResultLabel(station);
    const probeClass = station.last_probe_ok == null ? "muted" : (station.last_probe_ok ? "text-ok" : "text-danger");
    const error = station.last_error || station.last_probe_error || "-";
    const scoreTitle = scoreBreakdownTitle(station.health_score_breakdown);

    return `<tr data-monitor-station="${escapeHtml(station.name)}" class="${station.enabled ? "" : "is-disabled"}">
      <td class="col-rank muted rank-value">#${escapeHtml(station.rank || "-")}</td>
      <td class="col-station">
        <div class="station-name" title="${escapeHtml(station.base_url)}">
          <span class="status-dot ${state.className}" aria-hidden="true"></span>
          <span class="station-name-text">${escapeHtml(station.name)}</span>
        </div>
        <div class="tag-row">${protectionMarkup(station)}</div>
      </td>
      <td class="col-models">${window.ModelHarborVisualTests?.modelMarkup(station) || '<span class="muted">—</span>'}</td>
      <td class="col-billing muted">${billing}</td>
      <td class="col-score" title="${escapeHtml(scoreTitle)}"><span class="score-value ${scoreState(score)}">${score.toFixed(1)}</span></td>
      <td class="col-latency">${formatLatency(station.ema_latency)}</td>
      <td class="col-ttft">${formatLatency(station.ttft_ema ?? (station.avg_ttft_ms != null ? station.avg_ttft_ms / 1000 : null))}</td>
      <td class="col-cache">${station.cache_hit_rate == null ? '<span class="muted">-</span>' : (station.cache_hit_rate >= 0.8 ? `<span class="ok">${(station.cache_hit_rate * 100).toFixed(1)}%</span>` : `<span class="warn">${(station.cache_hit_rate * 100).toFixed(1)}%</span>`)}</td>
      <td class="col-juice" title="${escapeHtml(juiceDetail(station))}">${station.juice_score == null ? '<span class="muted">-</span>' : (station.juice_score >= 90 ? `<span class="ok">${station.juice_score}</span>` : (station.juice_score >= 60 ? `<span class="warn">${station.juice_score}</span>` : `<span class="bad">${station.juice_score}</span>`))}</td>
      <td class="col-rate number-value">${percentFormat.format(rate)}%</td>
      <td class="col-calls number-value">${formatCount(station.calls)} <span class="text-danger">/ ${formatCount(station.failures)}</span></td>
      <td class="col-balance">${formatBalance(station.balance, threshold, station)}</td>
      <td class="col-probe"><span class="${probeClass}">${probeLabel}</span></td>
      <td class="col-checked muted">${formatTime(station.last_check_at ?? station.last_probe_at)}</td>
      <td class="col-error error-cell" title="${escapeHtml(error)}">${escapeHtml(error)}</td>
      <td class="col-visual" data-table-visual="${escapeHtml(station.name)}"><span class="muted">—</span></td>
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

  if (!$("list-wrap").querySelector(".monitor-table")) $("list-wrap").innerHTML = `
    <div class="table-shell monitor-shell">
      <table class="monitor-table">
        <caption class="sr-only">中转站实时状态</caption>
        <thead><tr>
          <th class="col-rank">排名</th><th class="col-station">站点</th><th class="col-models">支持的模型</th><th class="col-billing">计费</th>
          <th class="col-score">评分</th><th class="col-latency">延迟 EMA</th><th class="col-ttft">首字</th><th class="col-cache">缓存命中</th><th class="col-juice">Juice</th><th class="col-rate">成功率</th>
          <th class="col-calls">调用 / 失败</th><th class="col-balance">额度</th><th class="col-probe">探测</th>
          <th class="col-checked">最后检查</th><th class="col-error">最近错误</th><th class="col-visual">智商测试</th><th class="col-actions">操作</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
`;
  updateMonitorRows($("list-wrap").querySelector("tbody"), rows || '<tr><td colspan="17" class="empty-cell">没有匹配的站点，请调整搜索或筛选条件</td></tr>');
  $("monitor-history").innerHTML = `
    <section class="probe-block" aria-labelledby="probe-heading">
      <div class="probe-header">
        <span class="probe-title" id="probe-heading">60 分钟可用性</span>
        <span class="probe-range">60 分钟前 → 现在 · 每格 1 分钟</span>
      </div>
      <div class="probe-scroll"><div class="probe-rows">${probes}</div></div>
      ${PROBE_LEGEND}
    </section>`;
  window.ModelHarborVisualTests?.syncTablePreviews();
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
  $("eventCount").textContent = `筛选结果 ${formatCount(total)} 条 · 留存 ${formatCount(data.retained_events ?? total)} / ${formatCount(data.max_events || 500)} 条`;
  $("log-body").innerHTML = events.map((event) => {
    const latency = finiteNumber(event.latency_ms, Number.NaN);
    const detail = event.detail || "-";
    const attempt = finiteNumber(event.attempts, 1);
    return `<tr>
      <td class="event-time muted">${formatTime(event.ts)}</td>
      <td class="event-ip" title="${escapeHtml(event.client_ip || "此记录未保存客户端地址，无法还原来源")}">${escapeHtml(event.client_ip || "未记录 IP")}</td>
      <td class="event-station">${escapeHtml(event.station || "-")}</td>
      <td class="event-model">${escapeHtml(event.model || "-")}</td>
      <td class="event-result ${event.ok ? "text-ok" : "text-danger"}">${event.ok ? "成功" : "失败"}</td>
      <td class="event-latency">${Number.isFinite(latency) && latency > 0 ? `${(latency / 1000).toFixed(2)}s` : "-"}</td>
      <td class="event-detail" title="${escapeHtml(detail)}">${escapeHtml(detail)}${attempt > 1 ? ` <span class="attempt-count">(${attempt} 次尝试)</span>` : ""}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="7" class="empty-cell">暂无事件</td></tr>';
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
    const monitorData = filteredMonitorData(statusData);
    renderCards(monitorData);
    renderList(monitorData);
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
    await window.refreshUsageHistory?.();
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
    const query = new URLSearchParams({
      page: String(Math.max(1, finiteNumber(page, 1))),
      page_size: String(EVENT_PAGE_SIZE),
    });
    for (const [id, name] of [["eventIpFilter", "ip"], ["eventModelFilter", "model"],
                              ["eventDateFrom", "date_from"], ["eventDateTo", "date_to"]]) {
      const value = $(id)?.value;
      if (value) query.set(name, value);
    }
    eventsData = await api(`/admin/logs?${query.toString()}`);
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
window.refreshEvents = refreshEvents;

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

function setProbeSettingsStatus(message, state = "") {
  const element = $("probeSettingsStatus");
  element.textContent = message;
  element.classList.toggle("is-error", state === "error");
  element.classList.toggle("is-success", state === "success");
}

function setProbeSettingsBusy() {
  const busy = probeSettingsLoading || probeSettingsSaving;
  $("probe-form").setAttribute("aria-busy", String(busy));
  $("probe-enabled").disabled = busy;
  $("probe-interval").disabled = busy;
  $("probe-save").disabled = busy;
  $("probe-reload").disabled = busy;
  $("probe-save").textContent = probeSettingsSaving ? "保存中..." : "保存设置";
}

function applyProbeSettings(settings) {
  if (typeof settings?.generation_probe_enabled !== "boolean") {
    throw new Error("接口未返回有效的生成探测状态");
  }
  const interval = finiteNumber(settings.generation_probe_interval, Number.NaN);
  if (!Number.isFinite(interval) || interval < 300 || interval > 86400) {
    throw new Error("接口未返回有效的生成探测间隔");
  }
  $("probe-enabled").checked = settings.generation_probe_enabled;
  $("probe-interval").value = String(interval);
  probeSettingsData = settings;
  renderEffectiveProbePolicy();
}

async function refreshProbeSettings() {
  if (probeSettingsLoading || probeSettingsSaving) return;
  probeSettingsLoading = true;
  setProbeSettingsBusy();
  setProbeSettingsStatus("正在读取设置...");
  try {
    const settings = await api("/admin/settings/probe");
    applyProbeSettings(settings);
    probeSettingsUpdatedAt = Date.now();
    setProbeSettingsStatus("");
  } catch (error) {
    setProbeSettingsStatus(`读取失败: ${error.message}`, "error");
  } finally {
    probeSettingsLoading = false;
    setProbeSettingsBusy();
  }
}

async function saveProbeSettings() {
  if (probeSettingsLoading || probeSettingsSaving) return;
  const interval = Number($("probe-interval").value);
  if (!Number.isInteger(interval) || interval < 300 || interval > 86400) {
    $("probe-interval").focus({ preventScroll: true });
    setProbeSettingsStatus("间隔必须是 300 到 86400 秒之间的整数", "error");
    return;
  }
  probeSettingsSaving = true;
  setProbeSettingsBusy();
  setProbeSettingsStatus("正在保存设置...");
  try {
    const settings = await api("/admin/settings/probe", "PUT", true, {
      generation_probe_enabled: $("probe-enabled").checked,
      generation_probe_interval: interval,
    });
    applyProbeSettings(settings);
    probeSettingsUpdatedAt = Date.now();
    setProbeSettingsStatus("探测设置已保存", "success");
    showToast("探测设置已保存");
  } catch (error) {
    setProbeSettingsStatus(`保存失败: ${error.message}`, "error");
  } finally {
    probeSettingsSaving = false;
    setProbeSettingsBusy();
  }
}

function setAccessSettingsStatus(message, failed = false) {
  const status = $("accessSettingsStatus");
  status.textContent = message;
  status.classList.toggle("is-error", failed);
  status.classList.toggle("is-success", !failed && accessSettingsLoaded);
}

function setAccessSettingsBusy(busy) {
  accessSettingsBusy = busy;
  $("access-form").setAttribute("aria-busy", String(busy));
  $("access-ips").disabled = busy || !accessSettingsLoaded;
  $("access-save").disabled = busy || !accessSettingsLoaded;
  $("access-reload").disabled = busy;
}

function applyAccessSettings(payload) {
  if (!Array.isArray(payload?.allowed_ips)
      || !payload.allowed_ips.every((value) => typeof value === "string")) {
    throw new Error("接口未返回有效的 IP 白名单");
  }
  $("access-ips").value = payload.allowed_ips.join("\n");
  $("access-client").textContent = `当前访问 IP：${payload.client_ip || "未知"}。保存时需保留当前 IP，避免失去访问权限。`;
  accessSettingsLoaded = true;
}

async function refreshAccessSettings() {
  if (accessSettingsBusy) return;
  setAccessSettingsBusy(true);
  setAccessSettingsStatus("正在读取白名单...");
  try {
    applyAccessSettings(await api("/admin/settings/access"));
    setAccessSettingsStatus("已加载最新白名单");
  } catch (error) {
    setAccessSettingsStatus(`读取失败: ${error.message}`, true);
  } finally {
    setAccessSettingsBusy(false);
  }
}

async function saveAccessSettings() {
  if (accessSettingsBusy || !accessSettingsLoaded) return;
  const body = {
    allowed_ips: [...new Set($("access-ips").value.split(/[\s,，]+/).filter(Boolean))],
  };
  setAccessSettingsBusy(true);
  setAccessSettingsStatus("正在保存白名单...");
  try {
    applyAccessSettings(await api("/admin/settings/access", "PUT", true, body));
    setAccessSettingsStatus("白名单已保存，对新请求立即生效");
    showToast("IP 白名单已保存");
  } catch (error) {
    setAccessSettingsStatus(`保存失败: ${error.message}`, true);
  } finally {
    setAccessSettingsBusy(false);
  }
}

async function refreshCapabilities() {
  try {
    const result = await api("/admin/capabilities");
    canManageService = result.can_manage_service === true;
  } catch {
    canManageService = false;
  }
  $("tab-service").hidden = !canManageService;
  if (!canManageService && currentTab === "service") setTab("monitor");
  else if (canManageService && localStorage.getItem("zg_tab") === "service") setTab("service");
}

function setServiceSettingsStatus(message, failed = false) {
  const status = $("serviceSettingsStatus");
  status.textContent = message;
  status.classList.toggle("is-error", failed);
  status.classList.toggle("is-success", !failed && serviceSettingsLoaded);
}

function setServiceSettingsBusy(busy) {
  serviceSettingsBusy = busy;
  $("service-form").setAttribute("aria-busy", String(busy));
  $("service-port").disabled = busy || !serviceSettingsLoaded;
  $("service-save").disabled = busy || !serviceSettingsLoaded;
  $("service-reload").disabled = busy;
  $("service-restart").disabled = busy || !serviceSettingsLoaded;
  $("service-restart").setAttribute("aria-busy", String(busy));
  $("endpoint-public-url").disabled = busy || endpointSettingsBusy;
  $("endpoint-save").disabled = busy || endpointSettingsBusy;
}

function applyServiceSettings(payload) {
  if (!Number.isInteger(payload?.port) || payload.port < 1 || payload.port > 65535
      || !Number.isInteger(payload?.active_port)) {
    throw new Error("接口未返回有效的服务端口");
  }
  serviceSettingsData = payload;
  $("service-port").value = String(payload.port);
  $("service-runtime-state").textContent = `当前监听端口：${payload.active_port}`;
  const changed = payload.port !== payload.active_port;
  $("service-port-state").textContent = changed
    ? `当前运行 ${payload.active_port}，保存值 ${payload.port} 将在重启后生效。`
    : `当前服务正在监听 ${payload.active_port}。`;
  serviceSettingsLoaded = true;
}

async function refreshServiceSettings() {
  if (!canManageService || serviceSettingsBusy || endpointSettingsBusy) return;
  setServiceSettingsBusy(true);
  setServiceSettingsStatus("正在读取服务配置...");
  try {
    applyServiceSettings(await api("/admin/settings/service"));
    applyEndpointSettings(await api("/admin/settings/endpoint"));
    serviceSettingsUpdatedAt = Date.now();
    setServiceSettingsStatus("服务配置已加载");
  } catch (error) {
    setServiceSettingsStatus(`读取失败: ${error.message}`, true);
  } finally {
    setServiceSettingsBusy(false);
  }
}

async function saveServiceSettings() {
  if (serviceSettingsBusy || !serviceSettingsLoaded) return;
  const port = $("service-port").valueAsNumber;
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    setServiceSettingsStatus("端口必须是 1 到 65535 之间的整数", true);
    return;
  }
  setServiceSettingsBusy(true);
  setServiceSettingsStatus("正在保存端口...");
  try {
    const result = await api("/admin/settings/service", "PUT", true, { port });
    applyServiceSettings(result);
    serviceSettingsUpdatedAt = Date.now();
    setServiceSettingsStatus(result.restart_required
      ? "端口已保存，下次启动或重启服务后生效。"
      : "端口已保存，当前服务已使用此端口。");
    showToast(result.restart_required ? "端口已保存，重启后生效" : "端口设置已保存");
  } catch (error) {
    setServiceSettingsStatus(`保存失败: ${error.message}`, true);
  } finally {
    setServiceSettingsBusy(false);
  }
}

function serviceRestartDestination() {
  const destination = new URL(window.location.href);
  const currentPort = Number(destination.port || (destination.protocol === "https:" ? 443 : 80));
  if (currentPort === serviceSettingsData?.active_port && serviceSettingsData?.port) {
    destination.port = String(serviceSettingsData.port);
  }
  destination.pathname = "/dashboard";
  destination.search = "";
  destination.hash = "";
  return destination.href;
}

async function restartGatewayService() {
  if (serviceSettingsBusy || !serviceSettingsLoaded) return;
  const portWillChange = serviceSettingsData.port !== serviceSettingsData.active_port;
  let destination = "";
  try {
    if (portWillChange) {
      destination = serviceRestartDestination();
    }
    setServiceSettingsBusy(true);
    setServiceSettingsStatus("正在请求服务重启...");
    await api("/admin/service/restart", "POST");
    setServiceSettingsStatus(portWillChange
      ? "重启已触发，将在新端口重新打开管理页。"
      : "服务正在重启，等待重新上线...");
    showToast("服务重启已触发");
    if (portWillChange) {
      window.setTimeout(() => window.location.assign(destination), 10000);
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    for (let attempt = 0; attempt < 45; attempt += 1) {
      try {
        const response = await fetch("/healthz", { cache: "no-store" });
        if (response.ok) {
          setServiceSettingsBusy(false);
          await refreshServiceSettings();
          setServiceSettingsStatus("服务已恢复");
          showToast("服务已恢复");
          return;
        }
      } catch { }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    setServiceSettingsStatus("尚未检测到服务恢复，请在服务管理器检查进程。", true);
  } catch (error) {
    setServiceSettingsStatus(`重启失败: ${error.message}`, true);
  } finally {
    if (!portWillChange || !serviceSettingsBusy) setServiceSettingsBusy(false);
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
    const storage = statusData.statistics_storage;
    const warning = $("statisticsStorageWarning");
    if (warning) {
      warning.hidden = !storage || storage.ok;
      warning.textContent = storage && !storage.ok ? "统计保存异常" : "";
      warning.title = storage?.message || "";
    }
    const storageStatus = $("eventRetentionStatus");
    if (storageStatus && storage) {
      storageStatus.textContent = storage.message;
      storageStatus.classList.toggle("text-danger", !storage.ok);
    }
    render(force);
    updateProtectionDisplays();
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
  const labels = { toggle: "站点状态已更新", probe: "连接检查已完成（不代表模型能生成，也不会解除熔断）", reset: "额度探测已完成" };
  try {
    const encodedName = encodeURIComponent(stationName);
    const result = await api(`/admin/station/${encodedName}/${action}`, "POST");
    let message = labels[action] || "操作已完成";
    let isError = false;
    if (action === "generation-probe") {
      message = result.skipped
        ? `${stationName} 未发送生成请求：${result.error || "缺少可用探测模型"}`
        : result.ok
          ? `${stationName} · ${result.model} 生成验证成功，已恢复路由`
          : `${stationName} 生成验证失败：${result.error || "上游未返回成功"}`;
      isError = !result.ok;
    } else if (action === "probe") {
      isError = result.ok === false;
      message = result.ok
        ? `${stationName} 连接可达；生成能力和额度需单独验证`
        : `${stationName} ${result.ok === false ? "连接检查未通过" : "本次检查未完成"}：${result.error || "请稍后再试"}`;
    } else if (action === "reset") {
      message = result.recovered || !result.quota_exhausted
        ? `${stationName} 额度已恢复`
        : `${stationName} 尚未确认额度恢复：${result.probe_error || "请检查上游额度或测试结果"}`;
      isError = Boolean(result.quota_exhausted);
    }
    showToast(message, isError);
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
$("access-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveAccessSettings();
});
$("access-reload").addEventListener("click", refreshAccessSettings);
$("service-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveServiceSettings();
});
$("service-reload").addEventListener("click", refreshServiceSettings);
$("service-restart").addEventListener("click", restartGatewayService);
$("retry-unlimited-duration").addEventListener("change", syncRetryLimitControls);

$("retry-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveRetrySettings();
});

$("retry-reload").addEventListener("click", () => {
  refreshRetrySettings({ notify: true });
});
$("probe-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveProbeSettings();
});
$("probe-reload").addEventListener("click", refreshProbeSettings);

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
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const tabs = [...document.querySelectorAll('.primary-tabs [data-tab]:not([hidden])')].map(button => button.dataset.tab);
  const direction = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1;
  const nextTab = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs.at(-1)
    : tabs[(tabs.indexOf(currentTab) + direction + tabs.length) % tabs.length];
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

let stationModelsBusy = false;
let generationTestBusy = false;

function stationProbeModels() {
  const station = STATIONS.find(item => item.name === editingStation);
  const candidates = station?.probe_model_candidates || station?.detected_models || [];
  const manual = $("sf-models").value.split(/[,，]/).map(value => value.trim()).filter(Boolean);
  const models = [...new Set([...($("sf-probe-models").checked ? candidates : []), ...manual])];
  return $("sf-provider").value === 'opencode' ? models.filter(model => station?.model_api_paths?.[model]?.length) : models;
}

function syncModelSelection(selected, model) {
  $("sf-model-list").querySelectorAll("[data-probe-model]").forEach(button => {
    const active = button.dataset.probeModel === (selected || model);
    button.classList.toggle("is-selected", active);
    button.setAttribute("aria-pressed", String(active));
    if (!button.disabled) button.title = button.dataset.probeModel + (active ? (selected ? " · 当前探测模型" : " · 自动选择的第一个模型") : " · 点击设为探测模型");
  });
}

function renderProbeModels(selected = $("sf-probe-chat-model").value) {
  const station = STATIONS.find(item => item.name === editingStation);
  const manual = $("sf-models").value.split(/[,，]/).map(value => value.trim()).filter(Boolean);
  const models = stationProbeModels();
  const go = $("sf-provider").value === 'opencode' && $("sf-opencode-plan").value === 'go';
  const select = $("sf-probe-chat-model");
  const options = [`<option value="">自动 · ${models[0] ? `第一个：${escapeHtml(models[0])}` : "等待模型列表"}</option>`,
    ...models.map(model => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`)];
  if (selected && !models.includes(selected)) options.push(`<option value="${escapeHtml(selected)}">${escapeHtml(selected)}（未在当前列表中）</option>`);
  select.innerHTML = options.join("");
  select.value = selected;
  const model = selected || models[0] || "";
  $("sf-probe-model-auto").checked = !selected;
  $("sf-probe-model-auto").disabled = !models.length && !selected;
  $("sf-probe-mode-label").textContent = selected ? "手动指定" : ($("sf-provider").value === 'opencode' ? "自动 · 第一个可接入模型" : "自动 · 上游返回的第一个");
  $("sf-probe-current").textContent = model || "等待模型列表";
  renderEffectiveProbePolicy();
  $("sf-probe-save-state").textContent = selected !== (station?.probe_chat_model || "")
    ? "待保存 · 仅修改当前站点" : (station ? "当前站点已保存" : "保存站点后生效");
  syncModelSelection(selected, model);
  const unsaved = station && ($("sf-base-url").value.trim() !== station.base_url
    || $("sf-api-key").value.trim() || selected !== (station.probe_chat_model || "")
    || $("sf-provider").value !== (station.provider || 'openai')
    || $("sf-opencode-plan").value !== (station.opencode_plan || 'balance')
    || $("sf-probe-models").checked !== station.probe_models
    || JSON.stringify(manual) !== JSON.stringify(station.models || [])
    || $("sf-enabled").checked !== station.enabled);
  $("sf-generation-test").disabled = go || generationTestBusy || !station?.enabled || !model || unsaved;
  $("sf-generation-test").textContent = generationTestBusy ? "正在测试…" : "生成测试一次";
  const result = station?.generation_probe;
  $("sf-generation-result").textContent = go ? "Go 不发送通用 ping 探测；请使用智商测试或正常编程请求。"
    : unsaved ? "请先保存模型或连接修改，再测试。"
    : !station ? "保存并启用站点后可测试。"
      : result?.last_at ? `最近生成测试：${result.ok ? "成功" : "未通过"} · ${formatAge(result.last_at)}${result.error ? ` · ${result.error}` : ""}`
        : "仅执行一次，可能按上游价格计费。";
}

function updatePriorityMode() {
  const auto = $("sf-priority-auto").checked;
  $("sf-priority").disabled = auto;
  $("sf-priority-note").textContent = auto
    ? "自动：按健康评分调度，不加手动优先级分；同分段按权重与延迟分流。"
    : "手动：1 / 2 / 3 / 4 / 5 分别加 15 / 12 / 9 / 6 / 3 分，6 及以上不加分；并非硬排序。";
}

function renderStationModels() {
  $("sf-visual-test").disabled = !editingStation;
  renderProbeModels();
  const station = STATIONS.find(item => item.name === editingStation);
  const discovery = station?.model_discovery || {};
  const changedConnection = station && ($("sf-base-url").value.trim() !== station.base_url
    || $("sf-api-key").value.trim() || $("sf-provider").value !== (station.provider || 'openai')
    || $("sf-opencode-plan").value !== (station.opencode_plan || 'balance'));
  const labels = { pending: "等待发现", loading: "正在读取模型列表", ready: "已发现", off: "自动发现已关闭", error: "读取失败", rate_limited: "限流冷却中", auth_error: "鉴权失败", unsupported: "接口不支持", invalid_response: "返回格式不支持" };
  let status = !station ? "保存并启用站点后自动发现模型。"
    : `${labels[discovery.status] || "等待发现"}${discovery.updated_at ? ` · 更新于 ${formatAge(discovery.updated_at)}` : ""}${discovery.error ? ` · ${discovery.error}` : ""}`;
  if (station && !station.enabled) status += " · 站点已停用，不发送发现请求";
  if (changedConnection) status = "连接或套餐尚未保存；下方仍为原配置的缓存，保存后重新发现。";
  const wait = Math.max(0, Math.ceil((discovery.checked_at || 0) + 60 - Date.now() / 1000));
  const limited = (discovery.status === "rate_limited" && discovery.next_refresh_at > Date.now() / 1000)
    || discovery.retry_after_until > Date.now() / 1000;
  const button = $("sf-models-refresh");
  button.disabled = stationModelsBusy || !station?.enabled || !station?.probe_models || changedConnection || !$("sf-probe-models").checked || wait > 0 || limited;
  button.textContent = stationModelsBusy ? "读取中…" : (wait > 0 ? `${wait}s 后刷新` : (limited ? "冷却中" : "刷新列表"));
  $("sf-models-status").textContent = status;
  const query = $("sf-model-search").value.trim().toLowerCase();
  const detectedSet = new Set(station?.detected_models || []);
  const detected = [...new Set([...(station?.probe_model_candidates || []), ...(station?.detected_models || [])])].filter(model => detectedSet.has(model));
  const allModels = [...new Set([...detected, ...stationProbeModels()])];
  const models = allModels.filter(model => model.toLowerCase().includes(query));
  $("sf-model-count").textContent = String(allModels.length);
  const list = $("sf-model-list");
  list.innerHTML = models.map(model => {
    const reason = station?.model_unavailable?.[model];
    const paths = station?.model_api_paths?.[model];
    const detail = reason || (paths?.length ? paths.map(p => ({'/responses':'Responses','/chat/completions':'Chat Completions','/messages':'Anthropic Messages','/generateContent':'Gemini GenerateContent'}[p] || p)).join(' / ') : '');
    return `<button type="button" class="catalog-model" data-probe-model="${escapeHtml(model)}" aria-pressed="false" ${reason ? 'disabled' : ''} title="${escapeHtml(detail)}"><span>${escapeHtml(model)}</span>${detail ? `<small>${escapeHtml(detail)}</small>` : ''}</button>`;
  }).join("")
    || `<span class="catalog-empty">${query ? "没有匹配模型" : (discovery.status === "ready" ? "上游返回空列表" : "暂无模型列表")}</span>`;
  syncModelSelection($("sf-probe-chat-model").value, stationProbeModels()[0]);
  updateModelRouting();
}

function routeRules() {
  return $("sf-route-models").value.split(/[,，\n]/).map(value => value.trim()).filter(Boolean);
}
function updateModelRouting() {
  const rules = routeRules();
  const station = STATIONS.find(item => item.name === editingStation);
  const known = [...new Set([...(station?.detected_models || []), ...$("sf-models").value.split(/[,，]/).map(v => v.trim()).filter(Boolean)])];
  const matches = model => !rules.length || rules.some(rule => rule.endsWith('*') ? model.startsWith(rule.slice(0, -1)) : model === rule);
  const eligible = known.filter(model => matches(model) && ($("sf-provider").value === 'opencode'
    ? station?.model_api_paths?.[model]?.length : true));
  const passthrough = $("sf-model-routing").value === 'passthrough';
  $("sf-routing-note").textContent = (passthrough
    ? '允许范围内未列出的模型，由上游确认支持情况。'
    : `只接收目录或手动补充的精确模型 ID；当前 ${eligible.length} / ${known.length} 个模型符合范围。`)
    + ' 多项用逗号分隔，支持末尾 *。故障切换保持原模型，不替换型号。';
}
function updateProvider(changed = false) {
  const opencode = $("sf-provider").value === 'opencode';
  const native = ['anthropic', 'gemini'].includes($("sf-provider").value);
  $("editor-connection").classList.toggle('is-opencode', opencode);
  $("editor-connection").classList.toggle('has-provider-note', native);
  if (changed && opencode) $("sf-opencode-plan").value = 'go';
  const go = opencode && $("sf-opencode-plan").value === 'go';
  $("sf-provider").querySelector('[value="opencode"]').textContent = opencode && !go ? 'OpenCode · 按量余额' : 'OpenCode Go';
  $("sf-provider-note").hidden = !opencode && !native;
  $("sf-opencode-plan-field").hidden = !opencode;
  $("sf-billing").disabled = opencode;
  $("sf-probe-chat").disabled = go;
  if (go) $("sf-probe-chat").checked = false;
  $("sf-provider-note").textContent = go
    ? 'Go 订阅使用独立额度，额外付费余额 $0 不代表订阅不可用。需保留编程对话的稳定会话 ID；不发送通用 ping，网关不切换到余额入口。若要避免上游超额扣费，请在 OpenCode 控制台关闭 Extra Usage。'
    : '按量余额使用余额接口，与 Go 订阅额度独立。模型按官方协议接入，免费专属模型及暂不支持的协议会标明。';
  if (native) {
    const anthropic = $("sf-provider").value === 'anthropic';
    $("sf-provider-note").textContent = anthropic
      ? '原生 Messages 接口，保留工具调用、图像、缓存与思考参数。客户端使用网关 /v1/messages；上游通过 API Key 鉴权。其他入口仅匹配对应协议的站点。'
      : '原生 Gemini 接口，保留 functionCall、thoughtSignature 与多模态内容。客户端使用 /v1beta/models/{模型}:generateContent，或现有 /v1/models/{模型}:generateContent；流式请求使用 :streamGenerateContent?alt=sse。';
    const presetUrls = ['https://api.anthropic.com/v1', 'https://generativelanguage.googleapis.com/v1beta', 'https://opencode.ai/inference/go/openai/v1', 'https://opencode.ai/inference/openai/v1'];
    if (changed && (!$("sf-base-url").value || presetUrls.includes($("sf-base-url").value))) $("sf-base-url").value = anthropic
      ? 'https://api.anthropic.com/v1' : 'https://generativelanguage.googleapis.com/v1beta';
  }
  $("sf-base-url").readOnly = opencode;
  $("sf-model-routing").disabled = opencode;
  $("sf-probe-models").disabled = opencode;
  $("sf-models").disabled = opencode;
  $("sf-models").closest('details').hidden = opencode;
  if (opencode) {
    $("sf-base-url").value = `https://opencode.ai/inference/${go ? 'go/' : ''}openai/v1`;
    $("sf-billing").value = go ? 'subscription' : 'balance';
    $("sf-model-routing").value = 'catalog';
    $("sf-probe-models").checked = true;
    if (changed) {
      if (!$("sf-name").value) $("sf-name").value = 'OpenCode Go';
      $("sf-probe-chat").checked = false;
      $("sf-probe-interval").value = '3600';
    }
  }
  updateModelRouting();
}
$("sf-provider").addEventListener('change', () => { updateProvider(true); renderStationModels(); });
$("sf-opencode-plan").addEventListener('change', () => { updateProvider(); renderStationModels(); });
$("sf-model-routing").addEventListener('change', updateModelRouting);
$("sf-route-models").addEventListener('input', updateModelRouting);

$("sf-priority-auto").addEventListener("change", updatePriorityMode);
for (const id of ["sf-model-search", "sf-base-url", "sf-api-key", "sf-probe-models", "sf-models", "sf-enabled"]) {
  $(id).addEventListener("input", renderStationModels);
}
$("sf-probe-chat-model").addEventListener("change", () => renderProbeModels());
$("sf-probe-model-auto").addEventListener("change", () => {
  renderProbeModels($("sf-probe-model-auto").checked ? "" : (stationProbeModels()[0] || ""));
});
$("sf-model-list").addEventListener("click", event => {
  const button = event.target.closest("[data-probe-model]");
  if (button) renderProbeModels(button.dataset.probeModel);
});
function renderEffectiveProbePolicy() {
  const policy = probeSettingsData;
  const model = $("sf-probe-chat-model").value || stationProbeModels()[0];
  let text = !policy ? "账号策略尚未读取，暂无法判断是否参与后台测试。"
    : !$("sf-enabled").checked ? "本站已停用，不执行后台测试。"
    : !policy.generation_probe_enabled ? "账号总开关已关闭，不执行后台生成测试；仍可手动测试一次。"
    : !$("sf-probe-chat").checked ? "本站未参与后台生成测试；仍可手动测试一次。"
    : !model ? "尚无可用测试模型，后台测试会跳过。"
    : `本站参与后台测试，最小间隔 ${policy.generation_probe_interval} 秒。近期有业务调用、限流或故障冷却时跳过。`;
  const station = STATIONS.find(item => item.name === editingStation);
  if ($("sf-provider").value === 'opencode' && $("sf-opencode-plan").value === 'go') text = "Go 订阅不执行通用 ping 生成测试；模型目录仍会按设定间隔刷新。";
  if (station && ($("sf-probe-chat").checked !== station.probe_chat || $("sf-enabled").checked !== station.enabled)) text = "待保存 · " + text;
  $("sf-effective-policy").textContent = text;
  const interval = Math.max(300, Number($("sf-probe-interval").value) || Number(statusData?.gateway?.probe_interval) || 300);
  $("sf-directory-effective").textContent = $("sf-probe-models").checked ? `目录检查间隔至少 ${interval} 秒，仅读取元数据；限流时延后。` : "自动发现已关闭；不自动读取模型目录。";
}
$("sf-probe-chat").addEventListener("change", renderEffectiveProbePolicy);
$("sf-probe-interval").addEventListener("input", renderEffectiveProbePolicy);
$("sf-account-policy").addEventListener("click", () => setSectionTab("stations", "probe"));
$("sf-generation-test").addEventListener("click", async () => {
  if (!editingStation || generationTestBusy || $("sf-generation-test").disabled) return;
  const name = editingStation;
  let message = "";
  generationTestBusy = true;
  renderProbeModels();
  try {
    const result = await api(`/admin/station/${encodeURIComponent(name)}/generation-probe`, "POST");
    await refreshStations({ force: true });
    await refresh(true);
    message = result.skipped ? `未发送生成请求：${result.error}`
      : result.ok ? `${name} · ${result.model} 生成成功${result.latency_ms != null ? `，${result.latency_ms} ms` : ""}`
        : `生成测试失败：${result.error || "上游未返回成功"}`;
    showToast(message, !result.ok);
  } catch (error) {
    message = `生成测试失败：${error.message}`;
    showToast(message, true);
  } finally {
    generationTestBusy = false;
    $("sf-generation-test").textContent = "生成测试一次";
    $("sf-generation-test").disabled = false;
    renderProbeModels();
    if (editingStation === name) $("sf-generation-result").textContent = message;
  }
});
$("sf-models-refresh").addEventListener("click", async () => {
  if (!editingStation || stationModelsBusy) return;
  const name = editingStation;
  stationModelsBusy = true;
  renderStationModels();
  try {
    await api(`/admin/station/${encodeURIComponent(name)}/models/refresh`, "POST");
    await refreshStations({ force: true });
    await refresh(true);
  } catch (error) {
    showToast(`模型列表读取失败: ${error.message}`, true);
  } finally {
    stationModelsBusy = false;
    renderStationModels();
  }
});

function renderStations() {
  renderStationModels();

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
    const models = [...new Set([...(station.models || []), ...(station.detected_models || [])])];
    const modelsLabel = models.length ? models.join(", ") : (station.accepts_any_model ? '允许未列出的模型' : '模型待配置 / 发现');
    const routed = station.effective_models?.length ?? models.length;
    const scopeLabel = station.accepts_any_model ? (station.route_models?.length ? '按范围透传' : '允许未列出模型') : `${routed} 可路由 / ${models.length} 已知`;
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
          ${station.quota_exhausted ? '<span class="station-warning">上游额度不可用</span>' : ""}</td>
        <td class="station-url-cell" title="${escapeHtml(station.base_url || "")}">${escapeHtml(station.base_url || "-")}</td>
        <td class="station-models-cell"><button type="button" class="catalog-link" data-manage-action="models" data-station="${escapeHtml(name)}" title="${escapeHtml(modelsLabel)}">${escapeHtml(scopeLabel)}</button></td>
        <td title="${station.priority_auto ? "按健康评分自动调度，无手动加分" : "数字越小加分越高，并非硬排序"}">${station.priority_auto ? `自动${station.rank ? ` · #${station.rank}` : ""}` : formatCount(station.priority)}</td>
        <td class="muted">${station.billing === "subscription" ? "订阅" : "余额"}</td>
        <td>${protectionMarkup(station)}</td>
        <td><div class="station-manage-actions">
          <button class="button station-action" type="button" data-manage-action="edit" data-station="${escapeHtml(name)}"${disabled}>编辑</button>
          <button class="button station-action" type="button" data-manage-action="toggle" data-station="${escapeHtml(name)}"${disabled}${busy(toggleBusy)}>${toggleBusy ? "处理中..." : (station.enabled ? "停用" : "启用")}</button>
          <button class="button station-action" type="button" data-manage-action="probe" data-station="${escapeHtml(name)}"${disabled}${busy(probeBusy)}>${probeBusy ? "检查中..." : "连接检查"}</button>
          ${station.quota_exhausted ? `<button class="button station-action" type="button" data-manage-action="reset" data-station="${escapeHtml(name)}"${disabled}${busy(resetBusy)}>${resetBusy ? "探测中..." : "立即探测"}</button>` : ""}
          <button class="button station-action is-destructive" type="button" data-manage-action="delete" data-station="${escapeHtml(name)}"${disabled}${busy(deleteBusy)}>${deleteBusy ? "删除中..." : "删除"}</button>
        </div></td>
      </tr>`;
  }).join("") || '<tr><td colspan="7" class="empty-cell">暂无站点，点击“添加站点”开始配置</td></tr>';
  fitViewportContent();
  document.dispatchEvent(new Event("modelharbor:stations-rendered"));
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
  $("sf-provider").value = station.provider || 'openai';
  $("sf-opencode-plan").value = station.opencode_plan || 'balance';
  $("sf-model-routing").value = station.model_routing || (station.accepts_any_model ? 'passthrough' : 'catalog');
  $("sf-route-models").value = (station.route_models || []).join(', ');
  $("sf-priority").value = station.priority;
  $("sf-priority-auto").checked = Boolean(station.priority_auto);
  updatePriorityMode();
  $("sf-model-search").value = "";
  $("sf-billing").value = station.billing;
  $("sf-probe-chat").checked = Boolean(station.probe_chat);
  renderProbeModels(station.probe_chat_model || "");
  $("sf-probe-interval").value = station.probe_interval ?? "";
  $("sf-probe-models").checked = Boolean(station.probe_models);
  $("sf-enabled").checked = station.enabled;
  updateProvider();
  renderStationModels();
  $("sf-heading").textContent = "编辑站点: " + station.name;
  $("sf-submit").textContent = "保存修改";
  $("sf-cancel").hidden = false;
  setStationFormStatus(`正在编辑 ${station.name}，API Key 留空将保持不变`);
  setSectionTab("stations", "editor");
  setSectionTab("editor", "connection");
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
  $("sf-provider").value = 'openai';
  $("sf-opencode-plan").value = 'go';
  $("sf-model-routing").value = 'catalog';
  $("sf-route-models").value = '';
  $("sf-priority").value = "5";
  $("sf-priority-auto").checked = true;
  updatePriorityMode();
  $("sf-model-search").value = "";
  $("sf-billing").value = "balance";
  $("sf-probe-chat").checked = false;
  $("sf-probe-chat-model").value = "";
  $("sf-probe-interval").value = "";
  $("sf-probe-models").checked = true;
  $("sf-enabled").checked = true;
  updateProvider();
  renderStationModels();
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
    provider: $("sf-provider").value,
    opencode_plan: $("sf-opencode-plan").value,
    model_routing: $("sf-model-routing").value,
    route_models: routeRules(),
    priority: parseInt($("sf-priority").value) || 5,
    priority_auto: $("sf-priority-auto").checked,
    billing: $("sf-billing").value,
    probe_chat: $("sf-probe-chat").checked,
    probe_chat_model: $("sf-probe-chat-model").value.trim(),
    probe_interval: $("sf-probe-interval").value ? Number($("sf-probe-interval").value) : null,
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
    if (listRefreshed) setSectionTab("stations", "list");
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
        : `${stationName} 尚未确认额度恢复：${result.probe_error || "请检查上游额度或测试结果"}`;
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
  if (action === "edit" || action === "models") {
    const station = STATIONS.find((item) => item.name === stationName);
    if (station) fillStationForm(station);
    if (station && action === "models") setSectionTab("editor", "models");
    return;
  }
  if (action === "delete"
      && !window.confirm(`确定删除站点 ${stationName}？该操作会立即停止后续路由。`)) return;
  if (action === "reset"
      && !window.confirm(`“立即探测”会发送一次生成请求，可能按上游价格计费。只需查看模型列表请使用“模型”页签。仍要继续吗？`)) return;
  runManagedStationAction(action, stationName);
});

$("stations-add").addEventListener("click", () => {
  if (stationSubmitting) return;
  resetStationForm();
  setSectionTab("stations", "editor");
  setSectionTab("editor", "connection");
  $("sf-name").focus({ preventScroll: true });
});

$("stations-refresh").addEventListener("click", () => {
  refreshStations({ notifyError: true });
});

$("sf-cancel").addEventListener("click", () => { resetStationForm(); setSectionTab("stations", "list"); });

$("show-details").addEventListener("change", () => {
  $("monitor-wrap").classList.toggle("show-details", $("show-details").checked);
  scheduleViewportLayout();
});
if (window.matchMedia) {
  const mobileNavigation = window.matchMedia("(max-width: 620px)");
  const updateNavigationOrientation = () => document.querySelector(".primary-tabs")
    .setAttribute("aria-orientation", mobileNavigation.matches ? "horizontal" : "vertical");
  mobileNavigation.addEventListener?.("change", updateNavigationOrientation);
  updateNavigationOrientation();
}
$("station-search").addEventListener("input", () => { viewportPages.monitor = 1; render(true); });
function setStationScope(scope) {
  if (!["enabled", "disabled"].includes(scope)) return;
  stationScope = scope;
  viewportPages.monitor = 1;
  $("stationScopeTabs").querySelectorAll("[data-station-scope]").forEach(button => {
    const selected = button.dataset.stationScope === scope;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  $("monitorViewport").setAttribute("aria-labelledby", "station-scope-" + scope);
  $("stationHealthFilter").hidden = scope === "disabled";
  render(true);
}
$("stationScopeTabs").addEventListener("click", event => {
  const button = event.target.closest("[data-station-scope]");
  if (button) setStationScope(button.dataset.stationScope);
});
$("stationScopeTabs").addEventListener("keydown", event => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const scope = event.key === "Home" ? "enabled" : event.key === "End" ? "disabled"
    : stationScope === "enabled" ? "disabled" : "enabled";
  setStationScope(scope);
  $("station-scope-" + scope).focus();
});
$("station-filter").addEventListener("change", () => { viewportPages.monitor = 1; render(true); });
$("refreshDashboard").addEventListener("click", async () => {
  const button = $("refreshDashboard");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    if (currentTab === "stats") await refreshStats();
    else if (currentTab === "events") await refreshEvents();
    else if (currentTab === "stations") {
      await refreshStations({ notifyError: true });
      await refreshStationPolicy(true);
    }
    else if (currentTab === "access") await refreshAccessSettings();
    else if (currentTab === "service") await refreshServiceSettings();
    else await refresh(true);
  } finally {
    button.disabled = false;
    button.setAttribute("aria-busy", "false");
  }
});
function applyEndpointSettings(data) {
  endpointData = data;
  $("endpoint-public-url").value = data.public_url || "";
  $("endpoint-preview").textContent = data.endpoint || "请配置客户端可访问的 URL";
  const labels = { configured: "已指定公开地址；协议、端口与路径按保存值使用", request: "自动使用当前访问地址", local_interface: "自动使用本机局域网地址与当前监听端口", needs_configuration: "未能确定唯一的局域网地址，请填写公开访问 URL" };
  $("endpoint-status").textContent = labels[data.source] || "";
  if (data.candidates?.length > 1) $("endpoint-status").textContent += `。候选：${data.candidates.join("、")}`;
}
$("endpoint-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!canManageService || serviceSettingsBusy || endpointSettingsBusy) return;
  const value = $("endpoint-public-url").value.trim();
  endpointSettingsBusy = true;
  setServiceSettingsBusy(true);
  try {
    applyEndpointSettings(await api("/admin/settings/endpoint", "PUT", true, { public_url: value }));
    showToast("对外接入地址已保存，立即生效");
  } catch (error) {
    $("endpoint-status").textContent = `保存失败：${error.message}`;
  } finally { endpointSettingsBusy = false; setServiceSettingsBusy(false); }
});
function copyEndpointFallback(endpoint) {
  const field = document.createElement("textarea");
  field.className = "clipboard-helper";
  field.value = endpoint;
  document.body.append(field);
  field.select();
  try { if (!document.execCommand("copy")) throw new Error("浏览器不允许写入剪贴板"); }
  finally { field.remove(); }
}
$("copyEndpoint").addEventListener("click", async () => {
  if ($("copyEndpoint").disabled) return;
  $("copyEndpoint").disabled = true;
  let endpoint = "";
  try {
    const data = await api("/admin/settings/endpoint");
    endpointData = data;
    endpoint = data.endpoint || "";
    if (!endpoint) throw new Error("请在部署机器的服务设置中配置对外接入地址");
    const parsed = new URL(endpoint);
    if (!["http:", "https:"].includes(parsed.protocol) || !parsed.pathname.endsWith("/v1")) throw new Error("接入地址格式无效");
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      try { await navigator.clipboard.writeText(endpoint); } catch { copyEndpointFallback(endpoint); }
    } else {
      copyEndpointFallback(endpoint);
    }
    showToast(`已复制：${endpoint}`);
  } catch (error) {
    showToast(endpoint ? `接入地址：${endpoint}，请手动复制` : error.message, true);
  } finally { $("copyEndpoint").disabled = false; $("copyEndpoint").focus({ preventScroll: true }); }
});
initializeViewportLayout();
applyTab();
applyView();
if (currentTab === "stations") refreshStations({ notifyError: true });
if (legacyStationSection) {
  localStorage.setItem("zg_tab", "stations");
  setSectionTab("stations", legacyStationSection);
}
if (currentTab === "access") refreshAccessSettings();
refresh();
