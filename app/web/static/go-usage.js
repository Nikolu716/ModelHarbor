"use strict";
(() => {
  const el = id => document.getElementById(id);
  const windows = [["rolling", "5 小时", "5 小时"], ["weekly", "本周", "本周"], ["monthly", "总用量 · 本周期", "本周期"]];
  let selected = "", busy = false;
  const percentage = value => Number.isFinite(value) ? new Intl.NumberFormat("zh-CN", {maximumFractionDigits:1}).format(value) + "%" : "—";
  const tone = value => value >= 100 ? "is-exhausted" : value >= 80 ? "is-warning" : "";
  function markup(station) {
    const data = station.go_usage;
    const items = windows.map(([key, label, short]) => {
      const value = data?.windows?.[key]?.percent;
      const progress = Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
      return '<span class="go-quota-item ' + tone(value) + '"><small>' + short + '</small><progress max="100" value="' + progress + '" aria-hidden="true"></progress><b>' + percentage(value) + '</b></span>';
    }).join("");
    return '<button type="button" class="go-quota-summary" data-go-usage="' + escapeHtml(station.name) + '" aria-label="查看 ' + escapeHtml(station.name) + ' 的 Go 订阅用量" title="Go 订阅已用额度；本周期为订阅总用量，点击查看重置时间"><span class="go-quota-caption"><span>Go 订阅' + (data?.stale ? ' · 缓存' : '') + '</span><svg aria-hidden="true" viewBox="0 0 16 16" width="11" height="11"><path d="m6 4 4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span class="go-quota-values">' + items + '</span></button>';
  }
  function station() { return (statusData?.stations || []).find(item => item.name === selected); }
  function renderDialog() {
    const st = station(), data = st?.go_usage;
    el("goUsageStation").textContent = selected;
    el("goUsageWindows").replaceChildren(...windows.map(([key, label]) => {
      const item = data?.windows?.[key] || {}, value = item.percent;
      const card = document.createElement("section");
      card.className = "go-usage-window " + tone(value);
      const heading = document.createElement("h3"); heading.textContent = label;
      const metric = document.createElement("strong"); metric.textContent = percentage(value);
      const caption = document.createElement("span"); caption.textContent = Number.isFinite(value) ? "已使用" : "上游未提供";
      const bar = document.createElement("progress"); bar.max = 100; bar.value = Math.min(100, Math.max(0, Number(value) || 0));
      bar.setAttribute("aria-label", label + "已用 " + percentage(value));
      const reset = document.createElement("small");
      reset.textContent = item.resets_at ? (item.resets_at * 1000 <= Date.now() ? "窗口已到期，等待上游刷新 · " : "重置于 ") + new Date(item.resets_at * 1000).toLocaleString() : "重置时间未提供";
      card.append(heading, metric, caption, bar, reset); return card;
    }));
    el("goUsageStatus").textContent = (data?.error ? data.error + "。" : "") + (data?.updated_at ? "上游数据更新于 " + new Date(data.updated_at * 1000).toLocaleString() : "尚未读取上游用量") + (!st?.enabled ? "；站点已停用，仅显示缓存" : "");
    const wait = Math.max(0, Math.ceil((data?.manual_refresh_at || 0) - Date.now() / 1000));
    el("goUsageRefresh").disabled = busy || !st?.enabled || wait > 0;
    el("goUsageRefresh").textContent = busy ? "读取中…" : wait > 0 ? wait + " 秒后可刷新" : "刷新用量";
  }
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-go-usage]");
    if (!button) return;
    selected = button.dataset.goUsage; renderDialog(); el("goUsageDialog").showModal();
  });
  el("goUsageClose").addEventListener("click", () => el("goUsageDialog").close());
  el("goUsageRefresh").addEventListener("click", async () => {
    if (busy) return;
    busy = true; renderDialog();
    try {
      const data = await api("/admin/station/" + encodeURIComponent(selected) + "/go-usage/refresh", "POST", true);
      const st = station(); if (st) st.go_usage = data;
      render(true);
    } catch (error) { showToast(error.message, true); }
    finally { busy = false; renderDialog(); }
  });
  setInterval(() => { if (el("goUsageDialog").open) renderDialog(); }, 1000);
  window.ModelHarborGoUsage = {markup};
  render(true);
})();
