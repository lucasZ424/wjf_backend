"""D44e: the communication page in a real browser (终版 §15.1, 方案 D44e).

Real Chrome against the running application.  Why it matters: this page is read when a source is quiet.  It must show
the reading the owner really holds (never a value from one place and a time from another), must not look fresher than
it is, must not leave the operator believing "nothing happened" because the log was empty or trimmed, and must stop
asking the server the moment it is no longer in front.
"""

import json
import time

import pytest
from browser_support import keep, sign_in
from web_rig import MB, UA, WebRig, nominal

from htsg import records as r

NAMES = ["系统总览", "水冷壁应变场", "热应力分析", "历史查询", "系统自检", "通信·日志", "系统管理·权限"]
HEALTH = "**/api/health"


@pytest.fixture
def fresh(tmp_path):
    """A rig of its own, for tests that leave counts and log entries behind."""
    with WebRig(tmp_path) as running:
        yield running


def open_page(page, rig, who="viewer"):
    sign_in(page, rig, who)
    page.get_by_role("button", name="通信·日志").click()
    page.locator("#comm-inputs tbody tr").first.wait_for()


def row_of(page, point):
    return page.locator("#comm-inputs tbody tr", has=page.locator("small", has_text=point)).first


def cells(row):
    return [c.strip() for c in row.locator("td").all_inner_texts()]


def health_json(page, rig):
    response = page.request.fetch(rig.url + "/api/health", headers={"Authorization": "Bearer " + rig.credentials["viewer"]})
    return response.json()


def shown(page, seconds):
    """How the page writes a time, so a test can look for it."""
    return page.evaluate("t => new Date(t * 1000).toLocaleString('zh-CN', {hour12: false})", seconds)


# -------------------------------------------------------------------------------------------------- the page


@pytest.mark.parametrize("who", ["viewer", "admin"])
def test_the_page_is_the_sixth_after_self_check_has_no_page_number_and_both_roles_read_it(page, rig, who):
    sign_in(page, rig, who)
    shown_names = [t.replace(" ", "").replace("\n", "") for t in page.locator("#navigation button").all_inner_texts()]
    assert shown_names == NAMES and not any(c.isdigit() for t in shown_names for c in t), "known by its title only"
    page.get_by_role("button", name="通信·日志").click()
    assert page.locator(".page-heading h2").inner_text() == "通信·日志"
    page.locator("#comm-inputs tbody tr").first.wait_for()
    inputs = [p for p in rig.config.points]
    assert page.locator("#comm-inputs tbody tr").count() == len(inputs), "every input has its row"
    for card in ("数据源状态", "点位映射与实际读数", "通信日志（本次运行）"):
        assert page.locator("h3", has_text=card).count() == 1
    assert "本次运行" in page.locator("main").inner_text() and "不读取归档" in page.locator("main").inner_text()
    assert page.errors == []


def test_it_reads_at_once_then_every_five_seconds_and_only_while_it_is_in_front(page, rig):
    requests = []
    page.on("request", lambda request: requests.append(time.monotonic()) if request.url.endswith("/api/health") else None)
    sign_in(page, rig)
    assert requests == [], "no page that needs the health reading is in front yet"

    def enter():
        before = len(requests)
        page.get_by_role("button", name="通信·日志").click()
        page.wait_for_timeout(1000)  # not "by the next poll": the 5 s clock has its own phase, the reading is taken on entering
        assert len(requests) >= before + 1, "read at once on entering"

    enter()
    page.locator("#comm-inputs tbody tr").first.wait_for()
    page.wait_for_timeout(10000)
    assert 3 <= len(requests) <= 4, f"then every 5 s: {len(requests)} reads in about 11 s"
    for _ in range(3):  # the 5 s clock runs on its own phase; entering must not wait for it
        page.get_by_role("button", name="系统总览").click()
        before = len(requests)
        page.wait_for_timeout(6000 if _ == 0 else 1200)
        assert len(requests) == before, "away from the page, nothing is asked"
        enter()
    assert page.errors == []


def test_each_row_shows_its_mapping_value_quality_validity_and_its_two_times(page, fresh):
    pid = next(p.id for p in fresh.config.points if p.source == UA and "temp_inner" in p.id)
    stamp = 1_700_000_000.0  # as the server gave it
    gen = fresh.call(lambda: fresh.owner.sources[UA].conn_gen)
    fresh.call(lambda: fresh.owner.accept(
        r.InputBatch(UA, gen, r.KIND_UA_NOTIFY, time.time(), time.monotonic(), (r.Item(pid, 481.25, r.GOOD, 0, stamp),), publish_seq=9_000)
    ))  # fmt: skip
    obs = fresh.call(lambda: fresh.owner.points[pid].obs)
    open_page(page, fresh)
    row = cells(row_of(page, pid))
    point = next(p for p in fresh.config.points if p.id == pid)
    assert point.node_id in row[2] and row[1] == UA
    assert row[3].startswith("481.25") and row[4] == "GOOD" and row[5] == "有效"
    assert row[6].startswith(shown(page, obs.received_wall)), "the time the backend received it"
    assert row[7].startswith(shown(page, stamp)) and row[7] != row[6], "the server's own timestamp is a separate column"
    bridge = next(p for p in fresh.config.points if p.source == MB)
    bridge_row = cells(row_of(page, bridge.id))
    assert f"寄存器 {bridge.address}" in bridge_row[2] and bridge_row[2].startswith("设备"), "Modbus: register, not a NodeId"
    assert bridge_row[7] == "未知", "Modbus has no source timestamp: unknown, nothing is made up"
    assert bridge_row[6] != "—" and bridge_row[3] != "—"
    # a Bad value stays visible with its full status code, marked unusable
    fresh.call(fresh.ua, [(pid, 470.0, r.BAD, 0x80000000)])
    page.get_by_role("button", name="系统总览").click()
    page.get_by_role("button", name="通信·日志").click()
    page.locator("#comm-inputs tbody tr").first.wait_for()
    bad = cells(row_of(page, pid))
    assert bad[4] == "BAD 0x80000000" and bad[3].startswith("470") and bad[5] != "有效"
    assert page.errors == []


def test_the_table_is_replaced_by_each_reading_and_never_patched_by_the_live_stream(page, fresh):
    """S3: values keep changing on the live stream, yet the table holds one reading until the next poll replaces it."""
    pid = next(p.id for p in fresh.config.points if p.source == UA and "temp_inner" in p.id)
    open_page(page, fresh)
    page.evaluate(
        """pid => {
            window.__seen = [];
            const read = () => { for (const tr of document.querySelectorAll('#comm-inputs tbody tr'))
                if (tr.querySelector('small')?.textContent === pid) return tr.children[3].textContent; return null; };
            let last = read();
            new MutationObserver(() => { const v = read(); if (v !== last) { last = v; window.__seen.push([performance.now(), v]); } })
                .observe(document.getElementById('comm-inputs'), {childList: true, subtree: true, characterData: true});
        }""",
        pid,
    )
    seq0 = page.evaluate("Number(document.querySelector('#stream-time').textContent.split('序号 ')[1])")
    for k in range(48):  # a new observation four times a second for 12 s
        fresh.call(fresh.ua, [(pid, 500.0 + k)])
        page.wait_for_timeout(250)
    seq1 = page.evaluate("Number(document.querySelector('#stream-time').textContent.split('序号 ')[1])")
    seen = page.evaluate("window.__seen")
    assert seq1 - seq0 >= 40, "the live stream really was delivering the changes meanwhile"
    values = [float(v.split()[0].replace(",", "")) for _, v in seen]
    assert 1 <= len(values) <= 4, f"one change per poll, not one per live message: {values}"
    assert all(500.0 <= v < 548.0 for v in values) and values == sorted(values)
    gaps = [b[0] - a[0] for a, b in zip(seen, seen[1:], strict=False)]
    assert all(g >= 4000 for g in gaps), f"changes only at poll boundaries (5 s apart): {gaps}"
    assert page.errors == []


def test_every_row_state_the_page_ever_shows_belongs_to_one_observation(page, fresh):
    """S3 in the browser: the value, the quality, the time it was received and the server's own timestamp of a row are
    always those of ONE observation, whatever arrives between two readings."""
    pid = next(p.id for p in fresh.config.points if p.source == UA and "temp_inner" in p.id)
    base, received = 1_700_000_000.0, {}
    gen = fresh.call(lambda: fresh.owner.sources[UA].conn_gen)
    open_page(page, fresh)
    page.evaluate(
        """pid => {
            window.__states = [];
            const read = () => { for (const tr of document.querySelectorAll('#comm-inputs tbody tr'))
                if (tr.querySelector('small')?.textContent === pid) return [...tr.children].map(td => td.textContent.trim()); return null; };
            let last = JSON.stringify(read());
            new MutationObserver(() => { const now = read(), text = JSON.stringify(now); if (text !== last) { last = text; window.__states.push(now); } })
                .observe(document.getElementById('comm-inputs'), {childList: true, subtree: true, characterData: true});
        }""",
        pid,
    )
    for k in range(40):
        def push(k=k):
            now = time.time()
            quality = r.GOOD if k % 2 == 0 else r.UNCERTAIN
            item = r.Item(pid, 500.0 + k, quality, 0, base + k)
            fresh.owner.accept(r.InputBatch(UA, gen, r.KIND_UA_NOTIFY, now, time.monotonic(), (item,), publish_seq=7_000 + k))
            return fresh.owner.points[pid].obs.received_wall

        received[k] = fresh.call(push)
        page.wait_for_timeout(300)
    page.wait_for_timeout(5500)  # one more reading after the last change
    states = page.evaluate("window.__states")
    assert len(states) >= 2, "the page did take readings while the values changed"
    for cell in states:
        k = round(float(cell[3].split()[0].replace(",", "")) - 500.0)
        assert cell[4].split()[0] == (r.GOOD if k % 2 == 0 else r.UNCERTAIN), f"its own quality (observation {k}): {cell}"
        assert cell[6].startswith(shown(page, received[k])), f"its own receive time (observation {k}): {cell}"
        assert cell[7].startswith(shown(page, base + k)), f"its own source timestamp (observation {k}): {cell}"
    assert page.errors == []


# --------------------------------------------------------------------------------------------------- counters


def test_the_two_counters_are_shown_apart_on_the_communication_page_and_on_the_self_check_page(page, fresh):
    def cycle(n):
        owner = fresh.owner
        for _ in range(n):
            gen = owner.sources[UA].conn_gen + 1
            owner.begin_generation(UA, gen, time.time(), time.monotonic())
            owner.source_down(UA, gen, "ERROR:SubscriptionError", time.time(), time.monotonic())
        owner.begin_generation(UA, owner.sources[UA].conn_gen + 1, time.time(), time.monotonic())  # connected again, quiet

    fresh.call(cycle, 5)
    fresh.acquisition.failures = {MB: 0, UA: 2}
    open_page(page, fresh)
    for place in ("#comm-sources", None):
        if place is None:
            page.get_by_role("button", name="系统自检").click()
            place = "#check-sources"
            page.locator(place + " .badge", has_text="持续失步").wait_for()
        table = page.locator(place)
        heads = [h.strip() for h in table.locator("th").all_inner_texts()]
        assert "重新同步次数" in heads and "连续建连失败次数" in heads, "two columns, not one"
        rows = {c[0]: c for c in (cells(r_) for r_ in table.locator("tbody tr").all())}
        resync, failures = heads.index("重新同步次数"), heads.index("连续建连失败次数")
        assert rows[UA][resync].startswith("5") and "持续失步" in rows[UA][resync] and rows[UA][failures] == "2"
        assert rows[MB][resync] == "0" and rows[MB][failures] == "0"
        assert "互不替代" in page.locator("main").inner_text()
        keep(page, "comm-counters" if place == "#comm-sources" else "selfcheck-counters")
    page.get_by_role("button", name="通信·日志").click()
    page.locator("#comm-inputs tbody tr").first.wait_for()
    fresh.acquisition.failures = {MB: 7, UA: 0}  # while the page stays in front, the next poll shows the new counts
    page.wait_for_function(
        "id => [...document.querySelectorAll('#comm-sources tbody tr')].some(tr => tr.firstElementChild.textContent === id && tr.lastElementChild.textContent === '7')",
        arg=MB, timeout=8000,
    )  # fmt: skip
    assert page.errors == []


# ------------------------------------------------------------------------------------------------------ the log


def test_the_log_says_which_run_what_was_trimmed_and_what_was_lost_and_never_grades_an_event(page, fresh):
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))

    def inject():
        owner = fresh.owner
        now, mono = time.time(), time.monotonic()
        for k in range(560):  # more than the 500 kept
            owner.commlog.event(MB, 1, "NEW_GENERATION", f"FILL_{k}", now, mono)
        for k in range(3):
            owner.attempt_failed(UA, "CONNECT", "ERROR:ConnectionRefusedError", "refused by opc.tcp://***@plant:4840/", False, now + k, mono + k)
        owner.attempt_failed(UA, "SUBSCRIBE", "ERROR:SubscriptionError", "x" * 512, True, now + 5, mono + 5)
        owner.attempt_ok(UA, "SUBSCRIBE", now + 6, mono + 6)
        owner.report_diag_lost(3)

    fresh.call(inject)
    open_page(page, fresh)
    meta = page.locator("#comm-log-meta").inner_text()
    assert "500 / 500" in meta and "已淘汰" in meta and "现存最早记录" in meta, meta
    assert "诊断丢失 3 次" in meta and "不是数据丢失" in meta and page.locator("#comm-log-meta .badge", has_text="日志不完整").count() == 1
    rows = page.locator("#comm-log tbody tr")
    assert rows.count() == 500
    first = cells(rows.first)
    assert "建订阅尝试失败" in first[3] and "已结束" in first[9] and "订阅创建成功" in first[9], "newest first; ended by the success"
    assert "已截断" in first[4] and "跨尝试，不绑定单一代次" in first[5], "the cut is declared; an attempt belongs to no single generation"
    connect = next(cells(r_) for r_ in rows.all() if "建连尝试失败" in r_.inner_text())
    assert connect[6] == "3" and "进行中" in connect[9], "three failures merged into one entry that is still going on"
    text = page.locator("#comm-log").inner_text()
    assert not any(word in text for word in ("严重", "警告", "致命", "一般")), "no level is invented"
    assert page.locator("audio").count() == 0 and dialogs == [], "no pop-up and no sound"
    keep(page, "comm-log")
    assert page.errors == []


def test_an_empty_log_says_so_without_claiming_that_nothing_ever_happened(page, fresh):
    open_page(page, fresh)
    payload = health_json(page, fresh)
    payload["communication"]["log"].update(entries=[], evicted=0, diag_lost=0, incomplete=False, oldest_time=None)
    page.route(HEALTH, lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(payload)))
    page.get_by_role("button", name="系统总览").click()
    page.get_by_role("button", name="通信·日志").click()
    page.locator("#comm-log").get_by_text("暂无记录").wait_for()
    assert "不表示从未发生通信故障" in page.locator("#comm-log").inner_text()
    keep(page, "comm-log-empty")
    assert page.locator("#comm-log-meta .badge").count() == 0
    assert page.errors == []


def test_an_older_answer_never_replaces_a_newer_one_and_a_page_that_was_left_is_not_drawn_on(page, rig):
    sign_in(page, rig)
    base = health_json(page, rig)
    old, new = json.loads(json.dumps(base)), json.loads(json.dumps(base))
    old["communication"]["taken_time"] = base["communication"]["taken_time"] - 1000
    held = []
    page.route(HEALTH, lambda route: held.append(route))  # hold every request; the test answers them in its own order

    def answer(route, payload):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    def wait_for_requests(n):
        deadline = time.monotonic() + 5
        while len(held) < n:
            assert time.monotonic() < deadline, "the page did not ask"
            page.wait_for_timeout(50)

    page.get_by_role("button", name="通信·日志").click()
    wait_for_requests(1)
    page.get_by_role("button", name="系统总览").click()  # leave with that request still unanswered
    page.get_by_role("button", name="通信·日志").click()  # ... and come back: a newer request
    wait_for_requests(2)
    answer(held[-1], new)  # the newest request answers first
    page.locator("#comm-inputs tbody tr").first.wait_for()
    shown_new, shown_old = shown(page, new["communication"]["taken_time"]), shown(page, old["communication"]["taken_time"])
    assert shown_new in page.locator("#comm-taken").inner_text()
    answer(held[0], old)  # the oldest answers late
    page.wait_for_timeout(500)
    taken = page.locator("#comm-taken").inner_text()
    assert shown_new in taken and shown_old not in taken, "the older answer did not win"
    # leave while a request is outstanding: its answer must not be drawn anywhere, nor flash when the page is entered again
    page.get_by_role("button", name="系统总览").click()
    page.get_by_role("button", name="通信·日志").click()
    n = len(held)
    wait_for_requests(n)
    page.get_by_role("button", name="系统总览").click()
    for route in held[n - 1:]:
        answer(route, old)
    page.wait_for_timeout(300)
    assert page.locator("#comm-taken").count() == 0, "the overview page has no communication content"
    page.unroute(HEALTH)
    page.get_by_role("button", name="通信·日志").click()
    assert shown_old not in page.locator("main").inner_text(), "no leftovers from an old visit"
    page.locator("#comm-inputs tbody tr").first.wait_for()
    assert shown_old not in page.locator("main").inner_text()
    assert page.errors == []


def test_a_failed_reading_is_said_so_and_the_last_good_content_stays(page, rig):
    open_page(page, rig)
    page.route(HEALTH, lambda route: route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"})))
    page.get_by_role("button", name="系统自检").click()
    page.get_by_role("button", name="通信·日志").click()  # a new visit starts empty: the failure is the whole story
    page.locator("#comm-sources").get_by_text("读取失败").wait_for()
    assert "boom" in page.locator("#comm-sources").inner_text()
    page.unroute(HEALTH)
    page.locator("#comm-inputs tbody tr").first.wait_for(timeout=8000)  # the next poll recovers by itself
    page.route(HEALTH, lambda route: route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"})))
    page.locator("#comm-taken").get_by_text("最近一次刷新失败").wait_for(timeout=8000)
    assert page.locator("#comm-inputs tbody tr").count() > 0, "what was read last stays on screen, marked"
    assert [e for e in page.errors if "500" not in e] == []


def test_the_live_pages_still_update_while_the_communication_page_exists(page, rig):
    sign_in(page, rig)
    point = next(d.inputs[0] for d in rig.config.derived if d.operation == "difference")
    page.get_by_role("button", name="通信·日志").click()
    page.get_by_role("button", name="系统总览").click()
    try:
        rig.call(rig.ua, [(point, 471.0)])
        seq = rig.call(lambda: rig.owner.pub_seq)
        page.wait_for_function("seq => Number(document.querySelector('#stream-time').textContent.split('序号 ')[1]) >= seq", arg=seq)
    finally:
        rig.call(rig.ua, [(point, nominal()[point])])
    assert page.errors == []
