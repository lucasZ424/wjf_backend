"""D43: page 4 - the live three-panel trend (终版 §17.3, 修复轮18; 验收表 F).

Real Chrome against the running application.  Why it matters: three curves on one time axis invite the reading "these were
sampled together" and "this one caused that one"; the page must show them side by side without normalising or mixing sources.
A wall and a bridge have nothing to do with each other any more, so each is chosen on its own, and two protocols going down are
two separate events on the chart.  The colours are what tells the panels apart at a glance: the ones of 用户 11-06.
"""

import json
import time

import pytest
from browser_support import sign_in
from live_support import BRIDGES
from test_pointsheet import BASE, add, blank, bridge, pointsheet, wall
from web_rig import MB, UA, WebRig, nominal

from htsg.config import Settings

WALL_INDEX, BRIDGE_INDEX = 2, 1  # the third wall and the second bridge of the configuration
READING = "**/api/snapshot"
BLUE, GREEN, PURPLE, ALARM = "#73bcff", "#4fd1a5", "#c79bff", "#ff9f43"  # F10, 用户 11-06


@pytest.fixture
def chosen(rig):
    """What the page is set to; every bridge held at 0.6 mV/V (100 με, 17 MPa), so that the test knows what the stress is."""
    wall_id = f"temp_wall_{WALL_INDEX + 1:03d}"
    rig.held = {b: 0.6 for b in BRIDGES}
    yield {"wall": wall_id, "stress": f"stress_{BRIDGE_INDEX + 1:02d}", "bridge": BRIDGES[BRIDGE_INDEX]}
    rig.held = None
    rig.call(rig.ua, [(wall_id, nominal()[wall_id])])


def open_trend(page, rig, wall_index=WALL_INDEX, bridge_index=BRIDGE_INDEX):
    sign_in(page, rig)
    page.get_by_role("button", name="热应力分析").click()
    page.locator("#wall-select").select_option(str(wall_index))
    page.locator("#stress-select").select_option(str(bridge_index))
    page.locator("#trend-latest").wait_for()


def close_look(page):
    """修复轮19: the 5-minute window (终版 §17.3 放大查看), where a stretch of a few seconds is wide enough to look at; the
    30-minute default spreads 1800 s over the same width."""
    page.locator("#trend-window").select_option("300")


def plot(page):
    return page.evaluate("() => { const p = document.querySelector('#trend-canvas').plot; return p ? JSON.parse(JSON.stringify(p)) : null; }")


def set_bridge(rig, chosen, value):
    rig.held = rig.held | {chosen["bridge"]: value}
    rig.call(rig.round)


def drive(rig, chosen, temperature, bridge_value=None, pause=2.1):  # longer than one reading interval: every value is read
    rig.call(rig.ua, [(chosen["wall"], temperature)])
    if bridge_value is not None:
        set_bridge(rig, chosen, bridge_value)
    time.sleep(pause)


def span(page, panel_id):
    return page.locator(f'#trend-latest [data-panel="{panel_id}"]').inner_text()


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


def test_three_panels_share_one_time_axis_and_keep_their_own_units_axes_signs_and_colours(page, rig, chosen):
    """F1, F4, F10."""
    open_trend(page, rig)
    # 0.6, 0.3, -0.6 mV/V are 100, 50, -100 με, which is +17, +8.5, -17 MPa
    for temperature, bridge_value in ((470.0, 0.6), (475.0, 0.3), (480.0, -0.6)):
        drive(rig, chosen, temperature, bridge_value)
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('-17.00')")
    got = plot(page)
    assert [p["id"] for p in got["panels"]] == ["load_mw", chosen["wall"], chosen["stress"]], "F1: the load, the wall, the stress"
    assert [p["unit"] for p in got["panels"]] == ["MW", "℃", "MPa"], "one unit per panel, never mixed"
    assert got["now"] - got["t0"] == 1800, "one time axis for all three, 30 minutes by default (修复轮19, 验收表 C1)"
    # Each panel's axis fits its own values (修复轮15: at least its least span, snapped to even steps); a normalised or shared
    # axis covers either too little of a panel's values or far more than them.
    for panel, least in zip(got["panels"], (50, 10, 10)):
        values = [q["v"] for q in panel["points"] if q["valid"]]
        reach = max(values) - min(values) + least
        assert min(values) - reach <= panel["lo"] <= min(values) and max(values) <= panel["hi"] <= max(values) + reach, panel["id"]
        assert panel["hi"] - panel["lo"] >= least, "a steady reading is not stretched over the whole panel"
    assert [p["color"] for p in got["panels"]] == [BLUE, GREEN, PURPLE], "F10: the load blue, the wall green, the stress purple"
    drawn = [line_and_red(page, i, got["t0"], got["now"])["line"] for i in range(3)]
    assert all(n > 0 for n in drawn), f"every panel's line is drawn in that panel's colour: {drawn}"
    walls = [p["v"] for p in got["panels"][1]["points"] if p["valid"]]
    assert walls[-3:] == [470.0, 475.0, 480.0], "values exactly as received"
    stress = [p["v"] for p in got["panels"][2]["points"] if p["valid"]]
    assert stress[-3:] == pytest.approx([17.0, 8.5, -17.0]), "the sign of the stress is kept"
    assert "-17.00 MPa" in span(page, chosen["stress"]), "F4: and shown"
    assert "480.0 ℃" in span(page, chosen["wall"]) and "+" not in span(page, chosen["wall"]), "F4: no plus sign on a wall"
    assert "MW" in span(page, "load_mw") and "+" not in span(page, "load_mw"), "F4: nor on the load"
    assert page.errors == []


def test_the_lists_hold_every_wall_and_every_bridge(page, rig, chosen):
    """F2: 215 and 8; a wall under the name the site gave it (the site's bridge names: the next test)."""
    open_trend(page, rig)
    walls = page.locator("#wall-select option").all_inner_texts()
    stresses = page.locator("#stress-select option").all_inner_texts()
    assert len(walls) == 215 and len(stresses) == 8
    labels = rig.config.ui.point_labels
    assert walls == [labels[f"temp_wall_{n:03d}"] for n in range(1, 216)]
    assert stresses == [labels[f"stress_{n:02d}"].removesuffix(" 热应力估算") for n in range(1, 9)]


def test_changing_the_wall_changes_only_the_wall_panel_and_changing_the_bridge_only_the_stress_panel(page, rig, chosen):
    """F3, F10, Q2: the two have no relation; a choice that moved the other would imply one, and the colours stay with the panel."""
    open_trend(page, rig)
    other_wall, other_stress = "temp_wall_006", "stress_05"
    rig.call(rig.ua, [(other_wall, 486.5)])
    rig.held = rig.held | {BRIDGES[4]: 0.3}
    rig.call(rig.round)
    drive(rig, chosen, 470.0, 0.6)
    before = plot(page)["panels"]
    page.locator("#wall-select").select_option("5")
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('486.5')")
    after = plot(page)["panels"]
    assert [p["id"] for p in after] == ["load_mw", other_wall, chosen["stress"]]
    assert [p["color"] for p in after] == [BLUE, GREEN, PURPLE], "the colour is the panel's, not the point's"
    assert after[2]["points"][0] == before[2]["points"][0], "the stress panel kept its curve"
    page.locator("#stress-select").select_option("4")
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('8.50')")
    after = plot(page)["panels"]
    assert [p["id"] for p in after] == ["load_mw", other_wall, other_stress]
    assert [p["color"] for p in after] == [BLUE, GREEN, PURPLE]
    assert "486.5" in span(page, other_wall), "the wall panel did not move"
    rig.call(rig.ua, [(other_wall, nominal()[other_wall])])


def source_outage(page, rig, source, panels_down, panels_up, restart):
    """Take a source down for a few seconds and look at the chart: what came from it is cut and marked, the rest goes on."""
    gen = rig.call(lambda: rig.owner.sources[source].conn_gen)
    rig.call(rig.owner.source_down, source, gen, "TEST", time.time(), time.monotonic())
    time.sleep(5)
    rig.call(restart, gen)
    time.sleep(2.5)  # the page then hears from the server at least once more
    got = plot(page)
    for index in panels_down:
        points = got["panels"][index]["points"]
        gap = next((i for i, p in enumerate(points) if not p["valid"]), None)
        assert gap is not None and points[gap + 1]["valid"], f"panel {index} records the invalid period"
        inside = line_and_red(page, index, points[gap]["t"], points[gap + 1]["t"])
        assert inside["line"] == 0 and inside["red"] > 0, f"panel {index}: nothing drawn through it, a mark instead: {inside}"
    down = got["panels"][panels_down[0]]["points"]
    start = next(p["t"] for p in down if not p["valid"])
    end = next(p["t"] for p in down if p["valid"] and p["t"] > start)
    for index in panels_up:
        assert all(p["valid"] for p in got["panels"][index]["points"] if start <= p["t"] <= end), f"panel {index} is not of that source"
        inside = line_and_red(page, index, start, end)
        assert inside["line"] > 0 and inside["red"] == 0, f"panel {index} goes straight on: {inside}"


def test_an_opcua_outage_cuts_the_load_and_the_wall_and_the_stress_goes_on(page, rig, chosen):
    """F5 (用户 11-05): the bridges are another protocol."""
    open_trend(page, rig)
    close_look(page)
    drive(rig, chosen, 480.0)

    def restart(gen):
        rig.owner.begin_generation(UA, gen + 1, time.time(), time.monotonic())
        rig.ua(list(nominal().items()))

    source_outage(page, rig, UA, (0, 1), (2,), restart)
    assert [e for e in page.errors if not e.startswith("Failed to load resource")] == []


def test_a_modbus_outage_cuts_the_stress_and_the_load_and_the_wall_go_on(page, rig, chosen):
    """F6 (用户 11-05): the other way round."""
    open_trend(page, rig)
    close_look(page)
    drive(rig, chosen, 480.0)

    def restart(gen):
        rig.owner.begin_generation(MB, gen + 1, time.time(), time.monotonic())
        rig.round()

    source_outage(page, rig, MB, (2,), (0, 1), restart)
    assert [e for e in page.errors if not e.startswith("Failed to load resource")] == []


def test_when_the_page_cannot_read_every_panel_is_cut_for_that_stretch(page, rig, chosen):
    """终版 §13.3: what happened while the page could not read is unknown, whichever source.  The readings fail for a while (修复轮05:
    in the browser, the backend is untouched) so that the unknown stretch is wide enough to look at."""
    open_trend(page, rig)
    close_look(page)
    started = time.time()  # the mark stands at the last good reading before the cut-off, which drive() lets the page take
    drive(rig, chosen, 480.0)
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
        assert points[cut]["t"] > started, "the mark is the cut-off's own"
        assert points[cut + 1]["valid"] and points[cut + 1]["t"] - points[cut]["t"] > 4, "the unknown stretch is real"
        inside = line_and_red(page, index, points[cut]["t"], points[cut + 1]["t"])
        assert inside["line"] == 0 and inside["red"] > 0, f"panel {index}: no line across the cut-off: {inside}"
    assert [e for e in page.errors if not e.startswith("Failed to load resource")] == [], "only the injected failures"


def test_the_cursor_reads_the_value_in_effect_in_every_panel_at_one_instant(page, rig, chosen):
    open_trend(page, rig)
    close_look(page)
    drive(rig, chosen, 470.0, pause=2.0)
    drive(rig, chosen, 460.0, pause=3.5)  # 460 has been in effect for 3.5 s: a cursor just before "now" is inside it
    got = plot(page)
    box = page.locator("#trend-canvas").bounding_box()
    offset = got["right"] - 5
    page.mouse.move(box["x"] + offset, box["y"] + 100)
    readout = page.locator("#trend-readout")
    readout.get_by_text("游标").wait_for()
    text = readout.inner_text()
    after = plot(page)
    t = after["t0"] + (offset - after["left"]) / (after["right"] - after["left"]) * (after["now"] - after["t0"])
    in_effect = [p for p in after["panels"][1]["points"] if p["t"] <= t][-1]
    assert in_effect["v"] == 460.0 and "壁温 460.0 ℃" in text, (text, in_effect)
    assert "负荷" in text and "热应力（估算）" in text and "17.00 MPa" in text
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


def wall_points(page):
    return plot(page)["panels"][1]["points"]


def latest_says(page, text):
    page.wait_for_function("t => document.querySelector('#trend-latest').textContent.includes(t)", arg=text)


def test_each_reading_is_recorded_at_the_time_it_was_taken_not_at_the_observation_time(page, rig, chosen):
    """修复轮05 (终版 §13.3): the live trend is what the page saw and when; it does not rebuild when things happened."""
    open_trend(page, rig)
    taken = rewrite_readings(page, lambda body: None)
    observed = time.time()
    rig.call(rig.ua, [(chosen["wall"], 471.0)])
    latest_says(page, "471.0")
    point = next(p for p in wall_points(page) if p["valid"] and p["v"] == 471.0)
    assert point["t"] in taken, "drawn at the time of the reading that showed it"
    assert point["t"] > observed, "not drawn back to the observation"


def test_a_value_that_stays_the_same_is_one_point_however_many_readings_show_it(page, rig, chosen):
    open_trend(page, rig)
    taken = rewrite_readings(page, lambda body: None)
    rig.call(rig.ua, [(chosen["wall"], 473.0)])
    latest_says(page, "473.0")
    count, seen = len(wall_points(page)), len(taken)
    page.wait_for_timeout(3500)
    assert len(taken) - seen >= 3, "several readings showed it"
    assert len(wall_points(page)) == count, "... and it is still one point"


def test_a_new_run_starts_a_new_stretch_even_with_the_same_value(page, rig, chosen):
    open_trend(page, rig)
    rig.call(rig.ua, [(chosen["wall"], 477.0)])
    latest_says(page, "477.0")
    rewrite_readings(page, lambda body: body.update(run_id="another-run"))  # the backend restarted
    page.wait_for_timeout(2500)
    points = wall_points(page)
    last = max(i for i, p in enumerate(points) if p["valid"] and p["v"] == 477.0)
    assert last > 0 and not points[last - 1]["valid"], "a new run is not joined to the old one by a line"
    assert any(p["valid"] and p["v"] == 477.0 for p in points[: last - 1]), "the same value before the restart"


def test_a_reading_time_that_goes_back_starts_the_trend_over(page, rig, chosen):
    open_trend(page, rig)
    rig.call(rig.ua, [(chosen["wall"], 479.0)])
    latest_says(page, "479.0")
    before = {p["t"] for p in wall_points(page)}
    rewrite_readings(page, lambda body: body.update(taken_time=body["taken_time"] - 3600))  # the clock went back
    page.wait_for_timeout(2200)
    page.unroute(READING)  # ... and forward again
    page.wait_for_timeout(2200)
    after = [p["t"] for p in wall_points(page)]
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


def test_a_confirmed_fault_of_the_wall_turns_its_curve_orange_and_only_while_it_lasts(page, rig, chosen):
    """F7, F10 (修复轮15, 用户 11-01, 11-06), 修复轮19 C11: orange follows the backend's confirmed fault, not the bare value: a value
    beyond the limit is still drawn in the panel's colour until the fault is confirmed, and comes back to it on recovery.  Looked at
    in the 5-minute window (修复轮19), where a stretch of a few seconds is wide enough to count."""
    open_trend(page, rig)
    close_look(page)
    drive(rig, chosen, 480.0)
    rig.call(rig.ua, [(chosen["wall"], 495.0)])  # beyond the limit of 490; confirmed after the confirmation time
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('越限待确认')")
    pending = plot(page)
    assert line_and_red(page, 1, pending["now"] - 2, pending["now"], ALARM)["line"] == 0, "a pending one is not yet a fault"
    latest = pending["panels"][1]["points"][-1]
    assert latest["valid"] and latest["v"] == 495.0 and not latest.get("alarm"), "F7: pending is not orange, only confirmed is"
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('故障已确认（高）')", timeout=15000)
    page.wait_for_timeout(8000)  # 300 s span the chart's width: a stretch of a few seconds is only a few pixels wide
    got = plot(page)
    points = got["panels"][1]["points"]
    confirmed = next(p["t"] for p in points if p.get("alarm"))
    assert line_and_red(page, 1, confirmed, got["now"], ALARM)["line"] > 5, "the confirmed stretch is orange"
    assert line_and_red(page, 1, got["t0"], confirmed - 1, ALARM)["line"] == 0, "before the confirmation it was not"
    assert line_and_red(page, 0, got["t0"], got["now"], ALARM)["line"] == 0, "the load has no fault: never orange"
    assert line_and_red(page, 2, got["t0"], got["now"], ALARM)["line"] == 0, "the stress has no limit: never orange"
    assert "橙色" in page.locator("#workspace").inner_text()
    drive(rig, chosen, 480.0)  # back inside, beyond the hysteresis: the fault recovers and the colour goes back
    page.wait_for_function("!document.querySelector('#trend-latest').textContent.includes('故障已确认')", timeout=15000)
    page.wait_for_timeout(8000)
    after = plot(page)
    recovered = [p["t"] for p in after["panels"][1]["points"] if p["valid"] and not p.get("alarm")][-1]
    assert line_and_red(page, 1, recovered, after["now"], ALARM)["line"] == 0, "after the recovery, the panel's own colour"
    assert line_and_red(page, 1, recovered, after["now"])["line"] > 5
    assert after["panels"][1]["color"] == GREEN


def test_the_page_says_estimate_once_and_nothing_of_the_old_model(page, rig, chosen):
    """F8 (用户 11-01, 11-05): only what an operator acts on; the words of the difference model would describe a page that is gone."""
    open_trend(page, rig)
    drive(rig, chosen, 480.0)
    text = page.locator("#workspace").inner_text()
    assert text.count("热应力（估算）") == 1, text
    for old in ("温差", "内外壁", "内壁", "外壁", "温度对"):
        assert old not in text, old
    assert page.evaluate("document.querySelector('#workspace').textContent").count("热应力（估算）") == 1


def site_config(tmp_path, walls, bridges, load=True):
    """A configuration as the point-sheet tool makes it for a site."""
    path = blank(tmp_path)
    for tag in walls:
        wall(path, tag)
    for n in range(1, bridges + 1):
        bridge(path, n)
    if load:
        add(path, "OPC UA点", 类别="机组负荷", 显示名称="LOAD", 数据源="ua_field", NodeId="ns=2;s=LOAD")
    return Settings.model_validate(pointsheet.convert(BASE, path)[0])


def test_the_lists_use_the_names_the_site_gave(page, tmp_path):
    """F2, Q2: the operator looks for the tag on the drawing, not for our number."""
    with WebRig(tmp_path, config=site_config(tmp_path, ("TE-A1", "TE-B7", "TE-C3"), 2)) as site:
        sign_in(page, site)
        page.get_by_role("button", name="热应力分析").click()
        page.locator("#trend-latest").wait_for()
        assert page.locator("#wall-select option").all_inner_texts() == ["TE-A1", "TE-B7", "TE-C3"]
        assert page.locator("#stress-select option").all_inner_texts() == ["SG-01", "SG-02"]
    assert page.errors == []


@pytest.mark.parametrize(
    ("walls", "bridges", "panels"),
    [
        pytest.param((), 2, ["load_mw", "stress_01"], id="F9-no-wall"),
        pytest.param(("TE-1", "TE-2"), 0, ["load_mw", "temp_wall_001"], id="F9-no-bridge"),
        pytest.param((), 0, ["load_mw"], id="F9-neither"),
    ],
)
def test_a_configuration_without_walls_or_bridges_draws_the_panels_it_has(page, tmp_path, walls, bridges, panels):
    """F9 (用户 11-05): a site can be connected piece by piece, and a page that fails on an empty list fails on the whole app."""
    with WebRig(tmp_path, config=site_config(tmp_path, walls, bridges)) as site:
        sign_in(page, site)
        page.get_by_role("button", name="热应力分析").click()
        page.locator("#trend-latest").wait_for()
        page.wait_for_function("document.querySelector('#trend-canvas').plot?.panels.length > 0")
        page.wait_for_timeout(1200)
        got = plot(page)
        assert [p["id"] for p in got["panels"]] == panels
        assert page.locator("#trend-latest [data-panel]").count() == len(panels)
        assert page.locator("#wall-select option").count() == len(walls)
        assert page.locator("#stress-select option").count() == bridges
    assert page.errors == []
