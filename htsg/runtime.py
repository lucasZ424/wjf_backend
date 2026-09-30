from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import psycopg

from .adapters import adapter
from .engine import Engine
from .storage import PolicyRefusal, Router

log = logging.getLogger(__name__)


class Subscription:
    def __init__(self, max_events):
        self.events = deque(maxlen=max_events)
        self.frame = None
        self.ready = asyncio.Event()
        self.overflow = False

    def push(self, message):
        if message["type"] == "frame":
            self.frame = message
        elif len(self.events) >= self.events.maxlen:
            self.overflow = True
        else:
            self.events.append(message)
        self.ready.set()

    async def next(self):
        while True:
            await self.ready.wait()
            if self.overflow:
                return {"type": "resync_required"}
            if self.events:
                return self.events.popleft()
            if self.frame:
                result, self.frame = self.frame, None
                return result
            self.ready.clear()


class Runtime:
    def __init__(self, config, router: Router):
        self.config, self.router = config, router
        self.ingress = asyncio.Queue(config.limits.ingress)
        self.ordinary = deque(maxlen=config.limits.ordinary_queue)
        self.events = deque(maxlen=config.limits.event_queue)
        self.write_ready = asyncio.Event()
        self.subscribers: set[Subscription] = set()
        self.sequence = 0
        self.engine = Engine(config, self.emit, time.time(), time.monotonic(), defer_calculation=True)
        self.tasks = []
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="htsg-writer")
        self.read_io = ThreadPoolExecutor(max_workers=2, thread_name_prefix="htsg-query")
        self.query_slots = asyncio.Semaphore(2)
        self.calc_io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="htsg-calc")
        self.calc_queue = asyncio.Queue(4)
        self.health = {"ingress_dropped": 0, "archive_dropped": 0, "write_failures": 0, "last_write": None}
        self.running = False
        self.accepting = True
        self.archive_refusal = None
        self.loss_summary = None
        self.pending_source_errors = set()
        self.event_losses = {}
        self.ingress_losses = {}
        self.capture_jobs = deque()
        self.capture_bytes = 0
        self.failed = asyncio.Event()
        self.started_mono = time.monotonic()
        self.communication_logs = deque(maxlen=500)
        self.source_errors = {}
        self.source_receipts = {}

    async def db_call(self, func, *args, **kwargs):
        from functools import partial

        async with self.query_slots:
            return await asyncio.get_running_loop().run_in_executor(
                self.read_io, partial(func, *args, **kwargs)
            )

    def submit(self, point, value, quality="GOOD", source_time=None, source_seq=None):
        item = (point, value, time.time(), time.monotonic(), quality, source_time, source_seq)
        definition = self.engine.points.get(point)
        if definition:
            source = definition.source
            if source not in self.source_receipts or source in self.source_errors:
                self.communication_logs.append(
                    {"time": item[2], "source": source, "level": "INFO", "message": "READING_RECEIVED"}
                )
            self.source_receipts[source] = item[2]
            self.source_errors.pop(source, None)
            self.health.pop(source, None)
        try:
            self.ingress.put_nowait(("sample", item))
            return True
        except asyncio.QueueFull:
            self.health["ingress_dropped"] += 1
            stamp = time.time()
            old = self.ingress_losses.get(point, (stamp, stamp, 0))
            self.ingress_losses[point] = (old[0], stamp, old[2] + 1)
            return False

    def report(self, source, reason):
        if self.source_errors.get(source) != reason:
            self.communication_logs.append(
                {"time": time.time(), "source": source, "level": "ERROR", "message": reason}
            )
        self.source_errors[source] = reason
        self.health[source] = reason
        try:
            self.ingress.put_nowait(("disconnect", source))
        except asyncio.QueueFull:
            self.pending_source_errors.add(source)
            self.loss_summary = ("DISCONNECT_NOTIFICATION_OVERFLOW", time.time())

    def emit(self, kind, payload, priority):
        if kind == "capture_plan":
            plan = payload["plan"]
            if (
                not self.running
                or not self.accepting
                or self.capture_bytes + plan.size > self.config.limits.capture_budget_bytes
            ):
                self.health["capture_refused"] = self.health.get("capture_refused", 0) + 1
                return False
            self.capture_jobs.append(
                {
                    "plan": plan,
                    "offset": 0,
                    "event_id": payload["event_id"],
                    "failed": False,
                    "size": plan.size,
                }
            )
            self.capture_bytes += plan.size
            self.write_ready.set()
            return True
        if kind == "frame_input":
            try:
                self.calc_queue.put_nowait(copy.deepcopy(payload["frame"]))
            except asyncio.QueueFull:
                self.engine.finish_frame(self.unavailable_frame(payload["frame"], "CALCULATION_QUEUE_FULL"))
            return
        # JSON-safe copy prevents the worker observing subsequent state mutations.
        copied = copy.deepcopy(payload)
        if self.running and self.archive_refusal:
            # Preserve live delivery, but never turn an unpersistable loss notice into more work.
            if kind != "gap":
                self.health["archive_dropped"] += 1
        elif self.running:
            target = self.events if priority else self.ordinary
            if len(target) == target.maxlen:
                self.health["archive_dropped"] += 1
                self.loss_summary = ("ARCHIVE_QUEUE_FULL", time.time())
                if priority:
                    self.accepting = False
                self.record_event_loss(kind, payload, "ARCHIVE_QUEUE_FULL")
            else:
                target.append((kind, copied))
                self.write_ready.set()
        if kind in ("frame", "trigger", "event", "gap") and (kind != "frame" or payload.get("display", True)):
            self.sequence += 1
            message = {"type": kind, "seq": self.sequence, "run_id": self.engine.run_id, "data": copied}
            for subscriber in self.subscribers:
                subscriber.push(message)

    def subscribe(self):
        if len(self.subscribers) >= self.config.limits.max_clients:
            raise RuntimeError("CLIENT_LIMIT")
        subscriber = Subscription(self.config.limits.ws_queue)
        self.subscribers.add(subscriber)
        subscriber.push(
            {"type": "snapshot", "seq": self.sequence, "run_id": self.engine.run_id, "data": self.snapshot()}
        )
        return subscriber

    def snapshot(self):
        from dataclasses import asdict

        return copy.deepcopy(
            {
                "frame": self.engine.latest_frame,
                "active_events": [asdict(e) for e in self.engine.active.values()],
                "mode": self.config.mode,
            }
        )

    def status(self):
        last_frame = (
            max(
                [self.health.get("last_frame_write") or 0]
                + [
                    self.router.health.get(name + "_metrics", {}).get("last_frame_saved_at") or 0
                    for name, _ in self.router.stores
                ]
            )
            or None
        )
        return {
            "uptime_seconds": max(0, time.monotonic() - self.started_mono),
            "storage_budget": {
                "bytes_written": self.router.bytes_written,
                "development_growth": self.router.development_growth,
                "budget_bytes": self.config.storage.development_budget_bytes,
            },
            "runtime": dict(self.health),
            "engine": dict(self.engine.health),
            "storage": copy.deepcopy(self.router.health),
            "routing": self.router.mode,
            "history_archive": {
                "last_success_at": last_frame,
                "seconds_since_last_success": None
                if last_frame is None
                else max(0, time.time() - last_frame),
                "suspended": bool(self.archive_refusal) or self.router.mode == "EMERGENCY",
                "reason": self.archive_refusal
                or ("RESULT_ARCHIVE_SUSPENDED" if self.router.mode == "EMERGENCY" else None),
            },
            "queues": {
                "ingress": self.ingress.qsize(),
                "ordinary": len(self.ordinary),
                "event": len(self.events),
                "capture_jobs": len(self.capture_jobs),
                "capture_bytes": self.capture_bytes,
            },
            "points": {
                p: {
                    "quality": "GOOD" if s.available else "UNAVAILABLE",
                    "state": s.state,
                    "ring_count": len(s.ring),
                }
                for p, s in self.engine.states.items()
            },
            "capabilities": {
                "real_sources": [s.id for s in self.config.sources if s.enabled and s.kind != "simulator"],
                "unconfigured_derived": [d.id for d in self.config.derived if d.operation == "unconfigured"],
            },
        }

    async def start(self):
        await asyncio.get_running_loop().run_in_executor(self.io, self.router.initialize)
        self.running = True
        self.tasks = [
            asyncio.create_task(self.owner()),
            asyncio.create_task(self.writer()),
            asyncio.create_task(self.calculator()),
        ]
        for task in self.tasks:
            task.add_done_callback(self.task_completed)
        for source in self.config.sources:
            if source.enabled:
                self.tasks.append(
                    asyncio.create_task(
                        adapter(
                            source,
                            [p for p in self.config.points if p.source == source.id],
                            self.submit,
                            self.report,
                        )
                    )
                )

    def task_completed(self, task):
        if self.running and not task.cancelled():
            error = task.exception()
            self.health["fatal_worker"] = type(error).__name__ if error else "UNEXPECTED_EXIT"
            self.failed.set()

    async def owner(self):
        while self.running:
            try:
                kind, item = await asyncio.wait_for(self.ingress.get(), timeout=0.05)
                self.synchronize_capture_availability()
                if kind == "sample":
                    self.engine.receive(*item)
                    definition = self.engine.points.get(item[0])
                    if definition:
                        self.engine.health.pop(definition.source, None)
                elif kind == "frame":
                    self.engine.finish_frame(item)
                elif kind == "capture_done":
                    self.engine.capture_complete(*item)
                else:
                    self.engine.disconnected(item, time.time(), time.monotonic())
                self.ingress.task_done()
            except TimeoutError:
                pass
            self.synchronize_capture_availability()
            for source in list(self.pending_source_errors):
                self.engine.disconnected(source, time.time(), time.monotonic())
                self.pending_source_errors.discard(source)
            for point, span in list(self.ingress_losses.items()):
                self.engine.note_loss(point, "INGRESS_OVERFLOW", *span)
                self.ingress_losses.pop(point, None)
            self.engine.tick(time.time(), time.monotonic())
            if self.loss_summary and len(self.events) < self.events.maxlen // 2:
                reason, stamp = self.loss_summary
                self.loss_summary = None
                self.engine.gap(reason, stamp, time.time())
            if self.event_losses and len(self.events) < self.events.maxlen // 2:
                event_id, (reason, start) = self.event_losses.popitem()
                for event in [*self.engine.active.values(), *self.engine.pending]:
                    if event.id == event_id:
                        event.coverage = "PARTIAL"
                        self.engine.event_record(event)
                self.engine.gap(reason, start, time.time(), event_id)

    def synchronize_capture_availability(self):
        if self.engine.capture_available != self.accepting:
            self.engine.capture_available = self.accepting
            if self.accepting:
                self.engine.blocked = {
                    p: r for p, r in self.engine.blocked.items() if r != "RESOURCE_LIMITED"
                }

    def record_event_loss(self, kind, payload, reason):
        ids = (
            payload.get("event_ids", [])
            if kind == "frame"
            else [payload.get("id") if kind == "event" else payload.get("event_id")]
        )
        for identity in ids:
            if identity and (identity in self.event_losses or len(self.event_losses) < 128):
                self.event_losses.setdefault(identity, (reason, time.time()))

    def unavailable_frame(self, frame, reason):
        return {
            **frame,
            "derived_results": {
                d.id: {"value": None, "status": "NOT_AVAILABLE", "reason": reason}
                for d in self.config.derived
            },
        }

    async def calculator(self):
        from .calculation import calculate

        previous = None
        while self.running:
            frame = await self.calc_queue.get()
            future = asyncio.get_running_loop().run_in_executor(
                self.calc_io, calculate, self.config, frame, previous
            )
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(future), self.config.limits.calculation_timeout
                )
            except TimeoutError:
                result = self.unavailable_frame(frame, "CALCULATION_TIMEOUT")
                await self.ingress.put(("frame", result))
                previous = result
                # Do not submit unlimited work after timeout; discard the late result.
                try:
                    await future
                except Exception:
                    pass
                self.calc_queue.task_done()
                continue
            except Exception as error:
                result = self.unavailable_frame(frame, type(error).__name__)
            await self.ingress.put(("frame", result))
            previous = result
            self.calc_queue.task_done()

    async def writer(self):
        backoff, maintained, turn = 0.2, time.monotonic(), 0
        loop = asyncio.get_running_loop()
        while self.running or self.events or self.ordinary or self.capture_jobs:
            if time.monotonic() - maintained >= self.config.limits.maintenance_interval:
                await loop.run_in_executor(self.io, self.router.maintain)
                maintained = time.monotonic()
            job = None
            # Bound each turn and give live events, Pre jobs and ordinary history a share.
            choices = ("events", "capture", "ordinary")
            chosen = None
            for n in range(3):
                candidate = choices[(turn + n) % 3]
                target = self.capture_jobs if candidate == "capture" else getattr(self, candidate)
                if target:
                    chosen = candidate
                    turn = (turn + n + 1) % 3
                    break
            if chosen is None:
                self.write_ready.clear()
                try:
                    await asyncio.wait_for(
                        self.write_ready.wait(), min(1, self.config.limits.maintenance_interval)
                    )
                except TimeoutError:
                    pass
                continue
            if chosen == "capture":
                job = self.capture_jobs[0]
                batch = await loop.run_in_executor(
                    self.io, job["plan"].batch, job["offset"], self.config.limits.write_batch_records
                )
            else:
                target = getattr(self, chosen)
                batch = [
                    target.popleft() for _ in range(min(len(target), self.config.limits.write_batch_records))
                ]
            completed = False
            try:
                refused = await loop.run_in_executor(self.io, self.router.write_batch, batch)
                for record, reason in refused:
                    self.health["archive_dropped"] += 1
                    self.loss_summary = (reason, time.time())
                    self.record_event_loss(*record, reason)
                    if job:
                        job["failed"] = True
                self.health["last_write"] = time.time()
                if any(k == "frame" for k, p in batch) and not refused:
                    self.health["last_frame_write"] = self.health["last_write"]
                if not self.archive_refusal:
                    self.health["write_error"] = None
                if (
                    not self.archive_refusal
                    and any(record[0] != "gap" for record in batch)
                    and len(refused) < len(batch)
                    and len(self.events) < max(1, self.events.maxlen // 2)
                ):
                    self.accepting = True
                backoff = 0.2
                completed = True
            except PolicyRefusal as error:
                self.health["archive_dropped"] += len(batch)
                self.health["write_error"] = str(error)
                if str(error) == "DEVELOPMENT_WRITE_BUDGET_EXHAUSTED":
                    # This cumulative run budget cannot recover when queues happen to drain.
                    self.archive_refusal = str(error)
                    self.health.setdefault("archive_suspended_at", time.time())
                if any(kind != "gap" for kind, _ in batch):
                    self.loss_summary = (str(error), time.time())
                self.accepting = False
                for record in batch:
                    if record[0] != "gap":
                        self.record_event_loss(*record, str(error))
                if job:
                    job["failed"] = True
                completed = True
            except psycopg.Error as error:
                self.health["write_failures"] += 1
                self.health["write_error"] = type(error).__name__
                self.accepting = False
                if job is None:
                    target = getattr(self, chosen)
                    for record in reversed(batch):
                        if len(target) < target.maxlen:
                            target.appendleft(record)
                        else:
                            self.health["archive_dropped"] += 1
                            self.loss_summary = ("RETRY_QUEUE_FULL", time.time())
                            self.record_event_loss(*record, "RETRY_QUEUE_FULL")
                await asyncio.sleep(backoff)
                backoff = min(5, backoff * 2)
            # Unexpected programming errors deliberately escape to the worker supervisor.
            if job and completed:
                job["offset"] += len(batch)
                if job["offset"] >= job["plan"].count:
                    self.capture_jobs.popleft()
                    self.capture_bytes -= job["size"]
                    if self.running:
                        await self.ingress.put(("capture_done", (job["event_id"], not job["failed"])))
                    else:
                        self.engine.capture_complete(job["event_id"], not job["failed"])

    async def stop(self):
        for task in self.tasks[2:]:
            task.cancel()
        if self.tasks:
            self.tasks[0].cancel()
        self.engine.shutdown(time.time())
        self.running = False
        self.write_ready.set()
        if len(self.tasks) > 1:
            try:
                await asyncio.wait_for(self.tasks[1], 8)
            except (TimeoutError, asyncio.CancelledError):
                self.tasks[1].cancel()
            except Exception:
                # The supervisor already marks this fatal; still release connections/workers.
                log.exception("Archive worker failed before shutdown completed")
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.io.shutdown(wait=False, cancel_futures=True)
        self.read_io.shutdown(wait=False, cancel_futures=True)
        self.calc_io.shutdown(wait=False, cancel_futures=True)
        self.router.close()
