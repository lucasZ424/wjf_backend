"""Routing of whole commit groups to the normal / emergency databases (终版 §5 C3, §13.5).

A group goes to exactly one database.  There is no splitting, no dropping of derived results in the
emergency route, and no cross-database rewrite of a group whose commit outcome is unknown: that one
is verified by its stable id or retried against the same target.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import psycopg

from . import records as rec
from .archive import ArchiveStore, CloseOutDeferred, SchemaError, WriteOutcome
from .config import Settings

DEV_RESERVE = 1024**3  # development: every data volume keeps at least 1 GiB free
PINNED_LIMIT = 256  # groups of unknown commit remembered with their database: the writer settles one at a time, so few are live
STATE_RETRY = 30.0  # seconds between two tries at an unreadable routing state: no group waits on the emergency database (01-3)


class Waiting(RuntimeError):
    """No database may take the group now; it is tried again later, chosen nowhere yet."""


class PolicyRefusal(RuntimeError):
    """A bounded-storage policy refuses the whole group; it is a gap, not a bug."""


def group_bytes(group: rec.CommitGroup) -> int:
    """Conservative size of a group on disk: serialised payload times a measured row+index factor."""
    return int(len(json.dumps(asdict(group), separators=(",", ":"), default=str)) * 2.5)


@dataclass(frozen=True, slots=True)
class RouteResult:
    status: str  # COMMITTED | ALREADY | FAILED | UNKNOWN | REFUSED
    target: str | None = None
    reason: str | None = None
    lacks_baseline: bool = field(default=False, compare=False)  # the target has no retention baseline of its bucket yet

    @property
    def durable(self) -> bool:
        return self.status in ("COMMITTED", "ALREADY")


class ArchiveRouter:
    def __init__(
        self,
        config: Settings,
        primary: ArchiveStore,
        emergency: ArchiveStore,
        *,
        disk_usage: Callable = shutil.disk_usage,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config, self.primary, self.emergency = config, primary, emergency
        self.disk_usage, self.clock = disk_usage, clock
        self.mode = "UNKNOWN"
        self.health: dict = {}
        self.ready: set[str] = set()
        self.bytes_written = 0  # this process, business writes only
        self.development_growth = 0
        self.budget_sizes: dict[str, int] = {}
        self.last_budget_check = -1e9
        self.pinned: dict[str, str] = {}  # group id -> the database its first write went to, until the group is settled
        self.buckets: Callable | None = None  # the maintenance thread's last snapshot, set by the runtime (§13.5)
        # 01-3: the routing state could not be read, and the normal database takes groups for the time being
        self.provisional = False
        self.waiting = False  # 修复轮13: the last group found no database while the routing state could not be read
        self.probed = False  # the normal database passed a write probe since it last failed a write
        self.state_retry_at = -1e9
        self.last_light = self.last_heavy = -1e9

    @property
    def stores(self):
        return [("primary", self.primary), ("emergency", self.emergency)]

    @property
    def target(self) -> str:
        """Where new groups go, as the pages must say it (修复轮12): the normal database for the time being and unchecked
        while the routing state cannot be read (§13.5), UNKNOWN while it cannot and the groups wait - also after a stretch
        for the time being (修复轮13) - else the mode.  Reads from the writer's thread: a switch in between shows for one
        reading.  Only what the pages say: the route itself never reads it."""
        return "UNKNOWN" if self.waiting else "PRIMARY_PROVISIONAL" if self.provisional else self.mode

    def bind_run(self, run_id, config_id, started_wall):
        for _, store in self.stores:
            store.bind_run(run_id, config_id, started_wall)

    def initialize(self, current_run_id: str | None = None):
        self.health.update(cleanup_enabled=self.config.storage.cleanup_enabled)
        readable, unreadable = [], []
        for name, store in self.stores:
            try:
                store.initialize(self.config)
                self.health[name] = "OK"
                self.ready.add(name)
                readable.append((name, store))
            except SchemaError as error:  # a legacy / foreign database is never read from or written to
                self.health[name] = "REFUSED:" + type(error).__name__
                unreadable.append(name)
            except psycopg.Error:
                self.health[name] = "UNAVAILABLE"
                unreadable.append(name)
        self._close_unfinished_runs(current_run_id, readable, unreadable)
        try:
            self.mode = self._read_mode()
        except psycopg.Error:
            self.mode = "UNKNOWN"

    def _read_mode(self) -> str:
        mode = self.emergency.routing_mode()
        if mode not in ("PRIMARY", "EMERGENCY"):  # the removable route of an earlier version: primary was full then
            mode = self.emergency.routing_mode("EMERGENCY")
        return mode

    def _close_unfinished_runs(self, current_run_id, readable, unreadable):
        """Close what a crash left open, with what EVERY bound database knows (终版 §5): a run that ended in one of them
        is not an abnormal exit because the others never heard of its end, and a segment with an end anywhere is not cut.
        While a bound database cannot be read nothing is closed - the next start does it; there is no retry while running.
        A close-out that cannot finish NOW (a statement too slow, a run that failed to close) is `DEFERRED`: it says so and
        which database and why, the database itself is not reported unavailable, and the runs that could be closed are."""
        facts, deferred = {}, []
        for name, store in readable:
            try:
                facts[name] = store.unfinished_facts(current_run_id)
            except psycopg.errors.QueryCanceled as error:  # too slow, not unreadable
                deferred.append(f"{name}:{type(error).__name__}")
            except psycopg.Error:
                self.health[name] = "UNAVAILABLE"
                unreadable.append(name)
        if unreadable:
            self.health["reconcile"] = "PENDING_UNREADABLE:" + ",".join(unreadable)
            return
        if deferred:  # what the other databases hold is not known: closing here could judge a run they ended
            self.health["reconcile"] = "DEFERRED:" + ",".join(deferred)
            return
        self.health.pop("reconcile", None)
        ended_runs = set().union(*(f["ended_runs"] for f in facts.values()))
        ended_segments = set().union(*(f["ended_segments"] for f in facts.values()))
        evidence: dict = {}
        for f in facts.values():
            for key, t in f["evidence"].items():
                evidence[key] = max(t, evidence.get(key, t))
        for name, store in readable:
            try:
                self.health[name + "_closed_runs"] = store.reconcile_unfinished_runs(
                    current_run_id, ended_runs=ended_runs, ended_segments=ended_segments, evidence=evidence
                )
            except CloseOutDeferred as error:  # some runs were closed, the others stay for the next start
                self.health[name + "_closed_runs"] = error.closed
                deferred.append(f"{name}:" + "+".join(sorted(set(error.failed.values()))))
            except psycopg.Error as error:
                deferred.append(f"{name}:{type(error).__name__}")
        if deferred:
            self.health["reconcile"] = "DEFERRED:" + ",".join(deferred)

    # ------------------------------------------------------------------ routing decision

    def route(self, primary_used: float, emergency_free: int | None, bucket: int | None = None) -> bool:
        """Pick the target for the next group.  Inputs are injectable: never fill a real disk to test.  `emergency_free` is None
        when the emergency volume cannot be read: the emergency route is then not allowed, its reserve cannot be checked.
        The emergency route ends below the deletion start line, once the primary has the group's bucket and a write probe
        succeeded (§13.5) - asked in that order, so a missing bucket never switches the route back and forth."""
        policy = self.config.storage
        if self.mode == "UNKNOWN" or self.provisional:
            stored = self._try_state()
            if stored is None:
                self._provisional(primary_used, bucket)
                self.health["active_target"] = "PRIMARY_PROVISIONAL"
                return True
            recovering, self.mode, self.provisional = True, stored, False  # unknown until now: save it, or stay provisional
        else:
            recovering = False
        if primary_used >= policy.emergency_start and self.mode != "EMERGENCY":
            self.emergency.routing_mode("EMERGENCY")
            self.mode = "EMERGENCY"
        elif primary_used < policy.cleanup_start and self.mode == "EMERGENCY":
            try:
                if not self.primary.bucket_takes(bucket):
                    self.health["switchback"] = "NO_BUCKET"
                else:
                    self.primary.probe_write()
                    self.emergency.routing_mode("PRIMARY")
                    self.mode = "PRIMARY"
                    self.health["switchback"] = "OK"
            except psycopg.Error as error:
                self.health["switchback"] = type(error).__name__
                if recovering and self.mode == "EMERGENCY":
                    # the state answered but could not be brought up to date: stay provisional while that stays safe
                    self._provisional(primary_used, bucket)
                    self.health["active_target"] = "PRIMARY_PROVISIONAL"
                    return True
        self.health["active_target"] = self.mode
        self.waiting = False
        return self.mode != "EMERGENCY" or (
            emergency_free is not None and emergency_free > self.reserve() + policy.batch_budget_bytes
        )

    def _try_state(self) -> str | None:
        """The routing state, read again at most every STATE_RETRY seconds while it cannot be; None while it cannot."""
        if self.clock() < self.state_retry_at:
            return None
        try:
            stored = self._read_mode()
        except psycopg.Error as error:
            self.state_retry_at = self.clock() + STATE_RETRY
            self.health["routing_state"] = "UNREADABLE:" + type(error).__name__
            return None
        self.state_retry_at = -1e9
        self.health["routing_state"] = "OK"
        return stored

    def _provisional(self, primary_used: float, bucket: int | None):
        """The normal database for the time being (01-3, 终版 §13.5): below the deletion start line, with the group's bucket,
        after a write probe that no failed write has made stale.  Otherwise the group waits.  Nothing is saved: the state
        cannot be, and the choice is made again once it can be read."""
        try:
            if primary_used >= self.config.storage.cleanup_start:
                raise Waiting("ROUTING_STATE_UNREADABLE:ABOVE_START_LINE")
            try:
                if not self.primary.bucket_takes(bucket):
                    raise Waiting("ROUTING_STATE_UNREADABLE:NO_BUCKET")
                if not self.probed:
                    self.primary.probe_write()
                    self.probed = True
            except psycopg.Error as error:
                raise Waiting("ROUTING_STATE_UNREADABLE:" + type(error).__name__) from None
        except Waiting:
            self.waiting = True  # 修复轮13: said as such, whatever mode a stretch for the time being left behind
            raise
        self.mode, self.provisional, self.waiting = "PRIMARY", True, False

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
        """Write one whole group.  The database its first write went to is remembered until the group is settled (§13.5): a
        retry - FAILED or UNKNOWN - goes to THAT database only, whatever the disks say by then.  An UNKNOWN commit may have
        become durable there, and a copy elsewhere would be the same group stored twice; a FAILED one waits there too, so a
        group's history is never split by the disks of the moment.  The memory ends when the group is durable or refused, and
        it is bounded by `PINNED_LIMIT`: no remembered group is ever dropped to make room.  A NEW group that finds the memory
        full is refused (`PIN_TABLE_FULL`, counted in `health`), because a retry of it could no longer be kept where it was."""
        pinned = self.pinned.get(group.group_id)
        if pinned is None:
            result = self._write_routed(group)
        else:
            result = self._write_to(pinned, group)  # no route, no policy: the target was chosen at the first write
            if result.durable:
                self.bytes_written += group_bytes(group)
        if result.status in ("UNKNOWN", "FAILED") and result.target == "primary":
            self.probed = False  # the next new group for the time being probes the normal database again (01-3)
        waiting = self.health.get("waiting_for_bucket")
        if (result.status, result.target, result.reason) == ("FAILED", "primary", "NO_BUCKET"):
            if not waiting or waiting["group"] != group.group_id:  # one wait: since when its group first found no bucket
                self.health["waiting_for_bucket"] = {"bucket": group.bucket, "group": group.group_id, "since": time.time()}
        if result.status in ("UNKNOWN", "FAILED") and result.target is not None:
            if group.group_id in self.pinned or len(self.pinned) < PINNED_LIMIT:
                self.pinned[group.group_id] = result.target
            else:
                self.health["pin_table_full"] = self.health.get("pin_table_full", 0) + 1
                result = RouteResult("REFUSED", None, "PIN_TABLE_FULL")
        else:  # durable, or refused: settled either way
            self.pinned.pop(group.group_id, None)
            if waiting and waiting["group"] == group.group_id:
                self.health.pop("waiting_for_bucket", None)
        return result

    def release(self, group_id: str):
        """The queue gave this group up (voided behind a lost group): it is never asked about again, so its place is free."""
        self.pinned.pop(group_id, None)
        if self.health.get("waiting_for_bucket", {}).get("group") == group_id:
            self.health.pop("waiting_for_bucket", None)

    def _write_routed(self, group: rec.CommitGroup) -> RouteResult:
        policy = self.config.storage
        size = group_bytes(group)
        if group.bucket is None:  # no bucket was ready when it was built: a gap, acquisition goes on (§13.6)
            return RouteResult("REFUSED", None, "NO_BUCKET")
        if size > policy.batch_budget_bytes:
            return RouteResult("REFUSED", None, "WRITE_BATCH_BUDGET")  # whole group, never split
        try:
            primary = self._disk("primary", policy.primary_path)
            if primary is None:  # the 95 % decision cannot be made without it: the group waits, nothing is guessed
                return RouteResult("FAILED", None, "PRIMARY_DISK_UNAVAILABLE")
            emergency = self._disk("emergency", policy.emergency_path)
            free = [primary.free] if emergency is None else [primary.free, emergency.free]
            if self.config.mode == "development" and min(free) < DEV_RESERVE:
                raise PolicyRefusal("DEVELOPMENT_DISK_RESERVE")
            previous = self.mode
            allowed = self.route(1 - primary.free / primary.total, None if emergency is None else emergency.free, group.bucket)
            if self.config.mode == "development":
                self._development_budget(size)
            if not allowed:
                if emergency is None:  # the target is the emergency route and its reserve cannot be checked: wait, do not refuse
                    return RouteResult("FAILED", None, "EMERGENCY_DISK_UNAVAILABLE")
                raise PolicyRefusal("EMERGENCY_RESERVE")
        except PolicyRefusal as refusal:
            return RouteResult("REFUSED", None, str(refusal))
        except Waiting as waiting:
            return RouteResult("FAILED", None, str(waiting))
        except psycopg.Error as error:
            return RouteResult("FAILED", None, type(error).__name__)
        name = self.mode.lower()
        if name == "primary" and not self.provisional and self.buckets is not None and self.buckets().lacks("primary"):
            # chosen before the first write (§13.5): the primary has no bucket a group may be put in at all - the whole group
            # goes to the emergency database, its bucket unchanged, the route stays where it is.  A primary that lacks only
            # THIS group's bucket is written to and waits there: the maintenance thread prepares what a database lacks (D2)
            if emergency is None:
                return RouteResult("FAILED", None, "EMERGENCY_DISK_UNAVAILABLE")
            if emergency.free <= self.reserve() + policy.batch_budget_bytes:
                return RouteResult("REFUSED", None, "EMERGENCY_RESERVE")
            self.health["no_bucket_to_emergency"] = self.health.get("no_bucket_to_emergency", 0) + 1
            name = "emergency"
        result = self._write_to(name, group)
        if name == "emergency" and (result.status, result.reason) == ("FAILED", "NO_BUCKET"):
            return RouteResult("REFUSED", None, "NO_BUCKET")  # neither database has it: a gap, certain that nothing is stored
        if result.status == "FAILED" and self._may_fall_back(name, previous):
            # nothing was committed (FAILED is certain), so a different target cannot duplicate the group
            self.health["switchback"] = result.reason
            try:
                self.emergency.routing_mode("EMERGENCY")
            except psycopg.Error as error:  # the emergency database cannot be reached either: the group waits, the writer stays
                return RouteResult("FAILED", None, type(error).__name__)
            self.mode = "EMERGENCY"  # only once the switch is recorded
            self.health["active_target"] = "EMERGENCY"
            if emergency is None:
                return RouteResult("FAILED", None, "EMERGENCY_DISK_UNAVAILABLE")
            if emergency.free <= self.reserve() + policy.batch_budget_bytes:
                return RouteResult("REFUSED", None, "EMERGENCY_RESERVE")
            result = self._write_to("emergency", group)
        if result.durable:
            self.bytes_written += size
        return result

    def _disk(self, role: str, path: str):
        """The reading of a storage volume, or None when its path cannot be read (the disk is gone, its letter changed, the folder
        was removed).  That is reported in `health` and routed around - it never ends the writer."""
        try:
            usage = self.disk_usage(path)
        except OSError:
            self.health[role + "_disk"] = "UNAVAILABLE"
            return None
        self.health.pop(role + "_disk", None)
        return usage

    def _may_fall_back(self, name: str, previous: str) -> bool:
        return previous == "EMERGENCY" and name == "primary"

    def _write_to(self, name: str, group: rec.CommitGroup) -> RouteResult:
        target = dict(self.stores)[name]
        try:
            if name not in self.ready:
                target.initialize(self.config)
                self.ready.add(name)
        except (psycopg.Error, SchemaError) as error:
            return RouteResult("FAILED", name, type(error).__name__)
        outcome: WriteOutcome = target.write_group(group)
        if outcome.status == "UNKNOWN":  # COMMIT was in flight: ask the same database, never write elsewhere
            try:
                committed = target.group_committed(group.group_id, group.bucket)
                outcome = WriteOutcome("COMMITTED" if committed else "UNKNOWN")
            except psycopg.Error:
                pass
        if outcome.status == "RETIRED":  # certain: not stored, and never moved into another bucket - a gap (§13.6)
            return RouteResult("REFUSED", name, outcome.error)
        return RouteResult(outcome.status, name, outcome.error, outcome.lacks_baseline)

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

    def verify(self, group_id: str, bucket: int | None) -> str | None:
        """Which database holds the group, by stable id in its bucket (None when none can confirm it)."""
        for name, store in self.stores:
            try:
                if store.group_committed(group_id, bucket):
                    return name
            except psycopg.Error:
                continue
        return None

    def close(self):
        for _, store in self.stores:
            store.close()
