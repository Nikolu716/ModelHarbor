/* Visual tests: manual by default; automatic plans require explicit saving. */
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const dialog = el("visualTestDialog");
  let station = null, templates = [], result = null, busy = false, loading = false, loadFailed = false;
  let generation = 0, view = "preview", nextAllowed = 0, returnFocus = null;
  let plan = null, saving = false, polling = false, submitting = false;
  let galleryPage = 1, galleryKey = "", galleryCapacity = 1, galleryRefreshing = false;
  let modelStation = null, lastPoll = 0;
  const cache = new Map();
  const thumbnailLoads = new Map();
  const tablePreviews = new Map();
  const thumbnailSizes = new ResizeObserver(entries => entries.forEach(({target}) => {
    const frame = target.querySelector("iframe");
    if (frame) frame.style.transform = `translate(-50%,-50%) scale(${Math.min(target.clientWidth / 640, target.clientHeight / 400)})`;
  }));
  const path = (name) => `/admin/station/${encodeURIComponent(name)}/visual-test`;
  const template = () => templates.find((item) => item.id === el("visualTestTemplate").value);
  const status = (text, error = false) => {
    el("visualTestStatus").textContent = text;
    el("visualTestStatus").classList.toggle("text-danger", error);
  };

  const modelsOf = item => {
    const candidates = [...new Set(item.probe_model_candidates || [...(item.detected_models || []), ...(item.models || [])])];
    return candidates.filter(model => (!item.effective_models || item.effective_models.includes(model))
      && (item.provider !== 'opencode' || item.model_api_paths?.[model]?.length));
  };
  function modelMarkup(item) {
    const models = modelsOf(item), name = escapeHtml(item.name);
    return `<button class="channel-model-link" type="button" data-channel-models="${name}" aria-label="查看 ${name} 的完整模型列表"><span>${models.length ? escapeHtml(models[0]) : "暂无模型目录"}</span><small>${models.length ? `${models.length} 个模型 ↗` : "查看详情 ↗"}</small></button>`;
  }

  function syncTablePreviews() {
    const names = new Set(STATIONS.map(item => item.name));
    for (const name of tablePreviews.keys()) if (!names.has(name)) tablePreviews.delete(name);
    for (const cell of document.querySelectorAll("[data-table-visual]")) {
      const item = STATIONS.find(item => item.name === cell.dataset.tableVisual);
      if (!item) continue;
      const s = item.visual_test, running = item.visual_running;
      const key = JSON.stringify([s?.id, s?.preview_available, s?.model]);
      let entry = tablePreviews.get(item.name);
      if (!entry || entry.key !== key) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `visual-table-preview${s?.preview_available ? " has-preview" : ""}${running ? " is-running" : ""}`;
        button.dataset.visualTest = item.name;
        button.dataset.visualOpen = s || running ? "result" : "config";
        const label = s ? `查看 ${item.name} 的智商测试结果` : `配置 ${item.name} 的智商测试`;
        button.setAttribute("aria-label", label);
        button.title = [label, s?.model, s?.created_at ? new Date(s.created_at * 1000).toLocaleString() : "", s?.configuration_changed ? "连接配置已变化，保留历史结果" : ""].filter(Boolean).join(" · ");
        if (s?.preview_available) button.dataset.visualThumbnail = s.id;
        const canvas = document.createElement("span");
        canvas.className = "visual-thumbnail-image";
        canvas.textContent = s?.preview_available ? "读取预览…" : running ? "生成中…" : s ? "查看结果 ↗" : "未测试";
        button.append(canvas);
        entry = {key, button}; tablePreviews.set(item.name, entry);
      }
      entry.button.classList.toggle("is-running", Boolean(running));
      entry.button.setAttribute("aria-busy", String(Boolean(running)));
      entry.button.dataset.visualOpen = s || running ? "result" : "config";
      const dot = entry.button.querySelector(".visual-thumbnail-running");
      if (running && !dot) {
        const marker = document.createElement("span"); marker.className = "visual-thumbnail-running"; marker.title = "后台生成中";
        entry.button.append(marker);
      } else if (!running) dot?.remove();
      if (!s?.preview_available) entry.button.querySelector(".visual-thumbnail-image").textContent = running ? "生成中…" : s ? "查看结果 ↗" : "未测试";
      if (cell.firstElementChild !== entry.button) cell.replaceChildren(entry.button);
    }
    observeThumbnails();
  }

  function renderModels() {
    const query = el("channelModelsSearch").value.trim().toLowerCase();
    const models = [...new Set([...(modelStation.detected_models || []), ...modelsOf(modelStation)])], filtered = models.filter(m => m.toLowerCase().includes(query));
    el("channelModelsCount").textContent = `${filtered.length} / ${models.length} 个模型`;
    el("channelModelsList").replaceChildren(...(filtered.length ? filtered.map(model => {
      const node = document.createElement("div");
      node.textContent = model;
      const reason = modelStation.model_unavailable?.[model];
      const paths = modelStation.model_api_paths?.[model];
      const limited = modelStation.effective_models && !modelStation.effective_models.includes(model);
      const detail = reason || (limited ? '未在当前路由范围' : paths?.map(p => ({'/responses':'Responses','/chat/completions':'Chat Completions','/messages':'Anthropic Messages','/generateContent':'Gemini GenerateContent'}[p] || p)).join(' / '));
      if (detail) { const small = document.createElement('small'); small.textContent = detail; node.append(small); }
      return node;
    }) : [Object.assign(document.createElement("p"), {textContent: query ? "没有匹配的模型" : "尚未读取模型目录，可在站点管理中读取或手动补充。"})]));
  }
  function openModels(name) {
    modelStation = STATIONS.find(item => item.name === name) || statusData?.stations?.find(item => item.name === name);
    if (!modelStation) return;
    el("channelModelsStation").textContent = name;
    el("channelModelsSearch").value = "";
    el("channelModelsNote").textContent = modelStation.provider === 'opencode' && modelStation.opencode_plan === 'go'
      ? 'Go 文本模型支持 Responses、Chat、Messages 与 Gemini；上游负责跨协议适配。目录可见不代表订阅额度、区域和隐私权限均可用；查看不会生成请求。'
      : "来自渠道模型目录与手动配置，查看不会发起生成请求。实际可用性以渠道响应为准。";
    renderModels(); el("channelModelsDialog").showModal();
  }
  el("channelModelsSearch").addEventListener("input", renderModels);
  el("channelModelsClose").addEventListener("click", () => el("channelModelsDialog").close());

  const gallery = el("monitor-visual"), grid = el("visualGalleryGrid");
  el("monitor-wrap").append(gallery);
  const nav = document.querySelector('[data-tab-group="monitor"]');
  nav.insertAdjacentHTML("beforeend", '<button id="monitor-tab-visual" type="button" class="section-tab" data-section="visual" role="tab" aria-controls="monitor-visual" aria-selected="false" tabindex="-1">智商测试</button>');
  nav.addEventListener("click", () => requestAnimationFrame(renderGallery));
  nav.addEventListener("keydown", () => requestAnimationFrame(renderGallery));

  function renderGallery() {
    if (!gallery.getClientRects().length) return;
    const query = el("visualGallerySearch").value.trim().toLowerCase();
    const items = STATIONS.filter(item => `${item.name} ${modelsOf(item).join(" ")}`.toLowerCase().includes(query));
    const columns = Math.min(3, Math.max(1, Math.floor((grid.clientWidth + 16) / 280)));
    const rows = Math.min(2, Math.max(1, Math.floor((grid.clientHeight + 16) / 260)));
    galleryCapacity = columns * rows;
    const pages = Math.max(1, Math.ceil(items.length / galleryCapacity));
    galleryPage = Math.min(pages, Math.max(1, galleryPage));
    const visible = items.slice((galleryPage - 1) * galleryCapacity, galleryPage * galleryCapacity);
    grid.style.setProperty("--visual-columns", columns);
    grid.style.setProperty("--visual-rows", Math.min(rows, Math.max(1, Math.ceil(visible.length / columns))));
    const key = JSON.stringify([galleryPage, columns, rows, visible.map(s => [s.name, s.enabled, modelsOf(s), s.visual_test, s.visual_running, s.visual_model, s.visual_job?.id, s.visual_schedule])]);
    if (key !== galleryKey) {
      galleryKey = key;
      grid.innerHTML = visible.map(item => {
        const s = item.visual_test, name = escapeHtml(item.name), running = item.visual_running;
        const state = running ? "生成中" : !item.enabled ? "已停用" : s ? s.ok ? "已完成" : "未完成" : "未测试";
        const model = running ? item.visual_model || s?.model : s?.model;
        const auto = item.visual_schedule?.enabled ? item.visual_schedule.paused_reason ? "自动已暂停" : "自动计划已开启" : "手动测试";
        return `<article class="visual-gallery-card${running ? " is-running" : ""}">
          <header><div><h3 title="${name}">${name}</h3><span title="${escapeHtml(model || "")}">${escapeHtml(model || "选择模型后开始测试")}</span></div><span class="visual-card-state${running ? " is-running" : ""}">${state}</span></header>
          <button type="button" class="visual-card-canvas" data-visual-test="${name}" data-visual-open="result" ${s?.preview_available ? `data-visual-thumbnail="${escapeHtml(s.id)}"` : ""} aria-busy="${Boolean(running)}" aria-label="查看 ${name} 的生成结果"><span class="visual-canvas-placeholder">${s?.preview_available ? "正在读取预览…" : running ? "正在生成，可离开此页面" : s ? "查看模型返回与详情 ↗" : "暂无生成预览"}</span></button>
          <div class="visual-card-meta"><span>${s ? new Date(s.created_at * 1000).toLocaleString([], {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}) : "尚未发送请求"}</span><span>${s?.latency_ms != null ? `${(s.latency_ms / 1000).toFixed(1)} s` : ""}</span></div>
          <footer><button type="button" class="visual-card-models" data-channel-models="${name}">${modelsOf(item).length} 个模型 ↗</button><span title="${auto}">${auto}</span><button type="button" class="button secondary" data-visual-test="${name}" data-visual-open="config">${running ? "设置 / 进度" : "测试设置"}</button></footer>
        </article>`;
      }).join("") || '<div class="visual-gallery-empty">没有匹配的渠道</div>';
      observeThumbnails();
    }
    el("visualGalleryStatus").textContent = `各渠道最近一次生成 · ${STATIONS.filter(item => item.visual_running).length} 个进行中`;
    el("visualGalleryPage").textContent = `第 ${galleryPage} / ${pages} 页 · ${items.length} 个渠道`;
    el("visualGalleryPrev").disabled = galleryPage === 1;
    el("visualGalleryNext").disabled = galleryPage === pages;
  }
  el("visualGallerySearch").addEventListener("input", () => { galleryPage = 1; renderGallery(); });
  el("visualGalleryPrev").addEventListener("click", () => { galleryPage--; renderGallery(); });
  el("visualGalleryNext").addEventListener("click", () => { galleryPage++; renderGallery(); });
  new ResizeObserver(() => requestAnimationFrame(renderGallery)).observe(grid);

  function setView(value) {
    view = value;
    for (const button of dialog.querySelectorAll("[data-visual-tab]")) {
      const active = button.dataset.visualTab === value;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      el(`visual-panel-${button.dataset.visualTab}`).hidden = !active;
    }
  }

  function showResult(value) {
    result = value;
    const frame = el("visualPreviewFrame");
    frame.hidden = !value?.preview_html;
    el("visualPreviewEmpty").hidden = Boolean(value?.preview_html);
    if (value?.preview_html) frame.srcdoc = value.preview_html;
    else frame.removeAttribute("srcdoc");
    el("visual-panel-source").textContent = value?.artifact || "暂无完整 HTML / SVG。可在「原始返回」查看模型输出。";
    const noText = Boolean(value && !value.raw_text);
    el("visual-panel-raw").textContent = value?.raw_response || value?.raw_text || (value
      ? "本次请求已有结果记录，但没有返回可见正文。此历史记录未保存 HTTP / SSE 原文，无法补回。"
      : "尚未发送测试请求。");
    el("visualResultDiagnosis").hidden = !noText;
    const completedRequest = value?.http_status >= 200 && value?.http_status < 300;
    el("visualDiagnosisTitle").textContent = value?.truncated ? "达到本次输出 Token 上限，尚未生成正文" : completedRequest ? "请求已结束，未生成可见正文" : "本次测试未完成";
    const usage = value?.usage || {};
    el("visualDiagnosisText").textContent = !completedRequest ? (value?.error || "没有返回可预览的内容") : "输入 " + (usage.input_tokens ?? "—") + " · 输出 " + (usage.output_tokens ?? "—") + " Token；没有 HTML / SVG 可供预览。" +
      (value?.diagnostics?.reasoning_observed || value?.diagnostics?.reasoning_tokens > 0
        ? "上游报告了推理内容。" : "这些输出是否用于内部推理，当前记录无法确认。");
    el("visualDiagnosisEvidence").textContent = (value?.diagnostics?.finish_reasons?.length ? "结束原因：" + value.diagnostics.finish_reasons.join(" / ") + "。" : "") +
      (completedRequest ? "可降低模型支持的推理深度，或手动增加输出上限后再测；不会自动重试。" : "请根据上游错误调整配置；未自动重试或切换线路。");
    el("visualTestCopy").disabled = !value?.raw_text;
    el("visualTestClear").hidden = !value;
    el("visualResultContext").hidden = !value;
    el("visualResultContext").textContent = value ? `${value.source === "automatic" ? "自动" : "手动"} · ${value.model} · ${({responses:"Responses",messages:"Messages",generate_content:"Gemini",chat_completions:"Chat",chat_completions_legacy:"Chat"}[value.protocol] || value.protocol)} · 推理 ${value.effective_reasoning_effort || "上游默认"} · ${new Date(value.created_at * 1000).toLocaleString()}` : "";
    const messages = [];
    if (value?.configuration_changed) messages.push("连接配置已变化，当前显示保留的历史结果");
    if (value?.error && !noText) messages.push(value.error);
    if (value?.truncated && !noText) messages.push("输出达到上限或未完成，代码可能不完整");
    if (value?.ok && !value?.artifact) messages.push("已返回文本，未识别到完整 HTML / SVG，请查看原始返回");
    if (value?.artifact) messages.push("静态预览禁用脚本、外部资源和网络请求。");
    if (value?.raw_response_truncated) messages.push("响应原文超过展示上限，仅保存前 256K 字符");
    el("visualResultNotice").textContent = messages.join("；");
    el("visualResultNotice").hidden = !messages.length;
    el("visualResultNotice").classList.toggle("text-danger", Boolean(value?.error));
    const metrics = value ? [
      ["状态", noText ? "无可见正文" : value.error ? "未完成" : value.truncated ? "输出不完整" : value.kind ? value.kind.toUpperCase() : "文本", value.http_status ? `HTTP ${value.http_status}` : ""],
      ["耗时", `${(value.latency_ms / 1000).toFixed(1)} s`, new Date(value.created_at * 1000).toLocaleString()],
      ["Token", value.usage?.total_tokens == null ? "上游未报告" : Number(value.usage.total_tokens).toLocaleString(), `输入 ${value.usage?.input_tokens ?? "—"} / 输出 ${value.usage?.output_tokens ?? "—"}`],
    ] : [["状态", "等待测试", ""], ["耗时", "—", ""], ["Token", "—", ""]];
    el("visualResultMetrics").innerHTML = metrics.map(([label, content, note]) => `<div><dt>${escapeHtml(label)}</dt><dd title="${escapeHtml(note)}">${escapeHtml(content)}</dd></div>`).join("");
    if (value?.error || value && !value.artifact) setView("raw");
    else setView("preview");
  }

  el("visualAdjustSettings").addEventListener("click", () => { setWorkspace("config"); setConfig("options"); el("visualTestLimit").focus(); });

  function applyTemplate(id) {
    el("visualTestTemplate").value = id;
    const item = template();
    el("visualTestPrompt").value = item?.prompt || "";
    el("visualTemplateName").value = item && !item.builtin ? item.name : "";
    el("visualTemplateUpdate").hidden = !item || item.builtin;
    el("visualTemplateDelete").hidden = !item || item.builtin;
    el("visualTemplateStatus").textContent = "";
    el("visualPromptNote").textContent = item?.builtin
      ? "经典题目只要求生成 SVG；可临时修改，也可另存为自己的模板。"
      : "模板仅保存在当前账号。建议要求模型输出完整 HTML 或 SVG。";
  }

  function renderTemplates(selected) {
    el("visualTestTemplate").replaceChildren(...templates.map((item) => new Option(item.name, item.id)), new Option("临时 / 新建提示词", "draft"));
    applyTemplate(selected || templates[0]?.id || "draft");
  }

  function syncControls() {
    for (const control of el("visualTestForm").querySelectorAll("input,textarea,select,button")) control.disabled = loading || saving || submitting;
    el("visualTestClose").disabled = false;
    el("visualTestClear").disabled = busy || loading;
    el("visualTestRun").disabled = busy || submitting || loading || saving || loadFailed || !station?.enabled || !el("visualTestModel").options.length;
    el("visualScheduleSave").disabled = loading || saving || submitting || loadFailed;
    el("visualTestRun").textContent = busy ? "生成中…" : "开始测试";
    el("visualTestRun").classList.toggle("is-generating", busy);
    el("visualTestForm").setAttribute("aria-busy", String(busy || loading));
    syncProtocolOptions();
  }

  function syncProtocolOptions() {
    if (!station) return;
    const paths = station.model_api_paths?.[el("visualTestModel").value]
      || (station.provider === 'anthropic' ? ['/messages'] : station.provider === 'gemini' ? ['/generateContent'] : null);
    const mapping = {responses:'/responses',chat_completions:'/chat/completions',chat_completions_legacy:'/chat/completions',messages:'/messages',generate_content:'/generateContent'};
    for (const option of el('visualTestProtocol').options) {
      option.disabled = option.value !== 'auto' && (paths ? !paths.includes(mapping[option.value]) : ['messages','generate_content'].includes(option.value));
    }
    if (el('visualTestProtocol').selectedOptions[0]?.disabled) el('visualTestProtocol').value = 'auto';
    const path = mapping[el('visualTestProtocol').value] || paths?.[0];
    const native = ['/messages','/generateContent'].includes(path);
    for (const option of el('visualTestReasoning').options) option.disabled = native && !['auto','omit'].includes(option.value);
    if (el('visualTestReasoning').selectedOptions[0]?.disabled) el('visualTestReasoning').value = 'auto';
    el('visualProtocolNote').textContent = native
      ? '原生思考参数随模型而异：使用上游默认，或跟随站点配置。不套用 OpenAI 推理等级。'
      : '参数不受支持时直接显示错误，不会自动重发。';
  }
  el('visualTestProtocol').addEventListener('change', syncProtocolOptions);
  el('visualTestModel').addEventListener('change', syncProtocolOptions);

  function setConfig(value) {
    el("visual-config-content").hidden = value !== "content";
    el("visual-config-options").hidden = value !== "options";
    el("visual-config-schedule").hidden = value !== "schedule";
    for (const button of dialog.querySelectorAll("[data-visual-config]")) button.setAttribute("aria-pressed", String(button.dataset.visualConfig === value));
    dialog.querySelector(".visual-config-scroll").scrollTop = 0;
  }
  for (const button of dialog.querySelectorAll("[data-visual-config]")) button.addEventListener("click", () => setConfig(button.dataset.visualConfig));

  function formSettings() {
    return { model: el("visualTestModel").value, prompt: el("visualTestPrompt").value,
      template_name: template()?.name || el("visualTemplateName").value.trim() || "临时提示词",
      protocol: el("visualTestProtocol").value, max_output_tokens: Number(el("visualTestLimit").value),
      reasoning_effort: el("visualTestReasoning").value };
  }

  function validContent() {
    const limit = el("visualTestLimit");
    limit.setCustomValidity(Number.isSafeInteger(Number(limit.value)) && Number(limit.value) > 0 ? "" : "请填写可精确表示的正整数");
    const invalid = [el("visualTestPrompt"), el("visualTestLimit")].find(control => !control.checkValidity());
    if (!invalid) return true;
    setWorkspace("config"); setConfig(invalid.id === "visualTestLimit" ? "options" : "content"); invalid.reportValidity();
    return false;
  }
  el("visualTestLimit").addEventListener("input", () => el("visualTestLimit").setCustomValidity(""));

  function showPlan(value, fill = false) {
    plan = value;
    el("visualScheduleDot").hidden = !value?.enabled;
    if (fill) {
      el("visualScheduleEnabled").checked = Boolean(value?.enabled);
      el("visualScheduleInterval").value = (value?.interval_seconds || 3600) / 60;
      el("visualScheduleFeedback").textContent = "";
    }
    const settings = value?.settings;
    el("visualScheduleSummary").textContent = settings ? `${settings.model} · ${settings.template_name} · 推理 ${settings.reasoning_effort} · 最多 ${settings.max_output_tokens} Token · 间隔 ${value.interval_seconds / 60} 分钟` : "尚未保存模型与提示词";
    const labels = {disabled:"仅手动测试", inactive:"账号未启用，自动计划不执行", waiting:"站点已停用，等待启用", running:"正在测试", paused:"已暂停"};
    const state = labels[value?.state] || "已开启自动测试";
    el("visualScheduleStatus").textContent = [state, value?.paused_reason, value?.next_run_at ? `下次执行不早于 ${new Date(value.next_run_at * 1000).toLocaleString()}` : "", value?.last_error ? `上次提示：${value.last_error}` : ""].filter(Boolean).join("。 ");
  }

  el("visualScheduleSave").addEventListener("click", async () => {
    if (loading || saving || loadFailed || !station) return;
    if (!el("visualScheduleInterval").reportValidity()) return;
    const enabled = el("visualScheduleEnabled").checked;
    if (enabled && !validContent()) return;
    const body = {enabled, interval_seconds: Number(el("visualScheduleInterval").value) * 60};
    if (enabled) body.settings = formSettings();
    const token = generation, name = station.name;
    saving = true; syncControls();
    try {
      const saved = await api(`${path(name)}/schedule`, "PUT", true, body);
      if (token !== generation) return;
      showPlan(saved, true);
      el("visualScheduleFeedback").textContent = enabled ? "已保存。首次执行会等待完整间隔，当前没有发送模型请求。" : "已关闭自动测试。";
      await refreshStations({force:true});
    } catch (error) { if (token === generation) el("visualScheduleFeedback").textContent = error.message; }
    finally { if (token === generation) { saving = false; syncControls(); } }
  });

  setInterval(async () => {
    if (!dialog.open || !station || loading || saving || submitting || polling || loadFailed || document.hidden) return;
    if (!busy && Date.now() - lastPoll < 15000) return;
    lastPoll = Date.now();
    const token = generation, name = station.name;
    polling = true;
    try {
      const value = await api(path(name));
      if (generation !== token || !dialog.open || saving) return;
      const wasBusy = busy;
      busy = value.running; nextAllowed = value.next_allowed_at || 0;
      showPlan(value.schedule);
      if (value.result?.id !== result?.id) { showResult(value.result); cache.delete(name); }
      if (busy) status("后台生成中，可关闭窗口或切换页面");
      else if (wasBusy) {
        status(value.job?.error || "测试完成", Boolean(value.job?.error));
        await refreshStations({force:true});
      }
      syncControls();
    } catch { /* Read failures never cause generation or overwrite unsaved settings. */ }
    finally { polling = false; }
  }, 2000);

  setInterval(async () => {
    if (document.hidden || galleryRefreshing || (!gallery.getClientRects().length && !el("monitor-stations").getClientRects().length && !dialog.open && !STATIONS.some(s => s.visual_running))) return;
    galleryRefreshing = true;
    try { await refreshStations(); } finally { galleryRefreshing = false; }
  }, 6000);

  async function open(name, trigger, workspace = "config") {
    const token = ++generation;
    returnFocus = trigger || document.activeElement;
    station = STATIONS.find((item) => item.name === name);
    if (!station) return;
    loading = true;
    busy = false; saving = false; submitting = false;
    loadFailed = false;
    setWorkspace(workspace);
    setConfig("content");
    showPlan(null, true);
    nextAllowed = 0;
    el("visualTestStation").textContent = name;
    el("visualTestProtocol").value = "auto";
    el("visualTestReasoning").value = "auto";
    el("visualTestLimit").value = "4096";
    const models = station.probe_model_candidates || [...new Set([...(station.detected_models || []), ...(station.models || [])])];
    el("visualTestModel").replaceChildren(...models.map((model) => new Option(model, model)));
    const selected = station.generation_probe?.model || station.probe_chat_model || models[0];
    if (models.includes(selected)) el("visualTestModel").value = selected;
    showResult(null);
    status("读取模板与上次结果…");
    syncControls();
    if (!dialog.open) dialog.showModal();
    try {
      const [prompts, latest] = await Promise.all([api("/admin/visual-tests/prompts"), api(path(name))]);
      if (generation !== token || !dialog.open) return;
      templates = prompts;
      renderTemplates();
      showResult(latest.result);
      showPlan(latest.schedule, true);
      busy = latest.running;
      const settings = (latest.running && latest.job?.state === "running" ? latest.job.settings : null) || latest.schedule?.settings || latest.result;
      if (settings) {
        if (!models.includes(settings.model)) el("visualTestModel").add(new Option(`${settings.model} · 不在当前目录`, settings.model));
        el("visualTestModel").value = settings.model;
        el("visualTestReasoning").value = settings.reasoning_effort || "auto";
        el("visualTestLimit").value = settings.max_output_tokens || 4096;
        el("visualTestProtocol").value = settings.protocol || "auto";
        const match = templates.find(item => item.prompt === settings.prompt);
        applyTemplate(match?.id || "draft");
        el("visualTestPrompt").value = settings.prompt;
        if (!match) el("visualTemplateName").value = settings.template_name || "";
      }
      nextAllowed = latest.next_allowed_at || 0;
      status(latest.running ? "后台生成中，可关闭窗口或切换页面" : !station.enabled ? "站点已停用，可浏览结果；启用后才能测试" : !models.length ? "请先读取或补充本站模型" : latest.job?.error || (latest.result ? `上次测试模型：${latest.result.model}${latest.result.configuration_changed ? " · 连接配置已变化，保留历史结果" : ""}` : "尚未发送请求"));
    } catch (error) {
      if (generation !== token) return;
      loadFailed = true;
      status(`读取测试配置失败：${error.message}`, true);
    } finally {
      if (generation === token) { loading = false; syncControls(); }
    }
  }

  function close() {
    ++generation;
    dialog.close();
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
  }
  el("visualTestClose").addEventListener("click", close);
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-visual-test]");
    if (button) open(button.dataset.visualTest, button, button.dataset.visualOpen || "config");
    const models = event.target.closest("[data-channel-models]");
    if (models) openModels(models.dataset.channelModels);
  });
  el("sf-visual-test").addEventListener("click", (event) => {
    if (editingStation) {
      setTab("monitor"); setSectionTab("monitor", "visual"); renderGallery();
      open(editingStation, event.currentTarget);
    }
  });
  el("visualTestTemplate").addEventListener("change", () => applyTemplate(el("visualTestTemplate").value));
  el("visualTemplateNew").addEventListener("click", () => { applyTemplate("draft"); el("visualTestPrompt").focus(); });

  async function saveTemplate(update) {
    const token = generation;
    const item = template();
    const body = { name: el("visualTemplateName").value.trim(), prompt: el("visualTestPrompt").value.trim() };
    if (!body.name || !body.prompt) { el("visualTemplateStatus").textContent = "请填写模板名称与提示词"; return; }
    const button = el(update ? "visualTemplateUpdate" : "visualTemplateSave");
    button.disabled = true;
    try {
      const saved = await api(`/admin/visual-tests/prompts${update ? `/${encodeURIComponent(item.id)}` : ""}`, update ? "PUT" : "POST", true, body);
      const refreshed = await api("/admin/visual-tests/prompts");
      if (generation !== token) return;
      templates = refreshed;
      renderTemplates(saved.id);
      el("visualTemplateStatus").textContent = "已保存到当前账号";
    } catch (error) { if (generation === token) el("visualTemplateStatus").textContent = error.message; }
    finally { if (generation === token) button.disabled = false; }
  }
  el("visualTemplateSave").addEventListener("click", () => saveTemplate(false));
  el("visualTemplateUpdate").addEventListener("click", () => saveTemplate(true));
  el("visualTemplateDelete").addEventListener("click", async () => {
    const token = generation;
    const item = template();
    if (!item || item.builtin || !window.confirm(`删除模板“${item.name}”？已生成的站点结果不受影响。`)) return;
    try {
      await api(`/admin/visual-tests/prompts/${encodeURIComponent(item.id)}`, "DELETE");
      const refreshed = await api("/admin/visual-tests/prompts");
      if (generation !== token) return;
      templates = refreshed;
      renderTemplates();
    } catch (error) { if (generation === token) el("visualTemplateStatus").textContent = error.message; }
  });

  el("visualTestForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || loading || saving || loadFailed || !station?.enabled || !validContent()) return;
    const wait = Math.ceil(nextAllowed - Date.now() / 1000);
    if (wait > 0) { status(`请等待 ${wait} 秒再测试`, true); return; }
    const name = station.name, token = generation;
    submitting = true;
    syncControls();
    status("正在提交后台任务…");
    const body = formSettings();
    try {
      await api(path(name), "POST", false, body);
      if (token === generation) {
        busy = true;
        status("后台生成中，可关闭窗口或切换页面");
      }
      showToast(`${name} 已开始后台测试`);
      await refreshStations({ force: true });
    } catch (error) {
      if (token === generation) status(`提交未确认：${error.message}。请重新查看进度。`, true);
      else showToast(`请重新查看 ${name} 的测试进度：${error.message}`, true);
    }
    finally { if (token === generation) { submitting = false; syncControls(); } }
  });

  function setWorkspace(value) {
    dialog.dataset.view = value;
    el("visualWorkspace").dataset.view = value;
    for (const button of dialog.querySelectorAll("[data-visual-workspace]")) button.setAttribute("aria-pressed", String(button.dataset.visualWorkspace === value));
  }
  for (const button of dialog.querySelectorAll("[data-visual-workspace]")) button.addEventListener("click", () => setWorkspace(button.dataset.visualWorkspace));

  for (const button of dialog.querySelectorAll("[data-visual-tab]")) {
    button.addEventListener("click", () => setView(button.dataset.visualTab));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...dialog.querySelectorAll("[data-visual-tab]")];
      const index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (tabs.indexOf(button) + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      setView(tabs[index].dataset.visualTab); tabs[index].focus();
    });
  }
  el("visualTestCopy").addEventListener("click", async () => {
    try {
      const text = view === "raw" ? result.raw_text : result.artifact || result.raw_text;
      if (navigator.clipboard?.writeText && window.isSecureContext) await navigator.clipboard.writeText(text);
      else {
        const field = document.createElement("textarea");
        field.className = "clipboard-helper"; field.value = text;
        dialog.append(field); field.select();
        try { if (!document.execCommand("copy")) throw new Error("浏览器未允许复制，请在代码页手动选择"); }
        finally { field.remove(); }
      }
      showToast("代码已复制");
    }
    catch (error) { showToast(`复制失败：${error.message}`, true); }
  });
  el("visualTestClear").addEventListener("click", async () => {
    if (busy || !station) return;
    const token = generation, name = station.name;
    try {
      await api(path(name), "DELETE");
      cache.delete(name);
      if (generation === token) { showResult(null); status("已清除上次结果"); }
      await refreshStations({ force: true });
    } catch (error) { if (generation === token) status(error.message, true); }
  });

  const observer = typeof IntersectionObserver === "function" ? new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) {
      observer.unobserve(entry.target);
      loadThumbnail(entry.target);
    }
  }) : null;
  async function loadThumbnail(button) {
    const name = button.dataset.visualTest, id = button.dataset.visualThumbnail;
    try {
      let value = cache.get(name);
      if (!value || value.id !== id) {
        const key = JSON.stringify([name, id]);
        let pending = thumbnailLoads.get(key);
        if (!pending) {
          pending = api(path(name)).then(data => data.result).finally(() => thumbnailLoads.delete(key));
          thumbnailLoads.set(key, pending);
        }
        value = await pending;
        if (!value || value.id !== id || !value.preview_html) return;
        if (cache.size >= 32) cache.delete(cache.keys().next().value);
        cache.set(name, { id, preview_html: value.preview_html });
      }
      if (!button.isConnected || button.querySelector("iframe")) return;
      const frame = document.createElement("iframe");
      frame.setAttribute("sandbox", "");
      frame.setAttribute("referrerpolicy", "no-referrer");
      frame.setAttribute("tabindex", "-1");
      frame.setAttribute("aria-hidden", "true");
      frame.title = "上次测试的静态缩略图";
      frame.srcdoc = value.preview_html;
      (button.querySelector(".visual-thumbnail-image") || button).replaceChildren(frame);
      const fit = () => {
        const scale = Math.min(button.clientWidth / 640, button.clientHeight / 400);
        frame.style.transform = `translate(-50%,-50%) scale(${scale})`;
      };
      fit();
      thumbnailSizes.observe(button);
    } catch { /* A failed preview read never triggers a model call. */ }
  }
  function observeThumbnails() {
    observer?.disconnect();
    thumbnailSizes.disconnect();
    for (const button of document.querySelectorAll("[data-visual-thumbnail]")) {
      if (button.querySelector("iframe")) thumbnailSizes.observe(button);
      else if (observer) observer.observe(button);
      else loadThumbnail(button);
    }
  }
  document.addEventListener("modelharbor:stations-rendered", () => { renderGallery(); syncTablePreviews(); });
  window.ModelHarborVisualTests = { modelMarkup, syncTablePreviews };
  refreshStations();
})();
