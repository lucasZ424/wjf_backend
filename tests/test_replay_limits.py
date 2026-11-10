"""Review item B (修复轮03, user rulings 2026-10-26/27): a replay asks for at most 12 channels at once.  A fault brings its fixed
template: the fault, its computation chain and the 7 process quantities (修复轮18) - taken from the configuration's own relations
and its list of process points only, since the site has no topology - and the default window.

Why it matters (the hard rule, a year without a crash): the per-channel limit alone let one replay of all channels pull
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
OPERATING = ["load_mw", "feedwater_flow", "main_steam_flow", "feedwater_temperature",
             "reheat_temperature", "working_fluid_temperature", "furnace_pressure"]  # fmt: skip


def test_the_template_of_a_wall_is_that_wall_and_the_seven_process_quantities():
    """E1, Q3: under the old rule every wall counted as an operating quantity once it fed no derived quantity: 12 walls, no load."""
    for wall in ("temp_wall_001", "temp_wall_215"):
        assert history.replay_template(FULL, wall) == [wall, *OPERATING]


def test_the_template_of_a_strain_is_its_chain_and_the_seven_process_quantities():
    """E2, Q3: the chain the fault is computed from and what is computed from it, 11 in all."""
    assert history.replay_template(FULL, "strain_03") == ["strain_03", "bridge_mvv_03", "strain_change_03", "stress_03", *OPERATING]


def test_the_template_of_a_process_quantity_is_the_seven_with_the_fault_first():
    """E3, Q3: the fault is the chart shown, and it is not listed twice."""
    assert history.replay_template(FULL, "feedwater_flow") == ["feedwater_flow", *(p for p in OPERATING if p != "feedwater_flow")]
    assert history.replay_template(FULL, "load_mw") == OPERATING


def test_no_template_takes_the_peak_or_more_than_twelve_channels():
    """E4, 终版 §14: checked for all 230 channels that can raise a fault."""
    assert len(LIMITED) == 230
    sizes = {}
    for channel in LIMITED:
        template = history.replay_template(FULL, channel)
        assert template[0] == channel, "the fault itself comes first: it is the chart shown"
        assert len(template) == len(set(template)) <= history.CHANNEL_LIMIT == 12
        assert "thermal_stress_peak" not in template, "a quantity of 8 inputs is not followed"
        assert template[-len(OPERATING):] == OPERATING or channel in OPERATING
        kind = channel.rsplit("_", 1)[0] if channel[-1].isdigit() else "process"
        sizes.setdefault(kind, set()).add(len(template))
    assert sizes == {"temp_wall": {8}, "strain": {11}, "process": {7}}, "终版 §14: 8, 11 and 7"


def test_a_quantity_of_more_than_two_inputs_is_not_followed_even_when_the_fault_is_one_of_them():
    """E4, 终版 §14: in the shipped table the peak rests on stresses, which raise no fault, so it is reached from nowhere; a site
    whose configuration lets a fault feed a quantity of three inputs must not drag that quantity and all its inputs in."""
    third = {"id": "t3", "source": "ua", "unit": "degC", "node_id": "ns=2;s=t3"}
    total = {"id": "tot", "inputs": ["ti", "to", "t3"], "operation": "sum", "unit": "degC"}  # one unit: a sum must not mix them
    config = small_config(extra_points=[third], extra_derived=[total])
    for channel in ("ti", "to", "t3"):
        template = history.replay_template(config, channel)
        assert "tot" not in template, channel
    assert history.replay_template(config, "ti") == ["ti", "to", "dt", "d2"], "nor its other inputs"


def test_a_configuration_without_process_quantities_gives_the_chain_alone():
    """E5 (用户 11-05): a site that filled in no process quantity; the template must not fail on the empty list."""
    assert history.replay_template(small_config(), "ti") == ["ti", "to", "dt", "d2"]
    assert history.replay_template(small_config(), "s1") == ["s1", "b1", "c1"]
    data = small_config().model_dump()
    assert data["ui"]["process_points"] in ((), [])
    lone = small_config(extra_points=[{"id": "x", "source": "ua", "unit": "u", "node_id": "ns=2;s=x"}])
    assert history.replay_template(lone, "x") == ["x"]


def test_the_template_is_cut_at_twelve_and_the_operating_quantities_go_last():
    """终版 §14: a site's configuration can have a longer chain; the fault and its chain are not what is lost."""
    process = [{"id": f"pr{n}", "source": "ua", "unit": "u", "node_id": f"ns=2;s=pr{n}"} for n in range(12)]
    config = small_config(extra_points=process, ui={"process_points": [p["id"] for p in process]})
    template = history.replay_template(config, "ti")
    assert template[:4] == ["ti", "to", "dt", "d2"] and template[4:] == [f"pr{n}" for n in range(8)]


@pytest.fixture
def full_api(archive_db_factory):
    router = ArchiveRouter(FULL, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    router.initialize()
    app = FastAPI()
    app.include_router(build_history_router(router, lambda: {"username": "viewer", "role": "viewer"}, FULL))
    return TestClient(app)


def test_a_replay_of_more_than_twelve_channels_is_refused_and_twelve_are_answered(full_api):
    """Before, any number of channels was taken - up to all of them in one request."""
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
