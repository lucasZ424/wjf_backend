"""Typed-record archive store.  Bounded synchronous DB operations: call from worker threads only."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import records as rec
from .config import Settings, Timing

SCHEMA_GENERATION = 6  # continues the legacy schema_version 1-4 numbering; 6 adds the buckets (§13.6)
PARTITIONED = ("commit_group", "observation", "result", "state_event")  # by bucket; every other table is kept for good
RETIRED = ("DELETING", "DELETED")  # a bucket in these states takes no group

OBSERVATION_SQL = (
    "INSERT INTO observation(bucket,id,group_id,run_id,source_id,conn_gen,seg,batch_seq,item_seq,point_id,value,"
    "quality,status_code,received_wall,received_mono,source_time,origin,orig_id,asserted_wall,asserted_mono,"
    "extreme,tick,poll_id,response_id) VALUES(" + ",".join(["%s"] * 24) + ") ON CONFLICT(id,bucket) DO NOTHING"
)
RESULT_SQL = (
    "INSERT INTO result(bucket,id,group_id,run_id,source_id,conn_gen,seg,batch_seq,point_id,value,status,reason,"
    "extreme,config_id,at_wall,basis,estimate,origin,orig_id,asserted_wall,basis_incomplete) "
    "VALUES(" + ",".join(["%s"] * 21) + ") ON CONFLICT(id,bucket) DO NOTHING"
)
STATE_SQL = (
    "INSERT INTO state_event(bucket,id,group_id,run_id,source_id,seg,kind,at_wall,at_mono,detail) "
    "VALUES(" + ",".join(["%s"] * 10) + ") ON CONFLICT(id,bucket) DO NOTHING"
)
FAULT_SQL = (
    "INSERT INTO fault_record(id,group_id,run_id,source_id,seg,batch_seq,point_id,direction,confirmed_wall,"
    "confirmed_mono,basis_kind,basis_id,basis_wall,basis_value,config_id,rule) "
    "VALUES(" + ",".join(["%s"] * 16) + ") ON CONFLICT(id) DO NOTHING"
)


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """Result of one group write.  UNKNOWN means COMMIT was sent but its fate is not known.  RETIRED: the group's bucket is
    being or has been deleted here - nothing was written, and the group is never moved into another bucket (§13.6)."""

    status: str  # COMMITTED | ALREADY | FAILED | UNKNOWN | RETIRED
    error: str | None = None
    lacks_baseline: bool = field(default=False, compare=False)  # its bucket has no retention baseline here yet (§13.6)

    @property
    def durable(self) -> bool:
        return self.status in ("COMMITTED", "ALREADY")


def _json(value):
    return None if value is None else Jsonb(value)


def group_span(group: rec.CommitGroup) -> tuple[float, float]:
    """The earliest and latest time a group is filed under: the effective time of each observation and result (the column a
    history window filters on: an asserted time first) and the time the group was built (what the deletion marks note)."""
    walls = [group.built_wall]
    walls += [o.received_wall if o.asserted_wall is None else o.asserted_wall for o in group.observations]
    walls += [x.at_wall if x.asserted_wall is None else x.asserted_wall for x in group.results]
    return min(walls), max(walls)


class SchemaError(RuntimeError):
    """The database cannot be used by this generation; nothing was changed."""


class CloseOutDeferred(RuntimeError):
    """The start-up close-out closed some runs of this database and could not close others now (a database error).  Nothing is
    lost: an unclosed run stays as it is and the next start tries it again."""

    def __init__(self, closed: list, failed: dict):
        super().__init__(f"{len(failed)} run(s) not closed now: {failed}")
        self.closed, self.failed = closed, failed


class LegacyDatabaseError(SchemaError):
    """A database created by the frame-based generation: kept as it is, never mixed into queries."""


class ArchiveStore:
    def __init__(self, dsn: str, timing: Timing | None = None):
        self.dsn = dsn
        self.timing = timing or Timing()
        self._local = threading.local()
        self._connections = []
        self._lock = threading.Lock()

    def connect(self):
        t = self.timing
        return psycopg.connect(
            self.dsn,
            connect_timeout=max(1, round(t.db_connect_timeout)),
            row_factory=dict_row,
            options=(
                f"-c statement_timeout={round(t.db_statement_timeout * 1000)} "
                f"-c lock_timeout={round(t.db_lock_timeout * 1000)}"
            ),
        )

    @contextmanager
    def session(self):
        conn = getattr(self._local, "connection", None)
        if conn is None or conn.closed:
            conn = self.connect()
            self._local.connection = conn
            with self._lock:
                self._connections = [c for c in self._connections if not c.closed]
                self._connections.append(conn)
        try:
            with conn.transaction():
                yield conn
        except psycopg.Error:
            conn.close()
            self._local.connection = None
            raise

    def close(self):
        with self._lock:
            for conn in self._connections:
                conn.close()
            self._connections.clear()

    def initialize(self, config: Settings):
        """Create the schema in an empty database, or accept one already at this generation.

        A legacy (frame-based) or foreign-generation database is refused before anything is written.
        """
        with self.session() as conn:
            if conn.execute("SELECT to_regclass('public.htsg_meta') AS t").fetchone()["t"]:
                row = conn.execute("SELECT value FROM htsg_meta WHERE key='schema_generation'").fetchone()
                if not row or row["value"] != str(SCHEMA_GENERATION):
                    raise SchemaError(f"unsupported schema generation {row and row['value']}")
                if not conn.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='bucket' "
                    "AND column_name='data_hi'"
                ).fetchone():  # laid down before the bucket range was kept: added once; its buckets stay "not known"
                    conn.execute(
                        "ALTER TABLE bucket ADD COLUMN IF NOT EXISTS data_lo double precision, "
                        "ADD COLUMN IF NOT EXISTS data_hi double precision"
                    )
            else:
                legacy = conn.execute(
                    "SELECT to_regclass('public.schema_version') AS v, to_regclass('public.frame') AS f"
                ).fetchone()
                if legacy["v"] or legacy["f"]:
                    raise LegacyDatabaseError(
                        "legacy frame-based database; create a new database instead of upgrading it"
                    )
                conn.execute(Path(__file__).with_name("archive_schema.sql").read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO htsg_meta(key,value) VALUES('schema_generation',%s) ON CONFLICT DO NOTHING",
                    (str(SCHEMA_GENERATION),),
                )
            conn.execute(
                "INSERT INTO config_version(id,payload) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (config.config_id, Jsonb(config.model_dump(mode="json"))),
            )

    # ------------------------------------------------------------------ group writes

    def bind_run(self, run_id: str, config_id: str, started_wall: float):
        """The run every group written next belongs to; its row is created inside the first group."""
        self.run_info = (run_id, config_id, started_wall)

    def write_group(self, group: rec.CommitGroup) -> WriteOutcome:
        """One transaction per commit group: all records or none, idempotent by stable id within the group's bucket.

        FAILED is certain (rolled back); `NO_BUCKET` when this database does not have the group's bucket.  UNKNOWN is
        returned only when the failure came after the last statement, i.e. while COMMIT was in flight; the caller must
        verify by id rather than rewrite.  The bucket's row is share-locked for the whole transaction and its state checked
        first: marking it for deletion needs the exclusive lock, so the two exclude each other (§13.6 写入资格).
        """
        reached_commit = False
        try:
            with self.session() as conn:
                row = conn.execute(
                    "SELECT state,baseline_wall FROM bucket WHERE no=%s FOR SHARE", (group.bucket,)
                ).fetchone()
                if row is None:
                    return WriteOutcome("FAILED", "NO_BUCKET")
                if row["state"] in RETIRED:
                    return WriteOutcome("RETIRED", "BUCKET_RETIRED")
                first = conn.execute(
                    "INSERT INTO commit_group(bucket,group_id,order_no,run_id,source_id,kind,records,built_wall) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(group_id,bucket) DO NOTHING RETURNING group_id",
                    (
                        group.bucket,
                        group.group_id,
                        group.order,
                        group.run_id,
                        group.source_id,
                        group.kind,
                        group.record_count(),
                        group.built_wall,
                    ),
                ).fetchone()
                lacks = row["baseline_wall"] is None
                if first is None:
                    return WriteOutcome("ALREADY", lacks_baseline=lacks)  # an earlier, complete commit of this very group
                # with the rows, so the range a query trusts can never miss one of them.  `used` is still the value from
                # before this group: a bucket that held rows with no range kept for them stays "not known" - a range of
                # this group alone would leave its older rows out of every query - and any bucket written to is used
                lo, hi = group_span(group)
                conn.execute(
                    "UPDATE bucket SET used=true,"
                    "data_lo=CASE WHEN used AND data_lo IS NULL THEN NULL ELSE least(coalesce(data_lo,%s),%s) END,"
                    "data_hi=CASE WHEN used AND data_hi IS NULL THEN NULL ELSE greatest(coalesce(data_hi,%s),%s) END "
                    "WHERE no=%s",
                    (lo, lo, hi, hi, group.bucket),
                )
                if row["state"] == "READY":
                    self._open_bucket(conn, group.bucket)
                self._write_members(conn, group)
                lacks = lacks and not any(st.kind == "RETENTION_BASELINE" for st in group.states)
                reached_commit = True
            return WriteOutcome("COMMITTED", lacks_baseline=lacks)
        except psycopg.Error as error:
            return WriteOutcome("UNKNOWN" if reached_commit else "FAILED", type(error).__name__)

    @staticmethod
    def _open_bucket(conn, no):
        """The first group of a bucket here: it becomes this database's current bucket, used, and every older one still
        current or never used here is sealed (plan D1) - unless a newer one is current already, then it is sealed at once.
        Nothing is ever un-sealed; a sealed bucket still takes the groups given to it before."""
        newer = conn.execute("SELECT 1 FROM bucket WHERE state='CURRENT' AND no>%s LIMIT 1", (no,)).fetchone()
        conn.execute(
            "UPDATE bucket SET state=%s,used=true,changed_at=now() WHERE no=%s", ("SEALED" if newer else "CURRENT", no)
        )
        if not newer:
            conn.execute(
                "UPDATE bucket SET state='SEALED',changed_at=now() WHERE state IN ('CURRENT','READY') AND no<%s", (no,)
            )

    def prepare_bucket(self, no: int) -> bool:
        """An EMPTY bucket here: its four partitions and its READY row, in one transaction; False when it is there already.
        Each partition is made apart and then attached: attaching takes only a SHARE UPDATE EXCLUSIVE lock on the table, so it
        neither waits for nor holds up the writes and reads going on - making it with PARTITION OF would take ACCESS EXCLUSIVE."""
        no = int(no)
        with self.session() as conn:
            if conn.execute("SELECT 1 FROM bucket WHERE no=%s", (no,)).fetchone():
                return False
            for table in PARTITIONED:
                part = sql.Identifier(f"{table}_b{no}")
                conn.execute(sql.SQL("CREATE TABLE {} (LIKE {} INCLUDING ALL)").format(part, sql.Identifier(table)))
                conn.execute(
                    sql.SQL("ALTER TABLE {} ATTACH PARTITION {} FOR VALUES IN ({})").format(
                        sql.Identifier(table), part, sql.Literal(no)
                    )
                )
            conn.execute("INSERT INTO bucket(no) VALUES(%s)", (no,))
        return True

    def bucket_takes(self, no: int | None) -> bool:
        """Whether this database has the bucket and it may still take a group."""
        with self.session() as conn:
            row = conn.execute("SELECT state FROM bucket WHERE no=%s", (no,)).fetchone()
        return row is not None and row["state"] not in RETIRED

    def bucket_catalog(self) -> dict[int, str]:
        """This database's bucket list: {number: state}."""
        with self.session() as conn:
            return {x["no"]: x["state"] for x in conn.execute("SELECT no,state FROM bucket").fetchall()}

    def bucket_span(self, no: int) -> tuple:
        """The first and last time its groups were built at, here."""
        with self.session() as conn:
            row = conn.execute(
                "SELECT min(built_wall) AS lo,max(built_wall) AS hi FROM commit_group WHERE bucket=%s", (no,)
            ).fetchone()
        return row["lo"], row["hi"]

    def bucket_bytes(self, no: int) -> int:
        """The disk its four partitions take, indexes included."""
        names = [f"{t}_b{int(no)}" for t in PARTITIONED]
        with self.session() as conn:
            row = conn.execute(
                "SELECT coalesce(sum(pg_total_relation_size(oid)),0)::bigint AS n FROM pg_class WHERE relname=ANY(%s)", (names,)
            ).fetchone()
        return row["n"]

    def deletion_times(self, action: str = "ROLLING_DELETE") -> list[float]:
        """When the rolling deletion took ranges away here (or, `ROLLING_DELETE_START`, began a cleanup), oldest first, from
        the management log, which is kept for good."""
        with self.session() as conn:
            rows = conn.execute(
                "SELECT time FROM (SELECT time FROM management_log WHERE action=%s ORDER BY time DESC LIMIT 200) t ORDER BY time",
                (action,),
            ).fetchall()
        return [x["time"] for x in rows]

    def cleanup_going_on(self) -> bool:
        """The management log says a cleanup began here and has not ended: its latest start is later than its latest end."""
        with self.session() as conn:
            row = conn.execute(
                "SELECT max(time) FILTER (WHERE action='ROLLING_DELETE_START') AS began,"
                "max(time) FILTER (WHERE action='ROLLING_DELETE_END') AS ended FROM management_log"
            ).fetchone()
        return row["began"] is not None and (row["ended"] is None or row["began"] > row["ended"])

    def bucket_rows(self) -> list[dict]:
        """This database's bucket list with what the deletion decides by, oldest first."""
        with self.session() as conn:
            return conn.execute("SELECT no,state,used,baseline_wall,first_wall FROM bucket ORDER BY no").fetchall()

    def mark_deleting(self, nos: list[int], keep: int) -> bool:
        """One short transaction: lock the rows of the range and of the bucket kept after it, check again what this database
        decides (every bucket of the range sealed, the kept one used with its baseline) and mark the whole range DELETING.  A
        write of a late group into the range holds a share lock on its row, so the two exclude each other (§13.6 删除步骤)."""
        with self.session() as conn:
            rows = {
                x["no"]: x
                for x in conn.execute(
                    "SELECT no,state,used,baseline_wall FROM bucket WHERE no=ANY(%s) FOR UPDATE", ([*nos, keep],)
                ).fetchall()
            }
            kept = rows.get(keep)
            if kept is None or not kept["used"] or kept["baseline_wall"] is None:
                return False
            if any(rows.get(no) is None or rows[no]["state"] != "SEALED" for no in nos):
                return False
            conn.execute(
                "UPDATE bucket b SET first_wall=r.lo,last_wall=r.hi FROM (SELECT bucket,min(built_wall) AS lo,"
                "max(built_wall) AS hi FROM commit_group WHERE bucket=ANY(%s) GROUP BY bucket) r WHERE b.no=r.bucket",
                (list(nos),),
            )  # what the range held stays known after its partitions are gone: the queries say it was deleted
            conn.execute("UPDATE bucket SET state='DELETING',changed_at=now() WHERE no=ANY(%s)", (list(nos),))
        return True

    def retire_bucket(self, no: int) -> bool:
        """Take one bucket marked DELETING away: for each of the four tables, finish a detach left pending on that table, detach
        the bucket's partition CONCURRENTLY (outside any transaction block, so reads and writes go on), drop it.  Every step
        looks at the system catalogue first, so it resumes wherever a crash or a timeout stopped it; at most one detach per
        table is ever pending.  DELETED only once the catalogue shows all four gone.  A long transaction makes a step time out:
        the error is raised, the bucket stays DELETING and the next round goes on (§13.6 删除步骤, 维护连接)."""
        with self.connect() as conn:
            conn.autocommit = True
            for table in PARTITIONED:
                part = f"{table}_b{int(no)}"
                pending = conn.execute(
                    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
                    "WHERE i.inhparent=%s::regclass AND i.inhdetachpending",
                    (table,),
                ).fetchall()
                for row in pending:
                    conn.execute(
                        sql.SQL("ALTER TABLE {} DETACH PARTITION {} FINALIZE").format(
                            sql.Identifier(table), sql.Identifier(row["relname"])
                        )
                    )
                attached = conn.execute(
                    "SELECT 1 FROM pg_inherits WHERE inhrelid=to_regclass(%s) AND inhparent=%s::regclass", (part, table)
                ).fetchone()
                if attached:
                    conn.execute(
                        sql.SQL("ALTER TABLE {} DETACH PARTITION {} CONCURRENTLY").format(
                            sql.Identifier(table), sql.Identifier(part)
                        )
                    )
                if conn.execute("SELECT to_regclass(%s) AS t", (part,)).fetchone()["t"] is not None:
                    conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(part)))
            left = [t for t in PARTITIONED if conn.execute("SELECT to_regclass(%s) AS t", (f"{t}_b{int(no)}",)).fetchone()["t"]]
            if left:
                return False
            conn.execute("UPDATE bucket SET state='DELETED',changed_at=now() WHERE no=%s AND state='DELETING'", (no,))
        return True

    def _write_members(self, conn, group: rec.CommitGroup):
        info = getattr(self, "run_info", None)
        if info and info[0] == group.run_id:
            conn.execute(
                "INSERT INTO run(run_id,config_id,started_wall) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", info
            )
        cur = conn.cursor()
        if group.observations:
            cur.executemany(OBSERVATION_SQL, [self._observation_row(group, o) for o in group.observations])
        if group.results:
            cur.executemany(RESULT_SQL, [self._result_row(group, x) for x in group.results])
        if group.states:
            cur.executemany(STATE_SQL, [self._state_row(group, x) for x in group.states])
            for state in group.states:
                self._apply_state(conn, group, state)
        if group.faults:
            cur.executemany(FAULT_SQL, [self._fault_row(group, f) for f in group.faults])

    @staticmethod
    def _observation_row(g, o):
        return (
            g.bucket, o.id, g.group_id, o.run_id, o.source_id, o.conn_gen, o.seg, o.batch_seq, o.item_seq, o.point_id,
            o.value, o.quality, o.status_code, o.received_wall, o.received_mono, o.source_time, o.origin,
            o.orig_id, o.asserted_wall, o.asserted_mono, o.extreme, o.tick, o.poll_id, o.response_id,
        )  # fmt: skip

    @staticmethod
    def _result_row(g, x):
        return (
            g.bucket, x.id, g.group_id, x.run_id, x.source_id, x.conn_gen, x.seg, x.batch_seq, x.point_id, x.value,
            x.status, x.reason, x.extreme, x.config_id, x.at_wall, _json(x.basis), _json(x.estimate),
            x.origin, x.orig_id, x.asserted_wall, x.basis_incomplete,
        )  # fmt: skip

    @staticmethod
    def _state_row(g, x):
        return (g.bucket, x.id, g.group_id, x.run_id, x.source_id, x.seg, x.kind, x.at_wall, x.at_mono, Jsonb(x.detail))

    @staticmethod
    def _fault_row(g, f):
        return (
            f.id, g.group_id, f.run_id, f.source_id, f.seg, f.batch_seq, f.point_id, f.direction,
            f.confirmed_wall, f.confirmed_mono, f.basis_kind, f.basis_id, f.basis_wall, f.basis_value,
            f.config_id, Jsonb(f.rule),
        )  # fmt: skip

    @staticmethod
    def _apply_state(conn, group: rec.CommitGroup, state: rec.StateRecord):
        """State records that define queryable structure also update their dedicated tables."""
        d = state.detail
        if state.kind == "SEG_START":
            conn.execute(
                "INSERT INTO segment(run_id,source_id,seg,conn_gen,start_wall,start_reason) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id,source_id,seg) DO UPDATE SET "
                "conn_gen=excluded.conn_gen,start_wall=excluded.start_wall,start_reason=excluded.start_reason",
                (state.run_id, state.source_id, state.seg, d.get("conn_gen"), state.at_wall, d.get("reason")),
            )
        elif state.kind == "SEG_END":
            conn.execute(
                "INSERT INTO segment(run_id,source_id,seg,end_wall,end_reason) VALUES(%s,%s,%s,%s,%s) "
                "ON CONFLICT(run_id,source_id,seg) DO UPDATE SET end_wall=excluded.end_wall,"
                "end_reason=excluded.end_reason",
                (state.run_id, state.source_id, state.seg, d.get("end_wall", state.at_wall), d.get("reason")),
            )
        elif state.kind == "GAP":
            conn.execute(
                "INSERT INTO archive_gap(id,run_id,source_id,start_wall,end_wall,reason,detail) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                (
                    state.id,
                    state.run_id,
                    state.source_id,
                    d["start_wall"],
                    d.get("end_wall"),
                    d.get("reason", "UNKNOWN"),
                    Jsonb(d),
                ),
            )
        elif state.kind == "WATERMARK":
            conn.execute(
                "INSERT INTO watermark(run_id,source_id,seg,covered_wall,group_order) VALUES(%s,%s,%s,%s,%s) "
                "ON CONFLICT(run_id,source_id,seg) DO UPDATE SET "
                "covered_wall=greatest(watermark.covered_wall,excluded.covered_wall),"
                "group_order=excluded.group_order,committed_at=now()",
                (state.run_id, state.source_id, d["seg"], d["covered_wall"], group.order),
            )
        elif state.kind == "RETENTION_BASELINE":  # the bucket's boundary here is its earliest baseline (§13.6)
            conn.execute(
                "UPDATE bucket SET baseline_wall=least(coalesce(baseline_wall,%s),%s) WHERE no=%s",
                (state.at_wall, state.at_wall, group.bucket),
            )
        elif state.kind == "RUN_END":
            conn.execute(
                "UPDATE run SET ended_wall=%s,end_reason=%s WHERE run_id=%s",
                (d.get("ended_wall", state.at_wall), d.get("reason", "NORMAL"), state.run_id),
            )
        elif state.kind == "FAULT_LOSS":
            conn.execute(
                "INSERT INTO fault_loss(id,group_id,run_id,source_id,lost_count,first_wall,last_wall,detail) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                (
                    state.id,
                    group.group_id,
                    state.run_id,
                    state.source_id,
                    d["count"],
                    d["first_wall"],
                    d["last_wall"],
                    Jsonb(d),
                ),
            )

    @staticmethod
    def _segment_evidence(conn, run_ids) -> dict:
        """(run, source, seg) -> the segment's watermark: the only evidence the start-up close-out reads (§5, 细则 5.1 as revised
        2026-10-23).  It is a row per segment, so what is read grows with the number of segments and never with the number of
        observations: a crashed run of any size is closed in the same time.  Observations, state events, results and faults
        are NOT read; the observations after the last watermark stay visible in history but are not held beyond themselves."""
        rows = conn.execute(
            "SELECT run_id,source_id,seg,covered_wall AS t FROM watermark WHERE run_id=ANY(%(runs)s)",
            {"runs": list(run_ids)},
        ).fetchall()
        return {(x["run_id"], x["source_id"], x["seg"]): x["t"] for x in rows}

    def unfinished_facts(self, current_run_id: str | None = None) -> dict:
        """What this database says about how runs and segments ended, for closing the unfinished ones across ALL
        bound databases: the runs and segments it holds an end for, and the evidence of the segments of its own
        unended runs.  Read only."""
        with self.session() as conn:
            # A real RUN_END in any bound database means the run ended there.  ABNORMAL_EXIT is only this database's
            # crash-reconciliation result; it must not make another database skip its own unfinished rows after a
            # previous cross-database reconciliation stopped halfway.
            ended_runs = {
                x["run_id"]
                for x in conn.execute(
                    "SELECT run_id FROM run WHERE ended_wall IS NOT NULL AND end_reason<>'ABNORMAL_EXIT'"
                ).fetchall()
            }
            ended_segments = {
                (x["run_id"], x["source_id"], x["seg"])
                for x in conn.execute(
                    "SELECT run_id,source_id,seg FROM segment "
                    "WHERE end_wall IS NOT NULL AND end_reason<>'EXIT_TRUNCATED'"
                ).fetchall()
            }
            open_runs = [
                x["run_id"]
                for x in conn.execute(
                    "SELECT run_id FROM run WHERE ended_wall IS NULL AND run_id IS DISTINCT FROM %s", (current_run_id,)
                ).fetchall()
            ]
            evidence = self._segment_evidence(conn, open_runs)
        return {"ended_runs": ended_runs, "ended_segments": ended_segments, "evidence": evidence}

    def reconcile_unfinished_runs(
        self, current_run_id: str | None = None, *, ended_runs=frozenset(), ended_segments=frozenset(), evidence=None
    ) -> list[dict]:
        """Close runs that never recorded an end, using ONLY what is persisted (§5).

        Each segment still open is cut at ITS OWN evidence - its watermark, its start when it has none (细则 5.1 as revised
        2026-10-23) - never at the run's, and never before its own start.  The run's end is the latest of its start and the
        watermarks this database holds; no crash time is guessed.  Only the `run`, `segment` and `watermark` tables are read, so
        the time this takes does not grow with the size of the crashed run.  What the other bound databases hold is respected
        (the router gathers it with `unfinished_facts`): a run or segment that ended in any of them is not closed here, and the
        evidence of a segment counts wherever it was written.

        Every run is closed in a transaction of its own.  One that cannot be closed now (a database error) leaves the others
        alone: they are closed, and `CloseOutDeferred` says which were not - the next start tries those again.
        """
        closed, failed, evidence = [], {}, evidence or {}
        with self.session() as conn:
            runs = conn.execute(
                "SELECT run_id,started_wall FROM run WHERE ended_wall IS NULL AND run_id IS DISTINCT FROM %s",
                (current_run_id,),
            ).fetchall()
            runs = [run for run in runs if run["run_id"] not in ended_runs]
            own = self._segment_evidence(conn, [run["run_id"] for run in runs])
        for run in runs:
            try:
                with self.session() as conn:
                    closed.append(self._close_run(conn, run, own, evidence, ended_segments))
            except psycopg.Error as error:
                failed[run["run_id"]] = type(error).__name__
        if failed:
            raise CloseOutDeferred(closed, failed)
        return closed

    @staticmethod
    def _close_run(conn, run, own, evidence, ended_segments) -> dict:
        rid = run["run_id"]
        latest = max([run["started_wall"], *(t for key, t in own.items() if key[0] == rid)])
        conn.execute(
            "UPDATE run SET ended_wall=%s,end_reason='ABNORMAL_EXIT',last_evidence_wall=%s WHERE run_id=%s",
            (latest, latest, rid),
        )
        cut = 0
        open_segments = conn.execute(
            "SELECT source_id,seg,start_wall FROM segment WHERE run_id=%s AND end_wall IS NULL AND start_wall IS NOT NULL",
            (rid,),
        ).fetchall()
        for segment in open_segments:
            key = (rid, segment["source_id"], segment["seg"])
            if key in ended_segments:
                continue  # it has its end in another database
            known = [t for t in (own.get(key), evidence.get(key)) if t is not None]
            cut += conn.execute(
                "UPDATE segment SET end_wall=%s,end_reason='EXIT_TRUNCATED' "
                "WHERE run_id=%s AND source_id=%s AND seg=%s AND end_wall IS NULL",
                (max([segment["start_wall"], *known]), rid, segment["source_id"], segment["seg"]),
            ).rowcount
        return {"run_id": rid, "ended_wall": latest, "segments_cut": cut}

    # --------------------------------------------------------------------- routing state

    def routing_mode(self, value=None) -> str:
        with self.session() as conn:
            if value:
                conn.execute(
                    "INSERT INTO routing_state(id,mode) VALUES(1,%s) "
                    "ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,updated_at=now()",
                    (value,),
                )
                return value
            row = conn.execute("SELECT mode FROM routing_state WHERE id=1").fetchone()
            return row["mode"] if row else "PRIMARY"

    def metrics(self) -> dict:
        """Heavy statistics (sizes, row estimates).  Run on the slow cadence, never per group."""
        with self.session() as conn:
            size = conn.execute("SELECT pg_database_size(current_database()) AS bytes").fetchone()["bytes"]
            tables = conn.execute("SELECT relname,n_live_tup,n_dead_tup FROM pg_stat_user_tables").fetchall()
        return {"bytes": size, "tables": tables, "measured_at": time.time()}

    def group_committed(self, group_id: str, bucket: int | None) -> bool:
        """Verify by stable id in the group's own bucket, e.g. after a commit whose outcome is unknown."""
        with self.session() as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM commit_group WHERE group_id=%s AND bucket=%s", (group_id, bucket)
            ).fetchone()
            return row is not None

    def probe_write(self):
        # Successful return includes COMMIT, not merely a readable connection.
        with self.session() as conn:
            conn.execute(
                "INSERT INTO write_probe(id,touched_at) VALUES(1,now()) "
                "ON CONFLICT(id) DO UPDATE SET touched_at=excluded.touched_at"
            )

    def audit(self, entry):
        """One management record (an account change is made only once this has committed: 2026-10-30)."""
        with self.session() as conn:
            conn.execute(
                "INSERT INTO management_log(id,time,actor,action,target,result) VALUES(%s,%s,%s,%s,%s,%s)",
                tuple(entry[key] for key in ("id", "time", "actor", "action", "target", "result")),
            )

    def audit_history(self):
        with self.session() as conn:
            return {
                "items": conn.execute(
                    "SELECT id,time,actor,action,target,result FROM management_log "
                    "ORDER BY time DESC,id DESC LIMIT 200"
                ).fetchall()
            }
