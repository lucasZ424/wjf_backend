import copy

import pytest
from owner_support import small_config

from htsg.config import Settings, Source
from htsg.research_config import Simulation
from htsg.timing import every_second


@pytest.mark.parametrize("interval", [0.05, 0.2, 2, 60])
def test_input_and_simulator_frequency_cannot_depart_from_one_hertz(interval):
    with pytest.raises(ValueError):
        Source(id="s", kind="modbus", interval=interval)
    with pytest.raises(ValueError):
        Simulation(interval=interval)


def test_aggregation_and_background_refresh_are_not_configurable():
    """终版 §18.1: the settings of the frame and capture-window mechanisms are gone and are not accepted again."""
    data = small_config().model_dump()
    assert Settings.model_validate(data)
    for where, key, value in (
        (lambda d: d["points"][0], "aggregation", "Mean"),
        (lambda d: d["points"][0], "stale_after", 5),
        (lambda d: d["derived"][0], "max_age", 2),
        (lambda d: d["limits"], "maintenance_interval", 3),
        (lambda d: d["limits"], "capture_budget_bytes", 1024),
        (lambda d: d["sources"][0], "timeout", 3),
        (lambda d: d, "profiles", []),
    ):
        edited = copy.deepcopy(data)
        where(edited)[key] = value
        with pytest.raises(ValueError, match=key):
            Settings.model_validate(edited)


async def test_one_second_schedule_excludes_work_time_and_skips_overruns(monkeypatch):
    import htsg.timing as timing

    clock = [0.0]
    monkeypatch.setattr(timing.time, "monotonic", lambda: clock[0])

    async def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(timing.asyncio, "sleep", sleep)
    ticks = every_second()
    try:
        for expected in (0, 1, 2):
            await anext(ticks)
            assert clock[0] == expected
            clock[0] += 0.25  # Reading channels must not lengthen the next period.
        clock[0] += 2
        await anext(ticks)
        assert clock[0] == 5.25, "late work must not trigger a burst of replacement samples"
    finally:
        await ticks.aclose()


async def test_generator_waits_one_second_after_initial_sample(monkeypatch):
    import htsg.timing as timing

    delays = []
    monkeypatch.setattr(timing.time, "monotonic", lambda: 0)

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(timing.asyncio, "sleep", sleep)
    ticks = every_second(immediate=False)
    try:
        await anext(ticks)
        assert delays == [1], "start() already generated the initial simulator sample"
    finally:
        await ticks.aclose()
