"""D08-D11: batch intake, validity, batch-end operators and change baselines (终版 §6-§8, §10).

Each test names the failure it prevents: borrowed old values, mixed-version results, hidden loss.
"""

import ast
import inspect

import pytest
from owner_support import Recorder, make_owner, notify, poll, small_config

from htsg import owner as owner_module
from htsg import records as r
from htsg.derive import GAUGE_FACTOR
from htsg.ingress import Ingress

# ---------------------------------------------------------------- D08 intake


def test_a_batch_straddling_a_whole_second_is_never_split():
    owner, rec = make_owner()
    batch = r.InputBatch(
        "mb", 1, r.KIND_MODBUS_POLL, 1005.0, 105.0, tick=5, poll_id="p5",
        items=(
            r.Item("b1", 0.6, wall=1004.99, mono=104.99, response_id="resp0"),
            r.Item("b2", 0.9, wall=1005.01, mono=105.01, response_id="resp1"),
        ),
    )  # fmt: skip
    assert owner.accept(batch)
    batch_groups = [g for g in rec.commits if g.kind == r.GROUP_BATCH]
    assert len(batch_groups) == 1, "one planned round is one commit group even if responses straddle a second"
    group = batch_groups[0]
    assert {o.batch_seq for o in group.observations} == {1}
    assert {x.point_id for x in group.results} >= {"s1", "s2"}
    # each response keeps its own receive time; the round is not rewritten to one instant
    assert [o.received_wall for o in group.observations] == [1004.99, 1005.01]
    assert group.states[0].detail["responses"].keys() == {"resp0", "resp1"}


def test_a_batch_from_an_old_generation_cannot_overwrite_the_new_one():
    owner, rec = make_owner()
    owner.accept(notify([("ti", 500.0), ("to", 450.0)], gen=1))
    owner.begin_generation("ua", 2, 1020.0, 120.0, reason="REBUILD")
    assert owner.accept(notify([("ti", 999.0)], gen=1, wall=1021.0, mono=121.0)) is False
    assert owner.health["stale_generation_batches"] == 1
    assert owner.points["ti"].obs is None, "the new generation starts unknown; the old value is not carried"
    assert [o.value for o in rec.observations("ti")] == [500.0]


def test_same_instant_items_are_processed_in_batch_order_and_the_last_one_is_the_state():
    owner, rec = make_owner()
    owner.accept(notify([("ti", 490.0), ("to", 450.0), ("ti", 495.0), ("ti", 500.0)]))
    obs = rec.observations("ti")
    assert [o.value for o in obs] == [490.0, 495.0, 500.0]
    assert [o.item_seq for o in obs] == [0, 2, 3]
    assert len({o.received_wall for o in obs}) == 1 and len({o.id for o in obs}) == 3
    assert owner.points["ti"].obs.value == 500.0
    assert rec.results("dt")[-1].value == pytest.approx(50.0), "derived values use the batch-end state"


def test_a_full_entry_queue_is_reported_to_the_owner_not_just_counted():
    owner, rec = make_owner()
    ingress = Ingress(owner, capacity=2)
    owner.accept(poll(1, {"b1": 0.5, "b2": 0.5}))
    assert [ingress.offer(poll(t, {"b1": 0.5, "b2": 0.5})) for t in (2, 3, 4)] == [True, True, False]
    losses = rec.states("APP_LOSS")
    assert ingress.dropped == 1 and not losses, "reported in order, after the batches already queued"
    ingress.drain()
    losses = rec.states("APP_LOSS")
    assert len(losses) == 1 and losses[0].detail == {"reason": "INGRESS_FULL", "tick": 4, "count": 1}
    assert owner.health["app_loss"] == 1
    owner.accept(poll(5, {"b1": 0.5, "b2": 0.5}))
    # tick 3 was accepted but tick 4 was lost: 5 may not be paired with 3 as if they were neighbours
    assert rec.results("c1")[-1].reason == "BASELINE_ONLY"


# ------------------------------------------------------------ D09 validity


def test_bad_and_uncertain_payloads_are_kept_with_the_full_status_code_but_do_not_compute():
    owner, rec = make_owner()
    owner.accept(notify([("ti", 500.0), ("to", 450.0)]))
    assert rec.results("dt")[-1].value == 50.0
    owner.accept(notify([("ti", 512.5, r.BAD, 0x80350000)], seq=2, wall=1011.0, mono=111.0))
    kept = rec.observations("ti")[-1]
    assert (kept.value, kept.quality, kept.status_code) == (512.5, r.BAD, 0x80350000)
    assert kept.extreme is False
    dt = rec.results("dt")[-1]
    assert (dt.status, dt.reason, dt.value) == (r.RESULT_NA, "INPUT_UNAVAILABLE", None)


def test_missing_values_are_never_zero_filled():
    owner, rec = make_owner()
    owner.accept(notify([("ti", None, r.BAD, 0x80000000), ("to", 450.0), ("ti", float("nan"), r.GOOD)]))
    first, third = [o for o in rec.observations("ti")]
    assert first.value is None and third.value is None
    assert third.quality == r.BAD, "a Good status that carries no usable number is not trusted"
    assert owner.points["ti"].good is False


@pytest.mark.parametrize(
    ("value", "extreme"), [(800.0, False), (800.001, True), (-50.0, False), (-50.001, True)]
)
def test_extreme_is_strictly_beyond_the_limit_and_keeps_the_raw_value(value, extreme):
    owner, rec = make_owner()
    owner.accept(notify([("ti", value), ("to", 450.0)]))
    obs = rec.observations("ti")[0]
    assert obs.value == value and obs.extreme is extreme
    dt = owner.results["dt"].current
    if extreme:
        assert (dt.status, dt.reason) == (r.RESULT_NA, "EXTREME_INPUT"), "dependents get no number"
        assert rec.results("dt") == []
    else:
        assert dt.value == pytest.approx(value - 450.0)


# ------------------------------------------------------------- D10 operators


def test_bridge_strain_and_thermal_proxy_match_independent_hand_calculation():
    owner, rec = make_owner()
    owner.accept(poll(1, {"b1": 0.708, "b2": -0.3}))
    # 0.708 mV/V x N(1) x 1000 / GF(6) = 118 microstrain; -0.3 -> -50
    assert GAUGE_FACTOR == 6
    assert rec.results("s1")[0].value == pytest.approx(118.0)
    assert rec.results("s2")[0].value == pytest.approx(-50.0)
    owner.accept(notify([("ti", 500.0), ("to", 450.0)]))
    # E=170000 MPa, alpha=1.35e-5 /K, restraint 0.5 -> 1.1475 MPa/K ; (500-450) K -> 57.375 MPa
    sp = rec.results("sp")[0]
    assert sp.value == pytest.approx(57.375)
    assert sp.estimate["qualification"] == "ESTIMATED" and sp.config_id == owner.config.config_id


def test_a_missing_modbus_input_is_not_borrowed_from_the_previous_round():
    owner, rec = make_owner()
    owner.accept(poll(1, {"b1": 0.6, "b2": 0.6}))
    owner.accept(poll(2, {"b1": 0.66}))  # b2 not answered this round
    s2 = rec.results("s2")[-1]
    assert (s2.status, s2.reason) == (r.RESULT_NA, "INPUT_UNAVAILABLE")
    assert rec.results("s1")[-1].value == pytest.approx(110.0), "the answered input still computes"


def peak_config():
    points = [
        {"id": f"t{i}{k}", "source": "ua", "unit": "degC", "node_id": f"ns=2;s=t{i}{k}"}
        for i in (1, 2, 3)
        for k in "io"
    ]
    derived = [
        {"id": f"st{i}", "inputs": [f"t{i}i", f"t{i}o"], "operation": "thermal_stress", "unit": "MPa", "model_id": "thermal"}
        for i in (1, 2, 3)
    ]  # fmt: skip
    derived.append({"id": "peak", "inputs": ["st1", "st2", "st3"], "operation": "maximum", "unit": "MPa"})
    return small_config(points, derived)


def test_peak_is_the_signed_maximum_and_needs_every_estimate():
    owner, rec = make_owner(peak_config())
    # all three pairs cool (outer hotter): every stress is negative, the peak is the least negative
    owner.accept(notify([("t1i", 400.0), ("t1o", 420.0), ("t2i", 400.0), ("t2o", 410.0), ("t3i", 400.0), ("t3o", 440.0)]))
    stress = {p: rec.results(p)[-1].value for p in ("st1", "st2", "st3")}
    assert all(v < 0 for v in stress.values())
    assert rec.results("peak")[-1].value == max(stress.values()), "signed maximum, not the largest magnitude"
    owner.accept(notify([("t2i", 400.0, r.BAD, 0x80000000)], seq=2, wall=1011.0, mono=111.0))
    peak = rec.results("peak")[-1]
    assert (peak.status, peak.reason) == (r.RESULT_NA, "INPUT_UNAVAILABLE"), "a missing estimate is not read as 0"


def test_the_owner_module_does_no_waiting_database_access_or_clock_reads():
    tree = ast.parse(inspect.getsource(owner_module))
    imported = {
        (n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
        if not (isinstance(n, ast.ImportFrom) and n.level)
    }
    assert not imported & {"asyncio", "psycopg", "time", "threading", "socket"}, imported
    assert not any(isinstance(n, ast.Await | ast.AsyncFunctionDef) for n in ast.walk(tree))
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"time", "monotonic", "sleep", "execute"}, called


def test_results_record_their_batch_and_configuration_basis():
    owner, rec = make_owner()
    owner.accept(poll(7, {"b1": 0.6, "b2": 0.6}))
    s1 = rec.results("s1")[0]
    assert (s1.run_id, s1.source_id, s1.conn_gen, s1.seg, s1.batch_seq) == ("run", "mb", 1, 1, 1)
    assert s1.config_id == owner.config.config_id
    assert s1.id == r.result_id("run", "mb", 1, "s1")


# ------------------------------------------------------------ D11 change


def test_change_is_the_difference_of_two_consecutive_valid_rounds_and_is_not_divided_by_time():
    owner, rec = make_owner()
    owner.accept(poll(1, {"b1": 0.6, "b2": 0.0}))
    owner.accept(poll(2, {"b1": 0.9, "b2": 0.0}))
    assert owner.results["c1"].current.value == pytest.approx(50.0)
    (second,) = rec.results("c1")
    assert second.value == pytest.approx(50.0), "(900-600)/6 microstrain, regardless of the 1 s spacing"
    assert second.basis["tick"] == 2 and second.basis["previous_tick"] == 1
    owner.accept(poll(3, {"b1": 0.3, "b2": 0.0}))
    assert rec.results("c1")[-1].value == pytest.approx(-100.0), "the sign is kept across a zero crossing"


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(lambda o: o.accept(poll(2, {}, outcome="TIMEOUT")), id="failed-round"),
        pytest.param(lambda o: None, id="skipped-tick"),  # tick 2 simply never happens
        pytest.param(lambda o: o.begin_generation("mb", 2, 1002.0, 102.0, reason="RECONNECT"), id="reconnect"),
    ],
)
def test_after_failure_skip_or_reconnect_the_first_round_only_rebuilds_the_baseline(interruption):
    owner, rec = make_owner()
    owner.accept(poll(1, {"b1": 0.6, "b2": 0.6}))
    interruption(owner)
    gen = owner.sources["mb"].conn_gen
    owner.accept(poll(3, {"b1": 0.9, "b2": 0.6}, gen=gen))
    after = owner.results["c1"].current
    assert (after.status, after.reason) == (r.RESULT_NA, "BASELINE_ONLY"), "no difference across the gap"
    assert rec.results("c1") == [], "nothing was ever known, so nothing is stored"
    owner.accept(poll(4, {"b1": 0.9, "b2": 0.6}, gen=gen))
    assert rec.results("c1")[-1].value == pytest.approx(0.0)


def test_an_unrelated_input_failing_does_not_clear_this_points_baseline():
    owner, rec = make_owner()
    owner.accept(poll(1, {"b1": 0.6, "b2": 0.6}))
    owner.accept(poll(2, {"b1": 0.9}))  # only b2 failed
    assert rec.results("c1")[-1].value == pytest.approx(50.0), "b1's two consecutive rounds still count"
    assert owner.results["c2"].current.reason == "INPUT_UNAVAILABLE"
    owner.accept(poll(3, {"b1": 0.9, "b2": 0.6}))
    assert owner.results["c2"].current.reason == "BASELINE_ONLY", "b2 itself did fail, so its chain restarts"


def test_published_inputs_and_results_come_from_the_same_batch():
    owner, rec = make_owner()
    pubs = []
    owner.publish = pubs.append
    for tick, (a, b) in enumerate([(0.6, 0.1), (0.62, 0.2), (0.7, 0.2), (0.7, 0.4)], start=1):
        owner.accept(poll(tick, {"b1": a, "b2": b}))
    assert pubs, "every accepted round is published"
    seqs = [p.pub_seq for p in pubs]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), "publish sequence is consecutive"
    for pub in pubs:
        raw = {o.point_id: o.value for o in pub.observations}
        for result in pub.results:
            if result.point_id in ("s1", "s2"):
                source = "b1" if result.point_id == "s1" else "b2"
                assert result.value == pytest.approx(raw[source] * 1000 / 6), "no old input with a new result"


def test_unchanged_results_are_not_rewritten_but_changed_inputs_are_always_stored():
    owner, rec = make_owner()
    for tick in (1, 2, 3):
        owner.accept(poll(tick, {"b1": 0.6, "b2": 0.6}))
    assert len(rec.observations("b1")) == 3, "every received round is stored"
    assert len(rec.results("s1")) == 1, "an identical strain is held, not rewritten each second"


def test_recorder_is_the_only_collaborator_needed():
    assert Recorder().commits == []


def test_the_configuration_hash_is_taken_once_not_once_per_result(monkeypatch):
    """Found while measuring the full-scale input in D45: the hash of the whole configuration was recomputed for every
    stored result, so one 200-item notification cost a third of a second of the single owner's time."""
    from htsg.config import Settings

    calls, real = [], Settings.config_id.fget
    monkeypatch.setattr(Settings, "config_id", property(lambda self: (calls.append(1), real(self))[1]))
    owner, _ = make_owner()
    taken = len(calls)
    owner.accept(notify([("ti", 500.0 + n / 10) for n in range(3)] + [("to", 440.0)]))
    owner.accept(poll(1, {"b1": 0.6, "b2": 0.7}))
    owner.accept(poll(2, {"b1": 0.7, "b2": 0.8}))
    assert len(calls) == taken, "no result asked for the hash again"
