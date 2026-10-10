"""Typed-record archive store.  Bounded synchronous DB operations: call from worker threads only."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import records as rec
from .config import Settings, Timing

SCHEMA_GENERATION = 5  # continues the legacy schema_version 1-4 numbering

OBSERVATION_SQL = (
    "INSERT INTO observation(id,group_id,run_id,source_id,conn_gen,seg,batch_seq,item_seq,point_id,value,"
    "quality,status_code,received_wall,received_mono,source_time,origin,orig_id,asserted_wall,asserted_mono,"
    "extreme,tick,poll_id,response_id) VALUES(" + ",".join(["%s"] * 23) + ") ON CONFLICT(id) DO NOTHING"
)
RESULT_SQL = (
    "INSERT INTO result(id,group_id,run_id,source_id,conn_gen,seg,batch_seq,point_id,value,status,reason,"
    "extreme,config_id,at_wall,basis,estimate,origin,orig_id,asserted_wall,basis_incomplete) "
    "VALUES(" + ",".join(["%s"] * 20) + ") ON CONFLICT(id) DO NOTHING"
)
STATE_SQL = (
    "INSERT INTO state_event(id,group_id,run_id,source_id,seg,kind,at_wall,at_mono,detail) "
    "VALUES(" + ",".join(["%s"] * 9) + ") ON CONFLICT(id) DO NOTHING"
)
FAULT_SQL = (
    "INSERT INTO fault_record(id,group_id,run_id,source_id,seg,batch_seq,point_id,direction,confirmed_wall,"
    "confirmed_mono,basis_kind,basis_id,basis_wall,basis_value,config_id,rule) "
    "VALUES(" + ",".join(["%s"] * 16) + ") ON CONFLICT(id) DO NOTHING"
)


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """Result of one group write.  UNKNOWN means COMMIT was sent but its fate is not known."""

    status: str  # COMMITTED | ALREADY | FAILED | UNKNOWN
    error: str | None = None

    @property
    def durable(self) -> bool:
        return self.status in ("COMMITTED", "ALREADY")


def _json(value):
    return None if value is None else Jsonb(value)


class SchemaError(RuntimeError):
    """The database cannot be used by this generation; nothing was changed."""


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
        """One transaction per commit group: all records or none, idempotent by stable id.

        FAILED is certain (rolled back).  UNKNOWN is returned only when the failure came after the last
        statement, i.e. while COMMIT was in flight; the caller must verify by id rather than rewrite.
        """
        reached_commit = False
        try:
            with self.session() as conn:
                first = conn.execute(
                    "INSERT INTO commit_group(group_id,order_no,run_id,source_id,kind,records,built_wall) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(group_id) DO NOTHING RETURNING group_id",
                    (
                        group.group_id,
                        group.order,
                        group.run_id,
                        group.source_id,
                        group.kind,
                        group.record_count(),
                        group.built_wall,
                    ),
                ).fetchone()
                if first is None:
                    return WriteOutcome("ALREADY")  # an earlier, complete commit of this very group
                self._write_members(conn, group)
                reached_commit = True
            return WriteOutcome("COMMITTED")
        except psycopg.Error as error:
            return WriteOutcome("UNKNOWN" if reached_commit else "FAILED", type(error).__name__)

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
            o.id, g.group_id, o.run_id, o.source_id, o.conn_gen, o.seg, o.batch_seq, o.item_seq, o.point_id,
            o.value, o.quality, o.status_code, o.received_wall, o.received_mono, o.source_time, o.origin,
            o.orig_id, o.asserted_wall, o.asserted_mono, o.extreme, o.tick, o.poll_id, o.response_id,
        )  # fmt: skip

    @staticmethod
    def _result_row(g, x):
        return (
            x.id, g.group_id, x.run_id, x.source_id, x.conn_gen, x.seg, x.batch_seq, x.point_id, x.value,
            x.status, x.reason, x.extreme, x.config_id, x.at_wall, _json(x.basis), _json(x.estimate),
            x.origin, x.orig_id, x.asserted_wall, x.basis_incomplete,
        )  # fmt: skip

    @staticmethod
    def _state_row(g, x):
        return (x.id, g.group_id, x.run_id, x.source_id, x.seg, x.kind, x.at_wall, x.at_mono, Jsonb(x.detail))

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

    def reconcile_unfinished_runs(self, current_run_id: str | None = None) -> list[dict]:
        """Close runs that never recorded an end, using ONLY what is persisted (§5).

        The end is the latest persisted evidence of that run (a watermark, an observation, a state record);
        no crash time is guessed.  Segments still open are cut at that same evidence.
        """
        closed = []
        with self.session() as conn:
            runs = conn.execute(
                "SELECT run_id,started_wall FROM run WHERE ended_wall IS NULL AND run_id IS DISTINCT FROM %s",
                (current_run_id,),
            ).fetchall()
            for run in runs:
                rid = run["run_id"]
                evidence = conn.execute(
                    "SELECT greatest("
                    "%(start)s,"
                    "coalesce((SELECT max(covered_wall) FROM watermark WHERE run_id=%(r)s),0),"
                    "coalesce((SELECT max(effective_wall) FROM observation WHERE run_id=%(r)s),0),"
                    "coalesce((SELECT max(effective_wall) FROM result WHERE run_id=%(r)s),0),"
                    "coalesce((SELECT max(at_wall) FROM state_event WHERE run_id=%(r)s AND kind<>'CLOCK_JUMP'),0),"
                    "coalesce((SELECT max(confirmed_wall) FROM fault_record WHERE run_id=%(r)s),0)) AS t",
                    {"start": run["started_wall"], "r": rid},
                ).fetchone()["t"]
                conn.execute(
                    "UPDATE run SET ended_wall=%s,end_reason='ABNORMAL_EXIT',last_evidence_wall=%s WHERE run_id=%s",
                    (evidence, evidence, rid),
                )
                cut = conn.execute(
                    "UPDATE segment SET end_wall=%s,end_reason='EXIT_TRUNCATED' "
                    "WHERE run_id=%s AND end_wall IS NULL AND start_wall IS NOT NULL",
                    (evidence, rid),
                ).rowcount
                closed.append({"run_id": rid, "ended_wall": evidence, "segments_cut": cut})
        return closed

    # --------------------------------------------------------------------- routing state

    def external_selected(self, select=False) -> bool:
        with self.session() as conn:
            if select:
                conn.execute(
                    "INSERT INTO routing_state(id,mode,external_selected) VALUES(1,'EXTERNAL',true) "
                    "ON CONFLICT(id) DO UPDATE SET external_selected=true,updated_at=now()"
                )
            row = conn.execute("SELECT external_selected FROM routing_state WHERE id=1").fetchone()
            return bool(row and row["external_selected"])

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

    def group_committed(self, group_id: str) -> bool:
        """Verify by stable id, e.g. after a commit whose outcome is unknown."""
        with self.session() as conn:
            row = conn.execute("SELECT 1 AS found FROM commit_group WHERE group_id=%s", (group_id,)).fetchone()
            return row is not None

    def probe_write(self):
        # Successful return includes COMMIT, not merely a readable connection.
        with self.session() as conn:
            conn.execute(
                "INSERT INTO write_probe(id,touched_at) VALUES(1,now()) "
                "ON CONFLICT(id) DO UPDATE SET touched_at=excluded.touched_at"
            )

    def read_accounts(self):
        with self.session() as conn:
            rows = conn.execute("SELECT username,role,salt,digest,active FROM account").fetchall()
            return {
                row["username"]: {key: value for key, value in row.items() if key != "username"}
                for row in rows
            }

    def sync_accounts(self, users, audit=None):
        with self.session() as conn:
            for name, user in users.items():
                conn.execute(
                    "INSERT INTO account(username,role,salt,digest,active) VALUES(%s,%s,%s,%s,%s) "
                    "ON CONFLICT(username) DO UPDATE SET role=excluded.role,salt=excluded.salt,"
                    "digest=excluded.digest,active=excluded.active",
                    (name, user["role"], user["salt"], user["digest"], user["active"]),
                )
            if audit is not None:
                self._audit(conn, audit)

    @staticmethod
    def _audit(conn, entry):
        conn.execute(
            "INSERT INTO management_log(id,time,actor,action,target,result) VALUES(%s,%s,%s,%s,%s,%s)",
            tuple(entry[key] for key in ("id", "time", "actor", "action", "target", "result")),
        )

    def audit(self, entry):
        with self.session() as conn:
            self._audit(conn, entry)

    def audit_history(self):
        with self.session() as conn:
            return {
                "items": conn.execute(
                    "SELECT id,time,actor,action,target,result FROM management_log "
                    "ORDER BY time DESC,id DESC LIMIT 200"
                ).fetchall()
            }
