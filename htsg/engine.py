"""Synchronous, single-owner state engine. No I/O and no await in state transitions."""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable
from uuid import uuid4

from .config import Settings
from .faults import FaultState


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
        self.states = {p.id: PointState() for p in config.points}
        self.faults = {p.id: FaultState() for p in config.points}
        self.run_id, self.seq = str(uuid4()), 0
        self.window = math.floor(wall)
        self.started_at, self.last_wall, self.last_mono = wall, wall, mono
        self.windows: dict[int, dict[str, list[Sample]]] = {}
        self.window_overflow: set[int] = set()
        self.window_facts: dict[int, dict[str, set[str]]] = {}
        self.loss_spans = {}
        self.latest_frame: dict | None = None
        self.previous: dict | None = None
        self.health: dict[str, str | int] = {}
        self.max_pre = 5

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
        if point.high is not None and (value > point.high):
            return "HIGH"
        if point.low is not None and (value < point.low):
            return "LOW"
        return "NORMAL"

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
        # Every input is archived, including normal and invalid observations.
        self.emit("raw", {"sample": asdict(sample)}, False)
        fault = self.faults[point_id]
        if not in_order or quality != "GOOD":
            fault.interrupt()
        elif fault.observe(assessment, mono):
            self.emit(
                "fault",
                {
                    "id": sample.id,
                    "sample_id": sample.id,
                    "point_id": point_id,
                    "value": sample.value,
                    "unit": self.points[point_id].unit,
                    "state": assessment,
                    "time": sample.received_at,
                    "run_id": self.run_id,
                    "config_id": self.config.config_id,
                    "rule": self.points[point_id].model_dump(),
                },
                True,
            )
        return sample

    def note_loss(self, point_id, reason, start, end, count):
        self.faults[point_id].interrupt()
        self.health["lost_observations"] = self.health.get("lost_observations", 0) + count
        old = self.loss_spans.get(point_id)
        if old and end - old[1] <= self.max_pre:
            start, count = min(start, old[0]), count + old[2]
        self.loss_spans[point_id] = (start, end, count)
        self.gap(reason, start, end, point_id=point_id, count=count)

    def disconnected(self, source_id, wall, mono):
        for point in self.config.points:
            if point.source == source_id:
                self.faults[point.id].interrupt()
                self.states[point.id].available = False
                self.states[point.id].state = "UNKNOWN"
                self.states[point.id].holds.clear()
        self.health[source_id] = "DISCONNECTED"

    def tick(self, wall, mono):
        if abs((wall - self.last_wall) - (mono - self.last_mono)) > self.config.limits.clock_jump:
            self.gap("CLOCK_DISCONTINUITY", min(wall, self.last_wall), max(wall, self.last_wall))
            for fault in self.faults.values():
                fault.interrupt()
            self.run_id, self.window = str(uuid4()), math.floor(wall)
            self.windows.clear()
            self.window_overflow.clear()
            self.window_facts.clear()
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
        display = frame["run_id"] == self.run_id and (
            self.latest_frame is None
            or self.latest_frame["run_id"] != self.run_id
            or frame["window_start"] > self.latest_frame["window_start"]
        )
        self.emit("frame", {"frame": frame, "event_ids": [], "display": display}, False)
        if frame["run_id"] == self.run_id:
            if (
                self.latest_frame is None
                or self.latest_frame["run_id"] != self.run_id
                or frame["window_start"] > self.latest_frame["window_start"]
            ):
                self.latest_frame, self.previous = frame, frame

    def make_frame(self, start):
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
        for fault in self.faults.values():
            fault.interrupt()
