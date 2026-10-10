"""D35: fault search and replay API (终版 §14, §17).

Why it matters: this is where the replay page gets everything it shows.  It must be readable without any
fault, say how far its answer reaches and where it is incomplete, refuse what it cannot answer, and offer
no way at all to change the channel table at run time.
"""

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient
from test_archive_router import Disks, config
from test_archive_write import make_group
from test_history_merge import Offline

from htsg.archive_router import ArchiveRouter
from htsg.history_api import build_history_router

TOKENS = {"viewer-token": "viewer", "admin-token": "admin"}
VIEWER = {"Authorization": "Bearer viewer-token"}
ADMIN = {"Authorization": "Bearer admin-token"}
# 2001-09-09T01:46:40Z, an instant chosen so the same moment can be written with different offsets
T0 = 1_000_000_000.0


@pytest.fixture
def api(archive_db_factory):
    cfg = config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=Disks())
    router.initialize()
    router.bind_run("run1", cfg.config_id, T0 - 100)
    for n in range(1, 6):
        assert router.write_group(make_group(order=n, config_id=cfg.config_id)).durable  # stamped 1001..1005

    bearer = HTTPBearer(auto_error=False)

    async def user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or credentials.credentials not in TOKENS:
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
        return {"username": TOKENS[credentials.credentials], "role": TOKENS[credentials.credentials]}

    app = FastAPI()
    app.include_router(build_history_router(router, user, cfg))
    client = TestClient(app)
    client.router, client.cfg, client.app_ref = router, cfg, app
    return client


def replay(client, **kw):
    params = {"center": 1003.0, "seconds": 10, "points": "ti", **kw}
    return client.get("/api/replay", params=params, headers=VIEWER)


def test_every_endpoint_needs_credentials_and_accepts_viewer_and_admin(api):
    for path in ("/api/channels", "/api/faults", "/api/replay?center=1&seconds=1&points=ti", "/api/replay/basis/x"):
        assert api.get(path).status_code == 401, path
        assert api.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    for headers in (VIEWER, ADMIN):
        assert api.get("/api/channels", headers=headers).status_code == 200
        assert api.get("/api/faults", headers=headers).status_code == 200


def test_there_is_no_way_to_add_remove_or_change_a_channel_at_run_time(api):
    paths = api.app_ref.openapi()["paths"]
    for path, methods in paths.items():
        assert set(methods) == {"get"}, (path, set(methods))
    assert api.post("/api/channels", headers=ADMIN, json={"id": "x"}).status_code in (404, 405)
    assert api.delete("/api/channels/ti", headers=ADMIN).status_code in (404, 405)
    listed = {c["id"]: c for c in api.get("/api/channels", headers=VIEWER).json()["channels"]}
    assert listed["ti"]["kind"] == "input" and listed["dt"]["kind"] == "derived" and listed["dt"]["source_id"] == "ua"


def test_a_window_can_be_replayed_without_any_fault_record(api):
    with api.router.primary.session() as conn:
        conn.execute("DELETE FROM fault_record")
    assert api.get("/api/faults", headers=VIEWER).json()["items"] == []
    body = replay(api).json()
    rows = [x for s in body["channels"]["ti"]["segments"] for x in s["rows"] if x["kind"] == "raw"]
    assert rows, "history is queried by channel and time, not by fault"
    assert body["channels"]["ti"]["unit"] == "degC", "the unit comes from the configuration"


def test_the_answer_states_query_time_coverage_gaps_sources_and_truncation(api):
    body = replay(api).json()
    for key in ("query_time", "coverage_end", "coverage_complete_until", "gaps", "sources", "truncated", "partial",
                "unavailable", "limit", "start", "end", "center", "seconds"):
        assert key in body, key
    assert (body["start"], body["end"]) == (998.0, 1008.0), "half a window either side of the centre"
    assert body["sources"] == ["ua"] and body["partial"] is False and body["truncated"] is False
    assert body["limit"] == 10000


def test_a_channel_that_hits_the_row_limit_says_so(api):
    body = replay(api, limit=2).json()
    assert body["truncated"] is True and body["channels"]["ti"]["truncated"] is True
    assert api.get("/api/replay", params={"center": 1, "seconds": 1, "points": "ti", "limit": 10001}, headers=VIEWER).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"points": "nope"},
        {"points": ""},
        {"seconds": 0},
        {"seconds": -5},
        {"seconds": 367 * 86400},
        {"center": "yesterday"},
        {"center": "2001-09-09T01:46:40"},  # no offset: ambiguous, refused
        {"center": "nan"},
    ],
)
def test_requests_that_cannot_be_answered_are_refused(api, params):
    assert replay(api, **params).status_code == 422


def test_the_same_instant_written_in_another_time_zone_queries_the_same_utc_window(api):
    utc = replay(api, center="2001-09-09T01:46:40+00:00", seconds=20).json()
    beijing = replay(api, center="2001-09-09T09:46:40+08:00", seconds=20).json()
    epoch = replay(api, center=T0, seconds=20).json()
    assert (utc["start"], utc["end"]) == (beijing["start"], beijing["end"]) == (epoch["start"], epoch["end"])
    assert utc["center"] == T0


def test_fault_search_filters_and_pages_with_a_cursor(api):
    page = api.get("/api/faults", params={"limit": 2}, headers=VIEWER).json()
    assert len(page["items"]) == 2 and page["has_more"] and page["next_cursor"]
    seen = [x["id"] for x in page["items"]]
    while page["has_more"]:
        page = api.get("/api/faults", params={"limit": 2, "cursor": page["next_cursor"]}, headers=VIEWER).json()
        seen += [x["id"] for x in page["items"]]
    assert len(seen) == len(set(seen)) == 5
    assert api.get("/api/faults", params={"point": "other"}, headers=VIEWER).json()["items"] == []
    assert api.get("/api/faults", params={"direction": "SIDEWAYS"}, headers=VIEWER).status_code == 422
    assert api.get("/api/faults", params={"limit": 0}, headers=VIEWER).status_code == 422
    other = api.get("/api/faults", params={"limit": 2}, headers=VIEWER).json()["next_cursor"]
    assert api.get("/api/faults", params={"point": "ti", "cursor": other}, headers=VIEWER).status_code == 422
    one = api.get("/api/faults", params={"point": "ti", "direction": "HIGH", "source": "ua"}, headers=VIEWER).json()
    assert len(one["items"]) == 5 and one["items"][0]["rule"]["high"] == 490.0


def test_a_selected_fault_leads_to_a_replay_window_around_its_time(api):
    fault = api.get("/api/faults", params={"limit": 1}, headers=VIEWER).json()["items"][0]
    body = replay(api, center=fault["confirmed_wall"], seconds=30, points="ti,dt").json()
    assert set(body["channels"]) == {"ti", "dt"}, "other channels of the same window can be looked at too"


def test_result_basis_is_reachable_and_an_unknown_result_is_404(api):
    assert api.get("/api/replay/basis/run1/ua/b1/r/dt", headers=VIEWER).status_code == 200
    assert api.get("/api/replay/basis/no/such/result", headers=VIEWER).status_code == 404


def test_storage_trouble_is_partial_when_one_database_fails_and_503_when_all_do(api):
    real = api.router.emergency
    api.router.emergency = Offline(real)
    body = replay(api).json()
    assert body["partial"] is True and body["unavailable"] == ["emergency"] and body["coverage_complete_until"] is None
    api.router.primary = Offline(api.router.primary)
    assert replay(api).status_code == 503
    assert api.get("/api/faults", headers=VIEWER).status_code == 503


def test_legacy_capture_products_are_never_mixed_into_the_answer(api):
    with api.router.primary.session() as conn:
        conn.execute("CREATE TABLE frame_value(point_id text, payload jsonb)")
        conn.execute("INSERT INTO frame_value VALUES('ti','{\"value\": 12345}')")
        conn.execute("CREATE TABLE source_sample(point_id text, effective_time float8, payload jsonb)")
        conn.execute("INSERT INTO source_sample VALUES('ti', 1003, '{\"value\": 12345}')")
    text = replay(api).text
    assert "12345" not in text
