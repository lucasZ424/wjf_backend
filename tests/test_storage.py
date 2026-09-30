import time
from dataclasses import asdict

from htsg.engine import Event
from htsg.storage import Router


def frame(settings, identity, start):
    return {
        "id": identity,
        "run_id": identity.split(":")[0],
        "config_id": settings.config_id,
        "window_start": start,
        "window_end": start + 1,
        "partial": False,
        "values": {"x": {"value": 2, "quality": "GOOD", "basis": "WINDOW_VALUE", "sample_ids": []}},
        "derived_results": {},
    }


def test_seed_survives_archive_silence_and_sparse_recovery(settings, databases):
    store = databases[0]
    now = time.time()
    for n in range(4):
        store.write("frame", {"frame": frame(settings, "old:" + str(n), now - 100 + n), "event_ids": []})
    store.cleanup(now, pressure=True, seed_count=3, month_seconds=10)
    assert len(store.query("history")) == 3
    store.cleanup(now + 10**6, pressure=True, seed_count=3, month_seconds=10)
    assert len(store.query("history")) == 3
    store.write("frame", {"frame": frame(settings, "new:0", now + 10**6), "event_ids": []})
    store.write("frame", {"frame": frame(settings, "new:0", now + 10**6), "event_ids": []})
    store.cleanup(now + 10**6 + 20, pressure=True, seed_count=3, month_seconds=10)
    assert len(store.query("history")) == 3
    assert len([r for r in store.query("history") if r["id"].startswith("old:")]) == 2


def test_one_year_source_protection_even_under_pressure(settings, databases):
    store = databases[0]
    event = asdict(
        Event("e", "p", "r", settings.config_id, 1, 2, phase="CLOSED", end=3, archive_status="SUBMITTED")
    )
    store.write("event", event)
    sample = {"id": "s", "point_id": "x", "effective_time": 1, "value": 10}
    store.write("sample", {"event_id": "e", "sample": sample})
    store.cleanup(time.time() + 364 * 86400, pressure=True, seed_count=1, month_seconds=1)
    assert len(store.query("samples", event_id="e")) == 1
    store.cleanup(time.time() + 366 * 86400, pressure=True, seed_count=1, month_seconds=1)
    assert not store.query("samples", event_id="e")


def test_cross_instance_merge_preserves_run_identity_and_deduplicates_samples(settings, databases):
    router = Router(settings, *databases)
    for store in databases:
        store.write(
            "sample",
            {"event_id": "e", "sample": {"id": "s", "point_id": "x", "effective_time": 100, "value": 10}},
        )
    for n, store in enumerate(databases):
        store.write("frame", {"frame": frame(settings, f"run{n}:100", 100), "event_ids": []})
    assert len(router.merged("samples", event_id="e")["items"]) == 1
    assert len(router.merged("history")["items"]) == 2


def test_emergency_round_trip_persisted_and_reserve_guard(settings, databases):
    router = Router(settings, *databases)
    router.initialize()
    assert router.route(0.94, 2 * 1024**3) and router.mode == "PRIMARY"
    assert router.route(0.95, 2 * 1024**3) and router.mode == "EMERGENCY"
    restarted = Router(settings, *databases)
    restarted.initialize()
    assert restarted.mode == "EMERGENCY"
    assert restarted.route(0.8, 2 * 1024**3) and restarted.mode == "EMERGENCY"
    assert not restarted.route(0.8, 100)
    assert restarted.route(0.7, 2 * 1024**3) and restarted.mode == "PRIMARY"


def test_recomputable_result_cleanup_never_removes_source(settings, databases):
    store = databases[0]
    now = time.time()
    value = frame(settings, "old", now - 100)
    store.write("frame", {"frame": value, "event_ids": ["e"]})
    store.write("frame", {"frame": frame(settings, "new", now), "event_ids": []})
    event = asdict(
        Event(
            "e",
            "p",
            "r",
            settings.config_id,
            now - 100,
            now - 99,
            phase="CLOSED",
            end=now - 90,
            archive_status="SUBMITTED",
        )
    )
    store.write("event", event)
    store.write(
        "sample",
        {"event_id": "e", "sample": {"id": "s", "point_id": "x", "effective_time": now - 100, "value": 10}},
    )
    assert not store.certify_recomputable("old")
    with store.connect() as conn:
        conn.execute("UPDATE event_frame SET recomputable=true WHERE event_id='e'")
    result = store.cleanup(now, pressure=True, seed_count=1, month_seconds=10)
    assert result["event_results_removed"] == 1
    assert len(store.query("samples", event_id="e")) == 1
    assert store.query("products", event_id="e")[0]["results_removed"]


def test_restart_marks_open_event_interrupted(settings, databases):
    store = databases[0]
    store.write("event", asdict(Event("e", "p", "r", settings.config_id, 1, 2)))
    assert store.interrupt_events() == 1
    event = store.query("event", event_id="e")[0]["payload"]
    assert event["close_reason"] == "PROCESS_INTERRUPTED" and event["coverage"] == "PARTIAL"


def test_after_switchback_newer_primary_event_revision_wins(settings, databases):
    event = asdict(Event("e", "p", "r", settings.config_id, 1, 2, revision=4))
    databases[1].write("event", event)
    event.update(revision=5, phase="CLOSED", close_reason="RECOVERED")
    databases[0].write("event", event)
    result = Router(settings, *databases).merged("event", event_id="e")
    assert result["items"][0]["payload"]["phase"] == "CLOSED"


def test_late_flags_are_queryable_without_rewriting_value(settings, databases):
    store = databases[0]
    store.write("frame", {"frame": frame(settings, "r:100", 100), "event_ids": []})
    store.write(
        "trigger",
        {"id": "t", "event_id": "e", "point_id": "x", "run_id": "r", "window_start": 100, "state": "HIGH"},
    )
    item = store.query("history")[0]
    assert item["payload"]["value"] == 2 and item["high_recorded"]


def test_restart_cannot_turn_newer_closed_event_into_interrupted(settings, databases):
    event = asdict(Event("e", "p", "r", settings.config_id, 1, 2, revision=4))
    databases[1].write("event", event)
    event.update(revision=5, phase="CLOSED", close_reason="RECOVERED", archive_status="SUBMITTED")
    databases[0].write("event", event)
    router = Router(settings, *databases)
    router.initialize()
    assert router.merged("event", event_id="e")["items"][0]["payload"]["close_reason"] == "RECOVERED"


def test_event_history_exposes_only_profile_points(settings, databases):
    store = databases[0]
    item = frame(settings, "r:100", 100)
    item["derived_results"] = {"stress": {"value": None, "status": "NOT_AVAILABLE"}}
    store.write("event", asdict(Event("e", "p", "r", settings.config_id, 1, 2)))
    store.write("frame", {"frame": item, "event_ids": ["e"]})
    assert len(store.query("history")) == 2
    assert [r["point_id"] for r in store.query("history", event_id="e")] == ["x"]


def test_merged_event_keeps_gap_from_older_replica(settings, databases):
    event = asdict(Event("e", "p", "r", settings.config_id, 1, 2, revision=4))
    databases[1].write("event", event)
    databases[1].write("gap", {"id": "g", "event_id": "e", "reason": "TEST_LOSS"})
    event.update(revision=5, phase="CLOSED")
    databases[0].write("event", event)
    item = Router(settings, *databases).merged("event", event_id="e")["items"][0]
    assert item["has_gaps"] and item["payload"]["revision"] == 5


def test_seed_health_does_not_count_missing_seconds_as_coverage(settings, databases):
    store = databases[0]
    for start in [100, 1000]:
        store.write("frame", {"frame": frame(settings, f"r:{start}", start), "event_ids": []})
    metrics = store.metrics(settings.points)
    seed = next(x for x in metrics["seed"] if x["point_id"] == "x")
    assert seed["covered_seconds"] == 2 and seed["last_window"] - seed["first_window"] == 901
    assert metrics["last_frame_saved_at"] is not None


def test_first_raw_batch_on_emergency_carries_its_event_metadata(settings, databases, monkeypatch):
    router = Router(settings, *databases)
    router.initialize()
    router.mode = "EMERGENCY"
    monkeypatch.setattr(router, "route", lambda *args: True)
    event = asdict(Event("e", "p", "r", settings.config_id, 1, 2, revision=4))
    router.write(
        "sample",
        {
            "event_id": "e",
            "event": event,
            "sample": {"id": "s", "point_id": "x", "effective_time": 3, "value": 10},
        },
    )
    assert databases[1].query("event", event_id="e")[0]["payload"]["config_id"] == settings.config_id
    assert len(databases[1].query("samples", event_id="e")) == 1


def test_reconnecting_store_does_not_interrupt_live_capture(settings, databases, monkeypatch):
    router = Router(settings, *databases)
    router.initialize()
    event = asdict(Event("e", "p", "r", settings.config_id, 1, 2, revision=4))
    databases[0].write("event", event)
    router.ready.discard("primary")
    router.mode = "PRIMARY"
    monkeypatch.setattr(router, "route", lambda *args: True)
    router.write(
        "sample",
        {
            "event_id": "e",
            "event": event,
            "sample": {"id": "s", "point_id": "x", "effective_time": 3, "value": 10},
        },
    )
    assert databases[0].query("event", event_id="e")[0]["payload"]["phase"] == "ACTIVE"
