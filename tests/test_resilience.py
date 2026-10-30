"""D47: crash recovery, storage switching and the capacity statement (方案模块 H; 终版 §13.5 容量报告).

The checks live in scripts/verify_resilience.py; these tests run them short against the real system - a backend
process that is killed hard, injected disk readings through the live runtime, measured database growth - and
insist on the conclusions that matter: never a half-written group, never a route chosen against the thresholds,
never a floor of buckets called held that the space below the stop line cannot hold.  With the rolling deletion there is
no one-year retention to pass any more: the capacity is how often a deletion comes and how long is kept after it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_resilience as resilience  # noqa: E402

GIB = 1024**3


def test_the_capacity_is_a_deletion_period_and_a_retention_and_the_floor_is_checked_against_the_stop_line():
    ten_gib_a_day = 10 * GIB / 86400
    lines = {"cleanup_start": 0.9, "cleanup_stop": 0.8, "floor_buckets": 14, "reserve_bytes": 100 * GIB}
    fits = resilience.retention_capacity(ten_gib_a_day, wal_peak=2 * GIB, maintenance_peak=GIB, primary_total=1000 * GIB,
                                         emergency_free=200 * GIB, **lines)  # fmt: skip
    assert abs(fits["days_between_deletions"] - 10.0) < 1e-9, "100 GiB between the lines at 10 GiB a day"
    assert abs(fits["days_kept_after_deletion"] - 79.7) < 1e-9, "800 GiB below the stop line, the log and maintenance peaks once"
    assert abs(fits["emergency_days"] - 10.0) < 1e-9, "the emergency database is not thinned: its room above the reserve"
    assert fits["verdict"] == "FLOOR_HELD" and "bytes_per_year" not in fits, "no one-year retention pass any more (§13.5)"
    tight = resilience.retention_capacity(ten_gib_a_day, wal_peak=2 * GIB, maintenance_peak=GIB, primary_total=150 * GIB,
                                          emergency_free=50 * GIB, **lines)  # fmt: skip
    assert tight["days_kept_after_deletion"] < 14 and tight["verdict"].startswith("FLOOR_NOT_HELD")
    assert tight["emergency_days"] == 0, "below the reserve it takes nothing"


async def test_a_hard_kill_at_any_moment_leaves_no_half_written_group_and_the_next_start_closes_the_run():
    report = await resilience.crash(2)
    assert [k["mode"] for k in report["kills"]] == ["during start-up", "under load"]
    for kill in report["kills"]:
        for check in (kill["primary"], kill["emergency"]):
            assert check["half_written_groups"] == 0 and check["orphan_rows"] == 0
    assert report["kills"][1]["primary"]["groups"] > 0, "the second process really had written before it was killed"
    after = report["after"]["primary"]
    assert after["half_written_groups"] == 0 and after["open_segments_in_ended_runs"] == 0
    closed = [reason for ended, reason in after["runs"] if ended]
    assert closed.count("ABNORMAL_EXIT") >= 1, "a killed run is closed from what is persisted, and says so"
    history = report["history_after_crashes"]
    assert history["partial"] is False and history["rows"]["bridge_mvv_01"] >= 2
    assert isinstance(report["graceful_stop"], int), f"the Ctrl+Break stop could be tried: {report['graceful_stop']}"
    assert after["runs"][-1] == (True, "NORMAL"), "a normal stop ends the run as normal (uvicorn re-raises the signal: the exit code is not 0)"


async def test_the_injected_thresholds_choose_the_route_and_history_stays_whole_across_the_switches():
    report = await resilience.storage(3)
    part = report["switching"]
    for step in part["steps"]:
        assert step["route"] == step["expected"], step
        assert set(step["archive"].values()) == {"OK"}, f"the switch did not cost the archive anything: {step}"
        landed = {name: n for name, n in step["groups_landed"].items() if n}
        target = step["expected"].lower()
        assert set(landed) == {target}, f"every new group went to the one database the route names: {step}"
    assert all(w == {"half_written_groups": 0, "orphan_rows": 0} for w in part["whole_groups"].values())
    assert part["group_ids_unique"], "no group is stored in two databases"
    assert part["history"]["partial"] is False and part["history"]["duplicates"] == 0 and part["history"]["in_order"]
    assert part["history"]["raw_rows"] >= 6, "the merged history carries the rounds from every database"
    assert part["owner_archive_lost_groups"] == 0
    assert report["real_disk_growth_bytes"] < GIB, "the thresholds were injected: no real disk was filled"


async def test_the_measured_growth_gives_a_year_figure_with_indexes_inside_and_a_verdict_that_follows_the_margin():
    report = await resilience.capacity(8)
    busy, quiet = report["every_input_changes_every_second"], report["no_input_changes"]
    assert busy["database_bytes_per_second"] > 5 * quiet["database_bytes_per_second"] > 0
    for part in (busy, quiet):
        # the first start prepares its buckets once; inside the window that one-off cost would be taken for a year's growth
        assert part["valid"] and part["buckets_at_start"] == part["buckets_at_end"], "no bucket was prepared inside the window"
        assert part["first_preparation"]["database_bytes_at_window_start"] > 0, "the one-off start is reported on its own"
        assert 0 < part["tables_total_bytes"] <= part["database_size_bytes"], "the tables with their indexes are inside the database size: counted once"
        assert all(t["indexes"] > 0 for name, t in part["tables"].items() if name in ("observation", "result")), "the indexes are part of the figure"
        cap = part["capacity"]
        assert (cap["days_kept_after_deletion"] >= cap["floor_buckets"]) == (cap["verdict"] == "FLOOR_HELD")
        assert cap["days_between_deletions"] > 0 and "bytes_per_year" not in cap
    assert "simulated field only" in report["not_proven"]
