"use strict";

(() => {
  let accounts = [];
  let activeId = "";
  let editingId = null;
  let targetId = null;
  let busy = false;
  let loading = false;
  const menu = $("accountMenu");
  const trigger = $("accountMenuToggle");
  const editor = $("accountEditorDialog");
  const editIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15z"/></svg>';
  const deleteIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/></svg>';
  const initial = name => Array.from(name || "M")[0].toLocaleUpperCase();

  function closeMenu(restoreFocus = false) {
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus({ preventScroll: true });
  }

  function menuStatus(message = "") {
    $("accountMenuStatus").textContent = message;
    $("accountMenuStatus").hidden = !message;
  }

  function renderAccounts() {
    const current = accounts.find(item => item.id === activeId);
    trigger.textContent = initial(current?.display_name);
    trigger.disabled = busy || !accounts.length;
    trigger.title = current ? current.display_name + " · 账号菜单" : "账号菜单";
    trigger.setAttribute("aria-label", trigger.title);
    $("accountMenuAvatar").textContent = trigger.textContent;
    $("accountMenuName").textContent = current?.display_name || "账号暂不可用";
    $("accountMenuUsername").textContent = current ? "@" + current.username : "";
    $("accountList").innerHTML = accounts.map(item => {
      const active = item.id === activeId;
      const id = escapeHtml(item.id);
      const name = escapeHtml(item.display_name);
      return '<div class="account-row' + (active ? ' is-current' : '') + '">' +
        '<button type="button" class="account-select" data-account-action="switch" data-id="' + id + '"' +
        (active ? ' aria-current="true"' : '') + (busy ? ' disabled' : '') + '>' +
        '<span class="account-row-avatar" aria-hidden="true">' + escapeHtml(initial(item.display_name)) + '</span>' +
        '<span class="account-identity"><strong>' + name + '</strong><span>' +
        (active ? '当前使用' : item.require_password ? '切换需验证' : '快捷切换') + '</span></span>' +
        (active ? '<span class="account-current" aria-hidden="true">✓</span>' : '') + '</button>' +
        '<div class="account-row-actions"><button type="button" data-account-action="edit" data-id="' + id +
        '" aria-label="编辑 ' + name + '" title="编辑账号"' + (busy ? ' disabled' : '') + '>' + editIcon + '</button>' +
        (!active && item.id !== 'default' ? '<button type="button" data-account-action="delete" data-id="' + id +
        '" aria-label="删除 ' + name + '" title="删除账号"' + (busy ? ' disabled' : '') + '>' + deleteIcon + '</button>' : '') + '</div></div>';
    }).join("");
    $("accountAdd").disabled = busy;
    $("accountEditCurrent").disabled = busy || !current;
  }

  window.refreshAccounts = async function () {
    try {
      const result = await api('/admin/accounts');
      if (window.modelHarborAccount && window.modelHarborAccount.revision !== result.revision) {
        window.location.reload();
        return;
      }
      accounts = result.accounts;
      activeId = result.active_id;
      window.modelHarborAccount = { id: activeId, revision: result.revision };
      menuStatus();
      renderAccounts();
    } catch (error) {
      menuStatus(error.message);
      trigger.disabled = false;
    }
  };

  async function editAccount(id = null) {
    if (busy || loading) return;
    loading = true;
    try {
      const account = id ? await api('/admin/accounts/' + encodeURIComponent(id)) : null;
      editingId = id;
      $("accountForm").reset();
      $("accountDisplay").value = account?.display_name || '';
      $("accountUsername").value = account?.username || '';
      $("accountRequirePassword").checked = !!account?.require_password;
      $("accountIps").value = (account?.allowed_ips || []).join('\n');
      $("accountIps").disabled = id === activeId;
      $("accountIpHelp").textContent = id === activeId
        ? '当前账号的白名单请在「IP 白名单」中修改。'
        : '仅名单中的 IP 可访问；切换前请填入当前 IP。留空将禁止访问，不会继承其他账号的地址。';
      $("accountEditorTitle").textContent = id ? '编辑账号' : '新建账号';
      $("accountFormStatus").textContent = '';
      closeMenu();
      editor.showModal();
      $("accountDisplay").focus({ preventScroll: true });
    } catch (error) { menuStatus(error.message); }
    finally { loading = false; }
  }

  async function performSwitch(id, credentials = {}) {
    if (busy || !id) return;
    busy = true;
    renderAccounts();
    $("switchConfirm").disabled = true;
    menuStatus();
    try {
      await api('/admin/accounts/' + encodeURIComponent(id) + '/switch', 'POST', true, credentials);
      $("switchPassword").value = '';
      $("accountSwitchDialog").close();
      closeMenu();
      window.location.reload();
    } catch (error) {
      if ($("accountSwitchDialog").open) $("switchAccountError").textContent = error.message;
      else menuStatus(error.message);
    } finally {
      busy = false;
      renderAccounts();
      $("switchConfirm").disabled = false;
    }
  }

  function beginSwitch(id) {
    const account = accounts.find(item => item.id === id);
    if (!account || id === activeId || busy) return;
    if (!account.require_password) { performSwitch(id); return; }
    targetId = id;
    $("switchAccountTitle").textContent = '切换至 ' + account.display_name;
    $("switchUsername").value = account.username;
    $("switchPassword").value = '';
    $("switchAccountError").textContent = '';
    closeMenu();
    $("accountSwitchDialog").showModal();
    $("switchPassword").focus();
  }

  trigger.addEventListener('click', () => {
    if (!menu.hidden) { closeMenu(); return; }
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    if (!accounts.length) window.refreshAccounts();
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('.account-switcher')) closeMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !menu.hidden) { event.preventDefault(); closeMenu(true); }
  });
  document.querySelector('.account-switcher').addEventListener('focusout', event => {
    if (!event.currentTarget.contains(event.relatedTarget)) closeMenu();
  });
  trigger.addEventListener('keydown', event => {
    if (event.key !== 'ArrowDown') return;
    event.preventDefault();
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    menu.querySelector('button:not(:disabled)')?.focus();
  });
  menu.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const buttons = [...menu.querySelectorAll('button:not(:disabled)')];
    const index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
      : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next].focus();
  });
  $("accountAdd").addEventListener('click', () => editAccount());
  $("accountEditCurrent").addEventListener('click', () => editAccount(activeId));
  $("accountCancel").addEventListener('click', () => editor.close());
  $("accountEditorClose").addEventListener('click', () => editor.close());
  editor.addEventListener('close', () => {
    $("accountPassword").value = '';
    trigger.focus({ preventScroll: true });
  });
  $("accountList").addEventListener('click', async event => {
    const button = event.target.closest('button[data-account-action]');
    if (!button || busy) return;
    const { id, accountAction } = button.dataset;
    if (accountAction === 'switch') beginSwitch(id);
    if (accountAction === 'edit') editAccount(id);
    if (accountAction === 'delete') {
      const account = accounts.find(item => item.id === id);
      if (!confirm('删除账号「' + account.display_name + '」？其历史文件保留在本机。')) return;
      try {
        busy = true;
        renderAccounts();
        await api('/admin/accounts/' + encodeURIComponent(id), 'DELETE');
        await window.refreshAccounts();
        showToast('账号已删除');
      } catch (error) { menuStatus(error.message); }
      finally { busy = false; renderAccounts(); }
    }
  });
  $("accountForm").addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    const body = {
      display_name: $("accountDisplay").value.trim(),
      username: $("accountUsername").value.trim(),
      password: $("accountPassword").value,
      require_password: $("accountRequirePassword").checked,
    };
    if (editingId !== activeId) body.allowed_ips = $("accountIps").value.split(/[\s,，;]+/).filter(Boolean);
    busy = true;
    $("accountSave").disabled = true;
    editor.setAttribute('aria-busy', 'true');
    try {
      await api('/admin/accounts' + (editingId ? '/' + encodeURIComponent(editingId) : ''), editingId ? 'PUT' : 'POST', true, body);
      $("accountPassword").value = '';
      await window.refreshAccounts();
      editor.close();
      showToast('账号已保存');
    } catch (error) { $("accountFormStatus").textContent = error.message; }
    finally {
      busy = false;
      $("accountSave").disabled = false;
      editor.setAttribute('aria-busy', 'false');
      renderAccounts();
    }
  });
  $("accountSwitchForm").addEventListener('submit', event => {
    event.preventDefault();
    performSwitch(targetId, { username: $("switchUsername").value, password: $("switchPassword").value });
  });
  $("switchCancel").addEventListener('click', () => $("accountSwitchDialog").close());
  $("accountSwitchDialog").addEventListener('close', () => {
    $("switchPassword").value = '';
    trigger.focus({ preventScroll: true });
  });
  window.refreshAccounts().then(async () => {
    await refreshCapabilities();
    refresh(true);
  });
})();
