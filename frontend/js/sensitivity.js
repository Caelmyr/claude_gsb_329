/* Parameter sensitivity analysis: scan builder, live progress, curves,
   heatmaps and OAT tornado ranking. */

let catalogData = null;
let scenes = [];
let numericParams = [];          // for the currently selected scene/model
let metricLabels = {};
let chart = null;
let currentSweep = null;
let pollTimer = null;
let createTimer = null;

const AGG_LABEL = { final: "末值", mean: "均值", max: "最大值", min: "最小值" };

/* -------------------------------------------------------------------- */
/* Catalog / scene setup                                                 */
/* -------------------------------------------------------------------- */
function sceneModel() {
  const s = scenes.find((x) => x.id === el("swScene").value);
  if (!s) return null;
  return catalogData.domains[s.domain]?.models?.[s.model] || null;
}

function onSceneChange() {
  const model = sceneModel();
  if (!model) return;
  numericParams = model.params.filter((p) => p.type === "int" || p.type === "float");
  el("paramRows").innerHTML = "";
  addParamRow();

  const metrics = catalogData.domains[scenes.find((x) => x.id === el("swScene").value).domain].metrics;
  metricLabels = {};
  for (const d of Object.values(catalogData.domains))
    for (const m of d.metrics) metricLabels[m.key] = m.label;

  el("swMetric").innerHTML = metrics.map((m) =>
    `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join("");
  el("swStopMetric").innerHTML =
    '<option value="">不提前结束</option>' +
    metrics.map((m) => `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join("");
  schedulePreview();
}

function addParamRow(key, mode = "linear", rangeVals = null, count = 7) {
  const div = document.createElement("div");
  div.className = "card param-row";
  div.style.cssText = "padding:10px;margin-bottom:8px";
  div.innerHTML = `
    <div class="grid grid-4">
      <div class="field" style="margin:0">
        <label>参数</label>
        <select class="pkey">${numericParams.map((p) =>
          `<option value="${esc(p.key)}">${esc(p.label)}（${esc(p.key)}）</option>`).join("")}</select>
      </div>
      <div class="field range-fields" style="margin:0">
        <label>区间起点 / 终点</label>
        <div class="row gap-6"><input class="pstart" type="number" step="any"><input class="pend" type="number" step="any"></div>
      </div>
      <div class="field count-field" style="margin:0;max-width:140px">
        <label>取值数</label><input class="pcount" type="number" min="2" max="50" value="${count}">
      </div>
      <div class="field values-field" style="margin:0;display:none">
        <label>自定义取值（逗号分隔）</label><input class="pvalues" placeholder="如 0.1, 0.2, 0.5">
      </div>
      <div class="field" style="margin:0;max-width:150px">
        <label>取值方式</label>
        <select class="pmode">
          <option value="linear">等距</option>
          <option value="log">对数（跨数量级）</option>
          <option value="values">自定义列表</option>
        </select>
      </div>
    </div>
    <div class="row between" style="margin-top:6px">
      <span class="muted small base-info"></span>
      <button class="btn danger small pdel">删除参数</button>
    </div>`;
  el("paramRows").appendChild(div);

  const keySel = div.querySelector(".pkey");
  if (key) keySel.value = key;
  const fillRange = () => {
    const spec = numericParams.find((p) => p.key === keySel.value);
    if (!spec) return;
    const sceneCfg = (scenes.find((x) => x.id === el("swScene").value) || {}).config || {};
    const base = sceneCfg[spec.key] != null ? sceneCfg[spec.key] : spec.default;
    div.querySelector(".pstart").value = rangeVals ? rangeVals[0] : spec.min;
    div.querySelector(".pend").value = rangeVals ? rangeVals[1] : spec.max;
    div.querySelector(".base-info").textContent =
      `允许范围 [${spec.min}, ${spec.max}] · 当前场景基线：${fmt(base, 4)}`;
  };
  fillRange();
  keySel.onchange = () => { fillRange(); schedulePreview(); };

  const modeSel = div.querySelector(".pmode");
  const applyMode = () => {
    const custom = modeSel.value === "values";
    div.querySelector(".range-fields").style.display = custom ? "none" : "";
    div.querySelector(".count-field").style.display = custom ? "none" : "";
    div.querySelector(".values-field").style.display = custom ? "" : "none";
  };
  modeSel.value = mode;
  applyMode();
  modeSel.onchange = () => { applyMode(); schedulePreview(); };
  div.querySelectorAll("input").forEach((i) => i.addEventListener("input", schedulePreview));
  div.querySelector(".pdel").onclick = () => {
    div.remove();
    schedulePreview();
  };
}

/* -------------------------------------------------------------------- */
/* Plan preview (group count before launching)                           */
/* -------------------------------------------------------------------- */
function collectParams() {
  const rows = [...document.querySelectorAll(".param-row")];
  return rows.map((row) => {
    const key = row.querySelector(".pkey").value;
    const mode = row.querySelector(".pmode").value;
    if (mode === "values") {
      const vals = row.querySelector(".pvalues").value.split(/[,，]/)
        .map((v) => v.trim()).filter(Boolean).map(Number);
      return { key, mode, values: vals };
    }
    return {
      key, mode,
      start: parseFloat(row.querySelector(".pstart").value),
      end: parseFloat(row.querySelector(".pend").value),
      count: parseInt(row.querySelector(".pcount").value || "0", 10),
    };
  });
}

function collectPayload() {
  const stopMetric = el("swStopMetric").value;
  const stopRule = stopMetric ? {
    metric: stopMetric, op: el("swStopOp").value,
    threshold: parseFloat(el("swStopThreshold").value),
    patience: parseInt(el("swStopPatience").value || "1", 10),
  } : null;
  return {
    name: el("swName").value.trim() || "参数敏感性分析",
    scene_id: el("swScene").value,
    steps: parseInt(el("swSteps").value || "200", 10),
    replicates: parseInt(el("swReplicates").value || "1", 10),
    seed: parseInt(el("swSeed").value || "0", 10),
    metric: el("swMetric").value,
    aggregation: el("swAgg").value,
    stop_rule: stopRule,
    params: collectParams(),
  };
}

function schedulePreview() {
  clearTimeout(createTimer);
  createTimer = setTimeout(previewPlan, 250);
}

async function previewPlan() {
  const payload = collectPayload();
  if (!payload.scene_id || !payload.params.length) return;
  const info = el("planInfo");
  try {
    const plan = await post("/api/sweeps/preview", payload);
    const typeLabel = { line: "敏感性曲线（1 个参数）",
                        heatmap: `热力图（2 个参数 ${plan.params[0].label} × ${plan.params[1].label}）`,
                        oat: `OAT 逐参数扫描 + 影响排名（${plan.params.length} 个参数）` }[plan.scan_type];
    const warn = plan.runs_total > 60
      ? `<span style="color:var(--orange)"> · 组数较多，耗时可能较长（可减少取值数或调小步数）</span>` : "";
    info.innerHTML = `方案：${typeLabel} · 共 <b>${plan.total}</b> 组 × ${plan.replicates} 次重复 = <b>${plan.runs_total}</b> 次完整运行${warn}`;
  } catch (e) {
    info.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`;
  }
}

/* -------------------------------------------------------------------- */
/* Launch / cancel / poll                                                */
/* -------------------------------------------------------------------- */
async function launchSweep() {
  const payload = collectPayload();
  if (!payload.scene_id) { alert("请选择基础场景"); return; }
  if (!payload.params.length) { alert("请至少添加一个扫描参数"); return; }
  if (payload.stop_rule && Number.isNaN(payload.stop_rule.threshold)) {
    alert("请填写提前结束阈值，或把指标选择改回“不提前结束”"); return;
  }
  el("runSweep").disabled = true;
  try {
    const sweep = await post("/api/sweeps", payload);
    await refreshList();
    selectSweep(sweep.id);
  } catch (e) {
    alert("创建扫描失败：" + e.message);
  } finally {
    el("runSweep").disabled = false;
  }
}

async function cancelSweep() {
  if (!currentSweep) return;
  await post(`/api/sweeps/${currentSweep.id}/cancel`);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

function pollSweep(id) {
  stopPolling();
  pollTimer = setInterval(async () => {
    const s = await get(`/api/sweeps/${id}`);
    currentSweep = s;
    renderProgress(s);
    if (["finished", "partial", "stopped", "error"].includes(s.status)) {
      stopPolling();
      el("cancelSweep").style.display = "none";
      await refreshList();
    }
    renderResult();
  }, 1000);
}

/* -------------------------------------------------------------------- */
/* Sweep list                                                            */
/* -------------------------------------------------------------------- */
async function refreshList() {
  const { sweeps } = await get("/api/sweeps");
  el("sweepList").innerHTML = sweeps.map((s) => {
    const plist = s.params.map((p) => esc(p.label)).join(" × ");
    return `
    <div class="list-item" data-id="${esc(s.id)}">
      <div style="min-width:0">
        <div class="t">${esc(s.name)}</div>
        <div class="s">${esc(s.scene_name)} · ${plist} · ${s.completed}/${s.total} 组成功${s.failed ? ` · ${s.failed} 组失败` : ""} · ${esc(s.created_at)}</div>
      </div>
      <div class="row gap-6">
        ${statusBadge(s.status)}
        <button class="btn danger small sdel" data-id="${esc(s.id)}">删除</button>
      </div>
    </div>`;
  }).join("") || '<p class="muted small">暂无扫描。</p>';

  el("sweepList").querySelectorAll(".list-item").forEach((li) => {
    li.onclick = (ev) => {
      if (ev.target.closest(".sdel")) return;
      selectSweep(li.dataset.id);
    };
  });
  el("sweepList").querySelectorAll(".sdel").forEach((b) => {
    b.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm("确认删除该扫描？（扫描产生的运行仍保留在历史中）")) return;
      await del(`/api/sweeps/${b.dataset.id}`);
      if (currentSweep && currentSweep.id === b.dataset.id) {
        stopPolling(); currentSweep = null; el("resultCard").style.display = "none";
      }
      refreshList();
    };
  });
}

async function selectSweep(id) {
  stopPolling();
  currentSweep = await get(`/api/sweeps/${id}`);
  el("resultCard").style.display = "block";
  el("resultTitle").textContent = currentSweep.name;

  // Metric selector allows re-slicing finished results without re-running.
  const firstOk = currentSweep.points.find((p) => p.aggregates && Object.keys(p.aggregates).length);
  const keys = firstOk ? Object.keys(firstOk.aggregates) : [currentSweep.metric];
  el("metricSel").innerHTML = keys.map((k) =>
    `<option value="${esc(k)}">${esc(metricLabels[k] || k)}</option>`).join("");
  el("metricSel").value = currentSweep.metric;
  el("aggSel").value = currentSweep.aggregation;

  el("paramSel").style.display = currentSweep.scan_type === "oat" ? "" : "none";
  if (currentSweep.scan_type === "oat") {
    el("paramSel").innerHTML =
      '<option value="__rank__">影响程度排名（Tornado）</option>' +
      currentSweep.params.map((p) =>
        `<option value="${esc(p.key)}">${esc(p.label)} 的曲线</option>`).join("");
  }
  el("metricSel").onchange = renderResult;
  el("aggSel").onchange = renderResult;
  el("paramSel").onchange = renderResult;

  renderProgress(currentSweep);
  renderResult();
  if (!["finished", "partial", "stopped", "error"].includes(currentSweep.status)) {
    el("cancelSweep").style.display = "";
    pollSweep(id);
  } else {
    el("cancelSweep").style.display = "none";
  }
}

/* -------------------------------------------------------------------- */
/* Progress                                                              */
/* -------------------------------------------------------------------- */
function renderProgress(s) {
  const wrap = el("progressWrap");
  const running = ["running", "pending"].includes(s.status);
  wrap.style.display = running ? "block" : "none";
  if (!running) return;
  const done = s.completed + s.failed;
  const pct = s.total ? Math.round(done / s.total * 100) : 0;
  el("progressBar").style.width = pct + "%";
  const cur = s.points.find((p) => p.status === "running");
  el("progressText").textContent =
    `进度 ${done}/${s.total}（成功 ${s.completed} · 失败 ${s.failed}）${pct}%` +
    (cur ? ` · 正在运行：${pointLabel(cur)}` : "");
}

function pointLabel(p) {
  return currentSweep.params.map((spec) => {
    const v = p.param_values[spec.key];
    return `${spec.label}=${typeof v === "number" ? fmt(v, 4) : v}`;
  }).join(", ");
}

/* -------------------------------------------------------------------- */
/* Result: summary + chart + table                                       */
/* -------------------------------------------------------------------- */
function pointMetric(p, metric, agg) {
  const row = (p.aggregates || {})[metric];
  return row ? row[agg] : null;
}
function pointStd(p, metric, agg) {
  const row = (p.aggregates || {})[metric];
  return row ? row[`${agg}_std`] || 0 : 0;
}

function renderResult() {
  const s = currentSweep;
  if (!s) return;
  const metric = el("metricSel").value || s.metric;
  const agg = el("aggSel").value || s.aggregation;
  el("thMetric").textContent = `${metricLabels[metric] || metric} · ${AGG_LABEL[agg]}`;

  const ok = s.points.filter((p) => ["ok", "degraded"].includes(p.status));
  const failed = s.points.filter((p) => p.status === "failed");
  const pending = s.points.filter((p) => ["pending", "running"].includes(p.status));
  const vals = ok.map((p) => pointMetric(p, metric, agg)).filter((v) => v != null);
  el("resultSummary").innerHTML = `
    共 ${s.total} 组：成功 <b style="color:var(--green)">${ok.length}</b>，
    失败 <b style="color:var(--red)">${failed.length}</b>，
    ${pending.length ? `未完成 ${pending.length}，` : ""}
    指标区间 ${vals.length ? `[${fmt(Math.min(...vals), 4)}, ${fmt(Math.max(...vals), 4)}]` : "—"}。
    每个取值的实际步数与耗时见下表（开启提前结束时各组步数可能不同）。`;

  if (!chart) chart = echarts.init(el("sweepChart"), "dark");
  const mode = el("paramSel").style.display !== "none" && el("paramSel").value !== "__rank__"
    ? "oat-curve" : s.scan_type;
  if (mode === "line" || mode === "oat-curve") renderLine(s, metric, agg, mode);
  else if (mode === "heatmap") renderHeatmap(s, metric, agg);
  else renderRanking(s);
  renderTable(s, metric, agg);
  chart.resize();
}

function axisBase() {
  return {
    axisLine: { lineStyle: { color: "#26313f" } },
    splitLine: { lineStyle: { color: "#1b2430" } },
    axisLabel: { color: "#8b98a5" },
  };
}

function failedMarkLines(rows, metric, agg) {
  const failed = rows.filter((r) => r.p.status === "failed");
  if (!failed.length) return {};
  const ys = rows.map((r) => pointMetric(r.p, metric, agg)).filter((v) => v != null);
  const base = ys.length ? Math.min(...ys) : 0;
  return {
    symbol: ["none", "none"],
    lineStyle: { color: "#e74c3c", type: "dashed", width: 1.5 },
    label: { formatter: "✕ 失败", color: "#e74c3c", fontSize: 10, position: "start" },
    data: failed.map((r) => [
      { xAxis: r.x, yAxis: base },
      { xAxis: r.x, yAxis: base + (ys.length ? (Math.max(...ys) - base) * 0.9 : 1) },
    ]),
  };
}

/* One-parameter curve (also used for a single OAT parameter). */
function renderLine(s, metric, agg, mode) {
  let rows;
  let xParam;
  if (mode === "oat-curve") {
    xParam = el("paramSel").value;
    rows = s.points
      .filter((p) => p.baseline || p.varied === xParam)
      .map((p) => ({ x: p.param_values[xParam], p }))
      .sort((a, b) => a.x - b.x);
  } else {
    xParam = s.params[0].key;
    rows = s.points
      .slice()
      .sort((a, b) => a.param_values[xParam] - b.param_values[xParam])
      .map((p) => ({ x: p.param_values[xParam], p }));
  }
  const xLabel = (s.params.find((p) => p.key === xParam) || {}).label || xParam;
  const xs = rows.map((r) => r.x);
  const ys = rows.map((r) => pointMetric(r.p, metric, agg));
  const haveStd = s.replicates > 1;

  const series = [];
  if (haveStd) {
    // Stacked transparent band to shade ±1 sample std across replicates.
    series.push(
      { name: "_bandBase", type: "line", stack: "band", symbol: "none",
        lineStyle: { opacity: 0 }, itemStyle: { color: "transparent" },
        silent: true, data: rows.map((r) => {
          const y = pointMetric(r.p, metric, agg);
          return y == null ? null : +(y - pointStd(r.p, metric, agg)).toFixed(6);
        }) },
      { name: "±1 标准差", type: "line", stack: "band", symbol: "none",
        lineStyle: { opacity: 0 }, areaStyle: { color: "rgba(52,152,219,0.18)" },
        data: rows.map((r) => {
          const y = pointMetric(r.p, metric, agg);
          return y == null ? null : +(2 * pointStd(r.p, metric, agg)).toFixed(6);
        }) });
  }
  series.push({
    name: metricLabels[metric] || metric,
    type: "line", smooth: false, connectNulls: false,
    symbol: rows.length > 40 ? "none" : "circle", symbolSize: 6,
    lineStyle: { width: 2 },
    data: rows.map((r) => [r.x, pointMetric(r.p, metric, agg)]),
    markLine: failedMarkLines(rows, metric, agg),
    markPoint: {
      symbol: "pin", symbolSize: 42, itemStyle: { color: "#f39c12" },
      label: { color: "#fff", fontSize: 10 },
      data: rows.filter((r) => r.p.baseline)
        .map((r) => ({ coord: [r.x, pointMetric(r.p, metric, agg)], value: "基线" })),
    },
  });

  const zoomable = xs.length > 20;
  chart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#8b98a5" }, top: 0 },
    grid: { left: 64, right: 24, top: 46, bottom: zoomable ? 64 : 44 },
    xAxis: { type: "value", name: xLabel, nameLocation: "middle", nameGap: 28, ...axisBase() },
    yAxis: { type: "value", name: `${metricLabels[metric] || metric} · ${AGG_LABEL[agg]}`, ...axisBase() },
    dataZoom: zoomable
      ? [{ type: "inside" }, { type: "slider", height: 18, bottom: 16,
          borderColor: "#26313f", textStyle: { color: "#8b98a5" } }]
      : [],
    series,
  }, true);
}

/* Two-parameter heatmap; failed cells marked with red crosses. */
function renderHeatmap(s, metric, agg) {
  const [px, py] = [s.params[0], s.params[1]];
  const data = [];
  const failedPts = [];
  for (const p of s.points) {
    const x = p.param_values[px.key];
    const y = p.param_values[py.key];
    const v = pointMetric(p, metric, agg);
    if (v == null) {
      if (p.status === "failed") failedPts.push([x, y]);
    } else {
      data.push([x, y, +v.toFixed(6)]);
    }
  }
  const vs = data.map((d) => d[2]);
  chart.setOption({
    backgroundColor: "transparent",
    tooltip: {
      formatter: (o) => {
        if (o.seriesType === "scatter")
          return `${px.label}=${fmt(o.value[0], 4)}<br>${py.label}=${fmt(o.value[1], 4)}<br><b style="color:#e74c3c">运行失败</b>`;
        return `${px.label}=${fmt(o.value[0], 4)}<br>${py.label}=${fmt(o.value[1], 4)}<br>${metricLabels[metric] || metric}（${AGG_LABEL[agg]}）= <b>${fmt(o.value[2], 4)}</b>`;
      },
    },
    grid: { left: 80, right: 24, top: 30, bottom: 70 },
    xAxis: { type: "value", name: px.label, nameLocation: "middle", nameGap: 28, ...axisBase() },
    yAxis: { type: "value", name: py.label, nameLocation: "middle", nameGap: 56, ...axisBase() },
    visualMap: vs.length ? {
      min: Math.min(...vs), max: Math.max(...vs), calculable: true,
      orient: "horizontal", left: "center", bottom: 4,
      textStyle: { color: "#8b98a5" },
      inRange: { color: ["#1b3a5b", "#2e86c1", "#f1c40f", "#e74c3c"] },
    } : { show: false, min: 0, max: 1 },
    series: [
      { type: "heatmap", data,
        itemStyle: { borderColor: "#0e141b", borderWidth: 1 },
        emphasis: { itemStyle: { borderColor: "#fff", borderWidth: 1 } },
        label: { show: data.length <= 64, color: "#eaf0f6", fontSize: 10,
                 formatter: (o) => fmt(o.value[2], 2) } },
      { type: "scatter", symbol: "diamond", symbolSize: 12,
        itemStyle: { color: "#e74c3c" }, data: failedPts, z: 5 },
    ],
  }, true);
}

/* OAT tornado ranking: parameters sorted by relative effect on the metric. */
function renderRanking(s) {
  const metric = el("metricSel").value || s.metric;
  const agg = el("aggSel").value || s.aggregation;
  // Ranking was computed for the sweep's stored metric/agg; recompute live
  // when the user switched metric or aggregation.
  const ranking = (metric === s.metric && agg === s.aggregation && s.ranking.length)
    ? s.ranking
    : recomputeRanking(s, metric, agg);

  chart.setOption({
    backgroundColor: "transparent",
    title: { text: "影响程度排名（指标变化幅度 / 指标平均水平 ×100%）",
             left: "center", top: 0,
             textStyle: { color: "#8b98a5", fontSize: 13, fontWeight: "normal" } },
    tooltip: {
      formatter: (o) => {
        const r = ranking[ranking.length - 1 - o.dataIndex];
        const dir = r.direction >= 0 ? "增大 ↑" : "减小 ↓";
        return `<b>${esc(r.label)}</b><br>相对影响：<b>${fmt(r.relative_pct, 1)}%</b>`
             + `<br>指标绝对变化幅度：${fmt(r.range, 4)}<br>参数增大时指标${dir}`;
      },
    },
    grid: { left: 150, right: 40, top: 56, bottom: 40 },
    xAxis: { type: "value", name: "相对影响（%）", ...axisBase() },
    yAxis: { type: "category", data: ranking.map((r) => r.label).reverse(),
             ...axisBase(), splitLine: { show: false } },
    series: [{
      type: "bar", barWidth: "55%",
      itemStyle: {
        borderRadius: 4,
        color: (o) => {
          const v = o.value;
          if (v >= 30) return "#e74c3c";
          if (v >= 10) return "#f39c12";
          if (v >= 3) return "#2e86c1";
          return "#5d6d7e";
        },
      },
      label: { show: true, position: "right", color: "#8b98a5",
               formatter: (o) => fmt(o.value, 1) + "%" },
      data: ranking.map((r) => +r.relative_pct.toFixed(2)).reverse(),
    }],
  }, true);
}

function recomputeRanking(s, metric, agg) {
  const rows = [];
  const allVals = s.points
    .map((p) => pointMetric(p, metric, agg)).filter((v) => v != null);
  const scale = allVals.length
    ? allVals.reduce((a, b) => a + Math.abs(b), 0) / allVals.length : 0;
  for (const spec of s.params) {
    const pts = s.points
      .filter((p) => p.param_values[spec.key] !== undefined)
      .map((p) => ({ x: p.param_values[spec.key], y: pointMetric(p, metric, agg) }))
      .filter((r) => r.y != null)
      .sort((a, b) => a.x - b.x);
    const vs = pts.map((p) => p.y);
    const range = vs.length ? Math.max(...vs) - Math.min(...vs) : 0;
    rows.push({ key: spec.key, label: spec.label, points: pts, range,
                direction: vs.length >= 2 ? vs[vs.length - 1] - vs[0] : 0,
                relative_pct: scale ? Math.abs(range) / scale * 100 : 0 });
  }
  return rows.sort((a, b) => a.relative_pct - b.relative_pct);
}

/* -------------------------------------------------------------------- */
/* Per-point table                                                        */
/* -------------------------------------------------------------------- */
function renderTable(s, metric, agg) {
  const tb = el("pointsTable").querySelector("tbody");
  tb.innerHTML = s.points.map((p) => {
    const v = pointMetric(p, metric, agg);
    const runLinks = (p.runs || []).map((r) =>
      `<a class="btn small" href="/stats.html?run=${esc(r.run_id)}" title="查看该次运行的完整过程曲线">${esc(r.run_id.slice(-6))}</a>`
    ).join(" ");
    let note = p.error || "";
    if (p.early_stopped) {
      const reason = (p.runs || []).map((r) => r.stop_reason).find(Boolean);
      note = (note ? note + "；" : "") + (reason || "提前结束");
    }
    const rowCls = p.status === "failed" ? "row-failed"
      : ["running", "pending"].includes(p.status) ? "row-running" : "";
    return `<tr class="${rowCls}">
      <td class="num">${p.idx + 1}${p.baseline ? ' <span class="pill" title="使用场景当前参数值的基线组">基线</span>' : ""}</td>
      <td>${esc(pointLabel(p))}</td>
      <td>${statusBadge(p.status === "ok" ? "finished" : p.status)}</td>
      <td class="num">${p.steps == null ? "—" : `${p.steps} / ${s.steps}`}</td>
      <td class="num">${p.duration_s == null ? "—" : fmt(p.duration_s, 1)}</td>
      <td class="num">${v == null ? "—" : fmt(v, 4)}${s.replicates > 1 && v != null ? ` ±${fmt(pointStd(p, metric, agg), 3)}` : ""}</td>
      <td>${runLinks || "—"}${p.status === "running" ? " …" : ""}</td>
      <td class="muted small">${esc(note)}</td>
    </tr>`;
  }).join("");
}

/* -------------------------------------------------------------------- */
/* Init                                                                  */
/* -------------------------------------------------------------------- */
async function init() {
  catalogData = await get("/api/catalog");
  scenes = await fillSceneSelect(el("swScene"));
  el("swScene").onchange = onSceneChange;
  el("addParam").onclick = () => addParamRow();
  el("addParamValues").onclick = () => addParamRow(undefined, "values");
  el("runSweep").onclick = launchSweep;
  el("cancelSweep").onclick = cancelSweep;
  el("swMetric").onchange = () => {};
  onSceneChange();
  await refreshList();

  // Auto-open a sweep linked via ?sweep=... (e.g. from history page).
  const q = new URLSearchParams(window.location.search).get("sweep");
  if (q) selectSweep(q).catch(() => {});
}

window.addEventListener("resize", () => chart && chart.resize());
init().catch((e) => console.error(e));
