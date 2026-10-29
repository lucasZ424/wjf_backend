"""Single-owner state engine (终版 §5-§8, §10, §11).

Everything here is synchronous: no await, no database access, no clock reads.  Callers pass the
wall (UTC, labels only) and monotonic (durations) time of every event, so the engine is driven
identically by the live runtime and by tests.  Results leave through two sinks: `commit` receives
ordered CommitGroups (durable), `publish` receives PublishGroups (realtime).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from uuid import uuid4

from . import records as rec
from .commlog import CommLog, clean_detail
from .config import Settings, fault_rule
from .derive import Input, Outcome, Topology, evaluate, unavailable
from .extremes import is_extreme
from .fault_tracker import FaultTracker

# Source data states.  Only VALID sources contribute usable values.
#   WAITING  new connection / subscription generation, nothing received yet (a Modbus round nobody answered is not "received")
#   VALID    data usable
#   EXPIRED  OPC UA quiet for the data validity (3 s): data unusable, same generation may recover
#   RESYNC   definite loss or long silence: a new generation must be built (C1), no Republish
WAITING, VALID, EXPIRED, RESYNC = "WAITING", "VALID", "EXPIRED", "RESYNC"


@dataclass(slots=True)
class PointState:
    obs: rec.Observation | None = None
    good: bool = False  # latest observation is Good, carries a value and is not extreme
    reason: str | None = "UNKNOWN"


@dataclass(slots=True)
class SourceState:
    id: str
    kind: str
    enabled: bool
    point_ids: tuple[str, ...]
    conn_gen: int = 0
    seg: int = 0
    batch_seq: int = 0
    event_no: int = 0
    state: str = WAITING
    open_segment: bool = False
    last_tick: int | None = None  # last Modbus planned tick that produced an outcome
    # communication evidence (§11): the alarm baseline and the per-subscription wait baseline are separate
    enabled_mono: float = 0.0
    last_comm_wall: float | None = None
    last_comm_mono: float | None = None
    wait_mono: float | None = None  # set when this generation's subscription starts waiting; None until then
    alarm: bool = False
    # persistent resync (§11): transitions INTO RESYNC, cleared by a stable spell of valid communication
    resync_count: int = 0
    stable_since: float | None = None  # first valid response of the current uninterrupted spell
    interrupted: bool = False  # resync / expiry / alarm happened since the last valid response: the next one is a recovery
    last_outcome: str | None = None
    resync_reason: str | None = None
    # archive health of this source (§5 C2): OK -> LOST (refused / queue full) -> RECOVERING (recovery
    # group queued, not yet confirmed) -> OK.  While LOST nothing of this source is queued.
    archive: str = "OK"
    lost_since_wall: float | None = None
    lost_reason: str | None = None
    lost_from_batch_seq: int | None = None
    void_after_order: int | None = None  # groups queued after the first lost one are void until recovery
    recovery_order: int | None = None  # the recovery unit on its way (at most one): until it is confirmed or turned away
    overflow: tuple | None = None  # (wall, batch_seq, reason, order): lost again while that unit was on its way
    pending: list = field(default_factory=list)  # groups held, bounded, while RECOVERING
    # what the ARCHIVE holds (R03b): a segment is open there from the confirmation of its SEG_START to the confirmation of its
    # SEG_END; `closing` keeps the end of every segment left and not yet confirmed: seg -> (reason, end_wall)
    durable: set = field(default_factory=set)
    closing: dict = field(default_factory=dict)
    opening: dict = field(default_factory=dict)  # seg -> order of a SEG_START group that may still become durable
    inflight: set = field(default_factory=set)  # orders of this source's groups the queue accepted and has not settled
    shutdown_group: object | None = None  # stable terminal GAP + SEG_END group retried while the writer has no room
    shutdown_offered: bool = False
    shutdown_done: bool = False
    shutdown_failed: bool = False
    owed_jumps: list = field(default_factory=list)  # CLOCK_JUMP markers seen while LOST (no group could carry them): the next unit or final group does
    owed_dropped: int = 0  # markers evicted for the limit since the last ones were stored: the first one carried says how many
    watermark_mono: float | None = None
    watermark_wall: float = 0.0


@dataclass(slots=True)
class ResultState:
    current: Outcome | None = None
    emitted_key: tuple | None = None  # (value, status, reason, extreme) last emitted in this segment
    emitted: rec.Result | None = None


@dataclass(frozen=True, slots=True)
class Baseline:
    """Previous planned round of a strain_change input; usable only for the very next tick."""

    tick: int
    value: float
    conn_gen: int
    seg: int
    batch_seq: int


def _sink_none(_):
    return None


RUN_SOURCE = "_run"  # carrier "source" of run-level groups (start / end)
BASELINE_SOURCE = "_baseline"  # carrier "source" of retention-boundary baselines (§13.6): all sources in one group
BASELINE_RETRY = 60.0  # seconds between two tries for a database whose bucket still lacks its baseline (fixed pace)
BASELINE_ITEM = 2**31 - 1  # a baseline record comes after every record of the batch it follows, in history order
PENDING_LIMIT = 256  # groups held per source while a recovery group is not yet confirmed
OWED_JUMPS_LIMIT = 16  # clock-jump markers kept per source while its archive is lost: the newest ones (the rest are counted)


class Owner:
    def __init__(self, config: Settings, wall: float, mono: float, *, commit=None, publish=None, run_id=None, buckets=None):
        self.config = config
        self.buckets = buckets  # which bucket each group is put in (§13.6); without one a group has none and is a gap
        # retention baselines (§13.6 补基线): (database, bucket) still lacking one -> monotonic time of the next try; bounded,
        # as only the current bucket is kept, one entry per database
        self.baseline_due: dict[tuple, float] = {}
        self.baseline_inflight: int | None = None  # the order of the baseline group on its way
        self.baseline_sets = 0
        self.config_id = config.config_id  # a hash of the whole configuration: taken once, not once per result
        self.run_id = run_id or uuid4().hex
        self.commit, self.publish = commit or _sink_none, publish or _sink_none
        self.pending_limit = PENDING_LIMIT
        self.confirmed_watermark: dict[str, float] = {}  # per source: the watermark that is really durable
        self.run_events = 0
        self.last_tick_wall, self.last_tick_mono = wall, mono
        self.stopped = False
        self.shutdown_wall: float | None = None
        self.shutdown_mono: float | None = None
        self.run_end_group: rec.CommitGroup | None = None
        self.run_end_offered = False
        self.run_end_status: str | None = None  # COMMITTED | REFUSED
        self.on_rebuild = None  # called (source_id, reason) when a new generation must be built
        self.started_wall, self.started_mono = wall, mono
        self.topology = Topology(config)
        self.defs = {p.id: p for p in config.points}
        self.point_source = {p.id: p.source for p in config.points}
        self.sources = {
            s.id: SourceState(
                s.id,
                s.kind,
                s.enabled,
                tuple(p.id for p in config.points if p.source == s.id),
                enabled_mono=mono,
            )
            for s in config.sources
        }
        self.points = {p.id: PointState() for p in config.points}
        self.results = {d.id: ResultState() for d in config.derived}
        self.result_source: dict[str, str] = {}
        for rule in config.derived:
            first = next((n for n in rule.inputs), None)
            self.result_source[rule.id] = (
                self.point_source.get(first) or self.result_source.get(first) or config.sources[0].id
            )
        self.baselines: dict[str, Baseline] = {}
        # ordinary faults (§8): 304 channels in the research table; timing is the tracker's, state is ours
        self.rules = {x.id: fault_rule(x) for x in (*config.points, *config.derived) if fault_rule(x)}
        self.tracker = FaultTracker(self.rules, config.timing.fault_confirm_seconds)
        self.source_deps: dict[str, frozenset[str]] = {}
        for rule in config.derived:
            deps = set()
            for name in rule.inputs:
                deps |= {name} if name in self.point_source else set(self.source_deps.get(name, ()))
            self.source_deps[rule.id] = frozenset(deps)
        self.tracked_by_source: dict[str, list[str]] = {}
        for pid in self.rules:
            owner_source = self.point_source.get(pid) or self.result_source[pid]
            self.tracked_by_source.setdefault(owner_source, []).append(pid)
        self.fault_counter: dict[str, int] = {}
        self.fault_buffer: dict[str, rec.FaultRecord] = {}  # confirmed, not yet durable (in memory only)
        self.fault_loss: dict[str, dict] = {}  # per source: faults the full buffer refused
        self.fault_rejected_total = 0
        self._multi: set[str] = set()
        self.order = 0
        self.pub_seq = 0
        self.commlog = CommLog(self.run_id)  # this run's communication log (§15.1): in memory, appended here only
        self.health = {
            "stale_generation_batches": 0,
            "rejected_items": 0,
            "app_loss": 0,
            "dropped_while_resync": 0,
            "resyncs": 0,
            "expiries": 0,
            "recoveries": 0,
            "archive_lost_groups": 0,
            "archive_recoveries": 0,
            "pending_overflows": 0,
            "clock_jumps": 0,
            "clock_markers_dropped": 0,
        }

    # ------------------------------------------------------------------ plumbing

    def _state(self, s: SourceState, kind: str, wall: float, mono: float, **detail) -> rec.StateRecord:
        s.event_no += 1
        return rec.StateRecord(
            rec.state_id(self.run_id, s.id, s.event_no), self.run_id, s.id, s.seg, kind, wall, mono, detail
        )

    def _build(self, kind, source_id, wall, observations=(), results=(), states=(), faults=()):
        self.order += 1
        states = tuple(states)
        for st in states:
            if st.kind == "SEG_START" and st.source_id in self.sources:
                self.sources[st.source_id].opening[st.seg] = self.order
            if st.kind == "SEG_END" and st.source_id in self.sources:  # owed to the archive until a commit confirms it
                self.sources[st.source_id].closing[st.seg] = (st.detail.get("reason"), st.detail.get("end_wall", st.at_wall))
        return rec.CommitGroup(
            rec.group_id(self.run_id, self.order),
            self.order,
            self.run_id,
            source_id,
            kind,
            wall,
            tuple(observations),
            tuple(results),
            tuple(states),
            tuple(faults),
            None if self.buckets is None else self.buckets.label(),
        )

    def _publish(self, group: rec.CommitGroup):
        self.pub_seq += 1
        self.publish(
            rec.PublishGroup(
                self.pub_seq,
                group.order,
                group.source_id,
                group.built_wall,
                group.observations,
                group.results,
                group.states,
                group.faults,
            )
        )

    def _emit(self, kind, s: SourceState, wall, observations=(), results=(), states=(), faults=()):
        """Realtime delivery is immediate and unconditional; durability goes through the archive state."""
        if not (observations or results or states or faults):
            return None
        group = self._build(kind, s.id, wall, observations, results, states, faults)
        self._publish(group)
        self._submit(s, group)
        return group

    # ------------------------------------------------------------- archive state (C2)

    @staticmethod
    def _prune_closing(s: SourceState):
        possible = s.durable | set(s.opening)
        s.closing = {seg: end for seg, end in s.closing.items() if seg in possible}

    @classmethod
    def _settle_opening(cls, s: SourceState, group: rec.CommitGroup):
        for seg, order in list(s.opening.items()):
            if order == group.order:
                del s.opening[seg]
        cls._prune_closing(s)

    def _discard_groups(self, s: SourceState, groups):
        for group in groups:
            self._settle_opening(s, group)

    def _submit(self, s: SourceState, group: rec.CommitGroup):
        if s.archive == "OK":
            s.inflight.add(group.order)
            if self.commit(group) is False:  # None (a plain sink) means accepted
                s.inflight.discard(group.order)
                self._archive_lost(s, group, "QUEUE_FULL")
        elif s.archive == "LOST":
            self.health["archive_lost_groups"] += 1  # nothing is queued across a gap
            self._settle_opening(s, group)
        elif len(s.pending) < self.pending_limit:  # RECOVERING: bounded wait for the recovery group
            s.pending.append(group)
        else:
            self._discard_groups(s, s.pending)
            s.pending.clear()
            self.health["pending_overflows"] += 1
            self._archive_lost(s, group, "PENDING_OVERFLOW")  # re-take the baseline, never grow without limit

    def _archive_lost(self, s: SourceState, group: rec.CommitGroup, reason: str):
        """Archive loss for this source: a gap starts; recovery takes a fresh baseline at a batch boundary."""
        self._settle_opening(s, group)  # this group was refused or never entered the writer: its starts cannot land
        self.health["archive_lost_groups"] += 1
        if s.archive == "LOST" and s.recovery_order is None:
            # lost already, and a group that was queued before the loss was noticed is turned away only now: the loss began
            # no later than that group (a unit on its way would be too late; none is built while such groups are about)
            s.lost_since_wall = min(s.lost_since_wall, group.built_wall)
            if group.order < s.void_after_order:
                s.void_after_order = group.order
        if s.archive != "LOST":
            if s.archive == "RECOVERING":  # lost again while the unit is on its way: it is still tracked, the loss before it stands
                s.overflow = (group.built_wall, s.batch_seq, reason, group.order)
                s.archive = "LOST"
            else:
                s.archive, s.lost_reason = "LOST", reason
                s.lost_since_wall = min(s.lost_since_wall or group.built_wall, group.built_wall)
                s.lost_from_batch_seq = s.batch_seq
            if s.void_after_order is None or group.order < s.void_after_order:
                s.void_after_order = group.order
            self._discard_groups(s, s.pending)
            s.pending.clear()
            for rule in self.topology.rules:  # change chains must not span the gap
                if self.result_source[rule.id] == s.id:
                    self.baselines.pop(rule.id, None)

    def committed(self, group: rec.CommitGroup, result=None):
        """The writer confirmed `group` durable.  Only now may history be called recovered."""
        if result is not None and getattr(result, "lacks_baseline", False):
            self._baseline_wanted(result.target, group.bucket)
        if group.kind == rec.GROUP_BASELINE:
            self._baseline_settled(group, getattr(result, "target", None))
            return
        s = self.sources.get(group.source_id)
        for fault in group.faults:  # removed only now: durable
            self.fault_buffer.pop(fault.id, None)
        for st in group.states:
            if st.kind == "FAULT_LOSS":
                summary = self.fault_loss.get(st.source_id)
                if summary is not None:
                    summary["count"] -= st.detail["count"]
                    summary["inflight"] = 0
                    if summary["count"] <= 0:
                        del self.fault_loss[st.source_id]
            if st.kind == "WATERMARK":
                covered = st.detail["covered_wall"]
                self.confirmed_watermark[st.source_id] = max(self.confirmed_watermark.get(st.source_id, 0.0), covered)
        if s is None:
            if self.run_end_group is not None and group.group_id == self.run_end_group.group_id:
                self.run_end_status = "COMMITTED"
            return
        s.inflight.discard(group.order)
        if s.owed_jumps:  # the markers this group carried are stored now
            carried = {st.id for st in group.states if st.kind == "CLOCK_JUMP"}
            if s.owed_jumps[0].id in carried:
                s.owed_dropped = 0  # the first one said how many were dropped before the ones that were kept
            s.owed_jumps = [marker for marker in s.owed_jumps if marker.id not in carried]
        for st in group.states:  # the archive's own open segments follow what is CONFIRMED, nothing else
            if st.kind == "SEG_START":
                s.durable.add(st.seg)
            elif st.kind == "SEG_END":
                s.durable.discard(st.seg)
                s.closing.pop(st.seg, None)
        self._settle_opening(s, group)
        if s.shutdown_group is not None and group.group_id == s.shutdown_group.group_id:
            s.recovery_order = None
            s.shutdown_group, s.shutdown_offered, s.shutdown_done = None, False, True
            s.archive = "OK"
            return
        if group.order == s.recovery_order:  # the unit on its way is settled
            again, s.recovery_order, s.overflow = s.overflow, None, None
            self.health["archive_recoveries"] += 1
            held, s.pending = s.pending, []
            s.closing = {seg: end for seg, end in s.closing.items() if seg in s.durable}  # ends of segments never stored
            if again is None:
                s.archive, s.lost_since_wall, s.lost_reason = "OK", None, None
                s.void_after_order, s.lost_from_batch_seq = None, None
                for later in held:  # groups that waited behind the recovery group, in order
                    self._submit(s, later)
            else:  # lost again after it: still lost, and the next gap starts where that happened
                s.lost_since_wall, s.lost_from_batch_seq, s.lost_reason, s.void_after_order = again

    def history_status(self, source_id: str) -> dict:
        """What may be claimed about stored history: only up to the CONFIRMED watermark, and only if no gap
        is open.  No watermark means the stored points are all there is; nothing is implied beyond them."""
        s = self.sources[source_id]
        mark = self.confirmed_watermark.get(source_id)
        return {
            "archive": s.archive,
            "confirmed_watermark": mark,
            "coverage": "OBSERVATIONS_ONLY" if mark is None else "WATERMARK",
            "gap_since": s.lost_since_wall,
        }

    def refused(self, group: rec.CommitGroup, reason: str):
        """A policy refused the whole group (budget, reserve, capacity): it becomes, or extends, a gap.  A retention baseline
        is the exception: losing one costs its bucket only the deletion eligibility, it is tried again (§13.6 补基线)."""
        if group.kind == rec.GROUP_BASELINE:
            self._baseline_settled(group, None)
            return
        s = self.sources.get(group.source_id)
        if s is None:
            if self.run_end_group is not None and group.group_id == self.run_end_group.group_id:
                self.run_end_status = "REFUSED"
            return
        s.inflight.discard(group.order)
        self._settle_opening(s, group)
        if s.shutdown_group is not None and group.group_id == s.shutdown_group.group_id:
            s.recovery_order = None
            s.shutdown_group, s.shutdown_offered, s.shutdown_failed = None, False, True
            return
        if group.order == s.recovery_order:
            # the unit on its way was turned away: nothing it carried is stored, and the loss it was built for - from its
            # first moment, the overflow after it included - is still unrecovered
            s.archive, s.recovery_order, s.overflow = "LOST", None, None
            self._discard_groups(s, s.pending)
            s.pending.clear()
        else:
            self._archive_lost(s, group, "REFUSED:" + reason)

    # ------------------------------------------------------- retention baselines (§5, §13.6)

    def unsettled_buckets(self) -> set:
        """Buckets of groups this owner holds back for a recovery: they are still on their way (§13.6 可删范围)."""
        return {g.bucket for s in self.sources.values() for g in s.pending}

    def _baseline_wanted(self, target, bucket):
        """The route said `target` has no baseline of `bucket`: marked only, taken at the next batch boundary.  A notice for a
        bucket that is no longer the current one is dropped - a sealed bucket gets no new baseline."""
        if self.buckets is not None and bucket is not None and bucket == self.buckets.label():
            self.baseline_due.setdefault((target, bucket), 0.0)

    def _baseline_settled(self, group: rec.CommitGroup, target):
        if self.baseline_inflight == group.order:
            self.baseline_inflight = None
        if target is not None:
            self.baseline_due.pop((target, group.bucket), None)

    def _retention_tick(self, wall: float, mono: float):
        current = None if self.buckets is None else self.buckets.label()
        self.baseline_due = {key: when for key, when in self.baseline_due.items() if key[1] == current}
        if self.baseline_inflight is not None or not self.baseline_due or min(self.baseline_due.values()) > mono:
            return
        group = self.retention_baseline(wall, mono)
        for key in self.baseline_due:  # until it is confirmed where it is wanted, the next try is a fixed pace away
            self.baseline_due[key] = mono + BASELINE_RETRY
        if self.commit(group) is not False:
            self.baseline_inflight = group.order

    def retention_baseline(self, wall: float, mono: float) -> rec.CommitGroup:
        """Every source's values in force and the derived results computed from them, restated at this batch boundary as ONE
        group (§5 保留边界基线): new ids, the restated record referenced, counted from `wall` only.  A source without a valid
        segment is only marked.  Nothing in the owner changes: no change, no fault timer, nothing shown live."""
        self.baseline_sets += 1
        n, observations, results, sources = self.baseline_sets, [], [], {}
        for s in self.sources.values():
            if not (s.enabled and s.state == VALID and s.open_segment and s.archive == "OK"):
                sources[s.id] = "NO_VALID_SEGMENT"
                continue
            sources[s.id] = "SEGMENT"
            for pid in s.point_ids:
                obs = self.points[pid].obs
                if obs is not None:
                    observations.append(
                        replace(
                            obs, id=rec.baseline_id(self.run_id, n, "o", pid), seg=s.seg, batch_seq=s.batch_seq,
                            item_seq=BASELINE_ITEM, origin=rec.ORIGIN_BASELINE, orig_id=obs.id, asserted_wall=wall,
                            asserted_mono=mono,
                        )  # fmt: skip
                    )
            for rule in self.topology.rules:
                last = self.results[rule.id].emitted
                if self.result_source[rule.id] == s.id and last is not None:
                    results.append(
                        replace(
                            last, id=rec.baseline_id(self.run_id, n, "r", rule.id), seg=s.seg, batch_seq=s.batch_seq,
                            origin=rec.ORIGIN_BASELINE, orig_id=last.id, asserted_wall=wall,
                        )  # fmt: skip
                    )
        state = rec.StateRecord(
            rec.state_id(self.run_id, BASELINE_SOURCE, n), self.run_id, BASELINE_SOURCE, 0, "RETENTION_BASELINE", wall, mono,
            {"set": n, "sources": sources},
        )  # fmt: skip
        return self._build(rec.GROUP_BASELINE, BASELINE_SOURCE, wall, observations, results, [state])

    def _try_archive_recovery(self, s: SourceState, wall: float, mono: float):
        """At a batch boundary: gap + new segment + retained baseline + first watermark as ONE unit (§5)."""
        if s.archive != "LOST" or s.recovery_order is not None or s.inflight or s.state in (RESYNC, EXPIRED):
            return  # a unit on its way, or groups still on theirs, hold this up; an unsynchronised intake must be fixed by C1 first
        states = [self._gap_state(s, wall, mono), *self._owed_markers(s)]
        group = self._segment_group(s, "ARCHIVE_GAP", wall, mono, lead=states, unit=True)
        self._submit_recovery(s, group)  # archive-only: realtime clients saw these values long ago

    def _submit_recovery(self, s: SourceState, group: rec.CommitGroup):
        self._discard_groups(s, s.pending)
        s.pending.clear()
        s.recovery_order = group.order
        s.inflight.add(group.order)
        if self.commit(group) is False:
            s.recovery_order = None  # no room yet: stay LOST, try again at the next tick
            s.inflight.discard(group.order)
            self._settle_opening(s, group)
            return
        s.archive = "RECOVERING"

    def is_void(self, group: rec.CommitGroup) -> bool:
        """A group queued behind a lost one must not commit: it would hide the gap and a watermark
        above it would certify history that is missing.  Only the recovery group may pass."""
        s = self.sources.get(group.source_id)
        void = bool(
            s is not None
            and s.archive != "OK"
            and s.void_after_order is not None
            and group.order > s.void_after_order
            and group.order != s.recovery_order
        )
        if void:
            s.inflight.discard(group.order)  # the queue drops it without a word: it is settled
            self._settle_opening(s, group)
        return void

    def _gap_state(self, s: SourceState, wall: float, mono: float) -> rec.StateRecord:
        return self._state(
            s, "GAP", wall, mono, start_wall=s.lost_since_wall, end_wall=max(wall, s.lost_since_wall), reason=s.lost_reason
        )  # a clock stepped back since the loss began must not give a gap that ends before it started

    def _owed_markers(self, s: SourceState) -> list[rec.StateRecord]:
        """The clock-jump markers the next unit or final group stores.  When the limit made some go, the first one says how many
        (`earlier_markers_dropped`): the stored clock events must not be read as all of them.  Segments and gaps do not rest on
        these markers - they come from the segment facts - so the loss touches nothing but this record."""
        if not s.owed_dropped:
            return list(s.owed_jumps)
        first, *rest = s.owed_jumps
        return [replace(first, detail={**first.detail, "earlier_markers_dropped": s.owed_dropped}), *rest]

    def _watermark_state(self, s: SourceState, wall: float, mono: float):
        """History is certain only as far as communication was proven; never 'now' (§5)."""
        if s.last_comm_wall is None or not s.open_segment:
            return None
        return self._state(s, "WATERMARK", wall, mono, seg=s.seg, covered_wall=min(s.last_comm_wall, wall))

    def _durable_ends(self, s: SourceState, reason: str, wall: float, mono: float) -> list[rec.StateRecord]:
        """SEG_END for every segment the ARCHIVE holds open - not a guess from the segment the owner is in - each at the
        earlier of the end it was left at and the start of the gap.  A segment left before the gap began keeps the reason
        it was left with; one the gap cut off says `reason`.  (A cut made for a CLOCK_JUMP always counts as made before: the
        wall clock has stepped, so two wall times do not say which came first.)  The recovery unit and the final group of a
        stop both close segments with this, so a segment ends the same way whichever of them does it.  A segment that was
        never stored gets no end, so that no end is stored without its start."""
        ends = []
        for seg in sorted(s.durable):
            left_reason, left_end = s.closing.get(seg, (None, None))
            left = left_end is not None and (left_end <= s.lost_since_wall or left_reason == "CLOCK_JUMP")
            ended = self._state(
                s, "SEG_END", wall, mono,
                reason=(left_reason or reason) if left else reason,
                end_wall=left_end if left else s.lost_since_wall,
            )  # fmt: skip
            ends.append(replace(ended, seg=seg))
        return ends

    def _segment_group(
        self, s: SourceState, reason: str, wall: float, mono: float, *, lead=(), close_old_at=None, batch=None, unit=False
    ) -> rec.CommitGroup:
        """Cut the valid interval and start a new segment: [lead] + SEG_END + SEG_START + retained baseline
        (+ the recovery batch and its results) + the first watermark, as one unit.

        A recovery `unit` (the archive was lost) closes the segments the ARCHIVE holds open - not a guess from the
        segment the owner is in - each at the earlier of the end it was left at and the start of the gap; a segment
        that was never stored gets no end, so that no end is stored without its start."""
        states = list(lead)
        if unit:
            states.extend(self._durable_ends(s, reason, wall, mono))
        elif s.open_segment:
            end = wall if close_old_at is None else close_old_at
            states.append(self._state(s, "SEG_END", wall, mono, reason=reason, end_wall=end))
        s.seg, s.open_segment = s.seg + 1, True
        states.append(self._state(s, "SEG_START", wall, mono, conn_gen=s.conn_gen, reason=reason))
        base_obs, base_results = self._baseline(s, wall, mono)
        observations, results = [], []
        if batch is not None:
            if batch.items:
                s.batch_seq += 1
                observations, _ = self._ingest(s, batch)
            results = self._recompute(s, batch, list(s.point_ids))  # validity returned: revisit dependents
            self._fault_restart(s, mono)  # §11: timing restarts from the processed final state
        mark = self._watermark_state(s, wall, mono)
        s.watermark_wall, s.watermark_mono = (mark.detail["covered_wall"] if mark else 0.0), mono
        if mark is not None:
            states.append(mark)
        loss = self._fault_loss_state(s, wall, mono)
        if loss is not None:
            states.append(loss)
        return self._build(
            rec.GROUP_RECOVERY,
            s.id,
            wall,
            [*base_obs, *observations],
            [*base_results, *results],
            states,
            self._buffered_faults(s),
        )

    def _buffered_faults(self, s: SourceState) -> list:
        return [f for f in self.fault_buffer.values() if f.source_id == s.id]

    def _fault_loss_state(self, s: SourceState, wall: float, mono: float):
        summary = self.fault_loss.get(s.id)
        if not summary or summary["count"] <= 0:
            return None
        summary["inflight"] = summary["count"]
        return self._state(
            s,
            "FAULT_LOSS",
            wall,
            mono,
            count=summary["count"],
            first_wall=summary["first_wall"],
            last_wall=summary["last_wall"],
            points=sorted(summary["points"]),
        )

    def _input(self, name: str) -> Input:
        if name in self.points:
            ps = self.points[name]
            usable = self.sources[self.point_source[name]].state == VALID
            obs = ps.obs
            return Input(
                obs.value if obs else None,
                bool(usable and ps.good),
                bool(usable and obs and obs.extreme),
            )
        out = self.results[name].current
        if out is None:
            return Input(None, False)
        return Input(out.value, out.status == rec.RESULT_OK, out.extreme)

    # ------------------------------------------------------------ communication

    def _comm(self, s: SourceState, wall: float, mono: float) -> list[rec.StateRecord]:
        """A valid response arrived: only this advances the communication time (never a reconnect attempt).

        Three different facts are logged, never one in place of another (§15.1): the first valid communication of the
        run, the first valid response after an interruption (resync or expiry, alarmed or not), and the clearing of an
        alarm.  Being connected or subscribed is none of them.
        """
        before = self._comm_of(s)
        if before is None:
            self._log(s, "FIRST_COMM", "FIRST_VALID_RESPONSE", wall, mono)
        elif s.interrupted:
            self._log(s, "COMM_RECOVERED", "VALID_RESPONSE", wall, mono, comm=before)
        s.interrupted = False
        s.last_comm_wall, s.last_comm_mono = wall, mono
        if s.stable_since is None:
            s.stable_since = mono  # the stable spell is counted from the first response, interruptions restart it
        if s.wait_mono is None:
            s.wait_mono = mono  # a response of this subscription proves it is waiting, whatever the event order
        if s.alarm:
            s.alarm = False
            self._log(s, "COMM_ALARM_CLEARED", "VALID_RESPONSE", wall, mono, comm=before)
            return [self._state(s, "COMM_RESTORED", wall, mono)]
        return []

    @staticmethod
    def _comm_of(s: SourceState):
        """The last valid communication known now, as the log wants it: (wall, monotonic) or None."""
        return None if s.last_comm_wall is None else (s.last_comm_wall, s.last_comm_mono)

    def _log(self, s: SourceState, event: str, reason: str, wall: float, mono: float, *, comm=False, failure=False, detail=None):
        """One fact of this source into the communication log.  `comm` defaults to the last communication known now."""
        comm = self._comm_of(s) if comm is False else comm
        return self.commlog.event(s.id, s.conn_gen or None, event, reason, wall, mono, comm=comm, failure=failure, detail=detail)

    def _alarm_limit(self, s: SourceState) -> float:
        t = self.config.timing
        return t.modbus_alarm_seconds if s.kind == "modbus" else t.ua_alarm_seconds

    def tick(self, wall: float, mono: float):
        """Once per second from the monotonic schedule.  Communication is judged before anything else; the bucket is changed
        here, at a batch boundary, before any group of this tick is built."""
        if self.buckets is not None:
            self.buckets.tick(wall, mono)
        self._retention_tick(wall, mono)
        delta = (wall - self.last_tick_wall) - (mono - self.last_tick_mono)
        if abs(delta) > self.config.timing.clock_jump:  # exactly 2 s does not open a segment
            self._clock_jump(wall, mono, delta)
        for s in self.sources.values():
            if s.enabled:  # a source switched off by configuration never accumulates a communication alarm
                self._check_source(s, wall, mono)
                if s.archive == "LOST":
                    self._try_archive_recovery(s, wall, mono)
        self._advance_faults(wall, mono)  # AFTER communication has been judged for every source
        self._watermarks(wall, mono)
        self.last_tick_wall, self.last_tick_mono = wall, mono

    def _check_source(self, s: SourceState, wall: float, mono: float):
        t = self.config.timing
        baseline = s.last_comm_mono if s.last_comm_mono is not None else s.enabled_mono
        if not s.alarm and mono - baseline >= self._alarm_limit(s):
            s.alarm, s.interrupted = True, True
            s.stable_since = None
            self._log(s, "COMM_ALARM", "NEVER_RESPONDED" if s.last_comm_mono is None else "NO_VALID_RESPONSE", wall, mono, failure=True)
            self._emit(
                rec.GROUP_STATE,
                s,
                wall,
                states=[
                    self._state(
                        s,
                        "COMM_ALARM",
                        wall,
                        mono,
                        silent_seconds=mono - baseline,
                        never_responded=s.last_comm_mono is None,
                        last_outcome=s.last_outcome,
                    )
                ],
            )
        if s.kind == "modbus":
            self._settle(s, mono)
            return
        if s.state == RESYNC or s.wait_mono is None:
            return  # nothing to wait for, or the subscription has not started waiting: not silent, just not begun
        # OPC UA: silence is measured from the later of the last response and this subscription's own start
        latest = max(s.last_comm_mono if s.last_comm_mono is not None else -math.inf, s.wait_mono)
        quiet = mono - latest
        if s.state == VALID and quiet >= t.ua_data_validity:
            self._expire(s, wall, mono)
        if quiet >= t.ua_alarm_seconds:
            self._start_resync(s, "SILENCE", wall, mono)
        self._settle(s, mono)

    def _settle(self, s: SourceState, mono: float):
        """A stable spell of valid communication (§11) ends persistent resync: the count starts over.

        Not the backoff's failure count (which Modbus measures from connect) and not `state == VALID` (a Modbus source
        stays VALID through timed-out rounds): the spell is counted from this generation's first valid response and
        any timeout, disconnect, expiry, resync, alarm or new generation restarts it.
        """
        if s.stable_since is not None and mono - s.stable_since >= self.config.timing.backoff_stable_seconds:
            s.resync_count = 0

    def _invalidate(self, s: SourceState):
        """Cached values stay but stop counting: change chains break and derived results are unavailable."""
        for rule in self.topology.rules:
            if self.result_source[rule.id] == s.id:
                self.baselines.pop(rule.id, None)
                self.results[rule.id].current = unavailable("SOURCE_INVALID")
        for pid in self.tracked_by_source.get(s.id, ()):  # unknown data cannot confirm, nor recover, a fault
            self.tracker.cancel(pid)

    def _expire(self, s: SourceState, wall: float, mono: float):
        """3 s without a valid response: data unusable, segment cut, no rebuild (§11)."""
        end = min(wall, (s.last_comm_wall or wall) + self.config.timing.ua_data_validity)
        s.state, s.stable_since, s.interrupted = EXPIRED, None, True
        self._log(s, "DATA_EXPIRED", "VALIDITY_EXCEEDED", wall, mono, failure=True)
        self.health["expiries"] += 1
        states = []
        if s.open_segment:
            s.open_segment = False
            states.append(self._state(s, "SEG_END", wall, mono, reason="EXPIRED", end_wall=end))
        self._invalidate(s)
        self._emit(rec.GROUP_STATE, s, wall, states=states)

    def _start_resync(self, s: SourceState, reason: str, wall: float, mono: float):
        """C1: stop using the old state and ask for a new generation.  No Republish, no back-fill."""
        if s.state == RESYNC:
            return
        states = []
        if s.open_segment:
            s.open_segment = False
            states.append(self._state(s, "SEG_END", wall, mono, reason=reason, end_wall=wall))
        s.state, s.resync_reason, s.stable_since, s.interrupted = RESYNC, reason, None, True
        s.resync_count += 1  # a real transition: repeats while already in RESYNC returned above
        self._log(s, "RESYNC", reason, wall, mono, failure=True)
        self.health["resyncs"] += 1
        self._invalidate(s)
        states.append(self._state(s, "RESYNC", wall, mono, reason=reason))
        self._emit(rec.GROUP_STATE, s, wall, states=states)
        if self.on_rebuild is not None:
            self.on_rebuild(s.id, reason)

    def source_down(self, source_id: str, conn_gen: int, reason: str, wall: float, mono: float):
        """A definite disconnect: the data is invalid at once, without waiting for any timer."""
        s = self.sources[source_id]
        if conn_gen == s.conn_gen:
            self._start_resync(s, "DOWN:" + reason, wall, mono)

    def lost(self, source_id: str, conn_gen: int, reason: str, wall: float, mono: float):
        """Definite evidence of lost notifications (gap, overflow, callback failure): start C1."""
        s = self.sources[source_id]
        if conn_gen == s.conn_gen:
            self._start_resync(s, reason, wall, mono)

    # ----------------------------------------------------------------- loss

    def report_loss(
        self, source_id: str, conn_gen: int, reason: str, wall: float, mono: float, tick=None, count=1
    ):
        """Data was lost between receipt and acceptance (e.g. the ingress queue was full).

        Always becomes a stored state and a counter: a log line alone would let history look continuous.
        """
        s = self.sources[source_id]
        if conn_gen != s.conn_gen:
            return
        self.health["app_loss"] += count
        for rule in self.topology.rules:  # a lost planned round breaks every consecutive-round chain
            if self.result_source[rule.id] == s.id:
                self.baselines.pop(rule.id, None)
        self._log(s, "APP_LOSS", reason, wall, mono, failure=True).count = count  # batches lost, not times reported
        record = self._state(s, "APP_LOSS", wall, mono, reason=reason, tick=tick, count=count)
        self._emit(rec.GROUP_STATE, s, wall, states=[record])
        if s.kind == "opcua":  # notifications went missing: the OPC UA state can no longer be trusted
            self._start_resync(s, "APP_LOSS:" + reason, wall, mono)

    # ------------------------------------------------------------ generations

    def begin_generation(self, source_id: str, gen: int, wall: float, mono: float, reason="CONNECT"):
        """A new connection / subscription generation: fresh segment, nothing carried over."""
        s = self.sources[source_id]
        if gen <= s.conn_gen:
            raise ValueError("connection generation must increase")
        states = []
        if s.open_segment:
            states.append(self._state(s, "SEG_END", wall, mono, reason="NEW_GENERATION"))
        s.conn_gen, s.seg, s.open_segment = gen, s.seg + 1, True
        s.state, s.last_tick, s.wait_mono, s.resync_reason = WAITING, None, None, None  # waiting starts with begin_waiting
        s.stable_since = None
        s.watermark_wall, s.watermark_mono = 0.0, None
        for pid in s.point_ids:
            self.points[pid] = PointState()
        for rule in self.topology.rules:
            if self.result_source[rule.id] == s.id:
                self.results[rule.id] = ResultState()
                self.baselines.pop(rule.id, None)
        states.append(self._state(s, "SEG_START", wall, mono, conn_gen=gen, reason=reason))
        self._emit(rec.GROUP_STATE, s, wall, states=states)
        self.commlog.end_rounds(source_id, "NEW_GENERATION", wall, mono)  # round failures never outlive their generation
        self._log(s, "NEW_GENERATION", reason, wall, mono)  # connected: not the same thing as communicating again

    def begin_waiting(self, source_id: str, gen: int, mono: float):
        """CreateSubscription has returned: from now the 3 s / 6 s checks of this generation run (§11).

        Only the wait start is set.  The communication time is never touched, so a first response that was
        already processed keeps its own time.
        """
        s = self.sources[source_id]
        if gen == s.conn_gen and s.wait_mono is None:
            s.wait_mono = mono

    # ------------------------------------------------------- communication log

    def attempt_failed(self, source_id: str, phase: str, reason: str, detail, truncated: bool, wall: float, mono: float):
        """A CONNECT / SUBSCRIBE attempt failed (from the protocol task, through the ordered entry).

        Log only: no state record, no group, nothing archived.  A source switched off by configuration makes no attempts.
        """
        s = self.sources[source_id]
        if s.enabled:
            comm = None if s.last_comm_wall is None else (s.last_comm_wall, s.last_comm_mono)
            self.commlog.attempt_failed(source_id, phase, reason, wall, mono, detail=detail, truncated=truncated, comm=comm)

    def attempt_ok(self, source_id: str, phase: str, wall: float, mono: float):
        """The phase succeeded: its failure aggregate ends (and only that phase's)."""
        self.commlog.attempt_ok(source_id, phase, wall, mono)

    def report_diag_lost(self, count: int):
        """Diagnostic facts were refused at the ordered entry.  Counted for the log's own completeness note; this is
        not an application loss and starts no resync."""
        self.commlog.diag_lost += count

    # ---------------------------------------------------------------- intake

    def accept(self, batch: rec.InputBatch) -> bool:
        """Take one complete batch as a unit; False when it is dropped (and counted)."""
        s = self.sources[batch.source_id]
        if not s.enabled:
            return False
        if batch.conn_gen != s.conn_gen:
            self.health["stale_generation_batches"] += 1
            return False
        if s.state == RESYNC:
            self.health["dropped_while_resync"] += 1
            return False
        if batch.kind == rec.KIND_MODBUS_POLL:
            return self._accept_poll(s, batch)
        return self._accept_notification(s, batch)

    def _observe(self, s, batch, item: rec.Item, item_seq: int) -> rec.Observation | None:
        point = self.defs.get(item.point_id)
        if point is None or self.point_source[item.point_id] != s.id:
            self.health["rejected_items"] += 1
            return None
        value, quality = item.value, item.quality
        if value is not None and not math.isfinite(value):
            value, quality = None, rec.BAD
        if value is None and quality == rec.GOOD:
            quality = rec.BAD  # "Good" without a payload cannot be used and is not made up
        return rec.Observation(
            id=rec.observation_id(self.run_id, s.id, s.batch_seq, item_seq),
            run_id=self.run_id,
            source_id=s.id,
            conn_gen=s.conn_gen,
            seg=s.seg,
            batch_seq=s.batch_seq,
            item_seq=item_seq,
            point_id=item.point_id,
            value=value,
            quality=quality,
            status_code=item.status_code,
            received_wall=batch.wall if item.wall is None else item.wall,
            received_mono=batch.mono if item.mono is None else item.mono,
            source_time=item.source_time,
            extreme=quality == rec.GOOD and is_extreme(value, point),
            tick=batch.tick,
            poll_id=batch.poll_id,
            response_id=item.response_id,
        )

    def _apply(self, obs: rec.Observation):
        ps = self.points[obs.point_id]
        ps.obs = obs
        ps.good = obs.quality == rec.GOOD and obs.value is not None and not obs.extreme
        ps.reason = (
            None
            if ps.good
            else "EXTREME"
            if obs.extreme
            else "QUALITY"
            if obs.quality != rec.GOOD
            else "NO_VALUE"
        )

    def _ingest(self, s: SourceState, batch) -> tuple[list[rec.Observation], list[str]]:
        observations, touched = [], []
        versions: dict[str, set] = {}
        for item in batch.items:
            obs = self._observe(s, batch, item, len(observations))
            if obs is None:
                continue
            self._apply(obs)
            observations.append(obs)
            touched.append(obs.point_id)
            versions.setdefault(obs.point_id, set()).add((obs.value, obs.quality))
            if obs.point_id in self.rules:  # each version in batch order: a valid normal one clears the clock
                self.tracker.observe(obs.point_id, self._valid_value(obs.point_id), obs.received_mono)
        self._multi = {pid for pid, seen in versions.items() if len(seen) > 1}
        return observations, touched

    def _accept_notification(self, s: SourceState, batch) -> bool:
        states = self._comm(s, batch.wall, batch.mono)
        if s.state == EXPIRED:
            return self._recover(s, batch, states)
        if s.state == WAITING:
            s.state = VALID
        if not batch.items:  # keep-alive: proves communication, is not an observation
            self._emit(rec.GROUP_STATE, s, batch.wall, states=states)
            return True
        s.batch_seq += 1
        observations, touched = self._ingest(s, batch)
        results = self._recompute(s, batch, touched)
        self._emit(rec.GROUP_BATCH, s, batch.wall, observations, results, states)
        return True

    def _accept_poll(self, s: SourceState, batch) -> bool:
        s.last_outcome = batch.outcome
        if batch.outcome in ("OK", "EXCEPTION"):
            states = self._comm(s, batch.wall, batch.mono)
        else:
            states, s.stable_since = [], None  # a round without an answer ends the stable spell; VALID stays VALID, WAITING stays WAITING
        self._log_requests(s, batch)
        s.batch_seq += 1
        if s.state == WAITING and batch.outcome in ("OK", "EXCEPTION"):  # only a device that ANSWERED makes the source valid
            s.state = VALID
        valid = [pid for pid in s.point_ids if self.points[pid].good]  # valid before this round: only these can lose it
        if batch.outcome != "OK":
            return self._poll_failed(s, batch, states, valid)
        for pid in s.point_ids:  # only this round's responses count; nothing is borrowed
            ps = self.points[pid]
            ps.good, ps.reason = False, "POLL_MISSING"
        s.last_tick = batch.tick
        observations, touched = self._ingest(s, batch)
        lost = self._missing(s, batch, [pid for pid in valid if pid not in touched], "UNANSWERED", len(observations))
        for pid in self.tracked_by_source.get(s.id, ()):
            if pid in self.points and not self.points[pid].good:
                self.tracker.observe(pid, None, batch.mono)
        results = self._recompute(s, batch, list(s.point_ids))
        poll = self._state(s, "POLL", batch.wall, batch.mono, **self._poll_detail(s, batch, observations))
        self._emit(rec.GROUP_BATCH, s, batch.wall, [*observations, *lost], results, [*states, poll])
        return True

    def _missing(self, s: SourceState, batch, pids, cause: str, first_seq: int) -> list[rec.Observation]:
        """Inputs that were valid and are not any more because of this round: ONE record each, without a value, its
        quality naming the cause - so that history ends the earlier value here instead of holding it on through the
        failure (§5, §14).  The record is the input's current observation from now on; `reason` stays the poll's
        (POLL_*), which is what the live view and the communication page show."""
        records = []
        for k, pid in enumerate(pids):
            obs = rec.Observation(
                id=rec.observation_id(self.run_id, s.id, s.batch_seq, first_seq + k),
                run_id=self.run_id,
                source_id=s.id,
                conn_gen=s.conn_gen,
                seg=s.seg,
                batch_seq=s.batch_seq,
                item_seq=first_seq + k,
                point_id=pid,
                value=None,
                quality=rec.MISSING + cause,
                status_code=None,
                received_wall=batch.wall,
                received_mono=batch.mono,
                tick=batch.tick,
                poll_id=batch.poll_id,
            )
            self.points[pid].obs = obs
            records.append(obs)
        return records

    def _log_requests(self, s: SourceState, batch):
        """Failures are kept per request: a request that answered ends only ITS earlier failure, never another's."""
        for q in batch.requests:
            if q.outcome == "OK":
                self.commlog.round_ok(s.id, q.scope, batch.wall, batch.mono)
                continue
            event, reason = ("POLL_TIMEOUT", "NO_ANSWER") if q.outcome == "TIMEOUT" else ("POLL_EXCEPTION", f"EXCEPTION_CODE_{q.code}")
            text, truncated = clean_detail(q.detail)
            self.commlog.round_failed(
                s.id, s.conn_gen, event, reason, batch.wall, batch.mono,
                scope=q.scope, detail=text, truncated=q.truncated or truncated, comm=self._comm_of(s),
            )  # fmt: skip

    def _poll_detail(self, s, batch, observations):
        responses = {}
        for o in observations:
            if o.response_id is not None:
                responses.setdefault(o.response_id, {"wall": o.received_wall, "mono": o.received_mono})
        return {
            "tick": batch.tick,
            "poll_id": batch.poll_id,
            "outcome": batch.outcome,
            "start_wall": batch.poll_start_wall,
            "end_wall": batch.poll_end_wall,
            "received": len(observations),
            "expected": len(s.point_ids),
            "responses": responses,
        }

    def _poll_failed(self, s: SourceState, batch, states, valid) -> bool:
        for pid in s.point_ids:
            ps = self.points[pid]
            ps.good, ps.reason = False, "POLL_" + batch.outcome
        s.last_tick = batch.tick
        lost = self._missing(s, batch, valid, batch.outcome, 0)
        for pid in self.tracked_by_source.get(s.id, ()):
            if pid in self.points:
                self.tracker.observe(pid, None, batch.mono)
        results = self._recompute(s, batch, list(s.point_ids))
        poll = self._state(s, "POLL", batch.wall, batch.mono, **self._poll_detail(s, batch, ()))
        self._emit(rec.GROUP_BATCH, s, batch.wall, lost, results, [*states, poll])
        return True

    # -------------------------------------------------------------- recovery

    def _baseline(self, s: SourceState, wall: float, mono: float):
        """Re-state the retained cache of one source in the CURRENT segment (§5 C2, §11).

        Original observation identity, value and quality are kept; the assertion time is recorded
        separately.  A re-statement is not a new sample and never starts a change or a fault timer.
        """
        observations, results = [], []
        for pid in s.point_ids:
            obs = self.points[pid].obs
            if obs is None:
                continue
            observations.append(
                replace(
                    obs,
                    id=rec.assertion_id(self.run_id, s.id, s.seg, pid),
                    seg=s.seg,
                    batch_seq=s.batch_seq,
                    item_seq=-1,
                    origin=rec.ORIGIN_ASSERTION,
                    orig_id=obs.id,
                    asserted_wall=wall,
                    asserted_mono=mono,
                )
            )
        for rule in self.topology.rules:
            state = self.results[rule.id]
            if self.result_source[rule.id] != s.id or state.emitted is None:
                continue
            last = state.emitted
            # a derived value computed inside an archive gap has lost the inputs that justified it (§10.5)
            incomplete = last.basis_incomplete or (
                s.archive == "LOST"
                and s.lost_from_batch_seq is not None
                and last.batch_seq >= s.lost_from_batch_seq
            )
            asserted = replace(
                last,
                id=rec.result_assertion_id(self.run_id, s.id, s.seg, rule.id),
                seg=s.seg,
                origin=rec.ORIGIN_ASSERTION,
                orig_id=last.id,
                asserted_wall=wall,
                basis_incomplete=incomplete,
            )
            state.emitted = asserted
            results.append(asserted)
        return observations, results

    def _recover(self, s: SourceState, batch, states) -> bool:
        """Same subscription publishes again inside the 3-6 s window: new segment, no rebuild (§11).

        New segment + baseline of the retained cache + the recovery batch + updated derived results
        are ONE commit group and ONE publish group: the old cache is never certified as current alone.
        If the archive was also lost meanwhile, the gap rides in the same unit.
        """
        s.state = VALID
        self.health["recoveries"] += 1
        lead = list(states)
        lost = s.archive == "LOST" and s.recovery_order is None and not s.inflight  # one unit at a time, behind nothing older
        if lost:
            lead.append(self._gap_state(s, batch.wall, batch.mono))
        group = self._segment_group(s, "RECOVERY", batch.wall, batch.mono, lead=lead, batch=batch, unit=lost)
        self._publish(group)
        if lost:
            self._submit_recovery(s, group)
        else:
            self._submit(s, group)
        return True

    # ------------------------------------------------------------- run lifecycle

    def _make_run_group(self, wall: float, mono: float, kind: str, extra_states=(), **detail):
        self.run_events += 1
        record = rec.StateRecord(
            rec.state_id(self.run_id, RUN_SOURCE, self.run_events), self.run_id, RUN_SOURCE, 0, kind, wall, mono, detail
        )
        return self._build(rec.GROUP_STATE, RUN_SOURCE, wall, states=[record, *extra_states])

    def _run_group(self, wall: float, mono: float, kind: str, extra_states=(), **detail):
        group = self._make_run_group(wall, mono, kind, extra_states, **detail)
        if self.commit(group) is False:  # run-level facts are tiny; the writer queue drains them first
            self.health["run_group_rejected"] = self.health.get("run_group_rejected", 0) + 1
        return group

    def start(self, wall: float, mono: float):
        """Register this run before any data: the process, its configuration and start time.

        Also the moment acquisition starts: a source's "never responded" alarm counts from here (§11), not from
        when this object was built, which is before the archive opens.
        """
        for s in self.sources.values():
            s.enabled_mono = mono
        self._run_group(wall, mono, "RUN_START", config_id=self.config_id, started_wall=self.started_wall)

    def shutdown(self, wall: float, mono: float):
        """Begin a normal stop after intake is drained.  RUN_END is deliberately separate: it may be submitted only
        after every source's accepted records and terminal archive facts have settled (R04/Q03)."""
        if self.stopped:
            return
        self.stopped, self.shutdown_wall, self.shutdown_mono = True, wall, mono
        for s in self.sources.values():
            if s.open_segment:
                s.open_segment = False
                end = self._state(s, "SEG_END", wall, mono, reason="SHUTDOWN", end_wall=wall)
                if s.archive == "LOST":
                    if end.seg in s.durable or end.seg in s.opening:
                        s.closing[end.seg] = ("SHUTDOWN", wall)
                else:
                    self._emit(rec.GROUP_STATE, s, wall, states=[end])
        if self.shutdown_progress():
            self.submit_run_end()

    def _terminal_group(self, s: SourceState) -> rec.CommitGroup:
        wall, mono = self.shutdown_wall, self.shutdown_mono
        states = [self._gap_state(s, wall, mono), *self._owed_markers(s), *self._durable_ends(s, "ARCHIVE_GAP", wall, mono)]
        return self._build(rec.GROUP_RECOVERY, s.id, wall, states=states)

    def shutdown_progress(self) -> bool:
        """Advance archive-only shutdown work without waiting for I/O.  The runtime calls this while the writer runs."""
        if not self.stopped:
            return False
        for s in self.sources.values():
            if s.shutdown_done or s.shutdown_failed:
                continue
            if s.archive == "LOST" and s.recovery_order is None and not s.inflight:
                if s.shutdown_group is None:
                    s.shutdown_group = self._terminal_group(s)
                if not s.shutdown_offered:
                    s.recovery_order = s.shutdown_group.order
                    s.inflight.add(s.shutdown_group.order)
                    if self.commit(s.shutdown_group) is False:
                        s.recovery_order = None
                        s.inflight.discard(s.shutdown_group.order)
                    else:
                        s.shutdown_offered = True
                        s.archive = "RECOVERING"
                continue
            self._prune_closing(s)
            if (
                s.archive == "OK"
                and not s.pending
                and not s.inflight
                and s.recovery_order is None
                and not s.durable
                and not s.opening
                and not s.closing
            ):
                s.shutdown_done = True
        return all(s.shutdown_done for s in self.sources.values()) and not any(s.shutdown_failed for s in self.sources.values())

    def submit_run_end(self) -> bool:
        """Offer one stable RUN_END group after source shutdown has settled; a full queue retries this same object."""
        if not self.shutdown_progress() or any(s.shutdown_failed for s in self.sources.values()):
            return False
        if self.run_end_group is None:
            self.run_end_group = self._make_run_group(
                self.shutdown_wall, self.shutdown_mono, "RUN_END", reason="NORMAL", ended_wall=self.shutdown_wall
            )
        if not self.run_end_offered:
            if self.commit(self.run_end_group) is False:
                self.health["run_group_rejected"] = self.health.get("run_group_rejected", 0) + 1
                return False
            self.run_end_offered = True
        return True

    def shutdown_failed_sources(self) -> list[str]:
        """Sources whose final group the archive turned away: the run can no longer be registered as ended normally."""
        return sorted(s.id for s in self.sources.values() if s.shutdown_failed)

    def shutdown_settled(self) -> bool:
        """Every source is done or has failed: nothing more is going to happen to any of them."""
        return all(s.shutdown_done or s.shutdown_failed for s in self.sources.values())

    def run_end_state(self) -> str:
        """NOT_QUEUED (not built, or the queue never took it) | UNCONFIRMED (offered, no answer) | COMMITTED | REFUSED."""
        if self.run_end_status is not None:
            return self.run_end_status
        return "UNCONFIRMED" if self.run_end_offered else "NOT_QUEUED"

    def _clock_jump(self, wall: float, mono: float, delta: float):
        """Wall clock and monotonic clock disagree by more than 2 s: open a new segment per source (§11).

        The monotonic clock keeps measuring durations; the wall label may repeat or run backwards, so order
        within history is carried by (segment, batch) identity, never by wall time alone.
        """
        self.health["clock_jumps"] += 1
        for s in self.sources.values():
            if not s.enabled:
                continue
            marker = self._state(
                s, "CLOCK_JUMP", wall, mono, delta=delta, wall_before=self.last_tick_wall, wall_after=wall
            )
            if s.open_segment and s.archive != "LOST":
                # OK, or RECOVERING - then the cut waits behind the unit in `pending`, so that the segment the unit opens cannot
                # end up holding both halves of the stepped clock
                group = self._segment_group(
                    s, "CLOCK_JUMP", wall, mono, lead=[marker], close_old_at=self.last_tick_wall
                )
                self._publish(group)
                self._submit(s, group)
            else:
                if s.archive == "LOST":  # nothing is queued across a gap: the unit that ends it, or the final group, carries the marker
                    over = max(0, len(s.owed_jumps) + 1 - OWED_JUMPS_LIMIT)
                    s.owed_jumps = [*s.owed_jumps, marker][over:]
                    s.owed_dropped += over
                    self.health["clock_markers_dropped"] += over
                self._emit(rec.GROUP_STATE, s, wall, states=[marker])

    def _watermarks(self, wall: float, mono: float):
        """Every 10 s per source: history is certain up to the last PROVEN communication (§5, §11)."""
        interval = self.config.timing.watermark_interval
        for s in self.sources.values():
            if not s.enabled or s.archive != "OK":
                continue
            if s.watermark_mono is None:
                s.watermark_mono = mono
                continue
            if mono - s.watermark_mono < interval:
                continue
            s.watermark_mono = mono
            mark = self._watermark_state(s, wall, mono)
            if mark is not None and mark.detail["covered_wall"] > s.watermark_wall:
                s.watermark_wall = mark.detail["covered_wall"]
                self._submit(s, self._build(rec.GROUP_WATERMARK, s.id, wall, states=[mark]))

    # ------------------------------------------------------------------ faults

    def _valid_value(self, pid: str):
        """The current value of a tracked source point if it counts (Good, not extreme, source usable)."""
        ps = self.points[pid]
        usable = self.sources[self.point_source[pid]].state == VALID
        return ps.obs.value if (usable and ps.good and ps.obs is not None) else None

    def _fault_restart(self, s: SourceState, mono: float):
        for pid in self.tracked_by_source.get(s.id, ()):
            self.tracker.cancel(pid)
            if pid in self.points:
                value = self._valid_value(pid)
            else:
                out = self.results[pid].current
                value = out.value if out and out.status == rec.RESULT_OK and not out.extreme else None
            self.tracker.observe(pid, value, mono)

    def _advance_faults(self, wall: float, mono: float):
        confirmed: dict[str, list[rec.FaultRecord]] = {}
        refused: dict[str, list[str]] = {}
        for pid, direction in self.tracker.advance(mono):
            source_id = self.point_source.get(pid) or self.result_source[pid]
            record = self._fault_record(pid, direction, self.sources[source_id], wall, mono)
            if record is not None:
                confirmed.setdefault(source_id, []).append(record)
            else:  # the channel IS confirmed: clients must hear it even though no record could be kept
                refused.setdefault(source_id, []).append(pid)
        for source_id in dict.fromkeys((*confirmed, *refused)):
            s, states = self.sources[source_id], []
            if source_id in refused:
                states.append(self._state(s, "FAULT_REFUSED", wall, mono, points=refused[source_id]))
            self._emit(rec.GROUP_FAULT, s, wall, states=states, faults=confirmed.get(source_id, ()))

    def _fault_record(self, pid: str, direction: str, s: SourceState, wall: float, mono: float):
        if pid in self.points:
            obs = self.points[pid].obs
            kind, basis_id, basis_wall, value = "OBSERVATION", obs.id, obs.received_wall, obs.value
            seg, batch_seq = obs.seg, obs.batch_seq
        else:
            last = self.results[pid].emitted
            kind, basis_id, basis_wall, value = "RESULT", last.orig_id or last.id, last.at_wall, last.value
            seg, batch_seq = last.seg, last.batch_seq
        if len(self.fault_buffer) >= self.config.timing.fault_buffer_limit:
            self._fault_rejected(pid, s, wall)  # kept: what is already buffered; refused: this new one
            return None
        n = self.fault_counter[pid] = self.fault_counter.get(pid, 0) + 1
        record = rec.FaultRecord(
            rec.fault_id(self.run_id, pid, n),
            self.run_id,
            s.id,
            seg,
            batch_seq,
            pid,
            direction,
            wall,
            mono,
            kind,
            basis_id,
            basis_wall,
            value,
            self.config_id,
            dict(self.rules[pid]),  # the thresholds and hysteresis in force when it was confirmed
        )
        self.fault_buffer[record.id] = record
        return record

    def _fault_rejected(self, pid: str, s: SourceState, wall: float):
        self.fault_rejected_total += 1
        self.health["fault_archive_incomplete"] = True
        summary = self.fault_loss.setdefault(
            s.id, {"count": 0, "first_wall": wall, "last_wall": wall, "points": set(), "inflight": 0}
        )
        summary["count"] += 1
        summary["last_wall"] = wall
        summary["points"].add(pid)

    def fault_status(self) -> dict:
        """What the current page must show about fault records: incomplete archive and counted losses."""
        return {
            "buffered": len(self.fault_buffer),
            "limit": self.config.timing.fault_buffer_limit,
            "rejected_total": self.fault_rejected_total,
            "archive_incomplete": bool(self.fault_loss) or self.fault_rejected_total > 0,
            "loss": {k: {**v, "points": sorted(v["points"])} for k, v in self.fault_loss.items()},
            "durability": "IN_MEMORY_UNTIL_COMMITTED; lost if the process exits",
        }

    # ------------------------------------------------------------ realtime view

    def channel_views(self) -> dict[str, dict]:
        """How every channel stands now, as a client must show it.

        What a page reads comes from this one definition, taken whole between two batches, so a page cannot show
        a new input beside an old result.  Validity is decided here, never guessed by the page from lifecycle
        records.
        """
        views = {}
        for pid, ps in self.points.items():
            obs = ps.obs
            valid, reason = self._input_validity(pid, ps)
            views[pid] = {
                "kind": "input",
                "value": obs.value if obs else None,
                "quality": obs.quality if obs else None,
                "status_code": obs.status_code if obs else None,
                "time": obs.received_wall if obs else None,  # the observation time, also for a restatement
                "observation_id": obs.id if obs else None,  # 修复轮05: tells a new sample from one already shown
                "origin": obs.origin if obs else None,
                "asserted_wall": obs.asserted_wall if obs else None,
                "extreme": bool(obs and obs.extreme),
                "valid": valid,
                "reason": reason,
                **self._fault_view(pid),
            }
        for pid, state in self.results.items():
            out, last = state.current, state.emitted
            known = out is not None
            views[pid] = {
                "kind": "derived",
                "value": out.value if known else None,
                "status": out.status if known else rec.RESULT_NA,
                "reason": out.reason if known else "UNKNOWN",
                "extreme": bool(known and out.extreme),
                "estimate": out.estimate if known else None,
                "time": last.at_wall if last else None,  # the batch of the last CHANGE; equal results are not re-sent
                "valid": bool(known and out.status == rec.RESULT_OK and not out.extreme),
                **self._fault_view(pid),
            }
        return views

    def _input_validity(self, pid: str, ps: PointState) -> tuple[bool, str | None]:
        """(valid, why not): decided here, once, for the realtime view and for the communication table alike."""
        source = self.sources[self.point_source[pid]]
        usable = source.state == VALID
        valid = bool(usable and ps.good)
        return valid, None if valid else ps.reason if usable else "SOURCE_" + source.state

    def communication_inputs(self) -> list[dict]:
        """Every input as the communication page shows it: ONE row per input, ALL of it from the observation the owner
        holds for that input right now (id, value, quality, receive time, source timestamp), plus its validity as of this
        call.  Synchronous, so a row cannot be assembled from two observations.  A source timestamp is shown as received -
        absent for Modbus and when the server gave none - and judges nothing.  Never-observed inputs are empty, not zero.
        """
        rows = []
        for pid, ps in self.points.items():
            obs = ps.obs
            valid, reason = self._input_validity(pid, ps)
            rows.append(
                {
                    "point_id": pid,
                    "source_id": self.point_source[pid],
                    "observation_id": obs.id if obs else None,
                    "value": obs.value if obs else None,
                    "quality": obs.quality if obs else None,
                    "status_code": obs.status_code if obs else None,
                    "received_time": obs.received_wall if obs else None,
                    "source_time": obs.source_time if obs else None,
                    "valid": valid,
                    "reason": reason,
                }
            )
        return rows

    def _fault_view(self, pid: str) -> dict:
        if pid not in self.rules:
            return {}
        st = self.tracker.state[pid]
        return {"fault": self.tracker.status(pid), "fault_direction": st.confirmed or st.pending}

    def status(self) -> dict:
        """Health that is not a sample: per-source state and what the current page must say about fault records."""
        return {
            "sources": {
                s.id: {
                    "kind": s.kind,
                    "enabled": s.enabled,
                    "state": s.state,
                    "conn_gen": s.conn_gen,
                    "seg": s.seg,
                    "alarm": s.alarm,
                    "archive": s.archive,
                    "last_comm_wall": s.last_comm_wall,
                    "resync_count": s.resync_count,
                    "persistent_resync": s.resync_count >= self.config.timing.resync_persistent_after,
                }
                for s in self.sources.values()
            },
            "faults": self.fault_status(),
        }

    def snapshot(self) -> dict:
        """The whole current state as the live pages read it (终版 §13.3): one synchronous read, whole or nothing.  No
        publish sequence: a page replaces what it shows, it continues nothing."""
        return {
            "run_id": self.run_id,
            "channels": self.channel_views(),
            "status": self.status(),
        }

    # --------------------------------------------------------------- derived

    def _recompute(self, s: SourceState, batch, touched) -> list[rec.Result]:
        out = []
        for rule in self.topology.affected(touched):
            outcome = self._change(rule, s, batch) if rule.operation == "change" else self._plain(rule)
            result = self._record(rule, outcome, s, batch)
            if result is not None:
                out.append(result)
            if rule.id in self.rules:
                if self._multi & self.source_deps[rule.id]:
                    self.tracker.cancel(rule.id)  # §8.2: a differently valued second version restarts the clock
                ok = outcome.status == rec.RESULT_OK and not outcome.extreme
                self.tracker.observe(rule.id, outcome.value if ok else None, batch.mono)
        return out

    def _plain(self, rule) -> Outcome:
        return evaluate(rule, [self._input(n) for n in rule.inputs], self.topology.models)

    def _change(self, rule, s: SourceState, batch) -> Outcome:
        """Increment between two CONSECUTIVE planned rounds of one connection segment (§7)."""
        current = self._input(rule.inputs[0])
        if batch.kind != rec.KIND_MODBUS_POLL or batch.tick is None:
            return unavailable("NOT_A_PLANNED_ROUND")
        if not current.valid or current.value is None:
            self.baselines.pop(rule.id, None)  # a failed / invalid round breaks the chain
            return unavailable("EXTREME_INPUT" if current.extreme else "INPUT_UNAVAILABLE")
        previous = self.baselines.get(rule.id)
        self.baselines[rule.id] = Baseline(batch.tick, current.value, s.conn_gen, s.seg, s.batch_seq)
        if (
            previous is None
            or previous.tick != batch.tick - 1
            or (previous.conn_gen, previous.seg) != (s.conn_gen, s.seg)
        ):
            return unavailable("BASELINE_ONLY")  # first round after failure, skip or reconnect
        value = (current.value - previous.value) * rule.scale + rule.offset
        if not math.isfinite(value):
            return unavailable("NONFINITE")
        basis = {
            "tick": batch.tick,
            "previous_tick": previous.tick,
            "previous_batch_seq": previous.batch_seq,
            "seg": s.seg,
        }
        return Outcome(value, rec.RESULT_OK, None, is_extreme(value, rule), basis=basis)

    def _record(self, rule, outcome: Outcome, s: SourceState, batch) -> rec.Result | None:
        state = self.results[rule.id]
        state.current = outcome
        key = (outcome.value, outcome.status, outcome.reason, outcome.extreme)
        if state.emitted_key == key:
            return None  # unchanged results are not rewritten
        if state.emitted is None and outcome.status != rec.RESULT_OK:
            return None  # unknown stays unknown: nothing is stored until a value has been known
        result = rec.Result(
            id=rec.result_id(self.run_id, s.id, s.batch_seq, rule.id),
            run_id=self.run_id,
            source_id=s.id,
            conn_gen=s.conn_gen,
            seg=s.seg,
            batch_seq=s.batch_seq,
            point_id=rule.id,
            value=outcome.value,
            status=outcome.status,
            reason=outcome.reason,
            config_id=self.config_id,
            at_wall=batch.wall,
            extreme=outcome.extreme,
            basis=outcome.basis,
            estimate=outcome.estimate,
        )
        state.emitted_key, state.emitted = key, result
        return result
