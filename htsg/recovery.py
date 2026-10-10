"""Reconnect / rebuild backoff shared by both protocols (终版 §11).

Pure and clock-free: callers pass the monotonic time, so the schedule is testable without waiting.
"""

from __future__ import annotations

from .config import Timing


class Backoff:
    """1 s doubling to 30 s; reset only after stable, valid operation for 60 s.

    After `resync_persistent_after` consecutive failed cycles the source is reported as persistently
    failing, yet recovery keeps going at the maximum interval: nothing needs manual unlocking.
    """

    def __init__(self, timing: Timing):
        self.timing = timing
        self.failures = 0
        self.healthy_since: float | None = None

    def next_delay(self) -> float:
        """A recovery cycle failed (or a healthy period ended): delay before the next attempt."""
        t = self.timing
        delay = min(t.backoff_initial * 2**self.failures, t.backoff_max)
        self.failures += 1
        self.healthy_since = None
        return delay

    def healthy(self, mono: float) -> None:
        """The source is confirmed valid again; stability is measured from the first such moment."""
        if self.healthy_since is None:
            self.healthy_since = mono

    def settle(self, mono: float) -> bool:
        """Clear the failure count once the source has stayed healthy for the stable period."""
        if self.healthy_since is not None and mono - self.healthy_since >= self.timing.backoff_stable_seconds:
            self.failures = 0
            return True
        return False

    @property
    def persistent(self) -> bool:
        return self.failures >= self.timing.resync_persistent_after
