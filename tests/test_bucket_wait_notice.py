"""A group waiting in the normal database for its bucket is said, and unsaid once it is settled (修复轮10; 终版 §13.5).

Why it matters: the route keeps such a group at the head of the queue and the groups behind it pile up until the queue
refuses them - an accepted limit, but one that must be seen on the self-check page while it lasts, with when it began, and
must not stay there once the group is written.
"""

from test_archive_router import rig  # noqa: F401 - the fixture
from test_archive_write import make_group


def test_the_wait_for_a_missing_bucket_is_noted_once_and_cleared_when_its_group_is_written(rig):  # noqa: F811
    group = make_group(order=1, config_id=rig.cfg.config_id, bucket=2)  # the normal database has bucket 1 only
    first = rig.router.write_group(group)
    assert (first.status, first.target, first.reason) == ("FAILED", "primary", "NO_BUCKET")
    noted = rig.router.health["waiting_for_bucket"]
    assert (noted["bucket"], noted["group"]) == (2, group.group_id) and noted["since"] > 0
    assert rig.router.write_group(group).status == "FAILED"
    assert rig.router.health["waiting_for_bucket"]["since"] == noted["since"], "the same wait: since when it began"
    rig.primary.prepare_bucket(2)  # the maintenance thread makes up what the database lacks
    assert rig.router.write_group(group).durable
    assert "waiting_for_bucket" not in rig.router.health


def test_a_waiting_group_the_queue_gives_up_is_no_longer_said_to_wait(rig):  # noqa: F811
    group = make_group(order=1, config_id=rig.cfg.config_id, bucket=2)
    rig.router.write_group(group)
    rig.router.release(group.group_id)
    assert "waiting_for_bucket" not in rig.router.health
