"""History reconstruction from typed records (终版 §5, §10, §14).  Read-only.

A value counts from its effective time until the EARLIEST of: the next record of that channel, the end of
its segment, the start of an archive gap, and - for a segment still open - the confirmed watermark.  There
is no interpolation and nothing crosses a segment: a window with no change still reports the value in
effect at its start (with the time it counts from), and an unbounded tail is reported as unknown, not guessed.

Rows are the narrow rows of `sample` (修复轮14): channel and run are this database's own numbers, read back through its
`channel` and `run_number` tables, and `state` is read back into a quality or a result state (`records.read_state`).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import psycopg

from .records import read_state

ROW_LIMIT = 10000  # rows of the window per channel, plus the value in effect at its start; reaching it is reported
CHANNEL_LIMIT = 12  # channels one replay may ask for (修复轮03): the fixed template of a fault, never all channels at once
DEFAULT_WINDOW_SECONDS = 3600  # the window a fault brings (修复轮03): half before, half after; set from the measurement
INF = math.inf

SAMPLE_COLUMNS = "s.t,s.value,s.run,s.seg,s.ua_status,s.channel,s.state,s.item"


@dataclass
class StoreRead:
    """What one database contributes to a query."""

    name: str
    segments: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    watermarks: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # inputs and derived results, marked by kind
    truncated: set = field(default_factory=set)
    retention: dict = field(default_factory=lambda: dict(NOTHING_DELETED))  # what the rolling deletion took (§13.6)


RETIRED = ("DELETING", "DELETED")
NOTHING_DELETED = {"retired": [], "possibly_incomplete": [], "deleted_before": None}
BUCKET_ROWS = "SELECT no,state,used,baseline_wall,first_wall,last_wall,data_lo,data_hi FROM bucket ORDER BY no"


def _alive(buckets: list) -> list:
    """Buckets that may hold rows to show: written to here and not being or been deleted.  A prepared bucket never written
    holds nothing, so the emergency database's growing stock of empty buckets is never planned over (修复轮09 第1项)."""
    return [b for b in buckets if b["used"] and b["state"] not in RETIRED]


def _reach(buckets: list, lo: float, hi: float) -> list[int]:
    """The live buckets whose rows can fall in [lo, hi]: their recorded range meets it, or their range is not known."""
    return [b["no"] for b in _alive(buckets) if b["data_lo"] is None or (b["data_hi"] >= lo and b["data_lo"] <= hi)]


def _newest_first(buckets: list, upto: float, floor: float = -INF) -> list:
    """Live buckets that can hold a row at or before `upto` and not before `floor`, the one reaching latest first (a bucket
    whose range is not known first of all): the order a search for the latest row before a time goes through them."""
    found = [b for b in _alive(buckets) if b["data_lo"] is None or (b["data_lo"] <= upto and b["data_hi"] >= floor)]
    return sorted(found, key=lambda b: (INF if b["data_hi"] is None else b["data_hi"], b["no"]), reverse=True)


def _reach_of(rest: list) -> float:
    """The latest time any bucket left in a newest-first search can still hold."""
    return -INF if not rest else INF if rest[0]["data_hi"] is None else rest[0]["data_hi"]


def _retention(conn, buckets: list, start: float, end: float) -> dict:
    """What this database says about the rolling deletion for [start, end] (§13.6 保留边界与查询).  Two statements, never
    merged: the time a bucket taken away covered is `retired` (DELETING: no longer queryable; DELETED: deleted), and inside
    what is kept only the oldest bucket, from its first group to its baseline, is `possibly_incomplete` - as is kept data a
    clock set back put inside a retired range.  `deleted_before`: the boundary, when the window starts before it."""
    retired = [b for b in buckets if b["state"] in RETIRED]
    if not retired:
        return dict(NOTHING_DELETED)
    out = {"retired": [], "possibly_incomplete": [], "deleted_before": None}
    for b in retired:
        if b["first_wall"] is not None and b["last_wall"] >= start and b["first_wall"] <= end:
            out["retired"].append({"bucket": b["no"], "state": b["state"], "from": b["first_wall"], "to": b["last_wall"]})
            reach = _reach(buckets, b["first_wall"], b["last_wall"])  # the range includes the groups' built time
            overlap = conn.execute(
                "SELECT min(built_wall) AS lo,max(built_wall) AS hi FROM commit_group "
                "WHERE built_wall>=%s AND built_wall<=%s AND bucket=ANY(%s)",
                (b["first_wall"], b["last_wall"], reach),
            ).fetchone() if reach else {"lo": None}  # fmt: skip
            if overlap["lo"] is not None:
                out["possibly_incomplete"].append({"from": overlap["lo"], "to": overlap["hi"], "reason": "CLOCK_OVERLAP"})
    kept = [b for b in buckets if b["state"] not in RETIRED and b["used"]]
    if kept:
        oldest = kept[0]
        span = conn.execute(
            "SELECT min(built_wall) AS lo,max(built_wall) AS hi FROM commit_group WHERE bucket=%s", (oldest["no"],)
        ).fetchone()
        lo, hi = span["lo"], oldest["baseline_wall"] if oldest["baseline_wall"] is not None else span["hi"]
        if lo is not None and hi is not None and hi >= start and lo <= end:
            out["possibly_incomplete"].append({"from": lo, "to": hi, "reason": "BEFORE_BASELINE"})
        boundary = oldest["baseline_wall"] if oldest["baseline_wall"] is not None else lo
        if boundary is not None and start < boundary:
            out["deleted_before"] = boundary
    return out


def _rows(conn, sql, params):
    return conn.execute(sql, params).fetchall()


def read_store(store, name, channels: dict[str, str], start: float, end: float, limit: int = ROW_LIMIT) -> StoreRead:
    """Rows of `channels` (point id -> source id) in [start, end] plus the value in effect at `start` of
    every segment that overlaps the window.  `limit` rows per channel (one more is read to detect overflow)."""
    out = StoreRead(name)
    points = list(channels)
    sources = sorted(set(channels.values()))
    with store.session() as conn:
        # one snapshot of the bucket list, the records and their states: a deletion going on cannot hand over half a bucket
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        buckets = _rows(conn, BUCKET_ROWS, ())  # a range being deleted is not shown as data: never named below
        out.retention = _retention(conn, buckets, start, end)
        window_buckets = _reach(buckets, start, end)
        out.segments = _rows(
            conn,
            "SELECT run_id,source_id,seg,conn_gen,start_wall,start_reason,end_wall,end_reason FROM segment "
            "WHERE source_id=ANY(%s) AND coalesce(end_wall,'infinity'::float8)>=%s "
            "AND coalesce(start_wall,'-infinity'::float8)<=%s",
            (sources, start, end),
        )
        out.gaps = _rows(
            conn,
            "SELECT id,run_id,source_id,start_wall,end_wall,reason FROM archive_gap "
            "WHERE source_id=ANY(%s) AND start_wall<=%s AND coalesce(end_wall,'infinity'::float8)>=%s",
            (sources, end, start),
        )
        out.watermarks = _rows(
            conn, "SELECT run_id,source_id,seg,covered_wall FROM watermark WHERE source_id=ANY(%s)", (sources,)
        )
        restated = _restated(conn, buckets, out.segments, start)
        names = _Names(conn, points, out.name)
        window = _rows(
            conn,
            f"SELECT * FROM (SELECT {SAMPLE_COLUMNS},row_number() OVER (PARTITION BY c.point_id "
            f"ORDER BY s.t,s.run,s.seg,s.item) AS rn FROM sample s JOIN channel c ON c.no=s.channel "
            f"WHERE s.channel=ANY(%s) AND s.t>=%s AND s.t<=%s AND s.bucket=ANY(%s)) w WHERE rn<=%s",
            (list(names.channels), start, end, window_buckets, limit + 1),
        ) if window_buckets and names.channels else []  # fmt: skip
        for row in window:
            row = names.read(row)
            if row is None:
                continue
            if row.pop("rn") > limit:
                out.truncated.add(row["point_id"])
            out.rows.append(row)  # the extra row stays: it ends the last one shown
        out.rows.extend(_held_in(conn, buckets, out.segments, names, start, restated))
    return out


class _Names:
    """This database's numbers for the channels asked for and for runs, and a stored row read back in words: its point,
    source and run by name, its state as a quality or a result state, its OPC UA status code unsigned again.  A row whose
    channel or run this database cannot name is left out rather than shown under a wrong name - it cannot happen while
    the rows and their numbers are written in one transaction.  Every row gets an id of this read alone: rows carry
    none in the database, and the databases are merged by joining their rows (a group is written to one database only)."""

    def __init__(self, conn, points, name):
        self.channels = {
            x["no"]: (x["point_id"], x["source_id"])
            for x in conn.execute("SELECT no,point_id,source_id FROM channel WHERE point_id=ANY(%s)", (points,)).fetchall()
        }
        self.runs = {x["no"]: x["run_id"] for x in conn.execute("SELECT no,run_id FROM run_number").fetchall()}
        self.run_no = {run_id: no for no, run_id in self.runs.items()}
        self.channel_no = {key: no for no, key in self.channels.items()}
        self.name, self.count = name, 0

    def read(self, row) -> dict | None:
        channel, run_id = self.channels.get(row["channel"]), self.runs.get(row["run"])
        if channel is None or run_id is None:
            return None
        self.count += 1
        state = read_state(row["state"])
        out = {
            "id": f"{self.name}/{self.count}",
            "point_id": channel[0],
            "source_id": channel[1],
            "run_id": run_id,
            "seg": row["seg"],
            "effective_wall": row["t"],
            "item": row["item"],
            "value": row["value"],
            "status_code": row["ua_status"] & 0xFFFFFFFF if row["ua_status"] else None,
            "quality": state.get("quality"),
            "status": state.get("status"),
            "reason": state.get("reason"),
            "kind": state["kind"],
            "extreme": state["extreme"],
        }
        if "rn" in row:
            out["rn"] = row["rn"]
        return out


def _restated(conn, buckets, segments, start) -> dict:
    """(run, source, seg) -> the time of the latest retention baseline at or before `start` that restated that segment: every
    input and derived result of it that had a value then was written again at that time (§13.6 保留边界基线), so the value in
    effect at `start` is never older than it.  A source the baseline only marked (no valid segment then) is not in it."""
    named = [b["no"] for b in _alive(buckets) if b["baseline_wall"] is not None and b["baseline_wall"] <= start]
    if not named:
        return {}
    row = conn.execute(
        "SELECT run_id,at_wall,detail FROM state_event WHERE kind='RETENTION_BASELINE' AND bucket=%s AND at_wall<=%s "
        "ORDER BY at_wall DESC LIMIT 1",
        (max(named), start),
    ).fetchone()
    if row is None:
        return {}
    wall, marked = row["at_wall"], (row["detail"] or {}).get("sources", {})
    out = {}
    for s in segments:
        if s["run_id"] != row["run_id"] or marked.get(s["source_id"]) != "SEGMENT":
            continue
        begun = [x["seg"] for x in segments if x["run_id"] == s["run_id"] and x["source_id"] == s["source_id"]
                 and (x["start_wall"] is None or x["start_wall"] <= wall)]  # fmt: skip
        if begun and s["seg"] == max(begun) and (s["end_wall"] is None or s["end_wall"] >= wall):  # the one open then
            out[(s["run_id"], s["source_id"], s["seg"])] = wall
    return out


def _held_in(conn, buckets, segments, names, start, restated) -> list:
    """The value in effect at `start` of every (segment, channel): its latest row before `start`, inside the segment.

    The buckets are searched the one reaching latest first, a few at a time and twice as many each round.  A pair is settled
    once its row is later than anything a bucket left can hold, or once no bucket left reaches back to its floor: the start
    of its segment, or the last baseline that restated it (`restated`) - usually inside the window's own bucket.  A pair
    with no row (a channel this source never gave, say) so stops at that baseline too.  Only the pairs this database has
    numbers for are asked: a run or channel it never stored has no row here."""
    rank = lambda row: (row["t"], row["item"])  # noqa: E731 - the ORDER BY below
    pairs = [
        (s["run_id"], s["source_id"], s["seg"], s["start_wall"], point)
        for s in segments
        for (point, source) in names.channel_no
        if source == s["source_id"] and s["run_id"] in names.run_no
    ]
    floors = {
        q: max(-INF if q[3] is None else q[3], restated.get(q[:3], -INF)) for q in pairs
    }  # fmt: skip
    floor = min(floors.values(), default=start)
    order, best, i, size = _newest_first(buckets, start, floor), {}, 0, 1
    while pairs and i < len(order):
        chunk = [b["no"] for b in order[i : i + size]]
        i, size = i + size, size * 2
        for row in _rows(
            conn,
            f"SELECT b.* FROM unnest(%s::int[],%s::int[],%s::int[],%s::float8[]) AS k(run,seg,channel,start_wall) "
            f"CROSS JOIN LATERAL (SELECT {SAMPLE_COLUMNS} FROM sample s WHERE s.channel=k.channel AND s.run=k.run "
            f"AND s.seg=k.seg AND s.t<%s AND s.t>=coalesce(k.start_wall,'-infinity'::float8) AND s.bucket=ANY(%s) "
            f"ORDER BY s.t DESC,s.item DESC LIMIT 1) b",
            ([names.run_no[q[0]] for q in pairs], [q[2] for q in pairs],
             [names.channel_no[(q[4], q[1])] for q in pairs], [q[3] for q in pairs], start, chunk),
        ):  # fmt: skip
            key = (row["run"], row["seg"], row["channel"])
            if key not in best or rank(row) > rank(best[key]):
                best[key] = row
        reach = _reach_of(order[i:])
        pairs = [
            q for q in pairs
            if reach >= floors[q] and not (
                (key := (names.run_no[q[0]], q[2], names.channel_no[(q[4], q[1])])) in best and best[key]["t"] > reach
            )
        ]  # fmt: skip
    return [row for row in map(names.read, best.values()) if row is not None]


def read_segment_rows(store, keys) -> list:
    """Whatever this database stores about the given segments, wherever the window falls."""
    with store.session() as conn:
        return _rows(
            conn,
            "SELECT run_id,source_id,seg,conn_gen,start_wall,start_reason,end_wall,end_reason FROM segment "
            "WHERE (run_id,source_id,seg) IN (SELECT * FROM unnest(%s::text[],%s::text[],%s::int[]))",
            ([k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys]),
        )


def _merge_segments(reads: list[StoreRead]) -> dict:
    """One row per segment from the rows of all databases.  The end is ONE record's: a real end beats a cut made at an
    exit (`EXIT_TRUNCATED`), the latest end wins among either kind, and its reason is the reason of that same record."""
    merged: dict[tuple, dict] = {}
    ends: dict[tuple, list] = {}
    for read in reads:
        for seg in read.segments:
            key = (seg["run_id"], seg["source_id"], seg["seg"])
            cur = merged.setdefault(key, {})
            for field_name, value in seg.items():
                if field_name not in ("end_wall", "end_reason") and value is not None and cur.get(field_name) is None:
                    cur[field_name] = value
            if seg["end_wall"] is not None:
                ends.setdefault(key, []).append((seg["end_wall"], seg["end_reason"]))
    for key, cur in merged.items():
        found = ends.get(key, [])
        real = [end for end in found if end[1] != "EXIT_TRUNCATED"]
        cur["end_wall"], cur["end_reason"] = max(real or found, key=lambda end: end[0], default=(None, None))
    return merged


def _ranking(row: dict) -> tuple:
    """The order the queries rank the rows of a channel in: by time, then by run, segment and order inside the batch."""
    return row["effective_wall"], row["run_id"], row["seg"], row["item"], row["id"]


def _limit_per_channel(rows: dict, start: float, limit: int, truncated: set):
    """The limit is per channel over ALL the databases, not per database: each was asked for `limit`+1 rows of the window, so
    together they may hold up to twice that.  The rows of a channel are those of the union (a group is in one database), in the
    order every database ranks them in; what is left over the limit is not shown and the channel is marked truncated.  The
    first row over the limit is kept, marked `boundary`, to bound the hold of the last row shown (without it that row would
    be held to the end of its segment, as if nothing followed it); `assemble` never shows it.  The value held in from
    before the window (a row ahead of `start`) is not a row of the window and is never cut.  So a channel shows at most
    `limit` rows of the window plus the value in effect at its start."""
    window: dict[str, list] = {}
    for row in rows.values():
        if row["effective_wall"] >= start:
            window.setdefault(row["point_id"], []).append(row)
    for pid, found in window.items():
        if len(found) > limit:
            found.sort(key=_ranking)
            rows[found[limit]["id"]] = {**found[limit], "boundary": True}
            for row in found[limit + 1 :]:
                del rows[row["id"]]
            truncated.add(pid)


def assemble(
    reads: list[StoreRead],
    channels: dict[str, str],
    start: float,
    end: float,
    *,
    unavailable: tuple = (),
    limit: int = ROW_LIMIT,
    query_time: float | None = None,
) -> dict:
    """Merge store reads into per-channel, per-segment value spans."""
    segments = _merge_segments(reads)
    gaps: dict[str, dict] = {}
    marks: dict[tuple, float] = {}
    rows: dict[str, dict] = {}
    truncated: set[str] = set()
    for read in reads:
        for gap in read.gaps:
            gaps.setdefault(gap["id"], gap)
        for mark in read.watermarks:
            key = (mark["run_id"], mark["source_id"], mark["seg"])
            marks[key] = max(marks.get(key, -INF), mark["covered_wall"])
        for row in read.rows:
            rows[row["id"]] = row  # ids are made per read, so no database's row hides another's
        truncated |= read.truncated
    _limit_per_channel(rows, start, limit, truncated)
    by_channel: dict[str, dict[tuple, list]] = {p: {} for p in channels}
    for row in rows.values():
        key = (row["run_id"], row["source_id"], row["seg"])
        ended = segments.get(key, {}).get("end_wall")
        if ended is not None and ended < start and row["effective_wall"] < start:
            continue  # the value in effect at `start` of a segment that was over by then: nothing is held
        by_channel[row["point_id"]].setdefault(key, []).append(row)
    gap_list = sorted(gaps.values(), key=lambda g: (g["start_wall"], g["id"]))
    channels_out = {}
    for pid, per_segment in by_channel.items():
        spans = []
        for key, seg_rows in per_segment.items():
            seg_rows.sort(key=lambda r: (r["effective_wall"], r["item"], r["id"]))  # one batch's rows share a time
            info = segments.get(key, {})
            seg_end, mark = info.get("end_wall"), marks.get(key)
            cut_at_exit = info.get("end_reason") == "EXIT_TRUNCATED"
            out_rows = []
            for i, row in enumerate(seg_rows):
                if row.get("boundary"):
                    continue  # only there to end the row before it; always the last of its segment
                t = row["effective_wall"]
                last = i + 1 == len(seg_rows)
                # a segment end bounds every record; the watermark only vouches for the TAIL of an open
                # segment (a later record of the same segment already proves it went on), and only if it
                # reaches the record at all
                until, why = (seg_end, "SEGMENT_END") if seg_end is not None else (None, None)
                if seg_end is None and last and mark is not None and mark >= t:
                    until, why = mark, "WATERMARK"
                if cut_at_exit and (last or (seg_end is not None and t >= seg_end)):
                    # a cut made at an exit is no end the source reached: the last sample is held as far as the
                    # watermark vouches for it, and without one it holds nothing beyond itself.  A sample AT or after the
                    # cut (the cut is the segment's watermark, so everything after it is unvouched - 细则 5.1 as revised
                    # 2026-10-23) is still shown, last or not, and holds nothing beyond itself, never from before its own time
                    until = max(t, min(seg_end, mark if mark is not None else t))
                    why = "WATERMARK" if mark is not None else "SEGMENT_END"
                if not last and (until is None or seg_rows[i + 1]["effective_wall"] <= until):
                    until, why = seg_rows[i + 1]["effective_wall"], "NEXT_CHANGE"
                for gap in gap_list:
                    if gap["run_id"] == key[0] and gap["source_id"] == key[1] and gap["start_wall"] >= t:
                        if until is None or gap["start_wall"] < until:
                            until, why = gap["start_wall"], "GAP"
                        break
                valid = row["kind"] == "raw" and row["quality"] == "GOOD" and not row["extreme"] or (
                    row["kind"] == "derived" and row["status"] == "OK" and not row["extreme"]
                )
                out_rows.append(
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "time": t,
                        "value": row["value"],
                        "valid": bool(valid),
                        "quality": row.get("quality"),
                        "status_code": row.get("status_code"),
                        "status": row.get("status"),
                        "reason": row.get("reason"),
                        "extreme": row["extreme"],
                        "held": t < start,
                        "until": until,
                        "ended_by": why,
                    }
                )
            if not out_rows:
                continue  # a segment of which only the boundary row was left
            spans.append(
                {
                    "run_id": key[0],
                    "source_id": key[1],
                    "seg": key[2],
                    "start_wall": info.get("start_wall"),
                    "end_wall": seg_end,
                    "end_reason": info.get("end_reason"),
                    "covered_until": mark,
                    "rows": out_rows,
                }
            )
        spans.sort(key=lambda s: (s["start_wall"] if s["start_wall"] is not None else -INF, s["run_id"], s["seg"]))
        channels_out[pid] = {"source_id": channels[pid], "segments": spans, "truncated": pid in truncated}
    result = {
        "start": start,
        "end": end,
        "query_time": time.time() if query_time is None else query_time,
        "channels": channels_out,
        "gaps": gap_list,
        "partial": bool(unavailable),
        "unavailable": list(unavailable),
        "limit": limit,
        "truncated": bool(truncated),
        "sources": sorted(set(channels.values())),
    }
    result["retention"] = {read.name: read.retention for read in reads}  # per database, never merged (§13.6)
    result["coverage"] = "PARTIAL" if unavailable else "UP_TO_COVERAGE_END"
    result["coverage_end"] = coverage_end(result)
    return result


def query(stores, channels: dict[str, str], start: float, end: float, limit: int = ROW_LIMIT, **kw) -> dict:
    """Read every available store; an unreadable one makes the answer partial, never silently complete."""
    read, unavailable = [], []
    for name, store in stores:
        try:
            read.append((store, read_store(store, name, channels, start, end, limit)))
        except psycopg.Error:
            unavailable.append(name)
    # the start and the end of one segment can live in different databases, and the window test above is made
    # per database: a database that missed a segment it holds a part of (its end, say) is asked for it now
    seen = {(s["run_id"], s["source_id"], s["seg"]) for _, one in read for s in one.segments}
    for store, one in read:
        own = {(s["run_id"], s["source_id"], s["seg"]) for s in one.segments}
        if seen - own:
            try:
                one.segments += read_segment_rows(store, sorted(seen - own))
            except psycopg.Error:
                unavailable.append(one.name)
    reads = [one for _, one in read]
    return assemble(reads, channels, start, end, unavailable=tuple(unavailable), limit=limit, **kw)


# ------------------------------------------------------------------ channels, coverage, fault list


def channel_sources(config) -> dict[str, str]:
    """Every queryable channel (inputs and derived) and the source whose segments describe it."""
    out = {p.id: p.source for p in config.points}
    for rule in config.derived:
        first = rule.inputs[0] if rule.inputs else None
        out[rule.id] = out.get(first, config.sources[0].id)
    return out


def replay_template(config, channel: str) -> list[str]:
    """What a replay of a fault on `channel` shows (修复轮03): the fault, its computation chain - what it is computed from,
    what is computed from it or from the same inputs, and the other inputs of those - and the operating quantities, the
    inputs no derived quantity uses.  Only the configuration's own relations: the site has no topology.  A quantity of more
    than two inputs (the thermal stress peak) is not followed.  Fault first, then its own inputs as declared, then the rest of
    the chain and the operating quantities in configuration order; at most CHANNEL_LIMIT."""
    small = {d.id: list(d.inputs) for d in config.derived if len(d.inputs) <= 2}
    own = set(small.get(channel, []))
    chain = set(own)
    for derived, inputs in small.items():
        if channel in inputs or own & set(inputs):
            chain |= {derived, *inputs}
    chain.discard(channel)
    used = {i for d in config.derived for i in d.inputs}
    order = [p.id for p in config.points] + [d.id for d in config.derived]
    operating = [p.id for p in config.points if p.id not in used and p.id != channel]
    rest = [c for c in order if c in chain and c not in own]
    return ([channel] + small.get(channel, []) + rest + operating)[:CHANNEL_LIMIT]


def coverage_end(result: dict) -> dict:
    """Per source: how far the answer is certain (segment end or confirmed watermark), None when unknown.

    A source whose database could not be read is reported unknown: a later sample found elsewhere is not
    a complete watermark for everything before it.
    """
    ends: dict[str, float | None] = {}
    for channel in result["channels"].values():
        source = channel["source_id"]
        best = None
        for span in channel["segments"]:
            if span["end_reason"] == "EXIT_TRUNCATED":
                certain = span["covered_until"]  # a cut made at an exit: only the watermark is certain
            else:
                certain = span["end_wall"] if span["end_wall"] is not None else span["covered_until"]
            if certain is not None:
                best = certain if best is None else max(best, certain)
        ends[source] = best if source not in ends or ends[source] is None else max(ends[source], best or 0)
    if result["partial"]:
        return {source: None for source in ends}
    return ends


def encode_cursor(key: list, identity: str) -> str:
    from .pagination import encode

    return encode(key, identity)


def decode_cursor(token: str | None, identity: str, size: int) -> list | None:
    if token is None:
        return None
    import base64
    import json

    try:
        version, saved, key = json.loads(base64.urlsafe_b64decode(token))
        if version != 1 or saved != identity or not isinstance(key, list) or len(key) != size:
            raise ValueError()
        return key
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("翻页位置无效或不属于这次查询，请重新查询") from error


FAULT_COLUMNS = (
    "id,run_id,source_id,seg,batch_seq,point_id,direction,confirmed_wall,confirmed_mono,basis_kind,basis_id,"
    "basis_wall,basis_value,config_id,rule"
)


def list_faults(
    stores, *, point=None, direction=None, source=None, start=None, end=None, limit=100, cursor=None
) -> dict:
    """Committed fault records across all readable databases, newest first, stable keyset paging.

    The list shows only what is really stored; buffered or rejected faults never appear here.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("每页条数须在 1 到 1000 之间")
    import hashlib
    import json

    identity = hashlib.sha256(
        json.dumps([point, direction, source, start, end], sort_keys=True).encode()
    ).hexdigest()
    after = decode_cursor(cursor, identity, 2)
    where, params = ["true"], []
    for column, value in (("point_id", point), ("direction", direction), ("source_id", source)):
        if value is not None:
            where.append(f"{column}=%s")
            params.append(value)
    if start is not None:
        where.append("confirmed_wall>=%s")
        params.append(start)
    if end is not None:
        where.append("confirmed_wall<%s")
        params.append(end)
    if after is not None:
        where.append("(confirmed_wall,id)<(%s,%s)")
        params += list(after)
    sql = (
        f"SELECT {FAULT_COLUMNS} FROM fault_record WHERE {' AND '.join(where)} "
        "ORDER BY confirmed_wall DESC,id DESC LIMIT %s"
    )
    rows, unavailable = {}, []
    for name, store in stores:
        try:
            with store.session() as conn:
                for row in conn.execute(sql, [*params, limit + 1]).fetchall():
                    rows.setdefault(row["id"], {**row, "store": name})
        except psycopg.Error:
            unavailable.append(name)
    ordered = sorted(rows.values(), key=lambda r: (r["confirmed_wall"], r["id"]), reverse=True)
    page = ordered[:limit]
    more = len(ordered) > limit
    next_cursor = encode_cursor([page[-1]["confirmed_wall"], page[-1]["id"]], identity) if page and more else None
    return {
        "items": page,
        "has_more": more,
        "next_cursor": next_cursor,
        "partial": bool(unavailable),
        "unavailable": unavailable,
        "pagination_incomplete": bool(unavailable),
        "limit": limit,
    }
