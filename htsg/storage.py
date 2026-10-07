"""Bounded synchronous DB operations; call exclusively from dedicated worker threads."""

from __future__ import annotations

import ntpath
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import Settings


class PolicyRefusal(RuntimeError):
    """An explicit bounded-storage policy refusal, not a programming failure."""


class ExternalBindingError(ValueError):
    pass


def _volume(path):
    drive, tail = ntpath.splitdrive(path)
    if not drive or not tail.startswith(("/", "\\")):
        raise ExternalBindingError("External storage requires an absolute Windows volume path")
    return drive.casefold()


class Store:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._local = threading.local()
        self._connections = []
        self._lock = threading.Lock()

    def connect(self):
        return psycopg.connect(
            self.dsn,
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=3000 -c lock_timeout=1000",
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

    def validate_external_binding(external, config, primary, emergency):
        """Recheck on external reconnection; failed checks must never authorize writes.

        All stores belong to the local deployment. An unavailable peer leaves the
        check pending so the caller can retain the emergency route.
        """
        with external.session() as conn:
            if getattr(external._local, "binding_verified_connection", None) is conn:
                return
            row = conn.execute(
                "SELECT current_setting('data_directory') AS path,current_database() AS name"
            ).fetchone()
        external_path = ntpath.normcase(ntpath.normpath(row["path"]))
        external_volume = _volume(external_path)
        if external_volume != _volume(config.storage.external_path):
            raise ExternalBindingError("External database is not on its configured volume")
        if config.mode == "development" and external_volume != "d:":
            raise ExternalBindingError("Development external database must be on D:")
        identity = (external_path, row["name"])
        for peer in (primary, emergency):
            with peer.session() as peer_conn:
                other = peer_conn.execute(
                    "SELECT current_setting('data_directory') AS path,current_database() AS name"
                ).fetchone()
            peer_path = ntpath.normcase(ntpath.normpath(other["path"]))
            if identity == (peer_path, other["name"]):
                raise ExternalBindingError("External database must be distinct from the existing databases")
            if config.mode == "production" and external_volume == _volume(peer_path):
                raise ExternalBindingError("Production external database needs a separate volume")
        external._local.binding_verified_connection = conn

    def probe_write(self):
        # Successful return includes COMMIT, not merely a readable connection.
        with self.session() as conn:
            conn.execute(
                "INSERT INTO write_probe(id,touched_at) VALUES(1,now()) ON CONFLICT(id) DO UPDATE SET touched_at=excluded.touched_at"
            )

    def initialize(self, config: Settings):
        with self.session() as conn:
            conn.execute(Path(__file__).with_name("schema.sql").read_text())
            conn.execute(
                "INSERT INTO config_snapshot(id,payload) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (config.config_id, Jsonb(config.model_dump())),
            )

    def interrupt_events(self):
        with self.session() as conn:
            rows = conn.execute(
                "SELECT id,payload FROM event WHERE phase <> 'CLOSED' OR payload->>'archive_status'='PENDING'"
            ).fetchall()
            for row in rows:
                event = row["payload"]
                event.update(
                    phase="CLOSED",
                    close_reason="PROCESS_INTERRUPTED",
                    coverage="PARTIAL",
                    archive_status="INTERRUPTED",
                )
                event["revision"] = event.get("revision", 0) + 1
                conn.execute(
                    "UPDATE event SET phase='CLOSED',payload=%s,updated_at=now() WHERE id=%s",
                    (Jsonb(event), row["id"]),
                )
            return len(rows)

    def pending_events(self):
        with self.session() as conn:
            return conn.execute(
                "SELECT payload FROM event WHERE phase<>'CLOSED' OR payload->>'archive_status'='PENDING' LIMIT 256"
            ).fetchall()

    @staticmethod
    def write_event(conn, payload):
        conn.execute(
            "INSERT INTO event(id,profile_id,phase,payload) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(id) DO UPDATE SET phase=excluded.phase,payload=excluded.payload,updated_at=now() "
            "WHERE coalesce((event.payload->>'revision')::bigint,0)<=coalesce((excluded.payload->>'revision')::bigint,0)",
            (payload["id"], payload["profile_id"], payload["phase"], Jsonb(payload)),
        )

    def write(self, kind: str, payload: dict):
        self.write_batch([(kind, payload)])

    def write_batch(self, records):
        with self.session() as conn:
            with conn.pipeline():
                for kind, payload in records:
                    self._write(conn, kind, payload)

    def _write(self, conn, kind, payload):
        if kind == "fault":
            conn.execute(
                "INSERT INTO fault_point(id,point_id,time,payload) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (payload["id"], payload["point_id"], payload["time"], Jsonb(payload)),
            )
        elif kind == "event":
            self.write_event(conn, payload)
        elif kind in ("sample", "raw"):
            if "event" in payload:
                self.write_event(conn, payload["event"])
            sample = payload["sample"]
            if kind == "raw":
                sample = {**sample, "archive_scope": "continuous"}
            conn.execute(
                "INSERT INTO source_sample(id,point_id,effective_time,payload) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (sample["id"], sample["point_id"], sample["effective_time"], Jsonb(sample)),
            )
            if kind == "sample":
                conn.execute(
                    "INSERT INTO event_sample VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (payload["event_id"], sample["id"]),
                )
        elif kind == "frame":
            frame = payload["frame"]
            conn.execute(
                "INSERT INTO frame(id,run_id,window_start,window_end,config_id,partial) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    frame["id"],
                    frame["run_id"],
                    frame["window_start"],
                    frame["window_end"],
                    frame["config_id"],
                    frame["partial"],
                ),
            )
            for product, values in [("source", frame["values"]), ("derived", frame["derived_results"])]:
                for point, value in values.items():
                    conn.execute(
                        "INSERT INTO frame_value(frame_id,point_id,product,payload) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (frame["id"], point, product, Jsonb(value)),
                    )
            for event_id in payload["event_ids"]:
                conn.execute(
                    "INSERT INTO event_frame(event_id,frame_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (event_id, frame["id"]),
                )
        elif kind in ("trigger", "gap"):
            table = "trigger_fact" if kind == "trigger" else "gap"
            conn.execute(
                f"INSERT INTO {table}(id,event_id,payload) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (payload["id"], payload.get("event_id"), Jsonb(payload)),
            )
        else:
            raise ValueError("unknown record kind")

    def external_selected(self, select=False):
        with self.session() as conn:
            if select:
                conn.execute(
                    "INSERT INTO routing_state(id,mode,external_selected) VALUES(1,'EXTERNAL',true) "
                    "ON CONFLICT(id) DO UPDATE SET external_selected=true,updated_at=now()"
                )
            row = conn.execute("SELECT external_selected FROM routing_state WHERE id=1").fetchone()
            return bool(row and row["external_selected"])

    def mode(self, value=None):
        with self.session() as conn:
            if value:
                conn.execute(
                    "INSERT INTO routing_state(id,mode) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,updated_at=now()",
                    (value,),
                )
                return value
            row = conn.execute("SELECT mode FROM routing_state WHERE id=1").fetchone()
            return row["mode"] if row else "PRIMARY"

    def query(self, category, start=0, end=1e20, limit=100, offset=0, point=None, event_id=None, after=None):
        with self.session() as conn:
            params = []
            descending = False
            if category == "faults":
                select = "t.payload"
                source = "fault_point t"
                keys = ["t.time", "t.id"]
                descending = True
                where = "t.time>=%s AND t.time<%s AND (%s::text IS NULL OR t.point_id=%s)"
                params = [start, end, point, point]
            elif category == "replay":
                select = "t.id,t.run_id,t.window_start,t.window_end,t.point_id,t.product,t.payload"
                source = """(
                    SELECT s.id,s.payload->>'run_id' AS run_id,s.effective_time AS window_start,
                        s.effective_time AS window_end,s.point_id,'source'::text AS product,s.payload
                    FROM source_sample s
                    UNION ALL
                    SELECT f.id,f.run_id,f.window_start,f.window_end,v.point_id,v.product,v.payload
                    FROM frame f JOIN frame_value v ON v.frame_id=f.id WHERE v.product='derived'
                ) t"""
                where = "t.window_start>=%s AND t.window_start<%s AND (%s::text IS NULL OR t.point_id=%s)"
                params = [start, end, point, point]
                keys = ["t.window_start", "t.id", "t.point_id", "t.product"]
            elif category in ("events", "event"):
                select = "e.payload, EXISTS(SELECT 1 FROM gap g WHERE g.event_id=e.id) AS has_gaps"
                source = "event e"
                keys = ["coalesce((e.payload->>'triggered_at')::double precision,0)", "e.id"]
                descending = True
                if category == "event":
                    where = "e.id=%s"
                    params = [event_id]
                else:
                    where = keys[0] + ">=%s AND " + keys[0] + "<%s"
                    params = [start, end]
            elif category == "samples":
                select = "s.payload"
                source = "source_sample s JOIN event_sample e ON e.sample_id=s.id"
                where = "e.event_id=%s AND s.effective_time>=%s AND s.effective_time<%s"
                params = [event_id, start, end]
                keys = ["s.effective_time", "s.id"]
            elif category in ("triggers", "gaps"):
                table = "trigger_fact" if category == "triggers" else "gap"
                field = "time" if category == "triggers" else "start"
                select = "t.payload"
                source = table + " t"
                keys = [f"coalesce((t.payload->>'{field}')::double precision,0)", "t.id"]
                where = "(%s::text IS NULL OR t.event_id=%s) AND " + keys[0] + ">=%s AND " + keys[0] + "<%s"
                params = [event_id, event_id, start, end]
            elif category == "products":
                select = "e.frame_id,e.results_removed,e.recomputable"
                source = "event_frame e"
                where = "e.event_id=%s"
                params = [event_id]
                keys = ["e.frame_id"]
            elif category == "history":
                allowed = None
                if event_id is not None:
                    definition = conn.execute(
                        "SELECT c.payload,e.profile_id FROM event e JOIN config_snapshot c ON c.id=e.payload->>'config_id' WHERE e.id=%s",
                        (event_id,),
                    ).fetchone()
                    if not definition:
                        return []
                    profile = next(
                        (p for p in definition["payload"]["profiles"] if p["id"] == definition["profile_id"]),
                        None,
                    )
                    if not profile:
                        return []
                    allowed = profile["source_points"] + profile["derived_points"]
                select = "f.id,f.run_id,f.window_start,f.window_end,v.point_id,v.product,v.payload"
                for state, label in [("HIGH", "high_recorded"), ("LOW", "low_recorded")]:
                    select += f", EXISTS(SELECT 1 FROM trigger_fact t WHERE t.payload->>'point_id'=v.point_id AND t.payload->>'run_id'=f.run_id AND (t.payload->>'window_start')::double precision=f.window_start AND t.payload->>'state'='{state}') AS {label}"
                source = "frame f JOIN frame_value v ON v.frame_id=f.id"
                where = "f.window_start>=%s AND f.window_start<%s AND (%s::text IS NULL OR v.point_id=%s) AND (%s::text IS NULL OR EXISTS(SELECT 1 FROM event_frame e WHERE e.frame_id=f.id AND e.event_id=%s)) AND (%s::text[] IS NULL OR v.point_id=ANY(%s::text[]))"
                params = [start, end, point, point, event_id, event_id, allowed, allowed]
                keys = ["f.window_start", "f.id", "v.point_id", "v.product"]
            else:
                raise ValueError("unknown query")
            if after is not None:
                if len(after) != len(keys):
                    raise ValueError("Invalid cursor key")
                where += (
                    " AND ("
                    + ",".join(keys)
                    + ") "
                    + ("<" if descending else ">")
                    + " ("
                    + ",".join(["%s"] * len(keys))
                    + ")"
                )
                params += list(after)
            projection = select + "," + ",".join(f"{key} AS _k{i}" for i, key in enumerate(keys))
            ordering = ",".join(key + (" DESC" if descending else " ASC") for key in keys)
            rows = conn.execute(
                "SELECT "
                + projection
                + " FROM "
                + source
                + " WHERE "
                + where
                + " ORDER BY "
                + ordering
                + " LIMIT %s OFFSET %s",
                [*params, limit, offset],
            ).fetchall()
            for row in rows:
                row["_key"] = [row.pop(f"_k{i}") for i in range(len(keys))]
            return rows

    def history_flags(self, keys):
        """Only annotate the requested page; late facts may live on another disk."""
        with self.session() as conn:
            return conn.execute(
                """SELECT k.run_id,k.window_start,k.point_id,
                bool_or(t.payload->>'state'='HIGH') AS high_recorded,
                bool_or(t.payload->>'state'='LOW') AS low_recorded
                FROM jsonb_to_recordset(%s) AS k(run_id text,window_start double precision,point_id text)
                JOIN trigger_fact t ON t.payload->>'run_id'=k.run_id
                    AND (t.payload->>'window_start')::double precision=k.window_start
                    AND t.payload->>'point_id'=k.point_id
                GROUP BY k.run_id,k.window_start,k.point_id""",
                (Jsonb(keys),),
            ).fetchall()

    def metrics(self, points=()):
        with self.session() as conn:
            size = conn.execute("SELECT pg_database_size(current_database()) AS bytes").fetchone()
            size["tables"] = conn.execute(
                "SELECT relname,n_live_tup,n_dead_tup,last_autovacuum::text FROM pg_stat_user_tables"
            ).fetchall()
            size["last_frame_saved_at"] = conn.execute(
                "SELECT extract(epoch FROM max(saved_at))::double precision AS t FROM frame"
            ).fetchone()["t"]
            rows = conn.execute(
                """SELECT c.point_id,least(c.retained,2592000) AS covered_seconds,
                first_frame.window_start AS first_window,last_frame.window_end AS last_window,
                CASE WHEN c.retained<=2592000 THEN c.retained_good
                     ELSE c.total_good-first_value.good_prefix+CASE WHEN first_value.payload->>'quality'='GOOD' THEN 1 ELSE 0 END END AS good_seconds
                FROM source_counter c
                LEFT JOIN LATERAL (SELECT frame_id,good_prefix,payload FROM frame_value
                    WHERE point_id=c.point_id AND product='source' AND source_seq>=greatest(1,c.last_seq-2592000+1)
                    ORDER BY source_seq LIMIT 1) first_value ON true
                LEFT JOIN frame first_frame ON first_frame.id=first_value.frame_id
                LEFT JOIN frame_value last_value ON last_value.point_id=c.point_id AND last_value.product='source' AND last_value.source_seq=c.last_seq
                LEFT JOIN frame last_frame ON last_frame.id=last_value.frame_id
                WHERE c.point_id=ANY(%s)""",
                ([p.id for p in points],),
            ).fetchall()
            indexed = {r["point_id"]: r for r in rows}
            size["seed"] = [
                indexed.get(
                    p.id,
                    {
                        "point_id": p.id,
                        "covered_seconds": 0,
                        "good_seconds": 0,
                        "first_window": None,
                        "last_window": None,
                    },
                )
                for p in points
            ]
            size["seed_order"] = "COMMITTED_SEQUENCE; endpoints are not continuous wall-clock coverage"
            size["measured_at"] = time.time()
            return size

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
                    "ON CONFLICT(username) DO UPDATE SET role=excluded.role,salt=excluded.salt,digest=excluded.digest,active=excluded.active",
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
                    "SELECT id,time,actor,action,target,result FROM management_log ORDER BY time DESC,id DESC LIMIT 200"
                ).fetchall()
            }

    def certify_recomputable(self, frame_id: str):
        """Conservative proof: stored raw inputs exist; cross-frame algorithms remain protected."""
        with self.session() as conn:
            frame = conn.execute("SELECT config_id FROM frame WHERE id=%s", (frame_id,)).fetchone()
            if not frame:
                return False
            config = conn.execute(
                "SELECT payload FROM config_snapshot WHERE id=%s", (frame["config_id"],)
            ).fetchone()
            if not config or any(
                x["operation"] in ("derivative", "unconfigured") for x in config["payload"]["derived"]
            ):
                return False
            rows = conn.execute(
                "SELECT payload FROM frame_value WHERE frame_id=%s AND product='source'", (frame_id,)
            ).fetchall()
            ids = {item for r in rows for item in r["payload"].get("sample_ids", [])}
            if (
                not rows
                or not ids
                or any(not r["payload"].get("sample_ids") or r["payload"]["quality"] != "GOOD" for r in rows)
            ):
                return False
            count = conn.execute(
                "SELECT count(*) AS n FROM source_sample WHERE id=ANY(%s)", (list(ids),)
            ).fetchone()["n"]
            if count != len(ids):
                return False
            conn.execute("UPDATE event_frame SET recomputable=true WHERE frame_id=%s", (frame_id,))
            return True

    def cleanup(
        self,
        now: float,
        pressure=False,
        seed_count=2592000,
        month_seconds=2592000,
        batch=1024,
        budget_ms=None,
    ):
        """No loops chasing OS free space. Never call shortened retention settings in production."""
        month, year = now - month_seconds, now - 365 * 86400
        with self.session() as conn:
            deadline = None if budget_ms is None else time.monotonic() + budget_ms / 1000

            def execute(query, params=()):
                if deadline is not None:
                    remaining = int((deadline - time.monotonic()) * 1000)
                    if remaining <= 0:
                        raise psycopg.errors.QueryCanceled("MAINTENANCE_BUDGET_EXHAUSTED")
                    conn.execute("SELECT set_config('statement_timeout',%s,true)", (str(remaining),))
                return conn.execute(query, params)

            # Source history that is either within the recent month or the last N committed
            # distinct frames of its point is protected, even after prolonged archive silence.
            cutoff = month if pressure else year
            rows = execute(
                """WITH candidates AS (
                    SELECT q.ingest_id FROM source_counter c CROSS JOIN LATERAL (
                        SELECT v.ingest_id FROM frame_value v JOIN frame f ON f.id=v.frame_id
                        WHERE v.point_id=c.point_id AND v.product='source' AND v.source_seq<=c.last_seq-%s
                        AND f.window_end<%s AND NOT EXISTS(SELECT 1 FROM event_frame e WHERE e.frame_id=f.id)
                        ORDER BY v.source_seq LIMIT %s
                    ) q
                    UNION ALL
                    (SELECT v.ingest_id FROM frame f JOIN frame_value v ON v.frame_id=f.id
                        WHERE v.product='derived' AND f.window_end<%s
                        AND NOT EXISTS(SELECT 1 FROM event_frame e WHERE e.frame_id=f.id)
                        ORDER BY f.window_end,f.id LIMIT %s)
                ) SELECT ingest_id FROM candidates ORDER BY ingest_id LIMIT %s""",
                (seed_count, cutoff, batch, cutoff, batch, batch),
            ).fetchall()
            removed = len(rows)
            if rows:
                execute("DELETE FROM frame_value WHERE ingest_id=ANY(%s)", ([r["ingest_id"] for r in rows],))
            results = 0
            if not rows:
                # Events must all be CLOSED, with no unfinished archive. A single protected
                # association prevents deletion of a shared result.
                frames = execute(
                    """WITH watermarks AS MATERIALIZED (SELECT point_id,last_seq-%s AS boundary FROM source_counter)
                    SELECT f.id FROM frame f WHERE f.window_end < %s
                    AND EXISTS(SELECT 1 FROM event_frame e WHERE e.frame_id=f.id AND NOT e.results_removed)
                    AND NOT EXISTS(SELECT 1 FROM event_frame e LEFT JOIN event ev ON ev.id=e.event_id
                        WHERE e.frame_id=f.id AND ((NOT e.recomputable AND f.window_end >= %s) OR ev.id IS NULL OR ev.phase<>'CLOSED'
                          OR ev.payload->>'archive_status'='PENDING'))
                    AND NOT EXISTS(SELECT 1 FROM frame_value v LEFT JOIN watermarks w ON w.point_id=v.point_id
                        WHERE v.frame_id=f.id AND v.product='source'
                        AND (v.source_seq IS NULL OR w.boundary IS NULL OR v.source_seq>w.boundary))
                    ORDER BY f.window_start LIMIT %s""",
                    (seed_count, cutoff, year, batch),
                ).fetchall()
                if frames:
                    ids = [row["id"] for row in frames]
                    execute("DELETE FROM frame_value WHERE frame_id=ANY(%s)", (ids,))
                    execute("UPDATE event_frame SET results_removed=true WHERE frame_id=ANY(%s)", (ids,))
                results = len(frames)
            # Minimum one year from initial persistence; unknown or open references protected.
            expired = execute(
                """SELECT s.id FROM source_sample s WHERE s.saved_at < to_timestamp(%s)
                AND s.payload->>'archive_scope' IS DISTINCT FROM 'continuous'
                AND NOT EXISTS(SELECT 1 FROM event_sample es LEFT JOIN event e ON e.id=es.event_id
                    WHERE es.sample_id=s.id AND (e.id IS NULL OR e.phase<>'CLOSED' OR e.payload->>'archive_status'='PENDING'))
                AND NOT EXISTS(SELECT 1 FROM event_sample es JOIN event_frame ef ON ef.event_id=es.event_id
                    WHERE es.sample_id=s.id AND NOT ef.results_removed)
                ORDER BY s.saved_at LIMIT %s""",
                (year, batch),
            ).fetchall()
            if expired:
                ids = [r["id"] for r in expired]
                execute("DELETE FROM event_sample WHERE sample_id=ANY(%s)", (ids,))
                execute("DELETE FROM source_sample WHERE id=ANY(%s)", (ids,))
            # Expired event metadata is removed in bounded steps only after its retained
            # source data and results no longer need it. Shared frame rows remain protected.
            execute(
                """DELETE FROM trigger_fact WHERE id IN (
                SELECT t.id FROM trigger_fact t JOIN event e ON e.id=t.event_id
                WHERE t.saved_at<to_timestamp(%s) AND e.phase='CLOSED'
                AND NOT EXISTS(SELECT 1 FROM event_sample s WHERE s.event_id=e.id)
                AND NOT EXISTS(SELECT 1 FROM event_frame f WHERE f.event_id=e.id AND NOT f.results_removed)
                LIMIT %s)""",
                (year, batch),
            )
            execute(
                """DELETE FROM gap WHERE id IN (SELECT g.id FROM gap g WHERE g.saved_at<to_timestamp(%s)
                AND (g.event_id IS NULL OR EXISTS(SELECT 1 FROM event e WHERE e.id=g.event_id AND e.phase='CLOSED'
                    AND NOT EXISTS(SELECT 1 FROM event_sample s WHERE s.event_id=e.id)
                    AND NOT EXISTS(SELECT 1 FROM event_frame f WHERE f.event_id=e.id AND NOT f.results_removed))) LIMIT %s)""",
                (year, batch),
            )
            old_events = execute(
                """SELECT e.id FROM event e WHERE e.saved_at<to_timestamp(%s) AND e.phase='CLOSED'
                AND NOT EXISTS(SELECT 1 FROM event_sample s WHERE s.event_id=e.id)
                AND NOT EXISTS(SELECT 1 FROM event_frame f WHERE f.event_id=e.id AND NOT f.results_removed)
                AND NOT EXISTS(SELECT 1 FROM trigger_fact t WHERE t.event_id=e.id)
                AND NOT EXISTS(SELECT 1 FROM gap g WHERE g.event_id=e.id) LIMIT %s""",
                (year, batch),
            ).fetchall()
            if old_events:
                ids = [e["id"] for e in old_events]
                execute("DELETE FROM event_frame WHERE event_id=ANY(%s)", (ids,))
                execute("DELETE FROM event WHERE id=ANY(%s)", (ids,))
            # Keep frame metadata only when needed by a remaining value or event tombstone.
            execute(
                """DELETE FROM frame WHERE id IN (SELECT f.id FROM frame f WHERE
                NOT EXISTS(SELECT 1 FROM frame_value v WHERE v.frame_id=f.id)
                AND NOT EXISTS(SELECT 1 FROM event_frame e WHERE e.frame_id=f.id) LIMIT %s)""",
                (batch,),
            )
            return {
                "ordinary_removed": removed,
                "event_results_removed": results,
                "source_removed": len(expired),
            }


class Router:
    def __init__(self, config, primary: Store, emergency: Store, external: Store | None = None):
        self.config, self.primary, self.emergency = config, primary, emergency
        self.external = external
        if config.storage.external_enabled and external is None:
            raise ValueError("External storage is enabled but no database was configured")
        self.external_selected = False
        self.mode = "UNKNOWN"
        self.health: dict = {}
        self.bytes_written = 0
        self.baseline: dict[str, int] = {}
        self.ready = set()
        self.last_budget_check = 0.0
        self.development_growth = 0
        self.budget_sizes: dict[str, int] = {}

    @property
    def stores(self):
        return [("primary", self.primary), ("emergency", self.emergency)] + (
            [("external", self.external)] if self.external is not None else []
        )

    def initialize(self):
        self.health.update(
            cleanup_enabled=self.config.storage.cleanup_enabled,
            external_enabled=self.config.storage.external_enabled,
        )
        for name, store in self.stores:
            try:
                store.initialize(self.config)
                metrics = store.metrics(self.config.points)
                self.baseline[name] = metrics["bytes"]
                self.health[name + "_metrics"] = metrics
                self.health[name] = "OK"
                self.ready.add(name)
            except psycopg.Error:
                self.health[name] = "UNAVAILABLE"
        self.reconcile_open_events()
        try:
            self.mode = self.emergency.mode()
            self.external_selected = self.emergency.external_selected()
        except psycopg.Error:
            self.mode = "UNKNOWN"

    def reconcile_open_events(self):
        stores = [store for name, store in self.stores if name in self.ready]
        ids = set()
        for store in stores:
            try:
                ids.update(row["payload"]["id"] for row in store.pending_events())
            except psycopg.Error:
                continue
        for identity in ids:
            candidates = []
            for store in stores:
                try:
                    candidates.extend(row["payload"] for row in store.query("event", event_id=identity))
                except psycopg.Error:
                    continue
            if not candidates:
                continue
            event = max(candidates, key=lambda e: e.get("revision", 0))
            if event["phase"] != "CLOSED" or event.get("archive_status") == "PENDING":
                event.update(
                    phase="CLOSED",
                    close_reason="PROCESS_INTERRUPTED",
                    coverage="PARTIAL",
                    archive_status="INTERRUPTED",
                    revision=event.get("revision", 0) + 1,
                )
            for store in stores:
                try:
                    store.write("event", event)
                except psycopg.Error:
                    self.health["restart_reconciliation"] = "PARTIAL"

    def route(self, primary_used, emergency_free, external_used=None):
        """Inputs can be injected in tests; never simulate by exhausting a physical drive."""
        policy = self.config.storage
        if self.mode == "UNKNOWN":
            self.mode = self.emergency.mode()
            self.external_selected = self.emergency.external_selected()
        if (
            policy.external_enabled
            and self.external is not None
            and primary_used >= policy.external_start
            and not self.external_selected
        ):
            self.emergency.external_selected(select=True)
            self.external_selected = True
        if self.external_selected:
            desired = "EMERGENCY"
            external_safe = external_used is not None and (
                external_used <= policy.cleanup_stop
                if self.mode == "EMERGENCY"
                else external_used < policy.emergency_start
            )
            if policy.external_enabled and self.external is not None and external_safe:
                try:
                    self.external.validate_external_binding(self.config, self.primary, self.emergency)
                    if self.mode != "EXTERNAL":
                        self.external.probe_write()
                except (psycopg.Error, ExternalBindingError) as error:
                    self.health["external"] = type(error).__name__
                else:
                    desired = "EXTERNAL"
                    self.health["external"] = "OK"
            if self.mode != desired:
                self.emergency.mode(desired)
                self.mode = desired
        elif primary_used >= policy.emergency_start and self.mode != "EMERGENCY":
            self.emergency.mode("EMERGENCY")
            self.mode = "EMERGENCY"
        elif primary_used <= policy.cleanup_stop and self.mode == "EMERGENCY":
            try:
                self.primary.probe_write()
            except psycopg.Error as error:
                self.health["switchback"] = type(error).__name__
            else:
                self.emergency.mode("PRIMARY")
                self.mode = "PRIMARY"
                self.health["switchback"] = "OK"
        reserve = policy.emergency_reserve_bytes
        if self.config.mode == "development":
            reserve = 1024**3  # independent absolute safety guard, never a fill target
        self.health["active_target"] = self.mode
        self.health["external_selected"] = self.external_selected
        return self.mode != "EMERGENCY" or emergency_free > reserve + policy.batch_budget_bytes

    def write(self, kind, payload):
        refused = self.write_batch([(kind, payload)])
        if refused:
            raise PolicyRefusal(refused[0][1])

    def write_batch(self, records):
        import json
        import shutil

        if not records:
            return []
        policy = self.config.storage
        size = len(json.dumps(records, allow_nan=False).encode())
        if size * 4 > policy.batch_budget_bytes:
            if len(records) == 1:
                raise PolicyRefusal("WRITE_BATCH_BUDGET")
            mid = len(records) // 2
            return self.write_batch(records[:mid]) + self.write_batch(records[mid:])
        primary = shutil.disk_usage(policy.primary_path)
        emergency = shutil.disk_usage(policy.emergency_path)
        if self.config.mode == "development" and min(primary.free, emergency.free) < 1024**3:
            raise PolicyRefusal("DEVELOPMENT_DISK_RESERVE")
        previous_mode = self.mode
        external_used = None
        if self.external is not None:
            try:
                external_disk = shutil.disk_usage(policy.external_path)
                external_used = 1 - external_disk.free / external_disk.total
                if self.config.mode == "development" and external_disk.free < 1024**3:
                    external_used = 1.0
            except OSError:
                self.health["external"] = "DISK_UNAVAILABLE"
        allowed = self.route(1 - primary.free / primary.total, emergency.free, external_used)
        if self.config.mode == "development" and time.monotonic() - self.last_budget_check > 1:
            total = 0
            for name, store in self.stores:
                try:
                    with store.session() as conn:
                        self.budget_sizes[name] = conn.execute(
                            "SELECT coalesce(sum(pg_total_relation_size(relid)),0)::bigint AS bytes FROM pg_stat_user_tables"
                        ).fetchone()["bytes"]
                except psycopg.Error:
                    if name != "external" or self.mode != "EMERGENCY":
                        raise
                    if name not in self.budget_sizes:
                        raise PolicyRefusal("DEVELOPMENT_BUDGET_UNKNOWN")
                    # Preserve its last observation; include all process writes conservatively.
                    self.health["external_budget"] = "CACHED"
                    total += self.bytes_written * 4
                total += self.budget_sizes.get(name, 0)
            self.development_growth = total
            self.last_budget_check = time.monotonic()
        if self.config.mode == "development" and (
            self.bytes_written + size > policy.development_budget_bytes
            or self.development_growth + size * 4 > policy.development_budget_bytes
        ):
            raise PolicyRefusal("DEVELOPMENT_WRITE_BUDGET_EXHAUSTED")
        if not allowed:
            raise PolicyRefusal("EMERGENCY_RESERVE")
        name = self.mode.lower()
        target = dict(self.stores)[name]
        accepted = [r for r in records if self.mode != "EMERGENCY" or r[0] != "frame"]
        refused = [
            (r, "RESULT_ARCHIVE_SUSPENDED") for r in records if self.mode == "EMERGENCY" and r[0] == "frame"
        ]
        try:
            if name not in self.ready:
                target.initialize(self.config)
                self.ready.add(name)
            if self.mode == "EXTERNAL":
                target.validate_external_binding(self.config, self.primary, self.emergency)
            target.write_batch(accepted)
        except (
            psycopg.OperationalError,
            ExternalBindingError,
            psycopg.errors.ReadOnlySqlTransaction,
            psycopg.errors.InsufficientPrivilege,
        ) as error:
            if self.mode != "EXTERNAL" and (previous_mode != "EMERGENCY" or self.mode != "PRIMARY"):
                raise
            # A successful probe cannot prevent a failure immediately after the switch.
            self.mode = "EMERGENCY"
            self.emergency.mode("EMERGENCY")
            self.health["active_target"] = "EMERGENCY"
            self.health["switchback"] = type(error).__name__
            reserve = 1024**3 if self.config.mode == "development" else policy.emergency_reserve_bytes
            if emergency.free <= reserve + policy.batch_budget_bytes:
                raise PolicyRefusal("EMERGENCY_RESERVE") from error
            accepted = [r for r in records if r[0] != "frame"]
            refused = [(r, "RESULT_ARCHIVE_SUSPENDED") for r in records if r[0] == "frame"]
            self.emergency.write_batch(accepted)
        self.bytes_written += size
        # Certification is conservative maintenance, not part of the committed batch's success.
        if self.config.storage.cleanup_enabled and self.mode != "EMERGENCY":
            for kind, payload in accepted:
                if kind == "frame" and payload["event_ids"]:
                    try:
                        target.certify_recomputable(payload["frame"]["id"])
                    except psycopg.Error as error:
                        self.health["recomputable_check"] = type(error).__name__
        return refused

    def close(self):
        for _, store in self.stores:
            store.close()

    def merged(self, category, **kwargs):
        from .pagination import decode, encode, scope

        requested = kwargs.pop("limit", 100)
        offset = kwargs.pop("offset", 0)
        cursor = kwargs.pop("cursor", None)
        if not 1 <= requested <= 1000 or offset < 0 or requested + offset > 2000 or (cursor and offset):
            raise ValueError("Use next_cursor for traversal; offset and cursor cannot be combined")
        filters = {"start": 0, "end": 1e20, "point": None, "event_id": None, **kwargs}
        identity = scope(category, filters)
        after = decode(cursor, identity, category)
        rows, unavailable = [], []
        for name, store in self.stores:
            try:
                for row in store.query(category, **filters, limit=requested + offset + 1, after=after):
                    row["_instance"] = name
                    rows.append(row)
            except psycopg.Error:
                unavailable.append(name)
        unique = {}
        for row in rows:
            value = row.get("payload", row)
            key = (
                (row["id"], row["point_id"], row["product"])
                if category in ("history", "replay")
                else value.get("id", row.get("frame_id"))
            )
            old = unique.get(key)
            if old and category in ("event", "events"):
                gaps = old.get("has_gaps", False) or row.get("has_gaps", False)
                old["has_gaps"] = row["has_gaps"] = gaps
                if old["payload"].get("revision", 0) > value.get("revision", 0):
                    continue
            if old and category == "history":
                for flag in ("high_recorded", "low_recorded"):
                    row[flag] = row.get(flag, False) or old.get(flag, False)
            unique[key] = row
        ordered = sorted(
            unique.values(), key=lambda r: tuple(r["_key"]), reverse=category in ("event", "events", "faults")
        )
        page = ordered[offset : offset + requested]
        if category == "history" and page:
            keyed = {}
            for row in page:
                key = (row["run_id"], row["window_start"], row["point_id"])
                keyed.setdefault(key, []).append(row)
            keys = [dict(zip(("run_id", "window_start", "point_id"), key)) for key in keyed]
            for name, store in self.stores:
                if name in unavailable:
                    continue
                try:
                    for fact in store.history_flags(keys):
                        key = (fact["run_id"], fact["window_start"], fact["point_id"])
                        for row in keyed[key]:
                            for flag in ("high_recorded", "low_recorded"):
                                row[flag] = row.get(flag, False) or bool(fact[flag])
                except psycopg.Error:
                    unavailable.append(name)
        more = len(ordered) > offset + requested
        next_cursor = encode(page[-1]["_key"], identity) if page and more and not unavailable else None
        for row in page:
            row.pop("_key", None)
        return {
            "items": page,
            "partial": bool(unavailable),
            "unavailable_instances": unavailable,
            "limit": requested,
            "offset": offset,
            "has_more": more,
            "next_cursor": next_cursor,
            "pagination_incomplete": bool(unavailable),
        }

    def maintain(self):
        import shutil

        try:
            used = shutil.disk_usage(self.config.storage.primary_path)
        except OSError as error:
            self.health["disk_measurement"] = type(error).__name__
            return
        ratio = 1 - used.free / used.total
        pressure = self.health.get("cleaning", False)
        if ratio >= self.config.storage.cleanup_start:
            pressure = True
        elif ratio <= self.config.storage.cleanup_stop:
            pressure = False
        self.health["cleaning"] = pressure if self.config.storage.cleanup_enabled else False
        for name, store in self.stores:
            started = time.monotonic()
            try:
                result = (
                    store.cleanup(
                        time.time(),
                        pressure=pressure if name == "primary" else False,
                        batch=self.config.limits.cleanup_batch,
                        budget_ms=int(self.config.limits.maintenance_budget * 1000),
                    )
                    if self.config.storage.cleanup_enabled
                    else {}
                )
                self.health[name + "_cleanup"] = {
                    **result,
                    "seconds": time.monotonic() - started,
                    "batch_saturated": any(n >= self.config.limits.cleanup_batch for n in result.values()),
                }
                metrics = self.health.get(name + "_metrics", {})
                if time.time() - metrics.get("measured_at", 0) > 60:
                    self.health[name + "_metrics"] = store.metrics(self.config.points)
                self.health[name] = "OK"
            except psycopg.errors.QueryCanceled:
                self.health[name] = "MAINTENANCE_TIMEOUT"
            except psycopg.errors.LockNotAvailable:
                self.health[name] = "MAINTENANCE_LOCK_WAIT"
            except psycopg.OperationalError:
                self.health[name] = "UNAVAILABLE"
            except psycopg.Error as error:
                self.health[name] = "MAINTENANCE_ERROR:" + type(error).__name__
