"""R03b / Q07: which stored segment an archive recovery closes comes from what the archive HOLDS, and one recovery
unit at a time (终版 §5 C2, C3).

Why it matters: after a loss the owner used to guess the stored segment to close (`lost_seg`, "the one that was open
when the loss began") - and guessed wrong whenever the segment had been left, or a new one begun, while the archive
was away: the stored segment stayed open for ever and a "phantom" segment appeared with an end and no start.  A second
recovery unit built while the first was still on its way could overlap its gap and close a segment before it began.
Now the owner keeps the archive's own segment facts (a segment is open there once its SEG_START is CONFIRMED and until
its SEG_END is), keeps the end of every segment that was left but not yet confirmed, and has at most one unit on its
way per source; its SEG_END takes the earlier of that end and the start of the gap.

The writer here is the commit queue's contract as the owner sees it: an offer is accepted or turned away; a void group
is dropped without a word; the others are confirmed, refused, or held at the head (the queue's retry of a UNKNOWN commit).
"""

import random

import pytest
from owner_support import FixedBucket, Recorder
from test_source_validity import MONO0, WALL0, at, data, keepalive, tick, ua_only

from htsg import history as h
from htsg import owner as owner_module
from htsg import records as r
from htsg.owner import Owner

CONFIG = ua_only()


class Writer:
    def __init__(self):
        self.accepting, self.queue, self.stored, self.voided = True, [], [], []
        self.policy = lambda group: "COMMIT"  # COMMIT | REFUSE | HOLD, decided when the group is at the head
        self.owner = None

    def __call__(self, group):
        if not self.accepting:
            return False
        self.queue.append(group)
        return True

    def step(self):
        if not self.queue:
            return False
        group = self.queue[0]
        if self.owner.is_void(group):
            self.voided.append(self.queue.pop(0))
            return True
        verdict = self.policy(group)
        if verdict == "HOLD":
            return False
        self.queue.pop(0)
        if verdict == "COMMIT":
            self.stored.append(group)
            self.owner.committed(group)
        else:
            self.owner.refused(group, "POLICY")
        return True

    def settle(self):
        while self.step():
            pass

    def units(self):
        """The recovery units on their way: a recovery group that carries the gap (an ordinary recovery of the source, after
        a short silence, is a segment group too, but no unit)."""
        return [g for g in self.queue if g.kind == r.GROUP_RECOVERY and any(s.kind == "GAP" for s in g.states)]


def only(kind):
    return lambda verdict: (lambda group: verdict if group.kind == kind else "COMMIT")


def world():
    writer, rec = Writer(), Recorder()
    owner = Owner(CONFIG, WALL0, MONO0, commit=writer, publish=rec.publish, run_id="run", buckets=FixedBucket())
    writer.owner = owner
    owner.begin_generation("ua", 1, WALL0, MONO0)
    data(owner, 101.0)
    writer.settle()  # segment 1 and its first data are durable
    return owner, writer


def lose(owner, writer, mono, seq=2, value=501.0):
    """The next offer is turned away (the queue is full): the archive is lost from this moment."""
    writer.accepting = False
    data(owner, mono, items=(("ti", value),), seq=seq)
    writer.accepting = True


def facts(groups):
    starts, ends, gaps = {}, {}, []
    for group in groups:
        for s in group.states:
            if s.kind == "SEG_START":
                starts[s.seg] = s.at_wall
            elif s.kind == "SEG_END":
                ends.setdefault(s.seg, []).append(s.detail.get("end_wall", s.at_wall))
            elif s.kind == "GAP":
                gaps.append((s.detail["start_wall"], s.detail["end_wall"]))
    return starts, ends, sorted(gaps)


def sound(groups, *, closed=True, once=True):
    """The invariants of stored history: no end without a start, one end (`once`) - or, where an end whose group was still on
    its way was carried by the unit as well, ends that agree - never before its start, gaps apart; and - once everything is
    settled - every segment but the newest has its end."""
    starts, ends, gaps = facts(groups)
    assert set(ends) <= set(starts), f"a segment with an end and no start: {sorted(set(ends) - set(starts))}"
    for seg, found in ends.items():
        assert len(found) == 1 if once else len(set(found)) == 1, f"segment {seg} was ended {found}"
        assert found[0] >= starts[seg], f"segment {seg} ends at {found[0]} before it began at {starts[seg]}"
    for (_, end), (start, _) in zip(gaps, gaps[1:], strict=False):
        assert end <= start, f"gaps overlap: {gaps}"
    if closed and starts:
        assert set(starts) - set(ends) == {max(starts)}, f"open segments: {sorted(set(starts) - set(ends))}"
    return starts, ends, gaps


def in_database(factory, groups):
    store = factory()
    store.initialize(CONFIG)
    store.bind_run("run", CONFIG.config_id, WALL0)
    for group in groups:
        assert store.write_group(group).durable, group.group_id
    with store.session() as conn:
        segments = conn.execute("SELECT seg,start_wall,end_wall FROM segment ORDER BY seg").fetchall()
        gaps = conn.execute("SELECT start_wall,end_wall FROM archive_gap ORDER BY start_wall").fetchall()
    assert all(x["start_wall"] is not None for x in segments), "a phantom segment: an end and no start"
    assert all(x["end_wall"] is None or x["end_wall"] >= x["start_wall"] for x in segments)
    assert [x["seg"] for x in segments if x["end_wall"] is None] == [segments[-1]["seg"]], "only the newest is open"
    for first, second in zip(gaps, gaps[1:], strict=False):
        assert first["end_wall"] <= second["start_wall"]
    return segments, gaps


def the_unit(writer):
    (unit,) = writer.units()
    return unit


# ================================================================== Q07's four sequences


def test_1_left_by_expiry_while_the_archive_is_away_the_stored_segment_is_still_closed(archive_db_factory):
    """The loss comes first; then the source goes quiet (its SEG_END cannot be queued) and comes back within 6 s."""
    owner, writer = world()
    lose(owner, writer, 102.0)
    tick(owner, 105.0)  # 3 s without a valid response: EXPIRED
    data(owner, 106.0, items=(("ti", 502.0),), seq=3)  # back inside the window: the recovery unit
    unit = the_unit(writer)
    assert [(s.seg, s.detail["end_wall"]) for s in unit.states if s.kind == "SEG_END"] == [(1, at(102.0)[0])], (
        "the stored segment 1 ends where the loss began (earlier than the expiry at 105)"
    )
    writer.settle()
    sound(writer.stored)
    segments, gaps = in_database(archive_db_factory, writer.stored)
    assert [(x["seg"], x["end_wall"] is not None) for x in segments] == [(1, True), (2, False)]


def test_2_a_new_generation_whose_group_was_turned_away_leaves_segment_1_to_be_closed_not_the_one_never_stored(
    archive_db_factory,
):
    owner, writer = world()
    writer.accepting = False
    owner.begin_generation("ua", 2, *at(102.0))  # SEG_END 1 + SEG_START 2: turned away
    writer.accepting = True
    tick(owner, 103.0)
    unit = the_unit(writer)
    assert [s.seg for s in unit.states if s.kind == "SEG_END"] == [1], "segment 2 was never stored: no end for it"
    writer.settle()
    starts, ends, _ = sound(writer.stored)
    assert 2 not in starts and 2 not in ends, "no phantom segment 2"
    in_database(archive_db_factory, writer.stored)


def test_2_prime_a_disconnect_whose_end_was_turned_away_still_closes_the_stored_segment(archive_db_factory):
    owner, writer = world()
    writer.accepting = False
    owner.source_down("ua", 1, "CONNECTION", *at(103.0))  # its SEG_END group is the one turned away
    writer.accepting = True
    owner.begin_generation("ua", 2, *at(104.0))
    tick(owner, 105.0)
    unit = the_unit(writer)
    assert [(s.seg, s.detail["end_wall"]) for s in unit.states if s.kind == "SEG_END"] == [(1, at(103.0)[0])]
    writer.settle()
    sound(writer.stored)
    in_database(archive_db_factory, writer.stored)


def test_3_a_recovery_unit_that_is_turned_away_after_the_pending_overflow_leaves_the_first_loss_to_be_closed(
    archive_db_factory,
):
    owner, writer = world()
    owner.pending_limit = 2
    lose(owner, writer, 102.0)
    tick(owner, 103.0)  # the first unit is on its way
    writer.policy = only(r.GROUP_RECOVERY)("HOLD")
    for k, mono in enumerate((104.0, 105.0, 106.0)):  # two wait behind it, the third overflows the wait
        data(owner, mono, items=(("ti", 510.0 + k),), seq=3 + k)
    assert owner.sources["ua"].archive == "LOST"
    writer.policy = only(r.GROUP_RECOVERY)("REFUSE")
    writer.settle()  # the first unit is turned away
    tick(owner, 107.0)
    second = the_unit(writer)
    assert [(s.seg, s.detail["end_wall"]) for s in second.states if s.kind == "SEG_END"] == [(1, at(102.0)[0])]
    assert [s.detail["start_wall"] for s in second.states if s.kind == "GAP"] == [at(102.0)[0]], "the earliest loss still counts"
    writer.policy = lambda group: "COMMIT"
    writer.settle()
    starts, ends, gaps = sound(writer.stored)
    assert 2 not in starts, "the segment the refused unit began was never stored"
    in_database(archive_db_factory, writer.stored)


# =============================================== M1: the first unit settles in each of the possible orders


def overflow_during_the_first_unit():
    owner, writer = world()
    owner.pending_limit = 2
    lose(owner, writer, 102.0)
    tick(owner, 103.0)
    first = the_unit(writer)
    writer.policy = only(r.GROUP_RECOVERY)("HOLD")  # the first unit is on its way, not yet settled
    for k, mono in enumerate((104.0, 105.0, 106.0)):
        data(owner, mono, items=(("ti", 510.0 + k),), seq=3 + k)
    assert owner.sources["ua"].archive == "LOST" and writer.units() == [first]
    return owner, writer, first


def test_while_the_first_unit_is_on_its_way_no_second_one_is_built_and_the_first_is_never_voided():
    owner, writer, first = overflow_during_the_first_unit()
    for mono in (107.0, 108.0, 109.0):  # an unsettled (even a UNKNOWN) unit holds everything up
        keepalive(owner, mono)
        tick(owner, mono)
        assert writer.units() == [first] and owner.is_void(first) is False


@pytest.mark.parametrize("ticks_first", [False, True], ids=["confirmed-before-the-next-tick", "confirmed-after-it"])
def test_a_first_unit_confirmed_after_the_overflow_keeps_the_archive_lost_and_the_next_gap_starts_at_the_overflow(
    archive_db_factory, ticks_first
):
    owner, writer, first = overflow_during_the_first_unit()
    if ticks_first:
        keepalive(owner, 107.0)
        tick(owner, 107.0)
        assert writer.units() == [first], "no second unit while the first is on its way"
    keepalive(owner, 108.0)
    writer.policy = lambda group: "COMMIT"
    writer.step()  # the first unit is confirmed
    assert owner.sources["ua"].archive == "LOST", "lost again after it: not recovered"
    tick(owner, 108.0)
    second = the_unit(writer)
    (gap,) = [s for s in second.states if s.kind == "GAP"]
    assert gap.detail["start_wall"] == at(106.0)[0], "from the overflow, not from the first loss: the confirmed baseline is not covered"
    assert [(s.seg, s.detail["end_wall"]) for s in second.states if s.kind == "SEG_END"] == [(2, at(106.0)[0])]
    writer.settle()
    starts, ends, gaps = sound(writer.stored)
    assert gaps == [(at(102.0)[0], at(103.0)[0]), (at(106.0)[0], at(108.0)[0])]
    assert owner.sources["ua"].archive == "OK"
    in_database(archive_db_factory, writer.stored)


def test_a_unit_held_for_a_long_time_and_confirmed_twice_is_settled_once():
    """UNKNOWN keeps the unit at the head for as long as it takes; a repeated confirmation (ALREADY) changes nothing."""
    owner, writer = world()
    lose(owner, writer, 102.0)
    tick(owner, 103.0)
    unit = the_unit(writer)
    writer.policy = only(r.GROUP_RECOVERY)("HOLD")
    for mono in range(104, 114):
        keepalive(owner, float(mono))
        tick(owner, float(mono))
        assert writer.units() == [unit], "no second unit while the first is held"
    writer.policy = lambda group: "COMMIT"
    writer.settle()
    assert owner.sources["ua"].archive == "OK"
    recoveries = owner.health["archive_recoveries"]
    owner.committed(unit)  # the same group confirmed again
    assert owner.sources["ua"].archive == "OK" and owner.health["archive_recoveries"] == recoveries
    sound(writer.stored)


def test_long_loss_does_not_accumulate_closing_intentions_for_segments_that_can_never_reach_the_archive():
    """R03b/Q07: while LOST, repeated reconnect generations are realtime-only; their SEG_START groups are neither
    durable nor queued, so their later SEG_END intentions must not grow memory without bound."""
    owner, writer = world()
    lose(owner, writer, 102.0)
    gen = 1
    for mono in range(103, 1103):
        owner.source_down("ua", gen, "CONNECTION", *at(float(mono)))
        gen += 1
        owner.begin_generation("ua", gen, *at(float(mono) + 0.1))
    source = owner.sources["ua"]
    assert source.durable == {1} and source.opening == {}
    assert source.closing == {1: ("DOWN:CONNECTION", at(103.0)[0])}


def test_shutdown_during_recovery_submits_run_end_only_after_the_recovery_pending_data_and_segment_end():
    """R04/Q03: RUN_END is a durability statement, so a recovery unit and every accepted group waiting behind it
    must settle before the owner is allowed to offer the stable run-end group."""
    owner, writer = world()
    lose(owner, writer, 102.0)
    tick(owner, 103.0)
    recovery = the_unit(writer)
    writer.policy = only(r.GROUP_RECOVERY)("HOLD")
    data(owner, 104.0, items=(("ti", 510.0),), seq=3)
    owner.shutdown(*at(105.0))
    assert all(not any(s.kind == "RUN_END" for s in g.states) for g in [*writer.stored, *writer.queue])
    assert owner.sources["ua"].pending, "the data and shutdown SEG_END wait behind the recovery unit"

    writer.policy = lambda group: "COMMIT"
    assert writer.step() and recovery in writer.stored
    writer.settle()
    assert owner.shutdown_progress()
    assert owner.submit_run_end()
    writer.settle()
    assert owner.run_end_status == "COMMITTED"
    kinds = [[s.kind for s in g.states] for g in writer.stored]
    assert kinds[-1] == ["RUN_END"]
    assert any("SEG_END" in found for found in kinds[-3:-1]), "the source ended before the run"


@pytest.mark.parametrize("terminal_verdict", ["COMMIT", "REFUSE"])
def test_shutdown_of_a_lost_source_writes_only_a_terminal_gap_and_segment_end_before_run_end(terminal_verdict):
    """R04/Q03: stopping cannot invent a C2 baseline.  A LOST source gets one terminal archive unit; only its durable
    confirmation permits NORMAL, while an explicit refusal leaves the run unfinished."""
    owner, writer = world()
    lose(owner, writer, 102.0)
    tick(owner, 103.0)
    writer.policy = only(r.GROUP_RECOVERY)("REFUSE")
    writer.settle()
    assert owner.sources["ua"].archive == "LOST"

    writer.policy = only(r.GROUP_RECOVERY)(terminal_verdict)
    owner.shutdown(*at(104.0))
    terminal = owner.sources["ua"].shutdown_group
    assert terminal is not None
    assert [s.kind for s in terminal.states] == ["GAP", "SEG_END"]
    assert not terminal.observations and not terminal.results
    writer.settle()

    if terminal_verdict == "REFUSE":
        assert owner.sources["ua"].shutdown_failed and owner.submit_run_end() is False
        assert all(not any(s.kind == "RUN_END" for s in g.states) for g in writer.stored)
    else:
        assert owner.sources["ua"].shutdown_done and owner.submit_run_end()
        writer.settle()
        assert owner.run_end_status == "COMMITTED"
        assert [s.kind for s in writer.stored[-1].states] == ["RUN_END"]


def spans_of(result):
    return [
        (s["seg"], s["start_wall"], s["end_wall"], s["end_reason"], [x["time"] for x in s["rows"]])
        for s in result["channels"]["ti"]["segments"]
    ]


def test_segment_fragments_spread_over_three_databases_read_as_the_same_history_as_in_one(archive_db_factory):
    """细则 6.2 ④: the start of a segment, its data and its end may each sit in another database."""
    owner, writer = world()
    lose(owner, writer, 102.0)
    tick(owner, 105.0)
    data(owner, 106.0, items=(("ti", 502.0),), seq=3)
    writer.settle()
    data(owner, 107.0, items=(("ti", 503.0),), seq=4)
    owner.source_down("ua", 1, "CONNECTION", *at(108.0))
    owner.begin_generation("ua", 2, *at(109.0))
    data(owner, 110.0, items=(("ti", 504.0),), seq=5, gen=2)
    writer.settle()
    groups = writer.stored
    sound(groups, closed=False)
    stores = [archive_db_factory("primary"), archive_db_factory("emergency"), archive_db_factory("primary")]
    for store in stores:
        store.initialize(CONFIG)
        store.bind_run("run", CONFIG.config_id, WALL0)
    for i, group in enumerate(groups):  # the groups go where they go: no database holds a whole segment
        assert stores[i % 3].write_group(group).durable
    whole = archive_db_factory("primary")
    whole.initialize(CONFIG)
    whole.bind_run("run", CONFIG.config_id, WALL0)
    for group in groups:
        assert whole.write_group(group).durable
    channels, start, end = {"ti": "ua"}, WALL0, WALL0 + 60.0
    three = h.query([("primary", stores[0]), ("emergency", stores[1]), ("third", stores[2])], channels, start, end)
    one = h.query([("primary", whole)], channels, start, end)
    assert three["partial"] is False and spans_of(three) == spans_of(one)
    assert all(s[1] is not None and (s[2] is None or s[2] >= s[1]) for s in spans_of(three)), "no phantom, no end before a start"


# ======================================================================== random sequences, same invariants


@pytest.mark.parametrize("seed", range(6))
def test_whatever_the_order_of_losses_failures_and_confirmations_stored_history_stays_sound(seed, archive_db_factory):
    rng = random.Random(seed)
    owner, writer = world()
    owner.pending_limit = 3
    mono, seq, gen = 101.0, 2, 1
    for _ in range(260):
        mono += 1.0
        event = rng.random()
        if event < 0.40:
            seq += 1
            writer.accepting = rng.random() > 0.15
            data(owner, mono, items=(("ti", 500.0 + seq),), seq=seq, gen=gen)
            writer.accepting = True
        elif event < 0.55:
            tick(owner, mono)
        elif event < 0.62:
            writer.accepting = rng.random() > 0.3
            owner.source_down("ua", gen, "CONNECTION", *at(mono))
            writer.accepting = True
            gen += 1
            writer.accepting = rng.random() > 0.3
            owner.begin_generation("ua", gen, *at(mono))
            writer.accepting = True
        verdicts = ["COMMIT"] * 6 + ["REFUSE"] * 2 + ["HOLD"] * 2
        writer.policy = lambda group, v=rng.choice(verdicts): v
        for _ in range(rng.randint(0, 3)):
            writer.step()
        assert len(writer.units()) <= 1, "at most one unit on its way"
        for unit in writer.units():
            assert owner.is_void(unit) is False
        sound(writer.stored, closed=False, once=False)
    writer.policy = lambda group: "COMMIT"
    for _ in range(12):  # let everything settle: the source keeps talking, the archive takes everything
        mono += 1.0
        seq += 1
        if owner.sources["ua"].state == "RESYNC":  # what the acquirer does when the owner asks for a new generation
            gen += 1
            owner.begin_generation("ua", gen, *at(mono))
        data(owner, mono, items=(("ti", 500.0 + seq),), seq=seq, gen=gen)
        tick(owner, mono)
        writer.settle()
    assert owner.sources["ua"].archive == "OK" and not writer.queue
    sound(writer.stored, once=False)
    in_database(archive_db_factory, writer.stored)


# ===================================================== a normal stop completes, whatever happened before it (R04 / Q03)


def test_a_new_segment_dropped_with_a_refused_unit_does_not_keep_a_later_normal_stop_from_completing():
    """The start of a segment that waited in `pending` behind the recovery unit is dropped when the unit is turned away.
    Its entry in `opening` ("may still become durable") has to go with it: kept, the source counted as unsettled for ever,
    the stop waited out its whole deadline, and the run was left to be recorded as an ABNORMAL_EXIT."""
    owner, writer = world()
    lose(owner, writer, 102.0)
    tick(owner, 103.0)  # the recovery unit is built and queued
    unit = the_unit(writer)
    owner.source_down("ua", 1, "CONNECTION", *at(103.2))  # the source reconnects while the unit is on its way:
    owner.begin_generation("ua", 2, *at(103.5))  # the end of segment 1 and the start of the next wait in pending
    s = owner.sources["ua"]
    assert s.pending and s.opening, "the start of the new segment waits behind the unit"
    writer.policy = lambda group: "REFUSE" if group is unit else "COMMIT"
    writer.settle()  # the unit is turned away, and what waited behind it is dropped with it
    assert s.archive == "LOST" and not s.pending
    assert s.opening == {} and set(s.closing) <= s.durable, "nothing that was dropped can still become durable"
    writer.policy = lambda group: "COMMIT"
    for mono in (106.0, 107.0, 108.0):  # the archive is back: the source recovers and everything is confirmed
        data(owner, mono, items=(("ti", 510.0 + mono),), seq=int(mono), gen=2)
        tick(owner, mono)
        writer.settle()
    assert s.archive == "OK" and not writer.queue
    owner.shutdown(*at(110.0))
    writer.settle()
    assert owner.shutdown_progress() and s.shutdown_done, "everything is confirmed: the stop can register the end"


def random_history(seed, *, settle):
    """The event mix of the soundness test above: a random number of steps - or, with `settle`, 260 steps and then a quiet,
    fully confirmed archive.  Returns the generator, the owner, the writer and the monotonic time reached."""
    rng = random.Random(seed)
    owner, writer = world()
    owner.pending_limit = 3
    mono, seq, gen = 101.0, 2, 1
    for _ in range(260 if settle else rng.randint(5, 200)):
        mono += 1.0
        event = rng.random()
        if event < 0.40:
            seq += 1
            writer.accepting = rng.random() > 0.15
            data(owner, mono, items=(("ti", 500.0 + seq),), seq=seq, gen=gen)
            writer.accepting = True
        elif event < 0.55:
            tick(owner, mono)
        elif event < 0.62:
            writer.accepting = rng.random() > 0.3
            owner.source_down("ua", gen, "CONNECTION", *at(mono))
            writer.accepting = True
            gen += 1
            writer.accepting = rng.random() > 0.3
            owner.begin_generation("ua", gen, *at(mono))
            writer.accepting = True
        verdicts = ["COMMIT"] * 6 + ["REFUSE"] * 2 + ["HOLD"] * 2
        writer.policy = lambda group, v=rng.choice(verdicts): v
        for _ in range(rng.randint(0, 3)):
            writer.step()
    if settle:
        writer.policy = lambda group: "COMMIT"
        for _ in range(12):  # the source keeps talking, the archive takes everything
            mono += 1.0
            seq += 1
            if owner.sources["ua"].state == "RESYNC":
                gen += 1
                owner.begin_generation("ua", gen, *at(mono))
            data(owner, mono, items=(("ti", 500.0 + seq),), seq=seq, gen=gen)
            tick(owner, mono)
            writer.settle()
        assert owner.sources["ua"].archive == "OK" and not writer.queue
    return rng, owner, writer, mono


def stop_of(seed, *, settle, refuse=0.0):
    """Stop after a random history, the way the runtime does: the archive is back (and turns away a share `refuse` of what
    it is offered), the writer works, and the runtime asks again how far the stop is.  Returns what is left of the stop and,
    for a stop that completed, what is wrong with the history it left behind."""
    rng, owner, writer, mono = random_history(seed, settle=settle)
    writer.accepting = True
    writer.policy = lambda group: "REFUSE" if rng.random() < refuse else "COMMIT"
    owner.shutdown(*at(mono + 1.0))
    s = owner.sources["ua"]
    for _ in range(60):
        writer.settle()
        if owner.shutdown_progress() or s.shutdown_failed:
            break
    if not owner.shutdown_progress():
        left = {"opening": dict(s.opening), "closing": sorted(s.closing), "durable": sorted(s.durable),
                "pending": len(s.pending), "inflight": sorted(s.inflight), "archive": s.archive}  # fmt: skip
        return (None if s.shutdown_failed and refuse else left), None  # a refusal is a way to end only where one was provoked
    try:
        starts, ends, _ = sound(writer.stored, closed=False, once=False)
        assert set(starts) == set(ends), f"segments left open: {sorted(set(starts) - set(ends))}"
    except AssertionError as error:
        return None, str(error)
    return None, None


@pytest.mark.parametrize("settle", [False, True], ids=["stopped-at-a-random-moment", "stopped-after-everything-settled"])
def test_a_stop_completes_and_leaves_sound_history_whatever_came_before_it(settle):
    """R04/Q03: with an archive that takes everything again, a normal stop can always register the end of the run - after a
    loss, a unit on its way, a reconnect while it was, a unit turned away, a pending list that overflowed."""
    stuck, unsound = {}, {}
    for seed in range(300):
        left, wrong = stop_of(seed, settle=settle)
        if left is not None:
            stuck[seed] = left
        if wrong is not None:
            unsound[seed] = wrong
    assert not stuck, f"{len(stuck)} of 300 stops could never complete, e.g. {dict(list(stuck.items())[:2])}"
    assert not unsound, f"{len(unsound)} of 300 stops left unsound history, e.g. {dict(list(unsound.items())[:2])}"


def test_a_stop_is_never_left_hanging_when_the_archive_turns_part_of_it_away():
    """What the archive turns away at the stop ends it with an explicit failure (the run is then not registered as ended);
    it must never leave it waiting for something that can no longer happen."""
    stuck = {}
    for seed in range(300):
        left, _ = stop_of(seed, settle=False, refuse=0.3)
        if left is not None:
            stuck[seed] = left
    assert not stuck, f"{len(stuck)} of 300 stops neither completed nor failed, e.g. {dict(list(stuck.items())[:2])}"


# ====================================================== a clock that steps while the archive is not OK (audit must-fix 4)


def step_clock(owner, mono, shift):
    """A tick at `mono` whose wall clock reads `shift` seconds off what it should: the monotonic clock does not care."""
    owner.tick(at(mono)[0] + shift, mono)


def stepped_data(owner, mono, shift, value, seq):
    wall, mono = at(mono)
    return owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, wall + shift, mono, (r.Item("ti", value),), publish_seq=seq))


def stepped_keepalive(owner, mono, shift):
    """Keeps the source valid across the steps: a source silent for 3 s is EXPIRED, and an expired one gets no unit."""
    wall, mono = at(mono)
    return owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, wall + shift, mono))


def one_clock_per_segment(groups):
    """Inside a stored segment the wall label never runs backwards: what it holds is one clock's time."""
    walls = {}
    for group in groups:
        for o in group.observations:
            if o.origin == r.ORIGIN_RECEIVED:
                walls.setdefault(o.seg, []).append((o.batch_seq, o.received_wall))
    for seg, found in walls.items():
        ordered = [wall for _, wall in sorted(found)]
        assert ordered == sorted(ordered), f"segment {seg} holds both halves of a stepped clock: {ordered}"


STEPS = pytest.mark.parametrize("shift", [-20.0, 3600.0], ids=["step-back", "step-forward"])


@STEPS
def test_a_clock_that_steps_while_the_unit_is_on_its_way_cuts_the_segment_the_unit_opens(shift, archive_db_factory):
    """The unit's segment used to take both halves of the stepped clock: the cut was made only while the archive was OK."""
    owner, writer = world()
    lose(owner, writer, 102.0)
    tick(owner, 103.0)  # the unit is built and queued
    assert owner.sources["ua"].archive == "RECOVERING"
    data(owner, 103.5, items=(("ti", 505.0),), seq=3)  # waits behind the unit, still on the old clock
    step_clock(owner, 104.0, shift)  # the clock steps while the unit is on its way: the cut waits behind it as well
    stepped_data(owner, 105.0, shift, 510.0, seq=4)
    writer.settle()
    starts, ends, _ = sound(writer.stored)
    assert set(starts) == {1, 2, 3} and ends[2] == [at(103.0)[0]], "the unit's segment ends where the old clock stopped"
    reasons = {s.seg: s.detail["reason"] for g in writer.stored for s in g.states if s.kind == "SEG_END"}
    assert reasons == {1: "ARCHIVE_GAP", 2: "CLOCK_JUMP"}
    one_clock_per_segment(writer.stored)
    in_database(archive_db_factory, writer.stored)


@STEPS
def test_a_clock_that_steps_while_the_archive_is_lost_rides_on_the_unit_and_leaves_no_gap_that_ends_before_it_began(
    shift, archive_db_factory
):
    owner, writer = world()
    lose(owner, writer, 102.0)
    writer.accepting = False  # the unit tried at the stepped tick is turned away too
    step_clock(owner, 104.0, shift)
    s = owner.sources["ua"]
    assert s.archive == "LOST" and [m.kind for m in s.owed_jumps] == ["CLOCK_JUMP"]
    writer.accepting = True
    stepped_keepalive(owner, 104.5, shift)
    step_clock(owner, 105.0, shift)
    unit = the_unit(writer)
    assert [x.kind for x in unit.states].count("CLOCK_JUMP") == 1, "nothing could carry the marker across the gap but the unit"
    gap = next(x for x in unit.states if x.kind == "GAP")
    assert gap.detail["end_wall"] >= gap.detail["start_wall"], "a clock stepped back does not make a gap end before it began"
    writer.settle()
    assert s.archive == "OK" and s.owed_jumps == [], "stored, so no longer owed"
    sound(writer.stored)
    in_database(archive_db_factory, writer.stored)


@STEPS
def test_a_cut_for_a_clock_step_that_was_turned_away_still_ends_the_stored_segment_where_the_clock_stepped(
    shift, archive_db_factory
):
    """The loss began at the stepped clock; comparing the two wall times, a step back made the segment look cut off by the
    gap - and end before its own data.  A segment left by a clock step was left before the gap, whatever the labels say."""
    owner, writer = world()
    tick(owner, 102.0)
    keepalive(owner, 103.5)
    writer.accepting = False
    step_clock(owner, 104.0, shift)  # the cut (SEG_END 1 + SEG_START 2) is turned away
    assert owner.sources["ua"].archive == "LOST"
    writer.accepting = True
    stepped_keepalive(owner, 104.5, shift)
    step_clock(owner, 105.0, shift)
    unit = the_unit(writer)
    assert [(x.seg, x.detail["reason"], x.detail["end_wall"]) for x in unit.states if x.kind == "SEG_END"] == [
        (1, "CLOCK_JUMP", at(102.0)[0])
    ]
    writer.settle()
    sound(writer.stored)
    in_database(archive_db_factory, writer.stored)


# ====================================== the clock-jump markers a long loss cannot hold (rulings of 2026-10-20, item 10b)


def many_clock_steps_while_lost(steps):
    """The archive is lost, and then the wall clock steps `steps` times, alternately forward and back, before anything can be stored."""
    owner, writer = world()
    lose(owner, writer, 102.0)
    writer.accepting = False
    for k in range(steps):
        shift = 3600.0 if k % 2 == 0 else 0.0
        step_clock(owner, 104.0 + 0.1 * k, shift)
        stepped_keepalive(owner, 104.05 + 0.1 * k, shift)
    writer.accepting = True
    return owner, writer


def the_unit_after(owner, writer):
    stepped_keepalive(owner, 106.0, 0.0)
    step_clock(owner, 106.1, 0.0)
    return the_unit(writer)


def dropped_said(group):
    return [x.detail.get("earlier_markers_dropped") for x in group.states if x.kind == "CLOCK_JUMP"]


def stored_markers(factory, groups):
    store = factory()
    store.initialize(CONFIG)
    store.bind_run("run", CONFIG.config_id, WALL0)
    for group in groups:
        assert store.write_group(group).durable, group.group_id
    with store.session() as conn:
        return [x["detail"] for x in conn.execute("SELECT detail FROM state_event WHERE kind='CLOCK_JUMP' ORDER BY at_mono,id").fetchall()]


def test_clock_steps_beyond_the_limit_are_counted_and_the_first_marker_stored_says_how_many_went(archive_db_factory):
    """The oldest markers were dropped without a trace: the stored clock events looked like all of them."""
    owner, writer = many_clock_steps_while_lost(20)
    s = owner.sources["ua"]
    assert s.archive == "LOST" and owner.health["clock_jumps"] == 20
    assert (len(s.owed_jumps), s.owed_dropped, owner.health["clock_markers_dropped"]) == (16, 4, 4), "the newest sixteen are kept"
    unit = the_unit_after(owner, writer)
    assert dropped_said(unit) == [4] + [None] * 15
    writer.settle()
    assert s.archive == "OK" and s.owed_jumps == [] and s.owed_dropped == 0, "stored, so nothing is owed any more"
    assert owner.health["clock_markers_dropped"] == 4, "what went stays counted for the run"
    sound(writer.stored)
    found = stored_markers(archive_db_factory, writer.stored)
    assert len(found) == 16 and [x.get("earlier_markers_dropped") for x in found] == [4] + [None] * 15


def test_up_to_the_limit_nothing_is_dropped_and_nothing_is_said():
    owner, writer = many_clock_steps_while_lost(16)
    s = owner.sources["ua"]
    assert (len(s.owed_jumps), s.owed_dropped, owner.health["clock_markers_dropped"]) == (16, 0, 0)
    assert dropped_said(the_unit_after(owner, writer)) == [None] * 16
    owner, writer = many_clock_steps_while_lost(17)
    assert (owner.sources["ua"].owed_dropped, owner.health["clock_markers_dropped"]) == (1, 1), "the seventeenth is the first to cost one"


def test_a_unit_turned_away_keeps_the_count_and_the_next_one_says_it_again():
    owner, writer = many_clock_steps_while_lost(20)
    writer.policy = only(r.GROUP_RECOVERY)("REFUSE")
    assert dropped_said(the_unit_after(owner, writer)) == [4] + [None] * 15
    writer.settle()
    s = owner.sources["ua"]
    assert s.archive == "LOST" and s.owed_dropped == 4, "nothing was stored, so the loss is still owed"
    writer.policy = lambda group: "COMMIT"
    step_clock(owner, 107.0, 0.0)
    stepped_keepalive(owner, 107.05, 0.0)
    step_clock(owner, 107.1, 0.0)
    assert dropped_said(the_unit(writer)) == [4] + [None] * 15


def test_the_gap_and_the_segments_of_the_unit_do_not_rest_on_the_markers_the_limit_dropped(monkeypatch):
    """The markers are information about the clock; the cut of the history comes from the segment facts.  The same loss with room
    for every marker gives the same gap and the same segment records."""

    def the_rest():
        owner, writer = many_clock_steps_while_lost(20)
        return [x for x in the_unit_after(owner, writer).states if x.kind != "CLOCK_JUMP"]

    capped = the_rest()
    monkeypatch.setattr(owner_module, "OWED_JUMPS_LIMIT", 100)
    assert the_rest() == capped
    assert {"GAP", "SEG_END", "SEG_START"} <= {x.kind for x in capped}


def test_the_final_group_of_a_stop_says_how_many_markers_went_too():
    owner, writer = many_clock_steps_while_lost(20)
    owner.shutdown(*at(106.0))
    terminal = owner.sources["ua"].shutdown_group
    assert dropped_said(terminal) == [4] + [None] * 15
    writer.settle()
    assert owner.sources["ua"].shutdown_done and owner.sources["ua"].owed_dropped == 0
