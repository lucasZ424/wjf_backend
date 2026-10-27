import json
import sys
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo


@pytest.fixture
def archive_db_factory():
    """Throw-away databases on the development clusters; every one is dropped at teardown.

    Never touches the existing development or research databases.
    """
    from htsg.archive import ArchiveStore

    credentials = json.loads(Path(".local/database.json").read_text())
    created, stores = [], []

    def make(instance="primary", name=None):
        name = name or "htsg_test_" + uuid4().hex
        admin = make_conninfo(credentials[instance], dbname="postgres")
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append((admin, name))
        store = ArchiveStore(make_conninfo(credentials[instance], dbname=name))
        stores.append(store)
        return store

    try:
        yield make
    finally:
        for store in stores:
            store.close()
        for admin, name in created:
            assert name.startswith("htsg_test_") and len(name) == 42
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


# ---- real-browser fixtures (Chrome through Playwright, a running web application); imports stay lazy so the
# rest of the suite does not need Playwright


# Playwright's synchronous API keeps an event loop running in the test thread for as long as it is open, and no
# async test can run meanwhile ("Runner.run() cannot be called from a running event loop").  The browser therefore
# lives for ONE module: tests that need it sit in their own modules, and the async tests of the rest still run.
@contextmanager
def proactor_close_gives_up(seconds=15.0):
    """修复轮05 (G2): on Windows, closing an event loop waits for every cancelled I/O operation to complete, and one on
    the Playwright driver's pipe sometimes never does - twice the whole run hung in this fixture's teardown.  For the
    test process only: wait at most `seconds`, then keep that proactor alive with whatever the OS may still write into
    (freeing it is the crash the wait prevents) until the process ends, and say so.  No loop of the product is here."""
    if sys.platform != "win32":
        yield
        return
    from asyncio import windows_events

    original = windows_events.IocpProactor.close

    def close(self):
        if self._iocp is None:
            return
        for fut, _ov, _obj, _callback in list(self._cache.values()):
            if not fut.cancelled() and not isinstance(fut, windows_events._WaitCancelFuture):
                try:
                    fut.cancel()
                except OSError:
                    pass
        deadline = time.monotonic() + seconds
        while self._cache and time.monotonic() < deadline:
            self._poll(1.0)
        if self._cache:
            self.close = lambda: None  # not even at garbage collection
            KEPT.append(self)
            warnings.warn(f"an event loop left {len(self._cache)} I/O operation(s) pending after {seconds:.0f} s; not waiting longer")
            return
        original(self)

    windows_events.IocpProactor.close = close
    try:
        yield
    finally:
        windows_events.IocpProactor.close = original


KEPT = []  # proactors given up on: alive until the process ends


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with proactor_close_gives_up(), sync_playwright() as playwright:
        chrome = playwright.chromium.launch(channel="chrome", headless=True)
        yield chrome
        chrome.close()


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    from web_rig import WebRig

    with WebRig(tmp_path_factory.mktemp("web")) as running:
        yield running


@pytest.fixture(scope="module")
def archive_rig(tmp_path_factory):
    from web_rig import WebRig

    with WebRig(tmp_path_factory.mktemp("web"), archive=True) as running:
        yield running


@pytest.fixture(scope="module")
def replay_rig(tmp_path_factory):
    """An archive that holds exactly the Capstone F scenario (run "f"), for tests that check a replay."""
    from test_capstone_f import script
    from web_rig import WebRig

    with WebRig(tmp_path_factory.mktemp("web"), archive=True, run_id="f", feed_archive=False, steady=False) as running:
        running.write_groups(script(running.config)[1])
        yield running


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN", timezone_id="Asia/Shanghai")
    page = context.new_page()
    page.errors, page.console_log = [], []
    page.on("pageerror", lambda error: (page.errors.append(str(error)), page.console_log.append(f"pageerror: {error}")))
    page.on("console", lambda m: page.console_log.append(f"{m.type}: {m.text}"))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    yield page
    context.close()
