"""Single-owner state engine (终版 §5-§8, §10).

Everything here is synchronous: no await, no database access, no clock reads.  Callers pass the
wall (UTC, labels only) and monotonic (durations) time of every event, so the engine is driven
identically by the live runtime and by tests.  Results leave through two sinks: `commit` receives
ordered CommitGroups (durable), `publish` receives PublishGroups (realtime).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import uuid4

from . import records as rec
from .config import Settings
from .derive import Input, Outcome, Topology, evaluate, unavailable
from .extremes import is_extreme

# Source data states.  Only VALID sources contribute usable values.
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


class Owner:
    def __init__(self, config: Settings, wall: float, mono: float, *, commit=None, publish=None, run_id=None):
        self.config = config
        self.run_id = run_id or uuid4().hex
        self.commit, self.publish = commit or _sink_none, publish or _sink_none
        self.started_wall, self.started_mono = wall, mono
        self.topology = Topology(config)
        self.defs = {p.id: p for p in config.points}
        self.point_source = {p.id: p.source for p in config.points}
        self.sources = {
            s.id: SourceState(
                s.id, s.kind, s.enabled, tuple(p.id for p in config.points if p.source == s.id)
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
        self.order = 0
        self.pub_seq = 0
        self.health = {
            "stale_generation_batches": 0,
            "rejected_items": 0,
            "app_loss": 0,
            "dropped_while_resync": 0,
        }

    # ------------------------------------------------------------------ plumbing

    def _state(self, s: SourceState, kind: str, wall: float, mono: float, **detail) -> rec.StateRecord:
        s.event_no += 1
        return rec.StateRecord(
            rec.state_id(self.run_id, s.id, s.event_no), self.run_id, s.id, s.seg, kind, wall, mono, detail
        )

    def _emit(self, kind, s: SourceState, wall, observations=(), results=(), states=(), faults=()):
        if not (observations or results or states or faults):
            return None
        self.order += 1
        group = rec.CommitGroup(
            rec.group_id(self.run_id, self.order),
            self.order,
            self.run_id,
            s.id,
            kind,
            wall,
            tuple(observations),
            tuple(results),
            tuple(states),
            tuple(faults),
        )
        self.commit(group)
        self.pub_seq += 1
        self.publish(
            rec.PublishGroup(
                self.pub_seq,
                self.order,
                s.id,
                wall,
                group.observations,
                group.results,
                group.states,
                group.faults,
            )
        )
        return group

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
        record = self._state(s, "APP_LOSS", wall, mono, reason=reason, tick=tick, count=count)
        self._emit(rec.GROUP_STATE, s, wall, states=[record])

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
        s.state, s.last_tick = WAITING, None
        for pid in s.point_ids:
            self.points[pid] = PointState()
        for rule in self.topology.rules:
            if self.result_source[rule.id] == s.id:
                self.results[rule.id] = ResultState()
                self.baselines.pop(rule.id, None)
        states.append(self._state(s, "SEG_START", wall, mono, conn_gen=gen, reason=reason))
        self._emit(rec.GROUP_STATE, s, wall, states=states)

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
        s.batch_seq += 1
        if batch.kind == rec.KIND_MODBUS_POLL:
            return self._accept_poll(s, batch)
        return self._accept_items(s, batch)

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

    def _accept_items(self, s: SourceState, batch) -> bool:
        if s.state == WAITING:
            s.state = VALID
        observations, touched = [], []
        for item in batch.items:
            obs = self._observe(s, batch, item, len(observations))
            if obs is None:
                continue
            self._apply(obs)
            observations.append(obs)
            touched.append(obs.point_id)
        results = self._recompute(s, batch, touched)
        self._emit(rec.GROUP_BATCH, s, batch.wall, observations, results)
        return True

    def _accept_poll(self, s: SourceState, batch) -> bool:
        if batch.outcome != "OK":
            return self._poll_failed(s, batch)
        for pid in s.point_ids:  # only this round's responses count; nothing is borrowed
            ps = self.points[pid]
            ps.good, ps.reason = False, "POLL_MISSING"
        s.state, s.last_tick = VALID, batch.tick
        observations, touched = [], list(s.point_ids)
        for item in batch.items:
            obs = self._observe(s, batch, item, len(observations))
            if obs is None:
                continue
            self._apply(obs)
            observations.append(obs)
        results = self._recompute(s, batch, touched)
        poll = self._state(s, "POLL", batch.wall, batch.mono, **self._poll_detail(s, batch, observations))
        self._emit(rec.GROUP_BATCH, s, batch.wall, observations, results, [poll])
        return True

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

    def _poll_failed(self, s: SourceState, batch) -> bool:
        for pid in s.point_ids:
            ps = self.points[pid]
            ps.good, ps.reason = False, "POLL_" + batch.outcome
        s.last_tick = batch.tick
        results = self._recompute(s, batch, list(s.point_ids))
        poll = self._state(s, "POLL", batch.wall, batch.mono, **self._poll_detail(s, batch, ()))
        self._emit(rec.GROUP_BATCH, s, batch.wall, (), results, [poll])
        return True

    # --------------------------------------------------------------- derived

    def _recompute(self, s: SourceState, batch, touched) -> list[rec.Result]:
        out = []
        for rule in self.topology.affected(touched):
            outcome = self._change(rule, s, batch) if rule.operation == "change" else self._plain(rule)
            result = self._record(rule, outcome, s, batch)
            if result is not None:
                out.append(result)
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
            config_id=self.config.config_id,
            at_wall=batch.wall,
            extreme=outcome.extreme,
            basis=outcome.basis,
            estimate=outcome.estimate,
        )
        state.emitted_key, state.emitted = key, result
        return result
