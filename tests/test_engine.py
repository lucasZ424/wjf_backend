import pytest

from htsg.engine import Engine


def engine(settings):
    records = []
    return Engine(settings, lambda k, p, priority: records.append((k, p, priority)), 1000, 0), records


def put(e, p, v, t, **kwargs):
    return e.receive(p, v, 1000 + t, t, **kwargs)


def test_multiple_samples_are_flagged_without_averaging_and_keep_raw_directions(settings):
    e, records = engine(settings)
    put(e, "x", 2, 0.1)
    put(e, "x", 10, 0.2)
    assert not any(k == "fault" for k, _, _ in records), "a short spike must not confirm a fault"
    assert e.latest_frame is None
    put(e, "x", -2, 0.3)
    put(e, "x", 2, 0.4)
    e.tick(1001.3, 1.3)
    value = e.latest_frame["values"]["x"]
    assert value["value"] is None and value["reason"] == "MULTIPLE_SAMPLES"
    assert value["high_occurred"] and value["low_occurred"]
    assert any(k == "raw" and p["sample"]["value"] == 10 for k, p, _ in records)


def test_current_state_does_not_recover_from_known_old_normal(settings):
    e, records = engine(settings)
    put(e, "x", 10, 0.1, source_time=1000.1)
    put(e, "y", 2, 0.1, source_time=1000.1)
    put(e, "x", 2, 0.2, source_time=999)
    assert e.states["x"].state == "HIGH"
    assert not e.faults["x"].recorded
    put(e, "x", 12, 0.3, source_time=998)
    assert not any(k == "fault" for k, _, _ in records)


def test_fault_recovery_allows_a_new_confirmation_without_post_capture(settings):
    e, records = engine(settings)
    for t in range(6):
        put(e, "x", 10, t)
        e.tick(1000 + t, t)
    put(e, "x", 2, 6)
    for t in range(7, 13):
        put(e, "x", 12, t)
        e.tick(1000 + t, t)
    faults = [p for k, p, _ in records if k == "fault"]
    assert [f["time"] for f in faults] == [1005, 1012]
    assert not any(k in ("event", "capture_plan", "trigger") for k, _, _ in records)


def test_missing_second_stays_unavailable_without_holding_last_value(settings):
    e, _ = engine(settings)
    put(e, "x", 2, 0.8)
    e.tick(1001.3, 1.3)
    e.tick(1002.3, 2.3)
    assert e.latest_frame["values"]["x"]["basis"] == "UNAVAILABLE"
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
    assert not any(k == "fault" for k, _, _ in records)
    assert records[-1][0] == "raw", "late observations remain archived without confirming current faults"


def test_resource_close_does_not_reopen_per_sample(settings):
    settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"max_capture": 1})})
    e, records = engine(settings)
    put(e, "x", 10, 0.1)
    e.tick(1002, 2)
    count = sum(k == "event" for k, _, _ in records)
    put(e, "x", 11, 2.1)
    put(e, "x", 12, 2.2)
    assert not any(f.recorded for f in e.faults.values())
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


@pytest.mark.parametrize("value", [-5, 0, 3, 1e308])
def test_one_hertz_sample_passes_through_without_numerical_aggregation(settings, value):
    e, _ = engine(settings)
    sample = put(e, "x", value, 0.1)
    e.tick(1001, 1)
    result = e.latest_frame["values"]["x"]
    assert result["value"] == value and result["quality"] == "GOOD"
    assert result["sample_ids"] == [sample.id]


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


def test_excess_samples_do_not_create_an_aggregate_even_with_large_values(settings):
    e, _ = engine(settings)
    put(e, "x", 1e308, 0.1)
    put(e, "x", 1e308, 0.2)
    e.tick(1001.3, 1.3)
    value = e.latest_frame["values"]["x"]
    assert value["value"] is None and value["quality"] == "PARTIAL"
    assert value["high_occurred"] and not e.faults["x"].recorded
