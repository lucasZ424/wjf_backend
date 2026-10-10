import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from htsg.config import Settings
from htsg.storage import Store


@pytest.fixture
def settings():
    return Settings.model_validate(
        {
            "sources": [{"id": "s", "kind": "simulator", "enabled": False}],
            "points": [
                {
                    "id": "x",
                    "source": "s",
                    "unit": "test",
                    "aggregation": "Direct",
                    "low": 0,
                    "high": 8,
                    "stale_after": 100,
                },
                {
                    "id": "y",
                    "source": "s",
                    "unit": "test",
                    "aggregation": "Direct",
                    "high": 8,
                    "stale_after": 100,
                },
            ],
            "derived": [
                {"id": "delta", "inputs": ["x", "y"], "operation": "difference", "unit": "test"},
                {"id": "stress", "inputs": [], "operation": "unconfigured", "unit": "MPa"},
            ],
            "profiles": [
                {
                    "id": "p",
                    "trigger_points": ["x", "y"],
                    "source_points": ["x", "y"],
                    "pre": 10,
                    "recovery": 5,
                    "post": 2,
                }
            ],
        }
    )


@pytest.fixture
def databases(settings):
    credentials = json.loads(Path(".local/database.json").read_text())
    stores = []
    names = []
    for instance in ("primary", "emergency"):
        name = "htsg_test_" + uuid4().hex
        admin = make_conninfo(credentials[instance], dbname="postgres")
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        names.append((admin, name))
        store = Store(make_conninfo(credentials[instance], dbname=name))
        store.initialize(settings)
        stores.append(store)
    try:
        yield stores
    finally:
        for store in stores:
            store.close()
        for admin, name in names:
            assert name.startswith("htsg_test_") and len(name) == 42
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


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


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
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
    page.errors = []
    page.on("pageerror", lambda error: page.errors.append(str(error)))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    yield page
    context.close()
