"""Continuous raw archive replaces Pre coverage: ring eviction cannot remove queued history."""

from htsg.engine import Engine


def test_ring_capacity_does_not_limit_continuous_archive(settings):
    config = settings.model_copy(update={"limits": settings.limits.model_copy(update={"ring_samples": 2})})
    records = []
    engine = Engine(config, lambda k, p, urgent: records.append((k, p)), 1000, 0)
    for t in range(20):
        engine.receive("x", 2, 1000 + t, t)
        engine.tick(1000 + t, t)
    assert len(engine.states["x"].ring) <= 2
    raw = [p["sample"] for k, p in records if k == "raw"]
    assert len(raw) == 20
    assert [s["received_at"] for s in raw] == list(range(1000, 1020))
    assert not any(k in ("capture_plan", "event") for k, _ in records)


def test_bad_quality_is_archived_without_certifying_fault_continuity(settings):
    records = []
    engine = Engine(settings, lambda k, p, urgent: records.append((k, p)), 1000, 0)
    engine.receive("x", None, 1000, 0, "BAD")
    assert records[-1][0] == "raw" and records[-1][1]["sample"]["quality"] == "BAD"
    assert engine.faults["x"].started is None
