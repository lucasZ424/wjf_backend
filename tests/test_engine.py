import pytest

from htsg.engine import Engine


def engine(settings):
    records = []
    return Engine(settings, lambda k, p, priority: records.append((k, p, priority)), 1000, 0), records


def put(e, p, v, t, **kwargs):
    return e.receive(p, v, 1000 + t, t, **kwargs)


def test_spike_is_detected_before_mean_and_both_directions_survive(settings):
    e, records = engine(settings)
    put(e, "x", 2, 0.1)
    put(e, "x", 10, 0.2)
    assert any(k == "trigger" for k, _, _ in records)
    assert e.latest_frame is None
    put(e, "x", -2, 0.3)
    put(e, "x", 2, 0.4)
    e.tick(1001.3, 1.3)
    value = e.latest_frame["values"]["x"]
    assert value["value"] == 3
    assert value["high_occurred"] and value["low_occurred"]
    assert any(k == "sample" and p["sample"]["value"] == 10 for k, p, _ in records)


def test_current_state_does_not_recover_from_known_old_normal(settings):
    e, records = engine(settings)
    put(e, "x", 10, 0.1, source_time=1000.1)
    put(e, "y", 2, 0.1, source_time=1000.1)
    put(e, "x", 2, 0.2, source_time=999)
    assert e.states["x"].state == "HIGH"
    assert e.active["p"].phase == "ACTIVE"
    put(e, "x", 12, 0.3, source_time=998)
    assert any(k == "trigger" and p["historical"] for k, p, _ in records)


def test_recovery_restarts_after_real_unavailability(settings):
    e, _ = engine(settings)
    put(e, "x", 10, 0.1)
    put(e, "y", 2, 0.2)
    put(e, "x", 2, 1)
    assert e.active["p"].phase == "RECOVERY_PENDING"
    e.tick(1004, 4)
    e.disconnected("s", 1004, 4)
    put(e, "x", 2, 5)
    put(e, "y", 2, 5)
    e.tick(1007, 7)
    assert e.active["p"].phase == "RECOVERY_PENDING"
    e.tick(1010, 10)
    assert e.active["p"].phase == "POST_CAPTURE"


def test_post_retrigger_keeps_event_and_final_frame_is_archived(settings):
    e, records = engine(settings)
    put(e, "x", 10, 0.1)
    put(e, "y", 2, 0.1)
    put(e, "x", 2, 1)
    identity = e.active["p"].id
    e.tick(1006.3, 6.3)
    put(e, "x", 12, 7)
    assert e.active["p"].id == identity and e.active["p"].phase == "ACTIVE"
    put(e, "x", 2, 8)
    e.tick(1013.1, 13.1)
    e.tick(1015.2, 15.2)
    assert "p" not in e.active
    e.tick(1016.3, 16.3)
    assert any(
        k == "frame" and identity in p["event_ids"] and p["frame"]["window_start"] == 1015
        for k, p, _ in records
    )


def test_hold_uses_window_end_and_disconnect_cannot_resurrect_cached_value(settings):
    e, _ = engine(settings)
    put(e, "x", 2, 0.8)
    e.tick(1001.3, 1.3)
    e.tick(1002.3, 2.3)
    assert e.latest_frame["values"]["x"]["basis"] == "HELD_VALUE"
    e.disconnected("s", 1002.4, 2.4)
    e.tick(1003.3, 3.3)
    assert e.latest_frame["values"]["x"]["value"] is None


def test_same_timestamp_normal_cannot_prove_recovery(settings):
    e, _ = engine(settings)
    put(e, "x", 10, 0.1)
    put(e, "y", 2, 0.1)
    put(e, "x", 2, 0.1)
    assert e.states["x"].state == "HIGH"


def test_cold_start_partial_frame_and_unknown_model(settings):
    e = Engine(settings, lambda *args: None, 1000.5, 0)
    e.tick(1001.3, 0.8)
    assert e.latest_frame["partial"]
    assert e.latest_frame["derived_results"]["stress"]["reason"] == "MODEL_NOT_CONFIGURED"
    assert e.latest_frame["derived_results"]["delta"]["value"] is None


def test_source_time_late_bucket_not_silently_rewritten(settings):
    settings = settings.model_copy(update={"time_basis": "source"})
    e, records = engine(settings)
    put(e, "x", 2, 0.1, source_time=1000.1)
    e.tick(1001.3, 1.3)
    before = e.latest_frame["values"]["x"]["value"]
    put(e, "x", 10, 1.4, source_time=1000.2)
    assert e.latest_frame["values"]["x"]["value"] == before
    assert any(k == "trigger" and p["window_start"] == 1000 for k, p, _ in records)


def test_resource_close_does_not_reopen_per_sample(settings):
    settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"max_capture": 1})})
    e, records = engine(settings)
    put(e, "x", 10, 0.1)
    e.tick(1002, 2)
    count = sum(k == "event" for k, _, _ in records)
    put(e, "x", 11, 2.1)
    put(e, "x", 12, 2.2)
    assert not e.active
    assert sum(k == "event" for k, _, _ in records) == count


def test_clock_rollback_changes_identity_and_breaks_hold(settings):
    e, _ = engine(settings)
    put(e, "x", 2, 0.1)
    e.tick(1001.3, 1.3)
    old = e.latest_frame["run_id"]
    e.tick(990, 2)
    e.tick(991.3, 3.3)
    assert e.latest_frame["run_id"] != old
    assert e.latest_frame["values"]["x"]["value"] is None


@pytest.mark.parametrize(
    "aggregation,expected",
    [("Mean", 2), ("Median", 3), ("Last", -5), ("RMS", (98 / 3) ** 0.5), ("Peak", 8), ("MaxAbs", 8)],
)
def test_aggregation_definitions(settings, aggregation, expected):
    point = settings.points[0].model_copy(update={"aggregation": aggregation})
    settings = settings.model_copy(update={"points": (point, settings.points[1])})
    e, _ = engine(settings)
    for t, v in [(0.1, 3), (0.2, 8), (0.3, -5)]:
        put(e, "x", v, t)
    e.tick(1001.3, 1.3)
    assert e.latest_frame["values"]["x"]["value"] == pytest.approx(expected)


def test_sample_budget_cannot_hide_observed_abnormal_directions(settings):
    settings = settings.model_copy(
        update={"limits": settings.limits.model_copy(update={"samples_per_window": 1})}
    )
    e, _ = engine(settings)
    put(e, "x", 2, 0.1)
    put(e, "x", 10, 0.2)
    put(e, "x", -2, 0.3)
    value = e.make_frame(1000)["values"]["x"]
    assert value["quality"] == "PARTIAL"
    assert value["high_occurred"] and value["low_occurred"]


def test_old_calculation_cannot_attach_to_event_after_clock_rollback(settings):
    e, records = engine(settings)
    put(e, "x", 10, 0.1)
    old = e.make_frame(1000)
    e.tick(990, 1)
    e.receive("x", 10, 990.1, 1.1)
    e.finish_frame(old)
    last = [payload for kind, payload, _ in records if kind == "frame"][-1]
    assert last["event_ids"] == [] and not last["display"]
    assert e.latest_frame is None


def test_finite_input_arithmetic_overflow_is_visible_without_stopping_engine(settings):
    e, _ = engine(settings)
    put(e, "x", 1e308, 0.1)
    put(e, "x", 1e308, 0.2)
    e.tick(1001.3, 1.3)
    value = e.latest_frame["values"]["x"]
    assert value["value"] is None and value["quality"] == "PARTIAL"
    assert value["high_occurred"] and e.active["p"].phase == "ACTIVE"
