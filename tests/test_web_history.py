"""D39: the history page - fault list on the left, replay settings above the results, fault search that pages.

Real Chrome against the real application with a real archive.  Why it matters: an operator finds the moment a
fault happened by searching and filtering a long list, and the settings must follow the chosen fault without ever
depending on one.
"""

from datetime import datetime, timedelta, timezone

import pytest
from browser_support import sign_in

BASE = 1_700_000_000  # 2023-11-14 22:13:20 UTC
LOCAL = timezone(timedelta(hours=8))  # the browser context runs in Asia/Shanghai
TOTAL = 45


def local_text(wall):
    return datetime.fromtimestamp(wall, LOCAL).strftime("%Y-%m-%dT%H:%M:%S")


@pytest.fixture(scope="module")
def channels(archive_rig):
    ruled = [d.id for d in (*archive_rig.config.points, *archive_rig.config.derived) if d.high is not None or d.low is not None]
    assert len(ruled) >= 3
    return ruled[:3]


@pytest.fixture(scope="module", autouse=True)
def seeded(archive_rig, channels):
    """45 faults, newest last: three channels in turn, directions alternate, one a minute."""
    archive_rig.seed_faults(
        [(channels[n % 3], "HIGH" if n % 2 == 0 else "LOW", BASE + 60 * n, 500.0 + n) for n in range(TOTAL)]
    )


def open_history(page, rig):
    sign_in(page, rig)
    page.get_by_role("button", name="历史查询").click()
    page.locator("#fault-list").wait_for()
    page.wait_for_function("document.querySelectorAll('#fault-list .fault-row').length > 0")


def rows(page):
    return page.locator("#fault-list .fault-row")


def shown_ids(page):
    return page.locator("#fault-list .fault-row").evaluate_all("rows => rows.map(r => r.dataset.fault)")


def test_the_fault_list_is_on_the_left_the_settings_above_and_the_results_in_the_middle(page, archive_rig):
    open_history(page, archive_rig)
    faults, settings, board = (page.locator(f"#{x}").bounding_box() for x in ("fault-panel", "replay-settings", "replay-board"))
    assert faults["x"] + faults["width"] <= settings["x"], "the list is left of the settings"
    assert settings["y"] + settings["height"] <= board["y"] + 1, "the settings are above the results"
    assert abs(settings["x"] - board["x"]) < 2 and abs(settings["width"] - board["width"]) < 2, "one middle column"
    assert page.errors == []


def test_the_list_pages_newest_first_without_a_repeat_or_a_gap(page, archive_rig):
    open_history(page, archive_rig)
    assert rows(page).count() == 20 and page.locator("#fault-more").is_visible()
    for expected in (40, TOTAL):
        page.locator("#fault-more").click()
        page.wait_for_function(f"document.querySelectorAll('#fault-list .fault-row').length === {expected}")
    assert not page.locator("#fault-more").is_visible(), "nothing more to load"
    ids = shown_ids(page)
    assert len(ids) == len(set(ids)) == TOTAL
    assert [int(i.rsplit("/", 1)[1]) for i in ids] == sorted((int(i.rsplit("/", 1)[1]) for i in ids), reverse=True)
    assert f"已显示 {TOTAL} 条" in page.locator("#fault-count").inner_text()


def test_search_and_filters_narrow_the_list_and_paging_continues_inside_the_search(page, archive_rig, channels):
    open_history(page, archive_rig)
    label = archive_rig.config.ui.point_labels[channels[0]]
    page.locator("#fault-direction").select_option("HIGH")
    page.locator("#fault-find").click()
    page.wait_for_function("document.querySelectorAll('#fault-list .fault-row').length === 20")
    assert all("高" in t for t in rows(page).all_inner_texts())
    page.locator("#fault-search").fill(channels[1])  # type to narrow the channel choice, then pick
    page.locator("#fault-point").select_option(channels[1])
    page.locator("#fault-find").click()
    page.wait_for_function("document.querySelectorAll('#fault-list .fault-row').length === 7")  # n % 6 == 4: 4, 10, ... 40
    texts = rows(page).all_inner_texts()
    assert all(archive_rig.config.ui.point_labels[channels[1]] in t and "高" in t for t in texts)
    assert label not in " ".join(texts)
    page.locator("#fault-direction").select_option("")
    page.locator("#fault-reset").click()  # clears every filter
    page.wait_for_function("document.querySelectorAll('#fault-list .fault-row').length === 20")
    # a filter edited but not applied must not break "load more": it continues the search that was run
    page.locator("#fault-direction").select_option("LOW")
    page.locator("#fault-more").click()
    page.wait_for_function("document.querySelectorAll('#fault-list .fault-row').length === 40")
    assert page.locator("#fault-count").inner_text().startswith("已显示 40"), "no error from a mismatched cursor"
    # time range: faults 10..14 are confirmed in [BASE+600, BASE+900)
    page.locator("#fault-reset").click()
    page.locator("#fault-start").fill(local_text(BASE + 600))
    page.locator("#fault-end").fill(local_text(BASE + 900))
    page.locator("#fault-find").click()
    page.wait_for_function("document.querySelectorAll('#fault-list .fault-row').length === 5")
    assert page.errors == []


def test_choosing_a_fault_fills_the_time_and_channel_and_a_manual_entry_needs_no_fault(page, archive_rig, channels):
    open_history(page, archive_rig)
    assert "未锁定故障" in page.locator("#replay-lock").inner_text()
    rows(page).nth(3).click()  # the fourth newest: n = TOTAL - 4
    n = TOTAL - 4
    assert page.locator("#replay-center").input_value() == local_text(BASE + 60 * n)
    selected = page.locator("#replay-points").evaluate("s => [...s.selectedOptions].map(o => o.value)")
    assert selected == [channels[n % 3]], "the fault's own channel is the default"
    assert "参考故障" in page.locator("#replay-lock").inner_text()
    assert rows(page).nth(3).get_attribute("class").count("selected") == 1
    # everything stays editable while the fault is locked
    page.locator("#replay-seconds").fill("120")
    page.locator("#replay-seconds").dispatch_event("change")
    page.locator("#replay-center").fill(local_text(BASE + 5))
    page.locator("#replay-center").dispatch_event("change")
    assert "中心时间已手动修改" in page.locator("#replay-lock").inner_text()
    page.locator("#replay-points").select_option([channels[0], channels[2]])
    # unlocking keeps the values; a fully manual setting works with no fault at all
    page.locator("#unlock").click()
    assert "未锁定故障" in page.locator("#replay-lock").inner_text()
    assert page.locator("#replay-center").input_value() == local_text(BASE + 5)
    assert page.locator("#replay-seconds").input_value() == "120"
    assert page.locator(".fault-row.selected").count() == 0
    assert page.errors == []


def test_a_slow_earlier_search_cannot_overwrite_the_newer_one(page, archive_rig):
    held = []

    def hold_the_high_search(route):
        if "direction=HIGH" in route.request.url and not held:
            held.append(route)  # answered only after the second search has been shown
        else:
            route.continue_()

    page.route("**/api/faults*", hold_the_high_search)
    open_history(page, archive_rig)
    page.locator("#fault-direction").select_option("HIGH")
    page.locator("#fault-find").click()
    page.locator("#fault-direction").select_option("LOW")
    page.locator("#fault-find").click()
    page.wait_for_function("(() => { const r = [...document.querySelectorAll('#fault-list .fault-row')]; return r.length === 20 && r.every(x => x.textContent.includes('低')); })()")
    assert held, "the first search was really in flight"
    held[0].continue_()  # now the late answer arrives
    page.wait_for_timeout(700)
    rows(page).nth(0).click()  # any later redraw shows what the page really holds, not only what it last drew
    assert rows(page).count() == 20 and all("低" in t for t in rows(page).all_inner_texts()), "the late answer was ignored"
    assert "已显示 20 条" in page.locator("#fault-count").inner_text()
    assert page.errors == []
