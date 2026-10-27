"""Review item B (修复轮03, user rulings 2026-10-26/27): a replay asks for at most 12 channels at once.  A fault brings its fixed
template: the fault, its computation chain and the 8 operating quantities - taken from the configuration's own relations only,
since the site has no topology - and the default window.

Why it matters (the hard rule, a year without a crash): the per-channel limit alone let one replay of all 417 channels pull
millions of rows into the process that also acquires and archives, by an everyday action on the history page.  12 channels
bound a replay by construction, whatever is selected or sent.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from live_support import FULL
from owner_support import small_config
from test_archive_router import Disks

from htsg import history
from htsg.archive_router import ArchiveRouter
from htsg.auth import Accounts
from htsg.history_api import build_history_router
from htsg.owner import Owner
from htsg.web import create_web_app

LIMITED = [x.id for x in [*FULL.points, *FULL.derived] if x.high is not None or x.low is not None]
OPERATING = ["load_mw", "waterwall_flow", "main_steam_flow", "main_steam_pressure", "feedwater_temperature",
             "reheat_temperature", "working_fluid_temperature", "furnace_pressure"]  # fmt: skip


def test_the_template_of_every_fault_is_its_computation_chain_and_the_eight_operating_quantities():
    """Checked for all 304 channels that can raise a fault: the counts the user ruled on (12 / 11 / 8, never more than 12)."""
    sizes = {}
    for channel in LIMITED:
        template = history.replay_template(FULL, channel)
        assert template[0] == channel, "the fault itself comes first: it is the chart shown"
        assert len(template) == len(set(template)) <= history.CHANNEL_LIMIT == 12
        assert template[-len(OPERATING):] == OPERATING or channel in OPERATING
        assert "thermal_stress_peak" not in template, "a quantity of 96 inputs is not followed"
        kind = channel.rsplit("_", 1)[0] if channel[-1].isdigit() else "process"
        sizes.setdefault(kind, set()).add(len(template))
    assert sizes == {"temp_inner": {12}, "temp_outer": {12}, "delta_t": {12}, "strain": {11}, "process": {8}}
    assert history.replay_template(FULL, "temp_inner_09")[:4] == ["temp_inner_09", "temp_outer_09", "delta_t_09", "stress_09"]
    assert history.replay_template(FULL, "delta_t_09")[:4] == ["delta_t_09", "temp_inner_09", "temp_outer_09", "stress_09"]
    assert history.replay_template(FULL, "strain_03")[:3] == ["strain_03", "bridge_mvv_03", "strain_change_03"]
    assert history.replay_template(small_config(), "ti") == ["ti", "to", "dt", "sp"], "no operating quantity: every input is used"


@pytest.fixture
def full_api(archive_db_factory):
    router = ArchiveRouter(FULL, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    router.initialize()
    app = FastAPI()
    app.include_router(build_history_router(router, lambda: {"username": "viewer", "role": "viewer"}, FULL))
    return TestClient(app)


def test_a_replay_of_more_than_twelve_channels_is_refused_and_twelve_are_answered(full_api):
    """Before, any number of channels was taken - up to all 417 in one request."""
    channels = [p.id for p in FULL.points][:13]
    refused = full_api.get("/api/replay", params={"center": 1000, "seconds": 60, "points": ",".join(channels)})
    assert refused.status_code == 422 and "12" in refused.json()["detail"]
    assert full_api.get("/api/replay", params={"center": 1000, "seconds": 60, "points": ",".join(channels[:12])}).status_code == 200


def test_the_page_is_handed_the_cap_the_default_window_and_the_template_of_every_fault(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    viewer = accounts.create("viewer", "viewer")
    app = create_web_app(config=FULL, accounts=accounts, owner=Owner(FULL, 1000.0, 100.0, run_id="t"))
    with TestClient(app) as client:
        replay = client.get("/api/ui-config", headers={"Authorization": "Bearer " + viewer}).json()["replay"]
    assert replay["max_channels"] == 12
    assert replay["default_seconds"] == history.DEFAULT_WINDOW_SECONDS in (1800, 3600), "set from the measurement (修复轮03)"
    assert set(replay["templates"]) == set(LIMITED), "every channel that can raise a fault, and only those"
    assert all(replay["templates"][c] == history.replay_template(FULL, c) for c in LIMITED)
