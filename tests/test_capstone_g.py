"""Capstone G: the seven pages end to end.

Chrome against the running application - owner, publisher, WebSocket and archive in a server thread - with
everything that goes wrong made to go wrong for real: damaged messages on the socket, a client that does not
read, a source that starts over, an archive that refuses.  The module-G tests of D36-D44 are re-run beside this
one; what is added here is what only the whole can show.

Why it matters: a monitoring screen is trusted at a glance.  Whatever happens behind it, it must never show a
new input next to an old result, a half-applied message, or a number from before an interruption as if it were
current - and it must stay readable on the screen the operator actually has.
"""

import base64
import json
import logging
import os
import socket
import struct
import time

import pytest
from browser_support import keep, number, sign_in, watch_socket
from live_support import BRIDGES, FULL
from web_rig import UA, WebRig, nominal

from htsg.config import Settings

OK = "document.querySelector('#connection').textContent === '实时连接'"
PAIR = 2  # the third temperature pair
PROXY_PER_K = 1.1475  # MPa per K: E 170 GPa x alpha 1.35e-5 1/K x restraint 0.5
FORGED = 987654.321  # a load nobody sent: "987,654" on screen would mean a damaged message was applied
NAMES = ["系统总览", "水冷壁应变场", "热应力分析", "历史查询", "系统自检", "通信·日志", "系统管理·权限"]


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


def chain_end(messages):
    """Checks one connection's messages as the contract demands (终版 §6): the snapshot first, then every group once
    and in order, the ends of a message equal to its first and last group, heartbeats carrying no sample.
    Returns the last sequence number."""
    snapshot, *rest = messages
    assert snapshot["type"] == "snapshot"
    seq = snapshot["pub_seq"]
    for message in rest:
        if message["type"] == "heartbeat":
            assert message["seq"] == seq and "groups" not in message and "channels" not in message, message
            continue
        assert message["type"] == "groups", message["type"]
        seqs = [g["seq"] for g in message["groups"]]
        assert message["first"] == seqs[0] and message["last"] == seqs[-1], "the ends are the first and last group"
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"groups inside are contiguous: {seqs}"
        assert seqs[0] == seq + 1, f"no group missed or applied twice: after {seq} came {seqs[0]}"
        seq = seqs[-1]
    return seq


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
            assert abs(stress - PROXY_PER_K * delta) <= 0.01, f"a new input beside an old result: {panels}"
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


# ------------------------------------------------------------------------------------ messages on the socket


def test_single_and_multi_group_messages_have_matching_ends_and_leave_no_hole(page, rig, calm):
    log = watch_socket(page)
    open_page(page, rig, "热应力分析")
    for value in (701.0, 702.0):  # each alone: the next change is far more than 100 ms away
        rig.call(rig.ua, [("load_mw", value)])
        page.wait_for_timeout(350)

    def three():
        for value in (711.0, 712.0, 713.0):  # three consistent states inside one 100 ms window
            rig.ua([("load_mw", value)])

    rig.call(three)
    page.wait_for_function("document.querySelector('#trend-latest').textContent.includes('713')")
    rig.paused = True  # the rig stops producing groups: the socket falls silent and must say so with a heartbeat
    page.wait_for_timeout(1800)
    messages = [m for n, m in log if n == 0]
    last = chain_end(messages)
    sizes = [len(m["groups"]) for m in messages if m["type"] == "groups"]
    assert 1 in sizes and max(sizes) >= 3, f"a single-group and a multi-group message were both really sent: {sizes}"
    assert any(m["type"] == "heartbeat" for m in messages), "idle seconds produced a heartbeat"
    assert f"序号 {last}" in page.locator("#stream-time").inner_text() or last < rig.call(lambda: rig.owner.pub_seq)
    series = [p["v"] for p in plot(page)["panels"][0]["points"] if p["valid"]]
    assert [711.0, 712.0, 713.0] in [series[i : i + 3] for i in range(len(series))], "none of the three was merged away"
    assert page.errors == []
    keep(page, "g01-stream-messages")


def damaged(kind, group, seq):
    """A message the sender must never produce.  `group(n)` is a well-formed group numbered n that carries a
    load of 987,654 MW - so any trace of it on the page means part of a bad message was applied."""
    s = seq
    return {
        "ends": {"type": "groups", "first": s + 1, "last": s + 3, "groups": [group(s + 1), group(s + 2)]},
        "hole inside": {"type": "groups", "first": s + 1, "last": s + 4, "groups": [group(s + 1), group(s + 3), group(s + 4)]},
        "out of order": {"type": "groups", "first": s + 1, "last": s + 4, "groups": [group(s + 1), group(s + 3), group(s + 2), group(s + 4)]},
        "hole before": {"type": "groups", "first": s + 2, "last": s + 3, "groups": [group(s + 2), group(s + 3)]},
        "old groups again": {"type": "groups", "first": s, "last": s + 1, "groups": [group(s), group(s + 1)]},
        "sender ahead": {"type": "heartbeat", "seq": s + 5},
    }[kind]


REASONS = {
    "ends": "first/last do not match",
    "hole inside": "missing or out of order",
    "out of order": "missing or out of order",
    "hole before": "before this message is missing",
    "old groups again": "before this message is missing",
    "sender ahead": "sender is ahead",
}


@pytest.mark.parametrize("kind", list(REASONS))
def test_a_damaged_message_changes_nothing_on_the_page_and_the_page_takes_a_new_snapshot(page, rig, calm, kind):
    page.add_init_script(SECRET_RECORDER + CONNECTION_RECORDER)
    log = watch_socket(page)
    open_page(page, rig, "热应力分析")
    rig.paused = True  # the rig adds no groups of its own while the damaged message is prepared
    try:
        page.wait_for_timeout(1300)
        views = rig.call(rig.owner.channel_views)
        seq = rig.call(lambda: rig.owner.pub_seq)
        page.wait_for_function(f"document.querySelector('#stream-time').textContent.includes('序号 {seq}')")
        forged = dict(views["load_mw"], value=FORGED)
        message = damaged(kind, lambda n: {"seq": n, "order": 0, "source": "ua_field", "wall": 0, "channels": {"load_mw": forged}, "states": [], "faults": []}, seq)
        rig.call(lambda: [deliver(message) for deliver in list(rig.publisher.subscribers)])
        page.locator("#notice").get_by_text("未通过校验").wait_for()
        assert REASONS[kind] in page.locator("#notice").inner_text()
        keep(page, f"g02-damaged-{kind.replace(' ', '-')}")
    finally:
        rig.paused = False
    page.locator("#notice").wait_for(state="hidden", timeout=10000)  # the new snapshot ends the notice
    connected(page)
    assert page.evaluate("window.__secret") == 0, "the damaged content was never on screen, not even for a moment"
    assert FORGED not in [p["v"] for p in plot(page)["panels"][0]["points"]], "nor in the trend"
    assert "重连中" in connection_states(page), "the page let go of the connection and took another"
    taken = [m for n, m in log if n == 1]
    assert taken and taken[0]["type"] == "snapshot" and taken[0]["pub_seq"] >= seq
    assert chain_end(taken) >= seq
    load = page.locator('#trend-latest [data-panel="load_mw"]').inner_text()
    assert fmt(views["load_mw"]["value"]) in load and "987" not in load
    assert page.errors == []


# --------------------------------------------------------------- no new input next to an old result (终版 §6)


def drive_strain(rig, events):
    """Rounds 1..40: station S01 reads 0.60 + 0.01 k mV/V (round 25 is not answered), 70 ms apart; rounds 11-13 are
    sent inside one turn.  `events` maps a round number to something done instead of waiting."""
    for k in range(1, 41):
        values = {b: 0.6 for b in BRIDGES}
        if k == 25:
            del values[BRIDGES[0]]
        else:
            values[BRIDGES[0]] = round(0.6 + 0.01 * k, 4)
        if k == 11:
            rig.call(lambda: [rig.round(values), rig.round({**values, BRIDGES[0]: round(0.6 + 0.01 * 12, 4)}), rig.round({**values, BRIDGES[0]: round(0.6 + 0.01 * 13, 4)})])
            continue
        if k in (12, 13):
            continue
        if k in events:
            events[k]()
        rig.call(rig.round, values)
        time.sleep(0.07)


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
            assert abs(strain - raw * unit) <= 0.006, f"the strain is not that of this input: {row}"
            if k == 26:
                assert change is None, f"a change was bridged across an unanswered round: {row}"
            elif k == 0:
                assert change == 0.0, row
            elif change is None:
                assert change_may_wait, f"the change is missing although the previous round was there: {row}"
            else:
                assert abs(change - 0.01 * unit) <= 0.006, f"the change is not that of this round: {row}"
            complete += 1
    return complete, dashes


def test_rounds_and_a_cut_connection_never_show_an_input_next_to_another_rounds_result(page, still):
    page.add_init_script(STRAIN_RECORDER + CONNECTION_RECORDER)
    for _ in range(2):  # two quiet rounds: every station starts with a change of 0
        still.call(still.round, {b: 0.6 for b in BRIDGES})
    open_page(page, still, "水冷壁应变场")
    page.locator("#strain-table tbody tr").nth(0).get_by_text("100").wait_for()
    drive_strain(still, {20: still.drop_clients})
    page.wait_for_function("window.__frames.at(-1)[0][1].startsWith('1 mV/V')")
    complete, dashes = check_strain_frames(frames(page))
    assert complete >= 15, f"enough different states were really seen: {complete}"
    assert dashes >= 2, "the unanswered round and the cut-off connection were both seen as dashes"
    assert "重连中" in connection_states(page)
    assert page.errors == []
    keep(page, "g03-strain-consistency")


def test_a_source_that_starts_over_and_a_cut_connection_never_leave_a_result_beside_the_wrong_input(page, rig, calm):
    page.add_init_script(TREND_RECORDER + CONNECTION_RECORDER)
    open_page(page, rig, "热应力分析")
    page.locator("#pair-select").select_option(str(PAIR))
    resume = None
    for k in range(1, 46):
        if k == 10:
            resume = start_over(rig)
        elif k == 19:
            rig.call(resume)
        elif k == 28:
            rig.drop_clients()
        elif not 10 < k < 19:
            push_pair(rig, k, groups=1 + k % 3)
        time.sleep(0.12)
    page.wait_for_function(OK)
    page.wait_for_timeout(500)
    valid, dashes = check_trend_frames(frames(page))
    assert valid >= 15 and dashes >= 2, (valid, dashes)
    panels = plot(page)["panels"]
    assert len(panels[1]["points"]) == len(panels[2]["points"]), "the difference and the proxy are recorded together"
    for delta, stress in zip(panels[1]["points"], panels[2]["points"], strict=True):
        assert (delta["t"], delta["valid"]) == (stress["t"], stress["valid"]), "one group, one instant, one validity"
        if delta["valid"]:
            assert abs(stress["v"] - PROXY_PER_K * delta["v"]) < 1e-6
    assert {"重连中", "实时连接"} <= connection_states(page)
    assert page.errors == []
    keep(page, "g04-source-restart-and-cut")


# ----------------------------------------------------------------------------- a client that cannot keep up


@pytest.fixture(scope="module")
def slow_rig(tmp_path_factory):
    """Writes to a real archive, cuts a client off after 1 s without a completed send and keeps 8 messages per client."""
    payload = FULL.model_dump(mode="json")
    payload["timing"]["client_send_timeout"] = 1.0
    payload["limits"]["ws_queue"] = 8
    with WebRig(tmp_path_factory.mktemp("web"), config=Settings.model_validate(payload), archive=True) as running:
        yield running


@pytest.fixture
def second_page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN", timezone_id="Asia/Shanghai")
    second = context.new_page()
    second.errors = []
    second.on("console", lambda m: second.errors.append(m.text) if m.type == "error" else None)
    second.on("pageerror", lambda error: second.errors.append(str(error)))
    yield second
    context.close()


def settle(rig, *pages):
    """The rig stops producing groups; once the last message has gone, every given page must show exactly the
    owner's sequence number - and nothing else could be showing it, since no group is in flight."""
    rig.paused = True
    time.sleep(1.5)
    seq = rig.call(lambda: rig.owner.pub_seq)
    for p in pages:
        p.wait_for_function(f"document.querySelector('#stream-time').textContent.includes('序号 {seq}')", timeout=10000)
    return seq


def wait_subscribers(rig, count):
    """A closed client is noticed at the next send (at most a heartbeat later), so the count settles, it does not jump."""
    deadline = time.monotonic() + 6
    while (now := rig.call(lambda: len(rig.publisher.subscribers))) != count:
        assert time.monotonic() < deadline, f"{now} subscribers, expected {count}"
        time.sleep(0.05)


def raw_connect(port, credential, buffer=2048):
    """A WebSocket client written on a plain socket, so that it really does not read: handshake, credential, silence."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buffer)  # a small window: the sender stalls sooner
    sock.connect(("127.0.0.1", port))
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        f"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        head += sock.recv(1)
    assert b" 101 " in head.split(b"\r\n")[0], head
    body = json.dumps({"credential": credential}).encode()
    mask = os.urandom(4)
    sock.sendall(bytes([0x81, 0x80 | len(body)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(body)))
    return sock


def read_message(sock):
    """One message of the server (unmasked text frame); {'closed': code} for a close frame."""

    def exactly(n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("the server ended the connection")
            data += chunk
        return data

    sock.settimeout(5)
    opcode, length = exactly(1)[0] & 0x0F, exactly(1)[0] & 0x7F
    if length == 126:
        length = struct.unpack(">H", exactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", exactly(8))[0]
    payload = exactly(length)
    return {"closed": struct.unpack(">H", payload[:2])[0] if payload else None} if opcode == 8 else json.loads(payload)


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


def test_a_client_that_never_reads_is_cut_off_while_a_browser_and_the_archive_carry_on(page, slow_rig):
    rig = slow_rig
    errors = []
    handler = logging.Handler(level=logging.ERROR)
    handler.emit = lambda record: errors.append(record.getMessage())
    uvicorn_log = logging.getLogger("uvicorn.error")
    uvicorn_log.addHandler(handler)
    page.add_init_script(CONNECTION_RECORDER)
    subscribers = lambda: rig.call(lambda: len(rig.publisher.subscribers))  # noqa: E731
    slow = None
    try:
        open_page(page, rig, "系统总览")
        wait_subscribers(rig, 1)
        slow = raw_connect(rig.port, rig.credentials["viewer"])
        wait_subscribers(rig, 2)
        page.evaluate("window.__conn.clear()")
        started, cut = time.monotonic(), None
        for k in range(1, 400):
            push_pair(rig, k, groups=3, heavy=True)
            time.sleep(0.1)
            if subscribers() == 1:
                cut = time.monotonic() - started
                break
            assert time.monotonic() - started < 40, "the client that does not read was never cut off"
        assert cut is not None
        # what the cut-off client did receive is a clean prefix: the snapshot, then groups without a hole
        got = []
        try:
            while True:
                message = read_message(slow)
                if "closed" in message:
                    break
                got.append(message)
        except (ConnectionError, TimeoutError, OSError, json.JSONDecodeError):
            pass  # the connection ended, possibly in the middle of a frame
        assert got and got[0]["type"] == "snapshot", "it was handed the snapshot first"
        prefix_end = chain_end(got)
        again = raw_connect(rig.port, rig.credentials["viewer"], buffer=65536)
        first = read_message(again)
        again.close()
        assert first["type"] == "snapshot" and first["pub_seq"] > prefix_end, "a reconnect starts from a newer full snapshot"
        # the other browser never noticed: connected throughout, and up to date once the traffic stops
        settle(rig, page)
        assert connection_states(page) == {"实时连接"}
        stats = archive_settled(rig)
        assert stats["committed"] > 0
    finally:
        rig.paused = False
        uvicorn_log.removeHandler(handler)
        if slow:
            slow.close()
    assert errors == [], f"closing a connection that was already gone must not raise: {errors}"
    assert page.errors == []
    keep(page, "g05-raw-client-cut-off")


BLOCK_SECONDS = 20


def test_a_browser_that_stops_reading_is_cut_off_and_comes_back_on_a_new_snapshot(page, second_page, slow_rig):
    rig = slow_rig
    page.add_init_script(TREND_RECORDER + CONNECTION_RECORDER)
    second_page.add_init_script(CONNECTION_RECORDER)
    log = watch_socket(page)
    open_page(page, rig, "热应力分析")
    page.locator("#pair-select").select_option(str(PAIR))
    open_page(second_page, rig, "系统总览")
    second_page.evaluate("window.__conn.clear()")
    subscribers = lambda: rig.call(lambda: len(rig.publisher.subscribers))  # noqa: E731
    wait_subscribers(rig, 2)
    # the first browser's page script is busy for 20 s: it cannot read what the server sends it
    page.evaluate(f"setTimeout(() => {{ const t = Date.now(); while (Date.now() - t < {BLOCK_SECONDS * 1000}); }}, 50)")
    started, cut = time.monotonic(), None
    for k in range(1, 1000):
        push_pair(rig, k, groups=3, heavy=True)
        time.sleep(0.1)
        if subscribers() == 1:
            cut = time.monotonic() - started
            break
        assert time.monotonic() - started < BLOCK_SECONDS - 2, "the browser that does not read was never cut off"
    assert cut is not None
    for k in range(1000, 1010):  # the other browser keeps receiving while the first one is gone
        push_pair(rig, k, groups=2)
        time.sleep(0.1)
    time.sleep(max(0.0, BLOCK_SECONDS - (time.monotonic() - started)) + 0.3)  # the blocked page wakes up
    connected(page)
    try:
        settle(rig, page, second_page)
    finally:
        rig.paused = False
    assert connection_states(second_page) == {"实时连接"}, "the healthy browser was not disturbed"
    assert {"重连中", "实时连接"} <= connection_states(page), "the cut-off browser says so, then recovers"
    sockets = sorted({n for n, _ in log})
    assert len(sockets) >= 2, "it connected again"
    first_after = [m for n, m in log if n == sockets[1]][0]
    assert first_after["type"] == "snapshot"
    assert chain_end([m for n, m in log if n == sockets[0]]) < first_after["pub_seq"], "a clean prefix, then a newer snapshot"
    valid, dashes = check_trend_frames(frames(page))
    assert valid >= 3, "the page showed results before and after, and none of them was inconsistent"
    archive_settled(rig)
    assert page.errors == [] and second_page.errors == []
    keep(page, "g06-browser-cut-off-and-back")


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
        drive_strain(rig, {8: lambda: rig.call(closed), 30: lambda: rig.call(opened)})
        said = page.evaluate("[...window.__banner]")
        assert any("历史归档中断" in text and "实时值仍在更新" in text for text in said), said
        page.locator("#archive-banner").wait_for(state="hidden", timeout=30000)  # recovered: the gap is closed
        keep(page, "g07-live-archive-gap")
        complete, _ = check_strain_frames(frames(page), change_may_wait=True)
        assert complete >= 15, "the live numbers kept coming, whole, while the archive was refusing"
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
    # same observation time, same batch, same origin, same gap mark and the same end: no result without its input
    # and no input without its result - across the gap too
    batch = lambda r: r[2].split("·")[0]  # noqa: E731
    assert [(r[1], batch(r), r[5], r[8], r[9]) for r in bridge] == [(r[1], batch(r), r[5], r[8], r[9]) for r in strain]
    before = [r for r in strain if r[8].startswith("至有效段结束 1970/1/1 08:17:06")]
    after = [r for r in strain if r[9] == "缺口后重申"]
    assert len(before) == 1 and len(after) == 1, "the last value before the gap ends at its start; the first after is a restatement"
    assert after[0][0].startswith("1970/1/1 08:17:09") and strain.index(after[0]) == strain.index(before[0]) + 1, "nothing in between"
    for b, s in zip(bridge, strain, strict=True):
        assert abs(float(s[3].replace(",", "")) - float(b[3].replace(",", "")) * 1000 / 6) <= 1.0, (b, s)


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
        for path in ("/api/ui-config", "/api/channels", "/api/points", "/api/derived", "/api/thresholds", "/api/config", "/api/stream"):
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
