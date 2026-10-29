"""D19: a commit group is written whole or not at all (终版 §5 C3, §11 database limits).

Real PostgreSQL.  Why it matters: a half-written group would leave an input without the result computed
from it, or a watermark without the data it claims to cover; a lost COMMIT reply must never lead to a
duplicate or to a blind rewrite elsewhere.
"""

import time
from dataclasses import replace

import psycopg
import pytest
from owner_support import small_config

from htsg import records as r
from htsg.archive import WriteOutcome

CONFIG = small_config()
RUN = "run1"


def make_group(order=1, n_obs=3, config_id=None, bucket=1, **over):
    config_id = config_id or CONFIG.config_id
    obs = tuple(
        r.Observation(
            id=r.observation_id(RUN, "ua", order, i), run_id=RUN, source_id="ua", conn_gen=1, seg=1,
            batch_seq=order, item_seq=i, point_id="ti", value=480.0 + i, quality=r.GOOD, status_code=0,
            received_wall=1000.0 + order, received_mono=100.0 + order,
        )
        for i in range(n_obs)
    )  # fmt: skip
    results = (
        r.Result(
            id=r.result_id(RUN, "ua", order, "dt"), run_id=RUN, source_id="ua", conn_gen=1, seg=1,
            batch_seq=order, point_id="dt", value=50.0, status=r.RESULT_OK, reason=None,
            config_id=config_id, at_wall=1000.0 + order, basis={"tick": order},
        ),
    )  # fmt: skip
    states = (
        r.StateRecord(r.state_id(RUN, "ua", order * 10), RUN, "ua", 1, "SEG_START", 1000.0, 100.0,
                      {"conn_gen": 1, "reason": "CONNECT"}),
        r.StateRecord(r.state_id(RUN, "ua", order * 10 + 1), RUN, "ua", 1, "WATERMARK", 1001.0, 101.0,
                      {"seg": 1, "covered_wall": 1000.5}),
    )  # fmt: skip
    faults = (
        r.FaultRecord(r.fault_id(RUN, "ti", order), RUN, "ua", 1, order, "ti", "HIGH", 1005.0, 105.0,
                      "OBSERVATION", obs[0].id, 1000.0, 495.0, config_id, {"high": 490.0}),
    )  # fmt: skip
    group = r.CommitGroup(
        r.group_id(RUN, order), order, RUN, "ua", r.GROUP_BATCH, 1001.0, obs, results, states, faults
    )
    return replace(group, bucket=bucket, **over)


@pytest.fixture
def store(archive_db_factory):
    s = archive_db_factory()
    s.initialize(CONFIG)
    s.bind_run(RUN, CONFIG.config_id, 999.0)
    return s


def counts(store):
    with store.session() as conn:
        return {
            t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
            for t in ("commit_group", "observation", "result", "state_event", "fault_record", "segment", "watermark", "run")
        }  # fmt: skip


def test_observations_results_states_and_faults_commit_in_one_transaction(store):
    assert store.write_group(make_group()) == WriteOutcome("COMMITTED")
    assert counts(store) == {
        "commit_group": 1, "observation": 3, "result": 1, "state_event": 2, "fault_record": 1,
        "segment": 1, "watermark": 1, "run": 1,
    }  # fmt: skip
    with store.session() as conn:
        tables = ("observation", "result", "state_event", "fault_record")
        for t in tables:
            assert conn.execute(f"SELECT DISTINCT group_id FROM {t}").fetchall() == [{"group_id": "run1/g1"}]
        seg = conn.execute("SELECT conn_gen,start_wall,start_reason FROM segment").fetchone()
        wm = conn.execute("SELECT covered_wall,group_order FROM watermark").fetchone()
    assert seg == {"conn_gen": 1, "start_wall": 1000.0, "start_reason": "CONNECT"}
    assert wm == {"covered_wall": 1000.5, "group_order": 1}


def test_a_failure_in_the_middle_leaves_no_half_group(store):
    bad = make_group()
    broken = replace(bad.results[0], config_id="no-such-config")  # violates the configuration reference
    outcome = store.write_group(replace(bad, results=(broken,)))
    assert outcome.status == "FAILED" and outcome.error == "ForeignKeyViolation"
    assert set(counts(store).values()) == {0}, "not one observation, state, fault or group row survives"
    assert store.write_group(make_group()).status == "COMMITTED", "the corrected group can still be written"


def test_a_repeated_group_is_idempotent_and_a_resubmitted_fault_keeps_its_id(store):
    group = make_group()
    assert store.write_group(group).status == "COMMITTED"
    before = counts(store)
    assert store.write_group(group) == WriteOutcome("ALREADY")
    assert counts(store) == before
    # a recovery group re-delivers the same confirmed fault inside a different group: no second fault row
    recovery = replace(make_group(order=2, n_obs=1), faults=group.faults)
    assert store.write_group(recovery).status == "COMMITTED"
    with store.session() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fault_record").fetchone()["n"] == 1


def test_an_unknown_commit_is_reported_as_unknown_and_can_be_verified_by_id(store):
    original = store._write_members

    def kill_own_backend_before_commit(conn, group):
        original(conn, group)
        with psycopg.connect(store.dsn, autocommit=True) as other:
            other.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))

    store._write_members = kill_own_backend_before_commit
    outcome = store.write_group(make_group())
    assert outcome.status == "UNKNOWN" and outcome.error, "the failure arrived while COMMIT was in flight"
    store._write_members = original
    assert store.group_committed("run1/g1", 1) is False, "verified by stable id: it did not become durable"
    assert store.write_group(make_group()).status == "COMMITTED", "same target, same ids: safe to retry"
    assert store.group_committed("run1/g1", 1) is True


def test_connection_and_session_limits_are_3_3_1_seconds_and_survive_reconnection(store):
    def limits():
        with store.session() as conn:
            show = {k: conn.execute(f"SHOW {k}").fetchone()[k] for k in ("statement_timeout", "lock_timeout")}
            return show, conn.info.get_parameters().get("connect_timeout"), conn.info.backend_pid

    first, connect_timeout, pid = limits()
    assert first == {"statement_timeout": "3s", "lock_timeout": "1s"} and connect_timeout == "3"
    with psycopg.connect(store.dsn, autocommit=True) as other:  # kill the pooled session
        other.execute("SELECT pg_terminate_backend(%s)", (pid,))
    with pytest.raises(psycopg.Error), store.session() as conn:
        conn.execute("SELECT 1")
    again, connect_timeout, new_pid = limits()
    assert new_pid != pid and again == first and connect_timeout == "3", "a fresh session carries the same limits"


def test_a_blocking_statement_is_cancelled_at_three_seconds(store):
    started = time.monotonic()
    with pytest.raises(psycopg.errors.QueryCanceled), store.session() as conn:
        conn.execute("SELECT pg_sleep(8)")
    assert 2.8 < time.monotonic() - started < 4.5


def test_lock_contention_gives_up_after_one_second_and_leaves_nothing(store):
    with psycopg.connect(store.dsn) as holder:
        holder.execute("LOCK TABLE commit_group IN ACCESS EXCLUSIVE MODE")  # held until the block ends
        started = time.monotonic()
        outcome = store.write_group(make_group())
        elapsed = time.monotonic() - started
    assert outcome.status == "FAILED" and outcome.error == "LockNotAvailable"
    assert 0.8 < elapsed < 2.5, elapsed
    assert set(counts(store).values()) == {0}
