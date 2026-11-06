/* Shared presentation for passive Codex recovery. This never sends requests. */
(() => {
  "use strict";
  function view(station, now = Date.now() / 1000) {
    const protection = station?.protection || {};
    if (!station?.quota_exhausted || protection.recovery_mode !== "passive") return null;
    const until = Math.max(Number(protection.until) || 0, Number(station.quota_resume_at) || 0);
    const remaining = Math.max(0, Math.ceil(until - now));
    const duration = remaining >= 3600
      ? `${Math.floor(remaining / 3600)}小时${Math.floor(remaining % 3600 / 60)}分`
      : `${Math.floor(remaining / 60)}分${String(remaining % 60).padStart(2, "0")}秒`;
    const outcome = protection.recovery_outcome;
    const error = protection.recovery_error || "";
    const cause = {
      client_cancelled: "客户端取消了请求", client_stream_closed: "客户端提前关闭了连接",
      response_dropped: "客户端未接收响应", service_restarted: "服务重启，验证未完成",
      request_aborted: "请求提前结束", incomplete_response: "未收到完整响应",
    }[error] || error || "未收到完整响应";
    const attempt = outcome === "interrupted" ? `上次验证中断：${cause}。`
      : outcome === "failed" ? `上次验证失败：${cause}。` : "";
    const upstream = protection.wait_source === "upstream";
    let label, detail;
    if (protection.recovery_inflight) {
      label = "恢复验证中";
      detail = "正在用一个真实请求确认恢复，其他请求暂不进入此渠道。完整响应成功后恢复调度。";
    } else if (remaining > 0) {
      const prefix = outcome === "interrupted" ? "验证中断" : outcome === "failed" ? "验证失败"
        : upstream ? "上游限制" : "网关等待";
      label = `${prefix} · ${duration}`;
      detail = `${attempt}${upstream ? "遵守上游返回的等待时间" : "网关保护等待，尚未确认上游恢复"}；${new Date(until * 1000).toLocaleString()} 后可验证（剩余 ${duration}）。`;
    } else {
      label = outcome === "interrupted" ? "验证中断 · 待重试" : outcome === "failed" ? "验证失败 · 待重试" : "待真实请求验证";
      detail = `${attempt}等待已结束，下一个真实请求将单独验证恢复，也可手动验证一次。完整成功后恢复调度。`;
    }
    const dated = (time, text) => time ? `${new Date(time * 1000).toLocaleString()} · ${text}` : text;
    const diagnostic = [
      dated(station.quota_at, `最近一次上游使用限制：${station.quota_error || protection.reason || "usage_limit_reached"}`),
      protection.recovery_attempt_at ? dated(protection.recovery_attempt_at,
        protection.recovery_inflight ? "恢复验证进行中" : attempt || "尚未确认恢复") : "",
    ].filter(Boolean).join("\n");
    return {label, detail, diagnostic, until, remaining, ready: remaining === 0 && !protection.recovery_inflight};
  }
  window.ModelHarborRecovery = {view};
})();
