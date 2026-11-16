"""修复轮19: the strain change as the difference to 5 minutes ago, and the decimals (验收表 A, B; 终版 §17.1, §17.6, §17.7).

Real Chrome against the running application; the readings are the real ones at times the test chooses (`page_clock`).
Why it matters: a 1-second difference of a slow strain is always about zero, so the column told the operator nothing
(用户 11-10, 11-16).  The change must be the two ends only - now and the moment 5 minutes ago - from what the browser
already saw, worked out on the values as received and only then rounded; where either end has no valid value it is a
bare dash, never a number made up from another moment.  A rounding that leaves a minus sign on zero reads as a change of
direction that never happened.  Each test names the row of the 验收表 it stands for; the expected texts are the table's.
"""

import re
import time

import pytest
from browser_support import sign_in
from live_support import BRIDGES
from page_clock import Backfill, Clock, flat, plot
from test_web_export import export, query

S01 = "strain_01"  # station S01, the first row of the strain table
STRESS = "stress_02"
CHANGE = 3  # the fourth column: 应变变化


@pytest.fixture
def base():
    """A whole second a little in the past: every reading of a test is at base + a few minutes or less."""
    return float(round(time.time()) - 100)


def strain_page(page, rig, clock, name="水冷壁应变场"):
    sign_in(page, rig)
    if name != "系统总览":
        page.get_by_role("button", name=name).click()
    page.locator("#strain-table tbody tr").first.wait_for()
    clock.at(clock.t)


def row(page, n=0):
    return [c.strip() for c in page.locator("#strain-table tbody tr").nth(n).locator("td").all_inner_texts()]


def change(page):
    return row(page)[CHANGE]


def block_text(page, station="S01"):
    return page.locator(f'#strain-map .block[data-station="{station}"]').inner_text()


def history_asked(page):
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/api/replay" in r.url or "/api/faults" in r.url else None)
    return asked


# ---------------------------------------------------------------------------------- A: the change against 5 minutes ago


def test_the_change_is_now_minus_the_value_of_5_minutes_ago_rounded_to_one_decimal(page, rig, base):
    """A1, A9, A10: 15.00 - 12.34 = 2.66, shown 2.7 με, in the table and under the block, and nothing asked of the
    history.  The readings before (11.00 at t-400) and after (14.00 at t-60) the moment 5 minutes ago are there so that
    a change against the oldest point or against another moment is another number."""
    t = base + 400
    asked = history_asked(page)
    clock = Clock(page, t - 400)
    clock.set(**{S01: 11.00})
    strain_page(page, rig, clock)
    clock.at(t - 300, **{S01: 12.34})
    clock.at(t - 60, **{S01: 14.00})
    clock.at(t, **{S01: 15.00})
    assert change(page) == "2.7 με", "A1: not the 1-second difference, not against t-400 (4.0) or t-60 (1.0)"
    assert row(page)[2] == "15.0 με"
    assert "应变变化 2.7 με" in block_text(page), "A9: the block shows the same 5-minute change"
    page.get_by_role("button", name="系统总览").click()
    page.locator("#strain-table tbody tr").first.wait_for()
    clock.at(t)
    assert change(page) == "2.7 με", "the overview's strain table is the same table"
    assert asked == [], "A10: worked out in the browser, the history is never asked"


def test_the_value_5_minutes_ago_is_the_one_in_effect_then_not_a_point_at_that_very_second(page, rig, base):
    """A2: 10.00 was received at t-400 and did not change, so it was in effect at t-300: 13.00 - 10.00."""
    t = base + 400
    clock = Clock(page, t - 400)
    clock.set(**{S01: 10.00})
    strain_page(page, rig, clock)
    clock.at(t - 200)  # the same value read again: no new point
    clock.at(t, **{S01: 13.00})
    assert change(page) == "3.0 με"


def test_a_page_open_for_less_than_5_minutes_shows_a_bare_dash(page, rig, base):
    """A3: 200 s of readings: there is no value of 5 minutes ago, and the oldest one does not stand in for it."""
    t = base + 200
    clock = Clock(page, t - 200)
    clock.set(**{S01: 10.00})
    strain_page(page, rig, clock)
    clock.at(t - 100, **{S01: 11.00})
    clock.at(t, **{S01: 12.00})
    assert change(page) == "—", "A3: a dash, not 2.0 (against the oldest point) or 0.0, and no words beside it"
    assert row(page)[2] == "12.0 με", "the strain itself is shown"


@pytest.mark.parametrize("how", ["invalid", "new-run"])
def test_a_moment_5_minutes_ago_without_a_valid_value_shows_a_dash(page, rig, base, how):
    """A4: at t-300 the strain was invalid (or the readings were cut by a backend restart); the valid values before
    (10.00) and after (12.00) it do not stand in for it."""
    t = base + 400
    clock = Clock(page, t - 400)
    clock.set(**{S01: 10.00})
    strain_page(page, rig, clock)
    if how == "invalid":
        clock.at(t - 320, **{S01: None})
        clock.at(t - 250, **{S01: 12.00})
    else:
        clock.at(t - 310)
        clock.run = "restarted-" + str(t)  # a new run: the live trend is cut where the old one ended (终版 §13.3)
        clock.at(t - 250, **{S01: 12.00})
    clock.at(t, **{S01: 13.00})
    assert change(page) == "—", "A4: not 3.0 (the value before) nor 1.0 (the value after)"


def test_an_invalid_value_now_shows_a_dash(page, rig, base):
    """A5: the change is not worked out from the last valid value."""
    t = base + 400
    clock = Clock(page, t - 400)
    clock.set(**{S01: 10.00})
    strain_page(page, rig, clock)
    clock.at(t - 60, **{S01: 12.00})
    clock.at(t, **{S01: None})
    assert row(page)[2] == "—"
    assert change(page) == "—", "A5: not 2.0"


def test_only_the_two_ends_count_whatever_happened_between_them(page, rig, base):
    """A6 (R2): 5.00 at t-300, invalid from t-200, 6.00 again from t-150, 8.00 now: 8.00 - 5.00."""
    t = base + 300
    clock = Clock(page, t - 300)
    clock.set(**{S01: 5.00})
    strain_page(page, rig, clock)
    clock.at(t - 200, **{S01: None})
    clock.at(t - 150, **{S01: 6.00})
    clock.at(t, **{S01: 8.00})
    assert change(page) == "3.0 με", "A6: not a dash for the break, not 2.0 from the first point after it"


def test_a_change_that_rounds_to_zero_has_no_minus_sign(page, rig, base):
    """A7: 4.96 - 5.00 = -0.04."""
    t = base + 300
    clock = Clock(page, t - 300)
    clock.set(**{S01: 5.00})
    strain_page(page, rig, clock)
    clock.at(t, **{S01: 4.96})
    assert change(page) == "0.0 με", "A7: not -0.0 με"


def test_the_column_says_it_is_the_difference_to_5_minutes_ago(page, rig, base):
    """A8."""
    clock = Clock(page, base)
    strain_page(page, rig, clock)
    head = [h.replace(" ", "") for h in page.locator("#strain-table thead th").all_inner_texts()]
    assert "应变变化" in head[CHANGE] and "与5分钟前之差" in head[CHANGE], head
    assert "与上一轮之差" not in "".join(head)


def test_the_change_is_worked_out_on_the_values_received_and_rounded_after(page, rig, base):
    """A11 (R8): 15.04 - 12.36 = 2.68, shown 2.7; the rounded values 15.0 - 12.4 would give 2.6."""
    t = base + 300
    clock = Clock(page, t - 300)
    clock.set(**{S01: 12.36})
    strain_page(page, rig, clock)
    clock.at(t, **{S01: 15.04})
    assert change(page) == "2.7 με"


def test_a_value_received_28_minutes_ago_and_still_in_effect_is_the_value_of_5_minutes_ago(page, rig, base):
    """A12 (P1): the browser keeps 30 minutes, so 6.00 received at t-1700 is still there: 9.00 - 6.00."""
    t = base + 1700
    clock = Clock(page, t - 1700)
    clock.set(**{S01: 6.00})
    strain_page(page, rig, clock)
    for r in (-1200, -700, -400):
        clock.at(t + r)  # read again and again, never changed
    clock.at(t, **{S01: 9.00})
    assert change(page) == "3.0 με", "A12: not a dash"


# --------------------------------------------------------------------------------------------------- B: decimals


def test_a_strain_has_one_decimal_and_the_bridge_signal_keeps_four(page, rig, base):
    """B1, B2, B4 (终版 §17.6)."""
    clock = Clock(page, base)
    clock.set(**{S01: 7.7333, "strain_02": -10.4167, BRIDGES[2]: -0.0625})
    strain_page(page, rig, clock)
    assert row(page, 0)[2] == "7.7 με", "B1"
    assert row(page, 1)[2] == "-10.4 με", "B2"
    assert row(page, 2)[1] == "-0.0625 mV/V", "B4: the other units are as they were"


def stress_text(page):
    return page.locator(f'#trend-latest [data-panel="{STRESS}"]').inner_text()


def trend_page(page, rig, clock):
    sign_in(page, rig)
    page.get_by_role("button", name="热应力分析").click()
    page.locator("#stress-select").select_option("1")
    page.locator("#trend-latest").wait_for()
    clock.at(clock.t)


def cursor_stress(page, at):
    """What the cursor says of the stress at the instant `at`."""
    got = plot(page)
    box = page.locator("#trend-canvas").bounding_box()
    x = got["left"] + (at - got["t0"]) / (got["now"] - got["t0"]) * (got["right"] - got["left"])
    page.mouse.move(box["x"] + x, box["y"] + 100)
    page.locator("#trend-readout").get_by_text("游标").wait_for()
    text = page.locator("#trend-readout").inner_text()
    page.mouse.move(box["x"] + 2, box["y"] + 2)
    return next(part for part in re.split("[｜|]", text) if "热应力" in part).strip()


def test_a_stress_that_rounds_to_zero_has_neither_sign_and_a_positive_one_keeps_its_plus(page, rig, base):
    """B3, B5 (R5): -0.004 and 0.004 MPa are 0.00 MPa, in the latest value and under the cursor; 0.006 is +0.01."""
    t = base + 300
    clock = Clock(page, t - 300)
    clock.set(**{STRESS: -0.004})
    trend_page(page, rig, clock)
    assert "0.00 MPa" in stress_text(page) and "-0.00" not in stress_text(page), f"B3: {stress_text(page)}"
    clock.at(t - 200, **{STRESS: 0.004})
    assert "0.00 MPa" in stress_text(page) and "+0.00" not in stress_text(page), f"B5: {stress_text(page)}"
    clock.at(t - 100, **{STRESS: 0.006})
    assert "+0.01 MPa" in stress_text(page), f"B5: {stress_text(page)}"
    clock.at(t, **{STRESS: 0.004})
    for at, value in ((t - 250, -0.004), (t - 150, 0.004)):
        said = cursor_stress(page, at)
        assert said.endswith("0.00 MPa") and not re.search(r"[+-]0\.00", said), f"B3, B5: the cursor at {value}: {said}"
    said = cursor_stress(page, t - 50)
    assert said.endswith("+0.01 MPa"), f"B5: the cursor at 0.006: {said}"
    assert [e for e in page.errors if not e.startswith("Failed to load resource")] == []


def test_the_csv_keeps_the_strain_as_received(page, rig, tmp_path, base):
    """B6 (R8, 终版 §17.6): the page rounds, the exported file does not."""
    Backfill(page, lambda points, start, end: {p: flat(start, end, 7.7333) for p in points})
    sign_in(page, rig)
    page.get_by_role("button", name="历史查询").click()
    page.locator("#replay-settings").wait_for()
    query(page, points=(S01,))
    _, (_, header, rows) = export(page, tmp_path)
    col = {n: header.index(n) for n in header}
    assert [r[col["值"]] for r in rows] == ["7.7333"], "B6"
