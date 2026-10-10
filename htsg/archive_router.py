"""Routing of whole commit groups to the normal / removable / emergency databases (终版 §5 C3, §13.5).

A group goes to exactly one database.  There is no splitting, no dropping of derived results in the
emergency route, and no cross-database rewrite of a group whose commit outcome is unknown: that one
is verified by its stable id or retried against the same target.
"""

from __future__ import annotations

import json
import ntpath
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

import psycopg

from . import records as rec
from .archive import ArchiveStore, SchemaError, WriteOutcome
from .config import Settings

DEV_RESERVE = 1024**3  # development: every data volume keeps at least 1 GiB free


class PolicyRefusal(RuntimeError):
    """A bounded-storage policy refuses the whole group; it is a gap, not a bug."""


class ExternalBindingError(ValueError):
    pass


def group_bytes(group: rec.CommitGroup) -> int:
    """Conservative size of a group on disk: serialised payload times a measured row+index factor."""
    return int(len(json.dumps(asdict(group), separators=(",", ":"), default=str)) * 2.5)


def _volume(path: str) -> str:
    drive, tail = ntpath.splitdrive(path)
    if not drive or not tail.startswith(("/", "\\")):
        raise ExternalBindingError("External storage requires an absolute Windows volume path")
    return drive.casefold()


def database_identity(store: ArchiveStore) -> tuple[str, str]:
    with store.session() as conn:
        row = conn.execute(
            "SELECT current_setting('data_directory') AS path,current_database() AS name"
        ).fetchone()
    return ntpath.normcase(ntpath.normpath(row["path"])), row["name"]


def validate_external_binding(external: ArchiveStore, config: Settings, primary, emergency):
    """Recheck on every (re)connection; a failed check never authorises a write."""
    with external.session() as conn:
        if getattr(external._local, "binding_verified_connection", None) is conn:
            return
        path, name = database_identity(external)
        verified = conn
    volume = _volume(path)
    if volume != _volume(config.storage.external_path):
        raise ExternalBindingError("External database is not on its configured volume")
    if config.mode == "development" and volume != "d:":
        raise ExternalBindingError("Development external database must be on D:")
    for peer in (primary, emergency):
        peer_path, peer_name = database_identity(peer)
        if (path, name) == (peer_path, peer_name):
            raise ExternalBindingError("External database must be distinct from the existing databases")
        if config.mode == "production" and volume == _volume(peer_path):
            raise ExternalBindingError("Production external database needs a separate volume")
    external._local.binding_verified_connection = verified


@dataclass(frozen=True, slots=True)
class RouteResult:
    status: str  # COMMITTED | ALREADY | FAILED | UNKNOWN | REFUSED
    target: str | None = None
    reason: str | None = None

    @property
    def durable(self) -> bool:
        return self.status in ("COMMITTED", "ALREADY")


class ArchiveRouter:
    def __init__(
        self,
        config: Settings,
        primary: ArchiveStore,
        emergency: ArchiveStore,
        external: ArchiveStore | None = None,
        *,
        disk_usage: Callable = shutil.disk_usage,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config, self.primary, self.emergency, self.external = config, primary, emergency, external
        if config.storage.external_enabled and external is None:
            raise ValueError("External storage is enabled but no database was configured")
        self.disk_usage, self.clock = disk_usage, clock
        self.external_selected = False
        self.mode = "UNKNOWN"
        self.health: dict = {}
        self.ready: set[str] = set()
        self.bytes_written = 0  # this process, business writes only
        self.development_growth = 0
        self.budget_sizes: dict[str, int] = {}
        self.last_budget_check = -1e9
        self.last_light = self.last_heavy = -1e9

    @property
    def stores(self):
        return [("primary", self.primary), ("emergency", self.emergency)] + (
            [("external", self.external)] if self.external is not None else []
        )

    def bind_run(self, run_id, config_id, started_wall):
        for _, store in self.stores:
            store.bind_run(run_id, config_id, started_wall)

    def initialize(self, current_run_id: str | None = None):
        self.health.update(
            cleanup_enabled=self.config.storage.cleanup_enabled,
            external_enabled=self.config.storage.external_enabled,
        )
        for name, store in self.stores:
            try:
                store.initialize(self.config)
                self.health[name] = "OK"
                self.ready.add(name)
                self.health[name + "_closed_runs"] = store.reconcile_unfinished_runs(current_run_id)
            except SchemaError as error:  # a legacy / foreign database is never read from or written to
                self.health[name] = "REFUSED:" + type(error).__name__
            except psycopg.Error:
                self.health[name] = "UNAVAILABLE"
        try:
            self.mode = self.emergency.routing_mode()
            self.external_selected = self.emergency.external_selected()
        except psycopg.Error:
            self.mode = "UNKNOWN"

    # ------------------------------------------------------------------ routing decision

    def route(self, primary_used: float, emergency_free: int, external_used: float | None = None) -> bool:
        """Pick the target for the next group.  Inputs are injectable: never fill a real disk to test."""
        policy = self.config.storage
        if self.mode == "UNKNOWN":
            self.mode = self.emergency.routing_mode()
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
                    validate_external_binding(self.external, self.config, self.primary, self.emergency)
                    if self.mode != "EXTERNAL":
                        self.external.probe_write()
                except (psycopg.Error, ExternalBindingError) as error:
                    self.health["external"] = type(error).__name__
                else:
                    desired = "EXTERNAL"
                    self.health["external"] = "OK"
            if self.mode != desired:
                self.emergency.routing_mode(desired)
                self.mode = desired
        elif primary_used >= policy.emergency_start and self.mode != "EMERGENCY":
            self.emergency.routing_mode("EMERGENCY")
            self.mode = "EMERGENCY"
        elif primary_used <= policy.cleanup_stop and self.mode == "EMERGENCY":
            try:
                self.primary.probe_write()
            except psycopg.Error as error:
                self.health["switchback"] = type(error).__name__
            else:
                self.emergency.routing_mode("PRIMARY")
                self.mode = "PRIMARY"
                self.health["switchback"] = "OK"
        self.health["active_target"] = self.mode
        self.health["external_selected"] = self.external_selected
        return self.mode != "EMERGENCY" or emergency_free > self.reserve() + policy.batch_budget_bytes

    def reserve(self) -> int:
        return DEV_RESERVE if self.config.mode == "development" else self.config.storage.emergency_reserve_bytes

    # ---------------------------------------------------------------------- writing

    def _development_budget(self, size: int):
        """Business writes of this process plus the user tables and indexes already in the bound databases."""
        policy = self.config.storage
        if self.clock() - self.last_budget_check > 1:
            total = 0
            for name, store in self.stores:
                try:
                    with store.session() as conn:
                        self.budget_sizes[name] = conn.execute(
                            "SELECT coalesce(sum(pg_total_relation_size(relid)),0)::bigint AS bytes "
                            "FROM pg_stat_user_tables"
                        ).fetchone()["bytes"]
                except psycopg.Error:
                    if name not in self.budget_sizes:
                        raise PolicyRefusal("DEVELOPMENT_BUDGET_UNKNOWN") from None
                    self.health[name + "_budget"] = "CACHED"  # keep the last observation, count process writes
                    total += self.bytes_written * 4
                total += self.budget_sizes.get(name, 0)
            self.development_growth = total
            self.last_budget_check = self.clock()
        if (
            self.bytes_written + size > policy.development_budget_bytes
            or self.development_growth + size > policy.development_budget_bytes
        ):
            raise PolicyRefusal("DEVELOPMENT_WRITE_BUDGET_EXHAUSTED")

    def write_group(self, group: rec.CommitGroup) -> RouteResult:
        policy = self.config.storage
        size = group_bytes(group)
        if size > policy.batch_budget_bytes:
            return RouteResult("REFUSED", None, "WRITE_BATCH_BUDGET")  # whole group, never split
        try:
            primary = self.disk_usage(policy.primary_path)
            emergency = self.disk_usage(policy.emergency_path)
            if self.config.mode == "development" and min(primary.free, emergency.free) < DEV_RESERVE:
                raise PolicyRefusal("DEVELOPMENT_DISK_RESERVE")
            external_used = None
            if self.external is not None:
                try:
                    disk = self.disk_usage(policy.external_path)
                    external_used = 1 - disk.free / disk.total
                    if self.config.mode == "development" and disk.free < DEV_RESERVE:
                        external_used = 1.0
                except OSError:
                    self.health["external"] = "DISK_UNAVAILABLE"
            previous = self.mode
            allowed = self.route(1 - primary.free / primary.total, emergency.free, external_used)
            if self.config.mode == "development":
                self._development_budget(size)
            if not allowed:
                raise PolicyRefusal("EMERGENCY_RESERVE")
        except PolicyRefusal as refusal:
            return RouteResult("REFUSED", None, str(refusal))
        except psycopg.Error as error:
            return RouteResult("FAILED", None, type(error).__name__)
        name = self.mode.lower()
        result = self._write_to(name, group)
        if result.status == "FAILED" and self._may_fall_back(name, previous):
            # nothing was committed (FAILED is certain), so a different target cannot duplicate the group
            self.mode = "EMERGENCY"
            self.emergency.routing_mode("EMERGENCY")
            self.health["active_target"] = "EMERGENCY"
            self.health["switchback"] = result.reason
            if emergency.free <= self.reserve() + policy.batch_budget_bytes:
                return RouteResult("REFUSED", None, "EMERGENCY_RESERVE")
            result = self._write_to("emergency", group)
        if result.durable:
            self.bytes_written += size
        return result

    def _may_fall_back(self, name: str, previous: str) -> bool:
        return name == "external" or (previous == "EMERGENCY" and name == "primary")

    def _write_to(self, name: str, group: rec.CommitGroup) -> RouteResult:
        target = dict(self.stores)[name]
        try:
            if name not in self.ready:
                target.initialize(self.config)
                self.ready.add(name)
            if name == "external":
                validate_external_binding(target, self.config, self.primary, self.emergency)
        except (psycopg.Error, ExternalBindingError, SchemaError) as error:
            return RouteResult("FAILED", name, type(error).__name__)
        outcome: WriteOutcome = target.write_group(group)
        if outcome.status == "UNKNOWN":  # COMMIT was in flight: ask the same database, never write elsewhere
            try:
                outcome = WriteOutcome("COMMITTED" if target.group_committed(group.group_id) else "UNKNOWN")
            except psycopg.Error:
                pass
        return RouteResult(outcome.status, name, outcome.error)

    def maintain(self, now: float | None = None) -> dict:
        """Slow bookkeeping.  There is NO cleanup in this generation: data is kept, nothing here deletes.

        Light status (memory only) every `status_interval`; heavy database statistics every
        `stats_interval`, so a maintenance step can never sit on the write path once a second.
        """
        now = self.clock() if now is None else now
        t = self.config.timing
        did = {"light": False, "heavy": False}
        if now - self.last_light >= t.status_interval:
            self.last_light, did["light"] = now, True
            self.health["light"] = {
                "mode": self.mode,
                "external_selected": self.external_selected,
                "bytes_written": self.bytes_written,
                "development_growth": self.development_growth,
            }
        if now - self.last_heavy >= t.stats_interval:
            self.last_heavy, did["heavy"] = now, True
            for name, store in self.stores:
                try:
                    self.health[name + "_metrics"] = store.metrics()
                    self.health[name] = "OK" if name in self.ready else self.health.get(name, "OK")
                except psycopg.errors.QueryCanceled:
                    self.health[name] = "MAINTENANCE_TIMEOUT"
                except psycopg.errors.LockNotAvailable:
                    self.health[name] = "MAINTENANCE_LOCK_WAIT"
                except psycopg.Error:
                    self.health[name] = "UNAVAILABLE"
        return did

    def verify(self, group_id: str) -> str | None:
        """Which database holds the group, by stable id (None when none can confirm it)."""
        for name, store in self.stores:
            try:
                if store.group_committed(group_id):
                    return name
            except psycopg.Error:
                continue
        return None

    def close(self):
        for _, store in self.stores:
            store.close()
