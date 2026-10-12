"""D48: the delivered start path works on a new database, stops normally and restarts on the same one (方案 D48).

`scripts/smoke.py` starts the backend through `scripts/run.ps1` - the way an operator starts it - against the
independent simulated field on throw-away databases.  Why it matters: everything else is tested through code paths a
test builds; this is the one check that the thing that is handed over starts, serves its own files and configuration,
ends a run properly and continues its history after a restart.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import smoke  # noqa: E402

START, RESTART = "first start on a new database", "restart on the same database"


async def test_the_start_path_serves_this_tree_stops_as_normal_and_restarts_without_a_duplicate():
    report = await smoke.smoke(5)
    assert set(report["candidate"]) >= {"commit", "clean", "code_sha256", "schema_sha256", "pages_sha256", "configs_sha256", "schema_generation"}
    for label in (START, RESTART):
        part = report[label]
        assert part["page_served_is_this_tree"] and part["config_id_is_the_one_on_disk"], "what runs is what is on disk"
        assert part["archive_sources"] == {"mb_field": "OK", "ua_field": "OK"} and part["queue_failures"] == 0
        assert part["accounts"] == "DATABASE_AUTHORITATIVE" and part["negotiated_ua"]["violations"] == []
        assert part["stop"] == "STOPPED", f"a normal stop ends the process: {part['stop']}"
    runs = [(ended, reason) for _, ended, reason in report[RESTART]["runs_in_database"]]
    assert runs == [(True, "NORMAL"), (True, "NORMAL")], "each start is one run and each normal stop ends it as NORMAL"
    first, second = report[START]["history"], report[RESTART]["history"]
    assert second["raw_rows"] > first["raw_rows"] >= 3, "the second window still holds the first run's rounds and adds its own"
    assert second["duplicates"] == 0 and second["in_order"] and second["partial"] is False
