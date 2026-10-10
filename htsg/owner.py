"""Single-owner state engine (终版 §5-§8, §10, §11).

Everything here is synchronous: no await, no database access, no clock reads.  Callers pass the
wall (UTC, labels only) and monotonic (durations) time of every event, so the engine is driven
identically by the live runtime and by tests.  Results leave through two sinks: `commit` receives
ordered CommitGroups (durable), `publish` receives PublishGroups (realtime).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from uuid import uuid4

from . import records as rec
from .config import Settings
from .derive import Input, Outcome, Topology, evaluate, unavailable
from .extremes import is_extreme

# Source data states.  Only VALID sources contribute usable values.
#   WAITING  new connection / subscription generation, nothing received yet
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
    wait_mono: float | None = None
    alarm: bool = False
    last_outcome: str | None = None
    resync_reason: str | None = None


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
        self.order = 0
        self.pub_seq = 0
        self.health = {
            "stale_generation_batches": 0,
            "rejected_items": 0,
            "app_loss": 0,
            "dropped_while_resync": 0,
            "resyncs": 0,
            "expiries": 0,
            "recoveries": 0,
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

    # ------------------------------------------------------------ communication

    def _comm(self, s: SourceState, wall: float, mono: float) -> list[rec.StateRecord]:
        """A valid response arrived: only this advances the communication time (never a reconnect attempt)."""
        s.last_comm_wall, s.last_comm_mono = wall, mono
        if s.alarm:
            s.alarm = False
            return [self._state(s, "COMM_RESTORED", wall, mono)]
        return []

    def _alarm_limit(self, s: SourceState) -> float:
        t = self.config.timing
        return t.modbus_alarm_seconds if s.kind == "modbus" else t.ua_alarm_seconds

    def tick(self, wall: float, mono: float):
        """Once per second from the monotonic schedule.  Communication is judged before anything else."""
        for s in self.sources.values():
            if s.enabled:  # a source switched off by configuration never accumulates a communication alarm
                self._check_source(s, wall, mono)

    def _check_source(self, s: SourceState, wall: float, mono: float):
        t = self.config.timing
        baseline = s.last_comm_mono if s.last_comm_mono is not None else s.enabled_mono
        if not s.alarm and mono - baseline >= self._alarm_limit(s):
            s.alarm = True
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
        if s.kind == "modbus" or s.state == RESYNC:
            return
        # OPC UA: silence is measured from the later of the last response and this subscription's own start
        latest = max(
            s.last_comm_mono if s.last_comm_mono is not None else -math.inf,
            s.wait_mono if s.wait_mono is not None else -math.inf,
        )
        quiet = mono - latest
        if s.state == VALID and quiet >= t.ua_data_validity:
            self._expire(s, wall, mono)
        if quiet >= t.ua_alarm_seconds:
            self._start_resync(s, "SILENCE", wall, mono)

    def _invalidate(self, s: SourceState):
        """Cached values stay but stop counting: change chains break and derived results are unavailable."""
        for rule in self.topology.rules:
            if self.result_source[rule.id] == s.id:
                self.baselines.pop(rule.id, None)
                self.results[rule.id].current = unavailable("SOURCE_INVALID")

    def _expire(self, s: SourceState, wall: float, mono: float):
        """3 s without a valid response: data unusable, segment cut, no rebuild (§11)."""
        end = min(wall, (s.last_comm_wall or wall) + self.config.timing.ua_data_validity)
        s.state = EXPIRED
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
        s.state, s.resync_reason = RESYNC, reason
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
        s.state, s.last_tick, s.wait_mono, s.resync_reason = WAITING, None, mono, None
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
        for item in batch.items:
            obs = self._observe(s, batch, item, len(observations))
            if obs is None:
                continue
            self._apply(obs)
            observations.append(obs)
            touched.append(obs.point_id)
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
        states = self._comm(s, batch.wall, batch.mono) if batch.outcome in ("OK", "EXCEPTION") else []
        s.batch_seq += 1
        if s.state == WAITING:
            s.state = VALID
        if batch.outcome != "OK":
            return self._poll_failed(s, batch, states)
        for pid in s.point_ids:  # only this round's responses count; nothing is borrowed
            ps = self.points[pid]
            ps.good, ps.reason = False, "POLL_MISSING"
        s.last_tick = batch.tick
        observations, _ = self._ingest(s, batch)
        results = self._recompute(s, batch, list(s.point_ids))
        poll = self._state(s, "POLL", batch.wall, batch.mono, **self._poll_detail(s, batch, observations))
        self._emit(rec.GROUP_BATCH, s, batch.wall, observations, results, [*states, poll])
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

    def _poll_failed(self, s: SourceState, batch, states) -> bool:
        for pid in s.point_ids:
            ps = self.points[pid]
            ps.good, ps.reason = False, "POLL_" + batch.outcome
        s.last_tick = batch.tick
        results = self._recompute(s, batch, list(s.point_ids))
        poll = self._state(s, "POLL", batch.wall, batch.mono, **self._poll_detail(s, batch, ()))
        self._emit(rec.GROUP_BATCH, s, batch.wall, (), results, [*states, poll])
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
            asserted = replace(
                last,
                id=rec.result_assertion_id(self.run_id, s.id, s.seg, rule.id),
                seg=s.seg,
                origin=rec.ORIGIN_ASSERTION,
                orig_id=last.id,
                asserted_wall=wall,
            )
            state.emitted = asserted
            results.append(asserted)
        return observations, results

    def _recover(self, s: SourceState, batch, states) -> bool:
        """Same subscription publishes again inside the 3-6 s window: new segment, no rebuild (§11).

        New segment + baseline of the retained cache + the recovery batch + updated derived results
        are ONE commit group and ONE publish group: the old cache is never certified as current alone.
        """
        s.seg, s.open_segment, s.state = s.seg + 1, True, VALID
        self.health["recoveries"] += 1
        states = [*states, self._state(s, "SEG_START", batch.wall, batch.mono, conn_gen=s.conn_gen, reason="RECOVERY")]
        base_obs, base_results = self._baseline(s, batch.wall, batch.mono)
        observations = []
        if batch.items:
            s.batch_seq += 1
            observations, _ = self._ingest(s, batch)
        results = self._recompute(s, batch, list(s.point_ids))  # validity returned: revisit every dependent
        self._emit(
            rec.GROUP_RECOVERY,
            s,
            batch.wall,
            [*base_obs, *observations],
            [*base_results, *results],
            states,
        )
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
