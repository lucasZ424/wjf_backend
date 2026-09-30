"""Synchronous, single-owner state engine. No I/O and no await in state transitions."""

from __future__ import annotations

import json
import math
import statistics
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable
from uuid import uuid4

from .capture import CapturePlan, FrameSnapshot
from .config import Settings


@dataclass(frozen=True)
class Sample:
    id: str
    run_id: str
    seq: int
    point_id: str
    value: float | None
    quality: str
    source_time: float | None
    received_at: float
    effective_time: float
    mono: float
    source_seq: int | None = None


@dataclass
class PointState:
    ring: deque = field(default_factory=deque)
    latest: Sample | None = None
    usable: Sample | None = None
    holds: deque = field(default_factory=deque)
    frontier: Sample | None = None
    state: str = "UNKNOWN"
    available: bool = False
    seen_mono: float = 0


@dataclass
class Event:
    id: str
    profile_id: str
    run_id: str
    config_id: str
    start: float
    triggered_at: float
    phase: str = "ACTIVE"
    end: float | None = None
    close_reason: str | None = None
    recovered_at: float | None = None
    archive_status: str = "PENDING"
    coverage: str = "UNKNOWN"
    recovery_mono: float | None = None
    post_mono: float | None = None
    unavailable_mono: float | None = None
    started_mono: float = 0
    previous_id: str | None = None
    revision: int = 0


class Engine:
    def __init__(
        self,
        config: Settings,
        emit: Callable[[str, dict, bool], None],
        wall: float,
        mono: float,
        defer_calculation=False,
    ):
        self.config, self.emit = config, emit
        self.defer_calculation = defer_calculation
        self.points = {p.id: p for p in config.points}
        self.profiles = {p.id: p for p in config.profiles}
        self.states = {p.id: PointState() for p in config.points}
        self.run_id, self.seq = str(uuid4()), 0
        self.window = math.floor(wall)
        self.started_at, self.last_wall, self.last_mono = wall, wall, mono
        self.windows: dict[int, dict[str, list[Sample]]] = {}
        self.window_overflow: set[int] = set()
        self.window_facts: dict[int, dict[str, set[str]]] = {}
        self.active: dict[str, Event] = {}
        self.pending: list[Event] = []
        self.suppressed: dict[str, str] = {}
        self.blocked: dict[str, str] = {}
        self.inflight_frames = set()
        self.frame_tail_seen = set()
        self.capture_pending = set()
        self.loss_spans = {}
        self.latest_frame: dict | None = None
        self.recent_frames: deque = deque()
        self.previous: dict | None = None
        self.health: dict[str, str | int] = {}
        self.capture_available = True
        self.max_pre = max((p.pre for p in config.profiles), default=600)

    def event_record(self, event: Event):
        event.revision += 1
        self.emit("event", asdict(event), True)

    def gap(self, reason: str, start: float, end: float, event_id: str | None = None, **details):
        self.emit(
            "gap",
            {
                "id": str(uuid4()),
                "reason": reason,
                "start": start,
                "end": end,
                "event_id": event_id,
                "run_id": self.run_id,
                **details,
            },
            True,
        )

    def assess(self, point_id, value):
        point = self.points[point_id]
        if point.high is not None and (value > point.high or point.inclusive and value == point.high):
            return "HIGH"
        if point.low is not None and (value < point.low or point.inclusive and value == point.low):
            return "LOW"
        return "NORMAL"

    def capture(self, event, sample):
        self.emit("sample", {"event_id": event.id, "event": asdict(event), "sample": asdict(sample)}, True)

    def receive(self, point_id, value, wall, mono, quality="GOOD", source_time=None, source_seq=None):
        if point_id not in self.points:
            raise ValueError("unknown point")
        self.seq += 1
        effective = wall if self.config.time_basis == "received" else source_time
        if effective is None or not math.isfinite(effective):
            effective, quality = wall, "BAD_TIME"
        if value is None or not math.isfinite(value):
            value, quality = None, "BAD"
        future = effective > wall
        if future:
            quality = "BAD_TIME"
        sample = Sample(
            str(uuid4()),
            self.run_id,
            self.seq,
            point_id,
            value,
            quality,
            source_time,
            wall,
            effective,
            mono,
            source_seq,
        )
        state = self.states[point_id]
        state.latest = sample
        state.ring.append(sample)
        # Keep one observation at/before the time boundary as coverage evidence.
        # Requiring an exact poll at the boundary would reject ordinary polling jitter.
        # The hard sample cap still wins; losing this predecessor keeps Pre partial.
        while state.ring and (
            len(state.ring) > self.config.limits.ring_samples
            or len(state.ring) > 1
            and state.ring[1].mono <= mono - self.max_pre
        ):
            state.ring.popleft()
        previous = state.frontier
        in_order = (
            previous is None
            or effective > previous.effective_time
            or (
                effective == previous.effective_time
                and source_seq is not None
                and previous.source_seq is not None
                and source_seq > previous.source_seq
            )
        )
        # Known historical observations never prove recovery in receive-time fallback either.
        known_old = (
            source_time is not None
            and previous is not None
            and previous.source_time is not None
            and source_time < previous.source_time
        )
        in_order = (
            in_order
            and not future
            and not known_old
            and abs(effective - wall) <= self.config.limits.historical_age
        )
        # A sample belonging to an already published source-time bucket is a historical fact,
        # even if it is newer than the last observation of a very slow point.
        in_order = in_order and math.floor(effective) >= self.window
        assessment = self.assess(point_id, value) if quality == "GOOD" else "NOT_EVALUATED"
        if in_order:
            state.frontier, state.seen_mono = sample, mono
            state.available = quality == "GOOD"
            state.state = assessment if state.available else "UNKNOWN"
            if state.available:
                state.usable = sample
                state.holds.append(sample)
                while len(state.holds) > self.config.limits.ring_samples:
                    state.holds.popleft()
            else:
                state.holds.clear()
        elif quality != "GOOD" and previous is not None and effective == previous.effective_time:
            state.available, state.state = False, "UNKNOWN"
            state.holds.clear()
        if future:
            self.note_loss(point_id, "FUTURE_SOURCE_TIME", wall, wall, 1)
        window = math.floor(effective)
        if not future and self.window <= window <= self.window + 5:
            if assessment in ("HIGH", "LOW"):
                self.window_facts.setdefault(window, {}).setdefault(point_id, set()).add(assessment)
            bucket = self.windows.setdefault(window, {}).setdefault(point_id, [])
            if len(bucket) < self.config.limits.samples_per_window:
                bucket.append(sample)
            else:
                self.window_overflow.add(window)
                self.health["window_overflow"] = window
        abnormal = assessment in ("HIGH", "LOW")
        for profile in self.config.profiles:
            event = self.active.get(profile.id)
            if abnormal and point_id in profile.trigger_points:
                if in_order:
                    opened = event is None
                    if event is None and self.capture_available and profile.id not in self.blocked:
                        event = self.open_event(profile, sample, mono)
                    elif event is not None:
                        event.phase, event.recovery_mono, event.post_mono = "ACTIVE", None, None
                        event.recovered_at = None
                    if event:
                        self.trigger(event, sample, assessment, False)
                        if opened:
                            self.plan_pre(event, profile, sample, mono)
                else:
                    # Historical facts have a separate closed capture; do not change current state.
                    historical = Event(
                        str(uuid4()),
                        profile.id,
                        self.run_id,
                        self.config.config_id,
                        effective - profile.pre,
                        effective,
                        phase="CLOSED",
                        end=effective,
                        close_reason="HISTORICAL",
                        coverage="PARTIAL",
                        archive_status="SUBMITTED",
                    )
                    self.event_record(historical)
                    self.trigger(historical, sample, assessment, True)
                    if wall - effective <= self.config.limits.historical_age:
                        self.capture(historical, sample)
                    else:
                        self.gap("HISTORICAL_OUTSIDE_LIMIT", effective, wall, historical.id)
            if event and point_id in profile.source_points:
                self.capture(event, sample)
        self.advance_events(wall, mono)
        return sample

    def trigger(self, event, sample, assessment, historical):
        self.emit(
            "trigger",
            {
                "id": sample.id + ":" + event.id,
                "event_id": event.id,
                "point_id": sample.point_id,
                "sample_id": sample.id,
                "value": sample.value,
                "state": assessment,
                "time": sample.effective_time,
                "detected_at": sample.received_at,
                "window_start": math.floor(sample.effective_time),
                "run_id": sample.run_id,
                "historical": historical,
                "rule": self.points[sample.point_id].model_dump(),
            },
            True,
        )
        self.event_record(event)

    def open_event(self, profile, sample, mono):
        event = Event(
            str(uuid4()),
            profile.id,
            self.run_id,
            self.config.config_id,
            sample.effective_time - profile.pre,
            sample.effective_time,
            started_mono=mono,
            previous_id=self.suppressed.pop(profile.id, None),
        )
        self.active[profile.id] = event
        # OBSERVED concerns retained observations, not GOOD quality or continuous physics.
        # Prove the boundary with an exact observation or a non-stale bracketing pair.
        # An arbitrarily old predecessor after silence must not certify coverage.
        boundary = mono - profile.pre

        def observed_boundary(point_id):
            before = None
            for old in self.states[point_id].ring:
                if old.mono == boundary:
                    return True
                if old.mono > boundary:
                    return before is not None and old.mono - before.mono <= self.points[point_id].stale_after
                before = old
            return False

        # Known losses overlapping Pre still force PARTIAL below.
        complete_pre = all(observed_boundary(p) for p in profile.source_points)
        event.coverage = "OBSERVED" if complete_pre else "PARTIAL"
        if not complete_pre:
            self.gap("PRE_INSUFFICIENT", event.start, sample.effective_time, event.id)
        for p in profile.source_points:
            span = self.loss_spans.get(p)
            if span and span[1] >= event.start:
                event.coverage = "PARTIAL"
                self.gap("PRE_SOURCE_LOSS", max(event.start, span[0]), span[1], event.id)
        self.event_record(event)
        return event

    def plan_pre(self, event, profile, sample, mono):
        samples = tuple(
            old
            for p in profile.source_points
            for old in self.states[p].ring
            if old.seq < sample.seq and old.mono >= mono - profile.pre
        )
        frames = tuple(f for f in self.recent_frames if f.window_end > event.start)
        plan = CapturePlan(json.dumps(asdict(event)).encode(), samples, frames)
        accepted = self.emit("capture_plan", {"plan": plan, "event_id": event.id}, True)
        if accepted is True:
            self.capture_pending.add(event.id)
        elif accepted is False:
            event.coverage = "PARTIAL"
            self.gap("PRE_BUDGET_EXHAUSTED", event.start, event.triggered_at, event.id)
            self.event_record(event)

    def note_loss(self, point_id, reason, start, end, count):
        self.health["lost_observations"] = self.health.get("lost_observations", 0) + count
        old = self.loss_spans.get(point_id)
        if old and end - old[1] <= self.max_pre:
            start, count = min(start, old[0]), count + old[2]
        self.loss_spans[point_id] = (start, end, count)
        self.gap(reason, start, end, point_id=point_id, count=count)
        for event in [*self.active.values(), *self.pending]:
            if (
                point_id in self.profiles[event.profile_id].source_points
                and event.start <= end
                and (event.end is None or event.end >= start)
            ):
                event.coverage = "PARTIAL"
                self.event_record(event)
                self.gap(reason, max(start, event.start), end, event.id, point_id=point_id, count=count)

    def capture_complete(self, identity, success):
        self.capture_pending.discard(identity)
        for event in [*self.active.values(), *self.pending]:
            if event.id == identity and not success:
                event.coverage = "PARTIAL"
                self.gap("PRE_ARCHIVE_FAILED", event.start, event.triggered_at, event.id)
                self.event_record(event)
        self.retire_pending()

    def retire_pending(self):
        for event in list(self.pending):
            if event.id not in self.frame_tail_seen or event.id in self.capture_pending:
                continue
            if any(
                run == event.run_id and start < event.end and start + 1 > event.start
                for run, start in self.inflight_frames
            ):
                continue
            event.archive_status = "SUBMITTED"
            self.event_record(event)
            self.pending.remove(event)
            self.frame_tail_seen.discard(event.id)

    def close_event(self, event, wall, reason):
        event.phase, event.end, event.close_reason = "CLOSED", wall, reason
        self.active.pop(event.profile_id, None)
        if reason != "RECOVERED":
            event.coverage = "PARTIAL"
            self.suppressed[event.profile_id] = event.id
            self.blocked[event.profile_id] = reason
            self.gap(reason, event.triggered_at, wall, event.id)
        self.pending.append(event)
        self.event_record(event)

    def advance_events(self, wall, mono):
        for profile_id in list(self.blocked):
            if self.blocked[profile_id] == "RESOURCE_LIMITED" and not self.capture_available:
                continue
            profile = self.profiles[profile_id]
            if all(
                self.states[p].available and self.states[p].state == "NORMAL" for p in profile.trigger_points
            ):
                self.blocked.pop(profile_id, None)
        for event in list(self.active.values()):
            profile = self.profiles[event.profile_id]
            states = [self.states[p] for p in profile.trigger_points]
            available = all(x.available for x in states)
            normal = available and all(x.state == "NORMAL" for x in states)
            abnormal = any(x.available and x.state in ("HIGH", "LOW") for x in states)
            if not available:
                if event.unavailable_mono is None:
                    event.unavailable_mono = mono
                if event.phase == "RECOVERY_PENDING":
                    event.phase, event.recovery_mono = "ACTIVE", None
            else:
                event.unavailable_mono = None
            if (
                not self.capture_available
                or mono - event.started_mono >= self.config.limits.max_capture
                or event.unavailable_mono is not None
                and mono - event.unavailable_mono >= self.config.limits.unavailable_timeout
            ):
                self.close_event(
                    event,
                    wall,
                    "RESOURCE_LIMITED"
                    if not self.capture_available
                    else (
                        "CAPTURE_DURATION_LIMIT"
                        if mono - event.started_mono >= self.config.limits.max_capture
                        else "SOURCE_UNAVAILABLE_TIMEOUT"
                    ),
                )
                continue
            if abnormal:
                changed = event.phase != "ACTIVE"
                event.phase, event.recovery_mono, event.post_mono, event.recovered_at = (
                    "ACTIVE",
                    None,
                    None,
                    None,
                )
                if changed:
                    self.event_record(event)
            elif normal and event.phase == "ACTIVE":
                event.phase, event.recovery_mono = "RECOVERY_PENDING", mono
                self.event_record(event)
            if event.phase == "RECOVERY_PENDING" and mono - event.recovery_mono >= profile.recovery:
                event.phase, event.recovered_at, event.post_mono = "POST_CAPTURE", wall, mono
                self.event_record(event)
            if event.phase == "POST_CAPTURE" and mono - event.post_mono >= profile.post:
                self.close_event(event, wall, "RECOVERED")

    def disconnected(self, source_id, wall, mono):
        for point in self.config.points:
            if point.source == source_id:
                self.states[point.id].available = False
                self.states[point.id].state = "UNKNOWN"
                self.states[point.id].holds.clear()
        self.health[source_id] = "DISCONNECTED"
        self.advance_events(wall, mono)

    def tick(self, wall, mono):
        if abs((wall - self.last_wall) - (mono - self.last_mono)) > self.config.limits.clock_jump:
            for event in list(self.active.values()):
                self.close_event(event, self.last_wall, "CLOCK_DISCONTINUITY")
            self.gap("CLOCK_DISCONTINUITY", min(wall, self.last_wall), max(wall, self.last_wall))
            for event in self.pending:
                event.archive_status = "INTERRUPTED"
                event.coverage = "PARTIAL"
                self.event_record(event)
            self.pending.clear()
            self.inflight_frames.clear()
            self.frame_tail_seen.clear()
            self.loss_spans.clear()
            self.run_id, self.window = str(uuid4()), math.floor(wall)
            self.windows.clear()
            self.window_overflow.clear()
            self.window_facts.clear()
            self.recent_frames.clear()
            self.previous = None
            self.latest_frame = None
            for state in self.states.values():
                state.ring.clear()
                state.holds.clear()
                state.available, state.usable, state.frontier = False, None, None
            self.health["clock_discontinuity"] = wall
        self.last_wall, self.last_mono = wall, mono
        for point_id, state in self.states.items():
            if mono - state.seen_mono > self.points[point_id].stale_after:
                state.available, state.state = False, "UNKNOWN"
                state.holds.clear()
        self.advance_events(wall, mono)
        # Bound catch-up work after scheduler stalls, with explicit missing coverage.
        if wall - self.window > 10:
            self.gap("SCHEDULER_GAP", self.window, math.floor(wall) - 1)
            self.window = math.floor(wall) - 1
            self.windows = {k: v for k, v in self.windows.items() if k >= self.window}
            self.window_facts = {k: v for k, v in self.window_facts.items() if k >= self.window}
            self.window_overflow = {k for k in self.window_overflow if k >= self.window}
        while self.window + 1 + self.config.limits.window_wait <= wall:
            frame = self.make_frame(self.window)
            if self.defer_calculation:
                self.emit("frame_input", {"frame": frame}, False)
            else:
                self.finish_frame(frame)
            self.window += 1

    def finish_frame(self, frame):
        self.inflight_frames.discard((frame["run_id"], frame["window_start"]))
        ids = [
            e.id
            for e in [*self.active.values(), *self.pending]
            if frame["run_id"] == e.run_id
            and frame["window_end"] > e.start
            and (e.end is None or frame["window_start"] < e.end)
        ]
        display = frame["run_id"] == self.run_id and (
            self.latest_frame is None
            or self.latest_frame["run_id"] != self.run_id
            or frame["window_start"] > self.latest_frame["window_start"]
        )
        self.emit("frame", {"frame": frame, "event_ids": ids, "display": display}, bool(ids))
        if frame["run_id"] == self.run_id:
            if (
                self.latest_frame is None
                or self.latest_frame["run_id"] != self.run_id
                or frame["window_start"] > self.latest_frame["window_start"]
            ):
                self.latest_frame, self.previous = frame, frame
            self.recent_frames.append(FrameSnapshot.seal(frame))
            while (
                self.recent_frames and frame["window_end"] - self.recent_frames[0].window_end > self.max_pre
            ):
                self.recent_frames.popleft()
        for event in self.pending:
            if frame["run_id"] == event.run_id and event.end is not None and frame["window_end"] >= event.end:
                self.frame_tail_seen.add(event.id)
        self.retire_pending()

    def make_frame(self, start):
        self.inflight_frames.add((self.run_id, start))
        buckets = self.windows.pop(start, {})
        facts = self.window_facts.pop(start, {})
        values = {}
        for point_id, point in self.points.items():
            raw = buckets.get(point_id, [])
            good = [s for s in raw if s.quality == "GOOD"]
            state = self.states[point_id]
            entry = {
                "value": None,
                "quality": "UNAVAILABLE",
                "basis": "UNAVAILABLE",
                "basis_time": None,
                "first_time": None,
                "last_time": None,
                "high_occurred": "HIGH" in facts.get(point_id, ()),
                "low_occurred": "LOW" in facts.get(point_id, ()),
                "sample_ids": [],
            }
            if good:
                numbers = [s.value for s in good]
                last = max(good, key=lambda s: (s.effective_time, s.seq))
                match point.aggregation:
                    case "Mean":
                        try:
                            value = statistics.fmean(numbers)
                        except OverflowError:
                            value = math.nan
                    case "Median":
                        value = statistics.median(numbers)
                    case "RMS":
                        value = math.hypot(*numbers) / math.sqrt(len(numbers))
                    case "Last":
                        value = last.value
                    case "Peak":
                        value = max(numbers)
                    case "MaxAbs":
                        value = max(reversed(numbers), key=abs)
                entry.update(
                    value=value if math.isfinite(value) else None,
                    quality="GOOD",
                    basis="WINDOW_VALUE",
                    basis_time=last.effective_time,
                    first_time=min(s.effective_time for s in good),
                    last_time=last.effective_time,
                    sample_ids=[s.id for s in good],
                )
                if len(good) != len(raw) or start in self.window_overflow or not math.isfinite(value):
                    entry["quality"] = "PARTIAL"
            elif state.available and state.holds:
                old = next((s for s in reversed(state.holds) if s.effective_time < start + 1), None)
                age = start + 1 - old.effective_time if old else float("inf")
                if old and 0 <= age <= point.max_age:
                    entry.update(
                        value=old.value,
                        quality="GOOD",
                        basis="HELD_VALUE",
                        basis_time=old.effective_time,
                        sample_ids=[old.id],
                    )
            while len(state.holds) > 1 and state.holds[1].effective_time < start + 1:
                state.holds.popleft()
            values[point_id] = entry
        self.window_overflow.discard(start)
        frame = {
            "id": f"{self.run_id}:{start}",
            "run_id": self.run_id,
            "config_id": self.config.config_id,
            "window_start": start,
            "window_end": start + 1,
            "partial": start < self.started_at,
            "values": values,
            "derived_results": {},
        }
        if self.defer_calculation:
            return frame
        from .calculation import calculate

        return calculate(self.config, frame, self.previous)

    def shutdown(self, wall):
        for event in list(self.active.values()):
            self.close_event(event, wall, "PROCESS_INTERRUPTED")
