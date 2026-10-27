"""D46: the full-scale measurement harness measures what it claims to (方案模块 H).

The harness (scripts/measure.py) is the instrument for the performance report, so these tests run it for a short
while against the real thing - simulator, runtime, PostgreSQL, a real Chrome - and check what the report must be
able to say: no unexplained loss, no backlog left behind, outages noticed and recovered from, and a latency that
is split into its parts rather than one number.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure  # noqa: E402


def test_the_spread_reports_the_quantiles_and_the_maximum_of_what_it_was_given():
    values = list(range(1, 101))
    spread = measure.spread(values)
    assert spread["count"] == 100 and spread["min"] == 1 and spread["max"] == 100
    assert spread["p50"] == 50.5 and spread["p95"] == 96 and spread["p99"] == 100
    assert measure.spread([]) == {"count": 0}


async def test_a_steady_full_scale_run_leaves_nothing_unexplained_and_no_backlog():
    async with measure.Lab(stats_interval=2) as lab:
        burst = await measure.initial_burst(lab)
        steady = await measure.steady(lab, 8)
        assert len(lab.views()) == 417 and lab.everything_valid()
    for report in (burst, steady):
        assert report["unexplained"] == {"ingress_dropped": 0, "queue_rejected": 0, "refused": 0, "failures": 0}, report
    assert burst["archive_caught_up_after_seconds"] is not None and burst["archive_caught_up_after_seconds"] < 10
    assert steady["backlog_left_after_seconds"] is not None and steady["backlog_left_after_seconds"] < 5
    assert steady["groups_per_second"] > 0.5 and steady["rows_per_second"] > 50, "the simulated plant changes every input every second"
    assert steady["committed_groups"] >= steady["published_groups"] - 2, "what was published was written, not just shown"
    assert lab.runtime.health["shutdown_incomplete"] is False
    assert lab.runtime.health["maintenance_max_seconds"] < 2 and steady["maintenance_max_seconds"] == round(lab.runtime.health["maintenance_max_seconds"], 3)


async def test_an_outage_of_either_protocol_is_noticed_and_the_source_comes_back_whole():
    async with measure.Lab(stats_interval=2, outage=4) as lab:
        await measure.initial_burst(lab)
        reports = [await measure.outage(lab, "disconnect_opcua", "ua_field", 4), await measure.outage(lab, "disconnect_modbus", "mb_field", 4)]
    for report in reports:
        assert report["outage_seconds"] >= 4 and report["noticed_after_seconds"] < 12, report
        assert report["valid_again_after_seconds"] < 60 and report["archive_backlog_cleared_after_seconds"] is not None, report
        assert set(report["archive_states"].values()) == {"OK"}, "the archive was not left with an open gap by the protocol outage"
        assert report["unexplained"]["ingress_dropped"] == 0 and report["unexplained"]["queue_rejected"] == 0
    assert reports[0]["resyncs"] >= 1, "an OPC UA session that is lost starts over from a new generation"


async def test_the_display_delays_are_reported_in_three_parts_and_only_new_samples_count():
    """修复轮05 (方案 D46④): what the page reads, measured from the page itself - the readings it asked for, a reading
    taken to drawn, and a NEW observation received to drawn.  The first reading is only the baseline and an
    observation no reading showed is not counted, so the delay is the system's, never the age of a steady value."""
    async with measure.Lab(stats_interval=2, browser=True) as lab:
        await measure.initial_burst(lab)
        await measure.steady(lab, 8)
        display = await measure.display_latency(lab)
    readings, taken, new = display["readings"], display["taken_to_drawn_ms"], display["receipt_to_drawn_ms"]
    assert 5 <= readings["answered"] <= readings["asked"], readings
    assert readings["refused_by_status"] == {} and readings["network_failed"] == 0 and readings["given_up_at_limit"] == 0, readings
    assert taken["samples"] >= 5 and taken["min"] >= -5, "drawn after it was taken, on one clock"
    assert new["samples"] > 0 and new["baseline_observations"] > 0, new
    assert new["p50"] >= taken["p50"], "an observation is received before the reading that shows it is taken"
    assert new["max"] < 3000, "at most a reading interval and a request after its receipt, under full load"
    assert display["excluded_wall_clock_steps"] == 0
