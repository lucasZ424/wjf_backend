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
