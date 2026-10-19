"""Local, real protocol servers with fault injection, for acquirer tests (no mocks of the client path)."""

import asyncio
import socket
import struct

from htsg.config import Point, Source, Timing


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def bridge_points(n=8):
    return [
        Point(id=f"bridge_{i + 1}", source="mb", unit="mV/V", address=2 * i, data_type="float32",
              extreme_low=-30, extreme_high=30)
        for i in range(n)
    ]  # fmt: skip


class FieldModbus:
    """Eight float32 bridge values at registers 0-15, served by a real pymodbus TCP server."""

    def __init__(self, port=None, values=None):
        self.port = port or free_port()
        self.values = list(values or [0.5 + 0.01 * i for i in range(8)])
        self.delay = 0.0  # seconds every request is held (injects timeouts)
        self.withhold = 0  # the next this-many requests are never answered - not even late - until the server is stopped
        self.exception = False  # answer with a Modbus exception instead of data
        self.requests = []  # (start_address, count) of every request seen
        self.release = asyncio.Event()
        self.server = None

    async def action(self, function_code, start_address, address, count, registers, set_values):
        from pymodbus.constants import ExcCodes

        self.requests.append((address, count))
        if self.withhold:
            self.withhold -= 1
            await self.release.wait()  # a late answer would be tagged with the id of the NEWEST request: it is never given
            return ExcCodes.DEVICE_FAILURE  # only after the server is stopped; nobody is waiting for it then
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exception:
            return ExcCodes.DEVICE_FAILURE
        words = []
        for v in self.values:
            words += list(struct.unpack(">HH", struct.pack(">f", v)))
        registers[: len(words)] = words  # the block is addressed absolutely
        return None

    async def start(self):
        from pymodbus.server import ModbusTcpServer
        from pymodbus.simulator import DataType, SimData, SimDevice

        self.release = asyncio.Event()
        device = SimDevice(
            1, simdata=SimData(0, values=[0] * 16, datatype=DataType.REGISTERS), action=self.action
        )
        self.server = ModbusTcpServer(device, address=("127.0.0.1", self.port))
        await self.server.serve_forever(background=True)

    async def stop(self):
        self.release.set()
        if self.server:
            await self.server.shutdown()
            self.server = None

    def source(self, **kw):
        return Source(id="mb", kind="modbus", enabled=True, host="127.0.0.1", port=self.port, interval=1, **kw)


def device_points(devices=(1, 2)):
    """Two float32 inputs per device: one read request per device, so a round has as many requests as devices."""
    return [
        Point(id=f"d{d}_{name}", source="mb", unit="mV/V", address=2 * i, device_id=d, data_type="float32",
              extreme_low=-30, extreme_high=30)
        for d in devices
        for i, name in enumerate("ab")
    ]  # fmt: skip


class FieldModbusDevices:
    """Several slaves on ONE real pymodbus TCP server, each holding eight float32 values at registers 0-15.

    Each slave is set (`mode[device]`, changeable at any time) to answer normally (OK), to never answer (SILENT: the
    request is held until `release`) or to answer with a protocol exception (EXCEPTION).  A silent slave never answers
    at all: the pymodbus server tags a late answer with the transaction id of the NEWEST request, so one that came
    late could pass as the answer to a request that has not been answered."""

    OK, SILENT, EXCEPTION = "OK", "SILENT", "EXCEPTION"

    def __init__(self, devices=(1, 2), port=None):
        self.port = port or free_port()
        self.mode = dict.fromkeys(devices, self.OK)
        self.values = {d: [0.5 + 0.01 * i + d for i in range(8)] for d in devices}
        self.requests = []  # (device_id, address, count) of every request seen
        self.release = asyncio.Event()
        self.server = None

    def _action_of(self, device):
        async def action(function_code, start_address, address, count, registers, set_values):
            from pymodbus.constants import ExcCodes

            self.requests.append((device, address, count))
            mode = self.mode[device]
            if mode == self.SILENT:
                await self.release.wait()
                return ExcCodes.DEVICE_FAILURE  # only after the server is stopped; nobody is waiting for it then
            if mode == self.EXCEPTION:
                return ExcCodes.DEVICE_FAILURE
            words = []
            for v in self.values[device]:
                words += list(struct.unpack(">HH", struct.pack(">f", v)))
            registers[: len(words)] = words  # the block is addressed absolutely
            return None

        return action

    async def start(self):
        from pymodbus.server import ModbusTcpServer
        from pymodbus.simulator import DataType, SimData, SimDevice

        self.release = asyncio.Event()
        devices = [
            SimDevice(d, simdata=SimData(0, values=[0] * 16, datatype=DataType.REGISTERS), action=self._action_of(d))
            for d in self.mode
        ]
        self.server = ModbusTcpServer(devices, address=("127.0.0.1", self.port))
        await self.server.serve_forever(background=True)

    async def stop(self):
        self.release.set()
        if self.server:
            await self.server.shutdown()
            self.server = None

    def source(self, **kw):
        return Source(id="mb", kind="modbus", enabled=True, host="127.0.0.1", port=self.port, interval=1, **kw)


def fast_ticker(period=0.02):
    """Planned tick numbers delivered quickly so tests do not wait whole seconds per round."""

    async def ticks():
        n = 0
        while True:
            yield n
            n += 1
            await asyncio.sleep(period)

    return ticks


def timing(**over):
    return Timing(**over)


class FieldUa:
    """A real asyncua server with writable values and quality (no mocks of the client path)."""

    def __init__(self, names, port=None, *, keepalive_adapter=True):
        self.port = port or free_port()
        self.names = list(names)
        self.keepalive_adapter = keepalive_adapter
        self.nodes = {}
        self.server = None

    @property
    def endpoint(self):
        return f"opc.tcp://127.0.0.1:{self.port}/t/"

    async def start(self):
        from asyncua import Server, ua

        from htsg.sim_ua import install_keepalive_adapter

        self.server = Server()
        await self.server.init()
        if self.keepalive_adapter:
            install_keepalive_adapter(self.server)
        self.server.set_endpoint(self.endpoint)
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        index = await self.server.register_namespace("urn:htsg:test")
        container = await self.server.nodes.objects.add_object(index, "T")
        for name in self.names:
            self.nodes[name] = await container.add_variable(
                ua.NodeId.from_string(f"ns={index};s={name}"), name, 0.0
            )
        await self.server.start()
        self.index = index

    def node_id(self, name):
        return f"ns={self.index};s={name}"

    async def stop(self):
        if self.server:
            await self.server.stop()
            self.server = None

    async def set(self, name, value, good=True):
        from datetime import UTC, datetime

        from asyncua import ua

        status = ua.StatusCode(ua.StatusCodes.Good if good else ua.StatusCodes.BadSensorFailure)
        await self.nodes[name].write_value(
            ua.DataValue(ua.Variant(value, ua.VariantType.Double), status, SourceTimestamp=datetime.now(UTC))
        )

    @property
    def subscription_count(self):
        service = self.server.iserver.isession.iserver.subscription_service
        return len(service.subscriptions)


class PausableProxy:
    """TCP forwarder that can stall traffic without closing it: silence, not a disconnect."""

    def __init__(self, upstream_port):
        self.upstream, self.port = upstream_port, free_port()
        self.flowing = asyncio.Event()
        self.flowing.set()
        self.tasks, self.writers, self.server = [], [], None

    async def start(self):
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", self.port)

    async def _accept(self, reader, writer):
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.upstream)
        self.writers += [writer, up_writer]
        self.tasks += [
            asyncio.create_task(self._pipe(reader, up_writer)),
            asyncio.create_task(self._pipe(up_reader, writer)),
        ]

    async def _pipe(self, source, target):
        try:
            while data := await source.read(65536):
                await self.flowing.wait()
                target.write(data)
                await target.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            target.close()

    def pause(self):
        self.flowing.clear()

    def resume(self):
        self.flowing.set()

    async def stop(self):
        self.flowing.set()
        for writer in self.writers:
            writer.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.server:
            self.server.close()
