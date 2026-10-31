"""2026-10-30 audit, user ruling: on OPC UA a refused monitored item or a queue overflow costs that point, not the source.

Why it matters (the hard rule comes before completeness): one tag deleted or renamed on the DCS during an overhaul used to
refuse the whole subscription again and again - all the source's points gone for as long as the tag stayed away; one
point's queue overflowing in a unit trip, exactly when the data matters, restarted the whole source.  What stays per
source is timeliness: a subscription never re-sends an unchanged value, so a point alone cannot tell it is stale - the
3 s / 6 s checks of the source decide for all of its points, as before.  A refused item is asked for again with the next
subscription (a rebuild or a restart, option A of the ruling); nothing retries it in between.
"""

import asyncio

from acquirer_support import drain_until, stop
from fault_support import at, fault_owner, faults_of, feed, keepalive, tick
from owner_support import make_owner, notify, small_config
from protocol_support import FieldUa
from test_comm_facts import attempt_entries, ua_bench, ua_points

from htsg import records as r

OVERFLOW = 0x00000480  # Good, with the info bits "the monitored item's queue discarded values"


def test_a_refused_item_makes_only_its_point_and_what_needs_it_unavailable_until_the_next_subscription():
    config = small_config(extra_derived=[{"id": "mx", "inputs": ["to"], "operation": "maximum", "unit": "degC"}])
    owner, recorder = make_owner(config, generations=("ua",))
    owner.items_refused("ua", 0, ["to"], 1000.2, 100.2)  # an older generation's word counts for nothing
    owner.items_refused("ua", 1, ["ti"], 1000.5, 100.5)
    owner.accept(notify([("to", 450.0)]))
    views = owner.channel_views()
    assert (views["ti"]["valid"], views["ti"]["reason"]) == (False, "ITEM_REFUSED"), "the page says why"
    assert views["to"]["valid"] and (views["mx"]["valid"], views["mx"]["value"]) == (True, 450.0), "the others go on"
    assert (views["dt"]["valid"], views["dt"]["reason"]) == (False, "INPUT_UNAVAILABLE"), "a result that needs it cannot be had"
    assert (views["sp"]["valid"], views["sp"]["reason"]) == (False, "INPUT_UNAVAILABLE")
    s = owner.sources["ua"]
    assert s.state == "VALID" and s.resync_count == 0, "no rebuild for it"
    stored = [st.detail["points"] for g in recorder.commits for st in g.states if st.kind == "ITEMS_REFUSED"]
    assert stored == [["ti"]], "history says 'not collected' for it in this segment, not 'unchanged'"
    (entry,) = [e for e in owner.commlog.entries.values() if e.event == "ITEMS_REFUSED"]
    assert entry.detail == "ti"
    owner.begin_generation("ua", 2, 1020.0, 120.0)  # the next subscription asks for it again; the server takes it now
    owner.accept(notify([("ti", 480.0), ("to", 450.0)], gen=2, wall=1021.0, mono=121.0))
    views = owner.channel_views()
    assert views["ti"]["valid"] and views["dt"]["valid"] and views["dt"]["value"] == 30.0


def test_an_overflow_restarts_the_fault_clock_of_its_point_and_of_what_is_derived_from_it():
    """The values dropped between might have been back inside the limit: like a second version in one batch (§8.2), the
    5 s are counted again from the overflowed notification, for the point and for the results computed from it."""
    owner, sink, _ = fault_owner()
    feed(owner, 100.0, [("ti", 495.0), ("to", 430.0)])  # ti above 490, dt = 65 above 55: both clocks start
    feed(owner, 103.0, [("ti", 496.0, r.GOOD, OVERFLOW)], seq=2)
    keepalive(owner, 105.0)  # the subscription stays alive; nothing changes
    tick(owner, 105.5)
    assert faults_of(sink) == [], "without the overflow both would have been confirmed at 105"
    keepalive(owner, 107.0)
    tick(owner, 108.1)
    assert sorted(f.point_id for f in faults_of(sink)) == ["dt", "ti"], "confirmed from the overflow on"
    assert owner.sources["ua"].state == "VALID" and owner.health["item_overflows"] == 1
    overflowed = [st for g in sink.groups for st in g.states if st.kind == "ITEM_OVERFLOW"]
    assert [st.detail["points"] for st in overflowed] == [["ti"]] and overflowed[0].at_wall == at(103.0)[0]


def test_an_overflow_of_a_point_without_limits_changes_nothing_but_the_record():
    owner, sink, _ = fault_owner()
    feed(owner, 100.0, [("ti", 480.0, r.GOOD, OVERFLOW), ("to", 470.0)])
    assert owner.channel_views()["ti"]["valid"] and owner.channel_views()["dt"]["value"] == 10.0
    assert faults_of(sink) == []


async def test_on_a_real_server_one_unknown_node_leaves_the_others_subscribed():
    field = FieldUa(["a", "b"])
    await field.start()
    try:
        owner, ingress, build = ua_bench(field.endpoint, ua_points(field, "a", "missing", "b"))
        acquirer, gate = build(10_000)
        task = asyncio.create_task(acquirer.run())
        try:
            await drain_until(
                ingress, lambda: owner.points["a"].obs is not None and owner.points["b"].obs is not None, why="first values"
            )
        finally:
            await stop(task)
        ingress.drain()
        s = owner.sources["ua"]
        assert (s.state, s.resync_count, s.conn_gen) == ("VALID", 0, 1), "one subscription, never rebuilt"
        assert [x["point"] for x in acquirer.negotiated.refused] == ["missing"]
        assert acquirer.negotiated.as_dict()["items"] == 2, "the granted parameters are those of the items taken"
        assert owner.points["missing"].reason == "ITEM_REFUSED" and owner.channel_views()["a"]["valid"]
        assert attempt_entries(owner, "SUBSCRIBE") == [], "a subscription that took items is no failed attempt"
    finally:
        await field.stop()
