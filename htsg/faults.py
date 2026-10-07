"""Per-channel fault confirmation. Elapsed time uses the monotonic clock."""

from dataclasses import dataclass

CONFIRM_SECONDS = 5.0
MAX_SAMPLE_GAP = 1.5


@dataclass
class FaultState:
    direction: str | None = None
    started: float | None = None
    last: float | None = None
    recorded: bool = False

    def interrupt(self):
        self.started = self.last = None
        self.direction = None

    def observe(self, direction, mono):
        if direction == "NORMAL":
            self.interrupt()
            self.recorded = False
            return False
        if direction not in ("HIGH", "LOW"):
            self.interrupt()
            return False
        if self.recorded:
            return False
        if self.last is None or not 0 < mono - self.last <= MAX_SAMPLE_GAP or direction != self.direction:
            self.started = mono
        self.direction, self.last = direction, mono
        if mono - self.started < CONFIRM_SECONDS:
            return False
        self.recorded = True
        return True
