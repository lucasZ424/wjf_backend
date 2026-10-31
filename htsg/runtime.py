"""Assembly of the batch-driven chain (方案 D45): protocol tasks -> ordered ingress -> the single owner -> ordered
commit queue -> archive router.  The pages read the owner's current state on request (修复轮05); nothing on this
path encodes or sends anything for them.

Nothing here decides anything about data.  The acquirers deliver batches and lifecycle events to `Ingress`, one
task drains it into the owner and ticks the owner once a second, the commit queue's own task writes whole
groups, and a maintenance task keeps the slow bookkeeping off the write path.  Stopping reverses the order:
protocols first, then what they delivered, then the end of the run, then whatever the archive still owes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from uuid import uuid4

from .buckets import BucketClock, Maintenance
from .commitq import CommitQueue
from .ingress import Ingress
from .modbus import ModbusAcquirer
from .owner import Owner
from .ua import UaAcquirer

log = logging.getLogger(__name__)
DRAIN_SLICE = 256  # batches handled in a row before the loop looks at the clock and the other tasks again
STOP_WRITE_WAIT = 8.0  # seconds the archive gets at shutdown to write what is already queued
START_WAIT = 5.0  # seconds a start waits, in all, for a bucket a group can be put in (plan D7)
MAINTENANCE_STOP_WAIT = 5.0


class Runtime:
    def __init__(self, config, router, accounts=None, *, number_file: Path, run_id=None):
        self.config, self.router, self.accounts = config, router, accounts
        self.run_id = run_id or uuid4().hex
        # What the stop ended with, as facts and not as a total (细则 8.1): `shutdown_incomplete` is False only when the end of the
        # run was CONFIRMED - it does not say nothing was lost: faults still in memory at the stop are not carried (细则 11 M).
        self.health = {
            "accounts": None, "fatal": None, "maintenance_max_seconds": 0.0,
            "shutdown_incomplete": None, "run_end_status": None, "failed_sources": None, "unsettled_queue_items": None,
        }  # fmt: skip
        self.failed, self.wake = asyncio.Event(), asyncio.Event()
        self.queue = CommitQueue(
            router, config.limits.commit_queue,
            on_committed=lambda group, result: self.owner.committed(group, result),
            on_refused=lambda group, reason: self.owner.refused(group, reason),
            is_void=lambda group: self.owner.is_void(group),
        )  # fmt: skip
        dsns = {name: store.dsn for name, store in getattr(router, "stores", ())}
        self.maintenance = Maintenance(config, dsns, number_file)  # buckets: own thread and connections (§13.6)
        self.buckets = BucketClock(self.maintenance)
        self.maintenance.clock = self.buckets
        router.buckets = lambda: self.maintenance.snapshot  # one reference read: which database has buckets (§13.5)
        self.owner = Owner(
            config, time.time(), time.monotonic(), commit=self.queue.offer, run_id=self.run_id, buckets=self.buckets
        )
        self.ingress = Ingress(self.owner, config.limits.ingress)
        self.ingress.wakeup = self.wake.set
        self.owner.on_rebuild = self._rebuild
        self.acquirers: dict[str, ModbusAcquirer | UaAcquirer] = {}
        self.tasks: list[asyncio.Task] = []
        self.running = False

    def _rebuild(self, source_id: str, reason: str):
        acquirer = self.acquirers.get(source_id)
        if isinstance(acquirer, UaAcquirer):
            acquirer.request_rebuild(source_id, reason)

    def diagnostics(self) -> dict:
        """What only the acquisition side knows: the OPC UA parameters the server really granted."""
        return {
            source_id: acquirer.negotiated.as_dict()
            for source_id, acquirer in self.acquirers.items()
            if isinstance(acquirer, UaAcquirer) and acquirer.negotiated is not None
        }

    def connect_failures(self) -> dict[str, int]:
        """The acquirers' own count of failed connect attempts since the last successful one (终版 §11), per source.
        Read as it stands: not rebuilt from the log, the backoff or the resync count."""
        return {source_id: acquirer.connect_failures for source_id, acquirer in self.acquirers.items()}

    # ------------------------------------------------------------------------------------------ start

    def _open_archive(self):
        """Blocking database work, run on the archive writer's own thread."""
        self.router.initialize(self.run_id)
        self.router.bind_run(self.run_id, self.config.config_id, self.owner.started_wall)
        if self.accounts is None:
            return
        self.accounts.record = self.router.primary.audit  # an account change is made only with its record in the archive
        if self.accounts.unreadable:
            self.health["accounts"] = "FILE_UNREADABLE"
            log.warning("account file unreadable (%s): only the initial password signs in", self.accounts.unreadable)
        elif not self.accounts.users:
            self.health["accounts"] = "NO_ACCOUNTS"
            log.warning("no account in the account file: nobody can sign in; run init-accounts, then restart")
        else:
            self.health["accounts"] = "FILE"
        if self.accounts.fallback_unreadable:  # 修复轮07: only the fallback is lost; nothing else waits for it
            log.warning("fallback credential unreadable (%s): the initial password does not sign in", self.accounts.fallback_unreadable)
        elif self.accounts.fallback is None:
            log.warning("no fallback credential beside the account file: only the accounts sign in")

    async def start(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.queue.io, self._open_archive)
        self.maintenance.start()
        # a bucket that is there already is found in the first round; a new database waits START_WAIT at most, then the
        # groups without one are a gap and the same thread goes on preparing (plan D7)
        if not await loop.run_in_executor(None, self.maintenance.usable.wait, START_WAIT):
            self.health["buckets"] = "NONE_READY_AT_START"
            log.warning("no bucket ready after %.0f s: groups are refused as a gap until one is", START_WAIT)
        self.buckets.tick(time.time(), time.monotonic())
        self.running = True
        self.owner.start(time.time(), time.monotonic())  # the run is registered before any data
        self.tasks = [
            asyncio.create_task(self.queue.run(), name="archive writer"),
            asyncio.create_task(self._loop(), name="owner loop"),
            asyncio.create_task(self._maintain(), name="maintenance"),
        ]
        for source in self.config.sources:
            if source.enabled:
                points = [p for p in self.config.points if p.source == source.id]
                factory = ModbusAcquirer if source.kind == "modbus" else UaAcquirer
                acquirer = self.acquirers[source.id] = factory(source, points, self.config.timing, self.ingress)
                self.tasks.append(asyncio.create_task(acquirer.run(), name=f"acquire {source.id}"))
        for task in self.tasks:
            task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task):
        if self.running:  # while running, a core task that ends is a failure - a cancel from outside as well
            error = None if task.cancelled() else task.exception()
            what = "CANCELLED" if task.cancelled() else type(error).__name__ if error else "UNEXPECTED_EXIT"
            self.health["fatal"] = f"{task.get_name()}: {what}"
            log.error("core task ended: %s", self.health["fatal"], exc_info=error)
            self.failed.set()

    # ------------------------------------------------------------------------------------------- tasks

    async def _loop(self):
        """Drain what the protocols delivered and tick the owner once a second on the monotonic schedule."""
        next_tick = time.monotonic() + 1
        while True:
            if not self.ingress.queue:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), max(0.0, next_tick - time.monotonic()))
                except TimeoutError:
                    pass
            self.ingress.drain(DRAIN_SLICE)
            # buckets a group is still on its way to: never taken away (§13.6); one reference, read by the maintenance thread
            self.maintenance.unsettled = frozenset({g.bucket for g in self.queue.queue} | self.owner.unsettled_buckets())
            now = time.monotonic()
            if now >= next_tick:
                self.owner.tick(time.time(), now)
                next_tick += 1
                if next_tick <= now:  # late: skipped seconds are not replayed
                    next_tick = now + 1
            elif self.ingress.queue:
                await asyncio.sleep(0)  # more waiting: let the other tasks run in between

    async def _maintain(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.config.timing.status_interval)
            started = time.monotonic()
            await loop.run_in_executor(self.queue.io, self.router.maintain)  # the writer's thread: never beside a write
            took = time.monotonic() - started
            self.health["maintenance_max_seconds"] = max(self.health["maintenance_max_seconds"], took)

    # ------------------------------------------------------------------------------------------- stop

    async def _end(self, tasks):
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self):
        self.running = False
        self.maintenance.request_stop()  # it ends its step while the rest stops, inside the same deadline
        named = {task.get_name(): task for task in self.tasks}
        await self._end([t for name, t in named.items() if name.startswith("acquire ")])  # no more input
        await self._end([named[n] for n in ("owner loop", "maintenance") if n in named])
        self.ingress.drain()  # everything the protocols delivered is processed before the run ends
        deadline = time.monotonic() + STOP_WRITE_WAIT
        self.owner.shutdown(time.time(), time.monotonic())
        while time.monotonic() < deadline:
            self.owner.shutdown_progress()
            if self.owner.shutdown_failed_sources():
                if self.owner.shutdown_settled():  # the run cannot be ended any more, but the other sources get their final groups
                    break
            else:
                self.owner.submit_run_end()
                if self.owner.run_end_status in ("COMMITTED", "REFUSED"):
                    break
            await asyncio.sleep(0.05)
        remaining = max(0.0, deadline - time.monotonic())
        queued = await self.queue.stop(remaining)
        self.health.update(
            shutdown_incomplete=self.owner.run_end_status != "COMMITTED",
            run_end_status=self.owner.run_end_state(),
            failed_sources=self.owner.shutdown_failed_sources(),
            unsettled_queue_items=queued,  # queue items only: a group may be in the queue AND counted by its source
        )
        if "archive writer" in named:
            try:
                await asyncio.wait_for(named["archive writer"], max(0.001, deadline - time.monotonic()))
            except (TimeoutError, asyncio.CancelledError):
                await self._end([named["archive writer"]])
        self.router.close()
        wait = min(MAINTENANCE_STOP_WAIT, max(0.1, deadline - time.monotonic()))
        # False: its step was still running (a statement may take up to a minute); it ends after it and closes its own
        # connections - the caller must not take this stop for the end of every use of the databases
        self.health["maintenance_stopped"] = await asyncio.get_running_loop().run_in_executor(None, self.maintenance.stop, wait)
