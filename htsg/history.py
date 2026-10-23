"""History reconstruction from typed records (终版 §5, §10, §14).  Read-only.

A value counts from its effective time until the EARLIEST of: the next record of that channel, the end of
its segment, the start of an archive gap, and - for a segment still open - the confirmed watermark.  There
is no interpolation and nothing crosses a segment: a window with no change still reports the value in
effect at its start (with its ORIGINAL time), and an unbounded tail is reported as unknown, not guessed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import psycopg

from .records import MISSING

ROW_LIMIT = 10000  # rows of the window per channel, plus the value in effect at its start; reaching it is reported
INF = math.inf

OBS_COLUMNS = (
    "id,run_id,source_id,conn_gen,seg,batch_seq,item_seq,point_id,value,quality,status_code,received_wall,"
    "effective_wall,origin,orig_id,asserted_wall,extreme,tick,poll_id,response_id"
)
RES_COLUMNS = (
    "id,run_id,source_id,conn_gen,seg,batch_seq,point_id,value,status,reason,extreme,config_id,at_wall,"
    "effective_wall,basis,estimate,origin,orig_id,asserted_wall,basis_incomplete"
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


def _rows(conn, sql, params):
    return conn.execute(sql, params).fetchall()


def read_store(store, name, channels: dict[str, str], start: float, end: float, limit: int = ROW_LIMIT) -> StoreRead:
    """Rows of `channels` (point id -> source id) in [start, end] plus the value in effect at `start` of
    every segment that overlaps the window.  `limit` rows per channel (one more is read to detect overflow)."""
    out = StoreRead(name)
    points = list(channels)
    sources = sorted(set(channels.values()))
    with store.session() as conn:
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
        for kind, table, columns, time_col in (
            ("raw", "observation", OBS_COLUMNS, "effective_wall"),
            ("derived", "result", RES_COLUMNS, "effective_wall"),
        ):
            window = _rows(
                conn,
                f"SELECT * FROM (SELECT {columns},row_number() OVER (PARTITION BY point_id "
                f"ORDER BY {time_col},run_id,seg,batch_seq,{'item_seq' if kind == 'raw' else 'id'}) AS rn "
                f"FROM {table} WHERE point_id=ANY(%s) AND {time_col}>=%s AND {time_col}<=%s) t WHERE rn<=%s",
                (points, start, end, limit + 1),
            )
            for row in window:
                if row.pop("rn") > limit:
                    out.truncated.add(row["point_id"])
                out.rows.append({**row, "kind": kind})  # the extra row stays: it ends the last one shown
            keys = [(s["run_id"], s["source_id"], s["seg"], s["start_wall"]) for s in out.segments]
            if keys:
                basis = _rows(
                    conn,
                    f"SELECT b.* FROM unnest(%s::text[],%s::text[],%s::int[],%s::float8[]) "
                    f"AS k(run_id,source_id,seg,start_wall) CROSS JOIN unnest(%s::text[]) AS p(point_id) "
                    f"CROSS JOIN LATERAL (SELECT {columns} FROM {table} o WHERE o.point_id=p.point_id "
                    f"AND o.run_id=k.run_id AND o.source_id=k.source_id AND o.seg=k.seg "
                    f"AND o.{time_col}<%s AND o.{time_col}>=coalesce(k.start_wall,'-infinity'::float8) "
                    f"ORDER BY o.{time_col} DESC,o.batch_seq DESC"
                    f"{',o.item_seq DESC' if kind == 'raw' else ''} LIMIT 1) b",
                    (
                        [k[0] for k in keys],
                        [k[1] for k in keys],
                        [k[2] for k in keys],
                        [k[3] for k in keys],
                        points,
                        start,
                    ),
                )
                out.rows.extend({**row, "kind": kind} for row in basis)
    return out


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


def _first(stores, sql, params):
    """First row any available store has, and the names of stores that could not be read."""
    down = []
    for name, store in stores:
        try:
            with store.session() as conn:
                row = conn.execute(sql, params).fetchone()
        except psycopg.Error:
            down.append(name)
            continue
        if row is not None:
            return name, row, down
    return None, None, down


def _all(stores, sql, params):
    seen, down = {}, []
    for name, store in stores:
        try:
            with store.session() as conn:
                for row in conn.execute(sql, params).fetchall():
                    seen.setdefault(row["id"], row)
        except psycopg.Error:
            down.append(name)
    return list(seen.values()), down


def _source_inputs(derived: dict, name: str, points: dict) -> list[str]:
    """Raw point ids a derived channel ultimately rests on, in rule order."""
    out = []
    for item in derived[name]["inputs"] if name in derived else [name]:
        for pid in [item] if item in points else _source_inputs(derived, item, points):
            if pid not in out:
                out.append(pid)
    return out


def input_basis(stores, result_id: str) -> dict:
    """Which stored observations a derived result was computed from, restored the way §10 prescribes.

    OPC UA: the state at the END of the triggering batch within the same segment, plus every version in
    that batch in batch order.  Modbus: only that round's responses (strain_change: also the previous
    planned round).  Nothing is borrowed from other rounds or segments; what cannot be found is reported.
    """
    name, result, down = _first(stores, "SELECT * FROM result WHERE id=%s", (result_id,))
    if result is None:
        return {"found": False, "unavailable": down}
    _, config, _ = _first(stores, "SELECT payload FROM config_version WHERE id=%s", (result["config_id"],))
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
    for pid in _source_inputs(derived, result["point_id"], points):
        source = points[pid]["source"]
        entry = {"source_id": source, "mode": None, "rows": [], "batch_end": None}
        if source != result["source_id"]:
            entry["mode"] = "CROSS_SOURCE_LATEST"  # no cross-source guarantee; shown for orientation only
            rows, _ = _all(
                stores,
                f"SELECT {BASIS_FIELDS} FROM observation WHERE point_id=%s AND run_id=%s AND effective_wall<=%s "
                "ORDER BY effective_wall DESC,batch_seq DESC,item_seq DESC LIMIT 1",
                (pid, result["run_id"], result["at_wall"]),
            )
        elif kinds[source] == "modbus":
            entry["mode"] = "THIS_ROUND"
            wanted = [result["batch_seq"]] + ([previous_batch] if previous_batch else [])
            rows, _ = _all(
                stores,
                f"SELECT {BASIS_FIELDS} FROM observation WHERE point_id=%s AND run_id=%s AND source_id=%s "
                "AND batch_seq=ANY(%s) ORDER BY batch_seq,item_seq",
                (pid, result["run_id"], source, wanted),
            )
            if previous_batch:
                out["previous_round"] = {"batch_seq": previous_batch, "tick": (result["basis"] or {}).get("previous_tick")}
        else:
            entry["mode"] = "BATCH_END_STATE"
            in_batch, _ = _all(
                stores,
                f"SELECT {BASIS_FIELDS} FROM observation WHERE point_id=%s AND run_id=%s AND source_id=%s "
                "AND seg=%s AND batch_seq=%s ORDER BY item_seq",
                (pid, result["run_id"], source, result["seg"], result["batch_seq"]),
            )
            if in_batch:
                rows = sorted(in_batch, key=lambda r: r["item_seq"])
            else:
                rows, _ = _all(
                    stores,
                    f"SELECT {BASIS_FIELDS} FROM observation WHERE point_id=%s AND run_id=%s AND source_id=%s "
                    "AND seg=%s AND batch_seq<%s ORDER BY batch_seq DESC,item_seq DESC LIMIT 1",
                    (pid, result["run_id"], source, result["seg"], result["batch_seq"]),
                )
        entry["rows"] = [dict(r) for r in rows]
        current = [r for r in entry["rows"] if r["batch_seq"] == result["batch_seq"] or entry["mode"] != "THIS_ROUND"]
        entry["batch_end"] = current[-1]["id"] if current else None
        this_round = [r for r in entry["rows"] if r["batch_seq"] == result["batch_seq"]] if entry["mode"] == "THIS_ROUND" else []
        if not entry["rows"] or (this_round and all((r["quality"] or "").startswith(MISSING) for r in this_round)):
            out["missing"].append(pid)  # nothing was found - or this round gave the input only a failure record
        for r in entry["rows"]:
            if r["origin"] == "STATE_ASSERTION":
                original, _, _ = _first(stores, "SELECT 1 AS found FROM observation WHERE id=%s", (r["orig_id"],))
                if original is None:
                    out["notes"].append(f"ORIGINAL_OBSERVATION_LOST:{r['orig_id']}")
        out["inputs"][pid] = entry
    if result["basis_incomplete"]:
        out["notes"].append("INPUTS_LOST_IN_ARCHIVE_GAP")
    if result["origin"] == "STATE_ASSERTION":
        out["notes"].append("RESTATED_RESULT")
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
        raise ValueError("Invalid cursor or cursor belongs to another query") from error


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
        raise ValueError("limit must be between 1 and 1000")
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
