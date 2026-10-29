"use strict";
// How replay records read and what the CSV of the shown result contains (终版 §14).  No DOM, no network: the page
// and the tests share this file, and the table on screen and the CSV are made from the very same rows.

export const REASONS = {
  SOURCE_WAITING: "等待首次数据", SOURCE_EXPIRED: "数据已失效", SOURCE_RESYNC: "重新同步中",
  QUALITY: "质量不良", EXTREME: "超出物理极值", NO_VALUE: "无数值", UNKNOWN: "尚无数据",
  POLL_MISSING: "本轮未应答", POLL_TIMEOUT: "本轮超时", POLL_EXCEPTION: "本轮协议异常", POLL_CONNECTION: "本轮连接失败",
  SOURCE_INVALID: "来源数据无效", INPUT_UNAVAILABLE: "输入不可用", BASELINE_ONLY: "等待连续两轮",
  NOT_A_PLANNED_ROUND: "非计划轮次", EXTREME_INPUT: "输入超出物理极值", NONFINITE: "结果非有限数",
};
export const reasonText = code => REASONS[code] || code || "无效";
export const ORIGINS = {RECEIVED: "实收", STATE_ASSERTION: "状态重申", RETENTION_BASELINE: "保留边界基线"};
const DATABASES = {primary: "正常库", emergency: "应急库"};

// What the rolling deletion took from the window, per database (终版 §13.6, §14): deleted and possibly incomplete stay apart.
export function retentionNotes(result, stamp) {
  const notes = [];
  for (const [db, k] of Object.entries(result.retention || {})) {
    const name = DATABASES[db] || db;
    if (k.deleted_before != null) notes.push(`${name}：${stamp(k.deleted_before)} 之前的数据已滚动删除`);
    for (const r of k.retired) notes.push(`${name}：${stamp(r.from)} → ${stamp(r.to)} ${r.state === "DELETING" ? "正在淘汰，已不可查询" : "已滚动删除"}`);
    for (const p of k.possibly_incomplete) notes.push(`${name}：${stamp(p.from)} → ${stamp(p.to)} 可能不完整（${p.reason === "CLOCK_OVERLAP" ? "墙钟回拨，与已删除的时段重叠" : "最旧保留桶在保留边界基线之前的部分"}）`);
  }
  return notes;
}
export const ENDS = {NEXT_CHANGE: "至下一次变化", WATERMARK: "至已确认水位", SEGMENT_END: "至有效段结束", GAP: "至归档缺口起点"};

export const channelRows = channel => channel.segments.flatMap(s => s.rows.map(row => ({...row, run: s.run_id, seg: s.seg})));
export function rowState(row) {
  const code = row.status_code ? ` 0x${(row.status_code >>> 0).toString(16).toUpperCase()}` : "";
  return row.kind === "raw"
    ? `${row.quality}${code}${row.extreme ? " 极值" : ""}`
    : `${row.status}${row.reason ? ` · ${reasonText(row.reason)}` : ""}${row.extreme ? " 极值" : ""}`;
}
// A record next to an archive gap says so: the last one before it, and the restatement that opens the segment after.
export function gapMark(row, channel, result) {
  if (row.ended_by === "GAP") return "终于缺口";
  const after = result.gaps.some(g => g.source_id === channel.source_id && g.end_wall === row.time);
  return after && row.origin === "STATE_ASSERTION" ? "缺口后重申" : "";
}

// Local time with its UTC offset, e.g. 1970-01-01T08:17:09.000+08:00: unambiguous wherever the file is opened.
export function isoLocal(seconds) {
  const d = new Date(seconds * 1000), offset = -d.getTimezoneOffset(), a = Math.abs(offset);
  const p = (n, len = 2) => String(n).padStart(len, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}${offset >= 0 ? "+" : "-"}${p(Math.floor(a / 60))}:${p(a % 60)}`;
}

// RFC 4180 quoting; numbers stay numbers, and text that a spreadsheet could take for a formula is made inert.
function cell(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  if (typeof value === "boolean") return value ? "是" : "否";
  const text = /^[=+\-@\t\r]/.test(String(value)) ? `'${value}` : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}
const line = cells => cells.map(cell).join(",");

// The shown result, as text (the caller adds the byte-order mark).  `request.chart` is the channel on screen.
export function buildCsv({result, request, fault, timezone, label, unit}) {
  const focus = request.chart, channel = result.channels[focus], rows = channelRows(channel);
  const limited = Object.entries(result.channels).filter(([, c]) => c.truncated).map(([id]) => id);
  const meta = [
    ["内容", "回溯记录：导出的是当前显示的结果，没有重新查询"],
    ["查询时刻", isoLocal(result.query_time)], ["查询时刻(UTC秒)", result.query_time],
    ["时区", `${timezone} (UTC${isoLocal(result.center).slice(-6)})`],
    ["中心时间", isoLocal(result.center)], ["中心时间(UTC秒)", result.center],
    ["窗口起", isoLocal(result.start)], ["窗口止", isoLocal(result.end)], ["窗口长度(秒)", result.seconds],
    ["参考故障", fault ? `${fault.id} ${fault.point_id} ${isoLocal(fault.confirmed_wall)} ${fault.direction}` : "无"],
    ["查询通道", request.points.join(" ")], ["导出通道", focus], ["通道名称", label(focus)], ["单位", unit(focus)],
    ["每通道行数上限", result.limit], ["是否达到行数上限", limited.length > 0], ["达到上限的通道", limited.join(" ")],
    ["覆盖终点", Number.isFinite(result.coverage_complete_until) ? isoLocal(result.coverage_complete_until) : "未知"],
    ["不可读的数据库", result.unavailable.join(" ") || "无"],
    ...retentionNotes(result, isoLocal).map(note => ["滚动删除", note]),
    ...result.gaps.map(g => ["归档缺口", `${g.source_id} ${isoLocal(g.start_wall)} → ${Number.isFinite(g.end_wall) ? isoLocal(g.end_wall) : "未恢复"} ${g.reason}`]),
    ["保持值", "“保持值”为是的行是窗口起点处仍有效的值，保留其原观测时间与来源，不是窗口起点的新采样"],
  ];
  const head = ["通道", "种类", "生效时间", "生效时间(UTC秒)", "原观测时间", "原观测时间(UTC秒)", "批次号", "批内序号", "值", "是否有效", "质量/状态", "来源", "保持值", "有效至", "终止原因", "有效段", "缺口标记"];
  const records = rows.map(r => [
    focus, r.kind === "raw" ? "原始" : "派生", isoLocal(r.time), r.time, isoLocal(r.observed_at), r.observed_at,
    r.batch_seq, r.item_seq, r.valid ? r.value : null, r.valid, rowState(r), ORIGINS[r.origin] || r.origin, r.held,
    r.until == null ? "" : isoLocal(r.until), r.until == null ? "其后未知" : ENDS[r.ended_by] || "", `${r.run}/${r.seg}`, gapMark(r, channel, result),
  ]);
  return [...meta.map(line), "", line(head), ...records.map(line)].join("\r\n") + "\r\n";
}
