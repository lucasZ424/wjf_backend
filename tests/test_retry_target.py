"""A retry goes to the database the group's first write went to (修复轮09 审核第4条; 终版 §13.5).

Why it matters: the target and the bucket are chosen before a group's first write, and an ordinary retry keeps both.  A
retry that is routed again follows the disks of the moment - a primary that filled up in between, an emergency route that
ended in between - and the same group's history is split over two databases for nothing.  In the primary route a failed
write to the primary waits and is tried there again, it is not sent to the emergency database: the emergency route is
for a full disk, not for a database that answers late or has not got the bucket yet.  The one exception, a certain
failure of the first write to the primary just after the emergency route ended, is left to test_storage_routing.  The
emergency database is chosen up front only when the primary has no bucket a group may be put in at all.
"""

from types import MappingProxyType

from test_archive_write import make_group
from test_bucket_switch import landed, pair  # noqa: F401

from htsg.archive import WriteOutcome
from htsg.buckets import Snapshot


def fail_once(store):
    real = store.write_group

    def write(group):
        store.write_group = real
        return WriteOutcome("FAILED", "OperationalError")  # certain: rolled back, nothing is there

    store.write_group = write


def test_a_failed_write_to_the_primary_is_retried_there_even_when_the_disks_now_point_elsewhere(pair):  # noqa: F811
    router, cfg = pair
    group = make_group(config_id=cfg.config_id)
    fail_once(router.primary)
    first = router.write_group(group)
    assert (first.status, first.target) == ("FAILED", "primary") and router.pinned == {group.group_id: "primary"}
    router.disk_usage.used["D:/a"] = 0.96  # a new group would now go to the emergency database
    again = router.write_group(group)
    assert (again.status, again.target) == ("COMMITTED", "primary") and router.pinned == {}
    assert landed(router.primary) == [(group.group_id, 1)] and landed(router.emergency) == []
    later = router.write_group(make_group(order=2, config_id=cfg.config_id))
    assert later.target == "emergency", "other groups follow the disks as before"


def test_a_failed_write_to_the_emergency_database_is_retried_there_after_the_route_could_end(pair):  # noqa: F811
    router, cfg = pair
    router.disk_usage.used["D:/a"] = 0.96
    assert router.write_group(make_group(order=1, config_id=cfg.config_id)).target == "emergency"
    group = make_group(order=2, config_id=cfg.config_id)
    fail_once(router.emergency)
    assert router.write_group(group).target == "emergency"
    router.disk_usage.used["D:/a"] = 0.80  # below the start line, the primary has bucket 1: the route may end now
    again = router.write_group(group)
    assert (again.status, again.target) == ("COMMITTED", "emergency")
    assert landed(router.primary) == [], "the retry did not follow the route back"


def test_in_the_primary_route_a_bucket_the_primary_lacks_waits_there_and_is_never_sent_to_emergency(pair):  # noqa: F811
    router, cfg = pair
    router.emergency.prepare_bucket(2)
    group = make_group(config_id=cfg.config_id, bucket=2)
    first = router.write_group(group)
    assert (first.status, first.target, first.reason) == ("FAILED", "primary", "NO_BUCKET")
    assert landed(router.emergency) == [] and router.mode == "PRIMARY"
    router.primary.prepare_bucket(2)  # what the maintenance thread does for a bucket a database lacks (plan D2)
    again = router.write_group(group)
    assert (again.status, again.target) == ("COMMITTED", "primary") and landed(router.primary) == [(group.group_id, 2)]


def test_the_emergency_database_is_chosen_up_front_only_when_the_primary_has_no_bucket_at_all(pair):  # noqa: F811
    router, cfg = pair
    router.emergency.prepare_bucket(2)
    snapshot = Snapshot(MappingProxyType({"primary": frozenset(), "emergency": frozenset({1, 2})}),
                        MappingProxyType({"primary": "OK", "emergency": "OK"}))  # fmt: skip
    router.buckets = lambda: snapshot
    first = router.write_group(make_group(order=1, config_id=cfg.config_id, bucket=2))
    assert (first.status, first.target) == ("COMMITTED", "emergency") and router.mode == "PRIMARY"
    unread = Snapshot(MappingProxyType({"primary": frozenset(), "emergency": frozenset({1, 2})}),
                      MappingProxyType({"primary": "UNAVAILABLE", "emergency": "OK"}))  # fmt: skip
    router.buckets = lambda: unread
    second = router.write_group(make_group(order=2, config_id=cfg.config_id, bucket=1))
    assert second.target == "primary", "a primary that could not be read is not known to lack a bucket: it is waited for"
