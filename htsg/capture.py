"""Immutable Pre snapshots; conversion into database records runs on the writer thread."""

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class FrameSnapshot:
    window_start: float
    window_end: float
    encoded: bytes

    @classmethod
    def seal(cls, frame):
        return cls(frame["window_start"], frame["window_end"], json.dumps(frame, allow_nan=False).encode())


@dataclass(frozen=True)
class CapturePlan:
    event_json: bytes
    samples: tuple
    frames: tuple[FrameSnapshot, ...]

    @property
    def size(self):
        return len(self.event_json) + len(self.samples) * 1024 + sum(len(f.encoded) for f in self.frames)

    @property
    def count(self):
        return len(self.samples) + len(self.frames)

    def batch(self, offset, limit):
        event = json.loads(self.event_json)
        result = []
        for i in range(offset, min(self.count, offset + limit)):
            if i < len(self.samples):
                result.append(
                    ("sample", {"event_id": event["id"], "event": event, "sample": asdict(self.samples[i])})
                )
            else:
                result.append(
                    (
                        "frame",
                        {
                            "frame": json.loads(self.frames[i - len(self.samples)].encoded),
                            "event_ids": [event["id"]],
                            "display": False,
                        },
                    )
                )
        return result
