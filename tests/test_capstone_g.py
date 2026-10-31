"""Capstone G: the seven pages end to end.

Chrome against the running application - owner and archive in a server thread, the page reading the owner's
current state once a second (修复轮05) - with everything that goes wrong made to go wrong for real: a damaged
reading, readings that fail or hang, a source that starts over, an archive that refuses.  The module-G tests of D36-D44 are re-run beside this
one; what is added here is what only the whole can show.

Why it matters: a monitoring screen is trusted at a glance.  Whatever happens behind it, it must never show a
new input next to an old result, a half-applied message, or a number from before an interruption as if it were
current - and it must stay readable on the screen the operator actually has.
"""

import json
import time

import pytest
from browser_support import keep, number, sign_in
from live_support import BRIDGES
from web_rig import UA, nominal

OK = "document.querySelector('#connection').textContent === '实时连接'"
CUT = "document.querySelector('#connection').textContent === '数据中断'"
READING = "**/api/snapshot"
PAIR = 2  # the third temperature pair
PROXY_PER_K = 1.1475  # MPa per K: E 170 GPa x alpha 1.35e-5 1/K x restraint 0.5
FORGED = 987654.321  # a load nobody sent: "987,654" on screen would mean a damaged message was applied
NAMES = ["系统总览", "水冷壁应变场", "热应力分析", "历史查询", "系统自检", "通信与日志", "系统管理与权限"]


def fmt(value):
    """The page's own number format: two decimals at most, thousands separated."""
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def connected(page):
    page.wait_for_function(OK, timeout=15000)


def open_page(page, rig, name, who="viewer"):
    sign_in(page, rig, who)
    if name != "系统总览":
        page.get_by_role("button", name=name).click()
    connected(page)


def pair_of(rig):
    chosen = [d for d in rig.config.derived if d.operation == "difference"][PAIR]
    return chosen.inputs[0], chosen.inputs[1]


def plot(page):
    return page.evaluate("() => { const p = document.querySelector('#trend-canvas').plot; return p ? JSON.parse(JSON.stringify(p)) : null; }")


# The pages are watched from the inside: whenever the page changes, what the operator could see at that moment is
# written down.  The browser paints between tasks, so a state recorded here is a state that was on screen.
STRAIN_RECORDER = """
window.__frames = [];
new MutationObserver(() => {
  const rows = [...document.querySelectorAll('#strain-table tbody tr')].map(tr => [...tr.children].map(td => td.innerText.trim()));
  if (rows.length) window.__frames.push(rows);
}).observe(document, {subtree: true, childList: true, characterData: true});
"""
TREND_RECORDER = """
window.__frames = [];
new MutationObserver(() => {
  const spans = [...document.querySelectorAll('#trend-latest [data-panel]')];
  if (spans.length === 3) window.__frames.push(spans.map(s => s.innerText.trim()));
}).observe(document, {subtree: true, childList: true, characterData: true});
"""
CONNECTION_RECORDER = """
window.__conn = new Set();
new MutationObserver(() => {
  const badge = document.getElementById('connection');
  if (badge) window.__conn.add(badge.textContent);
}).observe(document, {subtree: true, childList: true, characterData: true});
"""
BANNER_RECORDER = """
window.__banner = new Set();
new MutationObserver(() => {
  const banner = document.getElementById('archive-banner');
  if (banner && !banner.hidden && banner.textContent) window.__banner.add(banner.textContent);
}).observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
"""
SECRET_RECORDER = """
window.__secret = 0;
new MutationObserver(() => { if (document.body && document.body.innerText.includes('987,654')) window.__secret++; })
  .observe(document, {subtree: true, childList: true, characterData: true});
"""


def frames(page):
    return page.evaluate("window.__frames")


def connection_states(page):
    return set(page.evaluate("[...window.__conn]"))


def check_trend_frames(seen):
    """The temperature difference and the stress proxy come from the same pair in the same group, so on every
    screen state either both are dashes or the proxy is exactly 1.1475 MPa per K of the difference."""
    valid = dashes = 0
    for panels in seen:
        delta, stress = number(panels[1]), number(panels[2])
        if delta is None or stress is None:
            assert delta is None and stress is None, f"a result without its partner: {panels}"
            dashes += 1
        else:
            # 终版 §17.6 shows ℃ to 0.1 and MPa to 0.01; a partner from another group is a whole kelvin off, > 1 MPa
            assert abs(stress - PROXY_PER_K * delta) <= PROXY_PER_K * 0.05 + 0.005 + 1e-9, f"a new input beside an old result: {panels}"
            valid += 1
    return valid, dashes


def push_pair(rig, k, groups=1, heavy=False):
    """`groups` input changes inside ONE event-loop turn, so they leave as one message.  The pair under test
    changes by whole kelvins (the difference is never the same twice in a row); `heavy` also moves all 200
    OPC UA channels, which makes the messages large."""
    inner, outer = pair_of(rig)
    names = nominal()

    def run():
        for j in range(groups):
            step = 3 * k + j
            items = [(inner, 450.0 + step % 37), (outer, 440.0 + step % 5)]
            if heavy:
                items += [(n, v + 0.01 * (step % 5 + 1)) for n, v in names.items() if n not in (inner, outer)]
            rig.ua(items)

    rig.call(run)


def cut_off(page, seconds):
    """The page's readings fail for `seconds` - in the browser; the backend never notices - and then come back."""
    page.route(READING, lambda route: route.abort())
    try:
        page.wait_for_timeout(seconds * 1000)
    finally:
        page.unroute(READING)


def own_errors(page):
    """What the page itself reported; the browser's own line for a load that failed is an injected failure."""
    return [e for e in page.errors if not e.startswith("Failed to load resource")]


def start_over(rig):
    """A source that lost its session: invalid at once; later it begins a new generation and delivers everything."""
    gen = rig.owner.sources[UA].conn_gen
    rig.call(rig.owner.source_down, UA, gen, "TEST", time.time(), time.monotonic())

    def resume():
        rig.owner.begin_generation(UA, gen + 1, time.time(), time.monotonic())
        rig.ua(list(nominal().items()))

    return resume


@pytest.fixture
def calm(rig):
    """Every test leaves the shared rig as it found it: feeder running, all channels at their nominal values."""
    yield
    rig.paused, rig.steady = False, True
    rig.call(rig.ua, list(nominal().items()))


@pytest.fixture
def still(rig, calm):
    """The rig's own Modbus rounds are paused so the test's values stay what they are."""
    rig.steady = False
    yield rig
    rig.steady = True


# ------------------------------------------------------------------------------------------- a damaged reading


def test_a_damaged_reading_changes_nothing_on_the_page_and_the_next_good_one_puts_it_right(page, rig, calm):
    """Whole or nothing (终版 §13.3): not even the trend takes anything from it.  The other kinds of damage are in
    test_web_polling."""
    page.add_init_script(SECRET_RECORDER + CONNECTION_RECORDER)
    open_page(page, rig, "热应力分析")

    def forge(route):
        real = route.fetch().json()
        channels = dict(real["channels"], load_mw=dict(real["channels"]["load_mw"], value=FORGED))
        channels[next(k for k in channels if k != "load_mw")] = {"kind": "input"}  # one channel without its validity
        route.fulfill(status=200, content_type="application/json", body=json.dumps(dict(real, channels=channels)))

    page.route(READING, forge)
    try:
        page.wait_for_function(CUT, timeout=8000)  # answers came, none of them a reading
        keep(page, "g02-damaged-reading")
    finally:
        page.unroute(READING)
    connected(page)
    assert page.evaluate("window.__secret") == 0, "the damaged content was never on screen, not even for a moment"
    assert FORGED not in [p["v"] for p in plot(page)["panels"][0]["points"]], "nor in the trend"
    assert {"数据中断", "实时连接"} <= connection_states(page), "the page said so, then the next reading put it right"
    load = fmt(rig.call(lambda: rig.owner.channel_views()["load_mw"]["value"]))
    page.wait_for_function(f"""document.querySelector('#trend-latest [data-panel="load_mw"]').textContent.includes('{load}')""")
    assert "987" not in page.locator('#trend-latest [data-panel="load_mw"]').inner_text()
    assert own_errors(page) == []


# --------------------------------------------------------------- no new input next to an old result (终版 §6)


UNANSWERED = 6  # the round in which station S01 is not answered


def drive_strain(rig, page, events):
    """Rounds 1..16: station S01 reads 0.60 + 0.01 k mV/V (round 6 is not answered), a little more than one reading
    interval apart so that the page reads each of them.  `events` maps a round number to something done first."""
    for k in range(1, 17):
        values = {b: 0.6 for b in BRIDGES}
        if k == UNANSWERED:
            del values[BRIDGES[0]]
        else:
            values[BRIDGES[0]] = round(0.6 + 0.01 * k, 4)
        if k in events:
            events[k]()
        rig.call(rig.round, values)
        page.wait_for_timeout(1050)


def check_strain_frames(seen, change_may_wait=False):
    """Every state that was on screen: the three quantities of a station are those of ONE round.  A change is
    only ever the step between two consecutive rounds of one segment, so it may be missing (the first round of a
    new segment has none, `change_may_wait`) but is never another number."""
    complete = dashes = 0
    unit = 1000 / 6  # microstrain per mV/V at GF = 6
    for rows in seen:
        for index, row in enumerate(rows):
            raw, strain, change = (number(c) for c in row[1:4])
            if raw is None:
                assert strain is None and change is None, f"a result without its input: {row}"
                dashes += index == 0
                continue
            assert strain is not None, f"an input without its result: {row}"
            if index > 0:
                assert (raw, strain) == (0.6, 100.0) and (change == 0.0 or change is None and change_may_wait), f"a station nobody touched changed: {row}"
                continue
            k = round((raw - 0.6) / 0.01)
            assert abs(raw - (0.6 + 0.01 * k)) < 1e-6, row
            # 终版 §17.6 shows whole microstrain; the next round's input is 0.01 mV/V away, 1.67 με
            assert abs(strain - raw * unit) <= 0.5 + 1e-9, f"the strain is not that of this input: {row}"
            if k == UNANSWERED + 1:
                assert change is None, f"a change was bridged across an unanswered round: {row}"
            elif k == 0:
                assert change == 0.0, row
            elif change is None:
                assert change_may_wait, f"the change is missing although the previous round was there: {row}"
            else:
                # whole microstrain on the page (终版 §17.6): one step is 1.67 με, a change across two rounds 3.33
                assert abs(change - 0.01 * unit) <= 0.5 + 1e-9, f"the change is not that of this round: {row}"
            complete += 1
    return complete, dashes


def test_rounds_and_a_cut_off_never_show_an_input_next_to_another_rounds_result(page, still):
    page.add_init_script(STRAIN_RECORDER + CONNECTION_RECORDER)
    for _ in range(2):  # two quiet rounds: every station starts with a change of 0
        still.call(still.round, {b: 0.6 for b in BRIDGES})
    open_page(page, still, "水冷壁应变场")
    page.locator("#strain-table tbody tr").nth(0).get_by_text("100").wait_for()
    drive_strain(still, page, {10: lambda: cut_off(page, 4)})
    page.wait_for_function("window.__frames.at(-1)[0][1].startsWith('0.7600 mV/V')")
    complete, dashes = check_strain_frames(frames(page))
    assert complete >= 8, f"enough different states were really seen: {complete}"
    assert dashes >= 2, "the unanswered round and the cut-off were both seen as dashes"
    assert "数据中断" in connection_states(page)
    assert own_errors(page) == []
    keep(page, "g03-strain-consistency")


def test_a_source_that_starts_over_and_a_cut_off_never_leave_a_result_beside_the_wrong_input(page, rig, calm):
    page.add_init_script(TREND_RECORDER + CONNECTION_RECORDER)
    open_page(page, rig, "热应力分析")
    page.locator("#pair-select").select_option(str(PAIR))
    resume = None
    for k in range(1, 25):
        if k == 6:
            resume = start_over(rig)
        elif k == 10:
            rig.call(resume)
        elif k == 16:
            cut_off(page, 4)
        elif not 6 < k < 10:
            push_pair(rig, k, groups=1 + k % 3)
        page.wait_for_timeout(500)
    page.wait_for_function(OK)
    page.wait_for_timeout(1200)
    valid, dashes = check_trend_frames(frames(page))
    assert valid >= 8 and dashes >= 2, (valid, dashes)
    panels = plot(page)["panels"]
    assert len(panels[1]["points"]) == len(panels[2]["points"]), "the difference and the proxy are recorded together"
    for delta, stress in zip(panels[1]["points"], panels[2]["points"], strict=True):
        assert (delta["t"], delta["valid"]) == (stress["t"], stress["valid"]), "one reading, one instant, one validity"
        if delta["valid"]:
            assert abs(stress["v"] - PROXY_PER_K * delta["v"]) < 1e-6
    assert {"数据中断", "实时连接"} <= connection_states(page)
    assert own_errors(page) == []
    keep(page, "g04-source-restart-and-cut")


# ------------------------------------------------------------------ readings that fail or hang hold nothing up


def archive_settled(rig):
    """Everything the owner handed over has been written: nothing waiting, refused or retried, none lost."""

    def look():
        stats = dict(rig.queue.stats)
        return stats, rig.queue.depth, rig.owner.order, {s: rig.owner.history_status(s)["archive"] for s in rig.owner.sources}

    deadline = time.monotonic() + 15
    while True:
        stats, depth, order, states = rig.call(look)
        if depth == 0 and stats["highest_committed_order"] == order:
            break
        assert time.monotonic() < deadline, f"the archive did not catch up: {stats} depth {depth} order {order}"
        time.sleep(0.1)
    assert (stats["rejected"], stats["refused"], stats["failures"], stats["unknown"], stats["voided"]) == (0, 0, 0, 0, 0), stats
    assert set(states.values()) == {"OK"}, states
    return stats


def test_readings_that_fail_or_hang_never_hold_up_the_acquisition_or_the_archive(page, archive_rig):
    """User rule 5 (修复轮05): a display request is a request and nothing more.  For eight seconds no reading is
    answered (each one is given up at its 5 s limit) while all 200 OPC UA channels change ten times a second: the
    owner and the archive carry on, the page says it is cut off, and the next good reading is whole."""
    rig = archive_rig
    page.add_init_script(TREND_RECORDER + CONNECTION_RECORDER)
    open_page(page, rig, "热应力分析")
    page.locator("#pair-select").select_option(str(PAIR))
    before = rig.call(lambda: rig.queue.stats["committed"])
    page.route(READING, lambda route: None)  # held: never answered
    try:
        for k in range(1, 81):
            push_pair(rig, k, groups=3, heavy=True)
            page.wait_for_timeout(100)
        page.wait_for_function(CUT, timeout=3000)
    finally:
        page.unroute(READING)
    try:
        connected(page)
        stats = archive_settled(rig)
        assert stats["committed"] > before, "the archive went on writing throughout"
        page.wait_for_timeout(1200)
        valid, _ = check_trend_frames(frames(page))
        assert valid >= 2, "results before and after, none of them inconsistent"
        assert {"数据中断", "实时连接"} <= connection_states(page)
        assert own_errors(page) == []
        keep(page, "g05-readings-fail-archive-carries-on")
    finally:
        rig.call(rig.ua, list(nominal().items()))


# ------------------------------------------------------------------------------------------ the archive gap


def test_a_live_archive_gap_is_announced_and_the_live_pages_stay_consistent(page, archive_rig):
    rig = archive_rig
    rig.steady = False
    try:
        page.add_init_script(STRAIN_RECORDER + BANNER_RECORDER)
        for _ in range(2):
            rig.call(rig.round, {b: 0.6 for b in BRIDGES})
        open_page(page, rig, "水冷壁应变场")
        page.locator("#strain-table tbody tr").nth(0).get_by_text("100").wait_for()
        closed = lambda: setattr(rig.queue, "capacity", 0)  # noqa: E731 - the writer queue refuses: a gap opens
        opened = lambda: setattr(rig.queue, "capacity", 400)  # noqa: E731
        drive_strain(rig, page, {4: lambda: rig.call(closed), 10: lambda: rig.call(opened)})
        said = page.evaluate("[...window.__banner]")
        # the banner speaks of history only: whether a live value is valid is what the page itself shows (the sources may be
        # down too), so it never claims the live values are still updating
        assert any("历史归档中断" in t and "该段历史可能缺失" in t and "以页面当前状态为准" in t for t in said), said
        assert not any("实时值仍在更新" in t for t in said), said
        page.locator("#archive-banner").wait_for(state="hidden", timeout=30000)  # recovered: the gap is closed
        keep(page, "g07-live-archive-gap")
        complete, _ = check_strain_frames(frames(page), change_may_wait=True)
        assert complete >= 8, "the live numbers kept coming, whole, while the archive was refusing"
        assert page.errors == []
    finally:
        rig.call(lambda: setattr(rig.queue, "capacity", 400))
        rig.steady = True


def rows_of(page):
    return [[c.strip() for c in r.locator("td").all_inner_texts()] for r in page.locator("#replay-records tbody tr").all()]


def test_around_an_archive_gap_every_result_has_its_input_and_every_input_its_result(page, replay_rig):
    sign_in(page, replay_rig)
    page.get_by_role("button", name="历史查询").click()
    page.locator("#replay-settings").wait_for()
    page.locator("#replay-center").fill("1970-01-01T08:17:10")
    page.locator("#replay-center").dispatch_event("change")
    page.locator("#replay-seconds").fill("100")
    page.locator("#replay-seconds").dispatch_event("change")
    page.locator("#replay-points").select_option(["bridge_mvv_01", "strain_01"])
    page.locator("#replay-run").click()
    page.locator("#replay-meta").wait_for()
    notes = " ".join(page.locator(".note").all_inner_texts())
    assert "mb_field" in notes and "08:17:06" in notes and "08:17:09" in notes, "the gap of the Modbus source is announced"
    seen = {}
    for channel in ("bridge_mvv_01", "strain_01"):
        page.locator("#replay-focus").select_option(channel)
        page.wait_for_function(f"document.querySelector('#replay-focus')?.value === '{channel}' && document.querySelector('#replay-records tbody tr')")
        seen[channel] = rows_of(page)
        keep(page, f"g08-gap-{channel}") if channel == "strain_01" else None
    bridge, strain = seen["bridge_mvv_01"], seen["strain_01"]
    # same time, same end and the same gap mark: no result without its input and no input without its result - across
    # the gap too (columns: 生效时间, 值, 质量或状态, 种类, 有效段, 说明, 缺口标记)
    assert [(r[0], r[5], r[6]) for r in bridge] == [(r[0], r[5], r[6]) for r in strain]
    before = [r for r in strain if r[5].startswith("至有效段结束 1970/1/1 08:17:06")]
    assert len(before) == 1, "the last value before the gap ends at its start"
    after = strain[strain.index(before[0]) + 1]
    assert after[0].startswith("1970/1/1 08:17:09"), "nothing in between: the next is restated where the segment opens"
    for b, s in zip(bridge, strain, strict=True):
        assert abs(float(s[1].replace(",", "")) - float(b[1].replace(",", "")) * 1000 / 6) <= 1.0, (b, s)


# ----------------------------------------------------------------------- permissions and the missing write routes


def test_no_route_changes_a_channel_a_threshold_or_the_configuration_for_anyone(page, archive_rig):
    sign_in(page, archive_rig, "viewer")
    spec = page.request.get(archive_rig.url + "/openapi.json").json()
    writes = sorted((method.upper(), path) for path, ops in spec["paths"].items() for method in ops if method in {"post", "put", "patch", "delete"})
    assert writes == [("DELETE", "/api/admin/accounts/{username}"), ("POST", "/api/admin/accounts"), ("POST", "/api/admin/password")], (
        "only accounts can be written: made, stopped, and the administrator's own password (修复轮02)"
    )
    before = page.request.get(archive_rig.url + "/api/ui-config", headers={"Authorization": "Bearer " + archive_rig.credentials["viewer"]}).json()
    for who in ("viewer", "admin"):
        headers = {"Authorization": "Bearer " + archive_rig.credentials[who], "Content-Type": "application/json"}
        for path in ("/api/ui-config", "/api/channels", "/api/points", "/api/derived", "/api/thresholds", "/api/config", "/api/snapshot"):
            for method in ("post", "put", "patch", "delete"):
                answer = getattr(page.request, method)(archive_rig.url + path, headers=headers, data=json.dumps({"id": "load_mw", "high": 1}))
                assert answer.status in (404, 405), f"{who} {method} {path} -> {answer.status}"
    after = page.request.get(archive_rig.url + "/api/ui-config", headers={"Authorization": "Bearer " + archive_rig.credentials["viewer"]}).json()
    assert before == after, "the configuration is what it was"


# ---------------------------------------------------------------------------------------------------- the seven pages


def test_the_seven_pages_open_in_order_at_desktop_width(page, archive_rig):
    open_page(page, archive_rig, "系统总览", who="admin")
    for n, name in enumerate(NAMES, start=1):
        page.locator("#navigation").get_by_role("button", name=name).click()
        assert page.locator(".page-heading h2").inner_text() == name
        page.wait_for_timeout(400)
        keep(page, f"desktop-{n}")
    assert page.errors == []
