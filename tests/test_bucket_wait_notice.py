"""A group waiting in the normal database for its bucket is said, and unsaid once it is settled (修复轮10; 终版 §13.5).

Why it matters: the route keeps such a group at the head of the queue and the groups behind it pile up until the queue
refuses them - an accepted limit, but one that must be seen on the self-check page while it lasts, with when it began, and
must not stay there once the group is written.
"""

from archive_support import samples
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


class Bucket:
    def __init__(self, no):
        self.no = no

    def label(self):
        return self.no

    def tick(self, wall, mono):
        pass


async def test_a_long_wait_for_a_bucket_keeps_acquisition_live_bounded_and_honest_and_writing_goes_on_once_it_is_there(rig):  # noqa: F811
    """The accepted limit end to end (修复轮10): the normal database lacks the bucket the groups are given.  The head group
    waits, the queue stays bounded and refuses, the loss becomes a gap, the live view goes on, nothing is sent elsewhere;
    once the bucket is there the writing goes on by itself, the gap is closed and new readings are stored again.  The bucket
    is prepared here by hand - the fault removed.  That the maintenance thread itself makes up the buckets a database lacks is
    tested apart (test_bucket_maintenance: a database that was away gets them once it is back), not here."""
    import asyncio

    from owner_support import poll

    from htsg.commitq import CommitQueue
    from htsg.owner import Owner

    # wired the way the runtime wires them: every group the owner makes goes through the queue, the first one too
    queue = CommitQueue(rig.router, 5, on_committed=lambda g, r: owner.committed(g, r), on_refused=lambda g, why: owner.refused(g, why),
                        is_void=lambda g: owner.is_void(g), retry_delay=(0.05, 0.2))  # fmt: skip
    owner = Owner(rig.cfg, 1000.0, 100.0, commit=queue.offer, buckets=Bucket(2), run_id="run")  # the normal database has bucket 1
    rig.router.bind_run("run", rig.cfg.config_id, 1000.0)
    owner.begin_generation("mb", 1, 1000.0, 100.0)
    task = asyncio.create_task(queue.run())
    clock = {"tick": 0}

    async def rounds_until(condition, why, seconds=30.0, b1=None):
        """One Modbus round and one owner tick every 20 ms, as the runtime drives them, until `condition` or the deadline."""
        deadline = asyncio.get_running_loop().time() + seconds
        while not condition():
            assert asyncio.get_running_loop().time() < deadline, why
            clock["tick"] += 1
            n = clock["tick"]
            owner.accept(poll(n, {"b1": 0.5 + n * 0.001 if b1 is None else b1, "b2": 0.6}))
            owner.tick(1000.0 + n, 100.0 + n)  # the runtime ticks the owner every second
            assert queue.depth <= 5, "bounded: the groups behind the waiting one do not pile up without end"
            await asyncio.sleep(0.02)

    try:
        await rounds_until(lambda: "waiting_for_bucket" in rig.router.health and queue.stats["rejected"] > 0 and clock["tick"] >= 10,
                           "the head group never waited for its bucket, or the queue never turned one away")  # fmt: skip
        assert owner.channel_views()["b1"]["value"] == 0.5 + clock["tick"] * 0.001, "acquisition and the live view go on"
        assert owner.sources["mb"].archive == "LOST", "what the queue turned away is a gap, said so"
        assert rig.router.health["waiting_for_bucket"]["bucket"] == 2
        assert all(target == "primary" for target in rig.router.pinned.values()), "nothing is sent to another database"
        rig.primary.prepare_bucket(2)  # the fault removed
        await rounds_until(lambda: owner.sources["mb"].archive == "OK" and not queue.depth, "the writing did not go on by itself")
        assert "waiting_for_bucket" not in rig.router.health
        before = queue.stats["committed"]
        await rounds_until(lambda: queue.stats["committed"] > before and not queue.depth, "a new reading was not stored", b1=9.75)
        with rig.primary.session() as conn:
            found = samples(conn, "c.point_id='b1' AND s.value=9.75", order="s.t DESC")
            stored = found[0] if found else None
            gaps = conn.execute("SELECT end_wall FROM archive_gap WHERE source_id='mb'").fetchall()
        assert stored is not None and stored["bucket"] == 2, ("stored again, in its bucket", stored)
        assert gaps and all(g["end_wall"] is not None for g in gaps), "the loss is in the archive as a gap, and the gap is closed"
        with rig.emergency.session() as conn:
            assert conn.execute("SELECT count(*) AS n FROM commit_group").fetchone()["n"] == 0, "no group went to the emergency database"
    finally:
        await queue.stop(5.0)  # settle what is queued, then end its loop
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(queue.io.shutdown, True)  # a write still under way ends before the databases are dropped
