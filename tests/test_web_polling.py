"""修复轮05 (终版 §11, §13.3, §16.2 item 8): the page reads the whole current state once a second.

Real Chrome against the real application (the owner in a server thread, the page's own polling); what goes wrong with
a reading is made to go wrong by intercepting it in the browser.  Why it matters: a screen that shows an old number as
if it were current is worse than a blank one; a display that piles up requests or catches up in a burst is load nobody
asked for; and after anything that goes wrong with a reading, the next good one must put the whole screen right
without a refresh.
"""

import json
import time

import pytest
from browser_support import sign_in

OK = "document.querySelector('#connection').textContent === '实时连接'"
CUT = "document.querySelector('#connection').textContent === '数据中断'"
READING = "**/api/snapshot"
PROCESS_ROW = "#process-table tbody tr"
FORGED = 987654.321  # a load nobody sent: "987,654" on screen would mean a refused reading was used
SECRET_RECORDER = """
window.__secret = 0;
new MutationObserver(() => { if (document.body && document.body.innerText.includes('987,654')) window.__secret++; })
  .observe(document, {subtree: true, childList: true, characterData: true});
"""
VISIBILITY = "Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => window.__visibility || 'visible'});"


def row_of(page, rig, channel):
    return page.locator(PROCESS_ROW, has_text=rig.config.ui.point_labels[channel])


def watch(page):
    """When the page asked for a reading and when one failed in the browser, on the test's monotonic clock.  Events are
    taken while the test waits inside Playwright, so these tests wait with `page.wait_for_timeout`, never `time.sleep`."""
    sent, failed = [], []
    page.on("request", lambda r: sent.append(time.monotonic()) if "/api/snapshot" in r.url else None)
    page.on("requestfailed", lambda r: failed.append(time.monotonic()) if "/api/snapshot" in r.url else None)
    return sent, failed


def until(page, condition, seconds, what):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, what
        page.wait_for_timeout(25)


def own_errors(page):
    """What the page itself reported; the browser's own line for a load that failed is the injected failure."""
    return [e for e in page.errors if not e.startswith("Failed to load resource")]


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


def test_the_page_reads_about_once_a_second_and_never_has_two_readings_outstanding(page, rig):
    sent, _ = watch(page)
    sign_in(page, rig)
    page.wait_for_function(OK)
    first = len(sent)
    page.wait_for_timeout(4000)
    assert 3 <= len(sent) - first <= 5, f"about one reading a second: {len(sent) - first} in 4 s"
    held = []
    page.route(READING, lambda route: held.append(route))  # the next reading gets no answer for now
    until(page, lambda: held, 3, "the page stopped reading")
    page.wait_for_timeout(2500)
    assert len(held) == 1, "while one reading is outstanding no other is sent: nothing piles up"
    page.wait_for_function(CUT, timeout=3000)  # 3 s after the last answer the page says so, the reading still waiting
    held[0].continue_()  # still inside its 5 s: it is answered now (before unrouting, which would let it through itself)
    page.unroute(READING)
    page.wait_for_function(OK, timeout=5000)
    assert own_errors(page) == []


def test_a_reading_that_takes_too_long_is_given_up_at_its_limit_and_the_next_one_follows(page, rig):
    sent, failed = watch(page)
    sign_in(page, rig)
    page.wait_for_function(OK)
    held = []
    page.route(READING, lambda route: held.append((time.monotonic(), route)))
    until(page, lambda: held, 3, "the page stopped reading")
    page.wait_for_function(CUT, timeout=4000)
    assert not failed, "the page says it is cut off while the reading is still waiting: that is intended"
    until(page, lambda: failed, 7, "the reading was never given up")
    waited = failed[0] - held[0][0]
    assert 4.6 <= waited <= 6.0, f"given up at the 5 s limit, not before and not much later: {waited:.2f} s"
    assert len(held) == 1, "nothing else was asked meanwhile"
    page.unroute(READING)
    page.wait_for_function(OK, timeout=5000)
    after = [t for t in sent if t > failed[0]]
    assert after and after[0] - failed[0] <= 1.6, "the next reading follows within an interval"
    assert own_errors(page) == []


def test_three_seconds_without_a_good_reading_take_the_old_values_off_the_screen_until_the_next_one(page, rig):
    sign_in(page, rig)
    rig.call(rig.ua, [("load_mw", 640.0)])
    row = row_of(page, rig, "load_mw")
    row.get_by_text("640").wait_for()
    started = time.monotonic()
    page.route(READING, lambda route: route.fulfill(status=503, body="unavailable"))
    page.wait_for_function(CUT, timeout=6000)
    assert time.monotonic() - started >= 1.8, "one failed reading alone does not raise it"
    assert "640" not in page.locator("#process-table").inner_text(), "an old value is not shown as a current reading"
    said = page.locator("#notice").inner_text()  # what stopped is the page's reading, not the devices; history is apart
    assert page.locator("#notice").is_visible() and "3 秒" in said and "实时读数已停止显示，正在自动重试" in said
    assert "这不代表设备通信状态，历史查询不受此提示影响" in said and "此前的数值" not in said, said
    page.unroute(READING)  # the service is back: no refresh needed
    page.wait_for_function(OK, timeout=5000)
    row.get_by_text("640").wait_for()
    page.locator("#notice").wait_for(state="hidden")
    assert own_errors(page) == []


def test_a_page_that_never_got_a_reading_says_so_three_seconds_after_it_began(page, rig):
    page.route(READING, lambda route: route.abort())  # the network fails from the very first reading on
    sign_in(page, rig)
    began = time.monotonic()
    page.wait_for_function(CUT, timeout=6000)
    assert 2.5 <= time.monotonic() - began <= 5.0, "counted from the start of reading when nothing ever came"
    page.unroute(READING)
    page.wait_for_function(OK, timeout=5000)
    assert own_errors(page) == []


def damaged(kind, real):
    """A reading the backend never sends; where it carries channels, the load reads 987,654 MW."""
    channels = dict(real["channels"], load_mw=dict(real["channels"]["load_mw"], value=FORGED))
    other = next(k for k in channels if k != "load_mw")
    return {
        "unparsable": "{",
        "no channels": json.dumps({k: v for k, v in real.items() if k != "channels"}),
        "a channel without validity": json.dumps(dict(real, channels=dict(channels, **{other: {"kind": "input"}}))),
        "a list": json.dumps([dict(real, channels=channels)]),
    }[kind]


@pytest.mark.parametrize("kind", ["unparsable", "no channels", "a channel without validity", "a list"])
def test_a_reading_that_is_not_a_reading_is_refused_whole_and_does_not_count_as_an_answer(page, rig, kind):
    page.add_init_script(SECRET_RECORDER)
    sign_in(page, rig)
    rig.call(rig.ua, [("load_mw", 650.0)])
    row_of(page, rig, "load_mw").get_by_text("650").wait_for()

    def forge(route):
        route.fulfill(status=200, content_type="application/json", body=damaged(kind, route.fetch().json()))

    page.route(READING, forge)
    page.wait_for_function(CUT, timeout=6000)  # answers came, but none was a reading: the last good one stays the last
    assert page.evaluate("window.__secret") == 0, "nothing of a refused reading was ever on screen"
    page.unroute(READING)
    page.wait_for_function(OK, timeout=5000)
    row_of(page, rig, "load_mw").get_by_text("650").wait_for()
    assert page.evaluate("window.__secret") == 0
    assert own_errors(page) == []


def test_coming_back_to_the_page_reads_at_once_unless_a_reading_is_outstanding(page, rig):
    page.add_init_script(VISIBILITY)
    sent, _ = watch(page)
    sign_in(page, rig)
    page.wait_for_function(OK)
    away, back = ("() => { window.__visibility = '%s'; document.dispatchEvent(new Event('visibilitychange')); }" % s for s in ("hidden", "visible"))
    page.evaluate(away)
    taken = page.evaluate("Number(document.querySelector('#stream-time').dataset.taken)")
    # a reading has just been taken AND applied (not only answered: its body may still be read) - the next regular
    # one is about a second away and none is outstanding
    page.wait_for_function("t => Number(document.querySelector('#stream-time').dataset.taken) > t", arg=taken)
    count = len(sent)
    page.evaluate(back)
    page.wait_for_timeout(300)
    assert len(sent) == count + 1, "back on the page: one reading at once, not at the next tick"
    held = []
    page.route(READING, lambda route: held.append(route))
    until(page, lambda: held, 3, "the page stopped reading")
    count = len(sent)
    page.evaluate(away)
    page.evaluate(back)
    page.wait_for_timeout(300)
    assert len(sent) == count, "a reading is outstanding: coming back adds none"
    held[0].continue_()
    page.unroute(READING)
    page.wait_for_function(OK, timeout=5000)
    assert own_errors(page) == []


def test_a_page_that_was_busy_takes_one_reading_and_does_not_catch_up(page, rig):
    sent, _ = watch(page)
    sign_in(page, rig)
    page.wait_for_function(OK)
    start = time.monotonic()
    page.evaluate("setTimeout(() => { const t = Date.now(); while (Date.now() - t < 4000); }, 50)")  # 4 s of no script
    page.wait_for_timeout(4200)
    page.wait_for_timeout(1000)
    burst = [t for t in sent if start + 4.0 <= t <= start + 5.1]
    assert 1 <= len(burst) <= 2, f"the missed seconds are not made up: {len(burst)} readings right after the pause"
    page.wait_for_function(OK, timeout=5000)


def test_a_stopped_account_is_signed_out_at_its_next_reading(page, rig):
    rig.credentials["gone"] = rig.accounts.create("gone", "viewer")
    sign_in(page, rig, "gone")
    page.wait_for_function(OK)
    rig.accounts.revoke("gone")
    page.locator("#login").wait_for(state="visible", timeout=5000)
    assert "停用" in page.locator("#login-error").inner_text()
