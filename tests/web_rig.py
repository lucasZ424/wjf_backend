"""A running web application for real-browser tests (方案模块 G).

The owner, the publisher and the WebSocket live in a server thread exactly as they will in production; the test
thread only drives them through `call` and looks at the page with a real browser.  Data is produced by the rig's
own feeder (a steady Modbus round and an OPC UA keep-alive every second), and everything else is injected by the
test, so a browser test knows what the page must show.
"""

import asyncio
import socket
import threading
import time

import uvicorn
from live_support import BRIDGES, FULL, PROCESS, UA_NAMES
from owner_support import poll

from htsg import records as r
from htsg.auth import Accounts
from htsg.owner import Owner
from htsg.publisher import Publisher
from htsg.web import create_web_app

UA, MB = "ua_field", "mb_field"


def nominal():
    """Distinct, plausible values for all 200 OPC UA channels."""
    return {
        n: 440.0 if n.startswith("temp_outer") else 480.0 + i * 0.01 if n.startswith("temp_inner") else PROCESS[n]
        for i, n in enumerate(UA_NAMES)
    }


class WebRig:
    def __init__(self, tmp_path, config=FULL, archive_router=None, steady=True):
        self.config, self.archive_router, self.steady = config, archive_router, steady
        self.accounts = Accounts(tmp_path / "accounts.json")
        self.credentials = {
            "admin": self.accounts.create("admin", "admin"),
            "viewer": self.accounts.create("viewer", "viewer"),
        }
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self.ready, self.loop, self.error = threading.Event(), None, None
        self.tick_no, self.ua_seq, self.paused = 0, 0, False

    # ----------------------------------------------------------------------------- the server thread

    async def _main(self):
        try:
            self.loop = asyncio.get_running_loop()
            now, mono = time.time(), time.monotonic()
            self.owner = Owner(
                self.config, now, mono, publish=lambda group: self.publisher.offer(group), run_id="web"
            )
            self.publisher = Publisher(self.owner)
            for source in (MB, UA):
                self.owner.begin_generation(source, 1, now, mono)
            self.ua(list(nominal().items()))
            self.round()
            app = create_web_app(
                config=self.config, accounts=self.accounts, publisher=self.publisher,
                archive_router=self.archive_router,
            )  # fmt: skip
            self.server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
            )
            feeder = asyncio.create_task(self._feeder())
            starter = asyncio.create_task(self._announce())
            await self.server.serve()
            feeder.cancel()
            starter.cancel()
        except BaseException as error:  # noqa: BLE001 - reported to the test thread
            self.error = error
            self.ready.set()
            raise

    async def _announce(self):
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.ready.set()

    async def _feeder(self):
        while True:
            await asyncio.sleep(1.0)
            if self.paused:
                continue
            now, mono = time.time(), time.monotonic()
            self.owner.accept(r.InputBatch(UA, self.owner.sources[UA].conn_gen, r.KIND_UA_NOTIFY, now, mono))
            if self.steady:
                self.round()
            self.owner.tick(now, mono)

    # ------------------------------------------------------------ driven from the server thread (see `call`)

    def ua(self, items, quality=r.GOOD, status=0):
        self.ua_seq += 1
        now, mono = time.time(), time.monotonic()
        built = tuple(
            r.Item(i[0], i[1], i[2] if len(i) > 2 else quality, i[3] if len(i) > 3 else status) for i in items
        )
        gen = self.owner.sources[UA].conn_gen
        return self.owner.accept(r.InputBatch(UA, gen, r.KIND_UA_NOTIFY, now, mono, built, publish_seq=self.ua_seq))

    def round(self, values=None, **kw):
        """One Modbus round; by default the 8 bridges drift slowly so every round changes something."""
        self.tick_no += 1
        values = values or {b: 0.7 + 0.001 * k + 0.0001 * self.tick_no for k, b in enumerate(BRIDGES)}
        now, mono = time.time(), time.monotonic()
        batch = poll(self.tick_no, values, source=MB, wall=now, mono=mono, **kw)
        return self.owner.accept(batch)

    # ------------------------------------------------------------------------------------ the test thread

    def call(self, fn, *args, **kwargs):
        """Run `fn` on the server's event loop thread and return its result (the owner belongs there)."""

        async def run():
            return fn(*args, **kwargs)

        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(10)

    def drop_clients(self):
        """Cut every connected client off the way an overflow does."""

        def cut():
            for deliver in list(self.publisher.subscribers):
                client = getattr(deliver, "__self__", None)
                if client is not None:
                    client.lost = True
                    client.ready.set()

        self.call(cut)

    def start(self):
        self.thread.start()
        assert self.ready.wait(20), "the web application did not start"
        if self.error:
            raise self.error
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
