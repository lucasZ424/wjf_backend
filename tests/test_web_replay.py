"""D40: replay settings, query and display (终版 §14).

Real Chrome against the real application; the archive holds the Capstone F scenario, so every number on the page
can be checked against what the script put in: a deliberate archive gap, a quality-only change, three versions at
one instant, and a value restated after the gap.
"""

import json

import pytest
from browser_support import sign_in
from test_capstone_f import VALUES
from test_history_merge import Offline

CENTER = "1970-01-01T08:17:10"  # wall 1030 seen from Asia/Shanghai
KEY = "htsg.replay"


@pytest.fixture(autouse=True)
def faults(replay_rig):
    if not getattr(replay_rig, "faults_seeded", False):
        replay_rig.seed_faults([("temp_inner_06", "HIGH", 1020.0, 500.0)])
        replay_rig.faults_seeded = True


def open_history(page, rig):
    sign_in(page, rig)
    page.get_by_role("button", name="历史查询").click()
    page.locator("#replay-settings").wait_for()


def configure(page, center, seconds, points):
    page.locator("#replay-center").fill(center)
    page.locator("#replay-center").dispatch_event("change")
    page.locator("#replay-seconds").fill(str(seconds))
    page.locator("#replay-seconds").dispatch_event("change")
    page.locator("#replay-points").select_option(points)


def query(page, center=CENTER, seconds=100, points=("temp_inner_06",)):
    configure(page, center, seconds, list(points))
    page.locator("#replay-run").click()
    page.locator("#replay-meta").wait_for()


def stored(page):
    return page.evaluate(f"sessionStorage.getItem('{KEY}')")


def record_rows(page):
    return page.locator("#replay-records tbody tr")


def pixels(page, t0, t1):
    """Counts cyan / red / amber pixels of the chart between two instants (the geometry comes from the page)."""
    return page.evaluate(
        """([t0, t1]) => {
          const c = document.querySelector('#replay-chart'), p = c.plot, g = c.getContext('2d'), r = c.width / c.clientWidth;
          const x = t => p.left + (t - p.start) / (p.end - p.start) * (p.right - p.left);
          const a = Math.round((x(t0) + 3) * r), b = Math.round((x(t1) - 3) * r);
          const d = g.getImageData(a, Math.round(p.top * r), b - a, Math.round((p.bottom - p.top) * r)).data;
          const out = {cyan: 0, red: 0, amber: 0};
          for (let i = 0; i < d.length; i += 4) {
            const [R, G, B, A] = [d[i], d[i + 1], d[i + 2], d[i + 3]];
            if (A > 200 && Math.abs(R - 115) < 45 && Math.abs(G - 188) < 45 && B > 200) out.cyan++;
            else if (A > 200 && R > 220 && G < 150) out.red++;
            else if (A > 20 && A < 120 && R > 230 && G > 170 && G < 205) out.amber++;
          }
          return out;
        }""",
        [t0, t1],
    )


def test_nothing_is_asked_for_until_settings_exist(page, replay_rig):
    seen = []
    page.on("request", lambda r: seen.append(r.url) if "/api/replay" in r.url else None)
    open_history(page, replay_rig)
    assert "不会自动查询当前时刻" in page.locator("#replay-result").inner_text()
    page.reload()
    page.get_by_role("button", name="历史查询").click()
    page.wait_for_timeout(700)
    assert seen == [], "no settings: no query, and above all no default 'now' window"


def test_only_settings_are_kept_and_they_survive_a_reload_but_not_a_clear_or_a_logout(page, replay_rig):
    open_history(page, replay_rig)
    query(page, points=("temp_inner_06", "temp_inner_07"))
    kept = json.loads(stored(page))
    assert set(kept) == {"fault", "center", "seconds", "points", "chart"}, "settings only - no data"
    assert kept["center"] == 1030 and kept["seconds"] == 100 and kept["points"] == ["temp_inner_06", "temp_inner_07"]
    assert len(stored(page)) < 600
    page.reload()  # the sign-in and the settings are the tab's; the page asks the database again by itself
    page.get_by_role("button", name="历史查询").click()
    page.locator("#replay-meta").wait_for()
    assert page.locator("#replay-center").input_value() == CENTER and page.locator("#replay-seconds").input_value() == "100"
    assert page.locator("#replay-summary tbody tr").count() == 2
    page.locator("#replay-clear").click()
    assert stored(page) is None and page.locator("#replay-center").input_value() == ""
    assert "不会自动查询" in page.locator("#replay-result").inner_text()
    query(page)
    assert stored(page) is not None
    page.locator("#logout").click()
    assert stored(page) is None and page.evaluate("sessionStorage.getItem('htsg.credential')") is None
    assert page.errors == []


def test_the_value_in_effect_at_the_window_start_is_shown_with_its_original_time_and_identity(page, replay_rig):
    open_history(page, replay_rig)
    query(page, center="1970-01-01T08:17:13", seconds=4, points=("temp_outer_01",))  # [1031, 1035], no change inside
    basis = page.locator("#replay-summary tbody tr").inner_text()
    assert "440" in basis and "状态重申" in basis and "1970/1/1 08:16:41" in basis, "restated; observed at 1001"
    (row,) = record_rows(page).all()
    cells = row.locator("td").all_inner_texts()
    assert "held" in row.get_attribute("class")
    assert cells[0].startswith("1970/1/1 08:17:09"), "in effect from the restatement at 1029, not from the window"
    assert cells[1].startswith("1970/1/1 08:16:41") and cells[5] == "状态重申"
    assert "窗口起点的保持值" in cells[8] and "至有效段结束" in cells[8] and "08:17:16" in cells[8]


def test_a_gap_is_shaded_and_listed_and_nothing_is_drawn_or_invented_inside_it(page, replay_rig):
    open_history(page, replay_rig)
    query(page, points=("temp_inner_07", "bridge_mvv_01"))
    notes = page.locator(".note").all_inner_texts()
    assert any("ua_field" in n and "08:17:07" in n and "08:17:09" in n and "QUEUE_FULL" in n for n in notes), notes
    assert any("mb_field" in n and "08:17:06" in n for n in notes)
    page.locator("#replay-focus").select_option("temp_inner_07")
    page.locator("#replay-records tbody tr").first.wait_for()
    got = [c.locator("td").all_inner_texts() for c in record_rows(page).all()]
    assert [c[5] for c in got] == ["实收", "状态重申", "状态重申"]  # 1001, restated after the gap at 1029 and 1047
    assert not [c for c in got if c[5] == "实收" and c[3] == "479"], "the sample lost in the gap is not invented"
    inside = pixels(page, 1027.0, 1029.0)
    assert inside["cyan"] == 0 and inside["amber"] > 100, "shaded gap, no line through it"
    assert pixels(page, 1005.0, 1025.0)["cyan"] > 50 and pixels(page, 1031.0, 1034.0)["cyan"] > 5, "drawn on both sides"


def test_a_bad_quality_interval_is_marked_red_and_borrows_no_value(page, replay_rig):
    open_history(page, replay_rig)
    query(page, points=("temp_inner_06",))
    every = [(r.get_attribute("class"), r.locator("td").all_inner_texts()) for r in record_rows(page).all()]
    got = [c for c in every if c[1][5] == "实收"]  # what was received; restatements after the gap and the clock step follow
    assert [c[1][4].split()[0] for c in got] == ["GOOD", "BAD", "GOOD"]
    assert [c[1][5] for c in every[3:]] == ["状态重申", "状态重申"]
    assert "bad" in got[1][0] and got[1][1][3] == "—" and "0x80350000" in got[1][1][4]
    assert got[0][1][3] == got[2][1][3] == f"{VALUES['temp_inner_06']:g}", "same number, three separate records"
    assert "含无效或极值记录" in page.locator("#replay-summary").inner_text()
    between = pixels(page, 1020.0, 1022.0)
    assert between["red"] > 5 and between["cyan"] == 0, "the Bad interval is not filled with the earlier Good value"


def test_versions_at_one_instant_are_all_shown_in_batch_order(page, replay_rig):
    open_history(page, replay_rig)
    query(page, points=("temp_inner_05",))
    every = [r.locator("td").all_inner_texts() for r in record_rows(page).all()]
    got = [c for c in every[1:] if c[5] == "实收"]  # after the first value; the restatements come later
    assert [c[3] for c in got] == ["481", "482", "483"]
    assert len({c[0] for c in got}) == 1, "one instant, three rows"
    assert [c[2].split("·")[1] for c in got] == ["0", "1", "2"]


def test_reaching_the_row_limit_is_announced_and_never_hidden(page, replay_rig):
    page.route("**/api/replay*", lambda route: route.continue_(url=route.request.url + "&limit=3"))
    open_history(page, replay_rig)
    query(page, points=("bridge_mvv_01",))
    assert "每通道 3 行" in page.locator(".note").first.inner_text()
    assert "截断" in page.locator("#replay-summary").inner_text()
    assert record_rows(page).count() <= 3


def test_an_unreadable_database_makes_the_answer_incomplete_and_the_coverage_unknown(page, replay_rig):
    emergency = replay_rig.router.emergency
    replay_rig.router.emergency = Offline(emergency)
    try:
        open_history(page, replay_rig)
        query(page)
        assert "emergency" in page.locator(".note").first.inner_text() and "不完整" in page.locator(".note").first.inner_text()
        assert "覆盖终点未知" in page.locator("#replay-meta").inner_text()
    finally:
        replay_rig.router.emergency = emergency


def test_switching_the_chart_channel_asks_the_database_again_and_the_newest_window_wins(page, replay_rig):
    seen = []
    held = []
    page.on("request", lambda r: seen.append(r.url) if "/api/replay" in r.url else None)
    open_history(page, replay_rig)
    query(page, points=("temp_inner_06", "temp_inner_07"))
    before = len(seen)
    page.locator("#replay-focus").select_option("temp_inner_07")
    page.locator("#replay-records tbody tr").first.wait_for()
    assert len(seen) == before + 1 and json.loads(stored(page))["chart"] == "temp_inner_07"
    # a slow answer for an old window must not replace the answer for the new one
    page.route("**/api/replay*", lambda route: held.append(route) if "seconds=100" in route.request.url and not held else route.continue_())
    page.locator("#replay-run").click()  # window of 100 s: held in flight
    page.locator("#replay-seconds").fill("50")
    page.locator("#replay-seconds").dispatch_event("change")
    page.locator("#replay-run").click()
    page.wait_for_function("document.querySelector('#replay-meta') && document.querySelector('#replay-meta').textContent.includes('50 秒')")
    assert held, "the first request was really in flight"
    held[0].continue_()
    page.wait_for_timeout(700)
    assert "50 秒" in page.locator("#replay-meta").inner_text() and "100 秒" not in page.locator("#replay-meta").inner_text()


def test_choosing_a_fault_runs_the_replay_around_it(page, replay_rig):
    open_history(page, replay_rig)
    page.locator("#fault-list .fault-row").first.click()
    page.locator("#replay-meta").wait_for()
    assert "中心 1970/1/1 08:17:00" in page.locator("#replay-meta").inner_text()
    assert page.locator("#replay-summary tbody tr").count() == 1
    assert json.loads(stored(page))["fault"]["id"].startswith("seed/fault/")
