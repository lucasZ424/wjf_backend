"""D27-D28: ordinary fault timing and two-sided hysteresis (终版 §8, §8.1; defaults in dev-docs/默认越限阈值与回差.md).

Why it matters: a constant violating value must still confirm after 5 s without any new sample, a single
number must not stand in for both hysteresis sides, and bad data must neither confirm nor recover a fault.
"""

import pytest

from htsg.config import fault_rule, load_settings
from htsg.fault_tracker import HIGH, LOW, FaultTracker

RULES = {
    "temp": {"low": None, "high": 490.0, "hysteresis_low": 0.0, "hysteresis_high": 3.0},
    "feed": {"low": 250.0, "high": 320.0, "hysteresis_low": 6.0, "hysteresis_high": 1.0},
    "dt": {"low": -55.0, "high": 55.0, "hysteresis_low": 4.0, "hysteresis_high": 4.0},
}


def tracker():
    return FaultTracker(RULES, 5.0)


def run(t, pid, value, start, seconds):
    """Hold `value` for `seconds` of monotonic time, ticking once a second; return confirmations."""
    out = []
    t.observe(pid, value, start)
    for second in range(1, int(seconds) + 1):
        out += t.advance(start + second)
    return out


# ------------------------------------------------------------------- D27


def test_a_constant_violating_value_confirms_after_five_seconds_with_no_new_sample():
    t = tracker()
    t.observe("temp", 495.0, 100.0)  # ONE observation; afterwards only the clock moves
    assert [t.advance(100.0 + s) for s in (1, 2, 3, 4)] == [[], [], [], []]
    assert t.advance(105.0) == [("temp", HIGH)]
    assert t.status("temp") == "CONFIRMED"


def test_4_9_seconds_do_not_confirm_and_5_0_does():
    t = tracker()
    t.observe("temp", 495.0, 100.0)
    assert t.advance(104.9) == [] and t.status("temp") == "PENDING"
    assert t.advance(105.0) == [("temp", HIGH)]


def test_coming_back_inside_the_bound_before_five_seconds_clears_the_clock_without_needing_hysteresis():
    t = tracker()
    t.observe("temp", 495.0, 100.0)
    t.advance(103.0)
    t.observe("temp", 489.0, 103.5)  # inside the bound, but NOT below bound - hysteresis (487)
    assert t.status("temp") == "NORMAL" and t.advance(110.0) == []
    t.observe("temp", 495.0, 111.0)
    assert t.advance(115.9) == [] and t.advance(116.0) == [("temp", HIGH)], "restarted from zero"


def test_invalid_data_cancels_a_pending_clock_but_does_not_recover_a_confirmed_fault():
    t = tracker()
    t.observe("temp", 495.0, 100.0)
    t.observe("temp", None, 102.0)  # bad quality / communication lost
    assert t.status("temp") == "NORMAL" and t.advance(120.0) == []
    t.observe("temp", 495.0, 121.0)
    assert t.advance(126.0) == [("temp", HIGH)]
    t.observe("temp", None, 130.0)
    assert t.status("temp") == "CONFIRMED", "unknown is not recovery"


def test_the_clock_restarts_after_a_restart_of_the_tracker():
    t = tracker()
    t.observe("temp", 495.0, 100.0)
    t.advance(105.0)
    fresh = tracker()  # a new process: timing starts again, nothing is inherited
    fresh.observe("temp", 495.0, 200.0)
    assert fresh.status("temp") == "PENDING" and fresh.advance(204.0) == []


# ------------------------------------------------------------------- D28


@pytest.mark.parametrize(
    ("pid", "value", "expected"),
    [("temp", 490.0, None), ("temp", 490.0001, HIGH), ("feed", 250.0, None), ("feed", 249.9999, LOW),
     ("dt", -55.0, None), ("dt", -55.01, LOW)],
)  # fmt: skip
def test_equal_to_the_bound_is_normal_strictly_beyond_is_a_violation(pid, value, expected):
    assert tracker().direction(pid, value) == expected


def test_recovery_includes_equality_and_uses_the_side_specific_hysteresis():
    t = tracker()
    run(t, "temp", 495.0, 100.0, 5)
    t.observe("temp", 487.5, 106.0)
    assert t.status("temp") == "CONFIRMED", "above 490-3=487: still standing"
    assert t.observe("temp", 487.0, 107.0) == "RECOVERED", "<= is inclusive"
    assert t.status("temp") == "NORMAL"
    # feedwater: high side uses 1, low side uses 6
    run(t, "feed", 321.0, 200.0, 5)
    assert t.observe("feed", 319.5, 206.0) is None and t.observe("feed", 319.0, 207.0) == "RECOVERED"
    run(t, "feed", 249.0, 300.0, 5)
    assert t.observe("feed", 255.9, 306.0) is None and t.observe("feed", 256.0, 307.0) == "RECOVERED"


def test_the_opposite_direction_restarts_the_clock_and_a_standing_fault_is_not_timed_again():
    t = tracker()
    t.observe("dt", 60.0, 100.0)
    t.advance(103.0)
    t.observe("dt", -60.0, 103.5)  # straight to the other side: a new clock from now
    assert t.advance(105.0) == [] and t.advance(108.4) == [] and t.advance(108.5) == [("dt", LOW)]
    assert t.advance(200.0) == [], "a confirmed fault is recorded once, not every five seconds"


def test_a_confirmed_fault_going_straight_to_the_other_side_recovers_then_times_the_new_direction():
    t = tracker()
    run(t, "dt", 60.0, 100.0, 5)
    assert t.confirmed_direction("dt") == HIGH
    assert t.observe("dt", -60.0, 106.0) == "RECOVERED"
    assert t.status("dt") == "PENDING"


def test_cancel_is_not_a_recovery():
    t = tracker()
    run(t, "dt", 60.0, 100.0, 5)
    t.cancel("dt")
    assert t.confirmed_direction("dt") == HIGH


def test_one_hysteresis_number_cannot_stand_for_both_sides_in_the_configuration():
    research = load_settings("config/research-sim.json")
    rules = {x.id: fault_rule(x) for x in (*research.points, *research.derived) if fault_rule(x)}
    t = FaultTracker(rules, 5.0)
    assert len(t.tracked()) == 304
    feedwater = rules["feedwater_temperature"]
    assert (feedwater["hysteresis_low"], feedwater["hysteresis_high"]) == (6, 1)
    assert rules["waterwall_flow"]["high"] is None and rules["waterwall_flow"]["hysteresis_high"] == 0
