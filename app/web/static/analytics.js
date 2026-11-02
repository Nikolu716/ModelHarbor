"use strict";

(() => {
  const colors = ["#8b8fea", "#62b8df", "#6fc8a8", "#d5b965", "#d28fb4", "#99a7bc", "#b29add", "#77b8bf"];
  const metricLabels = { tokens: "Token", calls: "次请求", failures: "次失败" };
  let history = null;
  let busy = false;
  let period = 30;
  let metric = "tokens";
  let group = "model";
  let activeSeries = "";
  let plot = null;
  let canvasPoints = [];
  const count = value => Math.max(0, finiteNumber(value));
  const compact = value => {
    const n = count(value);
    if (n >= 1e9) return `${(n / 1e9).toLocaleString("en", { maximumFractionDigits: 2 })}B`;
    if (n >= 1e6) return `${(n / 1e6).toLocaleString("en", { maximumFractionDigits: 2 })}M`;
    if (n >= 1e3) return `${(n / 1e3).toLocaleString("en", { maximumFractionDigits: 1 })}K`;
    return formatCount(n);
  };
  const dateText = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  const parseDate = value => new Date(`${value}T12:00:00`);
  const shiftDate = (value, offset) => {
    const date = parseDate(value);
    date.setDate(date.getDate() + offset);
    return dateText(date);
  };
  const dateLabel = value => value.slice(5).replace("-", "/");
  const sum = (rows, key) => rows.reduce((total, row) => total + count(row[key]), 0);

  function selectedDates() {
    return (history?.daily || []).filter(row => row.date >= $("trend-from").value && row.date <= $("trend-to").value);
  }

  function setPeriod(days) {
    period = days;
    if (history) {
      $("trend-to").value = history.to;
      $("trend-from").value = shiftDate(history.to, 1 - days);
    }
    render();
  }

  function buildPlot() {
    const daily = selectedDates();
    const dates = new Set(daily.map(row => row.date));
    const grouped = new Map();
    const attributed = new Map(daily.map(row => [row.date, 0]));
    for (const row of history?.models || []) {
      if (!dates.has(row.date)) continue;
      const id = JSON.stringify(group === "station" ? [row.station] : group === "model_station" ? [row.model, row.station] : [row.model]);
      const label = group === "station" ? row.station : group === "model_station" ? `${row.model} · ${row.station}` : row.model;
      if (!grouped.has(id)) grouped.set(id, { id, label, calls: 0, failures: 0, tokens: 0, values: new Map() });
      const series = grouped.get(id);
      for (const field of ["calls", "failures", "tokens"]) series[field] += count(row[field]);
      series.values.set(row.date, (series.values.get(row.date) || 0) + count(row[metric]));
      attributed.set(row.date, (attributed.get(row.date) || 0) + count(row[metric]));
    }
    const sorted = [...grouped.values()].filter(row => row[metric] > 0).sort((a, b) => b[metric] - a[metric] || a.label.localeCompare(b.label));
    const series = sorted.slice(0, 7);
    const other = { id: "other", label: "其他 / 未归类", calls: 0, failures: 0, tokens: 0, values: new Map() };
    for (const row of sorted.slice(7)) {
      for (const field of ["calls", "failures", "tokens"]) other[field] += row[field];
      for (const [date, value] of row.values) other.values.set(date, (other.values.get(date) || 0) + value);
    }
    for (const row of daily) {
      const remainder = Math.max(0, count(row[metric]) - attributed.get(row.date));
      other.values.set(row.date, (other.values.get(row.date) || 0) + remainder);
      other[metric] += remainder;
    }
    if (other[metric] > 0) series.push(other);
    series.forEach((row, index) => { row.color = colors[index % colors.length]; });
    if (!series.some(row => row.id === activeSeries)) activeSeries = "";
    return { daily, series };
  }

  function renderSummary(daily) {
    const calls = sum(daily, "calls");
    const failures = sum(daily, "failures");
    const reports = sum(daily, "usage_reports");
    const cacheInput = sum(daily, "cache_input");
    const cache = sum(daily, "cache_read");
    const coverageKnown = daily.every(row => row.token_coverage_known !== false);
    const fields = [
      ["已统计 Token", compact(sum(daily, "tokens")), `${formatCount(reports)} 次调用回传用量`, ""],
      ["请求", formatCount(calls), `${formatCount(failures)} 次失败 · 成功率 ${calls ? formatShare((calls - failures) / calls) : "—"}`, ""],
      ["缓存命中率", cacheInput ? formatShare(cache / cacheInput) : "—", cacheInput ? `${compact(cache)} / ${compact(cacheInput)} 输入 Token` : "上游尚未回传可计算的缓存字段", "is-cache"],
      ["Token 回传覆盖", coverageKnown && calls ? formatShare(Math.min(1, reports / calls)) : "—", coverageKnown ? "仅统计已回传用量，不估算费用" : "部分历史未记录覆盖率", ""],
    ];
    $("trend-kpis").innerHTML = fields.map(([label, value, note, cls]) => `<div class="trend-kpi ${cls}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(note)}</small></div>`).join("");
  }

  function renderLegend() {
    const total = plot.series.reduce((value, row) => value + row[metric], 0);
    $("trend-legend").innerHTML = plot.series.map(row => `<button class="trend-series ${activeSeries && activeSeries !== row.id ? "is-dimmed" : ""}" type="button" data-series-id="${escapeHtml(row.id)}" aria-pressed="${activeSeries === row.id}"><span class="trend-series-top"><i aria-hidden="true" data-color="${row.color}"></i><strong title="${escapeHtml(row.label)}">${escapeHtml(row.label)}</strong><b>${compact(row[metric])}</b></span><span class="trend-series-track"><i data-color="${row.color}" data-width="${total ? row[metric] / total * 100 : 0}"></i></span><span class="trend-series-detail">${formatCount(row.calls)} 次请求 · ${formatCount(row.failures)} 次失败 · ${total ? formatShare(row[metric] / total) : "0%"}</span></button>`).join("") || '<div class="trend-no-series">有请求数据后，模型与站点明细将在这里呈现。</div>';
    $("trend-legend").querySelectorAll("[data-color]").forEach(element => { element.style.backgroundColor = element.dataset.color; });
    applyDataWidths($("trend-legend"));
  }

  function drawChart() {
    const canvas = $("usageTrendChart");
    if (!plot || !canvas.clientWidth || !canvas.clientHeight) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, width, height);
    const left = width < 500 ? 44 : 56, right = 10, top = 16, bottom = 25;
    const w = width - left - right, h = height - top - bottom;
    if (h <= 0 || w <= 0) return;
    const selected = plot.series.filter(row => !activeSeries || row.id === activeSeries);
    const maximum = Math.max(1, ...plot.daily.map(day => selected.reduce((value, row) => value + (row.values.get(day.date) || 0), 0)));
    const rawStep = maximum / 4;
    const power = 10 ** Math.floor(Math.log10(rawStep));
    const step = Math.ceil(rawStep / power) * power;
    const cap = step * 4;
    ctx.font = '11px system-ui, sans-serif';
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const y = top + h - h * i / 4;
      ctx.strokeStyle = "#e8ebf2";
      ctx.setLineDash([2, 4]);
      ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + w, y); ctx.stroke();
      ctx.fillStyle = "#9298a5";
      ctx.fillText(compact(step * i), left - 8, y);
    }
    ctx.setLineDash([]);
    const pitch = w / Math.max(1, plot.daily.length);
    const barWidth = Math.max(1, pitch * (plot.daily.length > 90 ? 0.85 : 0.68));
    canvasPoints = [];
    plot.daily.forEach((day, index) => {
      const x = left + pitch * index + (pitch - barWidth) / 2;
      let used = 0;
      for (const row of selected) {
        const value = row.values.get(day.date) || 0;
        const barHeight = h * value / cap;
        ctx.fillStyle = row.color;
        if (value > 0) ctx.fillRect(x, top + h - used - barHeight, barWidth, Math.max(0.7, barHeight));
        used += barHeight;
      }
      canvasPoints.push({ x: left + pitch * index, width: pitch, day });
      const tick = Math.max(1, Math.ceil(plot.daily.length / Math.max(2, Math.floor(w / 75))));
      if (index % tick === 0 || index === plot.daily.length - 1 && plot.daily.length % tick > tick / 2) {
        ctx.textAlign = "center"; ctx.fillStyle = "#9298a5";
        ctx.fillText(dateLabel(day.date), x + barWidth / 2, height - 9);
      }
    });
    canvas.setAttribute("aria-label", `${$("trend-from").value} 至 ${$("trend-to").value}，${metricLabels[metric]}堆叠柱状图，共 ${formatCount(sum(plot.daily, metric))} ${metricLabels[metric]}。按模型或站点的明细位于图表右侧。`);
  }

  function renderHeatmap() {
    const daily = history?.daily || [];
    if (!daily.length) return;
    const first = parseDate(daily[0].date);
    const leading = (first.getDay() + 6) % 7;
    const maximum = Math.max(1, ...daily.map(row => count(row[metric])));
    const cells = Array.from({ length: leading }, () => '<span class="heat-cell heat-outside" aria-hidden="true"></span>');
    for (const row of daily) {
      const unknown = !history.available_from || row.date < history.available_from;
      const value = count(row[metric]);
      const level = value ? Math.max(1, Math.ceil(Math.sqrt(value / maximum) * 4)) : 0;
      const label = `${row.date} · ${unknown ? "无保留数据" : `${formatCount(value)} ${metricLabels[metric]}`}`;
      cells.push(`<button type="button" class="heat-cell heat-level-${level}${unknown ? " heat-unknown" : ""}" data-heat-date="${row.date}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"></button>`);
    }
    const weeks = Math.ceil(cells.length / 7);
    const months = new Map();
    for (let index = 0; index < daily.length; index++) {
      if (index === 0 || daily[index].date.slice(5, 7) !== daily[index - 1].date.slice(5, 7)) {
        const week = Math.floor((index + leading) / 7) + 1;
        months.set(week, `<span data-week="${week}">${Number(daily[index].date.slice(5, 7))}月</span>`);
      }
    }
    $("usageHeatmap").innerHTML = `<div class="heat-weekdays"><span>一</span><span>三</span><span>五</span><span>日</span></div><div class="heat-calendar"><div class="heat-months">${[...months.values()].join("")}</div><div class="heat-cells">${cells.join("")}</div></div>`;
    $("usageHeatmap").style.setProperty("--heat-weeks", weeks);
    $("usageHeatmap").querySelectorAll("[data-week]").forEach(element => { element.style.gridColumn = element.dataset.week; });
    $("trend-heatmap-caption").textContent = `最近 365 天 · 日${metricLabels[metric]} · 点击某天查看趋势`;
    $("trend-heatmap-total").textContent = history.available_from ? `保留数据始于 ${history.available_from}` : "尚无保留数据";
  }

  function render() {
    if (!history) return;
    plot = buildPlot();
    renderSummary(plot.daily);
    renderLegend();
    renderHeatmap();
    $("trend-range-note").textContent = `${$("trend-from").value} — ${$("trend-to").value}`;
    $("trend-empty").hidden = sum(plot.daily, metric) > 0;
    $("trend-tooltip").hidden = true;
    for (const [id, attribute, value] of [["trend-periods", "trendPeriod", String(period)], ["trend-metrics", "trendMetric", metric], ["trend-groups", "trendGroup", group]]) {
      $(id).querySelectorAll("button").forEach(button => button.setAttribute("aria-pressed", String(button.dataset[attribute] === value)));
    }
    drawChart();
  }

  window.refreshUsageHistory = async () => {
    if (busy) return;
    busy = true;
    try {
      const result = await api("/admin/stats/history?days=365");
      if (!Array.isArray(result.daily) || !Array.isArray(result.models)) throw new Error("历史统计格式不可用");
      history = result;
      for (const id of ["trend-from", "trend-to"]) {
        $(id).min = history.from; $(id).max = history.to;
      }
      if (period) setPeriod(period); else render();
    } catch (error) {
      $("trend-range-note").textContent = `历史统计读取失败：${error.message}`;
    } finally { busy = false; }
  };

  $("trend-periods").addEventListener("click", event => {
    const button = event.target.closest("[data-trend-period]");
    if (button) setPeriod(Number(button.dataset.trendPeriod));
  });
  $("trend-metrics").addEventListener("click", event => {
    const button = event.target.closest("[data-trend-metric]");
    if (button) { metric = button.dataset.trendMetric; activeSeries = ""; render(); }
  });
  $("trend-groups").addEventListener("click", event => {
    const button = event.target.closest("[data-trend-group]");
    if (button) { group = button.dataset.trendGroup; activeSeries = ""; render(); }
  });
  $("trend-apply").addEventListener("click", () => {
    const from = $("trend-from"), to = $("trend-to");
    if (!history || !from.value || !to.value || !from.checkValidity() || !to.checkValidity() || from.value > to.value) {
      showToast("请选择保留范围内有效的起止日期", true); return;
    }
    period = 0; render();
  });
  $("trend-legend").addEventListener("click", event => {
    const button = event.target.closest("[data-series-id]");
    if (button) { activeSeries = activeSeries === button.dataset.seriesId ? "" : button.dataset.seriesId; renderLegend(); drawChart(); }
  });
  $("usageHeatmap").addEventListener("click", event => {
    const button = event.target.closest("[data-heat-date]");
    if (button) { period = 0; $("trend-from").value = button.dataset.heatDate; $("trend-to").value = button.dataset.heatDate; render(); }
  });
  $("usageTrendChart").addEventListener("mousemove", event => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const point = canvasPoints.find(item => event.clientX - bounds.left >= item.x && event.clientX - bounds.left < item.x + item.width);
    const tooltip = $("trend-tooltip");
    if (!point) { tooltip.hidden = true; return; }
    const rows = plot.series.filter(row => (!activeSeries || row.id === activeSeries) && row.values.get(point.day.date));
    tooltip.textContent = `${point.day.date}\n${rows.map(row => `${row.label}  ${formatCount(row.values.get(point.day.date))}`).join("\n") || `0 ${metricLabels[metric]}`}`;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(8, Math.min(bounds.width - 245, event.clientX - bounds.left + 12))}px`;
  });
  $("usageTrendChart").addEventListener("mouseleave", () => { $("trend-tooltip").hidden = true; });
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(drawChart).observe($("usageTrendChart"));
  window.addEventListener("resize", drawChart);
  $("stats-tab-trend").addEventListener("click", () => requestAnimationFrame(drawChart));
  $("tab-stats").addEventListener("click", () => requestAnimationFrame(drawChart));
  if (currentTab === "stats") window.refreshUsageHistory();
})();

(() => {
  const $ = id => document.getElementById(id);
  const tones = ["#398b7a", "#648dc1", "#d29a49", "#ba6876", "#7e79b8", "#64a9af", "#9b805e", "#718b63", "#ce789e", "#7b8290", "#c2ad52", "#528fa1", "#a36e51"];
  let analytics = null;
  let busy = false;
  let refreshAgain = false;
  let sourcePage = 1;
  let sourcePageSize = 10;
  const metric = () => $("eventMetric").value === "tokens" ? "tokens" : "calls";
  const coverage = (count, total) => total ? `${(100 * count / total).toFixed(1)}%` : "—";

  function renderSources() {
    if (!analytics) return;
    const rows = analytics.ip_models || [];
    const pages = Math.max(1, Math.ceil(rows.length / sourcePageSize));
    sourcePage = Math.max(1, Math.min(sourcePage, pages));
    $("eventSourceRows").innerHTML = rows.slice((sourcePage - 1) * sourcePageSize, sourcePage * sourcePageSize).map(row => `<tr><td>${escapeHtml(row.ip === "unknown" ? "未记录 IP" : row.ip)}</td><td title="${escapeHtml(row.model)}">${escapeHtml(row.model)}</td><td>${formatCount(row.calls)}</td><td>${formatCount(row.failures)}</td><td>${row.usage_reports ? formatCount(row.tokens) : "未上报"}</td><td>${formatCount(row.usage_reports)} / ${formatCount(row.calls)} · ${coverage(row.usage_reports, row.calls)}</td></tr>`).join("") || '<tr><td colspan="6" class="empty-cell">当前筛选范围没有留存请求</td></tr>';
    $("eventSourcePage").textContent = `第 ${sourcePage} / ${pages} 页 · ${rows.length} 组 IP × 模型`;
    $("eventSourcePrev").disabled = sourcePage <= 1;
    $("eventSourceNext").disabled = sourcePage >= pages;
    $("eventSourceSummary").innerHTML = [["请求次数", formatCount(analytics.calls)], ["已上报 Token", analytics.usage_reports ? formatCount(analytics.tokens) : "未上报"], ["Token 上报覆盖", coverage(analytics.usage_reports || 0, analytics.calls)], ["IP 记录覆盖", coverage(analytics.ip_reports || 0, analytics.calls)]].map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join("");
    $("eventSourceScope").textContent = `${analytics.date_from} 至 ${analytics.date_to} · 筛中 ${formatCount(analytics.calls)} / 留存 ${formatCount(analytics.retained_events)} 条（上限 ${formatCount(analytics.max_events)}）。缺失 Token 不按零用量推断，历史未记录 IP 不回填。`;
  }

  function dateText(value) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  }

  function setPeriod(days) {
    const end = new Date();
    const start = new Date(end);
    start.setDate(start.getDate() - days + 1);
    $("eventDateFrom").value = dateText(start);
    $("eventDateTo").value = dateText(end);
    document.querySelectorAll("[data-log-days]").forEach(button => {
      button.setAttribute("aria-pressed", String(Number(button.dataset.logDays) === days));
    });
  }

  function updateOptions(select, options, allLabel) {
    const selected = select.value;
    select.replaceChildren(new Option(allLabel, ""));
    for (const item of options || []) select.add(new Option(item.label, item.value));
    if (selected && ![...select.options].some(option => option.value === selected)) {
      select.add(new Option(selected === "unknown" ? "未记录 IP" : selected, selected));
    }
    select.value = selected;
  }

  const trendChart = window.echarts ? window.echarts.init($("eventTrendChart")) : null;
  const breakdownChart = window.echarts ? window.echarts.init($("eventBreakdownDonut")) : null;

  function drawTrend() {
    if (!analytics || !trendChart) return;
    const rows = analytics.daily || [];
    const tokens = metric() === "tokens";
    $("eventTrendTitle").textContent = tokens ? "已上报 Token 趋势" : "请求趋势";
    $("eventTrendKeys").hidden = tokens;
    trendChart.setOption({
      animationDuration: 220,
      aria: { enabled: true },
      color: ["#398b7a", "#bd6872"],
      grid: { left: 31, right: 22, top: 10, bottom: 20 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: "#aab4c0", type: "dashed" } },
        backgroundColor: "#19222f",
        borderWidth: 0,
        textStyle: { color: "#f6f8fa", fontSize: 11 },
        formatter: points => {
          const rows = Array.isArray(points) ? points : [points];
          const heading = rows[0]?.axisValue || "";
          return `${escapeHtml(heading)}<br>${rows.map(point => `${escapeHtml(point.seriesName)}\uFF1A${formatCount(point.value)}`).join("<br>")}`;
        },
      },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: rows.map(row => row.date),
        axisTick: { show: false },
        axisLine: { lineStyle: { color: "#e4e8ed" } },
        axisLabel: { color: "#9097a3", fontSize: 9, hideOverlap: true, formatter: value => value.slice(5).replace("-", "/") },
      },
      yAxis: {
        type: "value",
        min: 0,
        minInterval: 1,
        splitNumber: 3,
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { color: "#9097a3", fontSize: 9 },
        splitLine: { lineStyle: { color: "#edf0f3" } },
      },
      series: tokens ? [{ name: "已上报 Token", type: "bar", data: rows.map(row => Number(row.tokens) || 0), barMaxWidth: 26, itemStyle: { color: "#398b7a", borderRadius: [3, 3, 0, 0] } }] : [
        { name: "\u8bf7\u6c42", type: "line", data: rows.map(row => Number(row.calls) || 0), smooth: 0.24, showSymbol: rows.length <= 14, symbolSize: 5, lineStyle: { width: 2.2 }, itemStyle: { color: "#398b7a" }, areaStyle: { opacity: 0.08 }, emphasis: { focus: "series" } },
        { name: "\u5931\u8d25", type: "line", data: rows.map(row => Number(row.failures) || 0), smooth: 0.18, showSymbol: rows.length <= 14, symbolSize: 4, lineStyle: { width: 1.5 }, itemStyle: { color: "#bd6872" }, emphasis: { focus: "series" } },
      ],
    }, { notMerge: true, lazyUpdate: true });
    $("eventTrendEmpty").hidden = tokens ? analytics.usage_reports > 0 : analytics.calls > 0;
    $("eventTrendEmpty").textContent = tokens ? "当前范围没有 Token 上报" : "当前范围没有留存记录";
  }

  function renderBreakdown() {
    if (!analytics) return;
    const items = analytics.breakdown || [];
    const field = metric();
    const unit = field === "tokens" ? "Token" : "请求";
    const total = items.reduce((sum, item) => sum + (Number(item[field]) || 0), 0);
    $("eventBreakdownTotal").textContent = formatCount(total);
    $("eventDonutCenter").textContent = `${formatCount(total)}\n${unit}`;
    $("eventBreakdownTitle").textContent = $("eventGroupBy").value === "ip" ? "IP \u5206\u5e03" : "\u6a21\u578b\u5206\u5e03";
    if (breakdownChart) {
      breakdownChart.setOption({
        animationDuration: 220,
        aria: { enabled: true },
        tooltip: {
          trigger: "item",
          backgroundColor: "#19222f",
          borderWidth: 0,
          textStyle: { color: "#f6f8fa", fontSize: 11 },
          formatter: item => `${escapeHtml(item.name)}<br>${formatCount(item.value)} ${unit}（${Number(item.percent || 0).toFixed(1)}%）`,
        },
        series: [{
          type: "pie",
          radius: ["56%", "78%"],
          center: ["50%", "50%"],
          avoidLabelOverlap: true,
          label: { show: false },
          labelLine: { show: false },
          itemStyle: { borderColor: "#fff", borderWidth: 2, borderRadius: 3 },
          emphasis: { scale: true, scaleSize: 4, itemStyle: { shadowBlur: 10, shadowColor: "#202b3a22" } },
          data: items.map((item, index) => ({
            name: item.label,
            value: Number(item[field]) || 0,
            itemStyle: { color: tones[index % tones.length] },
          })).filter(item => item.value > 0),
        }],
      }, { notMerge: true, lazyUpdate: true });
    }
    $("eventBreakdownLegend").replaceChildren();
    items.forEach((item, index) => {
      const row = document.createElement("div");
      row.className = "event-breakdown-item";
      const swatch = document.createElement("i");
      swatch.style.backgroundColor = tones[index % tones.length];
      const label = document.createElement("span");
      label.textContent = item.value === "unknown" ? "\u672a\u8bb0\u5f55 IP" : item.label;
      label.title = item.value === "unknown" ? "\u8bb0\u5f55\u65f6\u6ca1\u6709\u53ef\u7528\u7684\u5ba2\u6237\u7aef\u5730\u5740" : item.label;
      const value = document.createElement("strong");
      value.textContent = field === "tokens" && !item.usage_reports ? "未上报" : formatCount(item[field]);
      row.append(swatch, label, value);
      $("eventBreakdownLegend").append(row);
    });
    if (!items.length) $("eventBreakdownLegend").textContent = "\u6682\u65e0\u7559\u5b58\u6570\u636e";
  }

  function resizeCharts() {
    trendChart?.resize();
    breakdownChart?.resize();
    const shell = document.querySelector(".source-table-shell");
    if (shell.clientHeight > 0) {
      const header = shell.querySelector("thead").getBoundingClientRect().height || 40;
      const row = shell.querySelector("tbody tr")?.getBoundingClientRect().height || 44;
      const size = Math.max(1, Math.min(50, Math.floor((shell.clientHeight - header - 4) / Math.max(40, row))));
      if (size !== sourcePageSize) { sourcePageSize = size; sourcePage = 1; renderSources(); }
    }
  }

  async function refreshAnalytics() {
    if (busy) { refreshAgain = true; return; }
    busy = true;
    try {
      const query = new URLSearchParams({
        date_from: $("eventDateFrom").value,
        date_to: $("eventDateTo").value,
        group_by: $("eventGroupBy").value,
      });
      if ($("eventIpFilter").value) query.set("ip", $("eventIpFilter").value);
      if ($("eventModelFilter").value) query.set("model", $("eventModelFilter").value);
      analytics = await api(`/admin/logs/analytics?${query}`);
      updateOptions($("eventIpFilter"), analytics.ips, "全部 IP");
      updateOptions($("eventModelFilter"), analytics.models, "全部模型");
      $("eventAnalyticsScope").textContent = `${analytics.date_from} 至 ${analytics.date_to}`;
      $("eventAnalyticsCoverage").textContent = `筛中 ${formatCount(analytics.calls)} / 留存 ${formatCount(analytics.retained_events)} 条 · 上限 ${formatCount(analytics.max_events)} · Token 上报 ${coverage(analytics.usage_reports || 0, analytics.calls)}`;
      renderSources();
      renderBreakdown();
      drawTrend();
    } catch (error) {
      showToast(`请求日志统计读取失败: ${error.message}`, true);
    } finally { busy = false; if (refreshAgain) { refreshAgain = false; refreshAnalytics(); } }
  }

  async function refreshRetention() {
    try {
      const result = await api("/admin/settings/logs");
      $("eventRetentionLimit").value = result.event_retention;
      $("eventRetentionLimit").min = result.min_event_retention;
      $("eventRetentionLimit").max = result.max_event_retention;
      const status = $("eventRetentionStatus");
      if (status && result.storage) {
        status.textContent = result.storage.message;
        status.classList.toggle("text-danger", !result.storage.ok);
      }
    } catch (error) {
      $("eventRetentionStatus").textContent = error.message;
    }
  }

  async function saveRetention() {
    const input = $("eventRetentionLimit");
    const value = Number(input.value);
    if (!input.checkValidity() || !Number.isInteger(value) || value < 100 || value > 10000) {
      showToast("留存条数需为 100 到 10000 之间的整数", true);
      input.focus();
      return;
    }
    $("eventRetentionSave").disabled = true;
    try {
      await api("/admin/settings/logs", "PUT", true, { event_retention: value });
      $("eventRetentionStatus").textContent = `已保存 ${formatCount(value)} 条`;
      eventPage = 1;
      await Promise.all([window.refreshEvents(1), refreshAnalytics()]);
    } catch (error) {
      $("eventRetentionStatus").textContent = error.message;
      showToast(`留存设置保存失败: ${error.message}`, true);
    } finally { $("eventRetentionSave").disabled = false; }
  }

  window.refreshLogAnalytics = refreshAnalytics;
  setPeriod(7);
  $("eventGroupBy").addEventListener("change", refreshAnalytics);
  $("eventMetric").addEventListener("change", () => { renderBreakdown(); drawTrend(); });
  $("eventSourcePrev").addEventListener("click", () => { sourcePage -= 1; renderSources(); });
  $("eventSourceNext").addEventListener("click", () => { sourcePage += 1; renderSources(); });
  $("events-tab-sources").addEventListener("click", () => { sourcePage = 1; refreshAnalytics(); requestAnimationFrame(resizeCharts); });
  $("eventAnalyticsApply").addEventListener("click", async () => {
    const from = $("eventDateFrom"), to = $("eventDateTo");
    if (!from.value || !to.value || !from.checkValidity() || !to.checkValidity() || from.value > to.value) {
      showToast("请选择有效的起止日期", true);
      return;
    }
    document.querySelectorAll("[data-log-days]").forEach(button => button.setAttribute("aria-pressed", "false"));
    eventPage = 1;
    await Promise.all([window.refreshEvents(1), refreshAnalytics()]);
  });
  document.querySelectorAll("[data-log-days]").forEach(button => button.addEventListener("click", async () => {
    setPeriod(Number(button.dataset.logDays));
    eventPage = 1;
    await Promise.all([window.refreshEvents(1), refreshAnalytics()]);
  }));
  for (const id of ["eventIpFilter", "eventModelFilter"]) $(id).addEventListener("change", async () => {
    eventPage = 1;
    await Promise.all([window.refreshEvents(1), refreshAnalytics()]);
  });
  $("eventRetentionSave").addEventListener("click", saveRetention);
  $("tab-events").addEventListener("click", () => {
    refreshRetention();
    refreshAnalytics();
    requestAnimationFrame(() => { resizeCharts(); drawTrend(); });
  });
  if (currentTab === "events") { refreshRetention(); refreshAnalytics(); }
  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(resizeCharts);
    observer.observe($("eventTrendChart"));
    observer.observe($("eventBreakdownDonut"));
    observer.observe(document.querySelector(".source-table-shell"));
  }
  window.addEventListener("resize", resizeCharts);
})();
