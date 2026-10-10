"""Ordinary-fault timing (终版 §8, §8.1).  Pure: no clock, no I/O; callers pass the monotonic time.

Rules: strictly beyond a bound starts the clock; the same direction for 5 s of VALID state confirms;
back inside the bound before that clears it; once confirmed, only the hysteresis reset line (inclusive)
recovers it; the opposite direction restarts the clock.  An invalid state cancels a pending clock but
never recovers a confirmed fault.  Time passes only in `advance()`, which the owner runs once a second
AFTER communication has been judged, so an unchanged violating value still confirms.
"""

from __future__ import annotations

from dataclasses import dataclass

HIGH, LOW = "HIGH", "LOW"


@dataclass(slots=True)
class _State:
    pending: str | None = None
    since: float | None = None
    confirmed: str | None = None


class FaultTracker:
    def __init__(self, rules: dict[str, dict], confirm_seconds: float = 5.0):
        self.rules = rules
        self.confirm_seconds = confirm_seconds
        self.state = {pid: _State() for pid in rules}

    def tracked(self):
        return self.rules.keys()

    def status(self, pid: str) -> str:
        st = self.state.get(pid)
        if st is None:
            return "UNTRACKED"
        return "CONFIRMED" if st.confirmed else "PENDING" if st.pending else "NORMAL"

    def confirmed_direction(self, pid: str) -> str | None:
        st = self.state.get(pid)
        return st.confirmed if st else None

    def direction(self, pid: str, value: float) -> str | None:
        """Strictly beyond a bound only; equal to the bound is normal."""
        rule = self.rules[pid]
        if rule["high"] is not None and value > rule["high"]:
            return HIGH
        if rule["low"] is not None and value < rule["low"]:
            return LOW
        return None

    def cancel(self, pid: str) -> None:
        """Drop a pending clock (invalid data, communication loss, multi-version batch).  Never recovers."""
        st = self.state.get(pid)
        if st is not None:
            st.pending = st.since = None

    def observe(self, pid: str, value: float | None, mono: float) -> str | None:
        """Feed the current state of one tracked channel; `None` means not valid.

        Returns "RECOVERED" when this observation crossed the hysteresis reset line of a confirmed fault.
        """
        st = self.state.get(pid)
        if st is None:
            return None
        if value is None:
            st.pending = st.since = None
            return None
        rule = self.rules[pid]
        recovered = None
        if st.confirmed == HIGH and value <= rule["high"] - rule["hysteresis_high"]:
            st.confirmed, recovered = None, "RECOVERED"
        elif st.confirmed == LOW and value >= rule["low"] + rule["hysteresis_low"]:
            st.confirmed, recovered = None, "RECOVERED"
        if st.confirmed is not None:  # a standing fault is not timed again
            st.pending = st.since = None
            return recovered
        direction = self.direction(pid, value)
        if direction is None:
            st.pending = st.since = None
        elif st.pending != direction:
            st.pending, st.since = direction, mono  # new violation, or the opposite side: start over
        return recovered

    def advance(self, mono: float) -> list[tuple[str, str]]:
        """One monotonic tick: confirm every clock that has run its 5 s.  Returns (channel, direction)."""
        confirmed = []
        for pid, st in self.state.items():
            if st.pending is not None and mono - st.since >= self.confirm_seconds:
                st.confirmed, st.pending, st.since = st.pending, None, None
                confirmed.append((pid, st.confirmed))
        return confirmed
