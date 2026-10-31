"""Capstone D: persistence and recovery closed loop (方案第 6 节).

Real PostgreSQL, throw-away databases, injected disk readings, and real child processes killed
at the three dangerous moments.  Reads here are direct SQL: the query layer arrives in module F.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from archive_support import said_and_held, samples
from test_archive_write import make_group
from test_history_merge import Rig

ROOT = Path(__file__).resolve().parents[1]
def all_stores(rig):
    return {"primary": rig.primary, "emergency": rig.emergency}


def history_union(rig, point="ti"):
    """Direct SQL over both databases: (database, time, value) of every stored row of `point`."""
    seen = []
    for name, store in all_stores(rig).items():
        with store.session() as conn:
            seen += [(name, x["t"], x["value"]) for x in samples(conn, "c.point_id=%s", (point,))]
    return seen


def where(rig):
    found = {}
    for name, store in all_stores(rig).items():
        with store.session() as conn:
            for row in conn.execute("SELECT group_id FROM commit_group").fetchall():
                found.setdefault(row["group_id"], []).append(name)
    return found


def rows(store):
    with store.session() as conn:
        return len([x for x in samples(conn) if x["kind"] == "raw"])


def test_two_databases_keep_whole_groups_and_both_histories_stay_visible_across_the_switches(archive_db_factory):
    rig = Rig(archive_db_factory)
    for primary in (0.40, 0.96, 0.96, 0.60, 0.30):  # to emergency at 95 %, back below 90 % after a successful probe
        assert rig.write(primary=primary).durable
    assert where(rig) == {
        "run1/g1": ["primary"], "run1/g2": ["emergency"], "run1/g3": ["emergency"],
        "run1/g4": ["primary"], "run1/g5": ["primary"],
    }  # fmt: skip
    big = make_group(order=6, n_obs=300, config_id=rig.cfg.config_id)  # a group of 300 records
    assert rig.router.write_group(big).durable
    assert rows(rig.primary) == 3 + 3 + 3 + 300 and rows(rig.emergency) == 3 + 3
    merged = history_union(rig)
    assert len(merged) == len(set(merged)), "no observation exists twice, before or after the switch"
    assert {n for n, t, _ in merged if t == 1001.0} == {"primary"}, "pre-switch data (group 1) was neither moved nor copied"
    assert {n for n, t, _ in merged if t == 1002.0} == {"emergency"}
    for name, store in all_stores(rig).items():  # every stored group is complete in its own database
        with store.session() as conn:
            said, held = said_and_held(conn)
        assert said == held, (name, said, held)


def run_worker(mode, run_id, kill_at, dsns, wall=1000):
    env = {**os.environ, "HTSG_CRASH_DSNS": json.dumps(dsns), "HTSG_CRASH_WALL": str(wall)}
    return subprocess.run(
        [sys.executable, str(ROOT / "tests" / "crash_worker.py"), mode, run_id, str(kill_at)],
        env=env, capture_output=True, text=True, timeout=120, cwd=ROOT,
    )  # fmt: skip


@pytest.mark.parametrize("mode", ["before_tx", "in_tx", "after_commit"])
def test_killing_the_process_before_inside_or_after_a_commit_leaves_no_half_group_and_no_bridge(
    archive_db_factory, mode
):
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    dsns = {"primary": primary.dsn, "emergency": emergency.dsn}
    first = run_worker(mode, "run1", 4, dsns)
    assert first.returncode == 9, (first.returncode, first.stderr[-400:])
    with primary.session() as conn:
        groups = [x["group_id"] for x in conn.execute("SELECT group_id FROM commit_group ORDER BY order_no")]
        expected = {"before_tx": 3, "in_tx": 3, "after_commit": 4}[mode]
        assert len(groups) == expected, groups
        said, held = said_and_held(conn)
        assert said == held, "every stored group is whole and nothing of the interrupted transaction survived"
        orphans = conn.execute(
            "SELECT count(*) AS n FROM fault_record m WHERE NOT EXISTS(SELECT 1 FROM commit_group c WHERE c.group_id=m.group_id)"
        ).fetchone()["n"]
        assert orphans == 0, "no fault without its group"
        run = conn.execute("SELECT ended_wall FROM run WHERE run_id='run1'").fetchone()
        last_evidence = conn.execute(  # R03a (revised 2026-10-23): the run's start and its watermarks, nothing else
            "SELECT greatest((SELECT started_wall FROM run WHERE run_id='run1'),"
            "coalesce((SELECT max(covered_wall) FROM watermark),0)) AS t"
        ).fetchone()["t"]
        segment_evidence = conn.execute(  # the segment's OWN evidence - its watermark, its start when it has none
            "SELECT greatest((SELECT start_wall FROM segment WHERE run_id='run1' AND seg=1),"
            "coalesce((SELECT max(covered_wall) FROM watermark WHERE seg=1),0)) AS t"
        ).fetchone()["t"]
    assert run["ended_wall"] is None, "the dead process registered no end"
    # restart: a NEW run in the same databases; the unfinished one is closed at persisted evidence only
    second = run_worker("none", "run2", 0, dsns, wall=2000)
    assert second.returncode == 0, second.stderr[-400:]
    with primary.session() as conn:
        old = conn.execute("SELECT ended_wall,end_reason FROM run WHERE run_id='run1'").fetchone()
        segment = conn.execute("SELECT end_wall,end_reason FROM segment WHERE run_id='run1' ORDER BY seg DESC").fetchone()
        new_start = conn.execute("SELECT min(start_wall) AS t FROM segment WHERE run_id='run2'").fetchone()["t"]
        dup = conn.execute(
            "SELECT count(*) AS d FROM (SELECT 1 FROM sample GROUP BY run,channel,t,item,value HAVING count(*) > 1) x"
        ).fetchone()["d"]
    assert old["end_reason"] == "ABNORMAL_EXIT" and old["ended_wall"] >= last_evidence - 1e-9
    assert old["ended_wall"] <= last_evidence + 5, "no crash time is invented beyond the stored evidence"
    assert segment["end_reason"] == "EXIT_TRUNCATED" and segment["end_wall"] == segment_evidence
    assert old["ended_wall"] < new_start, "the old interval cannot reach into the new run"
    assert dup == 0
