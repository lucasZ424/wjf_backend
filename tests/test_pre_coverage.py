from htsg.engine import Engine


def warmed(settings, seconds=13, deferred=False):
    records = []
    engine = Engine(settings, lambda *args: records.append(args), 1000, 0, defer_calculation=deferred)
    observed = []
    for second in range(seconds):
        for point, phase in (("x", 0.07), ("y", 0.36)):
            moment = second + phase
            observed.append(engine.receive(point, 1, 1000 + moment, moment))
        engine.tick(1000 + second + 0.6, second + 0.6)
    return engine, records, observed


def test_jittered_polling_proves_pre_without_exact_boundary_sample(settings):
    config = settings.model_copy(
        update={"points": tuple(point.model_copy(update={"stale_after": 5}) for point in settings.points)}
    )
    engine, records, observed = warmed(config)
    trigger = engine.receive("x", 9, 1012.77, 12.77)
    event = engine.active["p"]
    assert event.coverage == "OBSERVED"
    assert not any(kind == "gap" and payload["reason"] == "PRE_INSUFFICIENT" for kind, payload, _ in records)
    plan = next(payload["plan"] for kind, payload, _ in records if kind == "capture_plan")
    expected = {sample.id for sample in observed if sample.mono >= trigger.mono - 10}
    assert {sample.id for sample in plan.samples} == expected
    # The predecessor proves the boundary, but is not silently relabeled an in-window sample.
    assert engine.states["x"].ring[0].mono == 2.07
    assert engine.states["x"].ring[0].id not in expected
    # Frames intersecting the non-integer left boundary remain event products.
    assert [frame.window_start for frame in plan.frames] == list(range(1002, 1012))


def test_startup_without_full_pre_history_remains_partial(settings):
    engine, records, _ = warmed(settings, seconds=10)
    engine.receive("x", 9, 1009.77, 9.77)
    assert engine.active["p"].coverage == "PARTIAL"
    assert any(kind == "gap" and payload["reason"] == "PRE_INSUFFICIENT" for kind, payload, _ in records)


def test_sample_cap_overrides_boundary_evidence_and_does_not_invent_coverage(settings):
    config = settings.model_copy(update={"limits": settings.limits.model_copy(update={"ring_samples": 8})})
    engine, _, _ = warmed(config)
    engine.receive("x", 9, 1012.77, 12.77)
    assert engine.active["p"].coverage == "PARTIAL"
    assert all(len(state.ring) <= 8 for state in engine.states.values())


def test_shorter_profile_still_uses_its_own_boundary(settings):
    shorter = settings.profiles[0].model_copy(update={"id": "short", "pre": 3})
    config = settings.model_copy(update={"profiles": (*settings.profiles, shorter)})
    engine, _, _ = warmed(config, seconds=5)
    engine.receive("x", 9, 1004.77, 4.77)
    assert engine.active["p"].coverage == "PARTIAL"
    assert engine.active["short"].coverage == "OBSERVED"


def test_retained_boundary_does_not_hide_known_pre_loss_or_bad_quality(settings):
    engine, records, _ = warmed(settings)
    bad = engine.receive("y", None, 1012.65, 12.65, quality="BAD")
    engine.note_loss("x", "INGRESS_DROPPED", 1008, 1009, 2)
    engine.receive("x", 9, 1012.77, 12.77)
    event = engine.active["p"]
    assert event.coverage == "PARTIAL"
    assert any(
        kind == "gap" and payload["event_id"] == event.id and payload["reason"] == "PRE_SOURCE_LOSS"
        for kind, payload, _ in records
    )
    plan = next(payload["plan"] for kind, payload, _ in records if kind == "capture_plan")
    retained_bad = next(sample for sample in plan.samples if sample.id == bad.id)
    assert retained_bad.quality == "BAD" and retained_bad.value is None


def test_inflight_boundary_frame_associates_after_pre_plan(settings):
    engine, records, _ = warmed(settings, deferred=True)
    engine.receive("x", 9, 1012.77, 12.77)
    event = engine.active["p"]
    plan = next(payload["plan"] for kind, payload, _ in records if kind == "capture_plan")
    assert not plan.frames
    pending_inputs = [payload["frame"] for kind, payload, _ in records if kind == "frame_input"]
    for frame in pending_inputs:
        engine.finish_frame(frame)
    associated = [
        payload["frame"]["window_start"]
        for kind, payload, _ in records
        if kind == "frame" and event.id in payload["event_ids"]
    ]
    assert associated == list(range(1002, 1012))


def test_arbitrarily_old_predecessor_after_silence_does_not_prove_coverage(settings):
    config = settings.model_copy(
        update={"points": tuple(point.model_copy(update={"stale_after": 5}) for point in settings.points)}
    )
    engine = Engine(config, lambda *args: None, 1000, 0)
    engine.receive("x", 1, 1000, 0)
    engine.receive("y", 1, 1000, 0)
    engine.receive("y", 1, 1012, 12)
    engine.receive("x", 9, 1012.77, 12.77)
    assert engine.states["x"].ring[0].mono == 0
    assert engine.active["p"].coverage == "PARTIAL"


def test_point_without_observation_after_boundary_cannot_prove_pre(settings):
    engine = Engine(settings, lambda *args: None, 1000, 0)
    engine.receive("x", 1, 1001, 1)
    engine.receive("y", 1, 1001, 1)
    engine.receive("x", 1, 1003, 3)
    engine.receive("x", 9, 1012.77, 12.77)
    assert engine.active["p"].coverage == "PARTIAL"


def test_exact_left_boundary_observation_needs_no_outside_predecessor(settings):
    engine = Engine(settings, lambda *args: None, 1000, 0)
    engine.receive("x", 1, 1000, 0)
    engine.receive("y", 1, 1000, 0)
    engine.receive("x", 9, 1010, 10)
    assert engine.active["p"].coverage == "OBSERVED"
