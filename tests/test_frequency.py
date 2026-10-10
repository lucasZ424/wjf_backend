import pytest

from htsg.config import Settings, Source
from htsg.research_config import Simulation
from htsg.timing import every_second


@pytest.mark.parametrize("interval", [0.05, 0.2, 2, 60])
def test_input_and_simulator_frequency_cannot_depart_from_one_hertz(interval):
    with pytest.raises(ValueError):
        Source(id="s", kind="simulator", interval=interval)
    with pytest.raises(ValueError):
        Simulation(interval=interval)


def test_aggregation_and_background_refresh_are_not_configurable(settings):
    data = settings.model_dump()
    data["points"][0]["aggregation"] = "Mean"
    with pytest.raises(ValueError):
        Settings.model_validate(data)
    data["points"][0]["aggregation"] = "Direct"
    data["limits"]["maintenance_interval"] = 3
    with pytest.raises(ValueError):
        Settings.model_validate(data)


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
