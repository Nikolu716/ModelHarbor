/* Native selects remain the single source of form values. */
(() => {
  'use strict';
  const controls = new Map();
  let active = null, sequence = 0, queued = false;
  const descriptors = ['value', 'selectedIndex'].map(key => [key, Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, key)]);

  function close(restore = false) {
    if (!active) return;
    const item = active; active = null;
    item.button.setAttribute('aria-expanded', 'false');
    item.button.removeAttribute('aria-activedescendant');
    item.search.removeAttribute('aria-activedescendant');
    if (item.popup.showPopover && item.popup.matches(':popover-open')) item.popup.hidePopover();
    item.popup.hidden = true;
    if (restore && item.button.isConnected) item.button.focus({preventScroll: true});
  }

  function place(item) {
    const rect = item.button.getBoundingClientRect();
    if (!rect.width || !rect.height || item.button.checkVisibility && !item.button.checkVisibility()) { close(); return; }
    const viewport = window.visualViewport;
    const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
    const offsetX = viewport?.offsetLeft || 0, offsetY = viewport?.offsetTop || 0;
    const below = height + offsetY - rect.bottom - 16, above = rect.top - offsetY - 16;
    const wanted = Math.min(340, item.list.scrollHeight + (item.search.hidden ? 12 : 58));
    const up = below < Math.min(wanted, 180) && above > below;
    const available = Math.max(64, up ? above : below);
    const panelWidth = Math.min(width - 20, Math.max(rect.width, 220));
    item.popup.style.width = panelWidth + 'px';
    item.popup.style.left = Math.max(offsetX + 10, Math.min(rect.left, offsetX + width - panelWidth - 10)) + 'px';
    item.popup.style.maxHeight = Math.min(340, available) + 'px';
    item.popup.style.top = (up ? Math.max(offsetY + 10, rect.top - 6 - Math.min(wanted, available)) : rect.bottom + 6) + 'px';
    item.popup.dataset.direction = up ? 'up' : 'down';
  }

  function rowsOf(item) { return [...item.list.querySelectorAll('[role=option]:not([aria-disabled=true])')]; }
  function highlight(item, index, scroll = true) {
    const rows = rowsOf(item);
    if (!rows.length) { item.current = -1; return; }
    const row = rows[Math.max(0, Math.min(index, rows.length - 1))];
    item.current = Number(row.dataset.index);
    for (const option of item.list.querySelectorAll('[role=option]')) option.classList.toggle('is-active', option === row);
    item.button.setAttribute('aria-activedescendant', row.id);
    item.search.setAttribute('aria-activedescendant', row.id);
    if (scroll) row.scrollIntoView({block: 'nearest'});
  }

  function renderOptions(item) {
    const query = item.search.value.trim().toLocaleLowerCase();
    item.list.replaceChildren();
    let previousGroup = null;
    [...item.select.options].forEach((option, index) => {
      if (option.hidden || !option.text.toLocaleLowerCase().includes(query)) return;
      const group = option.closest('optgroup');
      if (group && group !== previousGroup) {
        const title = document.createElement('div'); title.className = 'mh-select-group'; title.textContent = group.label;
        item.list.append(title); previousGroup = group;
      }
      const row = document.createElement('div'); row.className = 'mh-select-option'; row.id = item.list.id + '-' + index;
      row.setAttribute('role', 'option'); row.dataset.index = String(index);
      row.setAttribute('aria-selected', String(option.selected));
      row.setAttribute('aria-disabled', String(option.disabled || Boolean(group?.disabled)));
      const label = document.createElement('span'); label.textContent = option.text;
      const check = document.createElement('span'); check.className = 'mh-select-check';
      check.textContent = option.selected ? '✓' : ''; check.setAttribute('aria-hidden', 'true');
      row.append(label, check); item.list.append(row);
    });
    if (!item.list.childElementCount) {
      const empty = document.createElement('div'); empty.className = 'mh-select-empty'; empty.textContent = '没有匹配的选项'; item.list.append(empty);
    }
    const rows = rowsOf(item);
    highlight(item, Math.max(0, rows.findIndex(row => Number(row.dataset.index) === item.select.selectedIndex)), false);
    if (active === item) place(item);
  }

  function open(item) {
    if (item.select.matches(':disabled') || item.select.hidden) return;
    if (active === item) { close(); return; }
    close(); sync(item); active = item; item.search.value = '';
    item.search.hidden = item.select.options.length < 8;
    (item.select.closest('dialog[open]') || document.body).append(item.popup); item.popup.hidden = false;
    item.button.setAttribute('aria-expanded', 'true');
    renderOptions(item);
    if (item.popup.showPopover) item.popup.showPopover();
    place(item);
    if (!item.search.hidden) item.search.focus({preventScroll: true});
    item.list.querySelector('[aria-selected=true]')?.scrollIntoView({block: 'nearest'});
  }

  function commit(item, index) {
    const option = item.select.options[index];
    if (!option || option.disabled || option.closest('optgroup')?.disabled) return;
    const changed = item.select.selectedIndex !== index;
    item.select.selectedIndex = index; close(true);
    if (changed) {
      item.select.dispatchEvent(new Event('input', {bubbles: true}));
      item.select.dispatchEvent(new Event('change', {bubbles: true}));
    }
    sync(item);
  }

  function keyboard(item, event) {
    if (event.key === 'Escape' && active === item) {
      event.preventDefault(); event.stopImmediatePropagation(); close(true); return;
    }
    if (event.key === 'Tab') { close(event.target === item.search); return; }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      if (active !== item) { open(item); return; }
      const rows = rowsOf(item), index = rows.findIndex(row => Number(row.dataset.index) === item.current);
      highlight(item, event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : index + (event.key === 'ArrowDown' ? 1 : -1)); return;
    }
    if (event.key === 'Enter' || event.key === ' ' && event.target !== item.search) {
      event.preventDefault(); active === item ? commit(item, item.current) : open(item); return;
    }
    if (event.target !== item.search && event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      item.typed = Date.now() - item.typedAt < 700 ? item.typed + event.key : event.key; item.typedAt = Date.now();
      const index = [...item.select.options].findIndex(option => !option.disabled && !option.hidden && option.text.toLocaleLowerCase().startsWith(item.typed.toLocaleLowerCase()));
      if (index >= 0) { if (active !== item) open(item); highlight(item, rowsOf(item).findIndex(row => Number(row.dataset.index) === index)); }
    }
  }

  function sync(item) {
    item.wrapper.hidden = item.select.hidden;
    item.button.disabled = item.select.matches(':disabled');
    item.label.textContent = item.select.selectedOptions[0]?.text || '请选择';
    item.button.title = item.label.textContent;
    const labels = [...(item.select.labels || [])].map(label => [...label.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent.trim()).join(' ')).filter(Boolean).join(' ');
    item.button.setAttribute('aria-label', item.select.getAttribute('aria-label') || labels || '选择选项');
    item.button.setAttribute('aria-required', String(item.select.required));
    if (active === item) {
      if (item.button.disabled || item.select.hidden || !item.select.isConnected) close(); else renderOptions(item);
    }
  }

  function enhance(select) {
    if (controls.has(select) || select.multiple || select.size > 1 || select.dataset.nativeSelect != null) return;
    const wrapper = document.createElement('span'); wrapper.className = 'mh-select';
    if (select.classList.contains('filter-select')) wrapper.classList.add('mh-select-compact');
    const button = document.createElement('button'); button.type = 'button'; button.className = 'mh-select-trigger';
    button.setAttribute('role', 'combobox'); button.setAttribute('aria-haspopup', 'listbox'); button.setAttribute('aria-expanded', 'false');
    const label = document.createElement('span'); label.className = 'mh-select-value'; button.append(label);
    button.insertAdjacentHTML('beforeend', '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m5 6 3 3 3-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>');
    select.before(wrapper); wrapper.append(select, button);
    select.classList.add('mh-select-native'); select.tabIndex = -1; select.setAttribute('aria-hidden', 'true');
    const popup = document.createElement('div'); popup.className = 'mh-select-popup'; popup.hidden = true;
    if ('showPopover' in popup) popup.setAttribute('popover', 'manual');
    const search = document.createElement('input'); search.type = 'search'; search.className = 'mh-select-search'; search.placeholder = '搜索选项…'; search.autocomplete = 'off'; search.setAttribute('aria-label', '搜索选项');
    const list = document.createElement('div'); list.className = 'mh-select-list'; list.id = 'mh-select-list-' + ++sequence; list.setAttribute('role', 'listbox');
    button.setAttribute('aria-controls', list.id); search.setAttribute('aria-controls', list.id); search.setAttribute('role', 'combobox'); search.setAttribute('aria-expanded', 'true'); popup.append(search, list);
    const item = {select, wrapper, button, label, popup, search, list, current: -1, typed: '', typedAt: 0}; controls.set(select, item);
    for (const [key, descriptor] of descriptors) Object.defineProperty(select, key, {
      configurable: true, get() { return descriptor.get.call(this); }, set(value) { descriptor.set.call(this, value); queueSync(); }
    });
    button.addEventListener('click', () => open(item)); button.addEventListener('keydown', event => keyboard(item, event));
    search.addEventListener('keydown', event => keyboard(item, event)); search.addEventListener('input', () => renderOptions(item));
    list.addEventListener('pointerdown', event => event.preventDefault());
    list.addEventListener('click', event => { const row = event.target.closest('[role=option]'); if (row) commit(item, Number(row.dataset.index)); });
    select.addEventListener('change', () => sync(item)); select.addEventListener('focus', () => button.focus({preventScroll: true}));
    select.addEventListener('invalid', event => { event.preventDefault(); button.focus(); });
    const observer = new MutationObserver(() => sync(item)); observer.observe(select, {subtree: true, childList: true, attributes: true, characterData: true}); item.observer = observer;
    sync(item);
  }

  function refresh() {
    queued = false;
    for (const [select, item] of controls) {
      if (!select.isConnected) { if (active === item) close(); item.observer.disconnect(); item.popup.remove(); controls.delete(select); }
      else sync(item);
    }
  }
  function queueSync() { if (!queued) { queued = true; queueMicrotask(refresh); } }
  document.querySelectorAll('select').forEach(enhance);
  new MutationObserver(records => {
    for (const record of records) {
      for (const node of record.addedNodes) if (node.nodeType === Node.ELEMENT_NODE) {
        if (node.matches('select')) enhance(node); node.querySelectorAll('select').forEach(enhance);
      }
      if (record.removedNodes.length) for (const [select, item] of controls) if (!select.isConnected) {
        if (active === item) close(); item.observer.disconnect(); item.popup.remove(); controls.delete(select);
      }
    }
  }).observe(document.body, {childList: true, subtree: true});
  document.addEventListener('pointerdown', event => { if (active && !active.wrapper.contains(event.target) && !active.popup.contains(event.target)) close(); }, true);
  document.addEventListener('keydown', event => { if (active && event.key === 'Escape') keyboard(active, event); }, true);
  document.addEventListener('close', () => close(), true); document.addEventListener('reset', queueSync, true);
  window.addEventListener('resize', () => { if (active) place(active); });
  window.addEventListener('scroll', event => { if (active && (!(event.target instanceof Node) || !active.popup.contains(event.target))) place(active); }, true);
  window.visualViewport?.addEventListener('resize', () => { if (active) place(active); });
  window.ModelHarborSelects = {refresh: queueSync, close};
})();
