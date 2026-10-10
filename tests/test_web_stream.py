"""D38: the page applies whole groups from the realtime socket and never shows what it cannot vouch for.

Real Chrome against the real application (owner, publisher and WebSocket in a server thread).  Why it matters: a
screen that shows an old or half-applied number as if it were current is worse than a blank one.
"""

import copy

from browser_support import sign_in

PROCESS_ROW = "#process-table tbody tr"


def row_of(page, rig, channel):
    return page.locator(PROCESS_ROW, has_text=rig.config.ui.point_labels[channel])


def test_a_value_shows_while_it_is_valid_and_becomes_a_dash_with_its_reason_when_it_is_not(page, rig):
    sign_in(page, rig)
    row = row_of(page, rig, "load_mw")
    row.get_by_text("600").wait_for()
    assert "有效" in row.inner_text()
    rig.call(rig.ua, [("load_mw", 610.0, "BAD", 0x80350000)])
    row.get_by_text("质量不良").wait_for()
    cells = row.locator("td").all_inner_texts()
    assert cells[1].strip() == "—", "an unusable value is not shown, and above all not shown as 0"
    assert "610" not in row.inner_text() and "0 MW" not in row.inner_text()
    rig.call(rig.ua, [("load_mw", 620.0)])
    row.get_by_text("620").wait_for()
    assert "有效" in row.inner_text() and "质量不良" not in row.inner_text()
    assert page.errors == []


def test_three_silent_seconds_raise_a_delay_notice_that_the_next_message_clears(page, rig):
    sign_in(page, rig)
    row_of(page, rig, "load_mw").get_by_text("620").wait_for()
    rig.paused = True  # no more rounds ...
    rig.call(setattr, rig.publisher, "heartbeat", 1000.0)  # ... and no heartbeat: the page must say so
    try:
        page.locator("#notice").wait_for(state="visible", timeout=8000)
        assert "没有收到后端消息" in page.locator("#notice").inner_text()
        assert page.locator("#connection").inner_text() == "消息中断"
        assert "620" in row_of(page, rig, "load_mw").inner_text(), "what is shown stays, marked as possibly old"
    finally:
        rig.paused = False
        rig.call(setattr, rig.publisher, "heartbeat", 1.0)
        rig.call(rig.publisher.wake.set)
    page.locator("#notice").wait_for(state="hidden", timeout=8000)
    assert page.locator("#connection").inner_text() == "实时连接"


def test_after_a_cut_off_the_page_keeps_nothing_and_takes_a_full_snapshot(page, rig):
    sign_in(page, rig)
    row = row_of(page, rig, "load_mw")
    row.get_by_text("620").wait_for()
    rig.drop_clients()
    rig.call(rig.ua, [("load_mw", 631.0)])  # changes while the page has no connection
    page.wait_for_function("document.querySelector('#connection').textContent === '重连中'")
    assert "620" not in row_of(page, rig, "load_mw").inner_text(), "the old value is not kept while out of sync"
    assert "等待实时数据" in page.locator("#process-table").inner_text()
    row.get_by_text("631").wait_for(timeout=8000)
    assert page.locator("#connection").inner_text() == "实时连接"


def test_a_damaged_message_is_refused_whole_and_the_page_takes_a_new_snapshot(page, rig):
    sign_in(page, rig)
    row = row_of(page, rig, "load_mw")
    row.get_by_text("631").wait_for()
    snapshot = rig.call(rig.owner.snapshot)
    seq = snapshot["pub_seq"]
    forged = copy.deepcopy(snapshot["channels"]["load_mw"])
    forged["value"] = 999.0
    group = lambda n: {"seq": seq + n, "order": 0, "source": "ua_field", "wall": 0, "channels": {"load_mw": forged}, "states": [], "faults": []}  # noqa: E731
    message = {"type": "groups", "first": seq + 1, "last": seq + 3, "groups": [group(1), group(2)]}  # `last` is wrong
    rig.call(lambda: [deliver(message) for deliver in list(rig.publisher.subscribers)])
    page.locator("#notice").get_by_text("未通过校验").wait_for()
    assert "999" not in page.locator("#process-table").inner_text(), "not even the groups before the damage"
    row.get_by_text("631").wait_for(timeout=8000)
    page.locator("#notice").wait_for(state="hidden", timeout=8000)  # the new snapshot ends the notice
    assert page.errors == []
