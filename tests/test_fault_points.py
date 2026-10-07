import pytest

from htsg.engine import Engine


def make_engine(settings):
    records = []
    engine = Engine(settings, lambda k, p, urgent: records.append((k, p)), 1000, 0)
    return engine, records


def receive(engine, t, value=10, point="x", quality="GOOD"):
    engine.receive(point, value, 1000 + t, t, quality)
    engine.tick(1000 + t, t)


def faults(records):
    return [p for k, p in records if k == "fault"]


def test_confirmation_is_elapsed_time_and_retains_exact_confirming_sample(settings):
    engine, records = make_engine(settings)
    for t in [0, 0.1, 0.2, 0.3, 0.4, 1, 2, 3, 4]:
        receive(engine, t)
    assert not faults(records), "five fast samples or four elapsed seconds must not confirm"
    receive(engine, 5, 12)
    record = faults(records)[0]
    assert (record["time"], record["value"], record["point_id"]) == (1005, 12, "x")
    assert record["sample_id"] == engine.states["x"].latest.id
    assert not any(k in ("event", "trigger", "capture_plan") for k, _ in records)


@pytest.mark.parametrize("interruption", ["normal", "bad", "disconnect", "gap", "loss", "direction"])
def test_confirmation_cannot_bridge_interrupted_evidence(settings, interruption):
    engine, records = make_engine(settings)
    for t in range(4):
        receive(engine, t)
    if interruption == "normal":
        receive(engine, 4, 8)
    elif interruption == "bad":
        receive(engine, 4, None, quality="BAD")
    elif interruption == "disconnect":
        engine.disconnected("s", 1004, 4)
    elif interruption == "loss":
        engine.note_loss("x", "INGRESS_OVERFLOW", 1004, 1004, 1)
    elif interruption == "direction":
        receive(engine, 4, -1)
    for t in range(5, 10):
        receive(engine, t)
    assert not faults(records)
    receive(engine, 10)
    assert len(faults(records)) == 1


def test_one_record_until_valid_recovery_and_channels_are_independent(settings):
    engine, records = make_engine(settings)
    for t in range(7):
        receive(engine, t, point="x")
        receive(engine, t, point="y")
    assert {f["point_id"] for f in faults(records)} == {"x", "y"}
    engine.disconnected("s", 1007, 7)
    for t in range(8, 15):
        receive(engine, t)
    assert len(faults(records)) == 2, "disconnect does not prove recovery"
    receive(engine, 15, 8)
    for t in range(16, 22):
        receive(engine, t)
    assert len(faults(records)) == 3


def test_all_inputs_archived_even_without_faults_or_profiles(settings):
    engine, records = make_engine(settings.model_copy(update={"profiles": ()}))
    receive(engine, 0, 2)
    receive(engine, 1, None, quality="BAD")
    assert [p["sample"]["value"] for k, p in records if k == "raw"] == [2, None]
    assert not faults(records)


def test_restart_does_not_inherit_confirmation_time(settings):
    engine, _ = make_engine(settings)
    for t in range(5):
        receive(engine, t)
    restarted, records = make_engine(settings)
    receive(restarted, 5)
    assert not faults(records)
