"use strict";
import {apply, newLive, Rejected} from "/static/stream.js";
import {buildCsv, channelRows, ENDS, gapMark, ORIGINS, reasonText, rowState} from "/static/replay.js";

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const state = {
  credential: sessionStorage.getItem("htsg.credential") || "", me: null, config: null, page: 1,
  live: newLive(), ws: null, stopped: true, reconnect: null, lastMessage: 0, stalled: false, drawing: false,
};

// ------------------------------------------------------------------------------------------ helpers

// A missing or invalid number is "—", never 0: an empty cell must not read as a measurement.
function fmt(value, digits = state.config?.ui?.precision ?? 2) {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", {maximumFractionDigits: digits}) : "—";
}
function clock(seconds) {
  return Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleTimeString("zh-CN", {hour12: false}) : "—";
}
function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}
const badge = (text, kind = "") => `<span class="badge ${kind}">${esc(text)}</span>`;
function table(head, rows) {
  const body = rows.length ? rows.map(row => `<tr>${row.map(x => `<td>${x}</td>`).join("")}</tr>`).join("")
    : `<tr><td colspan="${head.length}" class="empty">暂无数据</td></tr>`;
  return `<div class="table-wide"><table><thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// ---------------------------------------------------------------------------- channel definitions

const definitions = () => [...(state.config?.points || []), ...(state.config?.derived || [])];
const definition = id => definitions().find(p => p.id === id) || {id, unit: ""};
const label = id => state.config?.ui?.point_labels?.[id] || id;
const channel = id => state.live.channels?.[id] ?? null;

// What a channel shows: a value only while the owner says it is valid; otherwise "—" and the reason.
function valueText(id) {
  const view = channel(id);
  return view && view.valid ? `${fmt(view.value)} ${esc(unitText(definition(id).unit))}` : "—";
}
function stateBadges(id) {
  const view = channel(id);
  if (!view) return badge("等待实时数据", "warn");
  let out = view.valid ? badge("有效", "ok") : badge(reasonText(view.reason), "bad");
  if (view.fault === "PENDING") out += " " + badge("越限待确认", "warn");
  if (view.fault === "CONFIRMED") out += " " + badge(`故障已确认${view.fault_direction === "HIGH" ? "（高）" : view.fault_direction === "LOW" ? "（低）" : ""}`, "bad");
  if (view.estimate) out += " " + badge("研究估算", "warn");
  return out;
}

// ------------------------------------------------------------------------------------------- pages

const pages = [
  {id: 1, name: "系统总览", html: overviewHtml, update: updateOverview},
  {id: 2, name: "水冷壁应变场", html: strainHtml, update: updateStrain},
  {id: 4, name: "热应力分析", html: trendHtml, mount: mountTrend, update: updateTrend},
  {id: 5, name: "历史查询", html: historyHtml, mount: mountHistory},
  {id: 12, name: "系统自检", html: selfcheckHtml, mount: loadHealth, update: updateSelfcheck},
  {id: 13, name: "通信·日志", html: commHtml, mount: mountComm},  // no `update`: nothing here is redrawn from the live stream
  {id: 14, name: "系统管理·权限", html: permissionsHtml, mount: mountPeople},
];
const currentPage = () => pages.find(p => p.id === state.page) || pages[0];

function renderPage() {
  if (!state.config) return;
  const page = currentPage();
  $("navigation").innerHTML = pages.map(p => `<button data-page="${p.id}" class="${p.id === page.id ? "active" : ""}">${esc(p.name)}</button>`).join("");
  $("navigation").querySelectorAll("button").forEach(b => b.onclick = () => { state.page = Number(b.dataset.page); renderPage(); });
  $("workspace").innerHTML = `<div class="page-heading"><div><span class="eyebrow">HTSG</span><h2>${esc(page.name)}</h2><span class="muted">${esc(state.config.project?.title || "")}</span></div><span class="badge">数据来源：后端协议主动读取</span></div>${page.html()}`;
  page.mount?.();
  renderLive();
}
function renderLive() {
  if (!state.config) return;
  currentPage().update?.();
  renderBanner();
  const live = state.live;
  $("stream-time").textContent = live.channels ? `最新消息：${clock(state.lastMessage / 1000)} · 序号 ${live.seq}` : "等待后端数据";
}
// Many messages can arrive in one frame; the page is drawn once per frame, from the state as it then is.
function invalidate() {
  if (state.drawing) return;
  state.drawing = true;
  requestAnimationFrame(() => { state.drawing = false; renderLive(); });
}

// -------------------------------------------------------------- pages 1 and 2: overview and strain (终版 §17.1-17.2)

// Three different quantities per station, never mixed: the bridge signal, the converted strain and the change.
const stations = () => state.config?.ui?.stations || [];
const bridgeOf = s => definition(s.strain).inputs?.[0];
const SOURCE_STATES = {WAITING: "等待首次数据", VALID: "数据有效", EXPIRED: "数据已失效", RESYNC: "重新同步中"};
const KINDS = {modbus: "Modbus", opcua: "OPC UA"};
const STRAIN_HEAD = ["测点", "原始信号 (mV/V)", "换算应变 (με，GF=6，未作温度补偿)", "应变变化 (με，相邻两个计划轮次的增量)", "状态"];

// One state per station, from the owner's own validity and fault state: why a block is grey is named, never guessed.
function stationState(s) {
  const bridge = channel(bridgeOf(s)), strain = channel(s.strain), change = channel(s.strain_change);
  if (!strain) return {state: "invalid", why: "等待实时数据"};
  const root = bridge && !bridge.valid ? bridge : strain;
  if (!root.valid) return {state: "invalid", why: `无效：${reasonText(root.reason)}`};
  const faults = [strain, change].filter(v => v && v.fault);
  const confirmed = faults.find(v => v.fault === "CONFIRMED");
  if (confirmed) return {state: "confirmed", why: `故障已确认${confirmed.fault_direction === "HIGH" ? "（高）" : confirmed.fault_direction === "LOW" ? "（低）" : ""}`};
  if (faults.some(v => v.fault === "PENDING")) return {state: "pending", why: "越限待确认"};
  return {state: "normal", why: "正常"};
}
const STATE_KIND = {normal: "ok", pending: "warn", confirmed: "bad", invalid: ""};
const stationBadge = st => badge(st.why, STATE_KIND[st.state]);
const quantity = (id, digits) => { const view = channel(id); return view && view.valid ? `${fmt(view.value, digits)} ${esc(unitText(definition(id).unit))}` : "—"; };

function strainTable() {
  return table(STRAIN_HEAD, stations().map(s => [esc(s.label || s.id), quantity(bridgeOf(s), 4), quantity(s.strain), quantity(s.strain_change), stationBadge(stationState(s))]));
}
const STRAIN_NOTE = '<p class="chart-caption">“换算应变”按 GF=6 换算、未作温度补偿，不是已验证的机械应变；原始信号（mV/V）、换算应变（με）和应变变化（με）是三种不同的量，分别显示，互不覆盖。</p>';

function overviewHtml() {
  return `<div id="source-status" class="grid metrics"></div>
    <div class="card" style="margin-bottom:16px"><h3>越限状态</h3><div id="fault-summary"></div></div>
    <div class="grid two"><div class="card"><h3>过程量 · 实时</h3><div id="process-table"></div></div>
    <div class="card"><h3>应变测点 · 三种量分别显示</h3><div id="strain-table"></div>${STRAIN_NOTE}</div></div>`;
}
function updateOverview() {
  const sources = state.live.status?.sources || {};
  $("source-status").innerHTML = Object.entries(sources).map(([id, s]) => `<div class="card metric"><small>${esc(KINDS[s.kind] || s.kind)} · ${esc(id)}</small><div class="value" style="font-size:16px">${esc(SOURCE_STATES[s.state] || s.state)}</div>${s.alarm ? badge("通信告警", "bad") : badge("通信正常", "ok")} ${s.enabled ? "" : badge("已禁用")}<br><small>最后通信 ${clock(s.last_comm_wall)}</small></div>`).join("") || '<p class="muted">等待实时数据</p>';
  const views = Object.values(state.live.channels || {});
  const pending = views.filter(v => v.fault === "PENDING").length, confirmed = views.filter(v => v.fault === "CONFIRMED").length;
  $("fault-summary").innerHTML = state.live.channels
    ? `${badge(`越限待确认 ${pending} 个通道`, pending ? "warn" : "")} ${badge(`故障已确认 ${confirmed} 个通道`, confirmed ? "bad" : "")} <span class="muted">持续越限满 5 秒确认；此处只显示当前状态，不另设报警、确认或等级。</span>`
    : '<span class="muted">等待实时数据</span>';
  $("process-table").innerHTML = table(["过程量", "数值", "状态"], (state.config.ui.process_points || []).map(id => [esc(label(id)), valueText(id), stateBadges(id)]));
  $("strain-table").innerHTML = strainTable();
}

function strainHtml() {
  return `<div class="grid two"><div class="card"><h3>测点分布示意 · 8 个固定测点</h3><div id="strain-map" class="map strain-map"></div>
    <div class="legend"><b>● 正常</b><span style="color:var(--amber)">● 越限待确认</span><em>● 故障已确认</em><span>● 无效（灰色，注明原因）</span></div>
    <p class="chart-caption">8 个固定测点各一个色块；颜色只表示该测点的有效性与越限状态，不在测点之间插值，不构成连续应变场；示意坐标仅用于布局，不代表现场拓扑。</p></div>
    <div class="card"><h3>三种量 · 分别显示</h3><div id="strain-table"></div>${STRAIN_NOTE}</div></div>`;
}
function updateStrain() {
  $("strain-map").innerHTML = stations().map(s => {
    const st = stationState(s), x = Math.max(24, Math.min(76, Number(s.x) || 0)), y = Math.max(10, Math.min(90, Number(s.y) || 0));
    return `<div class="block" data-station="${esc(s.id)}" data-state="${st.state}" style="left:${x}%;top:${y}%"><b>${esc(s.label || s.id)}</b>
      <div>换算应变 <strong>${quantity(s.strain)}</strong></div>
      <div class="minor">原始信号 ${quantity(bridgeOf(s), 4)}</div>
      <div class="minor">应变变化 ${quantity(s.strain_change)}</div>
      <div class="why">${esc(st.why)}</div></div>`;
  }).join("");
  $("strain-table").innerHTML = strainTable();
}

// ----------------------------------------------------------------- page 4: live three-panel trend (终版 §17.3)

// Only what this page receives is plotted: the groups since it connected.  Custom history lives on page 5.
const TREND_SECONDS = 300, KEEP_SECONDS = 600;
const trend = {series: new Map(), pair: 0, cursor: null};
const signed = (value, digits = 2) => typeof value === "number" && Number.isFinite(value) ? (value > 0 ? "+" : "") + fmt(value, digits) : "—";
const UNIT_TEXT = {degC: "℃"};
const unitText = unit => UNIT_TEXT[unit] || unit || "";

const pairs = () => state.config._pairs ??= findPairs();  // the configuration does not change within a session
function findPairs() {
  const derived = state.config.derived, same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
  return derived.filter(d => d.operation === "difference").map(d => {
    const stress = derived.find(x => x.operation === "thermal_stress" && same(x.inputs, d.inputs));
    return {delta: d.id, stress: stress?.id, inner: d.inputs[0], outer: d.inputs[1], name: label(d.id).replace(/\s*内外壁温差$/, "")};
  }).filter(p => p.stress);
}
const loadId = () => (state.config.ui.process_points || []).find(id => id.includes("load")) || "load_mw";
const trendIds = () => state.config._trendIds ??= new Set([loadId(), ...pairs().flatMap(p => [p.delta, p.stress])]);

// A series is a list of {t, v, valid}: the value in effect from t on.  An invalid state or a lost connection is a
// point with no value, and nothing is drawn through it.
function trendNote(channels, wall) {
  const wanted = trendIds();
  for (const [id, view] of Object.entries(channels)) {
    if (!wanted.has(id)) continue;
    const list = trend.series.get(id) || [], last = list[list.length - 1], valid = view.valid && Number.isFinite(view.value);
    if (last && last.valid === valid && last.v === (valid ? view.value : null)) continue;
    list.push({t: wall, v: valid ? view.value : null, valid});
    while (list.length > 2 && list[1].t < wall - KEEP_SECONDS) list.shift();
    trend.series.set(id, list);
  }
}
function trendBreak(wall) {
  for (const list of trend.series.values()) if (list.length && list[list.length - 1].valid) list.push({t: wall, v: null, valid: false});
}
const valueAt = (list, t) => { let found = null; for (const p of list) { if (p.t <= t) found = p; else break; } return found; };

function trendHtml() {
  return `<div class="controls"><label>温度对<select id="pair-select">${pairs().map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join("")}</select></label><span id="pair-inputs" class="muted"></span><span class="badge warn">热应力为研究估算</span></div>
    <div class="card"><h3>负荷 · 内外壁温差 · 热应力代理（统一时间轴，最近 ${TREND_SECONDS / 60} 分钟）</h3>
      <div id="trend-latest" class="controls"></div>
      <canvas id="trend-canvas" class="chart" style="height:560px" aria-label="三分栏实时趋势"></canvas>
      <div id="trend-readout" class="chart-caption">把鼠标移到图上查看同一时刻三栏各自的值。</div>
      <p class="chart-caption">三栏共用同一条时间轴并带同步游标，各栏纵轴独立、保留单位与正负号，不做归一化、相关或回归；共同时间轴仅便于观察，不表示三者同步采样。热应力是基于温差与约束假设的研究代理量，不是实测应力；无效或连接中断处断开、不连线。本页只显示页面连接后收到的实时趋势，自定义历史查询请到“历史查询”页。</p></div>`;
}
function mountTrend() {
  $("pair-select").value = String(trend.pair);
  $("pair-select").onchange = event => { trend.pair = Number(event.target.value); updateTrend(); };
  const canvas = $("trend-canvas");
  canvas.onmousemove = event => {
    const plot = canvas.plot;
    if (!plot) return;
    const x = event.clientX - canvas.getBoundingClientRect().left;
    trend.cursor = x >= plot.left && x <= plot.right ? plot.t0 + (x - plot.left) / (plot.right - plot.left) * TREND_SECONDS : null;
    updateTrend();
  };
  canvas.onmouseleave = () => { trend.cursor = null; updateTrend(); };
}
function trendPanels() {
  const p = pairs()[trend.pair], ids = [loadId(), p.delta, p.stress];
  // the difference and the stress can be either sign and say so; the load is a plain quantity
  return ids.map((id, i) => ({id, name: i === 0 ? "负荷" : i === 1 ? "内外壁温差" : "热应力代理（研究估算）", unit: unitText(definition(id).unit), points: trend.series.get(id) || [], show: i === 0 ? fmt : signed}));
}
function updateTrend() {
  const canvas = $("trend-canvas");
  if (!canvas || !pairs().length) return;
  const p = pairs()[trend.pair], panels = trendPanels(), now = state.serverWall || Date.now() / 1000;
  $("pair-inputs").textContent = `内壁 ${p.inner} · 外壁 ${p.outer}（温差与热应力代理都由这一对算出）`;
  $("trend-latest").innerHTML = panels.map(x => { const view = channel(x.id); return `<span data-panel="${esc(x.id)}">${esc(x.name)}：<b>${view && view.valid ? `${x.show(view.value)} ${esc(x.unit)}` : "—"}</b></span> ${stateBadges(x.id)}`; }).join(" ");
  $("trend-readout").textContent = trend.cursor === null ? "把鼠标移到图上查看同一时刻三栏各自的值。"
    : `游标 ${clock(trend.cursor)}（各栏取游标时刻之前最近一次的值，不代表同步采样）：` + panels.map(x => { const v = valueAt(x.points, trend.cursor); return `${x.name} ${v && v.valid ? `${x.show(v.v)} ${x.unit}` : "—（无数据或无效）"}`; }).join(" ｜ ");
  plotTrend(canvas, panels, now);
}
function plotTrend(canvas, panels, now) {
  const ratio = window.devicePixelRatio || 1, w = canvas.clientWidth, h = 560, left = 70, right = w - 16, gap = 22, top0 = 10, height = (h - 34 - top0 - gap * 2) / 3, t0 = now - TREND_SECONDS;
  canvas.width = w * ratio;
  canvas.height = h * ratio;
  const c = canvas.getContext("2d");
  c.scale(ratio, ratio);
  c.font = '11px "Segoe UI",sans-serif';
  const x = t => left + (Math.min(Math.max(t, t0), now) - t0) / TREND_SECONDS * (right - left);
  canvas.plot = {left, right, t0, now, panels: []};
  panels.forEach((panel, i) => {
    const top = top0 + i * (height + gap), bottom = top + height;
    const inside = panel.points.filter(q => q.t <= now), seen = inside.filter(q => q.valid && q.t >= t0);
    const held = valueAt(inside, t0);
    const values = [...seen.map(q => q.v), ...(held && held.valid ? [held.v] : [])];
    let lo = Math.min(...values), hi = Math.max(...values);
    if (!values.length) { lo = 0; hi = 1; }
    const pad = (hi - lo) * 0.2 || Math.max(1, Math.abs(lo) * 0.02);
    lo -= pad; hi += pad;
    const y = v => bottom - (v - lo) / (hi - lo) * height;
    canvas.plot.panels.push({id: panel.id, unit: panel.unit, lo, hi, top, bottom, points: inside.filter(q => q.t >= t0 || q === held).map(q => ({...q}))});
    c.fillStyle = "#91aac5";
    c.fillText(`${panel.name} (${panel.unit})`, left + 4, top + 11);
    for (let k = 0; k <= 2; k++) {
      const yy = top + height * k / 2;
      c.strokeStyle = "#304968"; c.beginPath(); c.moveTo(left, yy); c.lineTo(right, yy); c.stroke();
      c.fillStyle = "#91aac5"; c.fillText(panel.show(hi - (hi - lo) * k / 2, 1), 2, yy + 4);
    }
    inside.forEach((q, j) => {
      const next = inside[j + 1], a = x(q.t), b = next ? x(next.t) : x(now);
      if (b < left || x(q.t) > right) return;
      if (q.valid) {
        c.strokeStyle = "#73bcff"; c.lineWidth = 1.8;
        c.beginPath(); c.moveTo(Math.max(a, left), y(q.v)); c.lineTo(Math.max(b, a + 1), y(q.v)); c.stroke();
        if (next && next.valid) { c.beginPath(); c.moveTo(b, y(q.v)); c.lineTo(b, y(next.v)); c.stroke(); }
      } else {
        c.fillStyle = "#ff777b"; c.fillRect(Math.max(a, left), bottom - 5, Math.max(3, b - Math.max(a, left)), 5);
      }
    });
    if (trend.cursor !== null) { c.strokeStyle = "#ffbd66"; c.beginPath(); c.moveTo(x(trend.cursor), top); c.lineTo(x(trend.cursor), bottom); c.stroke(); }
  });
  c.fillStyle = "#91aac5";
  c.fillText(clock(t0), left, h - 8);
  c.fillText(clock(now), right - 52, h - 8);
}

// ------------------------------------------------------------------------------- page 5: history (终版 §14)

// The replay settings.  A locked fault only fills the centre time and the default channel; every field stays
// editable, and a query never needs a fault.
const replay = {fault: null, center: null, seconds: 300, points: [], chart: null};
const faults = {items: [], cursor: null, more: false, loading: false, epoch: 0, partial: false, error: "", query: ""};

const stamp = seconds => Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString("zh-CN", {hour12: false}) : "—";
const toLocalInput = seconds => Number.isFinite(seconds) ? new Date(seconds * 1000 - new Date(seconds * 1000).getTimezoneOffset() * 60000).toISOString().slice(0, 19) : "";
const fromLocalInput = text => text ? new Date(text).getTime() / 1000 : null;
const faultChannels = () => definitions().filter(d => d.high != null || d.low != null);
const option = (id, selected = false) => `<option value="${esc(id)}" ${selected ? "selected" : ""}>${esc(label(id))} · ${esc(id)}${definition(id).unit ? ` (${esc(definition(id).unit)})` : ""}</option>`;

function historyHtml() {
  return `<div class="history-layout">
    <aside class="card" id="fault-panel" aria-label="故障记录">
      <h3>故障记录</h3>
      <div class="filters">
        <label>检索通道<input id="fault-search" placeholder="名称或编号" autocomplete="off"></label>
        <label>通道<select id="fault-point"></select></label>
        <label>方向<select id="fault-direction"><option value="">全部</option><option value="HIGH">高</option><option value="LOW">低</option></select></label>
        <label>起始<input id="fault-start" type="datetime-local" step="1"></label>
        <label>截止<input id="fault-end" type="datetime-local" step="1"></label>
        <div class="controls"><button id="fault-find" class="primary">检索</button><button id="fault-reset">清除过滤</button></div>
      </div>
      <div id="fault-list" class="scroll"></div>
      <div class="controls"><span id="fault-count" class="muted"></span><button id="fault-more" hidden>加载更多</button></div>
    </aside>
    <section id="history-center">
      <div class="card" id="replay-settings">
        <h3>回溯设置</h3>
        <p id="replay-lock" class="muted"></p>
        <div class="controls">
          <label>中心时间（本地）<input id="replay-center" type="datetime-local" step="1"></label>
          <label>窗口长度（秒）<input id="replay-seconds" type="number" min="1" max="31622400" step="1"></label>
        </div>
        <div class="controls"><label>查看通道<input id="replay-filter" placeholder="检索名称或编号" autocomplete="off"></label></div>
        <select id="replay-points" multiple size="7" aria-label="查看通道"></select>
        <div class="controls"><button id="replay-run" class="primary">查询</button><button id="replay-clear">清空设置</button></div>
        <p class="chart-caption">窗口取中心时间前后各半个窗口；可同时查看同一窗口内其他已保存通道。选择故障只填入时间点和默认通道，也可全部手动填写。</p>
      </div>
      <div class="card" id="replay-board"><h3>回溯结果</h3><div id="replay-result"></div></div>
    </section>
  </div>`;
}
function mountHistory() {
  $("fault-point").innerHTML = `<option value="">全部通道</option>${faultChannels().map(d => option(d.id)).join("")}`;
  $("replay-points").innerHTML = definitions().map(d => option(d.id, replay.points.includes(d.id))).join("");
  const narrow = (input, select, keepAll) => input.oninput = () => {
    const text = input.value.trim().toLowerCase();
    for (const o of select.options) o.hidden = !!text && !o.textContent.toLowerCase().includes(text) && !(keepAll && o.value === "");
  };
  narrow($("fault-search"), $("fault-point"), true);
  narrow($("replay-filter"), $("replay-points"), false);
  $("fault-find").onclick = () => loadFaults(true);
  $("fault-reset").onclick = () => {
    for (const id of ["fault-search", "fault-point", "fault-direction", "fault-start", "fault-end"]) $(id).value = "";
    $("fault-search").oninput();
    loadFaults(true);
  };
  $("fault-more").onclick = () => loadFaults(false);
  $("fault-list").onclick = event => { const row = event.target.closest("[data-fault]"); if (row) selectFault(row.dataset.fault); };
  $("replay-center").onchange = event => { replay.center = fromLocalInput(event.target.value); saveReplay(); drawLock(); };
  $("replay-seconds").onchange = event => {
    if (!event.target.checkValidity()) { event.target.reportValidity(); return; }
    replay.seconds = Number(event.target.value);
    saveReplay();
  };
  $("replay-points").onchange = event => { replay.points = [...event.target.selectedOptions].map(o => o.value); saveReplay(); };
  $("replay-run").onclick = runReplay;
  $("replay-clear").onclick = () => { clearReplay(); syncSettings(); drawFaults(); drawReplay(); };
  $("replay-lock").onclick = event => { if (event.target.id === "unlock") { replay.fault = null; saveReplay(); drawLock(); drawFaults(); } };
  syncSettings();
  loadFaults(true);
  drawReplay();
  if (replayReady()) runReplay();  // entering the page asks the database again with the kept settings
}
function syncSettings() {
  $("replay-center").value = toLocalInput(replay.center);
  $("replay-seconds").value = replay.seconds;
  for (const o of $("replay-points").options) o.selected = replay.points.includes(o.value);
  drawLock();
}
function drawLock() {
  const f = replay.fault;
  $("replay-lock").innerHTML = f
    ? `参考故障：${esc(label(f.point_id))} · ${stamp(f.confirmed_wall)} · ${f.direction === "HIGH" ? "高" : "低"}${replay.center !== f.confirmed_wall ? "（中心时间已手动修改）" : ""} <button id="unlock">解除锁定</button>`
    : "未锁定故障：请手动填写，或在左侧选择一条故障。";
}
function selectFault(id) {
  const f = faults.items.find(x => x.id === id);
  if (!f) return;
  replay.fault = f;
  replay.center = f.confirmed_wall;
  replay.points = [f.point_id];
  syncSettings();
  drawFaults();
  runReplay();
}
function faultFilter() {
  return {point: $("fault-point").value, direction: $("fault-direction").value, start: fromLocalInput($("fault-start").value), end: fromLocalInput($("fault-end").value)};
}
async function loadFaults(fresh) {
  if (fresh) {  // a new search: the filter is read once and then kept for "load more" (a cursor belongs to its query)
    Object.assign(faults, {items: [], cursor: null, more: false, partial: false});
    faults.query = new URLSearchParams(Object.entries(faultFilter()).filter(([, v]) => v !== "" && v !== null)).toString();
  }
  const epoch = ++faults.epoch;
  Object.assign(faults, {loading: true, error: ""});
  drawFaults();
  try {
    const result = await api(`/api/faults?limit=20&${faults.query}${!fresh && faults.cursor ? `&cursor=${encodeURIComponent(faults.cursor)}` : ""}`);
    if (epoch !== faults.epoch) return;  // a newer search replaced this one
    const seen = new Set(faults.items.map(x => x.id));
    faults.items.push(...result.items.filter(x => !seen.has(x.id)));
    Object.assign(faults, {cursor: result.next_cursor, more: result.has_more, partial: result.partial});
  } catch (error) {
    if (epoch === faults.epoch) faults.error = error.message;
  } finally {
    if (epoch === faults.epoch) { faults.loading = false; drawFaults(); }
  }
}
function drawFaults() {
  if (!$("fault-list")) return;
  $("fault-list").innerHTML = faults.items.map(f => `<button class="fault-row ${replay.fault?.id === f.id ? "selected" : ""}" data-fault="${esc(f.id)}"><b>${esc(label(f.point_id))}</b> ${badge(f.direction === "HIGH" ? "高" : "低", "bad")}<br><small>${stamp(f.confirmed_wall)} · ${fmt(f.basis_value)} ${esc(definition(f.point_id).unit)}</small></button>`).join("")
    || `<p class="empty">${faults.loading ? "正在读取…" : "没有符合条件的故障记录"}</p>`;
  $("fault-count").textContent = faults.error ? `读取失败：${faults.error}` : `已显示 ${faults.items.length} 条${faults.partial ? " · 部分数据库不可读，列表不完整" : ""}`;
  $("fault-more").hidden = !faults.more;
  $("fault-more").disabled = faults.loading;
}

// ----------------------------------------------------------------- page 5: the replay itself (终版 §14)

// Only the SETTINGS live in sessionStorage - never a result.  Logging out or clearing removes them.
const REPLAY_KEY = "htsg.replay";
function loadReplay() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(REPLAY_KEY));
    if (saved && Array.isArray(saved.points) && (saved.center === null || Number.isFinite(saved.center))) {
      Object.assign(replay, {fault: saved.fault ?? null, center: saved.center, seconds: Number(saved.seconds) || 300, points: saved.points.filter(x => typeof x === "string"), chart: saved.chart ?? null});
    }
  } catch { /* unreadable settings are simply not used */ }
}
function saveReplay() {
  try { sessionStorage.setItem(REPLAY_KEY, JSON.stringify({fault: replay.fault, center: replay.center, seconds: replay.seconds, points: replay.points, chart: replay.chart})); } catch { /* no storage: settings last this page only */ }
}
function clearReplay() {
  sessionStorage.removeItem(REPLAY_KEY);
  Object.assign(replay, {fault: null, center: null, seconds: 300, points: [], chart: null});
  view.controller?.abort();
  Object.assign(view, {epoch: view.epoch + 1, loading: false, result: null, request: null, error: ""});
}

const view = {epoch: 0, loading: false, result: null, request: null, error: "", controller: null};
const replayReady = () => Number.isFinite(replay.center) && replay.seconds > 0 && replay.points.length > 0;
const stampMs = seconds => `${stamp(seconds)}.${String(Math.round((seconds % 1) * 1000) % 1000).padStart(3, "0")}`;

// Always asks the database again: the page keeps settings, not data.  A newer request cancels the older one,
// and an answer that still arrives late is ignored, so an old window can never be shown under new settings.
async function runReplay() {
  if (!replayReady()) { drawReplay(); return; }
  if (!replay.points.includes(replay.chart)) replay.chart = replay.points[0];
  saveReplay();
  view.controller?.abort();
  const controller = view.controller = new AbortController();
  const epoch = ++view.epoch;
  const request = {center: replay.center, seconds: replay.seconds, points: [...replay.points], chart: replay.chart, fault: replay.fault};
  Object.assign(view, {loading: true, error: "", result: null});
  drawReplay();
  try {
    const query = new URLSearchParams({center: request.center, seconds: request.seconds, points: request.points.join(",")});
    const result = await api(`/api/replay?${query}`, {signal: controller.signal});
    if (epoch !== view.epoch) return;
    Object.assign(view, {result, request});
  } catch (error) {
    if (epoch === view.epoch && error.name !== "AbortError") view.error = error.message;
  } finally {
    if (epoch === view.epoch) { view.loading = false; drawReplay(); }
  }
}

const lastOf = rows => rows.length ? rows[rows.length - 1] : null;

function startBasis(rows, result, unit) {
  const held = rows.filter(r => r.held);
  if (held.length) return held.map(r => `${r.valid ? `${fmt(r.value)} ${esc(unit || "")}`.trim() : "无效"}（原观测 ${stamp(r.observed_at)}，${ORIGINS[r.origin] || r.origin}）`).join("；");
  return rows.some(r => r.time <= result.start) ? "窗口起点处的记录" : "窗口起点前无可依据的值";
}

function drawReplay() {
  const box = $("replay-result");
  if (!box) return;
  if (!replayReady()) { box.innerHTML = '<p class="muted">请填写中心时间和至少一个通道，或在左侧选择一条故障。不会自动查询当前时刻。</p>'; return; }
  if (view.loading) { box.innerHTML = '<p class="muted">正在从数据库读取…</p>'; return; }
  if (view.error) { box.innerHTML = `<p class="log-error" role="alert">历史读取失败：${esc(view.error)}</p>`; return; }
  if (!view.result) { box.innerHTML = '<p class="muted">设置已填好，点击“查询”。</p>'; return; }
  const {result, request} = view;
  const coverage = Number.isFinite(result.coverage_complete_until) ? `所有来源均确认到 ${stamp(result.coverage_complete_until)}` : "覆盖终点未知（至少一个来源无法确认）";
  const notes = [];
  if (result.partial) notes.push(`数据库 ${result.unavailable.map(esc).join("、")} 无法读取，结果不完整；不能据此断定窗口内没有记录。`);
  if (result.truncated) notes.push(`已达每通道 ${result.limit} 行的显示上限，下列通道被截断：${Object.entries(result.channels).filter(([, c]) => c.truncated).map(([id]) => esc(label(id))).join("、")}。请缩短窗口。`);
  for (const gap of result.gaps) notes.push(`归档缺口（${esc(gap.source_id)}）：${stamp(gap.start_wall)} → ${Number.isFinite(gap.end_wall) ? stamp(gap.end_wall) : "未恢复"}，原因 ${esc(gap.reason)}；缺口内的数值未知，不连线。`);
  const summary = request.points.map(id => {
    const channel = result.channels[id], rows = channelRows(channel), last = lastOf(rows);
    const warn = rows.some(r => !r.valid);
    return [`${esc(label(id))}<br><small>${esc(id)}</small>`, esc(channel.unit || ""), startBasis(rows, result, channel.unit), `${rows.filter(r => !r.held).length}${channel.truncated ? " + 截断" : ""}`,
      last ? (last.valid ? `${fmt(last.value)}` : `— ${badge(rowState(last), "bad")}`) : "—", warn ? badge("含无效或极值记录", "bad") : badge("全部有效", "ok")];
  });
  const focus = request.chart, focusData = result.channels[focus], rows = channelRows(focusData);
  box.innerHTML = `<p id="replay-meta">查询时刻 ${stamp(result.query_time)} · 窗口 ${stamp(result.start)} → ${stamp(result.end)}（中心 ${stamp(result.center)}，${fmt(result.seconds, 0)} 秒）· ${coverage}</p>
    ${notes.map(n => `<div class="note" role="alert">${n}</div>`).join("")}
    <div id="replay-summary">${table(["通道", "单位", "窗口起点依据", "窗口内记录", "窗口内最后一条", "状态"], summary)}</div>
    <div class="controls"><label>图表与记录通道<select id="replay-focus">${request.points.map(id => option(id, id === focus)).join("")}</select></label><button id="replay-export">导出 CSV</button></div>
    <canvas id="replay-chart" class="chart" aria-label="回溯图表"></canvas>
    <p class="chart-caption">阶梯线表示该值在生效期间一直有效；虚线起点为窗口起点的保持值（保留原观测时间）；红色表示无效或极值；阴影为归档缺口，竖虚线为有效段起点。不插值、不补点。</p>
    <div id="replay-records" class="scroll">${recordsTable(rows, focusData, result)}</div>`;
  $("replay-focus").onchange = event => { replay.chart = event.target.value; runReplay(); };
  $("replay-export").onclick = exportCsv;
  plotReplay($("replay-chart"), focusData, result);
}

function recordsTable(rows, channel, result) {
  const head = ["生效时间", "原观测时间", "批内序号", "值", "质量 / 状态", "来源", "种类", "有效段", "说明", "缺口标记"];
  if (!rows.length) return table(head, []);
  return `<div class="table-wide"><table><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r =>
    `<tr class="${r.valid ? "" : "bad"} ${r.held ? "held" : ""}"><td>${stampMs(r.time)}</td><td>${stampMs(r.observed_at)}</td><td>${r.batch_seq}${r.item_seq != null ? `·${r.item_seq}` : ""}</td><td>${r.valid ? fmt(r.value) : "—"}</td><td>${esc(rowState(r))}</td><td>${ORIGINS[r.origin] || esc(r.origin)}</td><td>${r.kind === "raw" ? "原始" : "派生"}</td><td>${esc(r.run)}/${r.seg}</td><td>${r.held ? "窗口起点的保持值；" : ""}${r.until == null ? "其后未知" : `${ENDS[r.ended_by] || ""} ${stamp(r.until)}`}</td><td>${gapMark(r, channel, result)}</td></tr>`).join("")}</tbody></table></div>`;
}

// The file is made from the result on screen - no new query - and carries the byte-order mark spreadsheets need.
function exportCsv() {
  if (!view.result) return;
  const {result, request} = view, focus = request.chart;
  const text = buildCsv({result, request, fault: request.fault, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, label, unit: id => definition(id).unit || ""});
  const url = URL.createObjectURL(new Blob(["\ufeff", text], {type: "text/csv;charset=utf-8"}));
  const link = Object.assign(document.createElement("a"), {href: url, download: `htsg-replay-${focus}-${Math.round(result.center)}.csv`});
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function plotReplay(canvas, channel, result) {
  const ratio = window.devicePixelRatio || 1, w = canvas.clientWidth, h = 240, left = 62, right = w - 14, top = 14, bottom = h - 28;
  canvas.width = w * ratio;
  canvas.height = h * ratio;
  const c = canvas.getContext("2d");
  c.scale(ratio, ratio);
  c.font = '11px "Segoe UI",sans-serif';
  canvas.plot = {left, right, top, bottom, start: result.start, end: result.end};  // the geometry, for tests that read pixels
  const {start, end} = result, x = t => left + (Math.min(Math.max(t, start), end) - start) / (end - start) * (right - left);
  const rows = channelRows(channel), good = rows.filter(r => r.valid && Number.isFinite(r.value));
  c.fillStyle = "#91aac5";
  c.fillText(channel.unit || "", 4, 12);
  for (const gap of result.gaps) {  // unknown time: shaded, nothing drawn through it
    const a = x(gap.start_wall), b = Number.isFinite(gap.end_wall) ? x(gap.end_wall) : right;
    c.fillStyle = "#ffbd6633";
    c.fillRect(a, top, Math.max(2, b - a), bottom - top);
  }
  c.strokeStyle = "#55708f";
  c.setLineDash([2, 4]);
  for (const s of channel.segments) if (Number.isFinite(s.start_wall) && s.start_wall > start && s.start_wall < end) { c.beginPath(); c.moveTo(x(s.start_wall), top); c.lineTo(x(s.start_wall), bottom); c.stroke(); }
  c.setLineDash([]);
  c.fillStyle = "#91aac5";
  c.fillText(clock(start), left, h - 8);
  c.fillText(clock(end), right - 52, h - 8);
  if (!good.length) { c.fillText("窗口内没有有效数值可画", left + 12, (top + bottom) / 2); }
  let lo = Math.min(...good.map(r => r.value)), hi = Math.max(...good.map(r => r.value));
  if (good.length) {
    const pad = (hi - lo) * 0.15 || Math.max(1, Math.abs(lo) * 0.01);
    lo -= pad; hi += pad;
    for (let i = 0; i <= 4; i++) {
      const yy = top + (bottom - top) * i / 4;
      c.strokeStyle = "#304968"; c.beginPath(); c.moveTo(left, yy); c.lineTo(right, yy); c.stroke();
      c.fillStyle = "#91aac5"; c.fillText(fmt(hi - (hi - lo) * i / 4), 2, yy + 4);
    }
  }
  const y = v => bottom - (v - lo) / (hi - lo) * (bottom - top);
  rows.forEach((r, i) => {
    const a = x(r.time), b = r.until == null ? a : x(r.until);
    if (r.valid && Number.isFinite(r.value)) {
      c.strokeStyle = "#73bcff"; c.fillStyle = "#73bcff"; c.lineWidth = 1.8;
      c.setLineDash(r.held ? [5, 3] : []);
      c.beginPath(); c.moveTo(a, y(r.value)); c.lineTo(Math.max(b, a + 1), y(r.value)); c.stroke();
      c.setLineDash([]);
      const next = rows[i + 1];
      if (r.ended_by === "NEXT_CHANGE" && next && next.valid && Number.isFinite(next.value)) { c.beginPath(); c.moveTo(b, y(r.value)); c.lineTo(b, y(next.value)); c.stroke(); }
      c.beginPath(); c.arc(a, y(r.value), r.held ? 3.5 : 2.5, 0, Math.PI * 2); r.held ? c.stroke() : c.fill();
      if (r.until == null) { c.fillStyle = "#91aac5"; c.fillText("?", a + 5, y(r.value) - 5); }
    } else {
      c.fillStyle = "#ff777b"; c.fillRect(a, bottom - 6, Math.max(3, b - a), 6);
    }
  });
}

// ------------------------------------------- pages 12, 14 and the archive banner (终版 §11, §12, §15)

const ARCHIVE_STATES = {OK: ["正常", "ok"], LOST: ["已中断，缺口尚未恢复", "bad"], RECOVERING: ["恢复中，等待确认写入", "warn"]};
const ROUTES = {PRIMARY: "正常库", EXTERNAL: "移动库", EMERGENCY: "应急库", UNKNOWN: "未知"};
const ROLES = {admin: "管理员", viewer: "只读用户"};
const health = {data: null, error: "", seq: 0};
const HEALTH_PAGES = [12, 13];  // the only pages that read /api/health, and only while they are in front

// What the page in front of the operator must say about the archive, whichever page that is.
function renderBanner() {
  const status = state.live.status, f = status?.faults, parts = [];
  if (f?.archive_incomplete) {
    const lost = Object.entries(f.loss || {}).map(([id, l]) => `${id}：${l.count} 条，${stamp(l.first_wall)} → ${stamp(l.last_wall)}`).join("；");
    parts.push(`故障归档不完整：未写入的故障缓冲 ${f.buffered}/${f.limit}，已拒绝 ${f.rejected_total} 条新确认的故障${lost ? `（尚未写入摘要：${lost}）` : ""}。实时的“已确认”状态仍然正确，但历史故障列表缺少这些记录。`);
  }
  for (const [id, s] of Object.entries(status?.sources || {})) {
    if (s.archive !== "OK") parts.push(`历史归档${s.archive === "LOST" ? "中断" : "恢复中"}：${id}。实时值仍在更新，但这一段历史可能缺失。`);
  }
  $("archive-banner").textContent = parts.join("　");
  $("archive-banner").hidden = parts.length === 0;
}

async function loadHealth() {
  const mine = ++health.seq;
  let data = null, error = "";
  try { data = await api("/api/health"); } catch (failure) { error = failure.message; }
  if (mine !== health.seq || state.stopped || !HEALTH_PAGES.includes(state.page)) return;  // an older answer, or the page was left
  health.data = data ?? health.data;
  health.error = error;
  if (state.page === 12) updateSelfcheck(); else drawComm();
}
setInterval(() => { if (!state.stopped && HEALTH_PAGES.includes(state.page)) loadHealth(); }, 5000);

// ------------------------------------------------------------- the two counters of a source, on two pages

// Both come from the same /api/health answer as everything else on the communication page, so the two pages agree.
const COUNTER_NOTE = "重新同步次数：实际进入重新同步的次数（已在重新同步中的重试不计），连续有效通信 60 秒后清零，达到 5 次为持续失步；"
  + "连续建连失败次数：自上次建连成功以来已完成并失败的建连尝试次数，建连成功立即清零（订阅或读取失败不计）。"
  + "二者与日志里的合并次数含义不同，互不替代。每 5 秒随健康状态刷新。";
function counterCells(id) {
  const s = health.data?.status?.sources?.[id];
  if (!s) return ["—", "—"];
  return [`${s.resync_count}${s.persistent_resync ? " " + badge("持续失步", "bad") : ""}`, String(s.connect_failures)];
}

// ----------------------------------------------------------------------------------- page 12: self-check

function selfcheckHtml() {
  return `<div class="grid two"><div class="card"><h3>数据源与通信</h3><div id="check-sources"></div></div><div class="card"><h3>OPC UA 订阅协商参数</h3><div id="check-negotiated"></div></div></div>
    <div class="grid two"><div class="card"><h3>历史归档</h3><div id="check-archive"></div></div><div class="card"><h3>故障记录归档</h3><div id="check-faults"></div></div></div>
    <div class="card"><h3>运行时限（配置值）</h3><div id="check-timing"></div></div>
    <div class="note">传感器零漂、自检信号、硬件温度与设备运行时间：未提供硬件诊断输入。此处展示的是软件采集、协商参数与归档的实际状态。</div>`;
}
function negotiatedRows(n) {
  const r = n.requested || {}, rows = [
    ["发布周期（秒）", r.publish_interval, n.publish_interval], ["保活计数", r.keepalive_count, n.keepalive_count],
    ["寿命计数", r.lifetime_count, n.lifetime_count], ["测点队列（最小值）", r.queue_size, n.min_queue], ["会话超时（秒）", r.session_timeout, n.session_timeout],
  ];
  return rows.map(([name, asked, got]) => [name, fmt(asked, 3), `<b>${fmt(got, 3)}</b>${asked !== got ? " " + badge("与请求不同", "warn") : ""}`]);
}
function updateSelfcheck() {
  const status = state.live.status, data = health.data, timing = state.config.timing || {};
  const sources = Object.entries(status?.sources || {});
  $("check-sources").innerHTML = table(["数据源", "类型", "数据状态", "通信", "最后通信", "重新同步次数", "连续建连失败次数"], sources.map(([id, s]) => [esc(id), esc(KINDS[s.kind] || s.kind), esc(SOURCE_STATES[s.state] || s.state), s.alarm ? badge("通信告警", "bad") : badge("正常", "ok"), clock(s.last_comm_wall), ...counterCells(id)]))
    + `<p class="chart-caption">${esc(COUNTER_NOTE)}</p>`;
  const negotiated = data?.negotiated || {};
  $("check-negotiated").innerHTML = health.error ? `<p class="log-error">读取失败：${esc(health.error)}</p>` : sources.filter(([, s]) => s.kind === "opcua").map(([id]) => {
    const n = negotiated[id];
    if (!n) return `<h3>${esc(id)}</h3><p class="muted">尚未取得协商结果（订阅未建立）。</p>`;
    const limit = n.queue_capability === "OVERWRITE_UNDETECTABLE"
      ? badge("覆盖不可检测", "warn") + " 服务端把测点队列修订为 1：中间通知可能被覆盖且没有溢出标记；发布序号连续不能证明监控项无丢值。"
      : badge("队列溢出可检测", "ok") + " 队列大于 1，溢出会被检测并按失步处理。";
    return `<h3>${esc(id)}</h3>${table(["参数", "请求值", "服务端实际授予"], negotiatedRows(n))}<p>${limit}</p>${(n.violations || []).map(v => `<p class="log-error">不满足约束：${esc(v)}</p>`).join("")}`;
  }).join("") || '<p class="muted">没有 OPC UA 数据源。</p>';
  const archive = data?.archive;
  $("check-archive").innerHTML = !archive ? '<p class="muted">正在读取…</p>' : `<p>写入目标：${badge(ROUTES[archive.routing] || archive.routing || "未知")}${archive.queue ? ` · 写入队列 ${archive.queue.depth}/${archive.queue.capacity} · 已确认 ${archive.queue.committed} 组 · 被拒 ${archive.queue.refused} · 失败重试 ${archive.queue.failures + archive.queue.unknown}${archive.queue.last_error ? ` · 最近错误 ${esc(archive.queue.last_error)}` : ""}` : ""}</p>`
    + table(["数据源", "归档状态", "已确认水位（历史覆盖终点）", "缺口起点"], Object.entries(archive.sources).map(([id, s]) => {
      const [text, kind] = ARCHIVE_STATES[s.archive] || [s.archive, "warn"];
      return [esc(id), badge(text, kind), s.confirmed_watermark == null ? "尚无已确认水位：只能说明已存的点，不推断其后" : stamp(s.confirmed_watermark), s.gap_since == null ? "—" : stamp(s.gap_since)];
    }));
  const f = status?.faults;
  $("check-faults").innerHTML = !f ? '<p class="muted">等待实时数据</p>' : `<p>${f.archive_incomplete ? badge("故障归档不完整", "bad") : badge("故障归档完整", "ok")} 未写入的缓冲 <b>${f.buffered}/${f.limit}</b> · 累计拒绝 <b>${f.rejected_total}</b> 条</p>`
    + table(["数据源", "缺口摘要：丢失条数", "最早", "最晚", "涉及通道"], Object.entries(f.loss || {}).map(([id, l]) => [esc(id), l.count, stamp(l.first_wall), stamp(l.last_wall), esc((l.points || []).join("、"))]))
    + `<p class="chart-caption">故障记录在写入数据库之前只在内存中，进程退出会丢失。缓冲满额时保留已有记录、拒绝新记录，不覆盖、不阻塞采集。</p>`;
  $("check-timing").innerHTML = table(["项目", "值"], [
    ["故障确认时长", `${timing.fault_confirm_seconds} 秒`], ["OPC UA 数据有效期 / 断连告警", `${timing.ua_data_validity} 秒 / ${timing.ua_alarm_seconds} 秒`], ["Modbus 断连告警", `${timing.modbus_alarm_seconds} 秒`],
    ["推送合并 / 空闲心跳", `${timing.push_coalesce * 1000} 毫秒 / ${timing.push_heartbeat} 秒`], ["前端消息延迟提示", `${timing.ui_stall_notice} 秒`], ["故障缓冲上限", `${timing.fault_buffer_limit} 条`], ["水位提交间隔", `${timing.watermark_interval} 秒`],
  ]);
}

// ------------------------------------------------------------------------ page 13: communication and log (终版 §15.1)

// Everything on this page comes from ONE /api/health answer, read every 5 s while the page is in front and replaced as a
// whole - the input table is never patched with live (WebSocket) values, so a row cannot mix two observations and the
// page cannot look fresher than the moment it was read.  The log is this run's memory only, no level, no history.
const COMM_EVENTS = {
  NEW_GENERATION: "新连接/订阅代次建立", FIRST_COMM: "首次有效通信", COMM_RECOVERED: "通信恢复（中断后首个有效响应）",
  COMM_ALARM_CLEARED: "通信告警解除", RESYNC: "进入重新同步", DATA_EXPIRED: "数据失效", COMM_ALARM: "通信告警",
  APP_LOSS: "应用丢失（入口满）", POLL_TIMEOUT: "Modbus 请求无应答", POLL_EXCEPTION: "Modbus 协议异常应答",
};
const COMM_PHASES = {CONNECT: "建连", SUBSCRIBE: "建订阅"};
const COMM_ENDS = {
  REASON_CHANGED: "原因改变", CONNECT_OK: "建连成功", SUBSCRIBE_OK: "订阅创建成功", SUCCESS: "请求成功", NEW_GENERATION: "新代次建立",
};
const commEvent = e => e.kind === "ATTEMPT" ? `${COMM_PHASES[e.phase] || e.phase}尝试失败` : COMM_EVENTS[e.event] || e.event;
const commEnd = reason => COMM_ENDS[reason] || (String(reason).startsWith("EVENT:") ? `其他事件（${COMM_EVENTS[reason.slice(6)] || reason.slice(6)}）` : reason);
const stampAt = seconds => Number.isFinite(seconds) ? stampMs(seconds) : "—";  // a missing time is a dash, never ".000"
const stateCode = code => code ? ` 0x${(code >>> 0).toString(16).toUpperCase()}` : "";

function commHtml() {
  return `<div class="note">本页是只读诊断：只显示<b>本次运行</b>内存中的通信记录，重启后清空，不读取归档，也没有更早的通信历史。读数是按“状态获取时刻”取的一次快照，不代表各点同时采样；页面刷新不是新的观测。源时间戳只按服务器给出的原样显示，不据此判断过期、断连或冻结。</div>
    <p id="comm-taken" class="chart-caption"></p>
    <div class="card"><h3>数据源状态</h3><div id="comm-sources"></div></div>
    <div class="card"><h3>点位映射与实际读数</h3><div id="comm-inputs" class="scroll"></div></div>
    <div class="card"><h3>通信日志（本次运行）</h3><div id="comm-log-meta"></div><div id="comm-log" class="scroll"></div></div>`;
}
function mountComm() {
  health.data = null;  // a visit starts empty: rows left over from an earlier visit would carry an old time
  health.error = "";
  drawComm();
  loadHealth();  // the first read is at once, then every 5 s while this page stays in front
}
function mappingText(point) {
  if (!point) return "—";
  return point.node_id ? `NodeId ${point.node_id}` : `设备 ${point.device_id} · ${point.register_kind === "input" ? "输入" : "保持"}寄存器 ${point.address} · ${point.data_type}`;
}
function commSources(data) {
  const sources = Object.entries(data.status?.sources || {});
  return table(["数据源", "类型", "数据状态", "通信", "最后有效通信", "代次", "重新同步次数", "连续建连失败次数"],
    sources.map(([id, s]) => [esc(id), esc(KINDS[s.kind] || s.kind), esc(SOURCE_STATES[s.state] || s.state),
      s.alarm ? badge("通信告警", "bad") : badge("正常", "ok"), stampAt(s.last_comm_wall), s.conn_gen || "尚无",
      `${s.resync_count}${s.persistent_resync ? " " + badge("持续失步", "bad") : ""}`, String(s.connect_failures)]))
    + `<p class="chart-caption">${esc(COUNTER_NOTE)}</p>`;
}
function commInputs(rows) {
  const points = new Map((state.config.points || []).map(p => [p.id, p]));
  return table(["通道", "数据源", "映射", "值", "质量", "可用性", "后端接收时刻", "源时间戳"], rows.map(row => {
    const point = points.get(row.point_id), seen = row.observation_id != null;
    const value = row.value == null ? "—" : `${fmt(row.value, 4)} ${esc(unitText(point?.unit))}`;
    return [`${esc(label(row.point_id))}<br><small>${esc(row.point_id)}</small>`, esc(row.source_id), esc(mappingText(point)), value,
      seen ? esc(row.quality) + stateCode(row.status_code) : "—",
      row.valid ? badge("有效", "ok") : badge(reasonText(row.reason), "bad"),
      seen ? stampAt(row.received_time) : "尚无观测",
      !seen ? "—" : row.source_time == null ? "未知" : stampAt(row.source_time)];
  }));
}
function commLog(log) {
  const meta = [`当前保留 <b>${log.entries.length}</b> / ${log.limit} 条（按首次出现顺序，聚合更新不改变位置）`,
    `已淘汰 <b>${log.evicted}</b> 条${log.oldest_time == null ? "" : `，现存最早记录 ${stampAt(log.oldest_time)}`}`,
    `诊断丢失 <b>${log.diag_lost}</b> 次（诊断事实未能进入日志；不是数据丢失，也不触发重新同步）`];
  const incomplete = log.incomplete ? " " + badge("日志不完整", "warn") : "";
  const body = log.entries.length
    ? table(["序号", "发现时刻", "数据源", "事件", "原因 / 详情", "代次", "次数", "首次失败", "最近失败", "状态", "最后有效通信"], [...log.entries].reverse().map(commEntry))
    : '<p class="empty">暂无记录。没有已记录的事件，不表示从未发生通信故障：这里只有本次运行的内存记录。</p>';
  return {meta: `<p>${meta.join(" · ")}${incomplete}</p>`, body};
}
function commEntry(e) {
  const detail = [e.scope ? `请求 ${esc(e.scope)}` : "", e.detail ? esc(e.detail) + (e.detail_truncated ? "（已截断）" : "") : e.detail_truncated ? "详情已省略（过长）" : ""].filter(Boolean);
  const when = e.state === "ACTIVE" ? badge("进行中")
    : e.state === "ENDED" ? `${badge("已结束")} ${stampAt(e.ended_time)} · ${esc(commEnd(e.end_reason))}` : "—";
  const last = e.last_comm_time == null ? "此前从未有过有效通信"
    : `${stampAt(e.last_comm_time)}，发现时已隔 ${fmt(e.silent_seconds, 1)} 秒`;
  return [e.seq, stampAt(e.found_time), esc(e.source_id), esc(commEvent(e)), `<code>${esc(e.reason)}</code>${detail.map(d => `<br>${d}`).join("")}`,
    e.kind === "ATTEMPT" ? "跨尝试，不绑定单一代次" : e.conn_gen ?? "尚无代次", e.count,
    e.state ? stampAt(e.first_failure) : "—", e.state ? stampAt(e.last_failure) : "—", when, last];
}
function drawComm() {
  if (state.page !== 13 || !$("comm-sources")) return;
  const comm = health.data?.communication;
  if (!comm) {
    const text = health.error ? `<p class="log-error">读取失败：${esc(health.error)}</p>` : '<p class="muted">正在读取…</p>';
    for (const id of ["comm-sources", "comm-inputs", "comm-log"]) $(id).innerHTML = text;
    $("comm-taken").textContent = "";
    $("comm-log-meta").innerHTML = "";
    return;
  }
  $("comm-taken").innerHTML = `状态获取时刻：<b>${stampAt(comm.taken_time)}</b>（取这一刻的快照；每行的“后端接收时刻”才是观测时刻）${health.error ? ` · <span class="log-error">最近一次刷新失败：${esc(health.error)}，以下仍是上次读到的内容</span>` : ""}`;
  $("comm-sources").innerHTML = commSources(health.data);
  $("comm-inputs").innerHTML = commInputs(comm.inputs);
  const log = commLog(comm.log);
  $("comm-log-meta").innerHTML = log.meta;
  $("comm-log").innerHTML = log.body;
}

// ------------------------------------------------------------------------- page 14: roles and accounts

const people = {list: null, audit: null, auditError: "", created: "", error: "", confirming: null};
function mountPeople() {
  people.created = "";  // a new credential is shown once, and not again when the page is entered later
  loadPeople();
}
async function loadPeople() {
  if (state.me.role !== "admin") { drawPeople(); return; }
  try {
    people.list = await api("/api/admin/accounts");
  } catch (error) { people.error = error.message; }
  try { people.audit = (await api("/api/admin/audit")).items; people.auditError = ""; } catch (error) { people.audit = null; people.auditError = error.message; }
  drawPeople();
}
function permissionsHtml() {
  return `<div class="grid two"><div class="card"><h3>权限</h3><p id="whoami"></p>${table(["操作", "只读用户", "管理员"], [
      ["查看实时值、历史回溯、故障记录、系统自检；导出当前显示的 CSV", "允许", "允许"],
      ["创建、停用账户；查看管理操作记录", "不允许", "允许"],
      ["在线增加、删除或修改通道、阈值与配置", "不允许", "不允许（改配置文件后由部署人员重启）"],
    ])}<p class="chart-caption">显示的是实际账户状态；不推算在线用户数量。配置不会在运行中热切换。</p></div>
    <div class="card"><h3>账户管理</h3><div id="account-area"></div></div></div>
    <div class="card"><h3>管理操作记录</h3><div id="audit-log" class="scroll"></div></div>`;
}
function drawPeople() {
  if (!$("account-area")) return;
  $("whoami").textContent = `当前身份：${state.me.username}（${ROLES[state.me.role] || state.me.role}）`;
  if (state.me.role !== "admin") {
    $("account-area").innerHTML = '<p class="muted">仅管理员可管理账户。</p>';
    $("audit-log").innerHTML = '<p class="muted">仅管理员可查看管理操作记录。</p>';
    return;
  }
  const rows = (people.list || []).map(a => [esc(a.username), esc(ROLES[a.role] || a.role), a.active ? "有效" : "已停用",
    !a.active ? "—" : people.confirming === a.username ? `<button data-revoke="${esc(a.username)}" class="primary">确认停用 ${esc(a.username)}</button>` : `<button data-revoke="${esc(a.username)}">停用</button>`]);
  $("account-area").innerHTML = `${people.created ? `<div class="credential-result" id="new-credential">仅此一次显示，请保存新凭据：${esc(people.created)}</div>` : ""}${people.error ? `<p class="log-error">${esc(people.error)}</p>` : ""}
    <form id="account-form" class="controls"><input name="username" aria-label="新用户名" placeholder="新用户名" required maxlength="64" pattern="[A-Za-z0-9_\\-]+"><select name="role" aria-label="账户角色"><option value="viewer">只读用户</option><option value="admin">管理员</option></select><button type="submit">创建账户</button></form>${table(["账户", "角色", "状态", "操作"], rows)}`;
  $("account-form").onsubmit = async event => {
    event.preventDefault();
    people.error = "";
    try {
      people.created = (await api("/api/admin/accounts", {method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.target)))})).credential;
    } catch (error) { people.created = ""; people.error = error.message; }
    loadPeople();
  };
  $("account-area").querySelectorAll("[data-revoke]").forEach(b => b.onclick = async () => {
    if (people.confirming !== b.dataset.revoke) { people.confirming = b.dataset.revoke; drawPeople(); return; }  // a second click confirms
    people.confirming = null;
    people.error = "";
    try { await api(`/api/admin/accounts/${encodeURIComponent(b.dataset.revoke)}`, {method: "DELETE"}); } catch (error) { people.error = error.message; }
    loadPeople();
  });
  $("audit-log").innerHTML = people.audit ? table(["时间", "操作者", "操作", "对象", "结果"], people.audit.map(a => [stamp(a.time), esc(a.actor), esc(a.action), esc(a.target), esc(a.result)])) : `<p class="muted">管理操作记录不可用：${esc(people.auditError || "正在读取…")}</p>`;
}

// ---------------------------------------------------------------------------------- the realtime socket

function setConnection(text, kind) {
  $("connection").textContent = text;
  $("connection").className = `badge ${kind}`;
}
function connect() {
  if (state.stopped) return;
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/stream`);
  state.ws = ws;
  ws.onopen = () => ws.send(JSON.stringify({credential: state.credential}));
  ws.onmessage = event => receive(ws, event.data);
  ws.onclose = event => lost(ws, event.code);
  ws.onerror = () => ws.close();
}
function receive(ws, text) {
  if (ws !== state.ws) return;
  let result, message;
  try {
    message = JSON.parse(text);
    result = apply(state.live, message);
  } catch (error) {
    if (!(error instanceof Rejected) && !(error instanceof SyntaxError)) throw error;
    // nothing was applied: drop the connection and take a new snapshot rather than show half of a message
    state.rejected = true;
    notice(`实时消息未通过校验（${error.message}），已整条丢弃并重新同步。`);
    ws.close();
    return;
  }
  state.lastMessage = Date.now();
  if (Number.isFinite(message.wall)) state.serverWall = message.wall;  // the server's clock: the trend never mixes in this PC's
  if (result.snapshot) trendNote(state.live.channels, state.serverWall);
  for (const group of result.groups) trendNote(group.channels, group.wall);
  if (state.stalled) { state.stalled = false; notice(""); }
  else if (state.rejected && result.snapshot) { state.rejected = false; notice(""); }
  setConnection("实时连接", "ok");
  invalidate();
}
function lost(ws, code) {
  if (ws !== state.ws || state.stopped) return;
  state.ws = null;
  trendBreak(state.serverWall);
  state.live = newLive();  // a snapshot replaces everything on reconnect; nothing partial is kept meanwhile
  if (code === 1008) { logout("实时通道认证失败，请重新登录。"); return; }
  setConnection("重连中", "warn");
  renderLive();
  state.reconnect = setTimeout(connect, 1000);
}
// Heartbeats come every second, so three seconds of nothing means the messages themselves have stopped.
setInterval(() => {
  const limit = (state.config?.timing?.ui_stall_notice ?? 3) * 1000;
  if (!state.stopped && state.live.channels && Date.now() - state.lastMessage > limit) {
    state.stalled = true;
    setConnection("消息中断", "bad");
    notice(`已超过 ${Math.round(limit / 1000)} 秒没有收到后端消息，显示的数值可能已过时；这不代表数据源状态。`);
  }
}, 500);

// -------------------------------------------------------------------------------------- login / logout

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {Authorization: `Bearer ${state.credential}`, "Content-Type": "application/json", ...options.headers}});
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { detail = response.statusText; }
    if (response.status === 401) logout("凭据无效或账户已停用，请重新登录。");
    throw new Error(`${response.status} · ${typeof detail === "string" ? detail : "请求未通过校验"}`);
  }
  return response.json();
}
async function login() {
  try {
    state.me = await api("/api/me");
    state.config = await api("/api/ui-config");
    state.stopped = false;
    sessionStorage.setItem("htsg.credential", state.credential);
    $("project-title").textContent = state.config.project?.title || "HTSG 科研监测";
    document.title = $("project-title").textContent;
    $("identity").textContent = `${state.me.username} · ${state.me.role === "admin" ? "管理员" : "只读用户"}`;
    $("login").hidden = true;
    $("workspace").hidden = false;
    $("logout").hidden = false;
    $("credential").value = "";
    notice("");
    renderPage();
    connect();
  } catch (error) {
    $("login-error").textContent = `连接失败：${error.message}`;
  }
}
function logout(message = "") {
  state.stopped = true;
  clearTimeout(state.reconnect);
  if (state.ws) { const ws = state.ws; state.ws = null; ws.close(); }
  state.credential = "";
  trend.series.clear();
  state.live = newLive();
  sessionStorage.removeItem("htsg.credential");
  clearReplay();
  $("login").hidden = false;
  $("workspace").hidden = true;
  $("workspace").innerHTML = "";
  $("navigation").innerHTML = "";
  $("logout").hidden = true;
  $("identity").textContent = "未登录";
  setConnection("未连接", "warn");
  $("stream-time").textContent = "等待后端数据";
  $("archive-banner").hidden = true;
  health.data = null;
  Object.assign(people, {list: null, audit: null, created: "", error: "", confirming: null});
  notice("");
  $("login-error").textContent = message;
}
$("login-form").onsubmit = async event => {
  event.preventDefault();
  state.credential = $("credential").value.trim();
  await login();
};
$("logout").onclick = () => logout();
loadReplay();
if (state.credential) login();
