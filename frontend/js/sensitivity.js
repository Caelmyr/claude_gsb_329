/* Parameter sensitivity analysis: sweep config, live progress, curves / heatmap / ranking. */

let CATALOG = null;
let SCENES = [];
let LABELS = {};          // metric key -> label
let current = null;       // currently displayed sweep (detail incl. analysis)
let pollTimer = null;
let charts = {};          // dom id -> echarts instance

const AGG_LABEL = { final: "终值", peak: "峰值", mean: "均值" };
const MODE_LABEL = { curve: "单参数曲线", grid: "双参数网格", oat: "逐参数（OAT）" };

/* ------------------------------------------------------------------ */
/* Small helpers
/* ------------------------------------------------------------------ */
function chartOf(id) {
  const dom = el(id);
  if (!dom) return null;
  let c = echarts.getInstanceByDom(dom);
  if (!c) c = echarts.init(dom, "dark");
  charts[id] = c;
  return c;
}

function disposeCharts(...prefixes) {
  Object.keys(charts).forEach((id) => {
    if (prefixes.some((p) => id.startsWith(p))) { charts[id].dispose(); delete charts[id]; }
  });
}

function fmtDur(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}分${s % 60}秒`;
}

function fmtNum(v, digits = 3) {
  if (v == null) return "—";
  if (typeof v !== "number") return String(v);
  if (Number.isInteger(v)) return String(v);
  return Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(digits);
}

function sceneById(id) { return SCENES.find((s) => s.id === id); }

function numericParams() {
  const scene = sceneById(el("swScene").value);
  if (!scene) return [];
  const dom = CATALOG[scene.domain];
  return (dom.models[scene.model].params || []).filter((p) => p.type === "int" || p.type === "float");
}

/* ------------------------------------------------------------------ */
/* Create form
/* ------------------------------------------------------------------ */
function addParamRow() {
  const params = numericParams();
  if (!params.length) return;
  const used = [...document.querySelectorAll(".pKey")].map((s) => s.value);
  const next = params.find((p) => !used.includes(p.key)) || params[0];
  const row = document.createElement("div");
  row.className = "card param-row";
  row.style.cssText = "padding:10px;margin:8px 0";
  row.innerHTML = `
    <div class="row">
      <select class="pKey" style="max-width:230px">${params.map((p) =>
        `<option value="${esc(p.key)}" ${p.key === next.key ? "selected" : ""}>${esc(p.label)} (${esc(p.key)})</option>`).join("")}</select>
      <span class="muted small">从</span><input class="pFrom" type="number" step="any" style="width:100px">
      <span class="muted small">到</span><input class="pTo" type="number" step="any" style="width:100px">
      <span class="muted small">取</span><input class="pCount" type="number" value="6" min="2" max="40" style="width:70px">
      <span class="muted small">个值</span>
      <button class="btn danger small pDel">删除</button>
    </div>
    <input class="pValues" placeholder="或直接填写取值（逗号分隔，优先生效），如 0.1, 0.3, 0.5" style="margin-top:6px">`;
  el("paramRows").appendChild(row);

  const sync = () => {
    const spec = params.find((p) => p.key === row.querySelector(".pKey").value);
    if (spec) {
      row.querySelector(".pFrom").value = spec.min;
      row.querySelector(".pTo").value = spec.max;
    }
    updateEstimate();
  };
  row.querySelector(".pKey").onchange = sync;
  row.querySelector(".pDel").onclick = () => { row.remove(); updateEstimate(); updateModeRow(); };
  row.querySelectorAll("input").forEach((i) => { i.oninput = updateEstimate; });
  sync();
  updateModeRow();
}

function rowSpec(row) {
  const explicit = row.querySelector(".pValues").value.trim();
  if (explicit) {
    const vals = explicit.split(/[，,\s]+/).filter(Boolean).map(Number).filter((v) => !Number.isNaN(v));
    return { key: row.querySelector(".pKey").value, values: vals, n: vals.length };
  }
  return {
    key: row.querySelector(".pKey").value,
    from: parseFloat(row.querySelector(".pFrom").value),
    to: parseFloat(row.querySelector(".pTo").value),
    count: parseInt(row.querySelector(".pCount").value || "2", 10),
    n: parseInt(row.querySelector(".pCount").value || "2", 10),
  };
}

function currentMode() {
  const rows = document.querySelectorAll(".param-row").length;
  if (rows === 1) return "curve";
  if (rows === 2) return document.querySelector('input[name="swMode"]:checked').value;
  return "oat";
}

function updateModeRow() {
  const rows = document.querySelectorAll(".param-row").length;
  el("modeRow").style.display = rows === 2 ? "block" : "none";
  el("modeHint").textContent = rows === 1 ? "（单参数 → 敏感性曲线）"
    : rows === 2 ? "（双参数 → 网格热力图或逐参数对比）"
    : rows >= 3 ? "（多参数 → 逐参数 OAT 筛选 + 敏感性排序）" : "";
  updateEstimate();
}

function updateEstimate() {
  const rows = [...document.querySelectorAll(".param-row")];
  if (!rows.length) { el("swEstimate").textContent = "—"; return; }
  const counts = rows.map((r) => rowSpec(r).n || 0);
  const mode = currentMode();
  let combos;
  if (mode === "curve") combos = counts[0];
  else if (mode === "grid") combos = counts[0] * counts[1];
  else combos = 1 + counts.reduce((a, b) => a + b, 0);
  const reps = parseInt(el("swReps").value || "1", 10);
  const total = combos * Math.max(1, reps);
  const est = el("swEstimate");
  est.textContent = `约 ${total} 组`;
  est.style.color = total > 400 ? "var(--red)" : "";
}

function renderMetrics() {
  const scene = sceneById(el("swScene").value);
  const metrics = scene ? CATALOG[scene.domain].metrics : [];
  el("metricChecks").innerHTML = metrics.map((m, i) => `
    <label class="check"><input type="checkbox" class="mCheck" value="${esc(m.key)}" ${i === 0 ? "checked" : ""}> ${esc(m.label)}</label>`).join("");
}

async function createSweep() {
  const scene_id = el("swScene").value;
  if (!scene_id) { alert("请选择基础场景"); return; }
  const rows = [...document.querySelectorAll(".param-row")];
  if (!rows.length) { alert("请至少添加一个扫描参数"); return; }
  const keys = rows.map((r) => r.querySelector(".pKey").value);
  if (new Set(keys).size !== keys.length) { alert("扫描参数重复，请删除重复行"); return; }
  const params = rows.map((r) => {
    const spec = rowSpec(r);
    const out = { key: spec.key };
    if (spec.values) out.values = spec.values;
    else { out.from = spec.from; out.to = spec.to; out.count = spec.count; }
    return out;
  });
  const metrics = [...document.querySelectorAll(".mCheck:checked")].map((c) => c.value);
  const body = {
    name: el("swName").value.trim() || "敏感性扫描",
    scene_id,
    params,
    metrics,
    steps: parseInt(el("swSteps").value || "200", 10),
    replicates: parseInt(el("swReps").value || "1", 10),
    seed: parseInt(el("swSeed").value || "0", 10),
    mode: currentMode(),
  };
  const stat = el("swStatus");
  stat.textContent = "正在创建…";
  try {
    const sw = await post("/api/sweeps", body);
    stat.textContent = `已创建（共 ${sw.total} 组），后台运行中…`;
    await refreshList();
    selectSweep(sw.id);
  } catch (e) {
    stat.textContent = "创建失败：" + e.message;
  }
}

/* ------------------------------------------------------------------ */
/* Sweep list
/* ------------------------------------------------------------------ */
async function refreshList() {
  const { sweeps } = await get("/api/sweeps");
  el("sweepList").innerHTML = sweeps.map((s) => {
    const failed = s.failed ? ` · <span style="color:var(--red)">失败 ${s.failed} 组</span>` : "";
    return `
    <div class="list-item" data-id="${esc(s.id)}">
      <div style="min-width:0">
        <div class="t">${esc(s.name)} ${statusBadge(s.status)}</div>
        <div class="s">${MODE_LABEL[s.mode] || s.mode} · ${s.params.map((p) => esc(p.label)).join(" × ")} ·
          ${s.done}/${s.total} 组${failed} · ${esc(s.created_at)}</div>
      </div>
      <button class="btn danger small xdel" data-id="${esc(s.id)}">删除</button>
    </div>`;
  }).join("") || '<p class="muted small">暂无扫描。</p>';

  el("sweepList").querySelectorAll(".list-item").forEach((li) => {
    li.onclick = (ev) => { if (ev.target.closest(".xdel")) return; selectSweep(li.dataset.id); };
  });
  el("sweepList").querySelectorAll(".xdel").forEach((b) => {
    b.onclick = async () => {
      if (!confirm("确认删除该扫描？其产生的运行将一并删除。")) return;
      await del(`/api/sweeps/${b.dataset.id}`);
      if (current && current.id === b.dataset.id) {
        current = null;
        el("detailCard").style.display = "none";
      }
      refreshList();
    };
  });
}

/* ------------------------------------------------------------------ */
/* Detail view
/* ------------------------------------------------------------------ */
async function selectSweep(id) {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  current = await get(`/api/sweeps/${id}`);
  renderDetail();
  if (["running", "pending"].includes(current.status)) {
    refreshList();   // keep the list's progress counters in sync while polling
    pollTimer = setTimeout(() => selectSweep(id), 900);
  }
}

function renderDetail() {
  const sw = current;
  const a = sw.analysis;
  el("detailCard").style.display = "block";
  el("dTitle").innerHTML = `${esc(sw.name)} ${statusBadge(sw.status)}`;

  // metric / agg selectors (preserve selection across polls)
  const metricSel = el("dMetric");
  const prevMetric = metricSel.value;
  metricSel.innerHTML = sw.metrics.map((k) =>
    `<option value="${esc(k)}">${esc(LABELS[k] || k)}</option>`).join("");
  if (sw.metrics.includes(prevMetric)) metricSel.value = prevMetric;
  metricSel.onchange = renderCharts;
  el("dAgg").onchange = renderCharts;

  // summary chips
  const s = a.summary;
  const chips = [
    `<span class="chip">已完成 ${s.done}/${s.total} 组</span>`,
    s.failed ? `<span class="chip bad">失败 ${s.failed} 组</span>` : `<span class="chip good">全部成功</span>`,
    `<span class="chip">累计耗时 ${fmtDur(s.elapsed_ms)}</span>`,
    s.done ? `<span class="chip">平均每组 ${fmtDur(s.avg_ms)}</span>` : "",
    s.ok ? `<span class="chip">实际步数 ${s.steps_min}${s.steps_min !== s.steps_max ? "–" + s.steps_max : ""} 步</span>` : "",
  ];
  el("dChips").innerHTML = chips.filter(Boolean).join("");

  // progress + ETA from measured per-group durations
  const running = ["running", "pending"].includes(sw.status);
  el("stopSweep").style.display = running ? "" : "none";
  el("dProgress").style.display = running ? "block" : "none";
  if (running) {
    const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
    el("dProgress").querySelector(".bar").style.width = `${pct}%`;
    const remaining = s.total - s.done;
    el("dEta").textContent = s.done
      ? `已完成 ${s.done}/${s.total}（${pct}%），按实测平均速度预计还需约 ${fmtDur(s.avg_ms * remaining)}`
      : `已排队 ${s.total} 组，正在启动…`;
  } else {
    el("dEta").textContent = sw.status === "stopped"
      ? `已手动停止：实际完成 ${s.done}/${s.total} 组，以下为已完成部分的结果。`
      : sw.status === "error" ? `扫描出错：${esc(sw.error || "全部组合失败")}`
      : `扫描完成：实际运行 ${s.done} 组，成功 ${s.ok} 组，失败 ${s.failed} 组。`;
  }

  // failed combinations
  el("dFailed").innerHTML = a.failed_points.length ? `
    <div class="notice error" style="margin-top:10px">
      <b>${a.failed_points.length} 组取值运行失败（已跳过，不影响其余结果）：</b>
      <ul style="margin:6px 0 0;padding-left:18px">
        ${a.failed_points.slice(0, 10).map((p) =>
          `<li>${esc(p.label.replace(sw.name + " · ", ""))} — ${esc(p.error)}</li>`).join("")}
        ${a.failed_points.length > 10 ? `<li>… 其余 ${a.failed_points.length - 10} 组见下表</li>` : ""}
      </ul>
    </div>` : "";

  renderCharts();
  renderTable();
}

function renderCharts() {
  const sw = current;
  if (!sw) return;
  const metric = el("dMetric").value || sw.metrics[0];
  const agg = el("dAgg").value || "final";
  renderRanking(metric, agg);
  if (sw.mode === "grid" && sw.analysis.heatmap) renderHeatmap(metric, agg);
  else renderCurves(metric, agg);
}

/* ---- sensitivity ranking (which parameters matter) ----------------- */
function renderRanking(metric, agg) {
  const sw = current;
  const sens = sw.analysis.sensitivity || {};
  const keys = Object.keys(sens);
  const card = el("dRankCard");
  if (keys.length < 2) { card.style.display = "none"; return; }
  const rows = keys
    .map((k) => ({
      key: k,
      label: (sw.params.find((p) => p.key === k) || {}).label || k,
      s: sens[k] && sens[k][metric] ? sens[k][metric][agg] : null,
    }))
    .filter((r) => r.s)
    .sort((x, y) => x.s.norm - y.s.norm);
  if (rows.length < 2) { card.style.display = "none"; return; }
  card.style.display = "block";

  const maxNorm = rows[rows.length - 1].s.norm;
  const chart = chartOf("rankChart");
  chart.setOption({
    backgroundColor: "transparent",
    tooltip: {
      formatter: (p) => {
        const r = rows[p.dataIndex];
        return `${esc(r.label)}<br>归一化影响幅度：${fmtNum(r.s.norm)}<br>` +
          `指标极差：${fmtNum(r.s.range)}<br>最低 @ ${fmtNum(r.s.min_at)} · 最高 @ ${fmtNum(r.s.max_at)}`;
      },
    },
    grid: { left: 110, right: 60, top: 10, bottom: 26 },
    xAxis: { type: "value", axisLine: { lineStyle: { color: "#26313f" } },
             splitLine: { lineStyle: { color: "#1b2430" } } },
    yAxis: { type: "category", data: rows.map((r) => r.label),
             axisLine: { lineStyle: { color: "#26313f" } } },
    series: [{
      type: "bar",
      data: rows.map((r) => ({
        value: r.s.norm,
        itemStyle: { color: r.s.norm === maxNorm ? "#4f8cff" : r.s.norm < 0.05 ? "#3a4656" : "#6ea8ff" },
      })),
      label: { show: true, position: "right", color: "#8b98a5",
               formatter: (p) => fmtNum(p.value) },
      barMaxWidth: 22,
    }],
  }, true);

  const top = rows[rows.length - 1];
  const weak = rows.filter((r) => r.s.norm < 0.05).map((r) => r.label);
  el("rankNote").innerHTML =
    `对「${esc(LABELS[metric] || metric)}（${AGG_LABEL[agg]}）」影响最大：<b>${esc(top.label)}</b>` +
    `（归一化幅度 ${fmtNum(top.s.norm)}）` +
    (weak.length ? `；基本无关：<span class="muted">${esc(weak.join("、"))}</span>（幅度 &lt; 0.05）` : "；其余参数均有可见影响。");
}

/* ---- sensitivity curves (single param or OAT) ---------------------- */
function renderCurves(metric, agg) {
  const sw = current;
  disposeCharts("hm", "curve");
  const cont = el("dCharts");
  const params = sw.params;
  cont.innerHTML = params.map((p, i) => `
    <h3 style="margin:14px 0 6px">${esc(p.label)} → ${esc(LABELS[metric] || metric)}（${AGG_LABEL[agg]}）</h3>
    <div class="chart" id="curve${i}" style="height:${params.length > 1 ? 260 : 380}px"></div>`).join("");

  params.forEach((p, i) => {
    const rows = (((sw.analysis.curves || {})[p.key] || {})[metric] || {})[agg] || [];
    const chart = chartOf(`curve${i}`);
    if (!rows.length) {
      chart.clear();
      chart.setOption({
        backgroundColor: "transparent",
        title: { text: "暂无成功数据", left: "center", top: "middle",
                 textStyle: { color: "#8b98a5", fontSize: 13 } },
      }, true);
      return;
    }
    const hasBand = rows.some((r) => r.n > 1);
    const series = [];
    if (hasBand) {
      series.push(
        { name: "min", type: "line", data: rows.map((r) => [r.value, r.min]), stack: "band",
          lineStyle: { opacity: 0 }, symbol: "none", silent: true },
        { name: "波动范围", type: "line", data: rows.map((r) => [r.value, r.max - r.min]), stack: "band",
          lineStyle: { opacity: 0 }, symbol: "none", areaStyle: { opacity: 0.18 }, silent: true },
      );
    }
    series.push({
      name: hasBand ? "均值" : (LABELS[metric] || metric),
      type: "line", smooth: true, symbolSize: 7,
      data: rows.map((r) => [r.value, r.mean]),
    });
    const failedAt = rows.filter((r) => r.failed > 0);
    if (failedAt.length) {
      series.push({
        name: "含失败取值", type: "scatter", symbol: "triangle", symbolSize: 10,
        itemStyle: { color: "#e74c3c" },
        data: failedAt.map((r) => [r.value, r.mean]),
      });
    }
    chart.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis",
        formatter: (ps) => {
          const row = rows[ps[0].dataIndex];
          let html = `<b>${esc(p.label)} = ${fmtNum(row.value)}</b><br>` +
            `${esc(LABELS[metric] || metric)}（${AGG_LABEL[agg]}）：<b>${fmtNum(row.mean)}</b>`;
          if (row.n > 1) html += `<br>重复 ${row.n} 次，范围 ${fmtNum(row.min)} ~ ${fmtNum(row.max)}`;
          if (row.failed) html += `<br><span style="color:#e74c3c">该取值另有 ${row.failed} 组失败</span>`;
          return html;
        },
      },
      grid: { left: 64, right: 24, top: 30, bottom: 46 },
      xAxis: { type: "value", name: p.label, min: "dataMin", max: "dataMax",
               axisLine: { lineStyle: { color: "#26313f" } },
               splitLine: { lineStyle: { color: "#1b2430" } } },
      yAxis: { type: "value", scale: true,
               axisLine: { lineStyle: { color: "#26313f" } },
               splitLine: { lineStyle: { color: "#1b2430" } } },
      series,
    }, true);
  });
}

/* ---- 2-parameter heatmap ------------------------------------------- */
function renderHeatmap(metric, agg) {
  const sw = current;
  disposeCharts("curve", "hm");
  const hm = sw.analysis.heatmap;
  const cont = el("dCharts");
  cont.innerHTML = `
    <h3 style="margin:14px 0 6px">${esc(hm.x_label)} × ${esc(hm.y_label)} → ${esc(LABELS[metric] || metric)}（${AGG_LABEL[agg]}）</h3>
    <div class="chart" id="hm0" style="height:440px"></div>`;
  const cells = ((hm.grids[metric] || {})[agg]) || [];
  const chart = chartOf("hm0");
  const okCells = cells.filter((c) => c.mean != null);
  if (!okCells.length) {
    chart.clear();
    chart.setOption({
      backgroundColor: "transparent",
      title: { text: "暂无成功数据", left: "center", top: "middle",
               textStyle: { color: "#8b98a5", fontSize: 13 } },
    }, true);
    return;
  }
  const xi = (v) => hm.x_values.indexOf(v);
  const yi = (v) => hm.y_values.indexOf(v);
  const vals = okCells.map((c) => c.mean);
  const failedCells = cells.filter((c) => c.failed > 0 && c.mean == null);
  chart.setOption({
    backgroundColor: "transparent",
    tooltip: {
      formatter: (p) => {
        const c = p.data.cell;
        if (!c) return "";
        let html = `<b>${esc(hm.x_label)} = ${fmtNum(c.x)}，${esc(hm.y_label)} = ${fmtNum(c.y)}</b><br>`;
        html += c.mean != null
          ? `${esc(LABELS[metric] || metric)}（${AGG_LABEL[agg]}）：<b>${fmtNum(c.mean)}</b>`
          : `<span style="color:#e74c3c">该组合运行失败</span>`;
        if (c.n > 1) html += `<br>重复 ${c.n} 次`;
        if (c.failed) html += `<br><span style="color:#e74c3c">失败 ${c.failed} 组</span>`;
        return html;
      },
    },
    grid: { left: 90, right: 90, top: 30, bottom: 60 },
    xAxis: { type: "category", name: hm.x_label, data: hm.x_values.map((v) => fmtNum(v)),
             axisLine: { lineStyle: { color: "#26313f" } }, splitArea: { show: true } },
    yAxis: { type: "category", name: hm.y_label, data: hm.y_values.map((v) => fmtNum(v)),
             axisLine: { lineStyle: { color: "#26313f" } }, splitArea: { show: true } },
    visualMap: {
      min: Math.min(...vals), max: Math.max(...vals),
      calculable: true, orient: "vertical", right: 6, top: "center",
      seriesIndex: 0,
      textStyle: { color: "#8b98a5" },
      inRange: { color: ["#16324f", "#1f6f8b", "#2ecc71", "#f1c40f", "#e74c3c"] },
    },
    series: [
      { type: "heatmap",
        data: okCells.map((c) => ({ value: [xi(c.x), yi(c.y), c.mean], cell: c })),
        label: { show: okCells.length <= 200, color: "#dfe7ef", fontSize: 10,
                 formatter: (p) => fmtNum(p.value[2], 2) },
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.6)" } } },
      { type: "scatter", symbol: "rect", symbolSize: 14,
        itemStyle: { color: "transparent", borderColor: "#e74c3c", borderWidth: 1.5 },
        data: failedCells.map((c) => ({ value: [xi(c.x), yi(c.y)], cell: c })),
        z: 3 },
    ],
  }, true);
}

/* ---- points table --------------------------------------------------- */
function renderTable() {
  const sw = current;
  const metric = el("dMetric").value || sw.metrics[0];
  const rows = sw.points || [];
  const cap = 300;
  const head = `<tr><th>#</th><th>参数取值</th><th>重复</th><th>状态</th>
    <th class="num">步数</th><th class="num">耗时</th>
    <th class="num">${esc(LABELS[metric] || metric)}·终值</th>
    <th class="num">峰值</th><th class="num">均值</th><th>运行</th></tr>`;
  const body = rows.slice(0, cap).map((p, i) => {
    const m = (p.metrics || {})[metric] || {};
    const vals = Object.keys(p.values || {}).length
      ? Object.entries(p.values).map(([k, v]) => `${esc(k)}=${fmtNum(v)}`).join(", ")
      : "基线";
    const status = p.status === "ok"
      ? '<span class="badge finished">成功</span>'
      : `<span class="badge error" title="${esc(p.error)}">失败</span>`;
    const run = p.run_id ? `<a href="/stats.html?run=${esc(p.run_id)}">查看</a>` : "—";
    return `<tr>
      <td class="num">${i + 1}</td><td class="mono small">${vals}</td>
      <td class="num">${p.replicate + 1}</td><td>${status}</td>
      <td class="num">${p.steps_run}</td><td class="num">${fmtDur(p.duration_ms)}</td>
      <td class="num">${fmtNum(m.final)}</td><td class="num">${fmtNum(m.peak)}</td>
      <td class="num">${fmtNum(m.mean)}</td><td>${run}</td></tr>`;
  }).join("");
  el("dTable").innerHTML = head + body +
    (rows.length > cap ? `<tr><td colspan="10" class="muted">仅显示前 ${cap} 组，共 ${rows.length} 组</td></tr>` : "");
}

/* ------------------------------------------------------------------ */
/* Init
/* ------------------------------------------------------------------ */
async function init() {
  const { domains } = await get("/api/catalog");
  CATALOG = domains;
  for (const d of Object.values(domains)) {
    for (const m of d.metrics) LABELS[m.key] = m.label;
  }
  SCENES = await fillSceneSelect(el("swScene"));
  el("swScene").onchange = () => {
    el("paramRows").innerHTML = "";
    addParamRow();
    renderMetrics();
  };
  el("addParam").onclick = () => {
    if (document.querySelectorAll(".param-row").length >= 4) { alert("最多 4 个扫描参数"); return; }
    addParamRow();
  };
  el("createSweep").onclick = createSweep;
  el("stopSweep").onclick = async () => {
    if (!current) return;
    await post(`/api/sweeps/${current.id}/stop`);
    el("dEta").textContent = "已请求停止：当前这一组跑完后即停，已完成部分的结果保留。";
  };
  document.querySelectorAll('input[name="swMode"]').forEach((r) => { r.onchange = updateEstimate; });
  ["swSteps", "swReps"].forEach((id) => { el(id).oninput = updateEstimate; });

  addParamRow();
  renderMetrics();
  await refreshList();

  const qs = new URLSearchParams(window.location.search).get("sweep");
  if (qs) selectSweep(qs);
}

window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));

init().catch((e) => console.error(e));
