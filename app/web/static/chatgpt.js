"use strict";
(() => {
  const el = id => document.getElementById(id);
  let loginBusy = false;
  let usageBusy = false;
  let usageProfile = "";

  function redemptionStorageKey(profile, creditId) {
    return `modelharbor.reset-credit.${profile}.${creditId || "next"}`;
  }

  function redemptionId(profile, creditId) {
    const key = redemptionStorageKey(profile, creditId);
    try {
      const saved = sessionStorage.getItem(key);
      if (saved) return saved;
      const id = crypto.randomUUID();
      sessionStorage.setItem(key, id);
      return id;
    } catch {
      return crypto.randomUUID();
    }
  }

  function clearRedemptionId(profile, creditId) {
    try { sessionStorage.removeItem(redemptionStorageKey(profile, creditId)); }
    catch { /* Session storage can be disabled by browser policy. */ }
  }

  function current() {
    const id = el("sf-chatgpt-account").value;
    return (typeof STATIONS === "undefined" ? [] : STATIONS).find(station => station.chatgpt_account === id)?.chatgpt;
  }

  function update() {
    const profile = el("sf-chatgpt-account").value;
    if (usageProfile && usageProfile !== profile) {
      usageProfile = "";
      el("sf-chatgpt-usage").hidden = true;
      el("sf-chatgpt-windows").replaceChildren();
      el("sf-chatgpt-credits").replaceChildren();
    }
    if (el('sf-provider').value === 'chatgpt' && ['codex_relay', 'codex_cli'].includes(el('sf-chatgpt-mode').value)) {
      window.ModelHarborCodexBinding?.update();
      return;
    }
    const linked = current();
    el("sf-chatgpt-email").textContent = linked?.email || (profile ? "等待授权确认" : "");
    const sharingConnected = linked?.shared_connected ?? linked?.connected;
    el('sf-chatgpt-plan').hidden = true;
    el('sf-chatgpt-help').textContent = '使用官方 OAuth 登录订阅共享接口；Codex 账号可通过接入方式单独绑定。';
    el("sf-chatgpt-status").textContent = sharingConnected ? "已连接" : linked ? "需要重新授权" : profile ? "已授权 · 保存站点后生效" : "尚未授权";
    el("sf-chatgpt-login").disabled = loginBusy;
    el("sf-chatgpt-login").textContent = loginBusy ? "等待官方授权…" : profile ? "重新授权 / 切换账号" : "Continue with ChatGPT";
    el("sf-chatgpt-usage-link").disabled = !sharingConnected || usageBusy;
    el("sf-chatgpt-usage-link").textContent = "查看订阅用量";
  }

  function renderUsage(data) {
    const panel = el("sf-chatgpt-usage");
    panel.hidden = false;
    const credits = data?.reset_credits;
    const windows = Array.isArray(data?.windows) ? data.windows : [];
    el("sf-chatgpt-usage-status").textContent = data?.status === "connected"
      ? (Number.isFinite(credits?.availableCount) ? `Codex 用量已连接 · ${credits.availableCount} 张可用重置卡` : "Codex 用量已连接 · 重置卡数量未提供")
      : data?.status === "pending" ? "用量授权进行中，点击「查看订阅用量」复制验证码并继续。"
        : data?.status === "disconnected" ? "可在「查看订阅用量」中授权读取额度，不影响模型调用。" : "尚未读取 Codex 用量";
    el("sf-chatgpt-windows").replaceChildren(...windows.map(item => {
      const line = document.createElement("div");
      const percent = Number(item.used_percent);
      const duration = Number(item.duration_mins);
      const windowLabel = duration === 10080 ? "7 天" : Number.isFinite(duration) && duration > 0
        ? duration % 60 === 0 ? duration / 60 + " 小时" : duration + " 分钟" : "用量窗口";
      line.textContent = `${item.name || "Codex"} · ${windowLabel} · ${Number.isFinite(percent) ? `${percent}% 已用` : "用量未提供"}${item.resets_at ? ` · 重置 ${new Date(item.resets_at * 1000).toLocaleString()}` : ""}`;
      return line;
    }));
    const rows = Array.isArray(credits?.credits)
      ? credits.credits.filter(item => item && item.status === "available") : [];
    const creditRows = rows.map(item => {
      const row = document.createElement("div");
      const label = document.createElement("span");
      const expiresAt = item.expiresAt ?? item.expires_at;
      const expires = expiresAt ? new Date(typeof expiresAt === "number" ? expiresAt * 1000 : expiresAt) : null;
      label.textContent = [item.title || "速率限制重置卡", expires && !Number.isNaN(expires.valueOf()) ? `有效期至 ${expires.toLocaleString()}` : "有效期未提供"].join(" · ");
      const button = document.createElement("button");
      button.type = "button"; button.className = "button secondary"; button.textContent = "使用";
      const id = typeof item.id === "string" ? item.id : typeof item.creditId === "string" ? item.creditId : null;
      button.addEventListener("click", () => consume(id, item.title || "速率限制重置卡"));
      row.append(label, button); return row;
    });
    const count = Number(credits?.availableCount ?? credits?.available_count);
    if (!creditRows.length && Number.isFinite(count) && count > 0) {
      const row = document.createElement("div");
      const label = document.createElement("span");
      label.textContent = `可用重置卡 ${count} 张 · 由 Codex 选择下一张`;
      const button = document.createElement("button");
      button.type = "button"; button.className = "button secondary"; button.textContent = "使用一张";
      button.addEventListener("click", () => consume(null, "下一张可用重置卡"));
      row.append(label, button);
      creditRows.push(row);
    }
    el("sf-chatgpt-credits").replaceChildren(...creditRows);
  }

  async function loadUsage(refresh = false) {
    const profile = el("sf-chatgpt-account").value;
    if (!profile || usageBusy) return;
    try {
      const data = await api(`/admin/chatgpt/profile/${encodeURIComponent(profile)}/usage${refresh ? "?refresh=true" : ""}`);
      if (profile !== el("sf-chatgpt-account").value) return;
      usageProfile = profile;
      renderUsage(data);
      update();
    } catch (error) {
      if (profile !== el("sf-chatgpt-account").value) return;
      renderUsage({status: "error"});
      el("sf-chatgpt-usage-status").textContent = error.message;
    }
  }

  function connectUsage() {
    if (usageBusy) return;
    const profile = el("sf-chatgpt-account").value;
    const station = (typeof STATIONS === "undefined" ? [] : STATIONS).find(item => item.chatgpt_account === profile);
    if (station) window.ModelHarborChatGPTQuota?.open(station);
  }

  async function consume(creditId, title, profile = el("sf-chatgpt-account").value) {
    if (usageBusy) return;
    if (!window.confirm(`将使用「${title}」。这会消耗一张 ChatGPT/Codex 重置卡，是否继续？`)) return;
    usageBusy = true; update();
    try {
      const data = await api(`/admin/chatgpt/profile/${encodeURIComponent(profile)}/reset-credit`, "POST", true,
        {credit_id: creditId || undefined, confirmed: true, idempotency_key: redemptionId(profile, creditId)});
      clearRedemptionId(profile, creditId);
      if (profile === el("sf-chatgpt-account").value) renderUsage(data.usage);
      window.ModelHarborChatGPTQuota?.update(profile, data.usage);
      render(true);
      showToast(data.outcome === "reset" || data.outcome === "alreadyRedeemed" ? "重置操作已确认" : `上游返回：${data.outcome || "未执行"}`);
    } catch (error) { showToast(error.message, true); }
    finally { usageBusy = false; update(); }
  }

  async function connect() {
    if (el('sf-provider').value === 'chatgpt' && ['codex_relay', 'codex_cli'].includes(el('sf-chatgpt-mode').value)) {
      return window.ModelHarborCodexBinding?.connect();
    }
    if (loginBusy) return;
    loginBusy = true;
    update();
    const popup = window.open("about:blank", "_blank");
    if (popup) popup.document.title = "正在连接 ChatGPT";
    let loginId = "";
    try {
      const profile = el("sf-chatgpt-account").value || undefined;
      const result = await api("/admin/chatgpt/login", "POST", true, profile ? {profile} : {});
      loginId = result.id;
      if (popup) popup.location.replace(result.auth_url);
      else window.location.assign(result.auth_url);
      const deadline = Math.min(Number(result.expires_at) * 1000, Date.now() + 10 * 60_000);
      let final = null;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 1500));
        final = await api("/admin/chatgpt/login/" + encodeURIComponent(loginId));
        if (final.status !== "pending" && final.status !== "exchanging") break;
      }
      if (!final || final.status === "pending" || final.status === "exchanging") throw new Error("授权等待超时，请重新尝试");
      if (final.status === "permission_required") throw new Error("账号已登录，但未授权使用 ChatGPT 订阅。请重新连接并允许订阅用量权限");
      if (final.status !== "completed") throw new Error(final.error || "ChatGPT 授权未完成");
      el("sf-chatgpt-account").value = final.profile;
      await refreshStations({force: true});
      update();
      showToast("ChatGPT 已授权，请保存站点后使用");
    } catch (error) {
      if (popup && !popup.closed) popup.close();
      showToast(error.message || "ChatGPT 授权失败", true);
    } finally {
      loginBusy = false;
      update();
    }
  }

  function markup(station) {
    return window.ModelHarborChatGPTQuota?.markup(station) || '<span class="subscription-value">ChatGPT订阅</span>';
  }

  el("sf-chatgpt-login").addEventListener("click", connect);
  el("sf-chatgpt-usage-link").addEventListener("click", connectUsage);
  document.addEventListener("click", event => {
    const edit = event.target.closest('[data-manage-action="edit"]');
    if (!edit) return;
    requestAnimationFrame(update);
  });
  document.addEventListener("modelharbor:stations-rendered", () => {
    // Background station refreshes must not overwrite a newly authorized,
    // unsaved profile in the editor. fillStationForm/resetStationForm own it.
    update();
    if (el("sf-provider").value === "chatgpt" && current()?.connected) loadUsage(false);
  });
  window.ModelHarborChatGPT = {markup, update, renderUsage, consume};
  document.addEventListener("DOMContentLoaded", update);
})();
