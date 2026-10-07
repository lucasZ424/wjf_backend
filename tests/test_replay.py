"""Persistence and global replay against an isolated database on the local primary."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from htsg.api import create_app
from htsg.auth import Accounts
from htsg.engine import Engine
from htsg.runtime import Runtime
from htsg.storage import Router, Store


@pytest.fixture
def replay_store(settings):
    config = json.loads(Path(".local/database.json").read_text())
    admin = make_conninfo(config["primary"], dbname="postgres", connect_timeout=3)
    name = "htsg_test_" + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    store = Store(make_conninfo(config["primary"], dbname=name))
    try:
        store.initialize(settings)
        yield store
    finally:
        store.close()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_replay_persists_normal_raw_fault_point_and_unrelated_channels(settings, replay_store):
    records = []
    engine = Engine(settings, lambda k, p, urgent: records.append((k, p)), 1000, 0)
    for t in range(11):
        engine.receive("x", 10 if t < 7 else 2, 1000 + t, t)
        engine.receive("y", 3, 1000 + t, t)
        engine.tick(1000 + t, t)
    replay_store.write_batch(records)
    replay_store.write_batch(records)  # Retries must not duplicate a fault or its source data.
    faults = replay_store.query("faults")
    assert len(faults) == 1 and faults[0]["payload"]["time"] == 1005
    assert replay_store.query("events") == []
    rows = replay_store.query("replay", start=1000, end=1011, point="y")
    assert len(rows) == 11 and all(r["payload"]["value"] == 3 for r in rows)
    assert replay_store.query("replay", start=1008, end=1011, point="x")[0]["payload"]["value"] == 2
    assert any(r["product"] == "derived" for r in replay_store.query("replay", start=1000, end=1011))
    with replay_store.session() as conn:
        assert conn.execute("SELECT count(*) AS n FROM event_sample").fetchone()["n"] == 0


def test_fault_pagination_and_replay_cursors_are_stable(settings, replay_store):
    for t in range(4):
        replay_store.write("fault", {"id": str(t), "point_id": "x", "time": t})
    router = Router(settings, replay_store, replay_store)
    first = router.merged("faults", limit=2)
    second = router.merged("faults", limit=2, cursor=first["next_cursor"])
    assert [r["payload"]["time"] for r in first["items"] + second["items"]] == [3, 2, 1, 0]


def test_replay_api_uses_symmetric_global_window_and_requires_auth(settings, tmp_path):
    calls = []

    def merged(category, **kwargs):
        calls.append((category, kwargs))
        return {"items": [], "partial": False, "unavailable_instances": []}

    router = SimpleNamespace(stores=[("primary", None)], merged=merged)
    runtime = Runtime(settings, router)
    accounts = Accounts(tmp_path / "accounts.json")
    headers = {"Authorization": "Bearer " + accounts.create("reader", "viewer")}
    client = TestClient(create_app(runtime, accounts))
    try:
        assert client.get("/api/replay?center=1000&seconds=600").status_code == 401
        response = client.get("/api/replay?center=1000&seconds=600&point=y", headers=headers)
        assert response.status_code == 200
        assert (response.json()["start"], response.json()["end"]) == (700, 1300)
        category, filters = calls[-1]
        assert category == "replay" and filters["point"] == "y"
        assert "event_id" not in filters, "fault references must not narrow history to old event membership"
        assert client.get("/api/faults", headers=headers).status_code == 200
        for query in ("center=nan&seconds=10", "center=1&seconds=0", "center=1&seconds=inf"):
            assert client.get("/api/replay?" + query, headers=headers).status_code == 422
    finally:
        runtime.io.shutdown(wait=True)
        runtime.read_io.shutdown(wait=True)
        runtime.calc_io.shutdown(wait=True)
