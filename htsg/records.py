"""Typed records of the batch-driven pipeline (终版设计 §5, §6, §10).

Identities are deterministic strings built from run / source / batch / item numbers, so a
retried commit group carries the same ids and the database can deduplicate it instead of
recording history twice.  Wall time is UTC epoch seconds and only labels records; durations
are computed from the monotonic clock.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

GOOD, BAD, UNCERTAIN = "GOOD", "BAD", "UNCERTAIN"
MISSING = "MISSING_"  # quality prefix of a failure record: an input a failed Modbus round took away (no value, §5, §14)
ORIGIN_RECEIVED = "RECEIVED"
ORIGIN_ASSERTION = "STATE_ASSERTION"
ORIGIN_BASELINE = "RETENTION_BASELINE"  # the retention-boundary baseline of a bucket (§5, §13.6): not a sample either
KIND_MODBUS_POLL = "MODBUS_POLL"
KIND_UA_NOTIFY = "UA_NOTIFY"
RESULT_OK, RESULT_NA = "OK", "NOT_AVAILABLE"

# Commit-group kinds.
GROUP_BATCH = "BATCH"  # accepted input batch and the results computed from it
GROUP_STATE = "STATE"  # validity / segment / poll bookkeeping without new input
GROUP_FAULT = "FAULT"  # timer-confirmed faults
GROUP_RECOVERY = "RECOVERY"  # gap + new segment + baseline + first watermark
GROUP_WATERMARK = "WATERMARK"
GROUP_BASELINE = "BASELINE"  # the retention-boundary baseline of all sources, one group (§13.6)


def observation_id(run_id: str, source_id: str, batch_seq: int, item_seq: int) -> str:
    return f"{run_id}/{source_id}/b{batch_seq}/i{item_seq}"


def assertion_id(run_id: str, source_id: str, seg: int, point_id: str) -> str:
    return f"{run_id}/{source_id}/a{seg}/{point_id}"


def result_id(run_id: str, source_id: str, batch_seq: int, point_id: str) -> str:
    return f"{run_id}/{source_id}/b{batch_seq}/r/{point_id}"


def result_assertion_id(run_id: str, source_id: str, seg: int, point_id: str) -> str:
    return f"{run_id}/{source_id}/a{seg}/r/{point_id}"


def baseline_id(run_id: str, set_no: int, kind: str, point_id: str) -> str:
    """A record of retention baseline `set_no`: its own id - the record it restates is referenced, never reused."""
    return f"{run_id}/rb{set_no}/{kind}/{point_id}"


def state_id(run_id: str, source_id: str, event_no: int) -> str:
    return f"{run_id}/{source_id}/e{event_no}"


def fault_id(run_id: str, point_id: str, confirmation_no: int) -> str:
    return f"{run_id}/fault/{point_id}/{confirmation_no}"


def group_id(run_id: str, order: int) -> str:
    return f"{run_id}/g{order}"


@dataclass(frozen=True, slots=True)
class Item:
    """One protocol value inside an input batch, in delivery order."""

    point_id: str
    value: float | None
    quality: str = GOOD
    status_code: int | None = None  # full 32-bit protocol status, kept even when not Good
    source_time: float | None = None
    response_id: str | None = None  # Modbus: which response of the poll carried it
    wall: float | None = None  # per-response receive time; defaults to the batch time
    mono: float | None = None


@dataclass(frozen=True, slots=True)
class RequestResult:
    """How ONE request of a Modbus round ended (the communication log keeps failures per request, §15.1)."""

    scope: str  # which request: register kind, device and address range
    outcome: str  # OK | TIMEOUT | EXCEPTION
    code: int | None = None  # the protocol exception code of an EXCEPTION answer
    detail: str | None = None  # the library's text, if it gave one, cleaned and bounded where the result is made
    truncated: bool = False  # the text was cut - or, as None, left out altogether for its length


@dataclass(frozen=True, slots=True)
class InputBatch:
    """One accepted protocol delivery: a Modbus poll or one OPC UA data notification."""

    source_id: str
    conn_gen: int
    kind: str
    wall: float
    mono: float
    items: tuple[Item, ...] = ()
    tick: int | None = None  # Modbus planned tick number
    poll_id: str | None = None
    outcome: str = "OK"  # Modbus: OK | TIMEOUT | EXCEPTION | CONNECTION
    poll_start_wall: float | None = None
    poll_end_wall: float | None = None
    publish_seq: int | None = None  # OPC UA sequence number
    requests: tuple[RequestResult, ...] = ()  # Modbus: the outcome of every request of the round


@dataclass(frozen=True, slots=True)
class Observation:
    """A stored input value.  `origin` tells a real receipt from a C2 state assertion.

    For a receipt `received_*` is the observation time.  For an assertion `received_*` still
    carries the ORIGINAL observation time and `asserted_*` the time the state was re-stated,
    so a re-stated value is never mistaken for a new sample.
    """

    id: str
    run_id: str
    source_id: str
    conn_gen: int
    seg: int
    batch_seq: int
    item_seq: int
    point_id: str
    value: float | None
    quality: str
    status_code: int | None
    received_wall: float
    received_mono: float
    source_time: float | None = None
    origin: str = ORIGIN_RECEIVED
    orig_id: str | None = None
    asserted_wall: float | None = None
    asserted_mono: float | None = None
    extreme: bool = False
    tick: int | None = None
    poll_id: str | None = None
    response_id: str | None = None


@dataclass(frozen=True, slots=True)
class Result:
    """A derived value located by run / source / generation / segment / batch."""

    id: str
    run_id: str
    source_id: str
    conn_gen: int
    seg: int
    batch_seq: int
    point_id: str
    value: float | None
    status: str
    reason: str | None
    config_id: str
    at_wall: float
    extreme: bool = False
    basis: dict | None = None  # e.g. strain_change: current and previous planned tick
    estimate: dict | None = None
    origin: str = ORIGIN_RECEIVED
    orig_id: str | None = None
    asserted_wall: float | None = None
    basis_incomplete: bool = False  # re-stated result whose inputs were lost in a gap


@dataclass(frozen=True, slots=True)
class StateRecord:
    """Lifecycle / validity / bookkeeping entry (segment start-end, gap, poll, watermark...)."""

    id: str
    run_id: str
    source_id: str
    seg: int
    kind: str
    at_wall: float
    at_mono: float
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FaultRecord:
    """A confirmed fault.  Confirmation time and basis observation time are kept apart."""

    id: str
    run_id: str
    source_id: str
    seg: int
    batch_seq: int
    point_id: str
    direction: str
    confirmed_wall: float
    confirmed_mono: float
    basis_kind: str  # OBSERVATION | RESULT
    basis_id: str
    basis_wall: float  # original observation time of the deciding value
    basis_value: float | None
    config_id: str
    rule: dict = field(default_factory=dict)  # thresholds and hysteresis in force


@dataclass(frozen=True, slots=True)
class CommitGroup:
    """Everything that must become durable together; never split across routes."""

    group_id: str
    order: int
    run_id: str
    source_id: str
    kind: str
    built_wall: float
    observations: tuple[Observation, ...] = ()
    results: tuple[Result, ...] = ()
    states: tuple[StateRecord, ...] = ()
    faults: tuple[FaultRecord, ...] = ()
    bucket: int | None = None  # set by the state owner at the batch boundary (§13.6); None is never written

    def record_count(self) -> int:
        return len(self.observations) + len(self.results) + len(self.states) + len(self.faults)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PublishGroup:
    """One complete group as pushed to clients: inputs and the results computed from them."""

    pub_seq: int
    order: int
    source_id: str
    wall: float
    observations: tuple[Observation, ...] = ()
    results: tuple[Result, ...] = ()
    states: tuple[StateRecord, ...] = ()
    faults: tuple[FaultRecord, ...] = ()
