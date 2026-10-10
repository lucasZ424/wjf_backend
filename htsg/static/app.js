"use strict";
import {apply, newLive, Rejected} from "/static/stream.js";

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
const REASONS = {
  SOURCE_WAITING: "等待首次数据", SOURCE_EXPIRED: "数据已失效", SOURCE_RESYNC: "重新同步中",
  QUALITY: "质量不良", EXTREME: "超出物理极值", NO_VALUE: "无数值", UNKNOWN: "尚无数据",
  POLL_MISSING: "本轮未应答", POLL_TIMEOUT: "本轮超时", POLL_EXCEPTION: "本轮协议异常", POLL_CONNECTION: "本轮连接失败",
  SOURCE_INVALID: "来源数据无效", INPUT_UNAVAILABLE: "输入不可用", BASELINE_ONLY: "等待连续两轮",
  NOT_A_PLANNED_ROUND: "非计划轮次", EXTREME_INPUT: "输入超出物理极值", NONFINITE: "结果非有限数",
};
const reasonText = code => REASONS[code] || code || "无效";
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
const replay = {fault: null, center: null, seconds: 300, points: []};
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
        <p class="chart-caption">窗口取中心时间前后各半个窗口；可同时查看同一窗口内其他已保存通道。选择故障只填入时间点和默认通道，也可全部手动填写。</p>
      </div>
      <div class="card" id="replay-board"><h3>回溯结果</h3><div id="replay-result" class="muted">尚未查询。</div></div>
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
  $("replay-center").onchange = event => { replay.center = fromLocalInput(event.target.value); drawLock(); };
  $("replay-seconds").onchange = event => {
    if (!event.target.checkValidity()) { event.target.reportValidity(); return; }
    replay.seconds = Number(event.target.value);
  };
  $("replay-points").onchange = event => { replay.points = [...event.target.selectedOptions].map(o => o.value); };
  $("replay-lock").onclick = event => { if (event.target.id === "unlock") { replay.fault = null; drawLock(); drawFaults(); } };
  syncSettings();
  loadFaults(true);
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
if (state.credential) login();
