"""OPC UA acquisition under application control (终版 §4, §11).

asyncua 2.0.1 is pinned because two PRIVATE methods are overridden (`_connection_supervisor`,
`_stale_watchdog_loop`); re-check them on every upgrade.  Library behaviour kept: Publish loop and
secure-channel renewal.  Library behaviour switched off: its periodic server_state read, its
automatic subscription recreate and its reconnect.  The application alone decides about liveness,
rebuild and backoff, and subscriptions are created with explicit parameters whose server-revised
values are stored (never inferred from the request).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from asyncua import Client, ua
from asyncua.common.subscription import Subscription

from . import records as rec
from .config import Point, Source, Timing
from .recovery import Backoff
from .timing import every_second

SEQ_MAX = 0xFFFFFFFF  # sequence numbers run 1..2**32-1 and skip 0 when wrapping
STATUS_INFO_DATAVALUE = 0x0400
STATUS_OVERFLOW = 0x0080


def is_overflow(status_value: int) -> bool:
    """DataValue info bits: the server's monitored-item queue discarded changes (Part 4, 7.39)."""
    return bool(status_value & STATUS_INFO_DATAVALUE) and bool(status_value & STATUS_OVERFLOW)


# ------------------------------------------------------------------------------- continuity


class PublishTracker:
    """Sequence-number continuity of one subscription: duplicates, gaps, wrap-around.

    A keep-alive carries the number of the NEXT message, so it proves liveness without consuming a number
    and still exposes a missing message when it does not match.
    """

    def __init__(self):
        self.expected: int | None = None
        self.duplicates = 0

    @staticmethod
    def _distance(seq: int, expected: int) -> int:
        return ((seq - 1) - (expected - 1)) % SEQ_MAX  # arithmetic over 1..2**32-1

    def observe(self, seq: int, has_data: bool) -> str:
        """'OK', 'DUPLICATE' (older than expected: ignore) or 'GAP' (messages were skipped)."""
        if self.expected is None:
            self.expected = (1 if seq == SEQ_MAX else seq + 1) if has_data else seq
            return "OK"
        d = self._distance(seq, self.expected)
        if d == 0:
            if has_data:
                self.expected = 1 if seq == SEQ_MAX else seq + 1
            return "OK"
        if d < SEQ_MAX // 2:
            return "GAP"
        self.duplicates += 1
        return "DUPLICATE"


# --------------------------------------------------------------------------- client and subscription


class ManagedClient(Client):
    """Client whose library-side recovery tasks end at once (see the module docstring)."""

    async def _connection_supervisor(self) -> None:
        return None

    async def _stale_watchdog_loop(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ItemRevision:
    point_id: str
    status: int
    sampling_interval: float  # seconds, as revised by the server
    queue_size: int


@dataclass
class Negotiated:
    """What the server actually granted.  Requested values are kept only for comparison."""

    requested: dict
    publish_interval: float
    keepalive_count: int
    lifetime_count: int
    session_timeout: float
    items: list[ItemRevision] = field(default_factory=list)
    queue_capability: str = "OVERFLOW_DETECTABLE"
    violations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "requested": self.requested,
            "publish_interval": self.publish_interval,
            "keepalive_count": self.keepalive_count,
            "lifetime_count": self.lifetime_count,
            "session_timeout": self.session_timeout,
            "queue_capability": self.queue_capability,
            "min_queue": min((i.queue_size for i in self.items), default=None),
            "violations": list(self.violations),
        }


class SubscriptionError(RuntimeError):
    pass


class ManagedSubscription(Subscription):
    """Subscription whose publish callback hands the raw result to the application at once."""

    def __init__(self, session, params, on_result, handles: dict[int, Point] | None = None):
        super().__init__(session, params, handler=None)
        self.on_result = on_result
        self.created = None
        # shared with the channel and filled BEFORE each request: initial data can beat the reply
        self.handles: dict[int, Point] = {} if handles is None else handles

    async def init(self):
        self.created = await super().init()  # explicit parameters; the revised result is what counts
        return self.created

    async def publish_callback(self, publish_result) -> None:
        self.on_result(publish_result)  # synchronous; never waits for storage

    async def create_items(self, points: list[Point], timing: Timing, chunk: int = 100) -> list[ItemRevision]:
        revisions = []
        for start in range(0, len(points), chunk):
            part = points[start : start + chunk]
            requests = []
            for k, point in enumerate(part):
                handle = start + k + 1
                self.handles[handle] = point  # known before the request: data may race the reply
                requests.append(
                    ua.MonitoredItemCreateRequest(
                        ItemToMonitor=ua.ReadValueId(
                            NodeId=ua.NodeId.from_string(point.node_id), AttributeId=ua.AttributeIds.Value
                        ),
                        MonitoringMode=ua.MonitoringMode.Reporting,
                        RequestedParameters=ua.MonitoringParameters(
                            ClientHandle=handle,
                            SamplingInterval=timing.ua_sampling_interval * 1000,
                            QueueSize=timing.ua_queue_size,
                            DiscardOldest=True,
                        ),
                    )
                )
            params = ua.CreateMonitoredItemsParameters(
                SubscriptionId=self.subscription_id,
                ItemsToCreate=requests,
                TimestampsToReturn=ua.TimestampsToReturn.Both,
            )
            results = await self.server.create_monitored_items(params)
            for point, result in zip(part, results, strict=True):
                revisions.append(
                    ItemRevision(
                        point.id,
                        int(result.StatusCode.value),
                        result.RevisedSamplingInterval / 1000,
                        int(result.RevisedQueueSize),
                    )
                )
        return revisions

    async def delete_quietly(self):
        try:
            await self.delete()
        except Exception:
            pass


async def open_subscription(
    client: ManagedClient, points: list[Point], timing: Timing, on_result, handles: dict | None = None
):
    """Create the subscription and its monitored items with explicit parameters; never leak on failure."""
    params = ua.CreateSubscriptionParameters(
        RequestedPublishingInterval=timing.ua_publish_interval * 1000,
        RequestedLifetimeCount=timing.ua_lifetime_count,
        RequestedMaxKeepAliveCount=timing.ua_keepalive_count,
        MaxNotificationsPerPublish=0,
        PublishingEnabled=True,
        Priority=0,
    )
    sub = ManagedSubscription(client.uaclient.session, params, on_result, handles)
    created = await sub.init()
    try:
        revisions = await sub.create_items(points, timing)
        bad = [r.point_id for r in revisions if r.status >> 30 == 2]
        if bad:
            raise SubscriptionError(f"monitored items refused: {bad[:5]}")
    except BaseException:
        await sub.delete_quietly()
        raise
    return sub, negotiate(timing, created, revisions, client.session_timeout / 1000)


def negotiate(timing: Timing, created, revisions: list[ItemRevision], session_timeout: float) -> Negotiated:
    publish = created.RevisedPublishingInterval / 1000
    keepalive, lifetime = int(created.RevisedMaxKeepAliveCount), int(created.RevisedLifetimeCount)
    smallest = min((r.queue_size for r in revisions), default=timing.ua_queue_size)
    violations = timing.check_negotiated(publish, keepalive, lifetime)
    return Negotiated(
        requested={
            "publish_interval": timing.ua_publish_interval,
            "keepalive_count": timing.ua_keepalive_count,
            "lifetime_count": timing.ua_lifetime_count,
            "queue_size": timing.ua_queue_size,
            "session_timeout": timing.ua_session_timeout,
        },
        publish_interval=publish,
        keepalive_count=keepalive,
        lifetime_count=lifetime,
        session_timeout=session_timeout,
        items=revisions,
        queue_capability=timing.queue_capability(smallest),
        violations=violations,
    )


# ---------------------------------------------------------------------------------- the channel


class UaChannel:
    """One subscription generation: publish results in, ordered batches and loss evidence out.

    Synchronous and I/O free.  Anything unexpected inside is definite evidence of a failed intake and
    is reported as loss (C1), never swallowed.
    """

    def __init__(self, source_id: str, gen: int, hub, handles: dict[int, Point], on_lost=None, clock=None):
        self.source_id, self.gen, self.hub, self.handles = source_id, gen, hub, handles
        self.tracker = PublishTracker()
        self.on_lost = on_lost
        self.clock = clock or (time.time, time.monotonic)
        self.stats = {"data": 0, "keepalive": 0, "duplicate": 0, "overflow": 0, "unknown_handle": 0}

    def _lost(self, reason: str, wall: float, mono: float):
        self.hub.lost(self.source_id, self.gen, reason, wall, mono)
        if self.on_lost is not None:
            self.on_lost(self.source_id, reason)

    def on_publish(self, result) -> None:
        wall, mono = self.clock[0](), self.clock[1]()
        try:
            self._handle(result, wall, mono)
        except Exception as error:  # a failed callback means data may be missing
            self._lost("CALLBACK_ERROR:" + type(error).__name__, wall, mono)

    def _handle(self, result, wall: float, mono: float):
        message = result.NotificationMessage
        notifications = message.NotificationData or []
        verdict = self.tracker.observe(int(message.SequenceNumber), bool(notifications))
        if verdict == "GAP":
            return self._lost("SEQUENCE_GAP", wall, mono)
        if verdict == "DUPLICATE":
            self.stats["duplicate"] += 1
            notifications = []  # already applied; it is still proof that the server is alive
        items, overflow = [], False
        for notification in notifications:
            if isinstance(notification, ua.StatusChangeNotification):
                return self._lost("SUBSCRIPTION_STATUS:" + str(notification.Status.name), wall, mono)
            if not isinstance(notification, ua.DataChangeNotification):
                continue
            for monitored in notification.MonitoredItems:
                point = self.handles.get(monitored.ClientHandle)
                if point is None:
                    self.stats["unknown_handle"] += 1
                    return self._lost("UNKNOWN_HANDLE", wall, mono)
                item = self.item_from(point, monitored.Value)
                overflow = overflow or is_overflow(item.status_code or 0)
                items.append(item)
        kind = "data" if items else "keepalive"
        self.stats[kind] += 1
        self.hub.offer(
            rec.InputBatch(
                self.source_id, self.gen, rec.KIND_UA_NOTIFY, wall, mono, tuple(items),
                publish_seq=int(message.SequenceNumber),
            )
        )  # fmt: skip
        if overflow:
            self.stats["overflow"] += 1
            self._lost("OVERFLOW", wall, mono)

    @staticmethod
    def item_from(point: Point, data_value) -> rec.Item:
        status = data_value.StatusCode or ua.StatusCode()
        code = int(status.value)
        quality = rec.GOOD if status.is_good() else rec.UNCERTAIN if status.is_uncertain() else rec.BAD
        raw = data_value.Value.Value if data_value.Value is not None else None
        numeric = isinstance(raw, int | float) and not isinstance(raw, bool)
        if numeric and raw not in point.invalid_values:
            value = raw * point.scale + point.offset
        else:
            value = None
            if quality == rec.GOOD:
                quality = rec.BAD  # a sentinel or non-numeric payload is not a usable Good value
        stamp = data_value.SourceTimestamp.timestamp() if data_value.SourceTimestamp else None
        return rec.Item(point.id, value, quality, code, stamp)


# --------------------------------------------------------------------------------- the acquirer


class UaAcquirer:
    """Connection owner for one OPC UA source: connect, subscribe, watch, tear down, back off."""

    def __init__(
        self,
        source: Source,
        points: list[Point],
        timing: Timing,
        hub,
        *,
        client_factory=None,
        ticker=every_second,
        sleep=asyncio.sleep,
    ):
        self.source, self.points, self.timing, self.hub = source, points, timing, hub
        self.client_factory = client_factory or self._default_client
        self.ticker, self.sleep = ticker, sleep
        self.gen = 0
        self.backoff = Backoff(timing)
        self.negotiated: Negotiated | None = None
        self.channel: UaChannel | None = None
        self.rebuild_reason: str | None = None
        self.last_error: str | None = None
        self.history: list[str] = []

    def _default_client(self):
        client = ManagedClient(
            self.source.endpoint,
            timeout=self.timing.ua_request_timeout,
            auto_reconnect=False,
        )
        client.session_timeout = round(self.timing.ua_session_timeout * 1000)
        if self.source.username_env:
            client.set_user(os.environ[self.source.username_env])
            client.set_password(os.environ[self.source.password_env])
        return client

    def request_rebuild(self, source_id: str, reason: str):
        """Owner callback / channel evidence: this generation is finished."""
        if source_id == self.source.id and self.rebuild_reason is None:
            self.rebuild_reason = reason

    async def _teardown(self, client, sub):
        if sub is not None:
            await asyncio.wait_for(sub.delete_quietly(), self.timing.ua_request_timeout)
        try:
            await asyncio.wait_for(client.disconnect(), self.timing.ua_request_timeout)
        except Exception:
            client.disconnect_socket()  # the session may already be gone; never leave the socket open
        task = getattr(client.uaclient.session, "_publish_task", None)
        if task is not None and not task.done():
            task.cancel()

    async def _generation(self) -> str:
        client = self.client_factory()
        if self.source.security:
            await client.set_security_string(self.source.security)
        sub = None
        try:
            await client.connect()
            self.gen += 1
            self.rebuild_reason = None
            self.hub.begin_generation(self.source.id, self.gen, time.time(), time.monotonic())
            handles: dict[int, Point] = {}
            self.channel = UaChannel(self.source.id, self.gen, self.hub, handles, on_lost=self.request_rebuild)
            sub, self.negotiated = await open_subscription(
                client, self.points, self.timing, self.channel.on_publish, handles
            )
            async for _ in self.ticker():
                if self.rebuild_reason:
                    return self.rebuild_reason
                try:
                    await client.check_connection()  # local transport state only; half-open links fall to the timers
                except (ConnectionError, OSError):
                    return "CONNECTION"
                state = self.hub.owner.sources[self.source.id].state
                if state == "VALID":
                    self.backoff.healthy(time.monotonic())
                    self.backoff.settle(time.monotonic())
            return "ENDED"
        finally:
            await self._teardown(client, sub)

    async def run(self):
        while True:
            try:
                reason = await self._generation()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                reason = "ERROR:" + type(error).__name__
                self.last_error = repr(error)
            self.history.append(reason)
            if self.gen:
                self.hub.source_down(self.source.id, self.gen, reason, time.time(), time.monotonic())
            await self.sleep(self.backoff.next_delay())
