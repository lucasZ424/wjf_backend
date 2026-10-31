"""修复轮13: what the pages say and how they show it (终版 §17.5, §17.6; 修复轮12 第1、2、8条; 优化方案第二版).

Real Chrome against the running application.  Why it matters: the operator reads these pages as a finished product.  A
word that says "research" or "simulation", a stray English code or a separator dot makes them read like a prototype; a
number whose decimals come and go cannot be read at a glance; and a page that stops drawing, keeps an old configuration
or piles up requests must say so instead of looking current.
"""

import json
import re
import time

from browser_support import sign_in

NAMES = ["系统总览", "水冷壁应变场", "热应力分析", "历史查询", "系统自检", "通信与日志", "系统管理与权限"]
BANNED = ["科研", "实验", "试验", "研究", "模拟", "演示", "·"]
READING, HEALTH, CONFIG = "**/api/snapshot", "**/api/health", "**/api/ui-config"
GIB = 2**30


def readable(page):
    """What a person can read: the text without the codes kept beside it, every option of every list, the title and
    the labels the maps draw for themselves."""
    return page.evaluate(
        """() => { const shown = [], walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          for (let n = walk.nextNode(); n; n = walk.nextNode()) {
            const e = n.parentElement;
            if (n.textContent.trim() && !e.closest('code') && e.checkVisibility()) shown.push(n.textContent.trim());
          }
          return [document.title, ...shown, ...[...document.querySelectorAll('option')].map(o => o.textContent),
                  ...[...document.querySelectorAll('.map')].map(m => getComputedStyle(m, '::before').content)].join('\\n'); }"""
    )


def wording_faults(text, title=""):
    text = text.replace(title, "") if title else text  # 终版 §17.5: the simulation site says so in its own title only
    codes = set(re.findall(r"\b[A-Z][A-Z_]{3,}\b", text)) - {"HTSG"}
    return [w for w in BANNED if w in text] + sorted(codes)


def rewrite(page, pattern, change):
    """Every answer to `pattern` passes through `change(body)` first."""

    def through(route):
        response = route.fetch()
        body = response.json()
        change(body)
        route.fulfill(response=response, body=json.dumps(body))

    page.route(pattern, through)


def test_no_page_says_research_simulation_or_trial_and_none_joins_words_with_a_dot(page, rig):
    page.goto(rig.url)
    page.locator("#login-form").wait_for()
    assert wording_faults(readable(page)) == [], "the login page"
    sign_in(page, rig, "admin")
    title = rig.config.project.title
    assert title.endswith("（模拟数据）"), "the simulation site names itself, so its data is not taken for the plant's"
    assert [t.strip() for t in page.locator("#navigation button").all_inner_texts()] == NAMES
    for name in NAMES:
        page.get_by_role("button", name=name).click()
        page.wait_for_timeout(1500)  # a reading, and the page's own reads
        assert wording_faults(readable(page), title) == [], name
    # this rig has no archive: the history and the management records answer 404 / 503, and say so on the page
    assert [e for e in page.errors if not e.startswith("Failed to load resource")] == []


def test_readings_keep_a_fixed_number_of_decimals_by_unit_and_no_thousands_separator(page, rig):
    sign_in(page, rig)
    process = page.locator("#process-table")
    process.get_by_text("MW").first.wait_for()
    text = process.inner_text()
    for shown in ["600.0 MW", "1850 t/h", "2117 t/h", "28.70 MPa", "309.0 ℃", "-0.050 kPa"]:
        assert shown in text, (shown, text)  # 终版 §17.6: 1 / 0 / 2 / 1 / 3 decimals
    assert "1,850" not in text
    strain = page.locator("#strain-table").inner_text()
    assert re.search(r"\b0\.7\d{3} mV/V", strain), "the bridge signal with four decimals"
    assert re.search(r"(?<![\d.])\d+ με", strain) and not re.search(r"\d\.\d+ με", strain), "strain in whole microstrain"


def test_reloading_stays_on_the_page_in_front_and_signing_out_forgets_it(page, rig):
    sign_in(page, rig)
    page.get_by_role("button", name="系统自检").click()
    page.reload()
    page.locator("#workspace").wait_for(state="visible")
    assert page.locator(".page-heading h2").inner_text() == "系统自检"
    page.locator("#logout").click()
    sign_in(page, rig)
    assert page.locator(".page-heading h2").inner_text() == "系统总览"


def test_badges_stay_on_one_line_cards_stand_apart_and_a_scrolled_table_keeps_its_header(page, rig):
    sign_in(page, rig)
    assert page.evaluate("getComputedStyle(document.querySelector('#connection')).whiteSpace") == "nowrap"
    gaps = "() => { const c = [...document.querySelectorAll('#workspace > .card')].map(e => e.getBoundingClientRect()); return c.slice(1).map((r, i) => r.top - c[i].bottom); }"
    page.get_by_role("button", name="系统自检").click()
    page.locator("#check-timing table").wait_for()
    assert min(page.evaluate(gaps)) >= 12, "the retention and the timing cards"
    page.get_by_role("button", name="通信与日志").click()
    box = page.locator("#comm-inputs")
    box.locator("table").wait_for()
    assert min(page.evaluate(gaps)) >= 12, "the three cards of the communication page"
    box.evaluate("e => e.scrollTop = 400")
    page.wait_for_timeout(100)
    top, head = box.evaluate("e => [e.getBoundingClientRect().top, e.querySelector('th').getBoundingClientRect().top]")
    assert abs(top - head) < 2, "the header row stays at the top of the scrolled table"


def test_the_self_check_says_where_new_data_goes_and_how_full_each_volume_is(page, rig):
    disk = {
        "measured_at": time.time(), "cleanup_start": 0.9, "cleanup_stop": 0.8, "emergency_start": 0.95, "emergency_reserve_bytes": 100 * GIB,
        "primary": {"total": 2000 * GIB, "free": 1160 * GIB, "used_fraction": 0.42}, "emergency": None,
    }  # fmt: skip
    retention = {"databases": {}, "last_deletion": None, "deletion_period_s": None, "forecast": None, "emergency_route": False,
                 "alarm": {"active": False, "above_seconds": None, "reasons": []}, "disk": disk}  # fmt: skip
    rewrite(page, HEALTH, lambda body: body["archive"].update(routing="PRIMARY", target="PRIMARY_PROVISIONAL", retention=retention))
    sign_in(page, rig)
    page.get_by_role("button", name="系统自检").click()
    archive = page.locator("#check-archive")
    archive.get_by_text("临时").wait_for()
    assert "未核对" in archive.inner_text(), "终版 §13.5: the mode says PRIMARY, but nothing has checked it"
    volumes = page.locator("#check-retention")
    volumes.get_by_text("磁盘占用").wait_for()
    text = volumes.inner_text()
    assert "42.0%" in text and "1160 GB" in text and "2000 GB" in text, text
    assert "无法读取" in text, "the emergency volume could not be read: said, not left blank"
    assert all(x in text for x in ["90%", "80%", "95%", "100 GB"]), "the lines the volumes are judged by"


def test_writing_to_the_emergency_database_or_waiting_is_said_on_every_page(page, rig):
    target = {"now": "EMERGENCY"}
    rewrite(page, READING, lambda body: body.update(archive_target=target["now"]))
    sign_in(page, rig)
    banner = page.locator("#archive-banner")
    banner.get_by_text("应急库").wait_for()
    for name in NAMES:
        page.get_by_role("button", name=name).click()
        assert "应急库" in banner.inner_text(), f"终版 §13.6 进入应急立即提示, on {name}"
    target["now"] = "UNKNOWN"
    banner.get_by_text("等待写入").wait_for()
    target["now"] = "PRIMARY"
    banner.wait_for(state="hidden")


def test_a_page_that_cannot_draw_a_reading_says_so_instead_of_looking_current(page, rig):
    sign_in(page, rig)
    connection = page.locator("#connection")
    connection.get_by_text("实时连接").wait_for()
    rewrite(page, READING, lambda body: body["status"]["sources"].update(broken=None))  # a reading the page cannot draw
    connection.get_by_text("显示出错").wait_for()
    assert "页面显示出错" in page.locator("#notice").inner_text()
    page.unroute(READING)
    connection.get_by_text("实时连接").wait_for()
    page.locator("#notice").wait_for(state="hidden")


def test_a_backend_restarted_with_another_configuration_asks_for_a_reload(page, rig):
    sign_in(page, rig)
    run = {"id": None}
    rewrite(page, READING, lambda body: body.update(run_id=run["id"]) if run["id"] else None)
    run["id"] = "restarted-same-configuration"
    page.wait_for_timeout(2500)
    assert page.locator("#notice").is_hidden(), "a new run with the same configuration needs nothing"
    rewrite(page, CONFIG, lambda body: body.update(config_id="another-configuration"))
    run["id"] = "restarted-another-configuration"
    page.locator("#notice").get_by_text("刷新").wait_for()
    assert "配置已更改" in page.locator("#notice").inner_text()


def test_the_health_reading_is_one_at_a_time_and_given_up_at_its_limit(page, rig):
    held, failed = [], []
    page.route(HEALTH, lambda route: held.append(route))  # never answered
    page.on("requestfailed", lambda request: failed.append(request.url) if "/api/health" in request.url else None)
    sign_in(page, rig)
    page.get_by_role("button", name="系统自检").click()
    most, deadline = 0, time.monotonic() + 12
    while time.monotonic() < deadline:
        page.wait_for_timeout(200)
        most = max(most, len(held) - len(failed))
    assert most == 1, "an unanswered reading is never joined by another"
    assert 2 <= len(held) <= 3, "given up after 5 s and tried again, not piled up every 5 s"
    page.locator("#check-negotiated").get_by_text("超过").wait_for()


def test_moving_the_mouse_over_the_trend_redraws_it_at_most_once_a_frame(page, rig):
    sign_in(page, rig)
    page.get_by_role("button", name="热应力分析").click()
    page.wait_for_function("document.querySelector('#trend-canvas') && document.querySelector('#trend-canvas').plot")
    drawn = page.evaluate(
        """async () => {
          const c = document.querySelector('#trend-canvas'), r = c.getBoundingClientRect();
          let n = 0, plot = c.plot;
          Object.defineProperty(c, 'plot', {configurable: true, get: () => plot, set: v => { n++; plot = v; }});
          for (let i = 0; i < 40; i++) c.dispatchEvent(new MouseEvent('mousemove', {clientX: r.left + 100 + i, clientY: r.top + 50}));
          await new Promise(go => requestAnimationFrame(() => requestAnimationFrame(go)));
          return n;
        }"""
    )
    assert 1 <= drawn <= 2, f"40 moves drew the chart {drawn} times"


def test_the_trend_axis_marks_whole_minutes(page, rig):
    sign_in(page, rig)
    page.get_by_role("button", name="热应力分析").click()
    page.wait_for_function("document.querySelector('#trend-canvas') && document.querySelector('#trend-canvas').plot")
    ticks = page.evaluate("document.querySelector('#trend-canvas').plot.ticks")
    assert len(ticks) >= 4, ticks
    assert all(t["t"] % 60 == 0 and re.fullmatch(r"\d\d:\d\d", t["label"]) for t in ticks), ticks


def test_a_table_whose_content_did_not_change_is_not_written_again(page, rig):
    sign_in(page, rig)
    page.locator("#process-table table").wait_for()
    page.evaluate("document.querySelector('#process-table table').dataset.kept = 'yes'")
    before = page.evaluate("Number(document.querySelector('#stream-time').dataset.taken)")
    page.wait_for_function("t => Number(document.querySelector('#stream-time').dataset.taken) > t + 1.5", arg=before)
    assert page.evaluate("document.querySelector('#process-table table').dataset.kept") == "yes", "same content: the same table"
    rig.call(rig.ua, [("load_mw", 601.0)])
    try:
        page.locator("#process-table").get_by_text("601.0 MW").wait_for()
        assert page.evaluate("document.querySelector('#process-table table').dataset.kept") is None, "new content is shown"
    finally:
        rig.call(rig.ua, [("load_mw", 600.0)])


def test_a_backend_that_cannot_be_reached_is_said_in_words_not_by_the_browser(page, rig):
    page.goto(rig.url)
    page.route("**/api/me", lambda route: route.abort())
    page.locator("#login-user").fill("viewer")
    page.locator("#login-secret").fill("viewer")
    page.locator("#login-form button").click()
    page.locator("#login-error").get_by_text("连不上后端").wait_for()
    assert "fetch" not in page.locator("#login-error").inner_text().lower()
