"""修复轮05 (终版 §6, §13.3, §18.1): the pages read the owner's whole current state on request, and nothing of the
display runs on the batch path.

Why it matters: a page that fails to read must never cost a batch its archive or the acquisition its process, and a
page must be able to tell a new sample from one it already showed and from a restated one, so that its own delay can
be measured honestly.
"""

import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from owner_support import notify, poll, small_config

from htsg import records as r
from htsg.auth import Accounts
from htsg.owner import Owner
from htsg.runtime import Runtime
from htsg.web import create_web_app

WALL0, MONO0 = 1000.0, 100.0


def rig(tmp_path):
    config = small_config()
    accounts = Accounts(tmp_path / "accounts.json")
    credential = accounts.create("viewer", "viewer")
    owner = Owner(config, WALL0, MONO0, run_id="snap")
    for source in ("mb", "ua"):
        owner.begin_generation(source, 1, WALL0, MONO0)
    app = create_web_app(config=config, accounts=accounts, owner=owner)
    return SimpleNamespace(config=config, accounts=accounts, owner=owner, app=app, headers={"Authorization": "Bearer " + credential})


def test_one_reading_is_the_whole_current_state_and_names_each_sample(tmp_path):
    t = rig(tmp_path)
    t.owner.accept(notify([("ti", 500.0), ("to", 450.0)], wall=WALL0 + 1, mono=MONO0 + 1))
    t.owner.accept(poll(1, {"b1": 0.7, "b2": 0.8}))
    with TestClient(t.app) as client:
        before = time.time()
        response = client.get("/api/snapshot", headers=t.headers)
        after = time.time()
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"], "a cached reading would pass for a new state"
    body = response.json()
    assert set(body) == {"taken_time", "run_id", "channels", "status", "archive_target"}, "no publish sequence: nothing to keep continuous"
    assert body["run_id"] == "snap" and before <= body["taken_time"] <= after
    assert set(body["channels"]) == {p.id for p in t.config.points} | {d.id for d in t.config.derived}
    assert set(body["status"]) == {"sources", "faults"} and set(body["status"]["sources"]) == {"mb", "ua"}
    ti = body["channels"]["ti"]
    assert (ti["value"], ti["valid"], ti["origin"]) == (500.0, True, r.ORIGIN_RECEIVED)
    assert ti["observation_id"] == t.owner.points["ti"].obs.id, "the page can tell this sample from the next one"
    assert body["channels"]["dt"]["value"] == 50.0 and "observation_id" not in body["channels"]["dt"]


def test_a_steady_modbus_value_is_a_new_sample_every_round_and_an_opc_ua_keepalive_is_none(tmp_path):
    """The user's rule for the display delay (修复轮05): a new observation is told by its identity, not by its value."""
    t = rig(tmp_path)
    t.owner.accept(poll(1, {"b1": 0.7, "b2": 0.8}))
    t.owner.accept(notify([("ti", 500.0), ("to", 450.0)], wall=WALL0 + 1, mono=MONO0 + 1, seq=1))
    with TestClient(t.app) as client:
        first = client.get("/api/snapshot", headers=t.headers).json()["channels"]
        t.owner.accept(poll(2, {"b1": 0.7, "b2": 0.8}))  # the same values again: still a new round
        t.owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, WALL0 + 2, MONO0 + 2, publish_seq=2))  # keep-alive only
        second = client.get("/api/snapshot", headers=t.headers).json()["channels"]
    assert second["b1"]["value"] == first["b1"]["value"] and second["b1"]["observation_id"] != first["b1"]["observation_id"]
    assert second["ti"]["observation_id"] == first["ti"]["observation_id"], "a keep-alive proves activity, it is no sample"


def test_every_reading_is_authenticated_and_an_unsettled_or_stopped_account_is_refused_at_once(tmp_path):
    t = rig(tmp_path)
    with TestClient(t.app) as client:
        assert client.get("/api/snapshot").status_code == 401
        assert client.get("/api/snapshot", headers={"Authorization": "Bearer viewer:wrong"}).status_code == 401
        assert client.get("/api/snapshot", headers=t.headers).status_code == 200
        t.accounts.revoke("viewer")  # stopped: the very next reading is refused, the remembered credential with it
        assert client.get("/api/snapshot", headers=t.headers).status_code == 401


def test_a_failing_reading_fails_only_that_request_and_every_batch_still_goes_to_the_archive(tmp_path):
    """User rule 5 (修复轮05): the batch path calls no page encoding.  It used to: the push encoded every group with the
    channel views BEFORE the group was handed to the archive, so a failure there lost the batch and ended the owner loop."""
    config = small_config()
    runtime = Runtime(config, SimpleNamespace(), number_file=tmp_path / "bucket_number.json")  # the production wiring: owner -> commit queue (not run here)
    owner = runtime.owner

    def broken():
        raise RuntimeError("the display encoding failed")

    owner.channel_views = broken  # what a page reading needs - and what the old push needed for every group
    now, mono = time.time(), time.monotonic()
    owner.begin_generation("ua", 1, now, mono)
    owner.accept(notify([("ti", 500.0), ("to", 450.0)], wall=now, mono=mono))
    queued = runtime.queue.depth
    assert queued >= 2, "the generation start and the batch both went to the archive queue"
    accounts = Accounts(tmp_path / "accounts.json")
    headers = {"Authorization": "Bearer " + accounts.create("viewer", "viewer")}
    with TestClient(create_web_app(config=config, accounts=accounts, owner=owner), raise_server_exceptions=False) as client:
        assert client.get("/api/snapshot", headers=headers).status_code == 500, "this reading fails ..."
        owner.accept(notify([("ti", 501.0)], wall=now + 1, mono=mono + 1, seq=2))
        assert runtime.queue.depth == queued + 1, "... and the next batch reaches the archive all the same"
        assert client.get("/api/health", headers=headers).status_code == 200, "the rest of the backend answers"


def test_the_page_gets_its_three_time_limits_from_the_configuration_only(tmp_path):
    t = rig(tmp_path)
    with TestClient(t.app) as client:
        timing = client.get("/api/ui-config", headers=t.headers).json()["timing"]
    assert (timing["ui_poll_interval"], timing["ui_poll_timeout"], timing["ui_stall_notice"]) == (1, 5, 3)
    assert not [name for name in timing if name.startswith("push_")], "nothing of the removed push is handed out"


def test_every_page_learns_where_new_data_goes_and_an_unchecked_route_is_never_called_the_normal_database(tmp_path):
    """修复轮12 (终版 §13.5 “临时、未核对”, §13.6 “进入应急立即提示”): the snapshot every page reads names the target, so the
    emergency route can be shown on any page; the self-check reads the same value, not the routing mode - which says PRIMARY
    while the normal database takes groups unchecked."""
    t = rig(tmp_path)
    router = SimpleNamespace(mode="PRIMARY", provisional=True, health={}, target="PRIMARY_PROVISIONAL")
    app = create_web_app(config=t.config, accounts=t.accounts, owner=t.owner, archive_router=router)
    with TestClient(app) as client:
        assert client.get("/api/snapshot", headers=t.headers).json()["archive_target"] == "PRIMARY_PROVISIONAL"
        archive = client.get("/api/health", headers=t.headers).json()["archive"]
    assert (archive["routing"], archive["target"]) == ("PRIMARY", "PRIMARY_PROVISIONAL")
    with TestClient(t.app) as client:
        assert client.get("/api/snapshot", headers=t.headers).json()["archive_target"] is None, "no archive: nothing to name"


def test_the_scripts_are_asked_for_again_after_an_upgrade_and_a_refusal_is_said_in_chinese(tmp_path):
    """修复轮12: without a cache header the browser kept an old app.js after an upgrade; 终版 §17.5: the backend's
    explanation reaches the page in Chinese."""
    t = rig(tmp_path)
    with TestClient(t.app) as client:
        script = client.get("/static/app.js")
        again = client.get("/static/app.js", headers={"If-None-Match": script.headers["etag"]})
        refused = client.get("/api/snapshot", headers={"Authorization": "Bearer nobody:nothing"})
    assert script.status_code == 200 and script.headers["cache-control"] == "no-cache"
    assert again.status_code == 304, "unchanged: a short answer, not the file again"
    assert refused.status_code == 401 and refused.json()["detail"] == "用户名或口令不对，或账户已停用"
