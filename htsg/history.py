"""History reconstruction from typed records (终版 §5, §10, §14).  Read-only.

A value counts from its effective time until the EARLIEST of: the next record of that channel, the end of
its segment, the start of an archive gap, and - for a segment still open - the confirmed watermark.  There
is no interpolation and nothing crosses a segment: a window with no change still reports the value in
effect at its start (with its ORIGINAL time), and an unbounded tail is reported as unknown, not guessed.
"""

from __future__ import annotations

import math
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

import psycopg

from .records import MISSING

ROW_LIMIT = 10000  # rows of the window per channel, plus the value in effect at its start; reaching it is reported
CHANNEL_LIMIT = 12  # channels one replay may ask for (修复轮03): the fixed template of a fault, never all channels at once
DEFAULT_WINDOW_SECONDS = 3600  # the window a fault brings (修复轮03): half before, half after; set from the measurement
INF = math.inf

OBS_COLUMNS = (
    "id,run_id,source_id,conn_gen,seg,batch_seq,item_seq,point_id,value,quality,status_code,received_wall,"
    "effective_wall,origin,orig_id,asserted_wall,extreme,tick,poll_id,response_id,bucket"
)
RES_COLUMNS = (
    "id,run_id,source_id,conn_gen,seg,batch_seq,point_id,value,status,reason,extreme,config_id,at_wall,"
    "effective_wall,basis,estimate,origin,orig_id,asserted_wall,basis_incomplete,bucket"
)


@dataclass
class StoreRead:
    """What one database contributes to a query."""

    name: str
    segments: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    watermarks: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # raw observations and derived results, marked by kind
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
        for kind, table, columns, time_col in (
            ("raw", "observation", OBS_COLUMNS, "effective_wall"),
            ("derived", "result", RES_COLUMNS, "effective_wall"),
        ):
            window = _rows(
                conn,
                f"SELECT * FROM (SELECT {columns},row_number() OVER (PARTITION BY point_id "
                f"ORDER BY {time_col},run_id,seg,batch_seq,{'item_seq' if kind == 'raw' else 'id'}) AS rn "
                f"FROM {table} WHERE point_id=ANY(%s) AND {time_col}>=%s AND {time_col}<=%s AND bucket=ANY(%s)) t "
                f"WHERE rn<=%s",
                (points, start, end, window_buckets, limit + 1),
            ) if window_buckets else []  # fmt: skip
            for row in window:
                if row.pop("rn") > limit:
                    out.truncated.add(row["point_id"])
                out.rows.append({**row, "kind": kind})  # the extra row stays: it ends the last one shown
            held = _held_in(conn, buckets, out.segments, points, start, kind, restated)
            out.rows.extend({**row, "kind": kind} for row in held)
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


def _held_in(conn, buckets, segments, points, start, kind, restated) -> list:
    """The value in effect at `start` of every (segment, channel): its latest row before `start`, inside the segment.

    The buckets are searched the one reaching latest first, a few at a time and twice as many each round.  A pair is settled
    once its row is later than anything a bucket left can hold, or once no bucket left reaches back to its floor: the start
    of its segment, or the last baseline that restated it (`restated`) - usually inside the window's own bucket.  A pair
    with no row (a raw channel asked of the results, say) so stops at that baseline too."""
    table, columns, tie = ("observation", OBS_COLUMNS, ",o.item_seq DESC") if kind == "raw" else ("result", RES_COLUMNS, "")
    rank = lambda row: (row["effective_wall"], row["batch_seq"], row.get("item_seq") or 0)  # noqa: E731 - the ORDER BY below
    pairs = [(s["run_id"], s["source_id"], s["seg"], s["start_wall"], p) for s in segments for p in points]
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
            f"SELECT b.* FROM unnest(%s::text[],%s::text[],%s::int[],%s::float8[],%s::text[]) "
            f"AS k(run_id,source_id,seg,start_wall,point_id) "
            f"CROSS JOIN LATERAL (SELECT {columns} FROM {table} o WHERE o.point_id=k.point_id "
            f"AND o.run_id=k.run_id AND o.source_id=k.source_id AND o.seg=k.seg "
            f"AND o.effective_wall<%s AND o.effective_wall>=coalesce(k.start_wall,'-infinity'::float8) AND o.bucket=ANY(%s) "
            f"ORDER BY o.effective_wall DESC,o.batch_seq DESC{tie} LIMIT 1) b",
            ([q[0] for q in pairs], [q[1] for q in pairs], [q[2] for q in pairs], [q[3] for q in pairs],
             [q[4] for q in pairs], start, chunk),
        ):  # fmt: skip
            key = (row["run_id"], row["source_id"], row["seg"], row["point_id"])
            if key not in best or rank(row) > rank(best[key]):
                best[key] = row
        reach = _reach_of(order[i:])
        pairs = [
            q for q in pairs
            if reach >= floors[q] and not (q[:3] + q[4:] in best and best[q[:3] + q[4:]]["effective_wall"] > reach)
        ]  # fmt: skip
    return list(best.values())


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
    """The order the queries rank the rows of a channel in: by effective time, then by identity (a result by its id)."""
    last = row["item_seq"] if row["kind"] == "raw" else row["id"]
    return row["effective_wall"], row["run_id"], row["seg"], row["batch_seq"], last


def _limit_per_channel(rows: dict, start: float, limit: int, truncated: set):
    """The limit is per channel over ALL the databases, not per database: each was asked for `limit`+1 rows of the window, so
    together they may hold up to twice that.  The rows of a channel are those of the union, once each (by stable id), in the
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
            rows.setdefault(row["id"], row)
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
            seg_rows.sort(key=lambda r: (r["batch_seq"], r.get("item_seq", 0), r["id"]))
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
                        "bucket": row.get("bucket"),  # where its basis is asked for (修复轮09 第1项)
                        "kind": row["kind"],
                        "time": t,
                        "observed_at": row["received_wall"] if row["kind"] == "raw" else row["at_wall"],
                        "origin": row["origin"],
                        "orig_id": row["orig_id"],
                        "value": row["value"],
                        "valid": bool(valid),
                        "quality": row.get("quality"),
                        "status_code": row.get("status_code"),
                        "status": row.get("status"),
                        "reason": row.get("reason"),
                        "extreme": row["extreme"],
                        "batch_seq": row["batch_seq"],
                        "item_seq": row.get("item_seq"),
                        "basis_incomplete": row.get("basis_incomplete", False),
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


# --------------------------------------------------------------------------- input basis (§10)

BASIS_FIELDS = (
    "id,point_id,value,quality,status_code,extreme,origin,orig_id,run_id,source_id,seg,batch_seq,item_seq,"
    "received_wall,effective_wall,tick"
)


HIDDEN = object()  # in the parameters of a statement: the buckets this database hides (being deleted, deleted)


@contextmanager
def _snapshots(stores):
    """Every database read for one lookup in ONE repeatable-read, read-only transaction, with the buckets it hides - the
    same isolation as the history page (§13.5): a deletion going on cannot hand the basis half a bucket, and what the page
    no longer shows is not given here either.  Yields the open reads and the names of databases that could not be read."""
    reads, down, stack = [], [], ExitStack()
    try:
        for name, store in stores:
            try:
                conn = stack.enter_context(store.session())
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                buckets = conn.execute(BUCKET_ROWS).fetchall()
            except psycopg.Error:
                down.append(name)
                continue
            reads.append((name, conn, [b["no"] for b in buckets if b["state"] in RETIRED], buckets))
        yield reads, down
    finally:
        try:
            stack.close()
        except psycopg.Error:  # read only: what was read stands; the session has closed the connection that broke
            pass


def _bind(params, hidden):
    return tuple(hidden if p is HIDDEN else p for p in params)


def _first(reads, down, sql, params):
    """First row any database still readable has, and its name; a database that fails is added to `down`."""
    for name, conn, hidden, _ in reads:
        if name in down:
            continue
        try:
            row = conn.execute(sql, _bind(params, hidden)).fetchone()
        except psycopg.Error:
            down.append(name)
            continue
        if row is not None:
            return name, row
    return None, None


def _all(reads, down, sql, params):
    seen = {}
    for name, conn, hidden, _ in reads:
        if name in down:
            continue
        try:
            for row in conn.execute(sql, _bind(params, hidden)).fetchall():
                seen.setdefault(row["id"], row)
        except psycopg.Error:
            down.append(name)
    return list(seen.values())


CHUNK = object()  # in the parameters of a searched statement: the buckets of this round of the search


def _search(reads, down, sql, params, order_of, settled):
    """Per database, `sql` over its buckets in the order `order_of(buckets)` gives, a few at a time and twice as many each
    round, until `settled(rows found, buckets left)`; the rows of every database, one per id.  The statement names only
    the buckets of its round, so it is planned and locked over those alone (修复轮09 第1项)."""
    seen = {}
    for name, conn, _, buckets in reads:
        if name in down:
            continue
        order, found, i, size = order_of(buckets), [], 0, 1
        try:
            while i < len(order) and not settled(found, order[i:]):
                chunk = [b["no"] for b in order[i : i + size]]
                i, size = i + size, size * 2
                found += conn.execute(sql, tuple(chunk if p is CHUNK else p for p in params)).fetchall()
        except psycopg.Error:
            down.append(name)
            continue
        for row in found:
            seen.setdefault(row["id"], row)
    return list(seen.values())


def _down_from(bucket: int, floor: float):
    """Live buckets numbered `bucket` or lower that can hold a row from `floor` on, highest first.  A run's groups get
    bucket numbers that never go down, so an earlier batch of the same run is in this bucket or a lower one."""
    return lambda buckets: sorted(
        (b for b in _alive(buckets) if b["no"] <= bucket and (b["data_hi"] is None or b["data_hi"] >= floor)),
        key=lambda b: b["no"], reverse=True,
    )  # fmt: skip


def _source_inputs(derived: dict, name: str, points: dict) -> list[str]:
    """Raw point ids a derived channel ultimately rests on, in rule order."""
    out = []
    for item in derived[name]["inputs"] if name in derived else [name]:
        for pid in [item] if item in points else _source_inputs(derived, item, points):
            if pid not in out:
                out.append(pid)
    return out


def input_basis(stores, result_id: str, bucket: int | None = None) -> dict:
    """Which stored observations a derived result was computed from, restored the way §10 prescribes.

    OPC UA: the state at the END of the triggering batch within the same segment, plus every version in
    that batch in batch order.  Modbus: only that round's responses (strain_change: also the previous
    planned round).  Nothing is borrowed from other rounds or segments; what cannot be found is reported.
    """
    with _snapshots(stores) as (reads, down):
        return _input_basis(reads, down, result_id, bucket)


def _input_basis(reads, down, result_id: str, bucket: int | None = None) -> dict:
    """`bucket`: where the history row of the result said it is - asked first; a result not there is looked for everywhere."""
    result = None
    if bucket is not None:
        name, result = _first(
            reads, down, "SELECT * FROM result WHERE id=%s AND bucket=%s AND bucket<>ALL(%s)", (result_id, bucket, HIDDEN)
        )
    if result is None:
        name, result = _first(reads, down, "SELECT * FROM result WHERE id=%s AND bucket<>ALL(%s)", (result_id, HIDDEN))
    if result is None:
        return {"found": False, "unavailable": down}
    _, config = _first(reads, down, "SELECT payload FROM config_version WHERE id=%s", (result["config_id"],))
    payload = config["payload"] if config else None
    out = {
        "found": True,
        "result": {k: result[k] for k in ("id", "point_id", "value", "status", "reason", "origin", "orig_id",
                                          "run_id", "source_id", "seg", "batch_seq", "basis", "basis_incomplete")},
        "config_id": result["config_id"],
        "unavailable": down,
        "inputs": {},
        "missing": [],
        "notes": [],
    }  # fmt: skip
    if payload is None:
        out.update(complete=False, notes=["CONFIGURATION_VERSION_NOT_FOUND"])
        return out
    derived = {d["id"]: d for d in payload["derived"]}
    points = {p["id"]: p for p in payload["points"]}
    kinds = {s["id"]: s["kind"] for s in payload["sources"]}
    rule = derived[result["point_id"]]
    out["rule"] = {"operation": rule["operation"], "inputs": rule["inputs"], "model_id": rule.get("model_id")}
    previous_batch = (result["basis"] or {}).get("previous_batch_seq")
    _, seg = _first(reads, down, "SELECT start_wall FROM segment WHERE run_id=%s AND source_id=%s AND seg=%s",
                    (result["run_id"], result["source_id"], result["seg"]))  # fmt: skip
    _, run = _first(reads, down, "SELECT started_wall FROM run WHERE run_id=%s", (result["run_id"],))
    seg_floor = -INF if seg is None or seg["start_wall"] is None else seg["start_wall"]
    run_floor = -INF if run is None else run["started_wall"]
    for pid in _source_inputs(derived, result["point_id"], points):
        source = points[pid]["source"]
        entry = {"source_id": source, "mode": None, "rows": [], "batch_end": None}
        if source != result["source_id"]:
            entry["mode"] = "CROSS_SOURCE_LATEST"  # no cross-source guarantee; shown for orientation only
            rows = _search(
                reads,
                down,
                f"SELECT {BASIS_FIELDS} FROM observation WHERE point_id=%s AND run_id=%s AND effective_wall<=%s "
                "AND bucket=ANY(%s) ORDER BY effective_wall DESC,batch_seq DESC,item_seq DESC LIMIT 1",
                (pid, result["run_id"], result["at_wall"], CHUNK),
                lambda buckets: _newest_first(buckets, result["at_wall"], run_floor),
                lambda found, rest: bool(found) and max(r["effective_wall"] for r in found) > _reach_of(rest),
            )
            rows = [max(rows, key=lambda r: (r["effective_wall"], r["batch_seq"], r["item_seq"]))] if rows else []
        elif kinds[source] == "modbus":
            entry["mode"] = "THIS_ROUND"
            wanted = [result["batch_seq"]] + ([previous_batch] if previous_batch else [])
            rows = _search(
                reads,
                down,
                f"SELECT {BASIS_FIELDS} FROM observation WHERE point_id=%s AND run_id=%s AND source_id=%s "
                "AND batch_seq=ANY(%s) AND bucket=ANY(%s) ORDER BY batch_seq,item_seq",
                (pid, result["run_id"], source, wanted, CHUNK),
                _down_from(result["bucket"], run_floor),
                lambda found, rest: set(wanted) <= {r["batch_seq"] for r in found},
            )
            rows = sorted(rows, key=lambda r: (r["batch_seq"], r["item_seq"]))
            if previous_batch:
                out["previous_round"] = {"batch_seq": previous_batch, "tick": (result["basis"] or {}).get("previous_tick")}
        else:
            entry["mode"] = "BATCH_END_STATE"
            in_batch = _search(
                reads,
                down,
                f"SELECT {BASIS_FIELDS} FROM observation WHERE point_id=%s AND run_id=%s AND source_id=%s "
                "AND seg=%s AND batch_seq=%s AND bucket=ANY(%s) ORDER BY item_seq",
                (pid, result["run_id"], source, result["seg"], result["batch_seq"], CHUNK),
                _down_from(result["bucket"], seg_floor),
                lambda found, rest: bool(found),  # a batch is one group: all its rows are in one bucket
            )
            if in_batch:
                rows = sorted(in_batch, key=lambda r: r["item_seq"])
            else:
                rows = _search(
                    reads,
                    down,
                    f"SELECT {BASIS_FIELDS} FROM observation WHERE point_id=%s AND run_id=%s AND source_id=%s "
                    "AND seg=%s AND batch_seq<%s AND bucket=ANY(%s) ORDER BY batch_seq DESC,item_seq DESC LIMIT 1",
                    (pid, result["run_id"], source, result["seg"], result["batch_seq"], CHUNK),
                    _down_from(result["bucket"], seg_floor),
                    lambda found, rest: bool(found),  # higher buckets hold later batches: the first found is the latest
                )
                rows = [max(rows, key=lambda r: (r["batch_seq"], r["item_seq"]))] if rows else []
        entry["rows"] = [dict(r) for r in rows]
        current = [r for r in entry["rows"] if r["batch_seq"] == result["batch_seq"] or entry["mode"] != "THIS_ROUND"]
        entry["batch_end"] = current[-1]["id"] if current else None
        this_round = [r for r in entry["rows"] if r["batch_seq"] == result["batch_seq"]] if entry["mode"] == "THIS_ROUND" else []
        if not entry["rows"] or (this_round and all((r["quality"] or "").startswith(MISSING) for r in this_round)):
            out["missing"].append(pid)  # nothing was found - or this round gave the input only a failure record
        for r in entry["rows"]:
            if r["origin"] in ("STATE_ASSERTION", "RETENTION_BASELINE"):
                original = _search(  # received no later than the restatement says, filed no later than it
                    reads, down, "SELECT id FROM observation WHERE id=%s AND bucket=ANY(%s)", (r["orig_id"], CHUNK),
                    lambda buckets, r=r: _newest_first(buckets, r["effective_wall"], r["received_wall"]),
                    lambda found, rest: bool(found),
                )
                if not original:
                    out["notes"].append(f"ORIGINAL_OBSERVATION_LOST:{r['orig_id']}")
        out["inputs"][pid] = entry
    if out["missing"]:  # 依据已裁剪 (§10.1): a deleted range that began by then may have held what is missing
        _, pruned = _first(
            reads, down, "SELECT 1 AS found FROM bucket WHERE state=ANY(%s) AND first_wall<=%s LIMIT 1", (list(RETIRED), result["at_wall"])
        )
        if pruned is not None:
            out["notes"].append("BASIS_PRUNED")
    if result["basis_incomplete"]:
        out["notes"].append("INPUTS_LOST_IN_ARCHIVE_GAP")
    if result["origin"] == "STATE_ASSERTION":
        out["notes"].append("RESTATED_RESULT")
    if result["origin"] == "RETENTION_BASELINE":
        out["notes"].append("RETENTION_BASELINE_RESULT")
    out["complete"] = not out["missing"] and not result["basis_incomplete"] and not any(
        n.startswith("ORIGINAL_OBSERVATION_LOST") for n in out["notes"]
    )
    return out


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
