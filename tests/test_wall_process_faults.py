"""The limits of the new point table on the real owner (验收表 C, D; 终版 §8, 默认越限阈值与回差 §2, 默认极值阈值 §1).

Why it matters: a fault record is the reference point of a replay, and these are the numbers an operator is told about.  The rules
were tested long ago on made-up channels; here the shipped configuration itself goes through them, because what changed in this
round is exactly which channel carries which number: 215 walls without a temperature difference, a flow that changed its name.
The values are the ones in the two tables, with their equal-to-the-limit neighbours.
"""

import pytest
from fault_support import Accepting, faults_of
from live_support import BRIDGES, FULL, NOMINAL
from owner_support import FixedBucket, notify, poll

from htsg.owner import Owner

WALL0, MONO0 = 1000.0, 100.0
WALL = "temp_wall_100"
OTHER = "temp_wall_101"


class Unit:
    """The owner of the shipped configuration on a fake clock: every channel at its normal reading, one second at a time."""

    def __init__(self):
        self.sink = Accepting()
        self.owner = Owner(FULL, WALL0, MONO0, commit=self.sink, buckets=FixedBucket(), run_id="u")
        self.sink.owner = self.owner
        for source in ("mb_field", "ua_field"):
            self.owner.begin_generation(source, 1, WALL0, MONO0)
        self.seq = 0
        self.second(101.0, NOMINAL.items())

    def second(self, mono, items=()):
        """One second: what OPC UA sent (nothing is a keep-alive), a Modbus round, the tick of the clock."""
        wall = WALL0 + mono - MONO0
        self.seq += 1
        self.owner.accept(notify(list(items), source="ua_field", wall=wall, mono=mono, seq=self.seq))
        self.owner.accept(poll(self.seq, {b: 0.6 for b in BRIDGES}, source="mb_field", wall=wall, mono=mono))
        self.owner.tick(wall, mono)

    def hold(self, first, last):
        for t in range(first, last + 1):
            self.second(float(t))

    def status(self, name):
        return self.owner.tracker.status(name)

    def direction(self, name):
        return self.owner.tracker.confirmed_direction(name)

    def faults(self):
        return faults_of(self.sink)


def confirmed(point, value):
    unit = Unit()
    unit.second(110.0, [(point, value)])
    unit.hold(111, 115)
    assert unit.status(point) == "CONFIRMED"
    return unit


def test_a_wall_exactly_at_490_is_no_fault_however_long():
    """C1: strictly beyond the limit (默认越限阈值与回差 §3.1)."""
    unit = Unit()
    unit.second(110.0, [(WALL, 490.0)])
    unit.hold(111, 125)
    assert unit.status(WALL) == "NORMAL" and unit.faults() == []


def test_a_wall_at_490_1_is_pending_then_confirmed_high_after_five_seconds():
    """C2: 终版 §8.  Not a value that keeps changing: one sample, and the clock does the rest."""
    unit = Unit()
    unit.second(110.0, [(WALL, 490.1)])
    unit.hold(111, 114)
    assert unit.status(WALL) == "PENDING" and unit.faults() == [], "4 s of the 5 s have passed"
    unit.second(115.0)
    assert unit.status(WALL) == "CONFIRMED" and unit.direction(WALL) == "HIGH"
    (fault,) = unit.faults()
    assert fault.point_id == WALL and fault.direction == "HIGH" and fault.basis_value == 490.1
    assert fault.rule == {"low": None, "high": 490.0, "hysteresis_low": 0.0, "hysteresis_high": 3.0}, "Q7: the limit and its hysteresis"
    assert unit.status(OTHER) == "NORMAL", "and the wall beside it knows nothing of it"


def test_a_confirmed_wall_stays_a_fault_at_487_1_and_recovers_at_487():
    """C3, C4, Q7: the way back is 490 - 3, inclusive."""
    unit = confirmed(WALL, 495.0)
    unit.second(116.0, [(WALL, 487.1)])
    assert unit.direction(WALL) == "HIGH", "inside the limit, not yet through the hysteresis"
    unit.second(117.0, [(WALL, 487.0)])
    assert unit.direction(WALL) is None and unit.status(WALL) == "NORMAL"
    assert len(unit.faults()) == 1, "a recovery is a state, not a second record"


def test_a_wall_at_800_is_not_extreme_and_at_800_1_it_is_extreme_and_raises_no_record():
    """C5, 默认极值阈值 §2: bad data is not a process fault.  800.0 is an ordinary value beyond the 490 limit, which is its own matter."""
    unit = Unit()
    unit.second(110.0, [(WALL, 800.0), (OTHER, 800.1)])
    views = unit.owner.channel_views()
    assert not views[WALL]["extreme"] and views[WALL]["valid"]
    assert views[OTHER]["extreme"] and not views[OTHER]["valid"] and views[OTHER]["value"] == 800.1, "the raw value is kept"
    unit.hold(111, 125)
    assert unit.status(OTHER) == "NORMAL"
    assert [f.point_id for f in unit.faults()] == [WALL]


def test_a_change_of_a_wall_gives_no_result_of_any_kind():
    """C6 (用户 11-05): no difference, no stress; the batch of a wall carries only its readings."""
    assert not [d.id for d in FULL.derived if any(i.startswith("temp_wall_") for i in d.inputs)]
    unit = Unit()
    before = len(unit.sink.groups)
    unit.owner.accept(notify([(WALL, 470.0), (OTHER, 471.0)], source="ua_field", wall=1011.0, mono=111.0, seq=99))
    (group,) = unit.sink.groups[before:]
    assert {o.point_id for o in group.observations} == {WALL, OTHER}
    assert len(group.results) == 0


def test_a_normal_value_inside_a_batch_starts_the_clock_of_a_wall_over_from_that_batch():
    """C7, 终版 §8.2: every valid normal value cancels the clock, in batch order, even with a value beyond the limit after it."""
    unit = Unit()
    unit.second(110.0, [(WALL, 495.0)])
    unit.second(111.0)
    unit.second(113.0, [(WALL, 480.0), (WALL, 495.0)])
    unit.hold(114, 115)
    unit.second(117.0)
    assert unit.status(WALL) == "PENDING" and unit.faults() == [], "the clock of 110 would have been due at 115"
    unit.second(118.0)
    assert unit.status(WALL) == "CONFIRMED", "five seconds from the batch"


def test_the_process_quantities_are_these_seven_in_this_order():
    """D1 (用户 11-05): no main steam pressure, no waterwall flow."""
    assert list(FULL.ui.process_points) == [
        "load_mw", "feedwater_flow", "main_steam_flow", "feedwater_temperature", "reheat_temperature",
        "working_fluid_temperature", "furnace_pressure",
    ]  # fmt: skip


def test_feedwater_flow_at_925_is_no_fault_and_below_it_is_confirmed_low():
    """D2, Q7: the limit the waterwall flow had, under its new name."""
    unit = Unit()
    unit.second(110.0, [("feedwater_flow", 925.0)])
    unit.hold(111, 120)
    assert unit.status("feedwater_flow") == "NORMAL"
    unit = confirmed("feedwater_flow", 924.9)
    assert unit.direction("feedwater_flow") == "LOW"
    assert unit.faults()[0].rule == {"low": 925.0, "high": None, "hysteresis_low": 90.0, "hysteresis_high": 0.0}


def test_a_confirmed_low_feedwater_flow_stays_at_1014_9_and_recovers_at_1015():
    """D3, Q7: a low limit recovers upwards, 925 + 90.  With the hysteresis the wrong way round it would never have left."""
    unit = confirmed("feedwater_flow", 900.0)
    unit.second(116.0, [("feedwater_flow", 1014.9)])
    assert unit.direction("feedwater_flow") == "LOW"
    unit.second(117.0, [("feedwater_flow", 1015.0)])
    assert unit.direction("feedwater_flow") is None
    assert len(unit.faults()) == 1


def test_main_steam_flow_at_2223_is_no_fault_and_above_it_is_confirmed_high():
    """D4, 默认越限阈值与回差 §2."""
    unit = Unit()
    unit.second(110.0, [("main_steam_flow", 2223.0)])
    unit.hold(111, 120)
    assert unit.status("main_steam_flow") == "NORMAL"
    unit = confirmed("main_steam_flow", 2223.1)
    assert unit.direction("main_steam_flow") == "HIGH"


@pytest.mark.parametrize(
    ("point", "beyond", "direction"),
    [
        ("load_mw", 693.1, "HIGH"), ("feedwater_temperature", 320.1, "HIGH"), ("feedwater_temperature", 249.9, "LOW"),
        ("reheat_temperature", 630.1, "HIGH"), ("working_fluid_temperature", 460.1, "HIGH"), ("furnace_pressure", 0.51, "HIGH"),
        ("furnace_pressure", -0.51, "LOW"),
    ],
)  # fmt: skip
def test_the_other_process_limits_came_along_with_the_new_table(point, beyond, direction):
    """默认越限阈值与回差 §2: the same numbers as before; a limit lost in the renumbering would say nothing at all."""
    assert confirmed(point, beyond).direction(point) == direction
