"""Protocol transport only; all observations enter the same engine queue."""

import asyncio
import math
import os
import struct
import time

from .config import Point, Source


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
                while True:
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
                    await asyncio.sleep(source.interval)
            elif source.kind == "modbus":
                from pymodbus.client import AsyncModbusTcpClient

                client = AsyncModbusTcpClient(
                    source.host, port=source.port, timeout=source.timeout, retries=0
                )
                try:
                    if not await client.connect():
                        raise ConnectionError("Modbus connection failed")
                    while True:
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
                        await asyncio.sleep(source.interval)
                finally:
                    client.close()
            else:
                from asyncua import Client

                client = Client(source.endpoint, timeout=source.timeout)
                if source.username_env:
                    client.set_user(os.environ[source.username_env])
                    client.set_password(os.environ[source.password_env])
                if source.security:
                    await client.set_security_string(source.security)
                async with client:
                    nodes = [client.get_node(p.node_id) for p in points]
                    while True:
                        for p, node in zip(points, nodes):
                            data = await node.read_data_value(raise_on_bad_status=False)
                            good = data.StatusCode.is_good()
                            value = data.Value.Value if data.Value else None
                            if (
                                not isinstance(value, (int, float))
                                or isinstance(value, bool)
                                or value in p.invalid_values
                            ):
                                good = False
                            stamp = data.SourceTimestamp.timestamp() if data.SourceTimestamp else None
                            submit(
                                p.id,
                                value * p.scale + p.offset if good else None,
                                "GOOD" if good else "BAD",
                                stamp,
                                None,
                            )
                        backoff = 1
                        await asyncio.sleep(source.interval)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            report(source.id, type(error).__name__)
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)
