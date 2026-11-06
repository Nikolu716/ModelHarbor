"use strict";
(() => {
  const el = id => document.getElementById(id);
  let selected = "", profile = "", data = null, busy = false, epoch = 0;
  let pollTimer = 0, pollFailures = 0, copyBusy = false, copiedCode = "", copiedUntil = 0;
  let identity = "", confirming = false, operation = "", copyError = "";
  let retryBusy = false, retryAfter = 0, retryStatus = "";
  const selectedStation = () => (statusData?.stations || []).find(item => item.name === selected)
    || (typeof STATIONS === "undefined" ? [] : STATIONS).find(item => item.name === selected);
  const native = () => ["codex_relay", "codex_cli"].includes(selectedStation()?.chatgpt_mode);
  const needsRecovery = station => Boolean(station?.quota_exhausted
    || ["breaker", "recovering"].includes(station?.protection?.kind)
    || ["down", "cooling"].includes(station?.status));
  const windows = value => (Array.isArray(value?.windows) ? value.windows : []).filter(item => item && typeof item === "object");
  const number = value => typeof value === "number" && Number.isFinite(value);
  const percentage = value => number(value) ? new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 1}).format(value) + "%" : "—";
  const tone = value => value >= 100 ? "is-exhausted" : value >= 80 ? "is-warning" : "";
  const progress = value => number(value) ? Math.min(100, Math.max(0, value)) : 0;
  function label(item) {
    const mins = item.duration_mins;
    if (mins === 10080) return "7 天";
    if (number(mins) && mins > 0) return mins % 60 === 0 ? mins / 60 + " 小时" : mins + " 分钟";
    return item.window === "secondary" ? "长时" : "短时";
  }
  function markup(station) {
    const usage = station.chatgpt_usage, items = windows(usage);
    const rows = items.slice(0, 2).map(item => {
      const name = label(item), value = item.used_percent;
      return '<span class="go-quota-item ' + tone(value) + '"><small title="' + escapeHtml(name) + '">' + escapeHtml(name) +
        '</small><progress max="100" value="' + progress(value) + '" aria-label="' + escapeHtml(name + "已用 " + percentage(value)) +
        '"></progress><b>' + percentage(value) + '</b></span>';
    }).join("");
    const empty = '<span class="subscription-quota-empty">' + (station.chatgpt?.connected ? "连接用量" : "等待授权") + '</span>';
    return '<button type="button" class="go-quota-summary chatgpt-quota-summary" data-chatgpt-usage="' + escapeHtml(station.name) +
      '" aria-label="查看 ' + escapeHtml(station.name) + ' 的订阅用量参考"><span class="go-quota-caption"><span>' +
      (station.chatgpt_mode === "codex_relay" ? 'Codex · 用量' : station.chatgpt_mode === "codex_cli" ? 'Codex CLI · 用量' : items.length && items.every(item => /^codex$/i.test(item.name)) ? 'Codex 用量参考' : '订阅用量参考') +
      (usage?.stale ? ' · 缓存' : '') + (items.length > 2 ? ' · +' + (items.length - 2) : '') +
      '</span><svg aria-hidden="true" viewBox="0 0 16 16" width="11" height="11"><path d="m6 4 4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span class="go-quota-values">' + (rows || empty) + '</span></button>';
  }
  function update(profileId, usage) {
    for (const station of [...(typeof STATIONS === "undefined" ? [] : STATIONS), ...(statusData?.stations || [])]) {
      if (station.chatgpt_account === profileId) station.chatgpt_usage = usage;
    }
    if (profile === profileId) { data = usage; renderDetail(); }
  }
  function renderCredits() {
    const credits = data?.reset_credits, count = credits?.availableCount ?? credits?.available_count;
    el("chatgptUsageCreditCount").textContent = number(count) ? count + " 张可用" : "上游未提供";
    const available = (Array.isArray(credits?.credits) ? credits.credits : []).filter(item => item && item.status === "available");
    if (!available.length && number(count) && count > 0) available.push({title: "下一张可用重置卡"});
    el("chatgptUsageCredits").replaceChildren(...available.map(item => {
      const row = document.createElement("div"), text = document.createElement("span"), button = document.createElement("button");
      const expiresAt = item.expiresAt ?? item.expires_at;
      const expires = expiresAt ? new Date(number(expiresAt) ? expiresAt * 1000 : expiresAt) : null;
      text.textContent = (item.title || "速率限制重置卡") + (expires && !Number.isNaN(expires.valueOf()) ? " · 有效期至 " + expires.toLocaleString() : "");
      const id = typeof item.id === "string" ? item.id : typeof item.creditId === "string" ? item.creditId : null;
      button.type = "button"; button.className = "button secondary"; button.textContent = "使用"; button.disabled = busy;
      button.addEventListener("click", async () => {
        if (busy) return;
        busy = true; renderDetail();
        try { await window.ModelHarborChatGPT.consume(id, item.title || "速率限制重置卡", profile); }
        finally { busy = false; renderDetail(); }
      });
      row.append(text, button); return row;
    }));
  }
  const pending = () => data?.status === "pending";
  const expired = () => pending() && number(data?.expires_at) && data.expires_at * 1000 <= Date.now();
  function renderDevice() {
    const waiting = pending(), elapsed = expired(), code = waiting ? data?.user_code || "" : "";
    el("chatgptUsageDevice").hidden = !waiting;
    el("chatgptUsageDevice").classList.toggle("is-expired", elapsed);
    el("chatgptDeviceTitle").textContent = elapsed ? "验证码已过期" : native() ? "重新授权 Codex 账号" : "连接 Codex 用量";
    if (el("chatgptUsageCode").textContent !== code) {
      el("chatgptUsageCode").textContent = code;
      copiedCode = ""; copiedUntil = 0; copyError = "";
    }
    const copied = code && copiedCode === code && Date.now() < copiedUntil;
    el("chatgptUsageCopy").disabled = !code || elapsed || copyBusy;
    el("chatgptUsageCopy").classList.toggle("is-copied", Boolean(copied));
    el("chatgptUsageCopy").querySelector("span").textContent = copyBusy ? "复制中…" : copied ? "已复制" : "复制验证码";
    el("chatgptUsageCopyFeedback").textContent = copyBusy ? "正在复制验证码…" : copyError || (copied ? "验证码已复制，可前往官方页面粘贴。" : "");
    el("chatgptUsageCopyFeedback").classList.toggle("is-error", Boolean(copyError));
    el("chatgptUsageLogin").hidden = elapsed;
    el("chatgptUsageWaiting").classList.toggle("is-expired", elapsed);
    const seconds = Math.max(0, Math.ceil((data?.expires_at || 0) - Date.now() / 1000));
    el("chatgptUsageExpiry").textContent = elapsed ? "请重新获取验证码" : seconds
      ? "等待授权 · " + Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0")
      : "等待官方授权";
  }
  function renderControls() {
    const waiting = pending(), elapsed = expired(), connected = data?.status === "connected";
    const isNative = native();
    el("chatgptUsageTitle").textContent = isNative ? "Codex 账号用量" : "ChatGPT 订阅用量";
    el("chatgptUsageEmpty").querySelector("h3").textContent = isNative ? "Codex 用量与重置卡" : "连接 Codex 用量参考";
    el("chatgptUsageEmpty").querySelector("p").textContent = isNative
      ? "使用此渠道绑定的 Codex 账号读取官方额度。绑定完成后无需再次授权；如果授权失效，可在这里重新授权同一账号。"
      : "读取官方返回的额度窗口和重置卡；此信息不能单独确认订阅共享接口是否可用。";
    el("chatgptUsageReauthTitle").textContent = isNative ? "重新授权此 Codex 账号" : "重新授权用量读取";
    el("chatgptUsageReauth").querySelector("p").textContent = isNative
      ? "此授权同时用于模型调用和用量查询，请登录当前绑定的同一个账号。日常查看额度使用「刷新用量」；更换账号请到编辑渠道中选择「更换绑定账号」，保存后生效。"
      : "此操作需要在官方页面再次登录并输入验证码。日常查看额度请使用「刷新用量」。这只更新用量读取授权，不影响订阅共享接口的模型调用授权。";
    el("chatgptUsageFooter").querySelector("details p").textContent = isNative
      ? "来源为此渠道的官方 Codex 账户接口，包含其他客户端的使用，仅展示上游返回的窗口。7 天窗口不是自然周。每 15 分钟读取元数据，不调用模型；额度有余量不代表每个模型均可用。限制等待结束后，由单个真实请求确认恢复。"
      : "来源为官方 Codex 账户接口，仅展示它返回的窗口。订阅共享接口还可能受到应用或其他限制；这里有剩余额度不保证共享接口可用，刷新或使用重置卡也不会直接解除网关保护。7 天窗口不是自然周。连接后每 15 分钟读取元数据，不调用模型。";
    const badge = el("chatgptUsageBadge");
    const account = identity || data?.email || "";
    el("chatgptUsageIdentity").textContent = account;
    el("chatgptUsageIdentity").title = account;
    badge.textContent = busy ? operation === "authorize" ? "正在获取验证码…" : "正在读取用量…"
      : waiting ? elapsed ? "验证码过期" : "等待官方授权" : connected ? "用量已连接" : data?.error ? "读取失败" : "尚未授权读取用量";
    badge.classList.toggle("is-connected", connected && !busy);
    badge.classList.toggle("is-loading", busy);
    el("chatgptUsageDialog").setAttribute("aria-busy", String(busy));
    el("chatgptUsageLoading").hidden = !busy || Boolean(windows(data).length) || waiting;
    el("chatgptUsageLoading").lastElementChild.textContent = operation === "authorize" ? "正在获取官方验证码…" : "正在读取订阅用量…";
    el("chatgptUsageEmpty").hidden = busy || Boolean(windows(data).length) || waiting;
    el("chatgptUsageReauth").hidden = !confirming;
    el("chatgptUsageReauthConfirm").disabled = busy;
    el("chatgptUsageFooter").hidden = waiting && !elapsed;
    const wait = Math.max(0, Math.ceil((data?.manual_refresh_at || 0) - Date.now() / 1000));
    el("chatgptUsageRefresh").hidden = waiting || !windows(data).length && !connected && !data?.error;
    el("chatgptUsageRefresh").disabled = busy || !profile || wait > 0;
    el("chatgptUsageRefresh").textContent = busy ? "读取中…" : wait > 0 ? wait + " 秒后可刷新" : "刷新用量";
    el("chatgptUsageConnect").disabled = busy || !profile;
    el("chatgptUsageConnect").hidden = confirming;
    el("chatgptUsageConnect").classList.toggle("primary", !connected);
    el("chatgptUsageConnect").classList.toggle("secondary", connected);
    el("chatgptUsageConnect").textContent = busy ? "处理中…" : elapsed ? "重新获取验证码" : isNative ? "重新授权 Codex 账号" : connected || data?.updated_at || windows(data).length ? "重新授权用量" : "授权读取用量";
    el("chatgptUsageConnect").title = "需要在官方页面验证账号；日常查询请使用刷新用量";
    renderRestriction();
  }
  function renderRestriction() {
    const station = selectedStation(), limited = Boolean(station?.quota_exhausted);
    const circuit = !limited && needsRecovery(station), restricted = limited || circuit;
    const protection = station?.protection || {}, passive = limited && protection.recovery_mode === "passive";
    const until = Math.max(station?.quota_resume_at || 0, protection.until || 0, station?.rate_limit_until || 0);
    const recovery = window.ModelHarborRecovery.view(station);
    el("chatgptUsageRestriction").hidden = !restricted && !retryStatus;
    el("chatgptUsageRestrictionTitle").textContent = recovery ? recovery.label
      : circuit ? protection.recovery_inflight ? "恢复验证中" : until > Date.now() / 1000 ? "熔断冷却中" : "待恢复验证"
      : limited ? "订阅接入受限" : "订阅接入已恢复";
    el("chatgptUsageRestrictionHint").textContent = recovery
      ? recovery.detail + " 下方用量是官方窗口的参考，仍有剩余不代表已恢复；不会发送后台生成请求。"
      : circuit ? "此前生成请求连续失败，网关暂停了此渠道。冷却结束后，可用下一次业务请求或手动验证一次；收到完整成功响应后解除熔断。刷新用量不会解除保护，也不会发送后台生成请求。"
      : !limited ? "完整生成验证已成功。" : ["codex_cli", "codex_relay"].includes(station?.chatgpt_mode)
      ? "Codex 返回使用限制，网关已暂停此渠道。查看官方用量后可手动验证一次；不会后台重试。"
      : "上游暂停了订阅共享接口的访问。下方 Codex 窗口仍有剩余，不代表此接口已恢复；网关不会据此推算恢复时间或后台重试。";
    const detail = recovery ? recovery.diagnostic : circuit ? protection.reason || station.last_error || "生成请求未完成"
      : limited ? [station.quota_at ? new Date(station.quota_at * 1000).toLocaleString() : "", station.quota_error || "上游拒绝了订阅请求"].filter(Boolean).join(" · ") : "";
    el("chatgptUsageRestrictionDetail").textContent = detail;
    const wait = Math.max(0, Math.ceil(Math.max(retryAfter, passive || circuit ? until : station?.rate_limit_until || 0) - Date.now() / 1000));
    const button = el("chatgptUsageRetry");
    button.hidden = !restricted;
    button.disabled = retryBusy || busy || !station?.enabled || wait > 0 || Boolean(protection.recovery_inflight);
    button.textContent = retryBusy || protection.recovery_inflight ? "正在验证…" : !station?.enabled ? "请先启用站点" : wait > 0 ? wait + " 秒后可验证" : "手动验证一次";
    el("chatgptUsageRetryStatus").textContent = retryStatus;
  }
  async function retrySubscription() {
    const station = selectedStation();
    if (retryBusy || busy || !station?.enabled || !needsRecovery(station)
        || station.protection?.recovery_inflight || retryAfter > Date.now() / 1000) return;
    if (!window.confirm("向此订阅发送一次简短的 OK 验证请求，可能消耗订阅用量；不会自动重试，也不会覆盖智商测试预览。继续吗？")) return;
    const revision = epoch, name = selected, profileId = profile;
    retryBusy = true; retryStatus = "正在等待上游完整响应…"; renderRestriction();
    try {
      const result = await api("/admin/station/" + encodeURIComponent(name) + "/subscription-retry", "POST", false, {confirmed: true});
      if (epoch !== revision || selectedStation()?.chatgpt_account !== profileId) return;
      for (const item of [...(typeof STATIONS === "undefined" ? [] : STATIONS), ...(statusData?.stations || [])]) {
        if (item.name === name) Object.assign(item, {quota_exhausted: result.quota_exhausted, quota_error: result.quota_error, quota_at: result.quota_at,
          quota_resume_at: result.quota_resume_at || 0, quota_probe_at: result.quota_probe_at || 0,
          ...(result.status ? {status: result.status} : {}), ...(result.protection ? {protection: result.protection} : {})});
      }
      if (epoch === revision) {
        retryAfter = result.next_allowed_at || Date.now() / 1000 + 60;
        retryStatus = result.recovered ? "上游已完整返回成功，订阅路由保护已解除。" : "本次未恢复：" + (result.error_code || result.error || "上游仍拒绝访问")
          + (result.request_id ? " · Request ID: " + result.request_id : "");
        showToast(retryStatus, !result.recovered);
      }
      await refreshStations({force: true});
      if (typeof window.refresh === "function") await window.refresh(true);
      render(true);
    } catch (error) {
      if (epoch === revision) {
        retryAfter = Date.now() / 1000 + 60;
        retryStatus = "验证未确认：" + error.message + "。请稍后查看状态，未自动重试。";
        showToast(retryStatus, true);
      }
    } finally { if (epoch === revision) { retryBusy = false; renderRestriction(); } }
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (pending() && !expired() && el("chatgptUsageDialog").open && !busy) {
      pollTimer = setTimeout(() => refresh(false), Math.min(30000, 2000 * 2 ** Math.min(pollFailures, 4)));
    }
  }
  function copyFallback(code) {
    const field = document.createElement("textarea"), focused = document.activeElement;
    field.value = code; field.className = "clipboard-helper"; field.setAttribute("readonly", "");
    // Modal dialogs make elements outside the dialog inert, including copy helpers.
    el("chatgptUsageDialog").append(field);
    field.focus({preventScroll: true});
    field.select();
    try { if (!document.execCommand("copy")) throw new Error("Clipboard unavailable"); }
    finally { field.remove(); focused?.focus({preventScroll: true}); }
  }
  async function copyCode() {
    const code = pending() ? data?.user_code : "", revision = epoch;
    if (!code || expired() || copyBusy) return;
    copyBusy = true; copyError = ""; renderDevice();
    try {
      if (navigator.clipboard?.writeText && window.isSecureContext) {
        try { await navigator.clipboard.writeText(code); } catch { copyFallback(code); }
      } else copyFallback(code);
      if (epoch === revision) {
        copiedCode = code; copiedUntil = Date.now() + 2500;
        showToast("验证码已复制");
      }
    } catch {
      if (epoch === revision) {
        copiedCode = ""; copiedUntil = 0;
        copyError = "浏览器未允许复制，验证码已选中，请按 Ctrl+C 或手动复制。";
        const range = document.createRange(); range.selectNodeContents(el("chatgptUsageCode"));
        const selection = window.getSelection(); selection?.removeAllRanges(); selection?.addRange(range);
        showToast(copyError, true);
      }
    } finally { copyBusy = false; if (epoch === revision) renderDevice(); }
  }
  function renderDetail() {
    el("chatgptUsageStation").textContent = selected;
    const items = windows(data);
    el("chatgptUsageWindows").hidden = !items.length || pending();
    el("chatgptUsageEmpty").hidden = Boolean(items.length || pending());
    el("chatgptUsageWindows").replaceChildren(...items.map(item => {
      const card = document.createElement("section"); card.className = "go-usage-window " + tone(item.used_percent);
      const heading = document.createElement("h3"); heading.textContent = (item.name || "Codex") + " · " + label(item);
      const metric = document.createElement("strong"); metric.textContent = percentage(item.used_percent);
      const caption = document.createElement("span"); caption.textContent = number(item.used_percent) ? "已使用" : "上游未提供";
      const bar = document.createElement("progress"); bar.max = 100; bar.value = progress(item.used_percent);
      bar.setAttribute("aria-label", heading.textContent + "已用 " + percentage(item.used_percent));
      const reset = document.createElement("small");
      reset.textContent = item.resets_at ? (item.resets_at * 1000 <= Date.now() ? "窗口已到期，等待刷新 · " : "重置于 ") + new Date(item.resets_at * 1000).toLocaleString() : "重置时间未提供";
      card.append(heading, metric, caption, bar, reset); return card;
    }));
    renderCredits();
    el("chatgptUsageCreditSection").hidden = pending() || !data?.reset_credits;
    const status = data?.error || (!pending() && data?.updated_at ? "更新于 " + new Date(data.updated_at * 1000).toLocaleString() + (data?.stale ? " · 缓存数据" : "") : "");
    el("chatgptUsageStatus").textContent = status;
    el("chatgptUsageStatus").hidden = !status;
    el("chatgptUsageStatus").classList.toggle("is-error", Boolean(data?.error));
    renderDevice(); renderControls();
  }
  async function refresh(manual) {
    if (busy || !profile) return;
    const id = profile, revision = epoch;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 55000);
    if (manual) confirming = false;
    busy = true; operation = "read"; renderControls();
    try {
      const usage = await api("/admin/chatgpt/profile/" + encodeURIComponent(id) + "/usage" + (manual ? "?refresh=true" : ""),
        "GET", true, null, {signal: controller.signal});
      if (epoch === revision) {
        pollFailures = 0; update(id, usage); render(true);
      }
    } catch (error) {
      if (epoch === revision) {
        const message = error.name === "AbortError" ? "读取用量超时，请检查网关连接后刷新；已保留上次数据" : error.message;
        pollFailures++; data = {...data, status: pending() ? "pending" : "error", stale: Boolean(data?.updated_at), error: message,
          manual_refresh_at: Date.now() / 1000 + 60};
        if (manual) showToast(message, true);
      }
    } finally {
      clearTimeout(timeout);
      if (epoch === revision) { busy = false; operation = ""; renderDetail(); schedulePoll(); }
    }
  }
  function connect() {
    if (busy || !profile) return;
    if (!pending() && (native() || data?.status === "connected" || data?.updated_at || windows(data).length)) {
      confirming = true; renderControls(); el("chatgptUsageReauthConfirm").focus();
      return;
    }
    beginAuthorization();
  }
  async function beginAuthorization() {
    if (busy || !profile) return;
    const id = profile, revision = epoch;
    busy = true; operation = "authorize"; confirming = false; renderControls();
    try {
      const login = await api("/admin/chatgpt/profile/" + encodeURIComponent(id) + "/codex-login", "POST", true, {});
      if (epoch !== revision) return;
      pollFailures = 0; copiedCode = ""; update(id, login);
    } catch (error) {
      if (epoch === revision) data = {...data, error: error.message};
      showToast(error.message, true);
    } finally { if (epoch === revision) { busy = false; operation = ""; renderDetail(); schedulePoll(); } }
  }
  function open(station) {
    if (!station?.chatgpt_account) return;
    selected = station.name; profile = station.chatgpt_account; data = station.chatgpt_usage; epoch++; busy = false;
    identity = station.chatgpt?.email || ""; confirming = false; operation = "";
    retryBusy = false; retryStatus = ""; retryAfter = (station.quota_probe_at || station.quota_at || 0) + 60;
    pollFailures = 0; copiedCode = ""; copyError = "";
    clearTimeout(pollTimer);
    renderDetail();
    if (!el("chatgptUsageDialog").open) el("chatgptUsageDialog").showModal();
    refresh(false);
  }
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-chatgpt-usage]");
    if (!button) return;
    const station = (statusData?.stations || []).find(item => item.name === button.dataset.chatgptUsage);
    if (!station) return;
    open(station);
  });
  el("chatgptUsageRefresh").addEventListener("click", () => refresh(true));
  el("chatgptUsageRetry").addEventListener("click", retrySubscription);
  el("chatgptUsageConnect").addEventListener("click", connect);
  el("chatgptUsageReauthConfirm").addEventListener("click", beginAuthorization);
  el("chatgptUsageReauthCancel").addEventListener("click", () => { confirming = false; renderControls(); el("chatgptUsageConnect").focus(); });
  el("chatgptUsageCopy").addEventListener("click", copyCode);
  el("chatgptUsageClose").addEventListener("click", () => el("chatgptUsageDialog").close());
  el("chatgptUsageDialog").addEventListener("close", () => { epoch++; busy = false; clearTimeout(pollTimer); });
  setInterval(() => { if (el("chatgptUsageDialog").open) { renderDevice(); renderControls(); } }, 1000);
  window.ModelHarborChatGPTQuota = {markup, update, open};
  render(true);
})();
