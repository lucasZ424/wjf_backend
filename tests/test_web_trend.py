"""D43: page 4 - the live three-panel trend (终版 §17.3).

Real Chrome against the running application.  Why it matters: three curves on one time axis invite the reading
"these were sampled together" and "this one caused that one"; the page must show them side by side without
normalising, mixing sources or joining across what is not known, and must say that it is an estimate.
"""

import json
import time

import pytest
from browser_support import sign_in
from web_rig import UA, nominal

PAIR = 2  # the third pair of the configuration
READING = "**/api/snapshot"


@pytest.fixture
def pair(rig):
    deltas = [d for d in rig.config.derived if d.operation == "difference"]
    stress = {tuple(d.inputs): d.id for d in rig.config.derived if d.operation == "thermal_stress"}
    chosen = deltas[PAIR]
    yield {"delta": chosen.id, "stress": stress[tuple(chosen.inputs)], "inner": chosen.inputs[0], "outer": chosen.inputs[1], "index": PAIR}
    rig.call(rig.ua, [(chosen.inputs[0], nominal()[chosen.inputs[0]]), (chosen.inputs[1], nominal()[chosen.inputs[1]])])


def open_trend(page, rig, index=PAIR):
    sign_in(page, rig)
    page.get_by_role("button", name="热应力分析").click()
    page.locator("#pair-select").select_option(str(index))
    page.locator("#trend-latest").wait_for()


def plot(page):
    return page.evaluate("() => { const p = document.querySelector('#trend-canvas').plot; return p ? JSON.parse(JSON.stringify(p)) : null; }")


def drive(rig, pair, inner, outer, pause=2.1):  # longer than one reading interval: every value is read
    rig.call(rig.ua, [(pair["inner"], inner), (pair["outer"], outer)])
    time.sleep(pause)


def line_and_red(page, panel, t0, t1, color=None):
    """Pixels of the panel's line - in the panel's own colour (修复轮15), or in `color` - and of the red 'no value' marks
    between two instants."""
    return page.evaluate(
        """([panel, t0, t1, color]) => {
          const c = document.querySelector('#trend-canvas'), p = c.plot, g = c.getContext('2d'), r = c.width / c.clientWidth;
          const x = t => p.left + (Math.min(Math.max(t, p.t0), p.now) - p.t0) / (p.now - p.t0) * (p.right - p.left);
          const a = Math.round((x(t0) + 3) * r), b = Math.round((x(t1) - 3) * r), q = p.panels[panel];
          if (b <= a) return {line: -1, red: -1};
          const [lr, lg, lb] = [1, 3, 5].map(k => parseInt((color || q.color).slice(k, k + 2), 16));
          const d = g.getImageData(a, Math.round(q.top * r), b - a, Math.round((q.bottom - q.top) * r)).data;
          const out = {line: 0, red: 0};
          for (let i = 0; i < d.length; i += 4) {
            const [R, G, B, A] = [d[i], d[i + 1], d[i + 2], d[i + 3]];
            if (A > 200 && Math.abs(R - lr) < 40 && Math.abs(G - lg) < 40 && Math.abs(B - lb) < 40) out.line++;
            else if (A > 200 && R > 220 && G < 150) out.red++;
          }
          return out;
        }""",
        [panel, t0, t1, color],
    )


def test_three_panels_share_one_time_axis_and_keep_their_own_units_axes_and_signs(page, rig, pair):
    open_trend(page, rig)
    for inner, outer in ((500.0, 450.0), (460.0, 450.0), (470.0, 490.0)):  # delta +50, +10, -20
        drive(rig, pair, inner, outer)
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('-20')")
    got = plot(page)
    assert [p["id"] for p in got["panels"]] == ["load_mw", pair["delta"], pair["stress"]]
    assert [p["unit"] for p in got["panels"]] == ["MW", "℃", "MPa"], "one unit per panel, never mixed"
    assert got["now"] - got["t0"] == 300, "one time axis for all three"
    # Each panel's axis fits its own values (修复轮15: at least its least span, snapped to even steps).  Two of them may
    # come out the same numbers - the difference and its proxy here - but a normalised or shared axis covers either too
    # little of a panel's values or far more than them.
    for panel, least in zip(got["panels"], (50, 10, 10)):
        values = [q["v"] for q in panel["points"] if q["valid"]]
        reach = max(values) - min(values) + least
        assert min(values) - reach <= panel["lo"] <= min(values) and max(values) <= panel["hi"] <= max(values) + reach, panel["id"]
        assert panel["hi"] - panel["lo"] >= least, "a steady reading is not stretched over the whole panel"
    colors = [p["color"] for p in got["panels"]]
    assert len(set(colors)) == 3, f"load, difference and stress each in a colour of their own (修复轮15): {colors}"
    drawn = [line_and_red(page, i, got["t0"], got["now"])["line"] for i in range(3)]
    assert all(n > 0 for n in drawn), f"every panel's line is drawn in that panel's colour: {drawn}"
    delta = [p["v"] for p in got["panels"][1]["points"] if p["valid"]]
    assert delta[-3:] == [50.0, 10.0, -20.0], "values and signs exactly as received"
    stress = [p["v"] for p in got["panels"][2]["points"] if p["valid"]]
    assert [v > 0 for v in stress[-3:]] == [True, True, False] and stress[-1] < 0, "the sign of the proxy is kept"
    latest = page.locator("#trend-latest").inner_text()
    assert "-20" in latest and "℃" in latest and "MPa" in latest and "MW" in latest
    assert "估算值" in latest and "不是实测" in page.locator("#workspace").inner_text()
    assert page.errors == []


def test_the_difference_and_the_stress_always_come_from_the_chosen_pair(page, rig, pair):
    for d in rig.config.derived:  # in the configuration itself: one pair of inputs behind both
        if d.id in (pair["delta"], pair["stress"]):
            assert list(d.inputs) == [pair["inner"], pair["outer"]]
    open_trend(page, rig)
    drive(rig, pair, 480.0, 440.0)
    assert f"内壁 {pair['inner']}" in page.locator("#pair-inputs").inner_text() and f"外壁 {pair['outer']}" in page.locator("#pair-inputs").inner_text()
    assert [p["id"] for p in plot(page)["panels"]][1:] == [pair["delta"], pair["stress"]]
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('+40')")
    other = [d for d in rig.config.derived if d.operation == "difference"][5]
    rig.call(rig.ua, [(other.inputs[0], 490.0), (other.inputs[1], 485.0)])
    page.locator("#pair-select").select_option("5")
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('+5')")
    assert [p["id"] for p in plot(page)["panels"]][1] == other.id
    assert "+40" not in page.locator("#trend-latest").inner_text(), "the other pair's difference is not shown"
    rig.call(rig.ua, [(other.inputs[0], nominal()[other.inputs[0]]), (other.inputs[1], nominal()[other.inputs[1]])])


def test_an_invalid_period_and_a_lost_connection_are_not_joined_by_a_line(page, rig, pair):
    open_trend(page, rig)
    drive(rig, pair, 480.0, 440.0)
    gen = rig.owner.sources[UA].conn_gen
    rig.call(rig.owner.source_down, UA, gen, "TEST", time.time(), time.monotonic())  # values invalid: source starts over
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('重新同步中')")
    time.sleep(5)
    def start_over():
        rig.owner.begin_generation(UA, gen + 1, time.time(), time.monotonic())
        rig.ua(list(nominal().items()))
    rig.call(start_over)
    page.wait_for_function("!document.querySelector('#trend-latest').textContent.includes('重新同步中')")
    recovered = time.time()
    time.sleep(2.5)  # the page then hears from the server at least once more: the cut-off marker carries THAT wall time
    got = plot(page)
    for index in range(3):
        points = got["panels"][index]["points"]
        gap = next((i for i, p in enumerate(points) if not p["valid"]), None)
        assert gap is not None and points[gap + 1]["valid"], f"panel {index} records the invalid period"
        inside = line_and_red(page, index, points[gap]["t"], points[gap + 1]["t"])
        assert inside["line"] == 0 and inside["red"] > 0, f"panel {index}: nothing drawn through it, a mark instead: {inside}"
    # the readings themselves: what happened while the page could not read is unknown.  The readings fail for a
    # while (修复轮05: in the browser, the backend is untouched) so that the unknown stretch is wide enough to look at.
    page.route(READING, lambda route: route.abort())
    try:
        page.wait_for_function("document.querySelector('#connection').textContent === '数据中断'", timeout=8000)
        page.wait_for_timeout(1500)
    finally:
        page.unroute(READING)
    page.wait_for_function("document.querySelector('#connection').textContent === '实时连接'", timeout=10000)
    page.wait_for_timeout(1200)
    for index in range(3):
        points = plot(page)["panels"][index]["points"]
        cut = [i for i, p in enumerate(points) if not p["valid"]][-1]
        assert points[cut]["t"] > recovered, "the mark is the cut-off's own, not the earlier invalid period's"
        assert points[cut + 1]["valid"] and points[cut + 1]["t"] - points[cut]["t"] > 4, "the unknown stretch is real"
        inside = line_and_red(page, index, points[cut]["t"], points[cut + 1]["t"])
        assert inside["line"] == 0 and inside["red"] > 0, f"panel {index}: no line across the cut-off: {inside}"
    assert [e for e in page.errors if not e.startswith("Failed to load resource")] == [], "only the injected failures"


def test_the_cursor_reads_the_value_in_effect_in_every_panel_at_one_instant(page, rig, pair):
    open_trend(page, rig)
    drive(rig, pair, 500.0, 450.0, pause=2.0)
    drive(rig, pair, 460.0, 450.0, pause=3.5)  # +10 has been in effect for 3.5 s: a cursor just before "now" is inside it
    got = plot(page)
    box = page.locator("#trend-canvas").bounding_box()
    offset = got["right"] - 5
    page.mouse.move(box["x"] + offset, box["y"] + 100)
    readout = page.locator("#trend-readout")
    readout.get_by_text("游标").wait_for()
    text = readout.inner_text()
    after = plot(page)
    t = after["t0"] + (offset - after["left"]) / (after["right"] - after["left"]) * 300
    in_effect = [p for p in after["panels"][1]["points"] if p["t"] <= t][-1]
    assert in_effect["v"] == 10.0 and "内外壁温差 +10" in text, (text, in_effect)
    assert "不代表同步采样" in text and "负荷" in text and "内外壁温差" in text and "热应力（估算）" in text
    page.mouse.move(box["x"] + 5, box["y"] + 5)  # outside the plot area: no cursor
    page.wait_for_function("!document.querySelector('#trend-readout').textContent.includes('游标')")


def rewrite_readings(page, change):
    """Every reading the page gets passes through `change(body)` first; the times the readings were taken are returned."""
    taken = []

    def through(route):
        response = route.fetch()
        body = response.json()
        change(body)
        taken.append(body["taken_time"])
        route.fulfill(response=response, body=json.dumps(body))

    page.route(READING, through)
    return taken


def delta_points(page):
    return plot(page)["panels"][1]["points"]


def test_each_reading_is_recorded_at_the_time_it_was_taken_not_at_the_observation_time(page, rig, pair):
    """修复轮05 (终版 §13.3): the live trend is what the page saw and when; it does not rebuild when things happened."""
    open_trend(page, rig)
    taken = rewrite_readings(page, lambda body: None)
    observed = time.time()
    rig.call(rig.ua, [(pair["inner"], 491.0), (pair["outer"], 450.0)])  # +41
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('+41')")
    point = next(p for p in delta_points(page) if p["valid"] and p["v"] == 41.0)
    assert point["t"] in taken, "drawn at the time of the reading that showed it"
    assert point["t"] > observed, "not drawn back to the observation"


def test_a_value_that_stays_the_same_is_one_point_however_many_readings_show_it(page, rig, pair):
    open_trend(page, rig)
    taken = rewrite_readings(page, lambda body: None)
    rig.call(rig.ua, [(pair["inner"], 493.0), (pair["outer"], 450.0)])  # +43
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('+43')")
    count, seen = len(delta_points(page)), len(taken)
    page.wait_for_timeout(3500)
    assert len(taken) - seen >= 3, "several readings showed it"
    assert len(delta_points(page)) == count, "... and it is still one point"


def test_a_new_run_starts_a_new_stretch_even_with_the_same_value(page, rig, pair):
    open_trend(page, rig)
    rig.call(rig.ua, [(pair["inner"], 497.0), (pair["outer"], 450.0)])  # +47
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('+47')")
    rewrite_readings(page, lambda body: body.update(run_id="another-run"))  # the backend restarted
    page.wait_for_timeout(2500)
    points = delta_points(page)
    last = max(i for i, p in enumerate(points) if p["valid"] and p["v"] == 47.0)
    assert last > 0 and not points[last - 1]["valid"], "a new run is not joined to the old one by a line"
    assert any(p["valid"] and p["v"] == 47.0 for p in points[: last - 1]), "the same value before the restart"


def test_a_reading_time_that_goes_back_starts_the_trend_over(page, rig, pair):
    open_trend(page, rig)
    rig.call(rig.ua, [(pair["inner"], 499.0), (pair["outer"], 450.0)])  # +49
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('+49')")
    before = {p["t"] for p in delta_points(page)}
    rewrite_readings(page, lambda body: body.update(taken_time=body["taken_time"] - 3600))  # the clock went back
    page.wait_for_timeout(2200)
    page.unroute(READING)  # ... and forward again
    page.wait_for_timeout(2200)
    after = [p["t"] for p in delta_points(page)]
    assert not before & set(after), "nothing from before the clock went back is drawn again"
    assert after == sorted(after), "the points stay in time order"


def test_custom_history_is_only_on_page_five(page, rig):
    requests = []
    page.on("request", lambda r: requests.append(r.url) if "/api/replay" in r.url or "/api/faults" in r.url else None)
    sign_in(page, rig)
    for name in ("系统总览", "水冷壁应变场", "热应力分析"):
        page.get_by_role("button", name=name).click()
        page.wait_for_timeout(300)
        assert page.locator("input[type=datetime-local], #replay-settings, #fault-panel").count() == 0, name
    assert requests == [], "the first, second and fourth pages never query the archive"
    page.get_by_role("button", name="历史查询").click()
    page.locator("#replay-settings").wait_for()
    assert page.locator("input[type=datetime-local]").count() >= 3


ALARM = "#ff9f43"


def test_a_confirmed_fault_turns_its_curve_orange_and_only_while_it_lasts(page, rig, pair):
    """修复轮15 (用户 11-01): an abnormal stretch stands out on the curve.  Orange follows the backend's confirmed fault,
    not the bare value: a value beyond the limit is still drawn in the panel's colour until the fault is confirmed."""
    open_trend(page, rig)
    drive(rig, pair, 480.0, 450.0)  # -30 K: normal
    drive(rig, pair, 400.0, 470.0, pause=0.1)  # -70 K: below the -55 K low limit; confirmed after the confirmation time
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('越限待确认')")
    pending = plot(page)
    assert line_and_red(page, 1, pending["now"] - 2, pending["now"], ALARM)["line"] == 0, "a pending one is not yet a fault"
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('故障已确认（低）')", timeout=15000)
    page.wait_for_timeout(8000)  # 300 s span the chart's width: a stretch of a few seconds is only a few pixels wide
    got = plot(page)
    points = got["panels"][1]["points"]
    confirmed = next(p["t"] for p in points if p.get("alarm"))
    assert line_and_red(page, 1, confirmed, got["now"], ALARM)["line"] > 5, "the confirmed stretch is orange"
    assert line_and_red(page, 1, got["t0"], confirmed - 1, ALARM)["line"] == 0, "before the confirmation it was not"
    assert line_and_red(page, 0, got["t0"], got["now"], ALARM)["line"] == 0, "the load has no fault: never orange"
    assert line_and_red(page, 2, got["t0"], got["now"], ALARM)["line"] == 0, "the stress proxy has no limit: never orange"
    assert "橙色" in page.locator("#workspace").inner_text()
    drive(rig, pair, 480.0, 450.0)  # back inside, beyond the hysteresis: the fault recovers and the colour goes back
    page.wait_for_function("!document.querySelector('#trend-latest').textContent.includes('故障已确认')", timeout=15000)
    page.wait_for_timeout(8000)
    after = plot(page)
    recovered = [p["t"] for p in after["panels"][1]["points"] if p["valid"] and not p.get("alarm")][-1]
    assert line_and_red(page, 1, recovered, after["now"], ALARM)["line"] == 0, "after the recovery, the panel's own colour"
    assert line_and_red(page, 1, recovered, after["now"])["line"] > 5
