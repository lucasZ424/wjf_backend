"""Bucket numbers and prepared buckets: the maintenance thread (终版 §13.6 桶号 / 准备与换桶 / 维护连接; 修复轮09 plan D2-D4, D7, D8).

The thread does database work only - reading the bucket lists, allocating numbers, preparing empty buckets.  It never reads
the state owner's changing state and never decides a route.  What it found is handed back as one immutable `Snapshot`,
replaced whole by a single assignment: a reader takes the reference it finds, there is no lock to wait on.  Every round is
bounded (60 s per statement, 1 s lock wait, on connections of its own) and an error in a round is reported in the next
snapshot and never ends the thread.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import psycopg

from .archive import ArchiveStore
from .config import Settings

log = logging.getLogger(__name__)

PREPARED_AHEAD = 7  # READY buckets kept in hand (§13.6: fixed, no configuration field)
SWITCH_RUN_SECONDS = 12 * 3600  # a bucket holds at least this much of the process running (§13.6: fixed)
SAVE_EVERY = 60.0  # the run time of the current bucket is saved about this often (plan D3)
DELETE_BUDGET = 30.0  # seconds of deletion in one round; then preparing has its turn again (§13.6 维护连接)
ALARM_AFTER = 600.0  # the normal disk above the start line this long is the high-water alarm (§13.6: fixed)
FORECAST_SECONDS = 7 * 86400.0  # the growth of the last seven days foresees the next deletion (§13.6)
SAMPLE_EVERY, SAMPLES = 3600.0, 7 * 24 + 1  # disk and database sizes, hourly over a week, in memory only
OTHER_FILES_FACTOR, OTHER_FILES_FLOOR = 2.0, 1024**3  # the disk grows more than twice the database, and by more than 1 GiB
STATEMENT_SECONDS, LOCK_SECONDS = 60, 1
USABLE = ("READY", "CURRENT")  # where a NEW group may be put
KEEPS_GROUPS = ("READY", "CURRENT", "SEALED")  # a bucket that may still receive a group given to it earlier


class Unreliable(Exception):
    """The number file cannot be used as evidence: unreadable, damaged, or behind a database."""


class NumberFile:
    """The highest bucket number this computer has used.  Written to a temporary file, forced to disk, then put in place of the
    old one: a power loss leaves either the old or the new file, never a torn one in its place."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict | None:
        """The file's content, or None when there is none.  A file that cannot be read or makes no sense raises `Unreliable`:
        it is never taken for number zero."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise Unreliable(f"UNREADABLE:{type(error).__name__}") from error
        try:
            content = json.loads(raw.decode("utf-8"))
            if not isinstance(content, dict) or type(content.get("max_no")) is not int or content["max_no"] < 0:
                raise ValueError("no max_no")
        except (UnicodeDecodeError, ValueError) as error:
            raise Unreliable("DAMAGED") from error
        return content

    def write(self, content: dict):
        temp = self.path.with_name(self.path.name + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp, "wb") as f:
            f.write(json.dumps(content).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)


def local_date(wall: float) -> str:
    """The date of this computer's local time: a bucket ends at a change of it (§13.6)."""
    return time.strftime("%Y-%m-%d", time.localtime(wall))


@dataclass(frozen=True)
class Snapshot:
    """What the maintenance thread found in its last round.  `usable`: per database, the buckets a new group may be put in."""

    usable: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    health: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    taken_at: float = 0.0

    def lacks(self, name: str) -> bool:
        """`name` was read and has no bucket a new group may be put in.  One that could not be read is not known to lack any."""
        return name in self.usable and not self.usable[name] and self.health.get(name) != "UNAVAILABLE"


def alarm_reasons(health: dict, *, usable: bool, other_files: bool) -> list[str]:
    """Why the deletion did not bring the normal disk below the start line, from what the last round said (§13.6 自检与报警)."""
    deletion, reasons = health.get("deletion") or "", []
    if deletion.startswith("POSTPONED:"):
        waited = deletion.split(":", 1)[1] in ("QueryCanceled", "LockNotAvailable")
        reasons.append("POSTPONED_BY_LONG_TRANSACTION" if waited else "DELETION_FAILING")
    elif deletion == "NO_RANGE:NO_BASELINE":
        reasons.append("BOUNDARY_WITHOUT_BASELINE")
    elif deletion.startswith("NO_RANGE:"):
        reasons.append("NO_ELIGIBLE_RANGE")
    elif deletion in ("SWITCHED_OFF", "DISK_UNAVAILABLE"):
        reasons.append("DELETION_SWITCHED_OFF" if deletion == "SWITCHED_OFF" else "DISK_UNAVAILABLE")
    if not usable:
        reasons.append("NO_WRITABLE_BUCKET")
    if other_files:
        reasons.append("OTHER_FILES_GROWING")
    return reasons


def choose_range(rows: list[dict], other: list[dict] | None, unsettled, floor: int):
    """The range to take away now from the normal database: ((numbers, kept bucket), None), or (None, why) (§13.6 可删范围).

    From the oldest bucket left, up to the first used bucket with its baseline that comes after it: that one begins with the
    state in force and is kept.  Every bucket of the range must be sealed, have no group on its way, and have at least `floor`
    newer used sealed buckets - counted over both databases, a number once.  Any one failing stops the whole range."""
    alive = [r for r in rows if r["state"] not in ("DELETING", "DELETED")]
    sealed = {r["no"] for r in [*rows, *(other or [])] if r["used"] and r["state"] in ("SEALED", "DELETING", "DELETED")}
    span, keep = [], None
    for r in alive:
        if span and r["used"] and r["baseline_wall"] is not None:
            keep = r["no"]
            break
        span.append(r)
    if not span:
        return None, "NOTHING"
    if keep is None:
        return None, "NO_BASELINE"
    for r in span:
        if r["state"] != "SEALED":
            return None, "NOT_SEALED"
        if r["no"] in unsettled:
            return None, "UNSETTLED_GROUPS"
        if sum(1 for no in sealed if no > r["no"]) < floor:
            return None, "FLOOR"
    return ([r["no"] for r in span], keep), None


class BucketClock:
    """The bucket the state owner gives its groups, and when it takes the next one (§13.6 准备与换桶; plan D3, D4).

    Event loop only.  It reads the maintenance thread's latest `snapshot` and `resume`; the maintenance thread reads `progress`
    and sets `persisted` once the run time is on disk.  Each is one reference, assigned whole: nothing here waits.  The next
    bucket is taken at a batch boundary (`tick`) when the local date has changed since the current one began AND twelve hours of
    its run time are on disk AND the next one is ready; otherwise the current one is used longer.  Run time is the process
    running (monotonic), never the wall clock."""

    def __init__(self, source):
        self.source = source  # has .snapshot and .resume (the Maintenance)
        self.current: int | None = None
        self.run_seconds, self.date, self.last_mono = 0.0, None, None
        self.persisted: tuple | None = None  # (bucket, run seconds) confirmed on disk by the maintenance thread
        self.progress: tuple = (None, 0.0, None)

    def label(self) -> int | None:
        return self.current

    def tick(self, wall: float, mono: float):
        if self.current is not None and self.last_mono is not None:
            self.run_seconds += max(0.0, mono - self.last_mono)
        self.last_mono = mono
        usable = self.source.snapshot.usable
        if self.current is None:
            self._adopt(usable, wall)
        elif local_date(wall) != self.date and self._twelve_hours_on_disk():
            following = self._first(usable, above=self.current)
            if following is not None:
                self.current, self.run_seconds, self.date = following, 0.0, local_date(wall)
        self.progress = (self.current, self.run_seconds, self.date)

    def _twelve_hours_on_disk(self) -> bool:
        return self.persisted is not None and self.persisted[0] == self.current and self.persisted[1] >= SWITCH_RUN_SECONDS

    @staticmethod
    def _first(usable, above=0) -> int | None:
        """The lowest ready bucket above `above`: the normal database's first, the emergency database's when it has none."""
        for name in ("primary", "emergency"):
            found = sorted(no for no in usable.get(name, ()) if no > above)
            if found:
                return found[0]
        return None

    def _adopt(self, usable, wall: float):
        """No bucket yet: the one in use before the restart, with its run time, while it can still take new groups; else the
        first ready one."""
        resume = self.source.resume
        if resume and any(resume["current"] in usable.get(name, ()) for name in ("primary", "emergency")):
            self.current, self.run_seconds, self.date = resume["current"], float(resume["run_seconds"]), resume["date"]
            return
        first = self._first(usable)
        if first is not None:
            self.current, self.run_seconds, self.date = first, 0.0, local_date(wall)


class Maintenance:
    def __init__(
        self, config: Settings, dsns: dict[str, str], number_file: Path, *, interval: float = 5.0, monotonic=time.monotonic,
        disk_usage=shutil.disk_usage, statement_seconds: float = STATEMENT_SECONDS,
    ):  # fmt: skip
        timing = config.timing.model_copy(update={"db_statement_timeout": statement_seconds, "db_lock_timeout": LOCK_SECONDS})
        self.stores = {name: ArchiveStore(dsn, timing) for name, dsn in dsns.items()}  # its own connections
        self.numbers = NumberFile(number_file)
        self.policy, self.disk_usage = config.storage, disk_usage
        self.interval, self.monotonic = interval, monotonic
        self.unsettled: frozenset = frozenset()  # buckets of groups still on their way, set by the event loop
        self._deleting_down: bool | None = None  # a cleanup started at the start line goes on until below the stop line;
        # None: not read yet - one the management log says is going on was begun before a restart and goes on (§13.6)
        self._open_range: list | None = None  # the range marked and not yet all taken away by this process
        self._above_since = None  # monotonic time the normal disk went above the start line
        self._samples: list = []  # (wall, disk used bytes, database bytes), hourly
        self._spans: dict = {}  # sealed bucket -> (first wall, last wall, bytes): fixed once sealed
        self.snapshot = Snapshot()
        self.usable = threading.Event()  # set once a database has a bucket a new group may be put in (D7)
        self.clock: BucketClock | None = None  # whose run time is saved
        self.resume: dict | None = None  # the bucket in use before the restart, read once from the file
        self._resume_read = False
        self._saved_no, self._last_save = None, None
        self._stop = threading.Event()
        self._health: dict = {}
        self.thread = threading.Thread(target=self._run, name="bucket maintenance", daemon=True)

    # ------------------------------------------------------------------ the thread

    def start(self):
        self.thread.start()

    def request_stop(self):
        """No step begins after the one under way: a stop says this first, so the thread ends while the rest stops."""
        self._stop.set()

    def stop(self, timeout: float) -> bool:
        """Ends the thread after the step under way and says whether it ended within `timeout` - a wait that ran out is not
        an end.  The thread closes its own connections on its way out, never under a step still running."""
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout)
        if self.thread.is_alive():
            return False
        self._close()  # a thread never started has nothing running on them
        return True

    def _close(self):
        for store in self.stores.values():
            store.close()

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self.cycle()
                except Exception as error:  # a round that went wrong is reported; the thread goes on with the next one
                    log.exception("bucket maintenance round failed")
                    self._health["error"] = f"{type(error).__name__}: {error}"[:200]
                    self._publish(None)
                self._stop.wait(self.interval)
            try:
                self._save_progress(force=True)
            except Exception:
                log.exception("run time not saved at the stop")
        finally:
            self._close()

    # ------------------------------------------------------------------ one round

    def cycle(self):
        """One round.  A stop is looked at between its steps: whatever is left waits for the next run, a range marked DELETING
        stays marked.  A bucket that can take a group is announced as soon as it is known, not at the end of the round."""
        self._read_resume()
        catalogs = self._read_catalogs()
        self._publish(catalogs)
        for step in (self._fill_missing, self._prepare_ahead, lambda _: self._save_progress(), self._delete, self._report):
            if self._stop.is_set():
                return
            step(catalogs)
        self._publish(catalogs)

    def _read_resume(self):
        """Once: the bucket and run time the file says were in use.  A file that cannot be trusted resumes nothing - the
        bucket in use starts its count again, so it is only used longer."""
        if self._resume_read:
            return
        self._resume_read = True
        try:
            content = self.numbers.read()
        except Unreliable:
            return
        if content and type(content.get("current")) is int and isinstance(content.get("run_seconds"), (int, float)):
            self.resume = {"current": content["current"], "run_seconds": content["run_seconds"], "date": content.get("date")}

    def _save_progress(self, force: bool = False):
        """The current bucket's run time, as a total, about every `SAVE_EVERY` seconds and at once when the bucket changed.  It
        is confirmed back to the clock only once it is on disk; over a file that cannot be trusted nothing is written."""
        clock = self.clock
        if clock is None:
            return
        no, seconds, date = clock.progress
        if no is None:
            return
        now = self.monotonic()
        if not force and no == self._saved_no and now - self._last_save < SAVE_EVERY:
            return
        try:
            content = self.numbers.read()
            if content is None:
                raise Unreliable("NO_FILE")
        except Unreliable as error:
            self._health["progress"] = f"NOT_SAVED:{error}"
            return
        try:
            self.numbers.write({**content, "current": no, "run_seconds": seconds, "date": date})
        except OSError as error:
            self._health["progress"] = f"NOT_SAVED:{type(error).__name__}"
            return
        clock.persisted = (no, seconds)
        self._saved_no, self._last_save = no, now
        self._health["progress"] = "OK"

    def _read_catalogs(self) -> dict:
        """Per database its bucket list {no: state}, or None when it cannot be read now."""
        catalogs = {}
        for name, store in self.stores.items():
            try:
                catalogs[name] = store.bucket_catalog()
                self._health[name] = "OK"
            except psycopg.Error as error:
                catalogs[name] = None
                self._health[name] = "UNAVAILABLE"
                log.debug("bucket list of %s unreadable: %s", name, error)
        return catalogs

    def _prepare(self, name: str, no: int, catalogs: dict):
        try:
            self.stores[name].prepare_bucket(no)
        except psycopg.Error as error:
            self._health[name] = f"PREPARE_FAILED:{type(error).__name__}"
            return
        if catalogs[name] is not None:
            catalogs[name][no] = "READY"
            self._publish(catalogs)  # usable now: a start waiting for its first bucket does not wait for the others (D7)

    def _fill_missing(self, catalogs: dict):
        """A database that was away when buckets were prepared gets them now: every bucket another database has that may still
        receive a group, above the last one this database has deleted (plan D2)."""
        for name, mine in catalogs.items():
            if mine is None:
                continue
            gone = max((no for no, state in mine.items() if state not in KEEPS_GROUPS), default=0)
            wanted = {
                no
                for other, theirs in catalogs.items()
                if other != name and theirs is not None
                for no, state in theirs.items()
                if state in KEEPS_GROUPS and no > gone and no not in mine
            }
            for no in sorted(wanted):
                if self._stop.is_set():
                    return
                self._prepare(name, no, catalogs)

    def _prepare_ahead(self, catalogs: dict):
        reference = next((catalogs[n] for n in ("primary", "emergency") if catalogs.get(n) is not None), None)
        if reference is None:
            return
        current = max((no for no, state in reference.items() if state == "CURRENT"), default=0)
        wanted = PREPARED_AHEAD - sum(1 for no, state in reference.items() if state == "READY" and no > current)
        for n in range(max(0, wanted)):
            if self._stop.is_set():
                return
            allocated = self._allocate(catalogs)
            if allocated is None:
                return
            no, evidence = allocated
            if n == 0:  # what the round's first number rested on: a first start, a rebuilt file, or the file
                self._health["numbering"] = evidence
            for name, mine in catalogs.items():
                if mine is not None:
                    self._prepare(name, no, catalogs)
            if no not in reference:  # it could not be prepared where it counts: no more numbers this round
                return

    def _allocate(self, catalogs: dict) -> tuple[int, str] | None:
        """The next number and its evidence, or None when it cannot be given safely now (§13.6 桶号).  It is the highest number used by any
        database that can be read or by the file, plus one, and it is on disk before it is used.  The file is only evidence when
        it is readable and not behind a database; without that evidence both databases must be read, or nothing is given."""
        online = [mine for mine in catalogs.values() if mine is not None]
        highest = max((max(mine, default=0) for mine in online), default=0)
        try:
            content = self.numbers.read()
            reason = None if content is not None else "NO_FILE"
            if content is not None and content["max_no"] < highest:
                reason = "FILE_BEHIND"
        except Unreliable as error:
            content, reason = None, f"FILE_{error}"
        everything_read = len(online) == len(catalogs)
        if reason is not None:
            if not everything_read:
                self._health["numbering"] = f"PAUSED:{reason}"
                return None
            evidence = "FIRST_START" if reason == "NO_FILE" and highest == 0 else "FILE_REBUILT"
        else:
            evidence = "OK"
        no = max(highest, content["max_no"] if content else 0) + 1
        try:
            self.numbers.write({**(content or {}), "max_no": no})
        except OSError as error:
            self._health["numbering"] = f"FILE_NOT_WRITTEN:{type(error).__name__}"
            return None
        return no, evidence

    # ------------------------------------------------------------------ the rolling deletion (§13.6), normal database only

    def _usage(self) -> float | None:
        try:
            disk = self.disk_usage(self.policy.primary_path)
        except OSError:
            return None
        return 1 - disk.free / disk.total

    def _delete(self, catalogs: dict):
        """A range marked DELETING is finished first, whatever the switch and the disk say.  A new range is chosen only with
        the switch on, from the start line until below the stop line, and within this round's budget."""
        store, mine = self.stores.get("primary"), catalogs.get("primary")
        if store is None or mine is None:
            return
        deadline = self.monotonic() + DELETE_BUDGET
        resumed = []
        for no in sorted(n for n, state in mine.items() if state == "DELETING"):
            if not self._may_go_on(deadline) or not self._retire(store, no):
                return
            resumed.append(no)
        if resumed:  # a range finished by a later round is logged once, whole when this process marked it
            self._log_range(store, self._open_range or resumed)
            self._open_range = None
        if self._deleting_down is None:
            try:
                self._deleting_down = store.cleanup_going_on()
            except psycopg.Error as error:
                self._health["deletion"] = f"POSTPONED:{type(error).__name__}"
                return
        if not self.policy.cleanup_enabled:
            self._health["deletion"] = "SWITCHED_OFF"
            return
        while self._may_go_on(deadline):
            usage = self._usage()
            if usage is None:
                self._health["deletion"] = "DISK_UNAVAILABLE"
                return
            if usage < (self.policy.cleanup_stop if self._deleting_down else self.policy.cleanup_start):
                if self._deleting_down and not self._log_cleanup(store, "ROLLING_DELETE_END"):
                    return  # the end is said once it is in the log: the next round tries again
                self._deleting_down = False
                self._health["deletion"] = "IDLE"
                return
            if not self._deleting_down and not self._log_cleanup(store, "ROLLING_DELETE_START"):
                return  # a cleanup that is not in the log has not begun: the period it gives must count it
            self._deleting_down = True
            try:
                rows = store.bucket_rows()
                other = self._other_rows()
                found, why = choose_range(rows, other, self.unsettled, self.policy.retention_min_buckets)
                if found is None:
                    self._health["deletion"] = f"NO_RANGE:{why}"
                    return
                nos, keep = found
                if not store.mark_deleting(nos, keep):
                    self._health["deletion"] = "NO_RANGE:CHANGED"
                    return
            except psycopg.Error as error:
                self._health["deletion"] = f"POSTPONED:{type(error).__name__}"
                return
            self._open_range = nos
            for no in nos:
                if not self._may_go_on(deadline) or not self._retire(store, no):
                    return  # the rest of the range stays DELETING: the next round finishes it first
            self._log_range(store, nos)
            self._open_range = None
        if not self._stop.is_set():
            self._health["deletion"] = "BUDGET_USED"

    def _may_go_on(self, deadline: float) -> bool:
        """Another bucket may be taken away in this round: no stop asked for and the round's budget not used up."""
        if self._stop.is_set():
            return False
        if self.monotonic() >= deadline:
            self._health["deletion"] = "BUDGET_USED"
            return False
        return True

    # ------------------------------------------------------------------ what the self-check page shows (§13.6 自检与报警)

    def _report(self, catalogs: dict):
        now_m, usage = self.monotonic(), self._usage()
        if usage is not None and usage >= self.policy.cleanup_start:
            self._above_since = now_m if self._above_since is None else self._above_since
        else:
            self._above_since = None
        primary = catalogs.get("primary")
        usable = primary is not None and any(state in USABLE for state in primary.values())
        try:
            report = self._read_report(usage)
        except psycopg.Error as error:
            self._health["retention_report"] = type(error).__name__
            report = dict(self._health.get("retention") or {})
        active = self._above_since is not None and now_m - self._above_since >= ALARM_AFTER
        report["alarm"] = {
            "active": active,
            "above_seconds": None if self._above_since is None else now_m - self._above_since,
            "reasons": alarm_reasons(self._health, usable=usable, other_files=self._other_files()) if active else [],
        }
        report["emergency_route"] = report.get("emergency_route", False)
        report["disk"] = self._volumes()
        self._health["retention"] = report

    def _volumes(self) -> dict:
        """Both storage volumes as this round read them, with the lines they are judged by (修复轮12, 第15节 磁盘占用).  A volume
        that cannot be read is said to be so; it never ends the round."""
        volumes = {"measured_at": time.time(), "cleanup_start": self.policy.cleanup_start, "cleanup_stop": self.policy.cleanup_stop,
                   "emergency_start": self.policy.emergency_start, "emergency_reserve_bytes": self.policy.emergency_reserve_bytes}  # fmt: skip
        for role, path in (("primary", self.policy.primary_path), ("emergency", self.policy.emergency_path)):
            try:
                disk = self.disk_usage(path)
            except OSError:
                volumes[role] = None
                continue
            volumes[role] = {"total": disk.total, "free": disk.free, "used_fraction": 1 - disk.free / disk.total}
        return volumes

    def _read_report(self, usage) -> dict:
        databases, rows = {}, {}
        for name, store in self.stores.items():
            try:
                rows[name] = store.bucket_rows()
            except psycopg.Error:
                databases[name] = {"readable": False}
                continue
            databases[name] = self._kept(store, rows[name])
        report = {"databases": databases, "last_deletion": None, "deletion_period_s": None}
        store = self.stores.get("primary")
        if store is not None and "primary" in rows:
            times = store.deletion_times()
            report["last_deletion"] = times[-1] if times else None
            starts = store.deletion_times("ROLLING_DELETE_START")  # one per cleanup, however long it took
            report["deletion_period_s"] = starts[-1] - starts[-2] if len(starts) >= 2 else None
            self._sample(store, usage)
            report["forecast"] = self._forecast(store, rows["primary"], usage)
            try:
                report["emergency_route"] = self.stores["emergency"].routing_mode() == "EMERGENCY"
            except (KeyError, psycopg.Error):
                report["emergency_route"] = None
        return report

    def _kept(self, store, rows) -> dict:
        kept = [r for r in rows if r["used"] and r["state"] in ("CURRENT", "SEALED")]
        if not kept:
            return {"readable": True, "kept_buckets": 0, "kept_from": None, "boundary": None}
        first, _ = store.bucket_span(kept[0]["no"])
        return {"readable": True, "kept_buckets": len(kept), "kept_from": first, "boundary": kept[0]["baseline_wall"]}

    def _sample(self, store, usage):
        if usage is None or (self._samples and time.time() - self._samples[-1][0] < SAMPLE_EVERY):
            return
        disk = self.disk_usage(self.policy.primary_path)
        self._samples = [*self._samples, (time.time(), disk.used, store.metrics()["bytes"])][-SAMPLES:]

    def _other_files(self) -> bool:
        """The disk grew more than twice as much as the database over at least a day of samples: something else fills it."""
        if len(self._samples) < 2 or self._samples[-1][0] - self._samples[0][0] < 86400:
            return False
        (_, disk0, db0), (_, disk1, db1) = self._samples[0], self._samples[-1]
        grown = disk1 - disk0
        return grown > OTHER_FILES_FLOOR and grown > OTHER_FILES_FACTOR * max(db1 - db0, 0)

    def _forecast(self, store, rows, usage) -> dict:
        """The next deletion from the growth of the last seven days: the size of the buckets sealed in them over the days they
        really cover, against the room left below the start line.  Said plainly when it cannot be foreseen reliably."""
        none = lambda reason: {"next_deletion": None, "reason": reason}  # noqa: E731
        now = time.time()
        since = now - FORECAST_SECONDS
        if usage is None:
            return none("DISK_UNAVAILABLE")
        if self.stores.get("emergency") is not None:
            try:
                if self.stores["emergency"].routing_mode() == "EMERGENCY":
                    return none("EMERGENCY")
            except psycopg.Error:
                pass
        spans = []
        for r in rows:
            if r["used"] and r["state"] == "SEALED":
                if r["no"] not in self._spans:
                    first, last = store.bucket_span(r["no"])
                    self._spans[r["no"]] = (first, last, store.bucket_bytes(r["no"]))
                spans.append(self._spans[r["no"]])
        oldest = min([s[0] for s in spans if s[0] is not None] + [r["first_wall"] for r in rows if r["first_wall"]], default=None)
        if oldest is None or oldest > since:
            return none("INSUFFICIENT_DATA")
        recent = [s for s in spans if s[1] is not None and s[1] >= since]
        covered = max((s[1] for s in recent), default=since) - max(min((s[0] for s in recent), default=now), since)
        size = sum(s[2] for s in recent)
        if covered <= 0 or size <= 0:
            return none("NO_GROWTH")
        if self._other_files():
            return none("DISK_GROWS_FASTER")
        per_day = size / (covered / 86400)
        disk = self.disk_usage(self.policy.primary_path)
        room = max(0.0, self.policy.cleanup_start - usage) * disk.total
        return {"next_deletion": now + room / per_day * 86400, "reason": None, "bytes_per_day": per_day}

    def _other_rows(self):
        other = self.stores.get("emergency")
        try:
            return None if other is None else other.bucket_rows()
        except psycopg.Error:
            return None  # the floor is then counted from the normal database alone: fewer, never more

    def _retire(self, store, no) -> bool:
        try:
            done = store.retire_bucket(no)
        except psycopg.Error as error:  # a long transaction, a lock: the bucket stays DELETING, the next round goes on
            self._health["deletion"] = f"POSTPONED:{type(error).__name__}"
            return False
        if not done:
            self._health["deletion"] = f"POSTPONED:PARTITIONS_LEFT:{no}"
            return False
        self._health["last_deleted"] = {"bucket": no, "at": time.time()}
        return True

    def _log_cleanup(self, store, action) -> bool:
        """The start or the end of one cleanup in the management log (§13.6 实测删除周期); False when it cannot be written."""
        now = time.time()
        entry = {"id": f"rolling-delete/{action.lower()}/{now:.6f}", "time": now, "actor": "system", "action": action,
                 "target": "cleanup", "result": "OK"}  # fmt: skip
        try:
            store.audit(entry)
        except psycopg.Error as error:
            self._health["deletion"] = f"POSTPONED:{type(error).__name__}"
            return False
        return True

    def _log_range(self, store, nos):
        now = time.time()
        entry = {"id": f"rolling-delete/{nos[0]}-{nos[-1]}/{now:.6f}", "time": now, "actor": "system",
                 "action": "ROLLING_DELETE", "target": f"buckets {nos[0]}-{nos[-1]}", "result": "OK"}  # fmt: skip
        try:
            store.audit(entry)
        except psycopg.Error as error:
            self._health["deletion_log"] = type(error).__name__

    def _publish(self, catalogs: dict | None):
        usable = {}
        for name in self.stores:
            mine = (catalogs or {}).get(name)
            if mine is None:
                usable[name] = self.snapshot.usable.get(name, frozenset()) if catalogs is None else frozenset()
            else:
                usable[name] = frozenset(no for no, state in mine.items() if state in USABLE)
        self.snapshot = Snapshot(MappingProxyType(usable), MappingProxyType(dict(self._health)), time.time())
        if any(usable.values()):
            self.usable.set()
