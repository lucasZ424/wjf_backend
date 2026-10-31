"use strict";
import {newLive, Rejected, take} from "/static/stream.js";
import {buildCsv, channelRows, ENDS, gapMark, ORIGINS, qualityText, reasonText, retentionNotes, rowText} from "/static/replay.js";

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const state = {
  credential: sessionStorage.getItem("htsg.credential") || "", me: null, config: null, page: 1,
  live: newLive(), stopped: true, stalled: false, drawing: false, drawFailed: false, configChanged: false,
};
const PAGE_KEY = "htsg.page";

// ------------------------------------------------------------------------------------------ helpers

// A missing or invalid number is "—", never 0: an empty cell must not read as a measurement.
function fmt(value, digits = state.config?.ui?.precision ?? 2) {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", {maximumFractionDigits: digits}) : "—";
}
// 终版 §17.6: a reading keeps the decimals of its unit, trailing zeros included and no thousands separator; a unit not in
// the table takes ui.precision.  Display only: the API, the archive and the CSV keep the values as they are.
const DIGITS = {"mV/V": 4, "με": 0, degC: 1, "℃": 1, MW: 1, "t/h": 0, MPa: 2, kPa: 3};
function reading(value, unit) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const digits = DIGITS[unit] ?? state.config?.ui?.precision ?? 2;
  return value.toLocaleString("zh-CN", {minimumFractionDigits: digits, maximumFractionDigits: digits, useGrouping: false});
}
function clock(seconds) {
  return Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleTimeString("zh-CN", {hour12: false}) : "—";
}
// A notice of the moment wins; without one, what still holds is said: a reading that could not be drawn, or a backend
// that restarted with another configuration (修复轮13).
const DRAW_FAILED = "页面显示出错：最新数据没能画出来，页面上的数值可能不是最新的。数据仍在读取，恢复后自动更新；一直不恢复请刷新页面。";
const CONFIG_CHANGED = "后端配置已更改：请刷新页面载入新配置。在此之前，页面上的通道、名称与限值仍是旧配置的。";
function notice(message) {
  const text = message || (state.drawFailed ? DRAW_FAILED : state.configChanged ? CONFIG_CHANGED : "");
  $("notice").textContent = text;
  $("notice").hidden = !text;
}
// 修复轮13: what did not change is not written again - a table being read, hovered or selected stays as it is.
const written = new WeakMap();
function setHtml(id, html) {
  const box = $(id);
  if (box && written.get(box) !== html) { box.innerHTML = html; written.set(box, html); }
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
  const unit = definition(id).unit;
  return view && view.valid ? `${reading(view.value, unit)} ${esc(unitText(unit))}` : "—";
}
function stateBadges(id) {
  const view = channel(id);
  if (!view) return badge("等待实时数据", "warn");
  let out = view.valid ? badge("有效", "ok") : badge(reasonText(view.reason), "bad");
  if (view.fault === "PENDING") out += " " + badge("越限待确认", "warn");
  if (view.fault === "CONFIRMED") out += " " + badge(`故障已确认${view.fault_direction === "HIGH" ? "（高）" : view.fault_direction === "LOW" ? "（低）" : ""}`, "bad");
  if (view.estimate) out += " " + badge("估算值", "warn");
  return out;
}

// ------------------------------------------------------------------------------------------- pages

const pages = [
  {id: 1, name: "系统总览", html: overviewHtml, update: updateOverview},
  {id: 2, name: "水冷壁应变场", html: strainHtml, update: updateStrain},
  {id: 4, name: "热应力分析", html: trendHtml, mount: mountTrend, update: updateTrend},
  {id: 5, name: "历史查询", html: historyHtml, mount: mountHistory},
  {id: 12, name: "系统自检", html: selfcheckHtml, mount: () => loadHealth(true), update: updateSelfcheck},
  {id: 13, name: "通信与日志", html: commHtml, mount: mountComm},  // no `update`: nothing here is redrawn from the live stream
  {id: 14, name: "系统管理与权限", html: permissionsHtml, mount: mountPeople},
];
const currentPage = () => pages.find(p => p.id === state.page) || pages[0];

function renderPage() {
  if (!state.config) return;
  const page = currentPage();
  try { sessionStorage.setItem(PAGE_KEY, String(page.id)); } catch { /* no storage: a reload starts at the overview */ }
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
  $("stream-time").textContent = live.channels ? `最新读取：${clock(live.taken)}` : "等待后端数据";
  $("stream-time").dataset.taken = live.taken ?? "";  // the exact time of the reading shown
}
// The page is drawn once per frame, from the state as it then is.
function invalidate() {
  if (state.drawing) return;
  state.drawing = true;
  requestAnimationFrame(() => { state.drawing = false; draw(); });
}
// A reading that cannot be drawn is said, never left looking current (修复轮13); the readings go on, and the first one
// drawn again clears it.
function draw() {
  try {
    renderLive();
  } catch (error) {
    console.error(error);
    if (!state.drawFailed) { state.drawFailed = true; setConnection("显示出错", "bad"); if (!state.stalled) notice(""); }
    return;
  }
  if (state.drawFailed) {
    state.drawFailed = false;
    if (!state.stalled) { setConnection("实时连接", "ok"); notice(""); }
  }
}

// -------------------------------------------------------------- pages 1 and 2: overview and strain (终版 §17.1-17.2)

// Three different quantities per station, never mixed: the bridge signal, the converted strain and the change.
const stations = () => state.config?.ui?.stations || [];
const bridgeOf = s => definition(s.strain).inputs?.[0];
const SOURCE_STATES = {WAITING: "等待首次数据", VALID: "数据有效", EXPIRED: "数据已失效", RESYNC: "重新同步中"};
const KINDS = {modbus: "Modbus", opcua: "OPC UA"};
const STRAIN_HEAD = ["测点", "原始信号（mV/V）", "换算应变（με，GF=6，未作温度补偿）", "应变变化（με，与上一轮之差）", "状态"];

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
const quantity = id => { const view = channel(id), unit = definition(id).unit; return view && view.valid ? `${reading(view.value, unit)} ${esc(unitText(unit))}` : "—"; };

function strainTable() {
  return table(STRAIN_HEAD, stations().map(s => [esc(s.label || s.id), quantity(bridgeOf(s)), quantity(s.strain), quantity(s.strain_change), stationBadge(stationState(s))]));
}
const STRAIN_NOTE = '<p class="chart-caption">换算应变按 GF=6 换算，未作温度补偿，不是已验证的机械应变。原始信号、换算应变和应变变化是三个不同的量。</p>';

function overviewHtml() {
  return `<div id="source-status" class="grid metrics"></div>
    <div class="card" style="margin-bottom:16px"><h3>越限状态</h3><div id="fault-summary"></div></div>
    <div class="grid two"><div class="card"><h3>过程量（实时）</h3><div id="process-table"></div></div>
    <div class="card"><h3>应变测点（三种量分别显示）</h3><div id="strain-table"></div>${STRAIN_NOTE}</div></div>`;
}
function updateOverview() {
  const sources = state.live.status?.sources || {};
  setHtml("source-status", Object.entries(sources).map(([id, s]) => `<div class="card metric"><small>${esc(id)}（${esc(KINDS[s.kind] || s.kind)}）</small><div class="value" style="font-size:16px">${esc(SOURCE_STATES[s.state] || s.state)}</div>${s.alarm ? badge("通信告警", "bad") : badge("通信正常", "ok")} ${s.enabled ? "" : badge("已禁用")}<br><small>最后通信 ${clock(s.last_comm_wall)}</small></div>`).join("") || '<p class="muted">等待实时数据</p>');
  const views = Object.values(state.live.channels || {});
  const pending = views.filter(v => v.fault === "PENDING").length, confirmed = views.filter(v => v.fault === "CONFIRMED").length;
  setHtml("fault-summary", state.live.channels
    ? `${badge(`越限待确认 ${pending} 个通道`, pending ? "warn" : "")} ${badge(`故障已确认 ${confirmed} 个通道`, confirmed ? "bad" : "")} <span class="muted">持续越限满 5 秒即确认。</span>`
    : '<span class="muted">等待实时数据</span>');
  setHtml("process-table", table(["过程量", "数值", "状态"], (state.config.ui.process_points || []).map(id => [esc(label(id)), valueText(id), stateBadges(id)])));
  setHtml("strain-table", strainTable());
}

function strainHtml() {
  return `<div class="grid two"><div class="card"><h3>测点分布示意（${stations().length} 个固定测点）</h3><div id="strain-map" class="map strain-map"></div>
    <div class="legend"><b>● 正常</b><span style="color:var(--amber)">● 越限待确认</span><em>● 故障已确认</em><span>● 无效（灰色，注明原因）</span></div>
    <p class="chart-caption">每个测点一个色块，颜色表示该点是否有效、是否越限；不在测点之间插值，位置只是示意，不代表现场布置。</p></div>
    <div class="card"><h3>三种量分别显示</h3><div id="strain-table"></div>${STRAIN_NOTE}</div></div>`;
}
function updateStrain() {
  setHtml("strain-map", stations().map(s => {
    const st = stationState(s), x = Math.max(24, Math.min(76, Number(s.x) || 0)), y = Math.max(10, Math.min(90, Number(s.y) || 0));
    return `<div class="block" data-station="${esc(s.id)}" data-state="${st.state}" style="left:${x}%;top:${y}%"><b>${esc(s.label || s.id)}</b>
      <div>换算应变 <strong>${quantity(s.strain)}</strong></div>
      <div class="minor">原始信号 ${quantity(bridgeOf(s))}</div>
      <div class="minor">应变变化 ${quantity(s.strain_change)}</div>
      <div class="why">${esc(st.why)}</div></div>`;
  }).join(""));
  setHtml("strain-table", strainTable());
}

// ----------------------------------------------------------------- page 4: live three-panel trend (终版 §17.3)

// Only what this page reads is plotted: the readings since it signed in.  Custom history lives on page 5.
const TREND_SECONDS = 300, KEEP_SECONDS = 600;
const trend = {series: new Map(), pair: 0, cursor: null};
const signed = (value, unit) => typeof value === "number" && Number.isFinite(value) ? (value > 0 ? "+" : "") + reading(value, unit) : "—";
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
// The live trend observes the current state (终版 §13.3, 修复轮05): a point is what a reading showed, at the time the
// reading was taken.  A stretch ends where two readings are more than the notice apart or the run changed; a reading
// time that went back starts the trend over, so the points stay in time order.
function trendReading(reading, before) {
  if (before.taken !== null && reading.taken_time < before.taken) trend.series.clear();
  else if (before.taken !== null && (reading.run_id !== before.run || reading.taken_time - before.taken > state.config.timing.ui_stall_notice)) trendBreak(before.taken);
  trendNote(reading.channels, reading.taken_time);
}
const valueAt = (list, t) => { let found = null; for (const p of list) { if (p.t <= t) found = p; else break; } return found; };

function trendHtml() {
  return `<div class="controls"><label>温度对<select id="pair-select">${pairs().map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join("")}</select></label><span id="pair-inputs" class="muted"></span><span class="badge warn">热应力为估算值，不是实测</span></div>
    <div class="card"><h3>负荷、内外壁温差与热应力（同一时间轴，最近 ${TREND_SECONDS / 60} 分钟）</h3>
      <div id="trend-latest" class="controls"></div>
      <canvas id="trend-canvas" class="chart" style="height:560px" aria-label="三分栏实时趋势"></canvas>
      <div id="trend-readout" class="chart-caption">把鼠标移到图上查看同一时刻三栏各自的值。</div>
      <p class="chart-caption">三栏共用一条时间轴，只为对照观察，不表示同时采样。热应力是按温差与约束假设算出的估算值，不是实测。数据无效或中断处曲线断开；页面每秒读取一次，一秒内的短暂变化可能看不到，过去的数据请到“历史查询”页查看。</p></div>`;
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
    invalidate();  // 修复轮13: drawn once a frame, however many moves came in it
  };
  canvas.onmouseleave = () => { trend.cursor = null; invalidate(); };
}
function trendPanels() {
  const p = pairs()[trend.pair], ids = [loadId(), p.delta, p.stress];
  // the difference and the stress can be either sign and say so; the load is a plain quantity
  return ids.map((id, i) => {
    const raw = definition(id).unit;
    return {id, name: i === 0 ? "负荷" : i === 1 ? "内外壁温差" : "热应力（估算）", unit: unitText(raw), points: trend.series.get(id) || [], show: v => i === 0 ? reading(v, raw) : signed(v, raw)};
  });
}
function updateTrend() {
  const canvas = $("trend-canvas");
  if (!canvas || !pairs().length) return;
  const p = pairs()[trend.pair], panels = trendPanels(), now = state.serverWall || Date.now() / 1000;
  $("pair-inputs").textContent = `内壁 ${p.inner}，外壁 ${p.outer}（温差与热应力都由这一对算出）`;
  setHtml("trend-latest", panels.map(x => { const view = channel(x.id); return `<span data-panel="${esc(x.id)}">${esc(x.name)}：<b>${view && view.valid ? `${x.show(view.value)} ${esc(x.unit)}` : "—"}</b></span> ${stateBadges(x.id)}`; }).join(" "));
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
    c.fillText(`${panel.name}（${panel.unit}）`, left + 4, top + 11);
    for (let k = 0; k <= 2; k++) {
      const yy = top + height * k / 2;
      c.strokeStyle = "#304968"; c.beginPath(); c.moveTo(left, yy); c.lineTo(right, yy); c.stroke();
      c.fillStyle = "#91aac5"; c.fillText(panel.show(hi - (hi - lo) * k / 2), 2, yy + 4);
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
  canvas.plot.ticks = drawTicks(c, x, t0, now, {left, right, axis: h - 34, text: h - 8});
}
// The time axis, marked on whole local seconds, minutes, hours or days (修复轮13); a window longer than a day says the dates.
const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 2592000];
const two = n => String(n).padStart(2, "0");
function timeTicks(start, end, most = 8) {
  const span = end - start, step = STEPS.find(s => span / s <= most) || STEPS[STEPS.length - 1];
  const shift = new Date(start * 1000).getTimezoneOffset() * 60, ticks = [];
  for (let t = Math.ceil((start - shift) / step) * step + shift; t <= end; t += step) {
    const d = new Date(t * 1000), hm = `${two(d.getHours())}:${two(d.getMinutes())}`;
    ticks.push({t, label: span > 86400 ? `${two(d.getMonth() + 1)}-${two(d.getDate())} ${hm}` : step < 60 ? `${hm}:${two(d.getSeconds())}` : hm});
  }
  return ticks;
}
function drawTicks(c, x, start, end, at) {
  const ticks = timeTicks(start, end);
  c.strokeStyle = "#304968"; c.fillStyle = "#91aac5"; c.lineWidth = 1;
  for (const tick of ticks) {
    const xx = x(tick.t), width = c.measureText(tick.label).width;
    c.beginPath(); c.moveTo(xx, at.axis); c.lineTo(xx, at.axis + 4); c.stroke();
    c.fillText(tick.label, Math.min(Math.max(xx - width / 2, at.left), at.right - width), at.text);
  }
  return ticks;
}

// ------------------------------------------------------------------------------- page 5: history (终版 §14)

// The replay settings.  A locked fault only fills the centre time and the default channel; every field stays
// editable, and a query never needs a fault.
const replay = {fault: null, center: null, seconds: null, points: [], chart: null};  // the default window comes with ui-config
const faults = {items: [], cursor: null, more: false, loading: false, epoch: 0, partial: false, error: "", query: ""};

const stamp = seconds => Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString("zh-CN", {hour12: false}) : "—";
const toLocalInput = seconds => Number.isFinite(seconds) ? new Date(seconds * 1000 - new Date(seconds * 1000).getTimezoneOffset() * 60000).toISOString().slice(0, 19) : "";
const fromLocalInput = text => text ? new Date(text).getTime() / 1000 : null;
const faultChannels = () => definitions().filter(d => d.high != null || d.low != null);
const option = (id, selected = false) => `<option value="${esc(id)}" ${selected ? "selected" : ""}>${esc(label(id))}（${esc(id)}${definition(id).unit ? `，${esc(unitText(definition(id).unit))}` : ""}）</option>`;
// A window length as people say it (修复轮13): 5400 s is 1.5 hours.
function durationText(seconds) {
  if (!(seconds > 0)) return "";
  for (const [size, name] of [[86400, "天"], [3600, "小时"], [60, "分钟"]]) if (seconds >= size) return `${+(seconds / size).toFixed(1)} ${name}`;
  return `${seconds} 秒`;
}

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
          <label>窗口长度（秒）<input id="replay-seconds" type="number" min="1" max="31622400" step="1"></label><span id="replay-span" class="muted"></span>
        </div>
        <div class="controls"><label>查看通道<input id="replay-filter" placeholder="检索名称或编号" autocomplete="off"></label></div>
        <select id="replay-points" multiple size="7" aria-label="查看通道"></select>
        <p id="replay-limit" class="muted"></p>
        <div class="controls"><button id="replay-run" class="primary">查询</button><button id="replay-clear">清空设置</button></div>
        <p class="chart-caption">以中心时间为准，前后各取半个窗口。在左侧选一条故障会自动填好时间、窗口和相关通道，也可以手动填写。</p>
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
    // a chosen channel stays in view (修复轮13): hidden, the next plain click dropped it without a word
    for (const o of select.options) o.hidden = !!text && !o.selected && !o.textContent.toLowerCase().includes(text) && !(keepAll && o.value === "");
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
  $("replay-seconds").oninput = drawSpan;
  $("replay-seconds").onchange = event => {
    if (!event.target.checkValidity()) { event.target.reportValidity(); return; }
    replay.seconds = Number(event.target.value);
    saveReplay();
  };
  $("replay-points").onchange = event => {
    const chosen = [...event.target.selectedOptions].map(o => o.value), most = state.config.replay.max_channels;
    if (chosen.length > most) {  // the backend refuses more (修复轮03): the choice stays as it was
      for (const o of event.target.options) o.selected = replay.points.includes(o.value);
      drawLimit(`一次最多查看 ${most} 个通道：这次选了 ${chosen.length} 个，未采用。`);
      return;
    }
    replay.points = chosen;
    saveReplay();
    drawLimit();
  };
  $("replay-run").onclick = runReplay;
  $("replay-clear").onclick = () => { clearReplay(); syncSettings(); drawFaults(); drawReplay(); };
  $("replay-lock").onclick = event => { if (event.target.id === "unlock") { replay.fault = null; saveReplay(); drawLock(); drawFaults(); } };
  if (!replay.seconds) replay.seconds = state.config.replay.default_seconds;
  syncSettings();
  drawLimit();
  loadFaults(true);
  drawReplay();
  if (replayReady()) runReplay();  // entering the page asks the database again with the kept settings
}
function syncSettings() {
  $("replay-center").value = toLocalInput(replay.center);
  $("replay-seconds").value = replay.seconds;
  for (const o of $("replay-points").options) o.selected = replay.points.includes(o.value);
  drawLock();
  drawSpan();
}
function drawSpan() {
  $("replay-span").textContent = durationText(Number($("replay-seconds").value));
}
function drawLimit(warning = "") {
  $("replay-limit").textContent = warning || `一次最多查看 ${state.config.replay.max_channels} 个通道。`;
  $("replay-limit").className = warning ? "log-error" : "muted";
}
function drawLock() {
  const f = replay.fault;
  $("replay-lock").innerHTML = f
    ? `参考故障：${esc(label(f.point_id))}，确认于 ${stamp(f.confirmed_wall)}，方向${f.direction === "HIGH" ? "高" : "低"}${replay.center !== f.confirmed_wall ? "（中心时间已手动修改）" : ""} <button id="unlock">解除锁定</button>`
    : "未锁定故障：请手动填写，或在左侧选择一条故障。";
}
function selectFault(id) {
  const f = faults.items.find(x => x.id === id);
  if (!f) return;
  replay.fault = f;
  replay.center = f.confirmed_wall;
  replay.seconds = state.config.replay.default_seconds;  // 修复轮03: a fault brings the default window and its fixed template
  replay.points = [...(state.config.replay.templates[f.point_id] || [f.point_id])];
  replay.chart = f.point_id;
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
  $("fault-list").innerHTML = faults.items.map(f => `<button class="fault-row ${replay.fault?.id === f.id ? "selected" : ""}" data-fault="${esc(f.id)}"><b>${esc(label(f.point_id))}</b> ${badge(f.direction === "HIGH" ? "高" : "低", "bad")}<br><small>${stamp(f.confirmed_wall)}，确认值 ${reading(f.basis_value, definition(f.point_id).unit)} ${esc(unitText(definition(f.point_id).unit))}</small></button>`).join("")
    || `<p class="empty">${faults.loading ? "正在读取…" : "没有符合条件的故障记录"}</p>`;
  $("fault-count").textContent = faults.error ? `读取失败：${faults.error}` : `已显示 ${faults.items.length} 条${faults.partial ? "；部分数据库不可读，列表不完整" : ""}`;
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
      Object.assign(replay, {fault: saved.fault ?? null, center: saved.center, seconds: Number(saved.seconds) || null, points: saved.points.filter(x => typeof x === "string"), chart: saved.chart ?? null});
    }
  } catch { /* unreadable settings are simply not used */ }
}
function saveReplay() {
  try { sessionStorage.setItem(REPLAY_KEY, JSON.stringify({fault: replay.fault, center: replay.center, seconds: replay.seconds, points: replay.points, chart: replay.chart})); } catch { /* no storage: settings last this page only */ }
}
function clearReplay() {
  sessionStorage.removeItem(REPLAY_KEY);
  Object.assign(replay, {fault: null, center: null, seconds: state.config?.replay.default_seconds ?? null, points: [], chart: null});
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
  if (held.length) return held.map(r => `${r.valid ? `${reading(r.value, unit)} ${esc(unitText(unit))}`.trim() : "无效"}（原观测 ${stamp(r.observed_at)}，${ORIGINS[r.origin] || r.origin}）`).join("；");
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
  for (const note of retentionNotes(result, stamp)) notes.push(`${esc(note)}。`);
  for (const gap of result.gaps) notes.push(`归档缺口（${esc(gap.source_id)}）：${stamp(gap.start_wall)} → ${Number.isFinite(gap.end_wall) ? stamp(gap.end_wall) : "未恢复"}，原因：${gapReason(gap.reason)}；缺口内的数值未知，不连线。`);
  const summary = request.points.map(id => {
    const channel = result.channels[id], rows = channelRows(channel), last = lastOf(rows);
    const warn = rows.some(r => !r.valid);
    return [`${esc(label(id))}<br><small>${esc(id)}</small>`, esc(unitText(channel.unit)), startBasis(rows, result, channel.unit), `${rows.filter(r => !r.held).length}${channel.truncated ? " + 截断" : ""}`,
      last ? (last.valid ? reading(last.value, channel.unit) : `— ${badge(rowText(last), "bad")}`) : "—", warn ? badge("含无效或极值记录", "bad") : badge("全部有效", "ok")];
  });
  const focus = request.chart, focusData = result.channels[focus], rows = channelRows(focusData);
  box.innerHTML = `<p id="replay-meta">查询时刻 ${stamp(result.query_time)}；窗口 ${stamp(result.start)} → ${stamp(result.end)}（中心 ${stamp(result.center)}，时长 ${durationText(result.seconds)}）；${coverage}</p>
    ${notes.map(n => `<div class="note" role="alert">${n}</div>`).join("")}
    <div id="replay-summary">${table(["通道", "单位", "窗口起点依据", "窗口内记录", "窗口内最后一条", "状态"], summary)}</div>
    <div class="controls"><label>图表与记录通道<select id="replay-focus">${request.points.map(id => option(id, id === focus)).join("")}</select></label><button id="replay-export">导出 CSV</button></div>
    <canvas id="replay-chart" class="chart" aria-label="回溯图表"></canvas>
    <p class="chart-caption">阶梯线是每个值的生效期；虚线是窗口起点时仍有效的值；红色是无效或超出极值；阴影是归档缺口，竖虚线是有效段起点。曲线不插值、不补点。</p>
    <div id="replay-records" class="scroll">${recordsTable(rows, focusData, result)}</div>`;
  $("replay-focus").onchange = event => { replay.chart = event.target.value; runReplay(); };
  $("replay-export").onclick = exportCsv;
  plotReplay($("replay-chart"), focusData, result);
}

// Why the archive lost a stretch, in words (终版 §17.5), the code beside it.
const GAP_REASONS = {QUEUE_FULL: "写入队列已满", PENDING_OVERFLOW: "待写数据超出上限", CLOCK_JUMP: "时钟跳变"};
function gapReason(code) {
  const text = String(code ?? ""), name = GAP_REASONS[text] || (text.startsWith("REFUSED:") ? "写入被拒" : "");
  return `${name ? `${name}（<code>${esc(text)}</code>）` : `<code>${esc(text)}</code>`}`;
}
function recordsTable(rows, channel, result) {
  const head = ["生效时间", "原观测时间", "批次与序号", "值", "质量或状态", "来源", "种类", "有效段", "说明", "缺口标记"];
  if (!rows.length) return table(head, []);
  return `<div class="table-wide"><table><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r =>
    `<tr class="${r.valid ? "" : "bad"} ${r.held ? "held" : ""}"><td>${stampMs(r.time)}</td><td>${stampMs(r.observed_at)}</td><td>${r.batch_seq}${r.item_seq != null ? `-${r.item_seq}` : ""}</td><td>${r.valid ? reading(r.value, channel.unit) : "—"}</td><td>${esc(rowText(r))}</td><td>${ORIGINS[r.origin] || esc(r.origin)}</td><td>${r.kind === "raw" ? "原始" : "派生"}</td><td>${esc(r.run)}/${r.seg}</td><td>${r.held ? "窗口起点的保持值；" : ""}${r.until == null ? "其后未知" : `${ENDS[r.ended_by] || ""} ${stamp(r.until)}`}</td><td>${gapMark(r, channel, result)}</td></tr>`).join("")}</tbody></table></div>`;
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
  c.fillText(unitText(channel.unit), 4, 12);
  for (const gap of result.gaps) {  // unknown time: shaded, nothing drawn through it
    const a = x(gap.start_wall), b = Number.isFinite(gap.end_wall) ? x(gap.end_wall) : right;
    c.fillStyle = "#ffbd6633";
    c.fillRect(a, top, Math.max(2, b - a), bottom - top);
  }
  c.strokeStyle = "#55708f";
  c.setLineDash([2, 4]);
  for (const s of channel.segments) if (Number.isFinite(s.start_wall) && s.start_wall > start && s.start_wall < end) { c.beginPath(); c.moveTo(x(s.start_wall), top); c.lineTo(x(s.start_wall), bottom); c.stroke(); }
  c.setLineDash([]);
  canvas.plot.ticks = drawTicks(c, x, start, end, {left, right, axis: bottom, text: h - 8});
  c.fillStyle = "#91aac5";
  if (!good.length) { c.fillText("窗口内没有有效数值可画", left + 12, (top + bottom) / 2); }
  let lo = Math.min(...good.map(r => r.value)), hi = Math.max(...good.map(r => r.value));
  if (good.length) {
    const pad = (hi - lo) * 0.15 || Math.max(1, Math.abs(lo) * 0.01);
    lo -= pad; hi += pad;
    for (let i = 0; i <= 4; i++) {
      const yy = top + (bottom - top) * i / 4;
      c.strokeStyle = "#304968"; c.beginPath(); c.moveTo(left, yy); c.lineTo(right, yy); c.stroke();
      c.fillStyle = "#91aac5"; c.fillText(reading(hi - (hi - lo) * i / 4, channel.unit), 2, yy + 4);
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

// The chart follows the page width (修复轮13): drawn again from the result on screen, never asked again.
let refit = false;
window.addEventListener("resize", () => {
  if (refit) return;
  refit = true;
  requestAnimationFrame(() => {
    refit = false;
    if (state.page === 5 && view.result && $("replay-chart")) plotReplay($("replay-chart"), view.result.channels[view.request.chart], view.result);
  });
});

// ------------------------------------------- pages 12, 14 and the archive banner (终版 §11, §12, §15)

const ARCHIVE_STATES = {OK: ["正常", "ok"], LOST: ["已中断，缺口尚未恢复", "bad"], RECOVERING: ["恢复中，等待确认写入", "warn"]};
// Where new groups go, as the backend says it (修复轮12/13; 终版 §13.5): never the routing mode, which reads PRIMARY while
// the normal database takes them for the time being, unchecked.
const TARGETS = {PRIMARY: ["正常库", "ok"], EMERGENCY: ["应急库", "warn"], PRIMARY_PROVISIONAL: ["正常库（临时，未核对）", "warn"], UNKNOWN: ["未知，新数据等待写入", "bad"]};
const ROLES = {admin: "管理员", viewer: "只读用户"};
const health = {data: null, error: "", seq: 0, controller: null};
const HEALTH_PAGES = [12, 13];  // the only pages that read /api/health, and only while they are in front

// What the page in front of the operator must say about the archive, whichever page that is.
function renderBanner() {
  const status = state.live.status, f = status?.faults, parts = [];
  if (state.live.target === "EMERGENCY") parts.push("正常库占用已到应急线，新数据正写入应急库。");  // 终版 §13.6 进入应急立即提示
  if (state.live.target === "UNKNOWN") parts.push("读不到写入路由状态，新数据正在等待写入，暂时没有进入任何数据库。");
  if (f?.archive_incomplete) {
    const lost = Object.entries(f.loss || {}).map(([id, l]) => `${id}：${l.count} 条，${stamp(l.first_wall)} → ${stamp(l.last_wall)}`).join("；");
    parts.push(`故障归档不完整：未写入的故障缓冲 ${f.buffered}/${f.limit}，已拒绝 ${f.rejected_total} 条新确认的故障${lost ? `（尚未写入摘要：${lost}）` : ""}。实时的“已确认”状态仍然正确，但历史故障列表缺少这些记录。`);
  }
  for (const [id, s] of Object.entries(status?.sources || {})) {
    if (s.archive !== "OK") parts.push(`历史归档${s.archive === "LOST" ? "中断" : "恢复中"}：${id}。该段历史可能缺失。实时数据是否有效，以页面当前状态为准。`);
  }
  const text = parts.join("　");
  if ($("archive-banner").textContent !== text) $("archive-banner").textContent = text;
  $("archive-banner").hidden = parts.length === 0;
}

// One reading at a time, given up at the page's limit (修复轮13, as the live reading): a tick that finds one outstanding is
// skipped; entering a page asks afresh and the older answer is dropped.
async function loadHealth(fresh = false) {
  if (health.controller && !fresh) return;
  health.controller?.abort();
  const controller = health.controller = new AbortController(), mine = ++health.seq, seconds = state.config.timing.ui_poll_timeout;
  const limit = setTimeout(() => controller.abort(), seconds * 1000);
  let data = null, error = "";
  try {
    data = await api("/api/health", {signal: controller.signal});
  } catch (failure) {
    error = failure.name === "AbortError" ? `超过 ${seconds} 秒没有答复` : failure.message;
  } finally {
    clearTimeout(limit);
    if (health.controller === controller) health.controller = null;
  }
  if (mine !== health.seq || state.stopped || !HEALTH_PAGES.includes(state.page)) return;  // an older answer, or the page was left
  health.data = data ?? health.data;
  health.error = error;
  if (state.page === 12) updateSelfcheck(); else drawComm();
}
setInterval(() => { if (!state.stopped && HEALTH_PAGES.includes(state.page)) loadHealth(); }, 5000);

// ------------------------------------------------------------- the two counters of a source, on two pages

// Both come from the same /api/health answer as everything else on the communication page, so the two pages agree.
const COUNTER_NOTE = "重新同步次数：通信连续正常 60 秒后清零，累计 5 次为持续失步。连续建连失败次数：建连成功后清零，订阅或读取失败不计。"
  + "两者与日志里的“次数”含义不同。每 5 秒刷新一次。";
function counterCells(id) {
  const s = health.data?.status?.sources?.[id];
  if (!s) return ["—", "—"];
  return [`${s.resync_count}${s.persistent_resync ? " " + badge("持续失步", "bad") : ""}`, String(s.connect_failures)];
}

// ----------------------------------------------------------------------------------- page 12: self-check

function selfcheckHtml() {
  return `<div class="grid two"><div class="card"><h3>数据源与通信</h3><div id="check-sources"></div></div><div class="card"><h3>OPC UA 订阅协商参数</h3><div id="check-negotiated"></div></div></div>
    <div class="grid two"><div class="card"><h3>历史归档</h3><div id="check-archive"></div></div><div class="card"><h3>故障记录归档</h3><div id="check-faults"></div></div></div>
    <div class="card"><h3>存储保留与滚动删除</h3><div id="check-retention"></div></div>
    <div class="card"><h3>运行时限（配置值）</h3><div id="check-timing"></div></div>
    <div class="note">本页不含传感器零漂、硬件温度等硬件诊断（没有这类输入），只显示软件的采集、通信协商与归档状态。</div>`;
}
// 终版 §13.6 自检与报警: what each database keeps, the deletion's real pace, the next one or why it cannot be foreseen, the alarm.
const ALARM_REASONS = {
  NO_ELIGIBLE_RANGE: "没有满足条件的删除范围", BOUNDARY_WITHOUT_BASELINE: "保留边界缺基线", POSTPONED_BY_LONG_TRANSACTION: "长事务推迟了删除",
  DELETION_FAILING: "删除连续失败", OTHER_FILES_GROWING: "疑似数据库之外的占用增长", NO_WRITABLE_BUCKET: "没有可写的桶",
  DELETION_SWITCHED_OFF: "滚动删除未启用", DISK_UNAVAILABLE: "主数据卷无法读取",
};
const FORECAST_REASONS = {
  INSUFFICIENT_DATA: "数据不足7天", NO_GROWTH: "近7天没有增长", EMERGENCY: "处在应急期间", DISK_GROWS_FASTER: "磁盘占用增长疑似超过数据库自身增长",
  DISK_UNAVAILABLE: "主数据卷无法读取",
};
function retentionHtml(archive) {
  const r = archive?.retention;
  if (!r) return '<p class="muted">正在读取…</p>';
  const parts = [];
  if (archive.target === "EMERGENCY") parts.push(`<p class="log-error" role="alert">已转写应急库：正常库占用达到应急线，新数据写入应急库。</p>`);
  if (r.alarm?.active) parts.push(`<p class="log-error" role="alert">主库超水位报警：占用持续高于删除启动线 ${fmt((r.alarm.above_seconds || 0) / 60, 0)} 分钟。原因：${(r.alarm.reasons || []).map(x => esc(ALARM_REASONS[x] || x)).join("、") || "未查明"}</p>`);
  const names = {primary: "正常库", emergency: "应急库"};
  // what the maintenance thread could not do, and a group the normal database keeps waiting for its bucket (修复轮10)
  const b = archive.buckets || {}, prep = {OK: "正常", UNAVAILABLE: "无法读取"}, numbering = String(b.numbering || "");
  const prepared = ["primary", "emergency"].filter(db => b[db]).map(db => `${names[db]} ${String(b[db]).startsWith("PREPARE_FAILED:") ? `准备失败（<code>${esc(String(b[db]).slice(15))}</code>）` : esc(prep[b[db]] || b[db])}`);
  const held = numbering.startsWith("PAUSED:") ? `；编号暂停（<code>${esc(numbering.slice(7))}</code>）` : numbering.startsWith("FILE_NOT_WRITTEN:") ? `；编号文件写不进去（<code>${esc(numbering.slice(17))}</code>）` : "";
  if (prepared.length) parts.push(`<p>桶准备：${prepared.join("，")}${held}${b.error ? `；维护出错（<code>${esc(b.error)}</code>）` : ""}</p>`);
  const w = archive.health?.waiting_for_bucket;
  if (w) parts.push(`<p class="log-error" role="alert">正常库缺第 ${esc(w.bucket)} 号桶：一组数据自 ${stamp(w.since)} 起在等待写入，其后的数据在队列里积压；补建为什么失败见“桶准备”。</p>`);
  parts.push(diskHtml(r.disk));
  parts.push(table(["数据库", "保留的桶", "保留自", "保留边界"], Object.entries(r.databases || {}).map(([db, k]) => [esc(names[db] || db),
    k.readable === false ? "无法读取" : k.kept_buckets, k.kept_from == null ? "—" : stamp(k.kept_from), k.boundary == null ? "—" : stamp(k.boundary)])));
  const f = r.forecast;
  const next = !f ? "—" : f.next_deletion == null ? `无法可靠预计（${esc(FORECAST_REASONS[f.reason] || f.reason)}）` : stamp(f.next_deletion);
  parts.push(`<p>最近一次删除：${r.last_deletion == null ? "尚未删除" : stamp(r.last_deletion)}；实测删除周期：${r.deletion_period_s == null ? "尚无" : `${fmt(r.deletion_period_s / 86400, 1)} 天`}；预计下次删除：${next}</p>`);
  return parts.join("");
}
// 终版 §15 磁盘占用 (修复轮12/13): each volume as the maintenance thread last read it, and the lines it is judged by.
const GIB = 2 ** 30;
function diskHtml(d) {
  if (!d) return "";
  const gb = bytes => `${Math.round(bytes / GIB)} GB`, line = x => `${Math.round(x * 100)}%`;
  const rows = [["primary", "正常库所在卷"], ["emergency", "应急库所在卷"]].map(([key, name]) => {
    const v = d[key];
    return v ? [name, `<b>${(v.used_fraction * 100).toFixed(1)}%</b>`, gb(v.free), gb(v.total)] : [name, badge("无法读取", "warn"), "—", "—"];
  });
  return `<h3>磁盘占用</h3>${table(["数据卷", "占用率", "剩余", "总量"], rows)}<p class="chart-caption">读取于 ${stamp(d.measured_at)}。正常库所在卷占用到 ${line(d.cleanup_start)} 开始滚动删除，删到 ${line(d.cleanup_stop)} 停止；到 ${line(d.emergency_start)} 转写应急库，应急库所在卷至少留 ${gb(d.emergency_reserve_bytes)}。</p>`;
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
  setHtml("check-sources", table(["数据源", "类型", "数据状态", "通信", "最后通信", "重新同步次数", "连续建连失败次数"], sources.map(([id, s]) => [esc(id), esc(KINDS[s.kind] || s.kind), esc(SOURCE_STATES[s.state] || s.state), s.alarm ? badge("通信告警", "bad") : badge("正常", "ok"), clock(s.last_comm_wall), ...counterCells(id)]))
    + `<p class="chart-caption">${esc(COUNTER_NOTE)}</p>`);
  const negotiated = data?.negotiated || {};
  setHtml("check-negotiated", health.error ? `<p class="log-error">读取失败：${esc(health.error)}</p>` : sources.filter(([, s]) => s.kind === "opcua").map(([id]) => {
    const n = negotiated[id];
    if (!n) return `<h3>${esc(id)}</h3><p class="muted">尚未取得协商结果（订阅未建立）。</p>`;
    const limit = n.queue_capability === "OVERWRITE_UNDETECTABLE"
      ? badge("覆盖不可检测", "warn") + " 服务端把测点队列修订为 1：中间通知可能被覆盖且没有溢出标记；发布序号连续不能证明监控项无丢值。"
      : badge("队列溢出可检测", "ok") + " 队列大于 1，溢出会被检测：只影响该点（记录溢出、该点重新计时），不重建订阅。";
    return `<h3>${esc(id)}</h3>${table(["参数", "请求值", "服务端实际授予"], negotiatedRows(n))}<p>${limit}</p>${(n.violations || []).map(v => `<p class="log-error">不满足约束：${esc(v)}</p>`).join("")}`;
  }).join("") || '<p class="muted">没有 OPC UA 数据源。</p>');
  const archive = data?.archive, [where, kind] = TARGETS[archive?.target] || [archive?.target || "未知", "warn"], q = archive?.queue;
  setHtml("check-archive", !archive ? '<p class="muted">正在读取…</p>' : `<p>写入目标：${badge(where, kind)}${q ? `；写入队列 ${q.depth}/${q.capacity}，已确认 ${q.committed} 组，被拒 ${q.refused}，失败重试 ${q.failures + q.unknown}${q.last_error ? `，最近错误（<code>${esc(q.last_error)}</code>）` : ""}` : ""}</p>`
    + table(["数据源", "归档状态", "已确认水位（历史覆盖终点）", "缺口起点"], Object.entries(archive.sources).map(([id, s]) => {
      const [text, kind] = ARCHIVE_STATES[s.archive] || [s.archive, "warn"];
      return [esc(id), badge(text, kind), s.confirmed_watermark == null ? "尚无（还没有确认写入的数据）" : stamp(s.confirmed_watermark), s.gap_since == null ? "—" : stamp(s.gap_since)];
    })));
  setHtml("check-retention", retentionHtml(archive));
  const f = status?.faults;
  setHtml("check-faults", !f ? '<p class="muted">等待实时数据</p>' : `<p>${f.archive_incomplete ? badge("故障归档不完整", "bad") : badge("故障归档完整", "ok")} 未写入的缓冲 <b>${f.buffered}/${f.limit}</b>，累计拒绝 <b>${f.rejected_total}</b> 条</p>`
    + table(["数据源", "缺口摘要：丢失条数", "最早", "最晚", "涉及通道"], Object.entries(f.loss || {}).map(([id, l]) => [esc(id), l.count, stamp(l.first_wall), stamp(l.last_wall), esc((l.points || []).join("、"))]))
    + `<p class="chart-caption">故障记录在写入数据库之前只在内存中，进程退出会丢失。缓冲满了就拒绝新记录，已有记录不受影响，采集照常。</p>`);
  setHtml("check-timing", table(["项目", "值"], [
    ["故障确认时长", `${timing.fault_confirm_seconds} 秒`], ["OPC UA 数据有效期 / 断连告警", `${timing.ua_data_validity} 秒 / ${timing.ua_alarm_seconds} 秒`], ["Modbus 断连告警", `${timing.modbus_alarm_seconds} 秒`],
    ["页面读取间隔 / 单次读取上限", `${timing.ui_poll_interval} 秒 / ${timing.ui_poll_timeout} 秒`], ["前端数据中断提示", `${timing.ui_stall_notice} 秒`], ["故障缓冲上限", `${timing.fault_buffer_limit} 条`], ["水位提交间隔", `${timing.watermark_interval} 秒`],
  ]));
}

// ------------------------------------------------------------------------ page 13: communication and log (终版 §15.1)

// Everything on this page comes from ONE /api/health answer, read every 5 s while the page is in front and replaced as a
// whole - the input table is never patched with the live readings, so a row cannot mix two observations and the
// page cannot look fresher than the moment it was read.  The log is this run's memory only, no level, no history.
const COMM_EVENTS = {
  NEW_GENERATION: "新连接/订阅代次建立", FIRST_COMM: "首次有效通信", COMM_RECOVERED: "通信恢复（中断后首个有效响应）",
  COMM_ALARM_CLEARED: "通信告警解除", RESYNC: "进入重新同步", DATA_EXPIRED: "数据失效", COMM_ALARM: "通信告警",
  APP_LOSS: "应用丢失（入口满）", POLL_TIMEOUT: "Modbus 请求无应答", POLL_EXCEPTION: "Modbus 协议异常应答",
  ITEMS_REFUSED: "监控项被服务器拒绝（仅这些点不可用，下次建订阅时再试）",
};
const COMM_PHASES = {CONNECT: "建连", SUBSCRIBE: "建订阅"};
const COMM_ENDS = {
  REASON_CHANGED: "原因改变", CONNECT_OK: "建连成功", SUBSCRIBE_OK: "订阅创建成功", SUCCESS: "请求成功", NEW_GENERATION: "新代次建立",
};
const commEvent = e => e.kind === "ATTEMPT" ? `${COMM_PHASES[e.phase] || e.phase}尝试失败` : COMM_EVENTS[e.event] || e.event;
const commEnd = reason => COMM_ENDS[reason] || (String(reason).startsWith("EVENT:") ? `其他事件（${COMM_EVENTS[reason.slice(6)] || reason.slice(6)}）` : reason);
const stampAt = seconds => Number.isFinite(seconds) ? stampMs(seconds) : "—";  // a missing time is a dash, never ".000"
const stateCode = code => code ? ` 0x${(code >>> 0).toString(16).toUpperCase()}` : "";
// The log's reason codes in words (终版 §17.5); the code itself stays beside them for whoever traces it.
const REASON_NAMES = {
  FIRST_VALID_RESPONSE: "首个有效响应", VALID_RESPONSE: "有效响应", NEVER_RESPONDED: "从未有过有效响应", NO_VALID_RESPONSE: "持续没有有效响应",
  VALIDITY_EXCEEDED: "超过数据有效期", SERVER_REFUSED: "服务器拒绝", CONNECT: "建连", CONNECT_FAILED: "建连失败", SILENCE: "通知中断",
  SEQUENCE_GAP: "发布序号缺号", UNKNOWN_HANDLE: "未知的监控项", NO_ANSWER: "请求无应答", INCOMPLETE_RESPONSE: "应答不完整",
  APP_CONNECT_TIMEOUT: "建连超时", LIBRARY_NO_REASON: "连接失败，库未提供原因", QUEUE_FULL: "入口已满", TIMEOUTS: "连续超时", ENDED: "会话结束",
};
const REASON_PREFIXES = [["ERROR:", "程序异常"], ["CALLBACK_ERROR:", "回调异常"], ["SUBSCRIPTION_STATUS:", "订阅状态变化"], ["DOWN:", "连接断开"], ["APP_LOSS:", "应用丢失"], ["EXCEPTION_CODE_", "协议异常码"]];
function reasonName(code) {
  const text = String(code ?? "");
  if (REASON_NAMES[text]) return REASON_NAMES[text];
  for (const [prefix, name] of REASON_PREFIXES) if (text.startsWith(prefix)) return `${name}（${reasonName(text.slice(prefix.length))}）`;
  return text;
}
const reasonCell = code => `${reasonName(code) !== String(code) ? `${esc(reasonName(code))} ` : ""}<code>${esc(code)}</code>`;

function commHtml() {
  return `<div class="note">这里只有<b>本次运行</b>的通信记录，后端重启后清空，不读取归档，也没有更早的历史。表中读数取自同一时刻的快照，不表示各点同时采样；源时间戳按服务器给出的原样显示。</div>
    <p id="comm-taken" class="chart-caption"></p>
    <div class="card"><h3>数据源状态</h3><div id="comm-sources"></div></div>
    <div class="card"><h3>点位映射与实际读数</h3><div id="comm-inputs" class="scroll"></div></div>
    <div class="card"><h3>通信日志（本次运行）</h3><div id="comm-log-meta"></div><div id="comm-log" class="scroll"></div></div>`;
}
function mountComm() {
  health.data = null;  // a visit starts empty: rows left over from an earlier visit would carry an old time
  health.error = "";
  drawComm();
  loadHealth(true);  // the first read is at once, then every 5 s while this page stays in front
}
function mappingText(point) {
  if (!point) return "—";
  return point.node_id ? `NodeId ${point.node_id}` : `设备 ${point.device_id}，${point.register_kind === "input" ? "输入" : "保持"}寄存器 ${point.address}（${point.data_type}）`;
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
    const value = row.value == null ? "—" : `${reading(row.value, point?.unit)} ${esc(unitText(point?.unit))}`;
    return [`${esc(label(row.point_id))}<br><small>${esc(row.point_id)}</small>`, esc(row.source_id), esc(mappingText(point)), value,
      seen ? esc(qualityText(row.quality)) + stateCode(row.status_code) : "—",
      row.valid ? badge("有效", "ok") : badge(reasonText(row.reason), "bad"),
      seen ? stampAt(row.received_time) : "尚无观测",
      !seen ? "—" : row.source_time == null ? "未知" : stampAt(row.source_time)];
  }));
}
function commLog(log) {
  const meta = [`保留 <b>${log.entries.length}</b> / ${log.limit} 条`,
    `已淘汰 <b>${log.evicted}</b> 条${log.oldest_time == null ? "" : `，现存最早记录 ${stampAt(log.oldest_time)}`}`,
    `诊断丢失 <b>${log.diag_lost}</b> 次（只是日志没记上，不是数据丢失）`];
  const incomplete = log.incomplete ? " " + badge("日志不完整", "warn") : "";
  const body = log.entries.length
    ? table(["序号", "发现时刻", "数据源", "事件", "原因 / 详情", "代次", "次数", "首次失败", "最近失败", "状态", "最后有效通信"], [...log.entries].reverse().map(commEntry))
    : '<p class="empty">暂无记录。这里只有本次运行的记录，没有记录不表示从未发生通信故障。</p>';
  return {meta: `<p>${meta.join("；")}${incomplete}</p>`, body};
}
function commEntry(e) {
  const detail = [e.scope ? `请求 ${esc(e.scope)}` : "", e.detail ? esc(e.detail) + (e.detail_truncated ? "（已截断）" : "") : e.detail_truncated ? "详情已省略（过长）" : ""].filter(Boolean);
  const when = e.state === "ACTIVE" ? badge("进行中")
    : e.state === "ENDED" ? `${badge("已结束")} ${stampAt(e.ended_time)}，${esc(commEnd(e.end_reason))}` : "—";
  const last = e.last_comm_time == null ? "此前从未有过有效通信"
    : `${stampAt(e.last_comm_time)}，发现时已隔 ${fmt(e.silent_seconds, 1)} 秒`;
  return [e.seq, stampAt(e.found_time), esc(e.source_id), esc(commEvent(e)), `${reasonCell(e.reason)}${detail.map(d => `<br>${d}`).join("")}`,
    e.kind === "ATTEMPT" ? "跨尝试，不绑定单一代次" : e.conn_gen ?? "尚无代次", e.count,
    e.state ? stampAt(e.first_failure) : "—", e.state ? stampAt(e.last_failure) : "—", when, last];
}
function drawComm() {
  if (state.page !== 13 || !$("comm-sources")) return;
  const comm = health.data?.communication;
  if (!comm) {
    const text = health.error ? `<p class="log-error">读取失败：${esc(health.error)}</p>` : '<p class="muted">正在读取…</p>';
    for (const id of ["comm-sources", "comm-inputs", "comm-log"]) setHtml(id, text);
    $("comm-taken").textContent = "";
    setHtml("comm-log-meta", "");
    return;
  }
  setHtml("comm-taken", `快照时刻：<b>${stampAt(comm.taken_time)}</b>（每行的“后端接收时刻”才是该点的观测时刻）${health.error ? `；<span class="log-error">最近一次刷新失败：${esc(health.error)}，以下仍是上次读到的内容</span>` : ""}`);
  setHtml("comm-sources", commSources(health.data));
  setHtml("comm-inputs", commInputs(comm.inputs));
  const log = commLog(comm.log);
  setHtml("comm-log-meta", log.meta);
  setHtml("comm-log", log.body);
}

// ------------------------------------------------------------------------- page 14: roles and accounts

const people = {list: null, audit: null, auditError: "", created: "", error: "", note: "", confirming: null};
// Management records in words (终版 §17.5): what was done, by whom, to what.
const ACTIONS = {CREATE_ACCOUNT: "新建只读账户", REVOKE_ACCOUNT: "停用账户", SET_PASSWORD: "修改口令", ROLLING_DELETE: "滚动删除", ROLLING_DELETE_START: "开始滚动删除", ROLLING_DELETE_END: "滚动删除结束"};
const RESULTS = {COMMITTED: "已生效", OK: "完成", FAILED: "失败"};
const auditTarget = text => String(text ?? "—").replace(/^buckets (\d+)-(\d+)$/, "第 $1 至 $2 号桶");
function mountPeople() {
  people.created = people.note = "";  // a new credential (or a changed password's note) is shown once, not again later
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
      ["创建、停用只读账户；修改自己的口令；查看管理操作记录", "不允许", "允许"],
      ["在线增加、删除或修改通道、阈值与配置", "不允许", "不允许（改配置文件后由部署人员重启）"],
    ])}<p class="chart-caption">这里显示账户的实际状态。配置要改配置文件、重启后才生效。</p></div>
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
    !a.active ? "—" : a.role === "admin" ? '<span class="muted">唯一管理员，不能停用</span>' : people.confirming === a.username ? `<button data-revoke="${esc(a.username)}" class="primary">确认停用 ${esc(a.username)}</button>` : `<button data-revoke="${esc(a.username)}">停用</button>`]);
  const cut = people.created.indexOf(":");  // shown apart, as the login page asks for them (修复轮07)
  $("account-area").innerHTML = `${people.created ? `<div class="credential-result" id="new-credential">仅此一次显示，请保存：用户名 <code id="new-user">${esc(people.created.slice(0, cut))}</code>，口令 <code id="new-secret">${esc(people.created.slice(cut + 1))}</code></div>` : ""}${people.error ? `<p class="log-error">${esc(people.error)}</p>` : ""}${people.note ? `<p class="muted">${esc(people.note)}</p>` : ""}
    <form id="account-form" class="controls"><input name="username" aria-label="新用户名" placeholder="新用户名" required maxlength="64" pattern="[A-Za-z0-9_\\-]+"><input type="hidden" name="role" value="viewer"><button type="submit">创建只读账户</button></form>${table(["账户", "角色", "状态", "操作"], rows)}
    <form id="password-form" class="controls"><input name="secret" type="password" aria-label="我的新口令" placeholder="我的新口令（至少 8 位）" required minlength="8" autocomplete="new-password"><input name="confirm" type="password" aria-label="再输一次新口令" placeholder="再输一次" required autocomplete="new-password"><button type="submit">修改我的口令</button></form>`;
  $("account-form").onsubmit = async event => {
    event.preventDefault();
    people.error = people.note = "";
    try {
      people.created = (await api("/api/admin/accounts", {method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(event.target)))})).credential;
    } catch (error) { people.created = ""; people.error = error.message; }
    loadPeople();
  };
  $("password-form").onsubmit = async event => {
    event.preventDefault();
    const {secret, confirm} = Object.fromEntries(new FormData(event.target));
    people.error = people.note = "";
    if (secret !== confirm) { people.error = "两次输入的口令不一致"; drawPeople(); return; }
    try {
      await api("/api/admin/password", {method: "POST", body: JSON.stringify({secret})});
      state.credential = `${state.me.username}:${secret}`;  // the credential in use stopped working: the page goes on with the new one
      sessionStorage.setItem("htsg.credential", state.credential);
      people.note = "口令已修改：本页已改用新口令，下次登录用新口令；部署时的初始口令仍可作兜底。";
    } catch (error) { people.error = error.message; }
    loadPeople();
  };
  $("account-area").querySelectorAll("[data-revoke]").forEach(b => b.onclick = async () => {
    if (people.confirming !== b.dataset.revoke) { people.confirming = b.dataset.revoke; drawPeople(); return; }  // a second click confirms
    people.confirming = null;
    people.error = "";
    try { await api(`/api/admin/accounts/${encodeURIComponent(b.dataset.revoke)}`, {method: "DELETE"}); } catch (error) { people.error = error.message; }
    loadPeople();
  });
  $("audit-log").innerHTML = people.audit ? table(["时间", "操作者", "操作", "对象", "结果"], people.audit.map(a => [stamp(a.time), esc(a.actor === "system" ? "系统" : a.actor), esc(ACTIONS[a.action] || a.action), esc(auditTarget(a.target)), esc(RESULTS[a.result] || a.result)])) : `<p class="muted">管理操作记录不可用：${esc(people.auditError || "正在读取…")}</p>`;
}

// --------------------------------------------------------------------- the current state, read once a second

function setConnection(text, kind) {
  $("connection").textContent = text;
  $("connection").className = `badge ${kind}`;
}
// One reading at a time (终版 §13.3, 修复轮05): a tick that finds a reading outstanding is skipped, never queued, so
// nothing piles up and nothing is made up afterwards.  A reading is given up at its limit; only a reading that is
// whole replaces what is shown.  Durations use the page's monotonic clock; the trend keeps the server's.
const reader = {timer: null, busy: false, controller: null, began: 0, good: 0, run: null, taken: null, failure: ""};
function startReading() {
  stopReading();
  Object.assign(reader, {began: performance.now(), good: 0, run: null, taken: null, failure: ""});
  state.stalled = false;
  setConnection("连接中", "warn");
  reader.timer = setInterval(readNow, state.config.timing.ui_poll_interval * 1000);
  readNow();
}
function stopReading() {
  clearInterval(reader.timer);
  const outstanding = reader.controller;
  Object.assign(reader, {timer: null, busy: false, controller: null});
  outstanding?.abort();
}
async function readNow() {
  if (state.stopped || reader.busy) return;
  const controller = new AbortController();
  const limit = setTimeout(() => controller.abort(), state.config.timing.ui_poll_timeout * 1000);  // up to the whole answer
  Object.assign(reader, {busy: true, controller});
  let response = null, reading, read = false;
  try {
    response = await fetch("/api/snapshot", {headers: {Authorization: `Bearer ${state.credential}`}, cache: "no-store", signal: controller.signal});
    if (response.ok) { reading = await response.json(); read = true; }
  } catch (error) {
    if (reader.controller === controller) {
      reader.failure = error.name === "AbortError" ? "超过时限未取得完整答复" : error instanceof SyntaxError ? "答复无法解析" : "网络不可用或后端未答复";
    }
  } finally {
    clearTimeout(limit);
  }
  if (reader.controller !== controller) return;  // signed out, or reading started over, meanwhile
  Object.assign(reader, {busy: false, controller: null});
  if (response?.status === 401) { logout("凭据无效或账户已停用，请重新登录。"); return; }
  if (!read) {
    if (response && !response.ok) reader.failure = `后端答复 ${response.status}`;
    return;
  }
  const before = {run: reader.run, taken: reader.taken};
  try {
    take(state.live, reading);  // whole or nothing: a refused reading changes nothing and is no answer
  } catch (error) {
    if (!(error instanceof Rejected)) throw error;
    reader.failure = `数据未通过校验：${error.message}`;
    return;
  }
  if (before.run !== null && reading.run_id !== before.run) checkConfig();  // the backend restarted
  Object.assign(reader, {good: performance.now(), run: reading.run_id, taken: reading.taken_time, failure: ""});
  trendReading(reading, before);
  state.serverWall = reading.taken_time;  // the server's clock: the trend never mixes in this PC's
  if (state.stalled) { state.stalled = false; notice(""); }
  if (!state.drawFailed) setConnection("实时连接", "ok");
  invalidate();
}
// A restart with the same configuration needs nothing; another one is said until the page is reloaded, since the
// channels, names and limits on screen are still the old ones (修复轮13).
async function checkConfig() {
  try {
    const fresh = await api("/api/ui-config");
    if (!state.stopped && fresh.config_id !== state.config.config_id) { state.configChanged = true; notice(state.stalled ? $("notice").textContent : ""); }
  } catch { /* asked again at the next restart; the readings go on meanwhile */ }
}
// Back on the page: read at once, unless a reading is outstanding; what was missed meanwhile is not made up.
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") readNow(); });
// Three seconds without a good reading - counted from the start of reading if none came yet - and the page says so;
// what it showed is taken off the screen rather than left looking current.
setInterval(() => {
  if (state.stopped || state.stalled || !state.config) return;
  const limit = state.config.timing.ui_stall_notice * 1000;
  if (performance.now() - (reader.good || reader.began) < limit) return;  // 满3秒 (§13.3); checked every 500 ms
  state.stalled = true;
  trendBreak(state.serverWall);
  state.live = newLive();
  setConnection("数据中断", "bad");
  state.drawFailed = false;  // nothing is drawn while interrupted; the interruption is what is said
  notice(`已超过 ${Math.round(limit / 1000)} 秒没有取得可用的后端数据${reader.failure ? `（最近一次：${reader.failure}）` : ""}。实时读数已停止显示，正在自动重试；这不代表设备通信状态，历史查询不受此提示影响。`);
  draw();
}, 500);

// -------------------------------------------------------------------------------------- login / logout

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {...options, headers: {Authorization: `Bearer ${state.credential}`, "Content-Type": "application/json", ...options.headers}});
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("连不上后端服务，请确认后端已启动、网络畅通");  // 终版 §17.5: not the browser's English
  }
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { detail = "服务器没有给出说明"; }
    if (response.status === 401) logout("凭据无效或账户已停用，请重新登录。");
    throw new Error(`${typeof detail === "string" ? detail : "输入不符合要求"}（${response.status}）`);  // 终版 §17.5: the backend's words, in Chinese, the code beside
  }
  return response.json();
}
async function login() {
  try {
    state.me = await api("/api/me");
    state.config = await api("/api/ui-config");
    state.stopped = false;
    sessionStorage.setItem("htsg.credential", state.credential);
    $("project-title").textContent = state.config.project?.title || "HTSG 锅炉高温应变监测";
    document.title = $("project-title").textContent;
    $("identity").textContent = `${state.me.username}（${state.me.role === "admin" ? "管理员" : "只读用户"}）`;
    const kept = Number(sessionStorage.getItem(PAGE_KEY));  // a reload stays on the page in front (修复轮13)
    state.page = pages.some(p => p.id === kept) ? kept : 1;
    $("login").hidden = true;
    $("workspace").hidden = false;
    $("logout").hidden = false;
    $("login-secret").value = "";
    notice("");
    renderPage();
    startReading();
  } catch (error) {
    $("login-error").textContent = `连接失败：${error.message}`;
  }
}
function logout(message = "") {
  state.stopped = true;
  stopReading();
  Object.assign(state, {stalled: false, drawFailed: false, configChanged: false, page: 1});
  sessionStorage.removeItem(PAGE_KEY);
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
  Object.assign(people, {list: null, audit: null, created: "", error: "", note: "", confirming: null});
  notice("");
  $("login-error").textContent = message;
}
$("login-form").onsubmit = async event => {
  event.preventDefault();
  state.credential = `${$("login-user").value.trim()}:${$("login-secret").value.trim()}`;  // asked apart, sent as before (修复轮07)
  await login();
};
$("logout").onclick = () => logout();
loadReplay();
if (state.credential) login();
