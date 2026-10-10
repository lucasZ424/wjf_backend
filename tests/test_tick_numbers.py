"""D07: planned tick numbers (终版 §7, §9).

Why it matters: strain_change may only be formed from two CONSECUTIVE planned rounds, so a
skipped round must show up as a gap in the numbers, and no catch-up burst may hide it.
"""

import pytest

import htsg.timing as timing


@pytest.fixture
def fake_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(timing.time, "monotonic", lambda: clock[0])

    async def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(timing.asyncio, "sleep", sleep)
    return clock


async def take(ticks, clock, work):
    """Collect (number, clock-at-yield); `work` seconds of processing happen between ticks."""
    seen = []
    async for number in ticks:
        seen.append((number, clock[0]))
        clock[0] += work(len(seen))
        if len(seen) >= 6:
            break
    return seen


async def test_on_time_rounds_are_numbered_consecutively(fake_clock):
    seen = await take(timing.every_second(), fake_clock, lambda n: 0.25)
    assert [n for n, _ in seen] == [0, 1, 2, 3, 4, 5]
    assert [t for _, t in seen] == [0, 1, 2, 3, 4, 5]


async def test_two_skipped_rounds_leave_a_visible_gap_in_the_numbers(fake_clock):
    # after round 2 the loop is blocked for 2.25 s (rounds at t=3 and t=4 pass unserved)
    seen = await take(timing.every_second(), fake_clock, lambda n: 2.25 if n == 3 else 0.0)
    numbers = [n for n, _ in seen]
    assert numbers[:3] == [0, 1, 2]
    assert numbers[3] == 5, "rounds 3 and 4 were skipped; 5 must not pretend to follow 2"


async def test_skipped_rounds_are_not_replayed_in_a_burst(fake_clock):
    seen = await take(timing.every_second(), fake_clock, lambda n: 4.5 if n == 2 else 0.0)
    times = [t for _, t in seen]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(g >= 1 for g in gaps), "no tick may fire closer than one period after the previous"


async def test_a_round_that_merely_runs_late_inside_its_period_is_not_a_skip(fake_clock):
    seen = await take(timing.every_second(), fake_clock, lambda n: 0.9)
    assert [n for n, _ in seen] == [0, 1, 2, 3, 4, 5]


async def test_utc_stepping_back_does_not_disturb_the_monotonic_cadence(fake_clock, monkeypatch):
    def forbidden(*args):
        raise AssertionError("the tick schedule must not read the wall clock")

    monkeypatch.setattr(timing.time, "time", forbidden)
    seen = await take(timing.every_second(), fake_clock, lambda n: 0.1)
    assert [n for n, _ in seen] == [0, 1, 2, 3, 4, 5]


async def test_delayed_start_numbers_begin_at_one(fake_clock):
    ticks = timing.every_second(immediate=False)
    try:
        assert await anext(ticks) == 1 and fake_clock[0] == 1
    finally:
        await ticks.aclose()
