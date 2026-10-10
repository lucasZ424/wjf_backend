"""Typed-record archive store.  Bounded synchronous DB operations: call from worker threads only."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import Settings, Timing

SCHEMA_GENERATION = 5  # continues the legacy schema_version 1-4 numbering


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
