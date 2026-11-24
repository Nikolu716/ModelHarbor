/* Account-local model routing. Opening this panel never calls an upstream. */
(() => {
  'use strict';
  let rules = [], stations = [], previews = {}, editing = null, busy = false, loaded = false;
  const el = id => document.getElementById(id);
  const esc = value => escapeHtml(String(value ?? ''));
  const status = (message, error = false) => {
    el('mapping-status').textContent = message;
    el('mapping-status').classList.toggle('text-danger', error);
  };

  function supportsModel(station, model) {
    if (!station || ['anthropic', 'gemini'].includes(station.provider)) return false;
    if (!(station.effective_models || []).includes(model)) return false;
    const paths = station.model_api_paths?.[model];
    return !paths?.length || paths.some(path => ['/responses', '/chat/completions'].includes(path));
  }

  function availabilityMarkup(rule) {
    if (!rule.enabled) return '<div class="mapping-availability">已停用，按请求模型正常路由</div>';
    return Object.entries({'/responses': 'Responses', '/chat/completions': 'Chat'}).map(([path, label]) => {
      const plan = previews[rule.id]?.[path];
      if (!plan) return `<div class="mapping-availability">${label}：正在同步路由状态</div>`;
      const details = [...plan.targets.flatMap(target => target.stations.map(station =>
        `${target.model} · ${station.name}：${station.available ? '可调度' : station.reason}`)),
        ...plan.fallback.map(station => `原模型 · ${station.name}：${station.available ? '可调度' : station.reason}`)].join('\n');
      const message = plan.mode === 'target' ? `优先 ${plan.model} · 当前候选：${plan.stations.join('、')}`
        : plan.mode === 'pending' ? '模型目录正在读取，短暂等待后决定是否回退'
        : plan.mode === 'fallback' ? `回退 ${rule.source_model} · 当前候选：${plan.stations.join('、')}`
        : '目标与原模型当前均无可调度渠道';
      return `<div class="mapping-availability ${plan.mode === 'target' ? '' : 'is-fallback'}" title="${esc(details)}">${label}：${esc(message)}</div>`;
    }).join('');
  }

  function availableModels() {
    const names = new Set();
    for (const station of stations) {
      for (const model of station.effective_models || []) {
        if (supportsModel(station, model)) names.add(model);
      }
    }
    for (const rule of rules) (rule.target_models || []).forEach(model => names.add(model));
    return [...names].sort();
  }

  function refreshModelOptions() {
    el('mapping-model-options').innerHTML = availableModels()
      .map(model => `<option value="${esc(model)}"></option>`).join('');
  }

  function render() {
    el('mapping-count').textContent = `${rules.filter(r => r.enabled).length} 条启用 · ${rules.length} 条规则`;
    el('mapping-list').innerHTML = rules.map(rule => {
      const models = rule.target_models || [];
      return `<article class="mapping-rule" data-mapping-id="${esc(rule.id)}">
      <div class="mapping-rule-head"><strong>${esc(rule.name)}</strong><span class="mapping-state ${rule.enabled ? 'is-on' : ''}">${rule.enabled ? '已启用' : '已停用'}</span><span class="mapping-scope" title="${esc(rule.source_ips.join(' · ') || '所有通过当前账号白名单的来源')}">${rule.source_ips.length ? `${rule.source_ips.length} 个指定 IP` : '所有来源'}</span></div>
      <div class="mapping-rule-body"><div class="mapping-source"><small>请求模型</small><code title="${esc(rule.source_model)}">${esc(rule.source_model)}</code></div><span class="mapping-arrow" aria-hidden="true">→</span><div class="mapping-destinations">${models.map(model => `<div><code title="${esc(model)}">${esc(model)}</code></div>`).join('')}</div></div>
      ${availabilityMarkup(rule)}
      <div class="mapping-rule-foot"><span>${rule.source_ips.length ? esc(rule.source_ips.join(' · ')) : '匹配此模型的全部请求'}</span><div><button type="button" class="button secondary" data-mapping-action="edit">编辑</button><button type="button" class="button secondary" data-mapping-action="toggle">${rule.enabled ? '停用' : '启用'}</button></div></div>
    </article>`;
    }).join('') || '<div class="mapping-empty"><h3>让一个模型名称使用另一个模型</h3><p>填写客户端请求的模型、来源 IP 与实际回答模型。网关按模型能力自动选择渠道，无需绑定具体站点。</p><button class="button secondary" type="button" id="mapping-empty-add">创建第一条映射</button></div>';
    el('mapping-empty-add')?.addEventListener('click', () => openEditor());
    refreshModelOptions();
  }

  window.refreshModelMappings = async () => {
    if (busy || el('mapping-dialog').open) return;
    busy = true;
    el('mapping-add').disabled = true;
    try {
      const [data, catalog] = await Promise.all([api('/admin/model-mappings'), api('/admin/stations')]);
      rules = data.rules; stations = catalog; previews = data.previews || {}; loaded = true;
      render(); status(data.error || data.migration_error || '', Boolean(data.error || data.migration_error));
    } catch (error) { status(error.message, true); }
    finally { busy = false; el('mapping-add').disabled = !loaded; }
  };

  function addTarget(model = '') {
    if (el('mapping-targets').children.length >= 16) return;
    const row = document.createElement('div');
    row.className = 'mapping-target-row';
    row.innerHTML = `<label>实际回答模型<input data-target-model list="mapping-model-options" maxlength="200" required placeholder="选择或输入模型名称" autocomplete="off" spellcheck="false"></label><button class="account-close mapping-remove-target" type="button" aria-label="移除此实际模型">×</button>`;
    row.querySelector('[data-target-model]').value = model;
    el('mapping-targets').append(row);
    row.querySelector('.mapping-remove-target').addEventListener('click', () => {
      if (el('mapping-targets').children.length === 1) {
        el('mapping-form-status').textContent = '至少保留一个实际回答模型';
        return;
      }
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
    refreshModelOptions();
    (rule?.target_models?.length ? rule.target_models : ['']).forEach(addTarget);
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
      rules = data.rules; previews = data.previews || {}; render(); status('已保存，对新请求立即生效');
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
    const models = [...el('mapping-targets').querySelectorAll('[data-target-model]')]
      .map(input => input.value.trim()).filter(Boolean);
    const unique = [...new Set(models)];
    if (!unique.length) {
      el('mapping-form-status').textContent = '请至少填写一个实际回答模型';
      return;
    }
    if (unique.length !== models.length) {
      el('mapping-form-status').textContent = '同一规则中每个实际模型只能填写一次';
      return;
    }
    const rule = {id: editing || 'map_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10),
      name: el('mapping-name').value.trim(), enabled: el('mapping-enabled').checked,
      source_model: el('mapping-model').value.trim(), source_ips: el('mapping-scope').value === 'all' ? [] : el('mapping-ips').value.split(/[\s,，]+/).filter(Boolean),
      target_models: unique};
    save(editing ? rules.map(r => r.id === editing ? rule : r) : [...rules, rule], true);
  });
  setInterval(() => {
    if (!document.hidden && el('mappings-wrap').getClientRects().length && !el('mapping-dialog').open && !busy) {
      window.refreshModelMappings();
    }
  }, 5000);
})();
