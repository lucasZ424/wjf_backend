"""R07 end to end: a bridge that fails and comes back reads the same on the live page, the history page and in the file.

Real Chrome against the running application, with its archive: the owner commits to real databases, the page asks them
through the API.  The wire itself (a real acquirer against real slaves) is `test_failure_records.py`; this checks what the
failure looks like from the owner onward.  Why it matters: a value that stops being valid must stop there in all three
places - the live page says "no value", the history ends the earlier value at the failed round and lists the failure as
a record of its own, and the CSV a person takes away says the same without a number - and a failure that goes on for
many rounds is ONE moment, not one row per round.

The failure used here is a protocol exception answer: the device answers, so the source stays valid however long it
goes on (a whole-round timeout lets the source expire after 3 s, and expiry has its own records; that is not under test).
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from browser_support import sign_in
from live_support import BRIDGES
from owner_support import poll
from test_web_export import export
from web_rig import MB

FOCUS = BRIDGES[0]
SHANGHAI = timezone(timedelta(hours=8))  # the browsers of these tests run in Asia/Shanghai (conftest `page`)
MISSING = "MISSING_EXCEPTION"
BLOCK = '#strain-map .block[data-station="S01"]'  # the station of the first bridge


@pytest.fixture(scope="module")
def failing_rig(tmp_path_factory):
    from web_rig import WebRig

    with WebRig(tmp_path_factory.mktemp("web"), archive=True, steady=False, run_id="r07") as running:
        yield running


def good(rig):
    rig.call(rig.round)


def fail(rig):
    def run():
        rig.tick_no += 1
        now, mono = time.time(), time.monotonic()
        return rig.owner.accept(poll(rig.tick_no, {}, source=MB, outcome="EXCEPTION", wall=now, mono=mono))

    rig.call(run)


def wait(condition, why, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out: {why}"
        time.sleep(0.05)


def settled(rig):
    """Everything the owner offered is in the databases."""
    return rig.call(lambda: rig.queue.depth == 0 and not any(s.inflight for s in rig.owner.sources.values()))


def stored(rig, point):
    with rig.router.primary.session() as conn:
        return conn.execute(
            "SELECT quality,value,received_wall FROM observation WHERE point_id=%s ORDER BY received_wall,batch_seq,item_seq",
            (point,),
        ).fetchall()


def local(seconds):
    """The centre for the page's date field.  Chrome writes a value on a whole minute without its seconds, and the field then
    refuses a text that has them; the window is two minutes wide, so one second more or less does not matter."""
    moment = int(seconds) + (1 if int(seconds) % 60 == 0 else 0)
    return datetime.fromtimestamp(moment, SHANGHAI).strftime("%Y-%m-%dT%H:%M:%S")


def seconds_of(text):
    return datetime.fromisoformat(text).timestamp()


def ask(page, center, seconds, limit_note=None):
    page.locator("#replay-center").fill(center)
    page.locator("#replay-center").dispatch_event("change")
    page.locator("#replay-seconds").fill(str(seconds))
    page.locator("#replay-seconds").dispatch_event("change")
    page.locator("#replay-points").select_option([FOCUS])
    page.locator("#replay-run").click()
    if limit_note:
        page.wait_for_function(f"document.querySelector('.note')?.textContent.includes('{limit_note}')")
    else:
        page.locator("#replay-meta").wait_for()
        page.locator("#replay-records tbody tr").first.wait_for()


def test_a_bridge_that_fails_and_comes_back_is_the_same_moment_on_the_live_page_the_history_page_and_in_the_file(
    page, failing_rig, tmp_path
):
    rig = failing_rig
    sign_in(page, rig)
    page.get_by_role("button", name="水冷壁应变场").click()
    good(rig)
    time.sleep(1.1)
    good(rig)
    time.sleep(1.1)
    page.wait_for_function(f"document.querySelector('{BLOCK}')?.dataset.state === 'normal'")

    # -- the failure: the live page says "no value" and why, the first round and the rounds after it
    fail(rig)
    page.wait_for_function(f"document.querySelector('{BLOCK}')?.dataset.state === 'invalid'")
    block = page.locator(BLOCK)
    assert block.locator("strong").inner_text() == "—" and "本轮协议异常" in block.inner_text(), "no value, and the reason of the round"
    for _ in range(3):  # four failed rounds in all
        time.sleep(1.1)
        fail(rig)
    assert page.locator(BLOCK).get_attribute("data-state") == "invalid"
    time.sleep(1.1)
    good(rig)
    page.wait_for_function(f"document.querySelector('{BLOCK}')?.dataset.state === 'normal'")
    wait(lambda: settled(rig), "the last round reaches the databases")

    # -- the archive: one failure record per bridge for four failed rounds, and the value of the bridge before it is intact
    with rig.router.primary.session() as conn:
        counts = conn.execute(
            "SELECT point_id,count(*) AS n FROM observation WHERE quality LIKE 'MISSING\\_%' GROUP BY point_id"
        ).fetchall()
    assert sorted(x["point_id"] for x in counts) == sorted(BRIDGES) and {x["n"] for x in counts} == {1}, counts
    series = stored(rig, FOCUS)
    qualities = [x["quality"] for x in series]
    at = qualities.index(MISSING)
    assert qualities[:at] and set(qualities[:at]) == {"GOOD"} and qualities[at + 1 :] == ["GOOD"], qualities
    failed_at, came_back_at = series[at]["received_wall"], series[at + 1]["received_wall"]
    assert series[at]["value"] is None

    # -- the history page: the failure is a record without a number, and the value before it holds only up to it
    page.get_by_role("button", name="历史查询").click()
    page.locator("#replay-settings").wait_for()
    ask(page, local(failed_at), 120)
    cells = [row.locator("td").all_inner_texts() for row in page.locator("#replay-records tbody tr").all()]
    classes = [row.get_attribute("class") for row in page.locator("#replay-records tbody tr").all()]
    words = {"GOOD": "良好", MISSING: "未取得（本轮协议异常）"}  # 终版 §17.5: the page names them, the file keeps the codes
    assert [c[4] for c in cells] == [words[q] for q in qualities], "the page lists the records the archive holds, failure included"
    assert cells[at][3] == "—" and "bad" in classes[at].split(), "the failure has no number and is marked unusable"
    assert cells[at - 1][8].startswith("至下一次变化"), "the value before it holds to the next record, not on to the segment end"

    # -- the file says the same, without a number, and its hold ends are the same instants
    _, (meta, header, rows) = export(page, tmp_path)
    col = {name: header.index(name) for name in header}
    assert len(rows) == len(cells) and [r[col["质量/状态"]] for r in rows] == qualities
    failure, before, after = rows[at], rows[at - 1], rows[at + 1]
    assert (failure[col["值"]], failure[col["是否有效"]], failure[col["来源"]]) == ("", "否", "实收")
    assert abs(float(failure[col["生效时间(UTC秒)"]]) - failed_at) < 1e-3
    assert before[col["终止原因"]] == "至下一次变化" and abs(seconds_of(before[col["有效至"]]) - failed_at) < 2e-3
    assert abs(seconds_of(failure[col["有效至"]]) - came_back_at) < 2e-3, "the failure lasts until the bridge answers again"
    assert after[col["值"]] != "" and after[col["是否有效"]] == "是"

    # -- the same, with a row limit that leaves the failure out: the last value shown must still end where the failure began
    shown = at  # the rows before the failure fit, the failure is the first one left out
    page.route("**/api/replay*", lambda route: route.continue_(url=route.request.url + f"&limit={shown}"))
    ask(page, local(failed_at), 120, limit_note=f"每通道 {shown} 行")
    _, (meta, header, limited) = export(page, tmp_path)
    assert len(limited) == shown and [r[col["质量/状态"]] for r in limited] == ["GOOD"] * shown, "the failure is not shown"
    last = limited[-1]
    assert last[col["终止原因"]] == "至下一次变化" and abs(seconds_of(last[col["有效至"]]) - failed_at) < 2e-3, (
        "held to the failure that is left out, not on to the segment end"
    )
    assert {m[0]: m[1] for m in meta if len(m) == 2}["是否达到行数上限"] == "是"
    assert page.errors == []
