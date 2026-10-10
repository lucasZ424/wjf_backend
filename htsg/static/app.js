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
  return view && view.valid ? `${fmt(view.value)} ${esc(definition(id).unit)}` : "—";
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
  {id: 1, name: "系统总览", html: () => `<div class="card"><h3>过程量 · 实时</h3><div id="process-table"></div></div>`,
    update() {
      $("process-table").innerHTML = table(["过程量", "数值", "状态"], (state.config.ui.process_points || []).map(id => [esc(label(id)), valueText(id), stateBadges(id)]));
    }},
  {id: 5, name: "历史查询", html: historyHtml, mount: mountHistory},
];
const currentPage = () => pages.find(p => p.id === state.page) || pages[0];

function renderPage() {
  if (!state.config) return;
  const page = currentPage();
  $("navigation").innerHTML = pages.map(p => `<button data-page="${p.id}" class="${p.id === page.id ? "active" : ""}"><span>${String(p.id).padStart(2, "0")}</span>${esc(p.name)}</button>`).join("");
  $("navigation").querySelectorAll("button").forEach(b => b.onclick = () => { state.page = Number(b.dataset.page); renderPage(); });
  $("workspace").innerHTML = `<div class="page-heading"><div><span class="eyebrow">HTSG / ${String(page.id).padStart(2, "0")}</span><h2>${esc(page.name)}</h2><span class="muted">${esc(state.config.project?.title || "")}</span></div><span class="badge">数据来源：后端协议主动读取</span></div>${page.html()}`;
  page.mount?.();
  renderLive();
}
function renderLive() {
  if (!state.config) return;
  currentPage().update?.();
  const live = state.live;
  $("stream-time").textContent = live.channels ? `最新消息：${clock(state.lastMessage / 1000)} · 序号 ${live.seq}` : "等待后端数据";
}
// Many messages can arrive in one frame; the page is drawn once per frame, from the state as it then is.
function invalidate() {
  if (state.drawing) return;
  state.drawing = true;
  requestAnimationFrame(() => { state.drawing = false; renderLive(); });
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
  let result;
  try {
    result = apply(state.live, JSON.parse(text));
  } catch (error) {
    if (!(error instanceof Rejected) && !(error instanceof SyntaxError)) throw error;
    // nothing was applied: drop the connection and take a new snapshot rather than show half of a message
    state.rejected = true;
    notice(`实时消息未通过校验（${error.message}），已整条丢弃并重新同步。`);
    ws.close();
    return;
  }
  state.lastMessage = Date.now();
  if (state.stalled) { state.stalled = false; notice(""); }
  else if (state.rejected && result.snapshot) { state.rejected = false; notice(""); }
  setConnection("实时连接", "ok");
  invalidate();
}
function lost(ws, code) {
  if (ws !== state.ws || state.stopped) return;
  state.ws = null;
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
