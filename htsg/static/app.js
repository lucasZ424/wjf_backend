"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const time = value => Number.isFinite(value) ? new Date(value * 1000).toLocaleString("zh-CN", {hour12:false}) : "—";
const number = value => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", {maximumFractionDigits:state.config?.ui?.precision ?? 2}) : "—";
const pages = [[1,"系统总览"],[2,"水冷壁应变场"],[4,"热应力分析"],[5,"历史查询"],[12,"系统自检"],[14,"通信 · 日志"],[15,"系统管理 · 权限"]];
const state = {credential:sessionStorage.getItem("htsg.credential") || "", config:null, me:null, frame:null, active:[], page:1, point:null, field:"strain", station:null, seconds:300, center:null, history:[], historyEpoch:0, thermalEpoch:0, historyLoading:false, thermalLoading:false, events:[], event:null, health:null, communications:null, ws:null, stopped:true, reconnect:null, refreshing:false, eventsLoading:false, eventsPending:false, eventsTimer:null};
function notice(message) { $("notice").textContent=message; $("notice").hidden=!message; }
function archiveStopReason() {
  const h=state.health, budget=h?.storage_budget;
  if(h?.history_archive?.reason==="DEVELOPMENT_WRITE_BUDGET_EXHAUSTED" || (budget?.budget_bytes>0 && Math.max(budget.bytes_written||0,budget.development_growth||0)>=budget.budget_bytes))return "开发库写入预算已用尽";
  return h?.history_archive?.suspended ? (h.history_archive.reason || "归档写入已暂停") : "";
}
function renderArchiveWarning() {
  let banner=$("archive-warning");
  if(!banner){banner=document.createElement("div");banner.id="archive-warning";banner.className="note";banner.setAttribute("role","alert");banner.style.cssText="margin:0;padding:14px 23px;border-left:5px solid var(--red);color:var(--red);font-weight:600";$("notice").before(banner);}
  const reason=archiveStopReason();banner.hidden=state.stopped||!reason;
  banner.textContent=reason?`归档已停止：${reason}。实时值仍可变化，但新的历史趋势和故障记录可能缺失。请查看系统自检并恢复可写存储后再验证追忆。`:"";
}
function scheduleEvents() {
  if(state.stopped||state.page!==5||state.eventsTimer)return;
  state.eventsTimer=setTimeout(()=>{state.eventsTimer=null;if(!state.stopped&&state.page===5)loadEvents(false,true);},1000);
}
async function api(path, options={}) {
  const response = await fetch(path, {...options, headers:{Authorization:`Bearer ${state.credential}`, "Content-Type":"application/json", ...options.headers}});
  if (!response.ok) {
    let detail; try { detail=(await response.json()).detail; } catch { detail=response.statusText; }
    if (response.status===401) logout("凭据无效或账户已停用，请重新登录。");
    throw new Error(`${response.status} · ${typeof detail === "string" ? detail : "请求未通过校验"}`);
  }
  return response.json();
}
function definitions() { return [...(state.config?.points || []), ...(state.config?.derived || [])]; }
function definition(id) { return definitions().find(p=>p.id===id) || {id,unit:""}; }
function label(id) { const p=definition(id); return state.config?.ui?.point_labels?.[id] || p.label || p.name || id; }
function value(id) { return state.frame?.values?.[id] || state.frame?.derived_results?.[id] || {}; }
function quality(item) { return item?.quality || item?.status || "UNAVAILABLE"; }
function badge(text) { const ok=["GOOD","OK","NORMAL","PRIMARY","EXTERNAL","connected","CONNECTED"].includes(text); const bad=["EXTREME","HIGH","LOW","BAD","UNAVAILABLE","NOT_AVAILABLE","DISCONNECTED","PARTIAL"].includes(text); return `<span class="badge ${ok?"ok":bad?"bad":"warn"}">${esc(text)}</span>`; }
function card(id) { const p=definition(id), v=value(id), abnormal=v.extreme||v.high_occurred||v.low_occurred; return `<div class="card metric ${abnormal?"alarm":""}"><small>${esc(label(id))}</small><div class="value">${number(v.value)}<small>${esc(p.unit)}</small></div>${badge(v.extreme?"EXTREME":abnormal?"窗口发生越限":quality(v))} ${v.estimate?'<span class="badge warn">研究估算</span>':""}</div>`; }
function table(head,rows) { return `<div class="table-wide"><table><thead><tr>${head.map(h=>`<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.length?rows.map(row=>`<tr>${row.map(x=>`<td>${x}</td>`).join("")}</tr>`).join(""):`<tr><td colspan="${head.length}" class="empty">暂无数据</td></tr>`}</tbody></table></div>`; }
function stations() { return state.config?.ui?.stations || []; }
function selectedStation() { return stations().find(s=>s.id===state.station) || stations()[0]; }
function historyDefinitions() { return definitions(); }
function historyWindow() {
  const center=state.center ?? Date.now()/1000-state.seconds/2;
  return {center,start:center-state.seconds/2,end:center+state.seconds/2};
}
function localInputTime(value) {
  if(value===null)return "";
  const date=new Date(value*1000);
  return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,19);
}
function chartBlock() { return `<div class="card"><h3 id="trend-title">窗口历史重现</h3><div class="controls"><label>通道<select id="point-select">${historyDefinitions().map(p=>`<option value="${esc(p.id)}">${esc(label(p.id))} (${esc(p.unit)})</option>`).join("")}</select></label><label>中心时间（本地）<input id="center-time" type="datetime-local" step="1" value="${localInputTime(state.center)}"></label><label>窗口长度（秒）<input id="window-select" type="number" min="1" max="31622400" step="1" value="${state.seconds}"></label><button id="refresh-history">重现窗口</button><button id="live-history" ${state.center===null?"hidden":""}>返回实时窗口</button></div><canvas class="chart" id="trend"></canvas><div class="chart-caption" id="history-status">读取已归档数据；无效值与缺口不连线。</div></div>`; }
function renderPage() {
  if(!state.config) return;
  state.historyEpoch++;state.thermalEpoch++;
  if(state.event){const allowed=historyDefinitions().map(p=>p.id);if(!allowed.includes(state.point))state.point=allowed[0];if(!allowed.includes(selectedStation()?.stress))state.station=stations().find(s=>allowed.includes(s.stress))?.id||state.station;}
  $("navigation").innerHTML=pages.map(([id,name])=>`<button data-page="${id}" class="${state.page===id?"active":""}"><span>${String(id).padStart(2,"0")}</span>${name}</button>`).join("");
  const name=pages.find(p=>p[0]===state.page)[1];
  let content="";
  if(state.page===1) content=`<div id="overview-metrics" class="grid metrics"></div><div class="card" style="margin-bottom:16px"><h3>过程量 · 全部配置通道</h3><div id="process-overview"></div></div><div class="card"><h3>模拟现场场景</h3><div id="scenario-controls"></div><div id="simulation-status" class="chart-caption"></div></div>`;
  if(state.page===2) content=`<div class="grid two"><div class="card"><div class="controls"><h3>测点着色示意</h3><select id="field-select"><option value="strain">应变</option><option value="strain_change">应变变化量</option><option value="temperature">壁温</option><option value="stress">应力研究估算</option></select></div><div id="station-map" class="map"></div><div class="legend"><b>● 有效值</b><em>● 配置阈值越限</em><span>● 无有效数据</span></div><p class="chart-caption">仅表示配置测点的位置，不插值连续场，不推断真实结构拓扑。</p></div><div class="card"><h3>测点排序 · 按当前数值</h3><div id="station-ranking"></div></div></div>`;
  if(state.page===4) content=`<div class="controls"><label>分析测点<select id="station-select">${stations().map(s=>`<option value="${esc(s.id)}" ${state.event&&!historyDefinitions().some(p=>p.id===s.stress)?"disabled":""}>${esc(s.label||s.id)}</option>`).join("")}</select></label><span class="badge warn">点级研究估算</span></div><div id="thermal-metrics" class="grid metrics"></div><p class="chart-caption">上方数值为当前实时估算；下方曲线展示最近 5 分钟归档；自定义窗口请进入历史查询。</p><div><div class="card"><h3>模型依据与参数</h3><div id="model-info"></div></div></div><div id="thermal-trends" class="grid three"></div><div class="note">应力值是基于温差与约束假设的研究代理量；参数敏感性范围不是统计置信区间。正负号描述两路温度的差异方向，不直接代表材料拉压应力。</div>`;
  if(state.page===5) content=`<div class="grid two">${chartBlock()}<div class="card"><h3>故障参考点</h3><div id="active-events"></div><div class="controls"><button id="events-refresh">刷新故障记录</button><button id="events-more" hidden>更多故障记录</button></div><div id="events" class="scroll"></div></div></div><div id="event-detail" class="card" hidden></div>`;
  if(state.page===12) content=`<div id="health-metrics" class="grid metrics"></div><div class="grid two"><div class="card"><h3>队列与采集质量</h3><div id="health-queues"></div><div id="health-points" class="scroll"></div></div><div class="card"><h3>归档与存储</h3><div id="health-storage"></div></div></div><div class="note">传感器零漂、自检信号、硬件温度与设备运行时间：未提供硬件诊断输入。此处展示的是软件采集、队列和归档的实际状态。</div>`;
  if(state.page===14) content=`<div id="source-cards" class="grid two"></div><div class="card"><h3>通信记录</h3><div id="communication-logs" class="scroll"></div></div>`;
  if(state.page===15) content=`<div class="grid two"><div class="card"><h3>权限与配置</h3>${table(["操作","只读用户","管理员"],[["查看实时值、历史、事件、健康状态","允许","允许"],["选择模拟场景、创建/停用账户","不允许","允许"],["配置文件修改后重启","由部署人员操作","由部署人员操作"]])}<p class="chart-caption">显示实际账户状态；不推算在线用户数量。配置不会在运行中热切换。</p></div><div class="card"><h3>账户管理</h3><div id="account-area"></div></div></div><div class="card"><h3>管理操作记录</h3><div id="audit-log" class="scroll"></div></div>`;
  $("workspace").innerHTML=`<div class="page-heading"><div><span class="eyebrow">HTSG / ${String(state.page).padStart(2,"0")}</span><h2>${name}</h2><span class="muted">${esc(state.config.project?.title || "锅炉科研模拟现场")} · ${esc(state.config.project?.rated_mw || "—")} MW</span></div><span class="badge">数据来源：后端协议主动读取</span></div>${content}`;
  $("navigation").querySelectorAll("button").forEach(b=>b.onclick=()=>{state.page=Number(b.dataset.page);renderPage();});
  if($("point-select")) {
    $("point-select").value=state.point;
    const refreshWindow=()=>{loadHistory();};
    $("point-select").onchange=e=>{state.point=e.target.value;refreshWindow();};
    $("window-select").onchange=e=>{
      if(!e.target.checkValidity()){e.target.reportValidity();return;}
      state.seconds=Number(e.target.value);refreshWindow();
    };
    $("center-time").onchange=e=>{
      if(!e.target.value)return;
      const stamp=new Date(e.target.value).getTime()/1000;
      if(!Number.isFinite(stamp))return;
      state.center=stamp;$("live-history").hidden=false;refreshWindow();
    };
    $("refresh-history").onclick=refreshWindow;
    $("live-history").onclick=()=>{state.event=null;state.center=null;renderPage();};
    loadHistory();
  }
  if($("field-select")){ $("field-select").value=state.field; $("field-select").onchange=e=>{state.field=e.target.value; state.point=selectedStation()?.[state.field] || state.point; renderPage();}; }
  if($("station-select")){ $("station-select").value=selectedStation()?.id; $("station-select").onchange=e=>{state.station=e.target.value;state.point=selectedStation()?.stress || state.point;renderPage();}; }
  if($("events-refresh")){ $("events-refresh").onclick=()=>loadEvents(); $("events-more").onclick=()=>loadEvents(true); loadEvents(); }
  if(state.page===1) loadSimulation();
  if($("active-events")) $("active-events").textContent="持续越限满 5 秒确认；选择已归档故障记录定位历史窗口。";
  if(state.page===15) loadAccounts();
  if(state.page===5&&state.event){
  $("event-detail").hidden=false;
  $("event-detail").textContent=`参考故障：${label(state.event.point_id)} · ${time(state.event.time)} · ${number(state.event.value)} ${state.event.unit}。可修改中心时间、窗口长度和查看通道。`;
  }
  renderLive(); renderDiagnostics();
  if(state.page===4) loadThermalTrends();
}
function parameterTable(parameters) {
  if(!parameters)return '<p class="muted">尚无模型参数</p>';
  const p=parameters;
  return table(["科研假设参数","配置值"],[
    ["弹性模量 E",`${number(p.young_mpa/1000)} GPa`],
    ["线膨胀系数 α",`${typeof p.expansion_per_k==="number"?p.expansion_per_k.toExponential(3):"—"} 1/K`],
    ["约束系数 k（0～1）",number(p.restraint)],
    ["弹性模量相对变化幅度",`±${number(p.young_relative_uncertainty*100)}%`],
    ["膨胀系数相对变化幅度",`±${number(p.expansion_relative_uncertainty*100)}%`],
    ["约束系数绝对变化幅度",`±${number(p.restraint_uncertainty)}`],
    ["温度适用范围",`${number(p.min_temperature_c)} ～ ${number(p.max_temperature_c)} ℃`]
  ]);
}
function renderLive() {
  $("frame-time").textContent=state.frame?`最新帧：${time(state.frame.window_end)} · ${state.frame.config_id.slice(0,12)}`:"等待后端数据";
  if($("overview-metrics")){const s=stations()[0]||{};const ids=[...(state.config.ui?.process_points||[]).slice(0,3),s.strain,s.temperature,s.delta_temperature,"thermal_stress_peak"].filter(id=>id&&definitions().some(p=>p.id===id));$("overview-metrics").innerHTML=ids.map(card).join("");}
  if($("process-overview"))$("process-overview").innerHTML=table(["过程量","数值","质量","本窗口异常"],(state.config.ui?.process_points||[]).map(id=>{const v=value(id);return[esc(label(id)),`${number(v.value)} ${esc(definition(id).unit)}`,badge(quality(v)),v.high_occurred?badge("HIGH"):v.low_occurred?badge("LOW"):"—"];}));
  if($("station-map")){
    const list=stations().map(s=>({s,v:value(s[state.field]),id:s[state.field]}));
    $("station-map").innerHTML=list.map(({s,v,id})=>{const p=definition(id);const bad=v.extreme||v.high_occurred||v.low_occurred;const color=!["GOOD","OK"].includes(quality(v))?"#91aac5":bad?"#ff777b":"#57d1a6";return `<button class="station ${s.id===state.station?"selected":""}" data-station="${esc(s.id)}" style="left:${Math.max(8,Math.min(92,Number(s.x)||0))}%;top:${Math.max(15,Math.min(90,Number(s.y)||0))}%;--dot:${color}"><i></i><span>${esc(s.label||s.id)}<br>${number(v.value)} ${esc(p.unit)}</span></button>`;}).join("");
    $("station-map").querySelectorAll("button").forEach(b=>b.onclick=()=>{state.station=b.dataset.station;state.point=selectedStation()[state.field];renderPage();});
    $("station-ranking").innerHTML=table(["测点","数值","质量"],list.sort((a,b)=>(b.v.value??-Infinity)-(a.v.value??-Infinity)).map(({s,v,id})=>[esc(s.label||s.id),`${number(v.value)} ${esc(definition(id).unit)}`,badge(quality(v))]));
  }
  if($("thermal-metrics")){const modelOpen=$("model-info").querySelector("details")?.open;const s=selectedStation()||{};$("thermal-metrics").innerHTML=[s.temperature,s.inner_temperature,s.delta_temperature,s.stress,s.strain_change].filter(Boolean).map(card).join("");const v=value(s.stress);const estimate=v.estimate;const rule=definition(s.stress);$("model-info").innerHTML=estimate?`<p>${esc(estimate.model_id)} / ${esc(estimate.model_version)}</p><p>${esc(estimate.basis)}</p><p>参数敏感性范围：${(estimate.parameter_range_mpa||[]).map(number).join(" ～ ")} MPa</p>${parameterTable(estimate.parameters)}`:`<p>${badge(quality(v))} ${esc(v.reason || "等待模型输出")}</p>${parameterTable((state.config.models||[]).find(m=>m.id===rule.model_id))}`;$("model-info").innerHTML+=`<details><summary>完整模型配置与来源</summary><pre>${esc(JSON.stringify({derived:rule,models:state.config.models||[]},null,2))}</pre></details>`;$("model-info").querySelector("details").open=!!modelOpen;}
}
async function loadHistory() {
  if(!$("trend")||!state.point) return;
  const epoch=++state.historyEpoch, point=state.point, {center,start,end}=historyWindow();
  let cursor=null, rows=[],partial=false,more=false;
  $("history-status").textContent="正在读取已归档历史…";
  state.historyLoading=true;
  try {
    do { const query=new URLSearchParams({center,seconds:state.seconds,point,limit:1000});if(cursor)query.set("cursor",cursor); const result=await api(`/api/replay?${query}`); if(epoch!==state.historyEpoch)return; rows.push(...result.items);partial ||= result.partial;cursor=result.next_cursor;more=result.has_more; } while(cursor && rows.length<10000);
    if(!$("trend")||epoch!==state.historyEpoch)return;
    state.history=rows.map(r=>({t:r.window_start,run:r.run_id,v:r.payload?.value,q:quality(r.payload),extreme:!!r.payload?.extreme})).sort((a,b)=>a.t-b.t);
    $("trend-title").textContent=`${label(point)} · ${state.center!==null?"窗口历史重现":"归档趋势"}`;
    $("history-status").textContent=`${time(start)} → ${time(end)} · ${rows.length} 条${partial?" · 部分数据库不可用":""}${more?" · 已达本次显示上限，请缩短时间窗":""}。无效值与时间缺口不连线。`;
    plot($("trend"),state.history,definition(point).unit,start,end);
  } catch(error){ if(epoch===state.historyEpoch&&$("history-status")){$("history-status").textContent=`历史读取失败：${error.message}`;state.history=[];plot($("trend"),[],definition(point).unit,start,end);} }
  finally {if(epoch===state.historyEpoch)state.historyLoading=false;}
}
function plot(canvas,rows,unit,start,end){
  if(!canvas)return;rows=rows.slice().sort((a,b)=>a.t-b.t);canvas.plotData={rows,unit,start,end};const ratio=window.devicePixelRatio||1,w=canvas.clientWidth,h=215;canvas.width=w*ratio;canvas.height=h*ratio;const c=canvas.getContext("2d");c.scale(ratio,ratio);const valid=rows.filter(r=>typeof r.v==="number"&&Number.isFinite(r.v)&&["GOOD","OK"].includes(r.q));c.font='11px "Segoe UI",sans-serif';c.fillStyle="#91aac5";c.fillText(unit||"",5,12);
  if(!valid.length){c.fillText("暂无有效的已归档数据",Math.max(45,w/2-70),110);return;}
  let lo=Math.min(...valid.map(r=>r.v)),hi=Math.max(...valid.map(r=>r.v));const pad=(hi-lo)*.12||Math.max(1,Math.abs(lo)*.01);lo-=pad;hi+=pad;const left=60,right=w-12,top=23,bottom=185;
  for(let i=0;i<=4;i++){const y=top+(bottom-top)*i/4;c.strokeStyle="#304968";c.beginPath();c.moveTo(left,y);c.lineTo(right,y);c.stroke();c.fillStyle="#91aac5";c.fillText(number(hi-(hi-lo)*i/4),2,y+4);}
  const x=t=>left+(t-start)/Math.max(1,end-start)*(right-left),y=v=>bottom-(v-lo)/(hi-lo)*(bottom-top);c.strokeStyle="#73bcff";c.lineWidth=1.7;c.beginPath();let last=null;for(const r of rows){if(typeof r.v!=="number"||!["GOOD","OK"].includes(r.q)){last=null;continue;}if(!last||r.t-last.t>1.5||(last.run&&r.run&&last.run!==r.run))c.moveTo(x(r.t),y(r.v));else c.lineTo(x(r.t),y(r.v));last=r;}c.stroke();for(const r of valid.filter(r=>r.extreme)){c.fillStyle="#ff777b";c.beginPath();c.arc(x(r.t),y(r.v),4,0,Math.PI*2);c.fill();}c.fillStyle="#73bcff";if(valid.length===1){c.beginPath();c.arc(x(valid[0].t),y(valid[0].v),3,0,Math.PI*2);c.fill();}c.fillStyle="#91aac5";c.fillText(new Date(start*1000).toLocaleTimeString("zh-CN",{hour12:false}),left,207);c.fillText(new Date(end*1000).toLocaleTimeString("zh-CN",{hour12:false}),Math.max(left,right-64),207);
}
async function loadEvents(more=false,background=false){
  if(state.eventsLoading){if(!more)state.eventsPending=true;return;}
  state.eventsLoading=true;
  try{
    const result=await api(`/api/faults?limit=20${more&&state.eventCursor?`&cursor=${encodeURIComponent(state.eventCursor)}`:""}`);
    const previous=state.events;
    const rows=more?[...previous,...result.items]:background?[...result.items,...previous]:result.items;
    state.events=[...new Map(rows.map(r=>[(r.payload||r).id,r])).values()];
    if(background){const fresh=new Map(result.items.map(r=>[(r.payload||r).id,r]));state.events=state.events.map(r=>fresh.get((r.payload||r).id)||r);}
    if(!background||!previous.length)state.eventCursor=result.next_cursor;
    if(!$("events"))return;
    $("events-more").hidden=!state.eventCursor;
    $("events").innerHTML=table(["确认时间 / 通道","方向 / 确认值","重现"],state.events.map(r=>{const f=r.payload||r;return [`${time(f.time)}<br><small>${esc(label(f.point_id))}</small>`,`${badge(f.state)} ${number(f.value)} ${esc(f.unit)}`,`<button data-event="${esc(f.id)}">定位窗口</button>`];}));
    $("events").querySelectorAll("button").forEach(b=>b.onclick=()=>showEvent(b.dataset.event));
  }catch(error){notice(`故障记录读取失败：${error.message}`);}
  finally{state.eventsLoading=false;if(state.eventsPending){state.eventsPending=false;scheduleEvents();}}
}
async function showEvent(id){
  const row=state.events.find(r=>(r.payload||r).id===id);
  if(!row)return;
  state.event=row.payload||row;
  state.center=state.event.time;state.point=state.event.point_id;
  renderPage();

}
function renderDiagnostics(){
  renderArchiveWarning();
  const h=state.health;
  if(h&&$("health-metrics")){const openDetails=[...$("health-storage").querySelectorAll("details")].map(d=>d.open);const fields=[["后端运行时长",`${number(h.uptime_seconds)} s`],["归档路由",h.routing],["待写事件",h.queues?.event],["Pre 捕获作业",h.queues?.capture_jobs],["归档停写时长",h.history_archive?.seconds_since_last_success==null?"—":`${number(h.history_archive.seconds_since_last_success)} s`]];$("health-metrics").innerHTML=fields.map(([k,v])=>`<div class="card metric"><small>${k}</small><div class="value">${esc(v)}</div></div>`).join("");$("health-queues").innerHTML=table(["队列","当前占用"],Object.entries(h.queues||{}).map(([k,v])=>[esc(k),esc(v)]));$("health-points").innerHTML=table(["测点","质量","状态"],Object.entries(h.points||{}).map(([k,v])=>[esc(label(k)),badge(v.quality),badge(v.state)]));$("health-storage").innerHTML=`<p>自动清理：${h.storage?.cleanup_enabled?"启用":"已暂停"}；移动盘接续：${h.storage?.external_enabled?"已配置":"未启用（待接入）"}</p><p>最后成功归档：${time(h.history_archive?.last_success_at)}</p><p>历史归档暂停：${h.history_archive?.suspended?"是":"否"}</p>${table(["存储预算项","当前值"],[["本进程累计写入估算",`${number(h.storage_budget?.bytes_written/1048576)} MiB`],["实验库表与索引总占用",`${number(h.storage_budget?.development_growth/1048576)} MiB`],["开发写入／占用上限",`${number(h.storage_budget?.budget_bytes/1048576)} MiB`]])}<details><summary>存储与维护诊断详情</summary><pre>${esc(JSON.stringify(h.storage,null,2))}</pre></details><details><summary>运行与引擎诊断</summary><pre>${esc(JSON.stringify({runtime:h.runtime,engine:h.engine},null,2))}</pre></details>`;$("health-storage").querySelectorAll("details").forEach((d,i)=>d.open=!!openDetails[i]);}
  if(state.communications&&$("source-cards")){const data=state.communications;$("source-cards").innerHTML=(data.sources||[]).map(s=>`<div class="card"><h3>${esc(s.id)} · ${esc(s.kind)} ${badge(s.connected?"CONNECTED":"DISCONNECTED")}</h3><p class="chart-caption">主动读取间隔 ${esc(s.interval)} s · 最后接收 ${time(s.last_received_at)} · ${esc(s.health||"")}</p><div class="scroll">${table(["通道 / 地址","数值","质量 / 接收时间"],(s.points||[]).map(p=>[`${esc(label(p.id))}<br><small>${esc(p.address??p.node_id??"—")} · ${esc(p.data_type||"")}</small>`,`${number(p.value)} ${esc(p.unit)}`,`${badge(p.extreme?"EXTREME":p.quality)}<br><small>${time(p.received_at)}</small>`]))}</div></div>`).join("");$("communication-logs").innerHTML=table(["时间","数据源","级别","信息"],(data.logs||[]).map(l=>[time(l.time),esc(l.source),esc(l.level),esc(l.message)]));}
}
function simulationText(data){
  const selected=(state.config.simulation?.scenarios||[]).find(s=>s.name===data.scenario);
  return `当前场景：${selected?.label||data.scenario||"等待确认"} · 现场更新步数：${data.step??"—"} · ${data.error?"现场故障："+data.error:"模拟现场已响应"}`;
}
async function loadSimulation(){if(!$("scenario-controls"))return;const sim=state.config.simulation||{};$("scenario-controls").innerHTML=sim.enabled?`<div class="controls">${(sim.scenarios||[]).map(s=>`<button data-scenario="${esc(s.name)}" ${state.me.role!=="admin"?"disabled":""}>${esc(s.label||s.name)}</button>`).join("")}</div><p class="chart-caption">${state.me.role==="admin"?"场景修改发生在模拟现场，后端仍通过 Modbus / OPC UA 读取。":"只读账户可观察场景；切换需要管理员。"}</p>`:'<p class="muted">当前未启用模拟现场控制。</p>';$("scenario-controls").querySelectorAll("button").forEach(b=>b.onclick=async()=>{b.disabled=true;try{const result=await api("/api/simulation",{method:"POST",body:JSON.stringify({scenario:b.dataset.scenario})});try{state.health=await api("/api/health");renderArchiveWarning();const reason=archiveStopReason();if($("simulation-status"))$("simulation-status").textContent=simulationText(result)+(reason?` · 归档已停止：${reason}，本次操作不代表追忆已记录。`:" · 等待后端采集与事件归档。");notice(reason?"模拟场景已切换，但归档不可用；不能据此确认追忆已记录。":"");}catch(error){notice(`模拟场景已切换，但无法确认归档状态：${error.message}`);}scheduleEvents();}catch(error){notice(`场景未确认：${error.message}`);}finally{b.disabled=false;}});if(sim.enabled)try{const data=await api("/api/simulation");if($("simulation-status"))$("simulation-status").textContent=simulationText(data);}catch(error){if($("simulation-status"))$("simulation-status").textContent=error.message;}}
async function loadAccounts(){if(state.me.role!=="admin"){$("account-area").innerHTML='<p class="muted">仅管理员可管理账户。</p>';$("audit-log").innerHTML='<p class="muted">仅管理员可查看管理操作记录。</p>';return;}try{const [accounts,audit]=await Promise.all([api("/api/admin/accounts"),api("/api/admin/audit")]);if(!$("account-area"))return;$("account-area").innerHTML=`<div id="new-credential" hidden></div><form id="account-form" class="controls"><input name="username" aria-label="新用户名" placeholder="新用户名" required maxlength="64" pattern="[A-Za-z0-9_-]+"><select name="role" aria-label="账户角色"><option value="viewer">只读用户</option><option value="admin">管理员</option></select><button type="submit">创建账户</button></form>${table(["账户","角色","状态","操作"],accounts.map(a=>[esc(a.username),esc(a.role),a.active?"有效":"已停用",a.active?`<button data-revoke="${esc(a.username)}">停用</button>`:"—"]))}`;$("account-form").onsubmit=async e=>{e.preventDefault();const data=new FormData(e.target);try{const result=await api("/api/admin/accounts",{method:"POST",body:JSON.stringify(Object.fromEntries(data))});await loadAccounts();const box=$("new-credential");box.hidden=false;box.className="credential-result";box.textContent=`仅此一次显示，请保存新凭据：${result.credential}`;}catch(error){notice(error.message);}};$("account-area").querySelectorAll("[data-revoke]").forEach(b=>b.onclick=async()=>{if(!window.confirm(`确认停用账户 ${b.dataset.revoke}？`))return;try{await api(`/api/admin/accounts/${encodeURIComponent(b.dataset.revoke)}`,{method:"DELETE"});await loadAccounts();}catch(error){notice(error.message);}});$("audit-log").innerHTML=table(["时间","操作者","操作","对象","结果"],(audit.items||[]).map(a=>[time(a.time),esc(a.actor),esc(a.action),esc(a.target),esc(a.result)]));}catch(error){notice(error.message);}}
async function refresh(){if(state.stopped||state.refreshing)return;state.refreshing=true;try{const jobs=[api("/api/health"),api("/api/communications")];if(!state.ws||state.ws.readyState!==WebSocket.OPEN)jobs.push(api("/api/current"));const result=await Promise.allSettled(jobs);if(result[0].status==="fulfilled")state.health=result[0].value;if(result[1].status==="fulfilled")state.communications=result[1].value;if(result[2]?.status==="fulfilled")applySnapshot(result[2].value);const failed=result.find(r=>r.status==="rejected");if(failed)notice(`状态读取失败：${failed.reason.message}`);renderDiagnostics();if(state.page===5)scheduleEvents();if(state.page===1&&state.config?.simulation?.enabled){try{const simulation=await api("/api/simulation");if($("simulation-status"))$("simulation-status").textContent=simulationText(simulation);}catch(error){if($("simulation-status"))$("simulation-status").textContent=`模拟现场状态不可用：${error.message}`;}}}finally{state.refreshing=false;}}
function applySnapshot(snapshot){state.frame=snapshot.frame;state.active=snapshot.active_events||[];renderLive();}
function connectStream(){if(state.stopped)return;const ws=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}/api/stream`);state.ws=ws;ws.onopen=()=>ws.send(JSON.stringify({credential:state.credential}));ws.onmessage=event=>{let message;try{message=JSON.parse(event.data);}catch{return;}$("connection").textContent="实时连接";$("connection").className="badge ok";if(message.type==="snapshot"){applySnapshot(message.data);loadHistory();scheduleEvents();}else if(message.type==="frame"){state.frame=message.data.frame;renderLive();}else if(message.type==="event"){const e=message.data;state.active=state.active.filter(x=>x.id!==e.id);if(e.phase!=="CLOSED")state.active.push(e);renderLive();scheduleEvents();}else if(message.type==="fault"||message.type==="trigger"){scheduleEvents();}else if(message.type==="resync_required"){notice("实时消息有缺口，正在重新同步归档与最新状态。");ws.close();}};ws.onclose=event=>{if(state.stopped)return;if(event.code===1008){logout("实时通道认证失败，请重新登录。");return;}$("connection").textContent="重连中 · HTTP 轮询";$("connection").className="badge warn";state.reconnect=setTimeout(connectStream,2000);};ws.onerror=()=>ws.close();}
async function login(){try{state.me=await api("/api/me");state.config=await api("/api/ui-config");state.stopped=false;sessionStorage.setItem("htsg.credential",state.credential);state.station=stations()[0]?.id;state.point=stations()[0]?.strain||definitions()[0]?.id;state.seconds=(state.config.ui?.history_windows||[300]).includes(300)?300:(state.config.ui?.history_windows||[300])[0];$("project-title").textContent=state.config.project?.title||"HTSG 科研监测";document.title=$("project-title").textContent;$("identity").textContent=`${state.me.username} · ${state.me.role==="admin"?"管理员":"只读用户"}`;$("login").hidden=true;$("workspace").hidden=false;$("logout").hidden=false;$("credential").value="";notice("");renderPage();await refresh();connectStream();}catch(error){$("login-error").textContent=`连接失败：${error.message}`;}}
function logout(message=""){state.stopped=true;clearTimeout(state.reconnect);clearTimeout(state.eventsTimer);state.eventsTimer=null;renderArchiveWarning();if(state.ws){state.ws.onclose=null;state.ws.close();state.ws=null;}state.credential="";sessionStorage.removeItem("htsg.credential");state.historyEpoch++;$("login").hidden=false;$("workspace").hidden=true;$("workspace").innerHTML="";$("navigation").innerHTML="";$("logout").hidden=true;$("identity").textContent="未登录";$("connection").textContent="未连接";$("connection").className="badge warn";$("login-error").textContent=message;}
$("login-form").onsubmit=async e=>{e.preventDefault();state.credential=$("credential").value.trim();await login();};$("logout").onclick=()=>logout();
async function loadThermalTrends(){
  if(!$("thermal-trends"))return;
  state.thermalLoading=true;
  const epoch=++state.thermalEpoch, station=selectedStation()||{}, load=(state.config.ui?.process_points||[]).find(id=>id.toLowerCase().includes("load"));
  const seconds=300, end=Date.now()/1000, start=end-seconds, center=end-seconds/2;
  const ids=[station.stress,station.delta_temperature,load].filter(Boolean);
  $("thermal-trends").innerHTML=ids.map((id,i)=>`<div class="card"><h3>${esc(label(id))} · 最近 5 分钟</h3><canvas id="thermal-chart-${i}" class="chart"></canvas><p id="thermal-status-${i}" class="chart-caption">加载同一时间窗的归档趋势…</p></div>`).join("");
  try { await Promise.all(ids.map(async(id,i)=>{try{
    const query=new URLSearchParams({center,seconds,point:id,limit:1000});
    let rows=[],cursor=null,partial=false,more=false;
    do {if(cursor)query.set("cursor",cursor);const result=await api(`/api/replay?${query}`);if(epoch!==state.thermalEpoch||!$("thermal-chart-"+i))return;rows.push(...result.items);partial ||= result.partial;cursor=result.next_cursor;more=result.has_more;}while(cursor&&rows.length<10000);
    plot($("thermal-chart-"+i),rows.map(r=>({t:r.window_start,run:r.run_id,v:r.payload?.value,q:quality(r.payload),extreme:!!r.payload?.extreme})),definition(id).unit,start,end);
    $("thermal-status-"+i).textContent=`${time(start)} → ${time(end)} · ${rows.length} 条${partial?" · 部分数据库不可用":""}${more?" · 达到显示上限":""}`;
  }catch(error){if(epoch===state.thermalEpoch&&$("thermal-status-"+i))$("thermal-status-"+i).textContent=error.message;}})); }
  finally {if(epoch===state.thermalEpoch)state.thermalLoading=false;}
}

setInterval(()=>{refresh();if(!state.stopped){if(state.center===null&&$("trend")&&!state.historyLoading)loadHistory();if(state.page===4&&!state.thermalLoading)loadThermalTrends();}},1000);window.addEventListener("resize",()=>{document.querySelectorAll("canvas").forEach(canvas=>{if(canvas.plotData){const p=canvas.plotData;plot(canvas,p.rows,p.unit,p.start,p.end);}});});if(state.credential)login();
