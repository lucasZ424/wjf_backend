"""The communication log of the current run (终版 §15.1): bounded, in memory only, appended by the owner alone.

Pure and clock-free like the owner: every call carries the wall (a label) and monotonic (durations) time of the
fact.  Nothing here reaches the archive: no record kind, no commit group, no route.  A restart starts empty.

Two kinds of repeated failure are merged, with different boundaries (§15.1 重复失败):
  * attempt failures (CONNECT / SUBSCRIBE) merge ACROSS generations - retrying a connection makes a new
    generation every time - keyed by source, phase and the stable reason code; `conn_gen` is None;
  * round failures merge only inside ONE generation, source, scope (one Modbus request) and the same type and
    reason code.
Every aggregate says whether it is still going on and, once it is over, when and why (`ended_time`, `end_reason`)
- which is never presented as "communication recovered".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LOG_LIMIT = 500  # entries kept in memory: the newest ones, first-in-first-out
DETAIL_LIMIT = 512  # characters of error detail kept
SCRUB_INPUT_LIMIT = 8 * DETAIL_LIMIT  # raw text above this is left out whole, so the cost of cleaning has a ceiling
DIAG_LIMIT = 500  # diagnostic facts that may wait at the ordered entry (used by Ingress)
PHASES = ("CONNECT", "SUBSCRIBE")

ATTEMPT, ROUND, EVENT = "ATTEMPT", "ROUND", "EVENT"  # entry kinds
ACTIVE, ENDED = "ACTIVE", "ENDED"

# -- error detail: leaves the process only after it has been cleaned (§15.1 原因证据) ---------------------------------

_USERINFO = re.compile(r"(?<![A-Za-z0-9+.-])(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^@]*@(?:[^\s/@]*@)*")
_BEARER = re.compile(r"(?i)\b(?P<kind>bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_SECRET = re.compile(
    r"""(?ix)
    (?P<key>(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|authorization|credential)[\w.-]*+)
    (?P<sep>["']?\s*[=:]\s*)
    (?P<value>"[^"]*"|'[^']*'|[^\s,;&)}\]]+)
    """
)


def scrub(text: str) -> str:
    """Remove credentials: in a URL everything from `scheme://` to the nearest `@` (blanks included - a little too much
    is masked rather than a part of a secret kept) and on to the last `@` of the pieces that follow it with no blank or
    slash between (a password that holds an unescaped `@`), secret-named values, bearer / basic tokens."""
    text = _USERINFO.sub(lambda m: m.group("scheme") + "***@", text)
    text = _BEARER.sub(lambda m: m.group("kind") + " ***", text)
    return _SECRET.sub(lambda m: m.group("key") + m.group("sep") + "***", text)


def clean_detail(raw) -> tuple[str | None, bool]:
    """(detail, truncated): scrubbed FIRST, then cut to the limit - a secret cut in half would still show its start.

    A message longer than SCRUB_INPUT_LIMIT is left out whole - (None, True) - not cut: the part that is kept could
    hold the start of a credential whose end is out of sight, and nothing of it is worth that risk.
    """
    if raw is None:
        return None, False
    text = str(raw)
    if len(text) > SCRUB_INPUT_LIMIT:
        return None, True
    text = scrub(text)
    return text[:DETAIL_LIMIT], len(text) > DETAIL_LIMIT


# -- entries and the log ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Entry:
    seq: int  # assigned by the log: the order in which the owner appended, never reused
    run_id: str
    source_id: str
    kind: str  # ATTEMPT | ROUND | EVENT
    event: str
    reason: str  # stable code: it, not the free text of `detail`, decides what counts as the same failure
    conn_gen: int | None  # None: no generation yet, or (attempts) not bound to one generation
    phase: str | None  # CONNECT | SUBSCRIBE for attempts
    scope: str | None  # which request of a round, e.g. the register range
    detail: str | None
    detail_truncated: bool
    found_wall: float  # when the application noticed
    found_mono: float
    last_comm_wall: float | None  # last valid communication known at that moment: "last at T0, noticed at T1"
    last_comm_mono: float | None
    first_wall: float
    first_mono: float
    last_wall: float
    last_mono: float
    count: int = 1
    state: str | None = None  # ACTIVE | ENDED for aggregates, None for a single event
    ended_wall: float | None = None
    ended_mono: float | None = None
    end_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "run_id": self.run_id,
            "source_id": self.source_id,
            "kind": self.kind,
            "event": self.event,
            "reason": self.reason,
            "conn_gen": self.conn_gen,
            "phase": self.phase,
            "scope": self.scope,
            "detail": self.detail,
            "detail_truncated": self.detail_truncated,
            "found_time": self.found_wall,
            "last_comm_time": self.last_comm_wall,
            "silent_seconds": None if self.last_comm_mono is None else self.found_mono - self.last_comm_mono,
            "first_failure": self.first_wall if self.state else None,
            "last_failure": self.last_wall if self.state else None,
            "span_seconds": self.last_mono - self.first_mono if self.state else None,
            "count": self.count,
            "state": self.state,
            "ended_time": self.ended_wall,
            "end_reason": self.end_reason,
        }


class CommLog:
    def __init__(self, run_id: str, limit: int = LOG_LIMIT):
        self.run_id, self.limit = run_id, limit
        self.entries: dict[int, Entry] = {}  # insertion order is the first-in-first-out order
        self.evicted = 0
        self.diag_lost = 0  # diagnostic facts that never reached the log (the ordered entry was full)
        self._seq = 0
        self._attempts: dict[tuple[str, str], Entry] = {}  # (source, phase) -> the aggregate still going on
        self._rounds: dict[tuple[str, str], Entry] = {}  # (source, scope) -> the aggregate still going on

    # -- reading -------------------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        oldest = next(iter(self.entries.values()), None)
        return {
            "scope": "THIS_RUN_IN_MEMORY",
            "limit": self.limit,
            "evicted": self.evicted,
            "oldest_time": None if oldest is None else oldest.found_wall,
            "diag_lost": self.diag_lost,
            "incomplete": bool(self.evicted or self.diag_lost),
            "entries": [e.as_dict() for e in self.entries.values()],
        }

    # -- appending (the owner only) --------------------------------------------------------------------------------

    def _append(self, entry: Entry) -> Entry:
        self.entries[entry.seq] = entry
        while len(self.entries) > self.limit:
            old = self.entries.pop(next(iter(self.entries)))  # the oldest by first insertion, however recently updated
            self.evicted += 1
            for index, key in ((self._attempts, (old.source_id, old.phase)), (self._rounds, (old.source_id, old.scope))):
                if index.get(key) is old:  # its active-aggregate slot goes with it: the next failure starts a new entry
                    del index[key]
        return entry

    def _new(self, kind, source_id, event, reason, conn_gen, phase, scope, detail, truncated, wall, mono, comm):
        self._seq += 1
        last_wall, last_mono = comm if comm else (None, None)
        return Entry(
            self._seq, self.run_id, source_id, kind, event, reason, conn_gen, phase, scope, detail, truncated,
            wall, mono, last_wall, last_mono, wall, mono, wall, mono,
        )  # fmt: skip

    @staticmethod
    def _end(entry: Entry, wall: float, mono: float, why: str):
        entry.state, entry.ended_wall, entry.ended_mono, entry.end_reason = ENDED, wall, mono, why

    def attempt_failed(self, source_id, phase, reason, wall, mono, *, detail=None, truncated=False, comm=None):
        """A failed CONNECT or SUBSCRIBE attempt.  Same source, phase and reason code: one entry, however many
        generations the retries went through; another reason code ends that entry and starts a new one."""
        key = (source_id, phase)
        active = self._attempts.get(key)
        if active is not None:
            if active.reason == reason:
                active.count, active.last_wall, active.last_mono = active.count + 1, wall, mono
                if detail is not None or truncated:
                    active.detail, active.detail_truncated = detail, truncated  # the latest text; identity is the code
                return active
            self._end(active, wall, mono, "REASON_CHANGED")
        entry = self._new(ATTEMPT, source_id, "ATTEMPT_FAILED", reason, None, phase, None, detail, truncated, wall, mono, comm)
        entry.state = ACTIVE
        self._attempts[key] = entry
        return self._append(entry)

    def attempt_ok(self, source_id, phase, wall, mono):
        """The phase succeeded (whether or not that is logged): it ends THIS phase's aggregate and no other."""
        active = self._attempts.pop((source_id, phase), None)
        if active is not None:
            self._end(active, wall, mono, phase + "_OK")

    def round_failed(self, source_id, conn_gen, event, reason, wall, mono, *, scope="", detail=None, truncated=False, comm=None):
        """A failed round / request inside one generation.  Merges only with the same generation, type and reason
        code of the same scope; a change of any of them ends the old entry."""
        key = (source_id, scope)
        active = self._rounds.get(key)
        if active is not None:
            if (active.conn_gen, active.event, active.reason) == (conn_gen, event, reason):
                active.count, active.last_wall, active.last_mono = active.count + 1, wall, mono
                if detail is not None or truncated:
                    active.detail, active.detail_truncated = detail, truncated
                return active
            self._end(active, wall, mono, "REASON_CHANGED" if active.conn_gen == conn_gen else "NEW_GENERATION")
        entry = self._new(ROUND, source_id, event, reason, conn_gen, None, scope, detail, truncated, wall, mono, comm)
        entry.state = ACTIVE
        self._rounds[key] = entry
        return self._append(entry)

    def round_ok(self, source_id, scope, wall, mono):
        """The condition of this request succeeded: ends its aggregate only - other requests of the same round keep
        theirs (a part of the round answering does not clear what still fails)."""
        active = self._rounds.pop((source_id, scope), None)
        if active is not None:
            self._end(active, wall, mono, "SUCCESS")

    def event(self, source_id, conn_gen, event, reason, wall, mono, *, detail=None, truncated=False, comm=None, failure=False):
        """A single fact (new generation, first communication, recovery, resync ...).

        A fact that is not itself a failure signal ends the source's round aggregates - something else happened, so
        the repeating failure is over.  A failure signal (a resync, an expiry, an alarm, a loss) is the same trouble
        speaking again and does not cut the stretch in two.  Attempt aggregates are only ever ended by their own phase
        succeeding or changing."""
        if not failure:
            self.end_rounds(source_id, "EVENT:" + event, wall, mono)
        entry = self._new(EVENT, source_id, event, reason, conn_gen, None, None, detail, truncated, wall, mono, comm)
        return self._append(entry)

    def end_rounds(self, source_id, why, wall, mono):
        """Every round aggregate of the source ends (new generation, or another event): none outlives its generation."""
        for key in [k for k in self._rounds if k[0] == source_id]:
            self._end(self._rounds.pop(key), wall, mono, why)
