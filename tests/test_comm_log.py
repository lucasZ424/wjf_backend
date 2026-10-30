"""D44b: the bounded diagnostic entry and the owner's communication log (终版 §15.1).

Why it matters: the log is what an operator reads to learn why a source is not talking.  It must stay small whatever
happens (a source that fails every second for a week), must not turn a pile of repeats into one lie ("one long
outage") or one failure into hundreds of lines, and must neither leak credentials to a read-only user nor ever look
like a data loss that sends the application into recovery.

Dependence: 方案 D44b; 审核 §8.1 S2、§8.3 P1(单列尝试失败)、§8.2 "凭据" 条; 终版 §15.1 "重复失败" "保留与淘汰" "记录路径与容量".
"""

import time

import pytest
from owner_support import Recorder, small_config

from htsg import records as r
from htsg.commlog import DETAIL_LIMIT, LOG_LIMIT, CommLog, clean_detail, scrub
from htsg.config import Settings
from htsg.ingress import Ingress
from htsg.owner import Owner

WALL0, MONO0 = 1000.0, 100.0


class Clock:
    """Strictly increasing wall / monotonic times, one second apart, so every moment in a test is distinct."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return WALL0 + self.n, MONO0 + self.n


@pytest.fixture
def clock():
    return Clock()


def fill_with_events(log, clock, n, source="mb"):
    for k in range(n):
        log.event(source, 1, "NOTE", f"N{k}", *clock())


# ------------------------------------------------------------------------------------------ keeping 500


def test_the_log_keeps_the_newest_500_in_order_and_counts_what_it_moved_out(clock):
    log = CommLog("run")
    fill_with_events(log, clock, LOG_LIMIT)
    view = log.snapshot()
    assert len(view["entries"]) == 500 and view["evicted"] == 0 and view["incomplete"] is False
    log.event("mb", 1, "NOTE", "N500", *clock())  # the 501st
    view = log.snapshot()
    assert len(view["entries"]) == 500 and view["evicted"] == 1 and view["incomplete"] is True
    assert [e["seq"] for e in view["entries"]] == list(range(2, 502)), "first in, first out; numbers never reused"
    assert view["oldest_time"] == view["entries"][0]["found_time"] == WALL0 + 2, "the earliest record still held"
    assert view["scope"] == "THIS_RUN_IN_MEMORY"


def test_updating_an_aggregate_does_not_move_it_so_it_goes_first_and_its_slot_goes_with_it(clock):
    log = CommLog("run")
    first = log.attempt_failed("ua", "CONNECT", "REFUSED", *clock())
    fill_with_events(log, clock, LOG_LIMIT - 1, "ua")  # 500 entries, the aggregate the oldest
    for _ in range(4):
        log.attempt_failed("ua", "CONNECT", "REFUSED", *clock())  # kept alive by updates, not by position
    assert first.count == 5 and list(log.entries)[0] == first.seq
    log.event("ua", 1, "NOTE", "N", *clock())
    assert first.seq not in log.entries and log.evicted == 1, "the oldest by first insertion left, however fresh its last update"
    again = log.attempt_failed("ua", "CONNECT", "REFUSED", *clock())
    assert again is not first and again.seq > first.seq, "a new entry: the evicted one is not revived"
    assert again.count == 1 and again.first_wall == again.last_wall > first.last_wall, (
        "its first failure covers only what is kept now, not the failures that were moved out"
    )
    assert log.attempt_failed("ua", "CONNECT", "REFUSED", *clock()) is again and again.count == 2, "and it aggregates normally"
    assert log.snapshot()["evicted"] == 2


# --------------------------------------------------------------------- attempt failures (CONNECT / SUBSCRIBE)


def test_connect_failures_merge_across_generations_resyncs_and_other_phases(clock):
    owner = Owner(small_config(), WALL0, MONO0, run_id="run")
    log = owner.commlog
    log.attempt_failed("ua", "CONNECT", "REFUSED", *clock(), detail="refused once")
    owner.begin_generation("ua", 1, *clock())  # a connection existed meanwhile
    log.event("ua", 1, "RESYNC", "DOWN:CONNECTION", *clock())
    log.attempt_failed("ua", "SUBSCRIBE", "ITEMS_REFUSED", *clock())  # another phase
    owner.begin_generation("ua", 2, *clock())
    entry = log.attempt_failed("ua", "CONNECT", "REFUSED", *clock(), detail="refused twice")
    connect = [e for e in log.entries.values() if e.phase == "CONNECT"]
    assert connect == [entry] and entry.count == 2 and entry.conn_gen is None
    assert entry.state == "ACTIVE" and entry.ended_wall is None
    assert entry.last_wall > entry.first_wall and entry.detail == "refused twice", "latest text; identity is the code"
    assert [e for e in log.entries.values() if e.phase == "SUBSCRIBE"][0].state == "ACTIVE"


def test_another_reason_in_the_same_phase_ends_the_old_entry_and_is_not_called_a_recovery(clock):
    log = CommLog("run")
    old = log.attempt_failed("ua", "CONNECT", "REFUSED", *clock())
    log.attempt_failed("ua", "CONNECT", "REFUSED", *clock())
    t_change = clock()
    new = log.attempt_failed("ua", "CONNECT", "TIMEOUT", *t_change)
    assert (old.state, old.end_reason, old.count) == ("ENDED", "REASON_CHANGED", 2)
    assert old.ended_wall == t_change[0] > old.last_wall, "the end is its own time, not the last failure's"
    assert (new.state, new.count, new.reason) == ("ACTIVE", 1, "TIMEOUT") and new.seq > old.seq


def test_each_phase_succeeding_ends_only_its_own_aggregate_whether_or_not_a_success_is_logged(clock):
    log = CommLog("run")
    c = log.attempt_failed("ua", "CONNECT", "REFUSED", *clock())
    s = log.attempt_failed("ua", "SUBSCRIBE", "ITEMS_REFUSED", *clock())
    t_ok = clock()
    log.attempt_ok("ua", "CONNECT", *t_ok)
    assert (c.state, c.end_reason, c.ended_wall) == ("ENDED", "CONNECT_OK", t_ok[0]) and s.state == "ACTIVE", (
        "the connection succeeding says nothing about the subscription"
    )
    log.attempt_failed("ua", "SUBSCRIBE", "ITEMS_REFUSED", *clock())
    assert s.count == 2
    log.attempt_ok("ua", "SUBSCRIBE", *clock())
    assert s.state == "ENDED" and s.end_reason == "SUBSCRIBE_OK"
    assert len(log.entries) == 2, "a success writes no line of its own"
    later = log.attempt_failed("ua", "SUBSCRIBE", "ITEMS_REFUSED", *clock())
    assert later is not s and later.count == 1, "after a success the same failure is a new episode"


def test_one_source_failing_does_not_touch_another_sources_aggregate(clock):
    log = CommLog("run")
    a = log.attempt_failed("ua", "CONNECT", "REFUSED", *clock())
    b = log.attempt_failed("mb", "CONNECT", "REFUSED", *clock())
    log.attempt_ok("mb", "CONNECT", *clock())
    assert a.state == "ACTIVE" and b.state == "ENDED" and a is not b


# ----------------------------------------------------------------------------- round failures (inside a generation)


def test_round_failures_merge_inside_one_generation_scope_type_and_reason(clock):
    log = CommLog("run")
    first = log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16")
    second = log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16")
    assert second is first and first.count == 2 and first.conn_gen == 1 and first.phase is None
    other_scope = log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="20:4")
    assert other_scope is not first and first.state == "ACTIVE", "another request is another condition"


def test_exception_then_ok_then_exception_is_two_entries(clock):
    """A Modbus exception answer is valid communication but not a good round: an OK round between two exception
    stretches ends the first, so the log does not claim one long exception period."""
    log = CommLog("run")
    one = log.round_failed("mb", 1, "POLL_EXCEPTION", "CODE_4", *clock(), scope="0:16")
    log.round_failed("mb", 1, "POLL_EXCEPTION", "CODE_4", *clock(), scope="0:16")
    t_ok = clock()
    log.round_ok("mb", "0:16", *t_ok)
    two = log.round_failed("mb", 1, "POLL_EXCEPTION", "CODE_4", *clock(), scope="0:16")
    assert two is not one and (one.count, one.state, one.end_reason, one.ended_wall) == (2, "ENDED", "SUCCESS", t_ok[0])
    assert (two.count, two.state) == (1, "ACTIVE")


def test_a_part_of_the_round_succeeding_does_not_end_what_still_fails(clock):
    log = CommLog("run")
    a = log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16")
    b = log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="20:4")
    log.round_ok("mb", "20:4", *clock())  # the second request answered in the next round
    assert b.state == "ENDED" and a.state == "ACTIVE"
    assert log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16") is a and a.count == 2


def test_a_different_type_or_reason_in_the_same_scope_ends_the_old_entry(clock):
    log = CommLog("run")
    t = log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16")
    e = log.round_failed("mb", 1, "POLL_EXCEPTION", "CODE_4", *clock(), scope="0:16")
    e2 = log.round_failed("mb", 1, "POLL_EXCEPTION", "CODE_2", *clock(), scope="0:16")
    assert (t.state, t.end_reason) == ("ENDED", "REASON_CHANGED") and (e.state, e.end_reason) == ("ENDED", "REASON_CHANGED")
    assert e2.state == "ACTIVE" and len({t.seq, e.seq, e2.seq}) == 3


@pytest.mark.parametrize("kind", ["attempt", "round"])
def test_a_later_omitted_detail_replaces_the_old_detail_and_keeps_the_aggregate_marked_truncated(clock, kind):
    """R02/Q04: an aggregate's latest failure may be too long to retain; the page must say it was omitted instead of
    continuing to display an earlier short error as though it described the latest occurrence."""
    log = CommLog("run")
    if kind == "attempt":
        entry = log.attempt_failed("ua", "CONNECT", "REFUSED", *clock(), detail="short")
        same = log.attempt_failed("ua", "CONNECT", "REFUSED", *clock(), detail=None, truncated=True)
    else:
        entry = log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16", detail="short")
        same = log.round_failed(
            "mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16", detail=None, truncated=True
        )
    assert same is entry and entry.count == 2
    assert (entry.detail, entry.detail_truncated) == (None, True)
    if kind == "attempt":
        latest = log.attempt_failed("ua", "CONNECT", "REFUSED", *clock(), detail="short again")
    else:
        latest = log.round_failed(
            "mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16", detail="short again"
        )
    assert latest is entry and entry.count == 3
    assert (entry.detail, entry.detail_truncated) == ("short again", False)


def test_a_new_generation_or_any_other_event_of_the_source_ends_round_failures_but_not_attempt_failures(clock):
    owner = Owner(small_config(), WALL0, MONO0, run_id="run")
    log = owner.commlog
    owner.begin_generation("mb", 1, *clock())
    a = log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16")
    attempt = log.attempt_failed("mb", "CONNECT", "REFUSED", *clock())
    owner.begin_generation("mb", 2, *clock())
    assert (a.state, a.end_reason) == ("ENDED", "NEW_GENERATION") and attempt.state == "ACTIVE"
    b = log.round_failed("mb", 2, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16")
    assert b is not a, "no continuation across generations"
    other = log.round_failed("ua", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="x")
    log.event("mb", 2, "COMM_RECOVERED", "FIRST_VALID_RESPONSE", *clock())
    assert (b.state, b.end_reason) == ("ENDED", "EVENT:COMM_RECOVERED") and other.state == "ACTIVE", (
        "another event of THIS source ends its round failures; other sources' stay"
    )
    assert attempt.state == "ACTIVE", "only its own phase succeeding ends an attempt failure"


def test_a_failure_signal_does_not_cut_a_round_stretch_but_any_other_event_does(clock):
    log = CommLog("run")
    stretch = log.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16")
    for signal in ("RESYNC", "DATA_EXPIRED", "COMM_ALARM", "APP_LOSS"):
        log.event("mb", 1, signal, "X", *clock(), failure=True)  # the same trouble speaking again
    assert stretch.state == "ACTIVE" and stretch.count == 1
    log.event("mb", 1, "COMM_RECOVERED", "VALID_RESPONSE", *clock())  # something else happened
    assert (stretch.state, stretch.end_reason) == ("ENDED", "EVENT:COMM_RECOVERED")


def test_aggregate_entries_carry_the_last_communication_known_at_discovery(clock):
    log = CommLog("run")
    t_comm = clock()
    found = clock()
    entry = log.attempt_failed("ua", "CONNECT", "REFUSED", *found, comm=t_comm)
    d = entry.as_dict()
    assert d["last_comm_time"] == t_comm[0] and d["found_time"] == found[0]
    assert d["silent_seconds"] == pytest.approx(found[1] - t_comm[1]), "the duration comes from the monotonic clock"
    never = log.attempt_failed("mb", "CONNECT", "REFUSED", *clock()).as_dict()
    assert never["last_comm_time"] is None and never["silent_seconds"] is None, "never communicated: empty, not zero"


# ----------------------------------------------------------------- through the ordered entry, into the owner


def owner_and_ingress(batch_capacity=100, diag_capacity=500, config=None):
    rec = Recorder()
    owner = Owner(config or small_config(), WALL0, MONO0, commit=rec.commit, run_id="run")
    return owner, Ingress(owner, batch_capacity, diag_capacity), rec


def test_facts_reach_the_log_through_the_ingress_in_order_with_the_batches(clock):
    owner, ingress, _ = owner_and_ingress()
    ingress.attempt_failed("ua", "CONNECT", "REFUSED", "refused", *clock())
    ingress.begin_generation("ua", 1, *clock())
    ingress.attempt_failed("ua", "SUBSCRIBE", "ITEMS_REFUSED", "bad item", *clock())
    ingress.attempt_ok("ua", "CONNECT", *clock())
    assert owner.commlog.entries == {}, "nothing is appended until the owner takes it from the queue"
    ingress.drain()
    kinds = [(e.event, e.phase, e.reason, e.state, e.end_reason) for e in owner.commlog.entries.values()]
    assert kinds == [
        ("ATTEMPT_FAILED", "CONNECT", "REFUSED", "ENDED", "CONNECT_OK"),
        ("NEW_GENERATION", None, "CONNECT", None, None),
        ("ATTEMPT_FAILED", "SUBSCRIBE", "ITEMS_REFUSED", "ACTIVE", None),
    ], "the order in which the owner took them from the queue"


def test_a_full_diagnostic_allowance_counts_a_lost_diagnostic_and_nothing_else(clock):
    owner, ingress, rec = owner_and_ingress(batch_capacity=3, diag_capacity=5)
    for k in range(5):
        assert ingress.attempt_failed("ua", "CONNECT", "REFUSED", f"try {k}", *clock()) is True
    assert ingress.attempt_failed("ua", "CONNECT", "REFUSED", "one too many", *clock()) is False
    assert ingress.diag_pending == 5 and ingress.diag_dropped == 1
    assert (ingress.pending, ingress.dropped) == (0, 0), "not a batch, not an application loss"
    ingress.drain()
    assert owner.health["app_loss"] == 0 and owner.health["resyncs"] == 0 and owner.sources["ua"].state == "WAITING", (
        "no APP_LOSS, no C1"
    )
    view = owner.commlog.snapshot()
    assert view["diag_lost"] == 1 and view["incomplete"] is True
    assert view["entries"][0]["count"] == 5, "the five that got through are all there"
    assert ingress.diag_pending == 0 and ingress.attempt_failed("ua", "CONNECT", "REFUSED", "again", *clock()) is True
    ingress.drain()
    assert owner.commlog.diag_lost == 1, "told once, however often it is drained"


def test_diagnostics_and_business_batches_do_not_take_each_others_capacity(clock):
    owner, ingress, _ = owner_and_ingress(batch_capacity=2, diag_capacity=2)
    owner.begin_generation("mb", 1, *clock())
    for tick in range(2):  # the batch allowance is used up ...
        assert ingress.offer(r.InputBatch("mb", 1, r.KIND_MODBUS_POLL, *clock(), (), tick=tick, outcome="TIMEOUT")) is True
    assert ingress.attempt_failed("ua", "CONNECT", "REFUSED", "x", *clock()) is True, "... and a diagnostic still fits"
    assert ingress.attempt_failed("ua", "CONNECT", "REFUSED", "x", *clock()) is True
    assert ingress.attempt_failed("ua", "CONNECT", "REFUSED", "x", *clock()) is False
    assert ingress.offer(r.InputBatch("mb", 1, r.KIND_MODBUS_POLL, *clock(), (), tick=3, outcome="TIMEOUT")) is False
    assert (ingress.dropped, ingress.diag_dropped) == (1, 1), "each allowance counts its own refusals"


def test_a_full_diagnostic_allowance_never_holds_up_a_lifecycle_event(clock):
    owner, ingress, _ = owner_and_ingress(diag_capacity=1)
    ingress.begin_generation("ua", 1, *clock())
    ingress.attempt_failed("ua", "CONNECT", "REFUSED", "x", *clock())
    ingress.attempt_failed("ua", "CONNECT", "REFUSED", "x", *clock())  # refused
    ingress.attempt_ok("ua", "SUBSCRIBE", *clock())  # a boundary of the log: accepted regardless
    ingress.source_down("ua", 1, "CONNECTION", *clock())
    ingress.drain()
    assert owner.sources["ua"].state == "RESYNC" and owner.sources["ua"].resync_count == 1


def test_the_ingress_cleans_and_cuts_the_text_before_it_queues_it(clock):
    _, ingress, _ = owner_and_ingress()
    ingress.attempt_failed("ua", "CONNECT", "E", "x" * 100_000 + " password=hunter2", *clock())
    text, truncated = ingress.queue[-1].args[3:5]
    assert text is None and truncated is True, "the queue holds at most 500 short texts, not 500 huge ones: that one is left out"
    ingress.attempt_failed("ua", "CONNECT", "E", "x" * 3000, *clock())
    text, truncated = ingress.queue[-1].args[3:5]
    assert len(text) == DETAIL_LIMIT and truncated is True, "a text within the clean-whole limit is cut to the limit"


def test_details_that_differ_do_not_split_one_failure_into_many(clock):
    owner, ingress, _ = owner_and_ingress()
    for port in (50001, 50002, 50003):  # the library names the ephemeral port in every message
        ingress.attempt_failed("ua", "CONNECT", "ERROR:ConnectionRefusedError", f"cannot reach host from port {port}", *clock())
    ingress.drain()
    (entry,) = owner.commlog.entries.values()
    assert entry.count == 3 and entry.detail.endswith("50003")


def test_a_source_switched_off_by_configuration_makes_no_attempts_and_logs_none(clock):
    payload = small_config().model_dump(mode="json")
    payload["sources"][1]["enabled"] = False
    owner, ingress, _ = owner_and_ingress(config=Settings.model_validate(payload))
    ingress.attempt_failed("ua", "CONNECT", "REFUSED", "x", *clock())
    ingress.attempt_failed("mb", "CONNECT", "REFUSED", "x", *clock())
    ingress.drain()
    assert [e.source_id for e in owner.commlog.entries.values()] == ["mb"]


def test_the_log_never_reaches_the_archive_or_the_realtime_stream(clock):
    """Memory only: no commit group, no realtime group, no state record - the archive's kinds and routes are untouched."""
    owner, ingress, rec = owner_and_ingress()
    ingress.attempt_failed("ua", "CONNECT", "REFUSED", "x", *clock())
    ingress.attempt_ok("ua", "CONNECT", *clock())
    owner.commlog.event("ua", None, "NOTE", "N", *clock())
    owner.commlog.round_failed("mb", 1, "POLL_TIMEOUT", "NO_ANSWER", *clock(), scope="0:16")
    ingress.drain()
    assert rec.commits == [] and owner.order == 0


def test_a_new_run_starts_with_an_empty_log(clock):
    owner, ingress, _ = owner_and_ingress()
    ingress.attempt_failed("ua", "CONNECT", "REFUSED", "x", *clock())
    ingress.drain()
    assert len(owner.commlog.entries) == 1
    restarted = Owner(small_config(), WALL0, MONO0, run_id="next")
    assert restarted.commlog.snapshot()["entries"] == [] and restarted.commlog.snapshot()["evicted"] == 0


# ---------------------------------------------------------------------------------------------- the error text


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("cannot open opc.tcp://admin:s3cret@10.0.0.5:4840/ca", "s3cret"),
        ("cannot open opc.tcp://admin:s3cret@10.0.0.5:4840/ca", "admin"),
        ("cannot open opc.tcp://u:p@ss@host/ca", "ss@"),
        ("cannot open opc.tcp://u:pw1@pw2@pw3@host/ca", "pw3"),
        ("login failed password=hunter2 for the account", "hunter2"),
        ("login failed password: hunter2 for the account", "hunter2"),
        ("bad request {'password': 'hunter2'}", "hunter2"),
        ('bad request {"password": "hunter two"}', "hunter two"),
        ("token=abc123xyz refused", "abc123xyz"),
        ("api_key='k e y 1' refused", "k e y 1"),
        ("HTTP 401 Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("proxy header Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("OPCUA_PASSWORD=hunter2", "hunter2"),
    ],
)
def test_credentials_never_survive_the_cleaning(raw, secret):
    assert secret not in scrub(raw) and secret not in (clean_detail(raw)[0] or "")


@pytest.mark.parametrize(
    "plain",
    [
        "connect call did not return within 0.8 s",
        "ConnectionRefusedError(10061, 'No connection could be made because the target machine actively refused it')",
        "Authentication failed: BadUserAccessDenied",
        "opc.tcp://10.0.0.5:4840/ua is not reachable",
        "monitored items refused: ['temp_inner_01', 'temp_inner_02']",
    ],
)
def test_ordinary_error_text_is_left_alone(plain):
    assert scrub(plain) == plain


def test_text_is_cleaned_before_it_is_cut_so_a_cut_cannot_expose_the_start_of_a_secret():
    raw = "x" * 490 + "opc.tcp://admin:hunter2@host:4840/ca"  # the 512-character cut would fall inside the user information
    text, truncated = clean_detail(raw)
    assert "hunter2" not in text and "admin" not in text and "***@" in text
    assert truncated is True and len(text) == DETAIL_LIMIT


@pytest.mark.parametrize(
    "pathological",
    ["x" * 1_000_000, "token" * 200_000, "a://" * 250_000, "password=" * 100_000],
    ids=["one-long-word", "keyword-repeated", "scheme-repeated", "secret-repeated"],
)
def test_cleaning_a_huge_or_hostile_message_costs_milliseconds_not_minutes(pathological):
    """The text is cleaned on the event loop when it is queued: a library message of any size must not stall it.  Above
    the limit it is left out unseen."""
    started = time.monotonic()
    text, truncated = clean_detail(pathological)
    assert time.monotonic() - started < 2.0 and text is None and truncated is True


@pytest.mark.parametrize(
    "hostile",
    ["token" * 819, "a://" * 1024, "password=" * 455, "a://" * 511 + "@"],
    ids=["keyword-repeated", "scheme-repeated-no-at-sign", "secret-repeated", "schemes-then-an-at-sign"],
)
def test_cleaning_a_hostile_message_of_the_largest_size_that_is_still_cleaned_costs_milliseconds(hostile):
    """The cleaning itself has to survive what is just under the limit: a URL rule that reaches to the nearest `@` scans
    on from every `scheme://` when there is none."""
    assert len(hostile) <= 4096
    started = time.monotonic()
    text, _ = clean_detail(hostile)
    assert time.monotonic() - started < 2.0 and len(text) <= DETAIL_LIMIT


def test_long_text_is_cut_to_the_limit_and_says_so_and_short_text_is_not_marked():
    text, truncated = clean_detail("y" * 4000)
    assert len(text) == 512 and truncated is True
    assert clean_detail("y" * 512) == ("y" * 512, False)
    assert clean_detail(None) == (None, False)
