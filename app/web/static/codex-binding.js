"use strict";
(() => {
  const el = id => document.getElementById(id);
  const native = () => el('sf-provider').value === 'chatgpt' && ['codex_relay', 'codex_cli'].includes(el('sf-chatgpt-mode').value);
  let attempt = null, staged = null, requesting = false, revision = 0, pollTimer = 0, failures = 0;
  let feedback = '', error = false, copying = false, copiedUntil = 0;
  const expired = () => attempt && attempt.expires_at * 1000 <= Date.now();
  const pending = () => requesting || Boolean(attempt && !expired());
  const account = () => staged?.profile === el('sf-chatgpt-account').value ? staged.account
    : (typeof STATIONS === 'undefined' ? [] : STATIONS).find(item => item.chatgpt_account === el('sf-chatgpt-account').value)?.chatgpt;
  const connected = () => Boolean(account()?.codex_connected);

  function update() {
    el('sf-codex-device').hidden = !native() || !attempt && !requesting;
    el('sf-codex-feedback').hidden = !native() || !feedback;
    if (!native()) return;
    const linked = account(), bound = connected();
    el('sf-chatgpt-status').textContent = bound ? staged ? '新账号已绑定 · 待保存' : 'Codex 账号已绑定' : '尚未绑定 Codex 账号';
    el('sf-chatgpt-email').textContent = linked?.email || '';
    const plan = linked?.plan;
    el('sf-chatgpt-plan').textContent = plan ? String(plan).replace(/^./, value => value.toUpperCase()) : '';
    el('sf-chatgpt-plan').hidden = !plan;
    el('sf-chatgpt-help').textContent = staged ? '保存渠道后使用此账号，并自动读取模型目录。' : '每个渠道独立绑定账号；一次官方登录即可用于模型调用和用量查询。';
    const button = el('sf-chatgpt-login');
    button.disabled = pending();
    button.textContent = requesting ? '正在获取验证码…' : attempt && !expired() ? '等待官方授权…' : expired() ? '重新获取验证码' : bound ? '更换绑定账号' : '绑定 Codex 账号';
    el('sf-chatgpt-usage-link').textContent = '查看用量与重置卡';
    el('sf-chatgpt-usage-link').disabled = !bound || Boolean(staged);
    el('sf-chatgpt-usage-link').title = staged ? '保存渠道后即可查看用量与重置卡' : '';
    el('sf-codex-feedback').textContent = feedback;
    el('sf-codex-feedback').classList.toggle('is-error', error);
    el('sf-codex-device').setAttribute('aria-busy', String(requesting));
    el('sf-codex-code-row').hidden = !attempt;
    el('sf-codex-title').textContent = requesting ? '准备官方授权' : expired() ? '验证码已过期' : '在官方页面完成绑定';
    const code = attempt?.user_code || '';
    if (el('sf-codex-code').textContent !== code) el('sf-codex-code').textContent = code;
    const seconds = Math.max(0, Math.ceil((attempt?.expires_at || 0) - Date.now() / 1000));
    el('sf-codex-waiting').textContent = requesting ? '正在获取一次性验证码…' : expired() ? '请重新获取验证码' : `等待授权 · ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    el('sf-codex-copy').disabled = !code || expired() || copying;
    el('sf-codex-copy').textContent = copying ? '复制中…' : Date.now() < copiedUntil ? '已复制' : '复制验证码';
    el('sf-codex-login').hidden = !code || expired();
  }

  function clearPoll() { clearTimeout(pollTimer); pollTimer = 0; }
  function schedule() {
    clearPoll();
    if (attempt && !expired() && native()) pollTimer = setTimeout(poll, Math.min(30000, 2000 * 2 ** Math.min(failures, 4)));
  }
  async function discard(identifier) {
    if (!identifier) return;
    try { await api('/admin/chatgpt/codex/login/' + encodeURIComponent(identifier), 'DELETE', true); }
    catch { /* Server expiry and account shutdown also cancel abandoned attempts. */ }
  }
  function reset() {
    const identifier = attempt?.id || staged?.attempt_id;
    revision++; clearPoll(); attempt = null; staged = null; requesting = false;
    failures = 0; feedback = ''; error = false; copying = false; copiedUntil = 0;
    if (identifier) discard(identifier);
    update();
  }
  async function cancel() {
    const identifier = attempt?.id;
    revision++; clearPoll(); attempt = null; requesting = false;
    feedback = '已取消本次绑定。'; error = false;
    update();
    await discard(identifier);
  }
  async function connect() {
    if (!native() || pending()) return;
    const old = attempt?.id;
    const current = ++revision;
    clearPoll(); attempt = null; requesting = true; feedback = ''; error = false; failures = 0; copiedUntil = 0;
    update();
    try {
      await discard(old);
      const result = await api('/admin/chatgpt/codex/login', 'POST', true, {});
      if (revision !== current || !native()) { await discard(result.id); return; }
      attempt = result;
      feedback = el('sf-chatgpt-account').value ? '请确认官方页面上的目标账号。保存渠道后才会替换当前绑定。' : '在官方页面选择目标账号并输入验证码，完成后这里会自动更新。';
      schedule();
    } catch (failure) {
      if (revision === current) { feedback = failure.message || '无法开始绑定，请稍后重试'; error = true; }
    } finally { if (revision === current) { requesting = false; update(); } }
  }
  async function poll() {
    if (!attempt || expired() || !native()) { update(); return; }
    const current = revision, identifier = attempt.id;
    try {
      const result = await api('/admin/chatgpt/codex/login/' + encodeURIComponent(identifier));
      if (revision !== current || !native()) return;
      failures = 0;
      if (result.status === 'completed') {
        staged = {profile: result.profile, account: result.account, attempt_id: identifier};
        el('sf-chatgpt-account').value = result.profile;
        attempt = null;
        el('sf-chatgpt-usage').hidden = true;
        feedback = '账号已绑定，请保存渠道。模型目录将自动读取，无需再次授权用量。'; error = false;
        if (typeof renderStationModels === 'function') renderStationModels();
        showToast('Codex 账号已绑定，请保存渠道');
      } else if (result.status !== 'pending') {
        attempt = null;
        feedback = result.error || (result.status === 'expired' ? '验证码已过期，请重新绑定。' : '本次授权已结束，请重新绑定。'); error = true;
      }
    } catch (failure) {
      if (revision !== current) return;
      failures++;
      feedback = '暂时未读到授权结果，正在等待同步。'; error = false;
    } finally { if (revision === current) { update(); schedule(); } }
  }
  function validate() {
    if (!native()) return '';
    return pending() ? '请先完成或取消 Codex 账号绑定' : connected() ? '' : '请先绑定 Codex 账号，再保存渠道';
  }
  function copyFallback(code) {
    const field = document.createElement('textarea'), focused = document.activeElement;
    field.value = code; field.className = 'clipboard-helper'; field.setAttribute('readonly', '');
    el('sf-codex-device').append(field); field.focus({preventScroll: true}); field.select();
    try { if (!document.execCommand('copy')) throw new Error('Clipboard unavailable'); }
    finally { field.remove(); focused?.focus({preventScroll: true}); }
  }
  async function copy() {
    const code = attempt?.user_code, current = revision;
    if (!code || expired() || copying) return;
    copying = true; update();
    try {
      if (navigator.clipboard?.writeText && window.isSecureContext) {
        try { await navigator.clipboard.writeText(code); } catch { copyFallback(code); }
      } else copyFallback(code);
      if (revision === current) { copiedUntil = Date.now() + 2500; feedback = '验证码已复制，可前往官方页面粘贴。'; error = false; }
    } catch {
      if (revision === current) {
        feedback = '浏览器未允许复制，验证码已选中，请按 Ctrl+C。'; error = true;
        const selection = window.getSelection(), range = document.createRange();
        range.selectNodeContents(el('sf-codex-code')); selection.removeAllRanges(); selection.addRange(range);
      }
    } finally { if (revision === current) { copying = false; update(); } }
  }
  el('sf-codex-copy').addEventListener('click', copy);
  el('sf-codex-cancel').addEventListener('click', cancel);
  for (const id of ['sf-provider', 'sf-chatgpt-mode']) el(id).addEventListener('change', () => {
    if (!native() && (attempt || requesting)) cancel();
    update();
  });
  setInterval(() => { if (native() && attempt) update(); }, 1000);
  window.ModelHarborCodexBinding = {update, connect, reset, validate};
})();
