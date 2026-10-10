"""Protocol transport only; all observations enter the same engine queue."""

import asyncio
import math
import os
import struct
import time

from .config import Point, Source
from .timing import every_second


class OpcUaSubscriptionHandler:
    """Keep the latest DataValue delivered for every configured monitored item."""

    def __init__(self, points: list[Point]):
        self.points = {point.node_id: point for point in points}
        self.values = {}
        self.ready = asyncio.Event()
        self.status = None

    def datachange_notification(self, node, value, data):
        point = self.points.get(node.nodeid.to_string())
        if point is None:
            return
        data_value = data.monitored_item.Value
        good = data_value.StatusCode.is_good()
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value in point.invalid_values
        ):
            good = False
        stamp = data_value.SourceTimestamp.timestamp() if data_value.SourceTimestamp else None
        self.values[point.id] = (
            value * point.scale + point.offset if good else None,
            "GOOD" if good else "BAD",
            stamp,
        )
        if len(self.values) == len(self.points):
            self.ready.set()

    def status_change_notification(self, status):
        self.status = status


def decode_registers(registers: list[int], point: Point):
    words = list(registers)
    if point.word_order == "little":
        words.reverse()
    raw = b"".join(x.to_bytes(2, point.byte_order) for x in words)
    fmt = {"uint16": "H", "int16": "h", "uint32": "I", "int32": "i", "float32": "f", "float64": "d"}[
        point.data_type
    ]
    value = struct.unpack(">" + fmt, raw)[0]
    if not math.isfinite(value) or value in point.invalid_values:
        return None, "BAD"
    scaled = value * point.scale + point.offset
    return (scaled, "GOOD") if math.isfinite(scaled) else (None, "BAD")


async def adapter(source: Source, points: list[Point], submit, report):
    backoff = 1.0
    while True:
        try:
            if source.kind == "simulator":
                phase = 0
                async for _ in every_second():
                    now = time.time()
                    for p in points:
                        submit(
                            p.id,
                            p.simulator_base + p.simulator_amplitude * math.sin(phase),
                            "GOOD",
                            now,
                            None,
                        )
                    phase += source.interval
            elif source.kind == "modbus":
                from pymodbus.client import AsyncModbusTcpClient

                client = AsyncModbusTcpClient(
                    source.host, port=source.port, timeout=source.timeout, retries=0
                )
                try:
                    if not await client.connect():
                        raise ConnectionError("Modbus connection failed")
                    async for _ in every_second():
                        for p in points:
                            count = {
                                "uint16": 1,
                                "int16": 1,
                                "uint32": 2,
                                "int32": 2,
                                "float32": 2,
                                "float64": 4,
                            }[p.data_type]
                            read = (
                                client.read_holding_registers
                                if p.register_kind == "holding"
                                else client.read_input_registers
                            )
                            response = await read(p.address, count=count, device_id=p.device_id)
                            if response.isError():
                                submit(p.id, None, "BAD", None, None)
                            else:
                                value, quality = decode_registers(response.registers, p)
                                submit(p.id, value, quality, None, None)
                        backoff = 1
                finally:
                    client.close()
            else:
                from asyncua import Client, ua

                client = Client(source.endpoint, timeout=source.timeout)
                if source.username_env:
                    client.set_user(os.environ[source.username_env])
                    client.set_password(os.environ[source.password_env])
                if source.security:
                    await client.set_security_string(source.security)
                async with client:
                    nodes = [client.get_node(p.node_id) for p in points]
                    handler = OpcUaSubscriptionHandler(points)
                    subscription = await client.create_subscription(source.interval * 1000, handler)
                    handles = await subscription.subscribe_data_change(
                        nodes, queuesize=1, sampling_interval=source.interval * 1000
                    )
                    for handle in handles:
                        if isinstance(handle, ua.StatusCode):
                            handle.check()
                    await asyncio.wait_for(
                        handler.ready.wait(), max(source.timeout, source.interval * 3)
                    )
                    async for _ in every_second():
                        await client.check_connection()
                        if handler.status is not None and not handler.status.is_good():
                            handler.status.check()
                        for point_id, (value, quality, stamp) in tuple(handler.values.items()):
                            submit(point_id, value, quality, stamp, None)
                        backoff = 1
        except asyncio.CancelledError:
            raise
        except Exception as error:
            report(source.id, type(error).__name__)
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)
