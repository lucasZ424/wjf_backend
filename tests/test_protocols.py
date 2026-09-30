import asyncio
import socket
import struct

from htsg.adapters import adapter
from htsg.config import Point, Source


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_modbus_local_server_enters_canonical_pipeline():
    from pymodbus.server import ModbusTcpServer
    from pymodbus.simulator import DataType, SimData, SimDevice

    port = free_port()
    words = list(struct.unpack(">HH", struct.pack(">f", 12.5)))
    context = SimDevice(
        id=1, simdata=SimData(address=0, values=words + [0] * 10, datatype=DataType.REGISTERS)
    )
    server = ModbusTcpServer(context, address=("127.0.0.1", port))
    await server.serve_forever(background=True)
    received = asyncio.Queue()
    point = Point(id="x", source="s", unit="test", aggregation="Last", address=0)
    source = Source(id="s", kind="modbus", enabled=True, host="127.0.0.1", port=port, interval=0.05)
    task = asyncio.create_task(
        adapter(source, [point], lambda *args: received.put_nowait(args), lambda *args: None)
    )
    try:
        sample = await asyncio.wait_for(received.get(), 5)
        assert sample[1] == 12.5 and sample[2] == "GOOD"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await server.shutdown()


async def test_opcua_local_server_preserves_value_quality_and_source_time():
    from asyncua import Server

    port = free_port()
    server = Server()
    await server.init()
    endpoint = f"opc.tcp://127.0.0.1:{port}/test/"
    server.set_endpoint(endpoint)
    node = await server.nodes.objects.add_variable(2, "TestValue", 25.0)
    point = Point(id="x", source="s", unit="test", aggregation="Last", node_id=node.nodeid.to_string())
    source = Source(id="s", kind="opcua", enabled=True, endpoint=endpoint, interval=0.05)
    received = asyncio.Queue()
    async with server:
        task = asyncio.create_task(
            adapter(source, [point], lambda *args: received.put_nowait(args), lambda *args: None)
        )
        try:
            sample = await asyncio.wait_for(received.get(), 10)
            assert sample[1] == 25.0 and sample[2] == "GOOD" and sample[3] is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
