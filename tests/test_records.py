"""D02: record identity contract (终版 §5, §10).

Why it matters: history is reconstructed from these identities.  If two values received
at the same instant collided, or a retried commit minted new ids, the archive would lose
or duplicate evidence.
"""

import dataclasses

from htsg import records as r


def obs(**changes):
    base = dict(
        id=r.observation_id("run1", "ua", 7, 0),
        run_id="run1",
        source_id="ua",
        conn_gen=2,
        seg=3,
        batch_seq=7,
        item_seq=0,
        point_id="p",
        value=1.5,
        quality=r.GOOD,
        status_code=0,
        received_wall=1_700_000_000.25,
        received_mono=50.0,
    )
    return r.Observation(**{**base, **changes})


def test_identity_carries_run_source_generation_segment_batch_and_item():
    fields = {f.name for f in dataclasses.fields(r.Observation)}
    assert {"run_id", "source_id", "conn_gen", "seg", "batch_seq", "item_seq"} <= fields
    assert {"run_id", "source_id", "conn_gen", "seg", "batch_seq"} <= {
        f.name for f in dataclasses.fields(r.Result)
    }


def test_same_point_same_instant_with_different_item_numbers_get_distinct_ids():
    # An OPC UA notification may carry two versions of one point in the same batch; both
    # share the batch receive time yet must stay separately storable and orderable.
    first = r.observation_id("run1", "ua", 7, 0)
    second = r.observation_id("run1", "ua", 7, 1)
    a = obs(id=first, item_seq=0, value=1.0)
    b = obs(id=second, item_seq=1, value=2.0)
    assert (a.point_id, a.received_wall) == (b.point_id, b.received_wall)
    assert a.id != b.id and a.item_seq < b.item_seq


def test_ids_are_deterministic_so_a_retried_group_is_idempotent_but_runs_differ():
    assert r.observation_id("run1", "ua", 7, 3) == r.observation_id("run1", "ua", 7, 3)
    assert r.observation_id("run1", "ua", 7, 3) != r.observation_id("run2", "ua", 7, 3)
    assert r.observation_id("run1", "ua", 7, 3) != r.observation_id("run1", "mb", 7, 3)
    ids = {
        r.observation_id("run1", "ua", 7, 0),
        r.assertion_id("run1", "ua", 4, "p"),
        r.result_id("run1", "ua", 7, "p"),
        r.state_id("run1", "ua", 7),
        r.fault_id("run1", "p", 1),
        r.group_id("run1", 7),
    }
    assert len(ids) == 6, "different record kinds must never share an id"


def test_assertion_keeps_original_observation_time_and_adds_assertion_time():
    original = obs()
    assertion = dataclasses.replace(
        original,
        id=r.assertion_id("run1", "ua", 4, "p"),
        seg=4,
        origin=r.ORIGIN_ASSERTION,
        orig_id=original.id,
        asserted_wall=1_700_000_100.0,
        asserted_mono=150.0,
    )
    assert assertion.received_wall == original.received_wall
    assert assertion.asserted_wall > assertion.received_wall
    assert assertion.orig_id == original.id
    assert original.origin == r.ORIGIN_RECEIVED and original.asserted_wall is None


def test_wall_and_monotonic_clocks_are_separate_fields():
    o = obs(received_wall=1_700_000_000.0, received_mono=12.5)
    # the monotonic value is not an epoch time and must not be derived from the wall value
    assert o.received_mono != o.received_wall


def test_unsigned_32_bit_status_code_is_representable():
    assert obs(status_code=0x80350000).status_code == 0x80350000
    assert obs(status_code=0xFFFFFFFF).status_code > 2**31


def test_fault_record_separates_confirmation_time_from_basis_observation_time():
    f = r.FaultRecord(
        id=r.fault_id("run1", "p", 1),
        run_id="run1",
        source_id="ua",
        seg=3,
        batch_seq=7,
        point_id="p",
        direction="HIGH",
        confirmed_wall=1005.0,
        confirmed_mono=55.0,
        basis_kind="OBSERVATION",
        basis_id="run1/ua/b7/i0",
        basis_wall=1000.0,
        basis_value=500.0,
        config_id="c",
    )
    assert f.confirmed_wall - f.basis_wall == 5.0
    assert f.basis_wall != f.confirmed_wall


def test_commit_group_counts_every_member_kind():
    o = obs()
    group = r.CommitGroup(
        group_id=r.group_id("run1", 1),
        order=1,
        run_id="run1",
        source_id="ua",
        kind=r.GROUP_BATCH,
        built_wall=1.0,
        observations=(o, o),
        states=(r.StateRecord("s", "run1", "ua", 3, "POLL", 1.0, 1.0),),
    )
    assert group.record_count() == 3
    assert group.as_dict()["observations"][0]["point_id"] == "p"
