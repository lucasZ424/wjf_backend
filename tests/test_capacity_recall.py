"""Capacity refusal must stop capture while leaving live observations available."""

import asyncio
import time

from htsg.runtime import Runtime
from htsg.storage import PolicyRefusal


class BudgetRouter:
    mode = "PRIMARY"
    stores = []
    health = {}
    bytes_written = 128 * 1024**2
    development_growth = 0

    def __init__(self):
        self.calls = []

    def write_batch(self, records):
        self.calls.append(records)
        if any(kind == "frame" for kind, _ in records):
            raise PolicyRefusal("DEVELOPMENT_WRITE_BUDGET_EXHAUSTED")
        return []  # Small metadata may fit; this does not prove capture can resume.

    def maintain(self):
        pass


def release(runtime):
    runtime.io.shutdown(wait=True)
    runtime.read_io.shutdown(wait=True)
    runtime.calc_io.shutdown(wait=True)


async def test_small_metadata_write_cannot_reopen_capture_after_budget_exhaustion(settings):
    router = BudgetRouter()
    runtime = Runtime(settings, router)
    runtime.ordinary.append(("frame", {"frame": {}, "event_ids": []}))
    try:
        await runtime.writer()
        assert not runtime.accepting
        runtime.events.append(("event", {"id": "previous-event"}))
        await runtime.writer()
        assert not runtime.accepting, "An old metadata write must not enable new costly captures"
        assert runtime.health["write_error"] == "DEVELOPMENT_WRITE_BUDGET_EXHAUSTED"
        archive = runtime.status()["history_archive"]
        assert archive["suspended"] and archive["reason"] == "DEVELOPMENT_WRITE_BUDGET_EXHAUSTED"
        assert runtime.health["archive_suspended_at"] > 0
    finally:
        release(runtime)


async def test_budget_pause_stops_archive_feedback_and_repeated_event_creation(settings):
    router = BudgetRouter()
    runtime = Runtime(settings, router)
    runtime.ordinary.append(("frame", {"frame": {}, "event_ids": []}))
    tasks = []
    try:
        await runtime.writer()
        runtime.running = True
        runtime.engine.blocked["p"] = "RESOURCE_LIMITED"
        subscriber = runtime.subscribe()
        tasks = [asyncio.create_task(runtime.owner()), asyncio.create_task(runtime.writer())]
        for index in range(6):
            # Repeated normal/high cycles must not create new events after a hard capacity refusal.
            runtime.submit("x", 2 if index % 2 else 20)
            runtime.submit("y", 2)
            await asyncio.sleep(0.02)
        assert not runtime.engine.active
        assert runtime.engine.blocked["p"] == "RESOURCE_LIMITED"
        assert runtime.engine.states["x"].latest.value == 2
        frame = runtime.engine.make_frame(int(time.time()))
        runtime.engine.finish_frame(frame)
        assert runtime.snapshot()["frame"]["id"] == frame["id"]
        assert subscriber.frame["data"]["frame"]["id"] == frame["id"]
        await asyncio.sleep(0.07)
        assert len(router.calls) == 1, "A refused write must not generate self-repeating gap writes"
        assert runtime.health["archive_dropped"] == 2  # Initial refusal and one new live frame.
        assert not runtime.events and not runtime.ordinary and not runtime.capture_jobs
        assert runtime.loss_summary is None
        gaps = [message for message in subscriber.events if message["type"] == "gap"]
        assert len(gaps) == 1 and gaps[0]["data"]["reason"] == "DEVELOPMENT_WRITE_BUDGET_EXHAUSTED"
    finally:
        runtime.running = False
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        release(runtime)


async def test_rejected_loss_notice_does_not_create_another_loss_notice(settings):
    class RefusingRouter:
        def write_batch(self, records):
            raise PolicyRefusal("EMERGENCY_RESERVE")

    runtime = Runtime(settings, RefusingRouter())
    runtime.events.append(("gap", {"event_id": "e", "reason": "EMERGENCY_RESERVE"}))
    try:
        await runtime.writer()
        assert runtime.loss_summary is None and not runtime.event_losses
        assert not runtime.accepting
    finally:
        release(runtime)
