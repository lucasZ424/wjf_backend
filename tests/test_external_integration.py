"""Three databases on existing D: instances; disk pressure is injected, never generated."""

import json
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_storage import frame

from htsg.engine import Event
from htsg.storage import PolicyRefusal, Router, Store


@pytest.fixture
def external_store(settings):
    credentials = json.loads(Path(".local/database.json").read_text())
    name = "htsg_test_" + uuid4().hex
    admin = make_conninfo(credentials["primary"], dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    store = Store(make_conninfo(credentials["primary"], dbname=name))
    try:
        store.initialize(settings)
        yield store
    finally:
        store.close()
        assert name.startswith("htsg_test_") and len(name) == 42
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def routed(settings, databases, external_store, monkeypatch):
    config = settings.model_copy(
        update={
            "storage": settings.storage.model_copy(
                update={
                    "external_enabled": True,
                    "external_path": "D:/htsg-test-external",
                }
            )
        }
    )
    usage = {
        config.storage.primary_path: 0.1,
        config.storage.emergency_path: 0.1,
        config.storage.external_path: 0.1,
    }

    def disk_usage(path):
        total = 100 * 1024**3
        free = int(total * (1 - usage[path]))
        return SimpleNamespace(total=total, free=free, used=total - free)

    monkeypatch.setattr("shutil.disk_usage", disk_usage)
    router = Router(config, *databases, external_store)
    router.initialize()
    yield router, usage
    router.close()


def test_three_store_handoff_keeps_full_history_until_emergency_and_merges_raw(routed):
    router, usage = routed
    config = router.config
    event = asdict(
        Event("e", "p", "r", config.config_id, 1, 2, phase="CLOSED", archive_status="SUBMITTED", revision=3)
    )

    def batch(start):
        return [
            ("frame", {"frame": frame(config, f"r:{start}", start), "event_ids": ["e"]}),
            (
                "sample",
                {
                    "event_id": "e",
                    "event": event,
                    "sample": {"id": f"s{start}", "point_id": "x", "effective_time": start, "value": 10},
                },
            ),
        ]

    router.write_batch(batch(1))
    assert router.mode == "PRIMARY"
    usage[config.storage.primary_path] = 0.95
    router.write_batch(
        batch(2)
        + [
            (
                "trigger",
                {
                    "id": "t",
                    "event_id": "e",
                    "point_id": "x",
                    "run_id": "r",
                    "window_start": 2,
                    "state": "HIGH",
                },
            )
        ]
    )
    assert router.mode == "EXTERNAL"
    assert router.external.query("history")[0]["high_recorded"]

    restarted = Router(config, router.primary, router.emergency, router.external)
    restarted.initialize()
    usage[config.storage.primary_path] = 0.1
    restarted.write_batch(batch(3))
    assert restarted.mode == "EXTERNAL"
    usage[config.storage.external_path] = 0.95
    refused = restarted.write_batch(batch(4))
    assert restarted.mode == "EMERGENCY"
    assert len(refused) == 1 and refused[0][1] == "RESULT_ARCHIVE_SUSPENDED"
    assert not router.emergency.query("history")
    assert len(router.emergency.query("samples", event_id="e")) == 1

    usage[config.storage.external_path] = 0.7
    restarted.write_batch(batch(5))
    assert restarted.mode == "EXTERNAL"
    history = restarted.merged("history")["items"]
    assert [item["id"] for item in history] == ["r:1", "r:2", "r:3", "r:5"]
    assert next(item for item in history if item["id"] == "r:2")["high_recorded"]
    assert len(restarted.merged("samples", event_id="e")["items"]) == 5
    assert len(restarted.merged("triggers", event_id="e")["items"]) == 1
    assert len(restarted.merged("events")["items"]) == 1


def test_external_write_failure_after_probe_still_preserves_raw_in_emergency(routed, monkeypatch):
    router, usage = routed
    usage[router.config.storage.primary_path] = 0.95

    def fail(_):
        raise psycopg.OperationalError("injected failure after successful write probe")

    monkeypatch.setattr(router.external, "write_batch", fail)
    event = asdict(Event("e", "p", "r", router.config.config_id, 1, 2))
    refused = router.write_batch(
        [
            ("frame", {"frame": frame(router.config, "r:2", 2), "event_ids": ["e"]}),
            (
                "sample",
                {
                    "event_id": "e",
                    "event": event,
                    "sample": {"id": "s", "point_id": "x", "effective_time": 2, "value": 9},
                },
            ),
        ]
    )
    assert router.mode == "EMERGENCY" and router.external_selected
    assert len(refused) == 1 and refused[0][1] == "RESULT_ARCHIVE_SUSPENDED"
    assert len(router.emergency.query("samples", event_id="e")) == 1
    assert not router.primary.query("samples", event_id="e")


def test_unknown_external_size_on_restart_blocks_dev_write_without_losing_selection(routed, monkeypatch):
    router, usage = routed
    router.route(0.95, 10 * 1024**3, 0.1)
    restarted = Router(router.config, router.primary, router.emergency, router.external)
    restarted.initialize()
    usage[router.config.storage.external_path] = 1.0

    @contextmanager
    def offline():
        raise psycopg.OperationalError("external offline after restart")
        yield

    monkeypatch.setattr(router.external, "session", offline)
    with pytest.raises(PolicyRefusal, match="DEVELOPMENT_BUDGET_UNKNOWN"):
        restarted.write("gap", {"id": "g", "reason": "test"})
    assert restarted.mode == "EMERGENCY" and restarted.external_selected
    assert not router.emergency.query("gaps")


def test_cached_external_size_allows_bounded_emergency_after_unplug(routed, monkeypatch):
    router, usage = routed
    usage[router.config.storage.primary_path] = 0.95
    router.write("gap", {"id": "before", "reason": "test"})
    assert "external" in router.budget_sizes
    router.last_budget_check = 0
    usage[router.config.storage.external_path] = 1.0

    @contextmanager
    def offline():
        raise psycopg.OperationalError("external offline")
        yield

    monkeypatch.setattr(router.external, "session", offline)
    router.write("gap", {"id": "after", "reason": "test"})
    assert router.mode == "EMERGENCY"
    assert router.health["external_budget"] == "CACHED"
    assert router.emergency.query("gaps")[0]["payload"]["id"] == "after"


@pytest.mark.parametrize("event_filter", [False, True])
def test_late_trigger_on_external_marks_primary_frame_without_rewriting_value(routed, event_filter):
    router, _ = routed
    event = asdict(Event("late", "p", "r", router.config.config_id, 1, 2))
    router.primary.write("event", event)
    for run in ("r", "other"):
        router.primary.write("frame", {"frame": frame(router.config, f"{run}:2", 2), "event_ids": ["late"]})
    router.external.write(
        "trigger",
        {
            "id": "late-trigger",
            "event_id": "late",
            "run_id": "r",
            "point_id": "x",
            "window_start": 2,
            "state": "HIGH",
        },
    )
    router.emergency.write(
        "trigger",
        {
            "id": "late-low",
            "event_id": "late",
            "run_id": "r",
            "point_id": "x",
            "window_start": 2,
            "state": "LOW",
        },
    )
    filters = {"event_id": "late"} if event_filter else {}
    result = router.merged("history", **filters)
    rows = {row["id"]: row for row in result["items"]}
    assert rows["r:2"]["high_recorded"] and rows["r:2"]["low_recorded"]
    assert not rows["other:2"]["high_recorded"] and not rows["other:2"]["low_recorded"]
    assert rows["r:2"]["payload"]["value"] == 2
    assert len(router.primary.query("history")) == 2
    assert not router.external.query("history")


def test_flag_lookup_failure_marks_page_partial_and_disables_cursor(routed, monkeypatch):
    router, _ = routed
    for start in (1, 2):
        router.primary.write("frame", {"frame": frame(router.config, f"r:{start}", start), "event_ids": []})

    def unavailable(_):
        raise psycopg.OperationalError("external disappeared between history and flag reads")

    monkeypatch.setattr(router.external, "history_flags", unavailable)
    result = router.merged("history", limit=1)
    assert len(result["items"]) == 1 and result["has_more"]
    assert result["partial"] and result["pagination_incomplete"]
    assert result["unavailable_instances"] == ["external"] and result["next_cursor"] is None
