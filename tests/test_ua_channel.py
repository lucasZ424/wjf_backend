"""D16: complete notification capture and continuity (终版 §4).

Deterministic library-boundary injection: synthetic publish results go through the same
UaChannel the live subscription uses.  Why it matters: a missing, duplicated or half-processed
notification must become an explicit loss (C1), never silently continuous history.
"""

from asyncua import ua

from htsg import records as r
from htsg.config import Point, Settings
from htsg.ingress import Ingress
from htsg.owner import Owner
from htsg.ua import PublishTracker, UaChannel, is_overflow

POINTS = {
    1: Point(id="ti", source="ua", unit="degC", node_id="ns=2;s=ti", extreme_low=-50, extreme_high=800),
    2: Point(id="to", source="ua", unit="degC", node_id="ns=2;s=to", extreme_low=-50, extreme_high=800),
}


def publish(seq, values=(), status=ua.StatusCodes.Good):
    """values: (handle, number[, status code]) tuples; empty means a keep-alive."""
    items = [
        ua.MonitoredItemNotification(
            ClientHandle=v[0],
            Value=ua.DataValue(
                ua.Variant(v[1], ua.VariantType.Double), ua.StatusCode(v[2] if len(v) > 2 else status)
            ),
        )
        for v in values
    ]
    data = [ua.DataChangeNotification(MonitoredItems=items)] if items else None
    return ua.PublishResult(
        SubscriptionId=1, NotificationMessage=ua.NotificationMessage(SequenceNumber=seq, NotificationData=data)
    )


def build(capacity=100):
    config = Settings.model_validate(
        {
            "sources": [{"id": "ua", "kind": "opcua", "enabled": True, "endpoint": "opc.tcp://127.0.0.1:1/"}],
            "points": [p.model_dump() for p in POINTS.values()],
        }
    )
    commits, rebuilds = [], []
    owner = Owner(config, 1000.0, 100.0, commit=commits.append, run_id="t")
    owner.on_rebuild = lambda source, reason: rebuilds.append(reason)
    ingress = Ingress(owner, capacity)
    owner.begin_generation("ua", 1, 1000.0, 100.0)
    clock = [1001.0, 101.0]
    channel = UaChannel("ua", 1, ingress, POINTS, clock=(lambda: clock[0], lambda: clock[1]))
    return channel, ingress, owner, commits, rebuilds, clock


def feed(channel, ingress, *results):
    for result in results:
        channel.on_publish(result)
    ingress.drain()


# ------------------------------------------------------------------------------ tracker


def test_in_order_data_and_keepalives_are_accepted():
    t = PublishTracker()
    # data 5, keep-alive announcing 6, data 6, data 7
    assert [t.observe(5, True), t.observe(6, False), t.observe(6, True), t.observe(7, True)] == ["OK"] * 4


def test_a_keepalive_does_not_consume_a_sequence_number():
    t = PublishTracker()
    t.observe(1, True)
    for _ in range(3):
        assert t.observe(2, False) == "OK"
    assert t.observe(2, True) == "OK", "the data message that follows still carries number 2"


def test_duplicates_are_recognised_and_not_counted_as_progress():
    t = PublishTracker()
    t.observe(10, True)
    assert t.observe(10, True) == "DUPLICATE" and t.observe(9, True) == "DUPLICATE"
    assert t.duplicates == 2 and t.observe(11, True) == "OK"


def test_a_skipped_number_is_a_gap_whether_data_or_keepalive_reveals_it():
    t = PublishTracker()
    t.observe(1, True)
    assert t.observe(3, True) == "GAP"
    t2 = PublishTracker()
    t2.observe(1, True)
    assert t2.observe(5, False) == "GAP", "a keep-alive naming a later number proves messages were sent and missed"


def test_numbering_wraps_from_the_maximum_to_one_skipping_zero():
    t = PublishTracker()
    t.observe(0xFFFFFFFE, True)
    assert t.observe(0xFFFFFFFF, True) == "OK"
    assert t.observe(1, True) == "OK", "after 4294967295 comes 1, not 0"
    assert t.observe(2, True) == "OK"
    assert t.observe(4, True) == "GAP"


def test_overflow_bit_is_read_from_the_data_value_info_bits():
    assert is_overflow(0x00000480) and not is_overflow(0x00000000) and not is_overflow(0x00000080)


# --------------------------------------------------------------------------------- channel


def test_data_and_keepalive_flow_into_the_owner_in_order():
    channel, ingress, owner, commits, rebuilds, _ = build()
    feed(channel, ingress, publish(1, [(1, 500.0), (2, 450.0)]), publish(2), publish(2, [(1, 501.0)]))
    assert channel.stats["data"] == 2 and channel.stats["keepalive"] == 1
    assert [o.value for g in commits for o in g.observations if o.point_id == "ti"] == [500.0, 501.0]
    assert owner.sources["ua"].state == "VALID" and not rebuilds


def test_the_same_point_twice_in_one_notification_keeps_both_versions_in_order():
    channel, ingress, owner, commits, *_ = build()
    feed(channel, ingress, publish(1, [(1, 490.0), (1, 495.0), (2, 450.0), (1, 500.0)]))
    observations = [o for g in commits for o in g.observations]
    assert [(o.point_id, o.value, o.item_seq) for o in observations] == [
        ("ti", 490.0, 0), ("ti", 495.0, 1), ("to", 450.0, 2), ("ti", 500.0, 3),
    ]  # fmt: skip
    assert len({o.batch_seq for o in observations}) == 1
    assert owner.points["ti"].obs.value == 500.0


def test_non_good_values_keep_payload_and_the_full_status_code():
    channel, ingress, owner, commits, *_ = build()
    feed(channel, ingress, publish(1, [(1, 512.5, 0x80350000), (2, 400.0, 0x40920000)]))
    bad, uncertain = [o for g in commits for o in g.observations]
    assert (bad.value, bad.quality, bad.status_code) == (512.5, r.BAD, 0x80350000)
    assert (uncertain.value, uncertain.quality, uncertain.status_code) == (400.0, r.UNCERTAIN, 0x40920000)
    assert owner.points["ti"].good is False and owner.points["to"].good is False


def test_a_duplicate_is_not_applied_twice_but_still_proves_the_server_is_alive():
    channel, ingress, owner, commits, *_ = build()
    feed(channel, ingress, publish(1, [(1, 500.0)]))
    clock_before = owner.sources["ua"].last_comm_mono
    channel.clock[1]()  # unchanged
    channel.on_publish(publish(1, [(1, 500.0)]))
    ingress.drain()
    assert sum(len(g.observations) for g in commits) == 1
    assert channel.stats["duplicate"] == 1 and owner.sources["ua"].last_comm_mono >= clock_before


def test_a_sequence_gap_starts_a_rebuild_and_later_old_notifications_are_dropped():
    channel, ingress, owner, commits, rebuilds, _ = build()
    feed(channel, ingress, publish(1, [(1, 500.0)]), publish(3, [(1, 999.0)]))
    assert rebuilds == ["SEQUENCE_GAP"] and owner.sources["ua"].state == "RESYNC"
    assert 999.0 not in [o.value for g in commits for o in g.observations], "data after a gap is not trusted"
    feed(channel, ingress, publish(4, [(1, 600.0)]))
    assert owner.health["dropped_while_resync"] == 0, "the channel stops handing batches over after the loss"


def test_overflow_is_stored_with_its_data_and_then_treated_as_a_loss():
    channel, ingress, owner, commits, rebuilds, _ = build()
    feed(channel, ingress, publish(1, [(1, 500.0, 0x00000480)]))
    assert [o.value for g in commits for o in g.observations] == [500.0], "the evidence itself is kept"
    assert rebuilds == ["OVERFLOW"] and owner.sources["ua"].state == "RESYNC"


def test_an_exception_inside_the_callback_is_reported_as_loss():
    channel, ingress, owner, commits, rebuilds, _ = build()
    feed(channel, ingress, publish(1, [(99, 1.0)]))  # unknown client handle: the mapping is broken
    assert rebuilds == ["UNKNOWN_HANDLE"] and owner.sources["ua"].state == "RESYNC"
    channel2, ingress2, owner2, _, rebuilds2, _ = build()
    channel2.item_from = staticmethod(lambda *a: 1 / 0)
    feed(channel2, ingress2, publish(1, [(1, 1.0)]))
    assert rebuilds2 == ["CALLBACK_ERROR:ZeroDivisionError"] and owner2.sources["ua"].state == "RESYNC"


def test_a_subscription_status_change_from_the_server_is_a_definite_loss():
    channel, ingress, owner, commits, rebuilds, _ = build()
    status = ua.StatusChangeNotification(Status=ua.StatusCode(ua.StatusCodes.BadTimeout))
    result = ua.PublishResult(
        SubscriptionId=1, NotificationMessage=ua.NotificationMessage(SequenceNumber=1, NotificationData=[status])
    )
    feed(channel, ingress, result)
    assert rebuilds and rebuilds[0].startswith("SUBSCRIPTION_STATUS:") and owner.sources["ua"].state == "RESYNC"


def test_a_full_ingress_loses_a_notification_visibly_and_forces_a_rebuild():
    channel, ingress, owner, commits, rebuilds, _ = build(capacity=1)
    channel.on_publish(publish(1, [(1, 500.0)]))
    channel.on_publish(publish(2, [(1, 501.0)]))  # the entry is full: this one is lost
    ingress.drain()
    assert ingress.dropped == 1 and owner.health["app_loss"] == 1
    assert rebuilds == ["APP_LOSS:INGRESS_FULL"] and owner.sources["ua"].state == "RESYNC"
    assert [s.kind for g in commits for s in g.states].count("APP_LOSS") == 1
