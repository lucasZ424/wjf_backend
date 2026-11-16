"""修复轮19: the windows of the thermal stress page and the back-fill of the 1-hour window (验收表 C, D3; 终版 §13.3, §17.3).

Real Chrome against the running application; the readings are the real ones at times the test chooses and the history
answers are written by the test (`page_clock`), so an hour of trend takes a few seconds.  Why it matters: the operator
reads this chart to see what the plant did over the last half hour; a chart that changes its window when another wall is
chosen, mixes two answers, draws a line across an archive gap, paints old history orange where the archive cannot say
whether a fault was confirmed, or piles up requests on a slow archive gives a picture of the plant that is not true - and
a page whose back-fill failing stops the live curve fails at the moment it is needed.  The browser's own memory must stay
bounded (硬规则): the cache is cut at 30 minutes, the three curves shown at an hour.

Each test names the rows of the 验收表 it stands for; the expected values are the table's.  The window is chosen in
`#trend-window`, whose options are its length in seconds (1800, 300, 3600).
"""

import re
import time

import pytest
from browser_support import sign_in
from page_clock import Backfill, Clock, flat, pixels, plot

LOAD, WALL, STRESS = "load_mw", "temp_wall_003", "stress_02"  # the wall and the bridge chosen: indexes 2 and 1
OTHER, OTHER_INDEX = "temp_wall_006", "5"
BLUE, GREEN, PURPLE, ALARM, RED = "#73bcff", "#4fd1a5", "#c79bff", "#ff9f43", "#ff777b"
PHRASE = "较早补取的部分不标故障，请到历史查询页查看"  # P3
FAILED = "更早的数据读取失败"  # R3
FORTY = (-2400, -1830, -1790, -1500, -1200, -900, -600, -300, -60, 0)  # 40 minutes of readings, 10 of them


@pytest.fixture
def t():
    """The time of the last reading of a test: a whole second, with everything before it in the past."""
    return float(round(time.time()) - 100)


def walk(k, r):
    """The values of the k-th reading: each of the three moves a little, so each reading is a new point of each."""
    return {LOAD: 600.0 + k % 3, WALL: 470.0 + 0.5 * (k % 3), STRESS: 17.0 + 0.1 * (k % 2), OTHER: 460.0 + 0.5 * (k % 2)}


def usual(points, start, end):
    """The history the back-fill gets unless a test says otherwise: each channel at one value of its own."""
    value = {LOAD: 590.0, WALL: 480.0, STRESS: 15.0, OTHER: 450.0}
    return {p: flat(start, end, value.get(p, 440.0)) for p in points}


def start(page, rig, first, values=walk, answer=usual):
    page.set_default_timeout(10000)  # a control that is not there fails in seconds, not in half a minute
    clock = Clock(page, first)
    clock.set(**values(0, None))
    fill = Backfill(page, answer)
    sign_in(page, rig)
    page.get_by_role("button", name="热应力分析").click()
    page.locator("#wall-select").select_option("2")
    page.locator("#stress-select").select_option("1")
    page.locator("#trend-latest").wait_for()
    clock.at(first)
    return clock, fill


def feed(clock, t, times, values=walk):
    for k, r in enumerate(times):
        if k:
            clock.at(t + r, **values(k, r))


def choose(page, seconds):
    page.locator("#trend-window").select_option(str(seconds))
    page.wait_for_function("s => { const p = document.querySelector('#trend-canvas').plot; return p && p.now - p.t0 === s; }", arg=seconds)


def asked_again(page, fill, before, timeout=5000):
    page.wait_for_function("n => window.__flight.replayCalls > n", arg=before, timeout=timeout)
    page.wait_for_timeout(300)


def calls(clock):
    return clock.flight()["replayCalls"]


def ids(page):
    return [p["id"] for p in plot(page)["panels"]]


def caption(page):
    """The words of the chart's card below the curves: not its heading, the latest values, the cursor or the controls."""
    return page.evaluate(
        """() => { const card = document.querySelector('#trend-canvas').closest('.card').cloneNode(true);
          card.querySelectorAll('h3, #trend-latest, #trend-readout, select, canvas').forEach(e => e.remove());
          return card.textContent.replace(/\\s+/g, ' ').trim(); }"""
    )


def without_phrase(text):
    return re.sub(r"\s+", " ", re.sub(f"[（(]?{PHRASE}[）)]?", "", text)).strip()


def own_errors(page):
    return [e for e in page.errors if not e.startswith("Failed to load resource")]


# ------------------------------------------------------------------------------------- the 30- and 5-minute windows


def test_the_page_opens_on_30_minutes_of_live_trend_asks_for_no_history_and_leaves_what_it_has_not_seen_blank(page, rig, t):
    """C1 (R7, P2): open for 10 minutes; the 20 minutes before the page opened are empty, not filled from the archive."""
    clock, fill = start(page, rig, t - 600)
    feed(clock, t, (-600, -300, -60, 0))
    got = plot(page)
    assert got["now"] - got["t0"] == 1800 and got["now"] == t, "C1: the last 30 minutes, to the latest reading"
    assert page.locator("#trend-window").input_value() == "1800", "C1: 30 minutes by default"
    assert fill.requests == [] and calls(clock) == 0, "C1: no back-fill in the 30-minute window"
    for index, color in enumerate((BLUE, GREEN, PURPLE)):
        assert pixels(page, index, t - 600, t, color) > 0, f"panel {index}: what the page saw is drawn"
        for what in (color, ALARM, RED):
            assert pixels(page, index, t - 1800, t - 610, what) == 0, f"panel {index}: blank before the page opened ({what})"
    assert own_errors(page) == []


def faulty(k, r):
    """The usual readings; the other wall had a confirmed fault from t-1200 to t-900, at 495 ℃ beyond its 490."""
    values = walk(k, r)
    if r is not None and -1200 <= r < -900:
        values[OTHER] = {"value": 495.0, "fault": "CONFIRMED", "fault_direction": "HIGH"}
    return values


THIRTY = (-1700, -1500, -1300, -1200, -1100, -1000, -900, -600, -300, -60, 0)


def test_another_wall_is_drawn_at_once_with_its_last_30_minutes_and_its_confirmed_fault_orange(page, rig, t):
    """C2 (P1): every wall is kept for 30 minutes, so the one chosen now has its curve and its orange without asking."""
    clock, fill = start(page, rig, t - 1700, values=faulty)
    feed(clock, t, THIRTY, values=faulty)
    page.locator("#wall-select").select_option(OTHER_INDEX)
    page.wait_for_function(f"document.querySelector('#trend-canvas').plot.panels[1].id === '{OTHER}'")
    assert calls(clock) == 0 and fill.requests == [], "C2: no request when a wall is chosen in the 30-minute window"
    assert pixels(page, 1, t - 1700, t - 1300, GREEN, 459.8, 460.7) > 0, "C2: its curve of 20 to 28 minutes ago"
    assert pixels(page, 1, t - 1200, t - 900, ALARM) > 5, "C2: the confirmed stretch is orange"
    assert pixels(page, 1, t - 1700, t - 1210, ALARM) == 0 and pixels(page, 1, t - 890, t, ALARM) == 0, "C2: and only it"


def test_the_5_minute_window_is_a_closer_look_at_the_same_curves_and_keeps_the_30_minutes(page, rig, t):
    """C3 (R7): no request; the last 5 minutes, the same points and colours as in the 30-minute window; back at 30
    minutes, the older part is still there."""
    clock, fill = start(page, rig, t - 1700, values=faulty)
    feed(clock, t, THIRTY, values=faulty)
    wide = plot(page)
    choose(page, 300)
    close = plot(page)
    assert close["now"] == t and close["t0"] == t - 300, "C3: the last 5 minutes"
    for a, b in zip(wide["panels"], close["panels"], strict=True):
        assert a["id"] == b["id"] and a["color"] == b["color"]
        assert [p for p in a["points"] if p["t"] >= t - 300] == [p for p in b["points"] if p["t"] >= t - 300], f"C3: {a['id']}"
    choose(page, 1800)
    assert pixels(page, 1, t - 1700, t - 1300, GREEN, 469.8, 471.2) > 0, "C3: the 30 minutes were not dropped"
    page.locator("#wall-select").select_option(OTHER_INDEX)
    page.wait_for_function(f"document.querySelector('#trend-canvas').plot.panels[1].id === '{OTHER}'")
    choose(page, 300)
    choose(page, 1800)
    assert pixels(page, 1, t - 1200, t - 900, ALARM) > 5, "C3: nor those of the wall chosen in the 5-minute window"
    assert calls(clock) == 0 and fill.requests == [], "C3: no request at all"


# ----------------------------------------------------------------------------------------------- the 1-hour window


def test_one_hour_asks_once_for_the_three_curves_shown_and_only_for_what_the_browser_has_not_seen(page, rig, t):
    """C4: 40 minutes open; one request for the load, the wall and the stress, from t-3600 to the oldest live point
    (about t-1800: the browser keeps 30 minutes)."""
    clock, fill = start(page, rig, t - 2400)
    feed(clock, t, FORTY)
    choose(page, 3600)
    asked_again(page, fill, 0)
    page.wait_for_timeout(1500)
    assert len(fill.requests) == 1 == calls(clock), f"C4: one request: {fill.requests}"
    points, since, until = fill.requests[0]
    assert sorted(points) == sorted([LOAD, WALL, STRESS]), "C4: the three curves shown, not one, not every wall"
    assert abs(since - (t - 3600)) <= 1, f"C4: from t-3600: {since - t}"
    assert t - 1835 <= until <= t - 1785, f"C4: to the oldest point the browser has, not into the live part: {until - t}"
    assert pixels(page, 1, t - 3500, t - 1900, GREEN, 479.8, 480.2) > 0, "the back-filled part is drawn"


def test_choosing_another_wall_in_the_hour_keeps_the_hour_and_asks_only_for_that_wall_drawing_what_it_has_first(page, rig, t):
    """C5, C6: the window stays; one request, for the new wall only; its last 30 minutes are drawn before the answer
    comes.  Back to 30 or 5 minutes: nothing is asked."""
    clock, fill = start(page, rig, t - 2400)
    feed(clock, t, FORTY)
    choose(page, 3600)
    asked_again(page, fill, 0)
    fill.hold = True
    page.locator("#wall-select").select_option(OTHER_INDEX)
    asked_again(page, fill, 1)
    assert page.locator("#trend-window").input_value() == "3600" and plot(page)["now"] - plot(page)["t0"] == 3600, "C5"
    assert ids(page) == [LOAD, OTHER, STRESS]
    assert [r[0] for r in fill.requests[1:]] == [[OTHER]], "C5: the new wall only; the load and the stress are not asked again"
    assert pixels(page, 1, t - 1700, t - 100, GREEN, 459.8, 460.7) > 0, "C5: drawn from the browser before the answer"
    assert fill.release(0)
    page.wait_for_function("window.__flight.replay === 0")
    page.wait_for_timeout(300)
    assert pixels(page, 1, t - 3500, t - 1900, GREEN, 449.8, 450.2) > 0, "and the answer joined on the left"
    before = calls(clock)
    choose(page, 1800)
    choose(page, 300)
    page.wait_for_timeout(1500)
    assert calls(clock) == before, "C6: going back to 30 or 5 minutes asks nothing"


def test_walls_chosen_one_after_another_leave_one_request_and_only_the_last_answer_drawn(page, rig, t):
    """C7: A, B, C chosen in turn while A and B are unanswered: never more than one request out, and the answers of A and
    B - had they come late - are not drawn in C's panel."""
    clock, fill = start(page, rig, t - 2400)
    feed(clock, t, FORTY)
    choose(page, 3600)
    asked_again(page, fill, 0)
    fill.hold = True
    for n, index in enumerate(("5", "6", "7"), start=1):
        page.locator("#wall-select").select_option(index)
        asked_again(page, fill, n)
    assert clock.flight()["replayMost"] <= 1, f"C7: requests piled up: {clock.flight()}"
    assert [r[0] for r in fill.requests[1:]] == [["temp_wall_006"], ["temp_wall_007"], ["temp_wall_008"]]
    late = {"temp_wall_006": 430.0, "temp_wall_007": 440.0, "temp_wall_008": 450.0}
    for wall in ("temp_wall_008", "temp_wall_006", "temp_wall_007"):  # C's answer first, then the late ones, if still awaited
        fill.release(wall, lambda points, s, e: {p: flat(s, e, late[p]) for p in points})
    page.wait_for_timeout(1500)
    assert ids(page) == [LOAD, "temp_wall_008", STRESS]
    assert pixels(page, 1, t - 3500, t - 1900, GREEN, 449.8, 450.2) > 0, "C7: C's history is drawn"
    for value in (430.0, 440.0):
        assert pixels(page, 1, t - 3600, t, GREEN, value - 0.2, value + 0.2) == 0, f"C7: an answer for another wall ({value})"
    assert clock.flight()["replayMost"] <= 1


def broken(points, since, until):
    """The wall's history: 478 until t-3000; 480 from t-3000 until a gap at t-2900; 481 from t-2800; 490 from t-1900,
    said to hold until t-1500 - past the oldest live point.  The others: one value each."""
    t = since + 3600
    rows = usual(points, since, until)
    if WALL in points:
        rows[WALL] = [(since, 478.0, t - 3000, "NEXT_CHANGE"), (t - 3000, 480.0, t - 2900, "GAP"),
                      (t - 2800, 481.0, t - 1900, "NEXT_CHANGE"), (t - 1900, 490.0, t - 1500, "WATERMARK")]  # fmt: skip
    return rows


def test_the_back_fill_is_drawn_as_held_values_with_nothing_across_a_gap_and_stops_at_the_live_part(page, rig, t):
    """C8, C9 (终版 §14): each value held to where it was valid until, no line through the gap and none from 480 to 481;
    the back-filled part ends where the live part begins, and is not drawn a second time over it."""
    clock, fill = start(page, rig, t - 2400, answer=broken)
    feed(clock, t, FORTY)
    choose(page, 3600)
    asked_again(page, fill, 0)
    page.wait_for_function("window.__flight.replay === 0")
    page.wait_for_timeout(300)
    assert pixels(page, 1, t - 3000, t - 2900, GREEN, 479.8, 480.2) > 0, "C8: 480 held from t-3000 to t-2900"
    for color in (GREEN, ALARM):
        assert pixels(page, 1, t - 2900, t - 2800, color) == 0, f"C8: nothing through the gap, nor a slope across it ({color})"
    assert pixels(page, 1, t - 2800, t - 1900, GREEN, 480.8, 481.2) > 0, "C8: 481 from t-2800"
    assert pixels(page, 1, t - 1900, t - 1840, GREEN, 489.8, 490.2) > 0, "C9: drawn up to the oldest live point"
    assert pixels(page, 1, t - 1780, t - 1500, GREEN, 489.8, 490.2) == 0, "C9: and not over the live part"
    assert pixels(page, 1, t - 1780, t, GREEN, 469.8, 471.2) > 0, "the live part is there"


def beyond(points, since, until):
    """C10: the wall's history beyond its limit (605 ℃, then 598 ℃; the limit of this configuration is 490 ℃)."""
    t = since + 3600
    rows = usual(points, since, until)
    if WALL in points:
        rows[WALL] = [(since, 605.0, t - 2900, "NEXT_CHANGE"), (t - 2900, 598.0, until, "WATERMARK")]
    return rows


def test_the_back_fill_is_drawn_in_the_channels_own_colour_however_far_beyond_its_limit(page, rig, t):
    """C10 (R4): the archive cannot say where a fault was confirmed, so the back-filled part is never orange."""
    clock, fill = start(page, rig, t - 2400, answer=beyond)
    feed(clock, t, FORTY)
    choose(page, 3600)
    asked_again(page, fill, 0)
    page.wait_for_function("window.__flight.replay === 0")
    page.wait_for_timeout(300)
    assert pixels(page, 1, t - 3500, t - 2900, GREEN, 604.0, 606.0) > 0, "C10: 605 in green"
    assert pixels(page, 1, t - 2900, t - 1900, GREEN, 597.0, 599.0) > 0, "C10: 598 in green"
    assert pixels(page, 1, t - 3600, t, ALARM) == 0, "C10: no orange anywhere"


def test_the_hour_says_the_back_filled_part_shows_no_fault_and_the_other_windows_do_not(page, rig, t):
    """C12 (P3)."""
    clock, fill = start(page, rig, t - 600)
    feed(clock, t, (-600, -300, 0))
    assert PHRASE not in caption(page), "C12: not in the 30-minute window"
    choose(page, 3600)
    asked_again(page, fill, 0)
    page.locator("#trend-canvas").locator("xpath=ancestor::*[contains(@class,'card')][1]").get_by_text(PHRASE).wait_for()
    choose(page, 1800)
    assert PHRASE not in caption(page), "C12: gone again at 30 minutes"
    choose(page, 300)
    assert PHRASE not in caption(page), "C12: nor at 5 minutes"


@pytest.mark.parametrize("fail", ["503", "abort"])
def test_a_back_fill_that_fails_leaves_the_live_part_going_says_so_and_a_later_one_puts_it_right(page, rig, t, fail):
    """C13 (R3): the older part stays empty and the chart says so; the live part and the readings go on; the next
    back-fill that succeeds takes the words away, and a success adds no words of its own."""
    clock, fill = start(page, rig, t - 2400)
    feed(clock, t, FORTY)
    baseline = caption(page)
    fill.fail = "abort" if fail == "abort" else True
    choose(page, 3600)
    asked_again(page, fill, 0)
    page.locator("#trend-canvas").locator("xpath=ancestor::*[contains(@class,'card')][1]").get_by_text(FAILED).wait_for()
    assert pixels(page, 1, t - 3500, t - 1900, GREEN) == 0, "C13: the older part is empty"
    assert pixels(page, 1, t - 1700, t, GREEN, 469.8, 471.2) > 0, "C13: the live part is drawn"
    clock.at(t + 1, **{WALL: 477.0})
    assert "477.0" in page.locator(f'#trend-latest [data-panel="{WALL}"]').inner_text(), "C13: the readings go on"
    fill.fail = None
    page.locator("#wall-select").select_option(OTHER_INDEX)
    asked_again(page, fill, 1)
    page.wait_for_function("window.__flight.replay === 0")
    page.wait_for_function("t => !document.querySelector('#trend-canvas').closest('.card').textContent.includes(t)", arg=FAILED)
    assert pixels(page, 1, t - 3500, t - 1900, GREEN, 449.8, 450.2) > 0, "C13: the next back-fill is drawn"
    assert without_phrase(caption(page)) == without_phrase(baseline), "C13 (R3): a success says nothing of its own"
    assert own_errors(page) == [], "C13: no error on the page"


def test_a_clock_that_goes_back_clears_the_back_fill_with_the_live_trend_and_the_hour_is_filled_again(page, rig, t):
    """C14 (R6): the old history is not left beside readings of the new time, and the hour is asked for once more."""
    clock, fill = start(page, rig, t - 2400)
    feed(clock, t, FORTY)
    choose(page, 3600)
    asked_again(page, fill, 0)
    page.wait_for_function("window.__flight.replay === 0")
    back = t - 900
    fill.answer = lambda points, s, e: {p: flat(s, e, {LOAD: 595.0, WALL: 455.0, STRESS: 16.0}[p]) for p in points}
    fill.hold = True  # the new back-fill is not answered yet: whatever is drawn now is what the page kept (用户 11-16 放开补)
    clock.at(back, **{LOAD: 603.0, WALL: 466.0, STRESS: 18.0})
    asked_again(page, fill, 1)
    page.wait_for_timeout(300)
    assert pixels(page, 1, back - 3600, back, GREEN, 479.8, 480.2) == 0, "C14: the old back-fill is cleared at once, not when the new one comes"
    fill.hold = False
    assert fill.release(0)
    page.wait_for_function("window.__flight.replay === 0")
    page.wait_for_timeout(2500)
    assert len(fill.requests) == 2, f"C14: asked again once: {fill.requests}"
    points, since, until = fill.requests[1]
    assert sorted(points) == sorted([LOAD, WALL, STRESS]) and abs(since - (back - 3600)) <= 1 and until <= back + 1, fill.requests[1]
    got = plot(page)
    assert got["now"] == back and got["now"] - got["t0"] == 3600
    assert pixels(page, 1, back - 3600, back, GREEN, 479.8, 480.2) == 0, "C14: the old back-fill is gone"
    assert pixels(page, 1, back - 880, back - 20, GREEN, 469.8, 471.2) == 0, "C14: and the old live trend"
    assert pixels(page, 1, back - 3500, back - 100, GREEN, 454.8, 455.2) > 0, "C14: the new back-fill is drawn"


def test_every_wall_is_kept_for_30_minutes_and_the_three_shown_in_the_hour_for_an_hour(page, rig, t):
    """C15 (P1): 70 minutes open, the hour chosen 40 minutes in.  The wall shown has its live curve over the whole hour;
    another wall, chosen at the end, has 30 minutes in the browser and asks for the rest."""
    times = (-4200, -3900, -3630, -3590, -3300, -3000, -2700, -2400, -2100, -1830, -1800, -1790, -1500, -1200, -900, -600, -300, 0)
    clock, fill = start(page, rig, t - 4200)
    shown = times.index(-1800)
    feed(clock, t, times[: shown + 1])
    choose(page, 3600)
    asked_again(page, fill, 0)
    points, since, until = fill.requests[0]
    assert t - 1800 - 1835 <= until <= t - 1800 - 1785, f"C4, C15: the browser had 30 minutes then: {until - t}"
    page.wait_for_function("window.__flight.replay === 0")
    for k, r in enumerate(times[shown + 1 :], start=shown + 1):
        clock.at(t + r, **walk(k, r))
    assert pixels(page, 1, t - 3500, t - 1900, GREEN, 469.8, 471.2) > 0, "C15: the wall shown kept its live hour"
    page.locator("#wall-select").select_option(OTHER_INDEX)
    asked_again(page, fill, 1)
    assert len(fill.requests) == 2 and fill.requests[1][0] == [OTHER], fill.requests
    until = fill.requests[1][2]
    assert t - 1835 <= until <= t - 1785, f"C15: a wall not shown is kept for 30 minutes, no more, no less: {until - t}"


def test_the_value_axes_keep_their_least_spans_in_every_window(page, rig, t):
    """C16 (U3): steady values; each axis spans at least, and less than twice, its least span (修复轮15: load 50 MW, a wall
    20 ℃, the thermal stress 10 MPa)."""
    steady = lambda k, r: {LOAD: 600.0, WALL: 470.0, STRESS: 17.0}  # noqa: E731
    clock, fill = start(page, rig, t - 600, values=steady,
                        answer=lambda points, s, e: {p: flat(s, e, {LOAD: 600.0, WALL: 470.0, STRESS: 17.0}[p]) for p in points})  # fmt: skip
    feed(clock, t, (-600, -300, 0), values=steady)
    for seconds in (1800, 300, 3600):
        choose(page, seconds)
        if seconds == 3600:
            asked_again(page, fill, 0)
            page.wait_for_function("window.__flight.replay === 0")
            page.wait_for_timeout(300)
        for panel, least in zip(plot(page)["panels"], (50, 20, 10), strict=True):
            assert least <= panel["hi"] - panel["lo"] < 2 * least, f"C16 at {seconds} s: {panel['id']} {panel['lo']}..{panel['hi']}"


def test_the_readings_go_on_once_a_second_one_at_a_time_while_a_back_fill_is_out(page, rig, t):
    """D3: the back-fill is not one of the readings' requests and does not hold them up."""
    clock, fill = start(page, rig, t - 600)
    feed(clock, t, (-600, -300, 0))
    fill.hold = True
    choose(page, 3600)
    asked_again(page, fill, 0)
    seen = clock.flight()["snapshotCalls"]
    for n in range(1, 5):
        clock.at(t + n, **{WALL: 471.0 + n})
    assert clock.flight()["replay"] == 1, "D3: the back-fill is still out"
    assert clock.flight()["snapshotCalls"] - seen >= 4, "D3: and the readings came meanwhile"
    assert clock.flight()["snapshotMost"] <= 1, "D3: one reading at a time, as before"
    assert "475.0" in page.locator(f'#trend-latest [data-panel="{WALL}"]').inner_text()
    fill.release(0)
