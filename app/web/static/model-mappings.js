/* Account-local model routing. Opening this panel never calls an upstream. */
(() => {
  'use strict';
  let rules = [], stations = [], editing = null, busy = false, loaded = false;
  const el = id => document.getElementById(id);
  const esc = value => escapeHtml(String(value ?? ''));
  const status = (message, error = false) => {
    el('mapping-status').textContent = message;
    el('mapping-status').classList.toggle('text-danger', error);
  };
  const option = (value, label = value) => `<option value="${esc(value)}">${esc(label)}</option>`;
  function render() {
    el('mapping-count').textContent = `${rules.filter(r => r.enabled).length} 条启用 · ${rules.length} 条规则`;
    el('mapping-list').innerHTML = rules.map(rule => `<article class="mapping-rule" data-mapping-id="${esc(rule.id)}">
      <div class="mapping-rule-head"><strong>${esc(rule.name)}</strong><span class="mapping-state ${rule.enabled ? 'is-on' : ''}">${rule.enabled ? '已启用' : '已停用'}</span><span class="mapping-scope" title="${esc(rule.source_ips.join(' · ') || '所有通过当前账号白名单的来源')}">${rule.source_ips.length ? `${rule.source_ips.length} 个指定 IP` : '所有来源'}</span></div>
      <div class="mapping-rule-body"><div class="mapping-source"><small>请求模型</small><code title="${esc(rule.source_model)}">${esc(rule.source_model)}</code></div><span class="mapping-arrow" aria-hidden="true">→</span><div class="mapping-destinations">${rule.targets.map(t => `<div><span title="${esc(t.station)}">${esc(t.station)}</span><code title="${esc(t.model)}">${esc(t.model)}</code></div>`).join('')}</div></div>
      <div class="mapping-rule-foot"><span>${rule.source_ips.length ? esc(rule.source_ips.join(' · ')) : '匹配此模型的全部请求'}</span><div><button type="button" class="button secondary" data-mapping-action="edit">编辑</button><button type="button" class="button secondary" data-mapping-action="toggle">${rule.enabled ? '停用' : '启用'}</button></div></div>
    </article>`).join('') || '<div class="mapping-empty"><h3>让一个模型名称连接更多渠道</h3><p>指定客户端请求的模型、来源 IP 与实际回答模型。可同时选择多个渠道，由网关调度。</p><button class="button secondary" type="button" id="mapping-empty-add">创建第一条映射</button></div>';
    el('mapping-empty-add')?.addEventListener('click', () => openEditor());
  }
  window.refreshModelMappings = async () => {
    if (busy || el('mapping-dialog').open) return;
    busy = true;
    el('mapping-add').disabled = true;
    try {
      const [data, catalog] = await Promise.all([api('/admin/model-mappings'), api('/admin/stations')]);
      rules = data.rules; stations = catalog; loaded = true;
      render(); status(data.error || '', Boolean(data.error));
    } catch (error) { status(error.message, true); }
    finally { busy = false; el('mapping-add').disabled = !loaded; }
  };
  function modelsFor(station) {
    if (!station || ['anthropic', 'gemini'].includes(station.provider)) return [];
    return (station.effective_models || []).filter(m => !station.model_api_paths?.[m]?.length || station.model_api_paths[m].some(p => ['/responses', '/chat/completions'].includes(p)));
  }
  function fillModels(row, selected = '') {
    const station = stations.find(s => s.name === row.querySelector('[data-target-station]').value);
    const select = row.querySelector('[data-target-model]');
    const custom = row.querySelector('[data-target-custom]');
    const models = modelsFor(station);
    select.innerHTML = option('', '选择实际模型') + models.map(m => option(m)).join('');
    if (selected && !models.includes(selected)) select.insertAdjacentHTML('beforeend', option(selected, selected + '（请确认仍可用）'));
    select.value = selected;
    custom.hidden = !station?.accepts_any_model;
    custom.required = Boolean(station?.accepts_any_model);
    custom.value = selected;
    select.hidden = Boolean(station?.accepts_any_model);
    select.required = !station?.accepts_any_model;
  }
  function addTarget(target = {}) {
    if (el('mapping-targets').children.length >= 16) return;
    const row = document.createElement('div'); row.className = 'mapping-target-row';
    const eligible = stations.filter(s => !['anthropic', 'gemini'].includes(s.provider));
    row.innerHTML = `<label>目标渠道<select data-target-station required>${option('', '选择当前账号的渠道')}${eligible.map(s => option(s.name, s.name + (s.enabled ? '' : '（已停用）'))).join('')}${target.station && !eligible.some(s => s.name === target.station) ? option(target.station, target.station + '（已删除）') : ''}</select></label><label>实际回答模型<select data-target-model required>${option('', '先选择渠道')}</select><input data-target-custom hidden maxlength="200" placeholder="输入渠道支持的模型名称" autocomplete="off"></label><button class="account-close mapping-remove-target" type="button" aria-label="移除此目标渠道">×</button>`;
    el('mapping-targets').append(row);
    row.querySelector('[data-target-station]').value = target.station || '';
    fillModels(row, target.model || '');
    row.querySelector('[data-target-station]').addEventListener('change', () => fillModels(row));
    row.querySelector('.mapping-remove-target').addEventListener('click', () => {
      if (el('mapping-targets').children.length === 1) { el('mapping-form-status').textContent = '至少保留一个目标渠道'; return; }
      row.remove();
    });
  }
  function scopeChanged() {
    const specific = el('mapping-scope').value === 'ips';
    el('mapping-ips-label').hidden = !specific;
    el('mapping-ips').required = specific;
  }
  function openEditor(rule = null) {
    if (busy || !loaded) return;
    editing = rule?.id || null;
    el('mapping-form').reset();
    el('mapping-title').textContent = rule ? '编辑模型映射' : '新建模型映射';
    el('mapping-name').value = rule?.name || '';
    el('mapping-model').value = rule?.source_model || '';
    el('mapping-enabled').checked = rule?.enabled ?? true;
    el('mapping-scope').value = rule?.source_ips.length ? 'ips' : 'all';
    el('mapping-ips').value = rule?.source_ips.join('\n') || '';
    el('mapping-delete').hidden = !rule;
    el('mapping-form-status').textContent = '';
    el('mapping-targets').replaceChildren();
    (rule?.targets || [{}]).forEach(addTarget);
    scopeChanged(); el('mapping-dialog').showModal();
  }
  async function save(next, close = false) {
    if (busy) return;
    busy = true;
    el('mapping-form-status').textContent = '正在保存…';
    const controls = [...el('mapping-dialog').querySelectorAll('button, input, select, textarea')];
    controls.forEach(c => { c.disabled = true; });
    try {
      const data = await api('/admin/model-mappings', 'PUT', true, {rules: next});
      rules = data.rules; render(); status('已保存，对新请求立即生效');
      if (close) el('mapping-dialog').close();
      el('mapping-form-status').textContent = '';
    } catch (error) { el('mapping-form-status').textContent = error.message; status(error.message, true); }
    finally { busy = false; controls.forEach(c => { c.disabled = false; }); }
  }
  el('mapping-add').addEventListener('click', () => openEditor());
  el('mapping-reload').addEventListener('click', () => window.refreshModelMappings());
  el('mapping-list').addEventListener('click', event => {
    const button = event.target.closest('[data-mapping-action]');
    if (!button || busy) return;
    const rule = rules.find(r => r.id === button.closest('[data-mapping-id]').dataset.mappingId);
    if (button.dataset.mappingAction === 'edit') openEditor(rule);
    else save(rules.map(r => r.id === rule.id ? {...r, enabled: !r.enabled} : r));
  });
  el('mapping-scope').addEventListener('change', scopeChanged);
  el('mapping-target-add').addEventListener('click', () => addTarget());
  for (const id of ['mapping-close', 'mapping-cancel']) el(id).addEventListener('click', () => { if (!busy) el('mapping-dialog').close(); });
  el('mapping-dialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  el('mapping-delete').addEventListener('click', () => {
    if (confirm('删除此映射？新的请求将恢复使用其他匹配规则或正常路由。')) save(rules.filter(r => r.id !== editing), true);
  });
  el('mapping-form').addEventListener('submit', event => {
    event.preventDefault();
    const rule = {id: editing || 'map_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10),
      name: el('mapping-name').value.trim(), enabled: el('mapping-enabled').checked,
      source_model: el('mapping-model').value.trim(), source_ips: el('mapping-scope').value === 'all' ? [] : el('mapping-ips').value.split(/[\s,，]+/).filter(Boolean),
      targets: [...el('mapping-targets').children].map(row => ({station: row.querySelector('[data-target-station]').value,
        model: (row.querySelector('[data-target-custom]').hidden ? row.querySelector('[data-target-model]').value : row.querySelector('[data-target-custom]').value).trim()}))};
    save(editing ? rules.map(r => r.id === editing ? rule : r) : [...rules, rule], true);
  });
})();
