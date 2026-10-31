"""D42: overview and the 8-point strain distribution (终版 §17.1-17.2).

Real Chrome against the running application, with values the test controls: a bridge of 0.6 mV/V converts to
100 με (GF=6), 0.9 to 150 με, 4.5 to 750 με - above the 690 με limit.  Why it matters: three different
quantities must never stand in for each other, and a colour must mean exactly one thing.
"""

import time

import pytest
from browser_support import sign_in
from live_support import BRIDGES
from web_rig import UA, nominal

MAP = "#strain-map"


@pytest.fixture
def still(rig):
    """The rig's own Modbus rounds are paused so the test's values stay what they are."""
    rig.steady = False
    yield rig
    rig.steady = True


def send_round(rig, **by_station):
    """One Modbus round: every bridge at 0.6 mV/V unless given (station number -> value; None = not answered)."""
    values = {b: 0.6 for b in BRIDGES}
    for number, value in by_station.items():
        station = int(number.removeprefix("s"))
        if value is None:
            del values[BRIDGES[station - 1]]
        else:
            values[BRIDGES[station - 1]] = value
    rig.call(rig.round, values)


def open_page(page, rig, name):
    page.on("dialog", lambda dialog: page.errors.append("dialog: " + dialog.message))
    sign_in(page, rig)
    if name != "系统总览":
        page.get_by_role("button", name=name).click()


def block(page, number):
    return page.locator(f'{MAP} .block[data-station="S{number:02d}"]')


def box_of(page, number):
    """The block's box.  The page draws the blocks anew with every message, so the element asked about can be replaced
    between finding it and measuring it: ask again for a moment instead of failing on that instant."""
    for _ in range(40):
        found = block(page, number).bounding_box()
        if found:
            return found
        page.wait_for_timeout(50)
    raise AssertionError(f"block S{number:02d} has no box")


def cells(page, row, table="#strain-table"):
    return page.locator(f"{table} tbody tr").nth(row).locator("td").all_inner_texts()


def test_the_three_quantities_are_shown_apart_with_their_names_and_units(page, still):
    send_round(still)
    send_round(still, s1=0.9)  # the next planned round: bridge 0.9 -> 150 με, change 150 - 100 = 50 με
    open_page(page, still, "水冷壁应变场")
    head = page.locator("#strain-table thead th").all_inner_texts()
    assert any("原始信号" in h and "mV/V" in h for h in head)
    assert any("换算应变" in h and "GF=6" in h and "未作温度补偿" in h for h in head)
    assert any("应变变化" in h and "με" in h for h in head)
    page.locator("#strain-table tbody tr").nth(0).get_by_text("150").wait_for()
    first = cells(page, 0)
    assert first[1:4] == ["0.9000 mV/V", "150 με", "50 με"], "three numbers, three names, none replacing another"
    assert cells(page, 1)[1:4] == ["0.6000 mV/V", "100 με", "0 με"]  # 终版 §17.6
    assert "GF=6" in page.locator("#workspace").inner_text() and "不是已验证的机械应变" in page.locator("#workspace").inner_text()
    page.get_by_role("button", name="系统总览").click()  # the overview shows the same three, apart
    page.locator("#strain-table tbody tr").nth(0).get_by_text("150").wait_for()
    assert cells(page, 0)[1:4] == ["0.9000 mV/V", "150 με", "50 με"]
    assert page.errors == []


def test_there_are_eight_fixed_blocks_laid_out_by_the_configuration_and_nothing_is_interpolated(page, still):
    open_page(page, still, "水冷壁应变场")
    page.locator(f"{MAP} .block").first.wait_for()
    assert page.locator(f"{MAP} .block").count() == 8 == len(still.config.ui.stations)
    assert page.locator("canvas").count() == 0, "no continuous field is drawn"
    assert "测点分布示意" in page.locator("#workspace").inner_text() and "不在测点之间插值" in page.locator("#workspace").inner_text()
    area = page.locator(MAP).bounding_box()
    boxes = []
    for number, station in enumerate(still.config.ui.stations, start=1):
        box = box_of(page, number)
        assert abs((box["x"] + box["width"] / 2 - area["x"]) / area["width"] * 100 - station.x) < 2
        assert abs((box["y"] + box["height"] / 2 - area["y"]) / area["height"] * 100 - station.y) < 3
        boxes.append(box)
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            apart = a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"] or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"]
            assert apart, "blocks do not overlap"


def test_a_block_is_green_amber_red_or_grey_and_says_why(page, still):
    send_round(still, s3=4.5, s5=None)  # S03 beyond +690 με; S05 does not answer
    open_page(page, still, "水冷壁应变场")
    colours = {}
    page.wait_for_function("document.querySelector('#strain-map .block[data-station=\"S03\"]')?.dataset.state === 'pending'")
    assert block(page, 1).get_attribute("data-state") == "normal" and "正常" in block(page, 1).inner_text()
    assert "越限待确认" in block(page, 3).inner_text() and "750" in block(page, 3).inner_text()
    grey = block(page, 5)
    assert grey.get_attribute("data-state") == "invalid" and "本轮未应答" in grey.inner_text()
    assert grey.locator("strong").inner_text() == "—", "an unanswered round shows a dash, not 0"
    for number in (1, 3, 5):
        colours[number] = block(page, number).evaluate("e => getComputedStyle(e).borderLeftColor")
    deadline = time.monotonic() + 15  # five seconds of the same direction confirm it
    page.wait_for_function("document.querySelector('#strain-map .block[data-station=\"S03\"]')?.dataset.state === 'confirmed'", timeout=15000)
    assert time.monotonic() < deadline
    assert "故障已确认（高）" in block(page, 3).inner_text()
    colours[33] = block(page, 3).evaluate("e => getComputedStyle(e).borderLeftColor")
    assert len(set(colours.values())) == 4, f"four states, four colours: {colours}"
    send_round(still)  # back to normal: the confirmed fault recovers
    page.wait_for_function("document.querySelector('#strain-map .block[data-station=\"S03\"]')?.dataset.state === 'normal'")
    assert page.locator("audio, dialog, #workspace [role=alert], #workspace [role=alertdialog]").count() == 0, "no pop-up, sound or alarm list"
    assert page.errors == [], "no dialog was raised either"


def test_the_overview_counts_pending_and_confirmed_and_separates_validity_from_communication(page, still):
    send_round(still, s3=4.5)
    open_page(page, still, "系统总览")
    page.locator("#fault-summary").get_by_text("越限待确认 1 个通道").wait_for()
    assert "故障已确认 0 个通道" in page.locator("#fault-summary").inner_text()
    assert "不另设报警、确认或等级" in page.locator("#fault-summary").inner_text()
    cards = page.locator("#source-status .card").all_inner_texts()
    assert any("Modbus" in c for c in cards) and any("OPC UA" in c and "数据有效" in c for c in cards)
    page.locator("#fault-summary").get_by_text("故障已确认 1 个通道").wait_for(timeout=15000)
    # a source that has to start over: its values are invalid AND its state says so - two different facts
    gen = still.owner.sources[UA].conn_gen
    still.call(still.owner.source_down, UA, gen, "TEST", time.time(), time.monotonic())
    try:
        page.locator("#source-status").get_by_text("重新同步中").wait_for()
        process = page.locator("#process-table tbody tr").first
        assert process.locator("td").nth(1).inner_text() == "—" and "重新同步中" in process.inner_text()
    finally:
        def start_over():
            still.owner.begin_generation(UA, gen + 1, time.time(), time.monotonic())
            still.ua(list(nominal().items()))
        still.call(start_over)
        send_round(still)
    page.locator("#source-status").get_by_text("数据有效").first.wait_for()
    page.locator("#process-table tbody tr").first.get_by_text("有效").wait_for()
    assert page.errors == []
