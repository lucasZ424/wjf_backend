"""Capstone F: a verifiable global replay (方案第 8 节).

Known values, a deliberate archive gap, a clock step, a quality-only change, duplicate-time versions and a route
switch to a second database are fed through the owner into real PostgreSQL; all 417 channels are then read back
through the HTTP API and checked against an oracle built from the script itself.
"""

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient
from live_support import BRIDGES, FULL, PROCESS, UA_NAMES
from test_archive_gap import Sink
from test_archive_router import Disks
from test_history_merge import Offline

from htsg import records as r
from htsg.archive_router import ArchiveRouter
from htsg.config import Settings
from htsg.history_api import build_history_router
from htsg.owner import Owner

HEADERS = {"Authorization": "Bearer t"}
MONO0 = 100.0


def wall(t):
    return 1000.0 + (t - 100) + (10.0 if t >= 137 else 0.0)  # a +10 s clock step at t=137


VALUES = {
    n: (440.0 if n.startswith("temp_outer") else 480.0 + i * 0.01 if n.startswith("temp_inner") else PROCESS[n])
    for i, n in enumerate(UA_NAMES)
}


def bridge(k, t):
    return 0.7 + 0.001 * k + 0.0001 * (t - 100)


def config_with_external():
    payload = FULL.model_dump(mode="json")
    payload["simulation"]["enabled"] = False
    payload["storage"].update(
        primary_path="D:/a", emergency_path="D:/b", external_path="D:/c", external_enabled=True
    )
    return Settings.model_validate(payload)


def script():
    """Run the scenario through the owner; returns (config, groups the archive accepted)."""
    cfg = config_with_external()
    sink, unconfirmed = Sink(), []

    def commit(group):
        accepted = sink(group)
        if accepted is not False:
            unconfirmed.append(group)  # the writer confirms durability only after the owner call has returned
        return accepted

    def confirm():
        while unconfirmed:
            owner.committed(unconfirmed.pop(0))

    owner = Owner(cfg, wall(100), MONO0, commit=commit, publish=lambda p: None, run_id="f")
    for source in ("mb_field", "ua_field"):
        owner.begin_generation(source, 1, wall(100), MONO0)
    seq = 0
    for t in range(101, 146):
        w, m = wall(t), float(t)
        sink.accept = not (126 <= t < 129)  # the archive refuses for three seconds
        items = []
        if t == 101:
            items = [r.Item(n, v) for n, v in VALUES.items()]
        elif t == 110:
            items = [r.Item("temp_inner_05", v) for v in (481.0, 482.0, 483.0)]  # three versions, one instant
        elif t == 120:
            items = [r.Item("temp_inner_06", VALUES["temp_inner_06"], r.BAD, 0x80350000)]
        elif t == 122:
            items = [r.Item("temp_inner_06", VALUES["temp_inner_06"])]  # same number, Good again
        elif t == 127:
            items = [r.Item("temp_inner_07", 479.0)]  # lost: the archive is refusing
        seq += 1
        owner.accept(r.InputBatch("ua_field", 1, r.KIND_UA_NOTIFY, w, m, tuple(items), publish_seq=seq))
        owner.accept(
            r.InputBatch("mb_field", 1, r.KIND_MODBUS_POLL, w, m,
                         tuple(r.Item(b, bridge(k, t)) for k, b in enumerate(BRIDGES)), tick=t - 100)
        )  # fmt: skip
        confirm()
        owner.tick(w, m)
        confirm()
    return cfg, sink.groups


@pytest.fixture(scope="module")
def scenario():
    return script()


@pytest.fixture
def world(archive_db_factory, scenario):
    cfg, groups = scenario
    disks = Disks()
    primary, emergency, external = (archive_db_factory(i) for i in ("primary", "emergency", "primary"))
    router = ArchiveRouter(cfg, primary, emergency, external, disk_usage=disks)
    router.initialize()
    router.bind_run("f", cfg.config_id, wall(100))
    for group in groups:
        # from wall 1024 on the normal database is over 95 %: later groups go to the removable one
        disks.used["D:/a"] = 0.96 if group.built_wall >= 1024 else 0.2
        disks.used["D:/c"] = 0.2
        assert router.write_group(group).durable, group.group_id
    bearer = HTTPBearer(auto_error=False)

    async def user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or credentials.credentials != "t":
            raise HTTPException(401, "no")
        return {"username": "viewer", "role": "viewer"}

    app = FastAPI()
    app.include_router(build_history_router(router, user, cfg))
    client = TestClient(app)
    client.router_ = router
    return client


ALL = ",".join(__import__("htsg.history", fromlist=["x"]).channel_sources(FULL))


def replay(client, **kw):
    params = {"center": 1030.0, "seconds": 100, "points": ALL, **kw}
    response = client.get("/api/replay", params=params, headers=HEADERS)
    assert response.status_code == 200, response.text[:300]
    return response.json()


def rows(body, pid, kind="raw", origin=None):
    out = [x for s in body["channels"][pid]["segments"] for x in s["rows"] if x["kind"] == kind]
    return [x for x in out if origin is None or x["origin"] == origin]


def test_all_417_channels_are_answered_with_nothing_truncated_or_partial(world):
    assert len(world.get("/api/channels", headers=HEADERS).json()["channels"]) == 417
    body = replay(world)
    assert len(body["channels"]) == 417 and not body["partial"] and not body["truncated"]
    assert body["sources"] == ["mb_field", "ua_field"]


def test_inputs_match_the_known_values_and_a_still_value_is_backed_by_its_evidence(world):
    body = replay(world)
    for name, value in VALUES.items():
        if name in ("temp_inner_05", "temp_inner_06", "temp_inner_07"):
            continue
        received = rows(body, name, origin=r.ORIGIN_RECEIVED)
        assert [(x["value"], x["observed_at"]) for x in received] == [(value, 1001.0)], name
    # a window that sees no change at all still carries the value in effect, with its original time
    quiet = replay(world, center=1033.0, seconds=4)  # inside the segment that began at the restatement (1029-1036)
    held = rows(quiet, "temp_outer_01")
    assert len(held) == 1 and held[0]["held"] is True and held[0]["value"] == 440.0
    assert held[0]["origin"] == r.ORIGIN_ASSERTION and held[0]["observed_at"] == 1001.0, "restated after the gap"
    assert held[0]["time"] == 1029.0 and held[0]["until"] == 1036.0, "counts from the restatement, ends with the segment"
    # the +10 s step left wall 1037-1046 with no segment at all: a window there must claim nothing
    hole = replay(world, center=1043.0, seconds=6)
    assert hole["channels"]["temp_outer_01"]["segments"] == []


def test_modbus_rounds_and_every_derived_value_follow_the_script(world):
    body = replay(world)
    # the round at t=129 reaches the owner while the archive is still lost (recovery is taken at the tick after it):
    # it is not stored as received, it enters history as the restated baseline of the recovery unit
    expected_polls = [t for t in range(101, 146) if not 126 <= t <= 129]
    for k, name in enumerate(BRIDGES):
        got = rows(body, name, origin=r.ORIGIN_RECEIVED)
        assert [x["observed_at"] for x in got] == [wall(t) for t in expected_polls], name
        assert [x["value"] for x in got] == pytest.approx([bridge(k, t) for t in expected_polls])
        restated = rows(body, name, origin=r.ORIGIN_ASSERTION)[0]
        assert (restated["time"], restated["observed_at"]) == (wall(129), wall(129)), "the round survives as a restatement"
        assert restated["value"] == pytest.approx(bridge(k, 129))
        strain = {x["observed_at"]: x["value"] for x in rows(body, f"strain_{k + 1:02d}", "derived", r.ORIGIN_RECEIVED)}
        for x in got:
            assert strain[x["observed_at"]] == pytest.approx(x["value"] * 1000 / 6), "no old input with a new result"
    first_inner, outer = VALUES["temp_inner_01"], VALUES["temp_outer_01"]
    assert rows(body, "delta_t_01", "derived", r.ORIGIN_RECEIVED)[0]["value"] == pytest.approx(first_inner - outer)
    assert rows(body, "stress_01", "derived", r.ORIGIN_RECEIVED)[0]["value"] == pytest.approx(1.1475 * (first_inner - outer))


def test_versions_at_one_instant_are_all_kept_in_batch_order(world):
    body = replay(world)
    got = rows(body, "temp_inner_05", origin=r.ORIGIN_RECEIVED)[1:]  # after the initial value
    assert [(x["value"], x["item_seq"]) for x in got] == [(481.0, 0), (482.0, 1), (483.0, 2)]
    assert len({x["time"] for x in got}) == 1
    derived = [x for x in rows(body, "delta_t_05", "derived", r.ORIGIN_RECEIVED) if x["time"] == got[0]["time"]]
    assert [x["value"] for x in derived] == [483.0 - 440.0], "only the batch-end result is calculated"


def test_a_quality_only_change_is_traceable_and_the_bad_interval_borrows_nothing(world):
    body = replay(world)
    got = rows(body, "temp_inner_06", origin=r.ORIGIN_RECEIVED)
    assert [(x["quality"], x["observed_at"]) for x in got] == [("GOOD", 1001.0), ("BAD", 1020.0), ("GOOD", 1022.0)]
    assert len({x["id"] for x in got}) == 3 and len({x["value"] for x in got}) == 1, "same number, three records"
    assert got[0]["until"] == 1020.0 and got[0]["ended_by"] == "NEXT_CHANGE", "Good ends where Bad begins"
    assert got[1]["valid"] is False and got[1]["status_code"] == 0x80350000 and got[1]["until"] == 1022.0
    derived = rows(body, "delta_t_06", "derived", r.ORIGIN_RECEIVED)
    assert [(x["status"], x["valid"]) for x in derived] == [("OK", True), ("NOT_AVAILABLE", False), ("OK", True)]
    third = world.get(f"/api/replay/basis/{derived[2]['id']}", headers=HEADERS).json()
    assert third["inputs"]["temp_inner_06"]["rows"][-1]["id"] == got[2]["id"], "the later Good value, not the first"
    middle = world.get(f"/api/replay/basis/{derived[1]['id']}", headers=HEADERS).json()
    assert middle["inputs"]["temp_inner_06"]["rows"][-1]["quality"] == "BAD"


def test_an_archive_gap_is_never_bridged_and_a_lost_sample_is_not_invented(world):
    body = replay(world)
    assert {(g["source_id"], g["start_wall"], g["end_wall"]) for g in body["gaps"]} >= {
        ("mb_field", 1026.0, 1029.0), ("ua_field", 1027.0, 1029.0),
    }  # fmt: skip
    spans = body["channels"]["temp_inner_07"]["segments"]
    assert len(spans) >= 2
    first = spans[0]["rows"][0]
    assert first["value"] == VALUES["temp_inner_07"] and first["until"] == 1027.0, "not stretched across the gap"
    assert not [x for x in rows(body, "temp_inner_07", origin=r.ORIGIN_RECEIVED) if x["value"] == 479.0]
    restated = spans[1]["rows"][0]
    assert restated["value"] == 479.0 and restated["origin"] == r.ORIGIN_ASSERTION
    assert (restated["time"], restated["observed_at"]) == (1029.0, 1027.0), "original time kept, counted from 1029"
    polls = {x["observed_at"] for x in rows(body, "bridge_mvv_01", origin=r.ORIGIN_RECEIVED)}
    assert not polls & {1026.0, 1027.0, 1028.0}, "no interpolation across the missing rounds"


def test_nothing_crosses_a_segment_and_the_clock_step_opens_one(world):
    body = replay(world)
    for pid in ("bridge_mvv_01", "temp_outer_01", "strain_01"):
        spans = body["channels"][pid]["segments"]
        assert len(spans) == 3, (pid, [s["seg"] for s in spans])
        for span in spans:
            for x in span["rows"]:
                if x["until"] is not None and span["end_wall"] is not None:
                    assert x["until"] <= span["end_wall"], "a hold never outlives its segment"
        assert spans[1]["rows"][0]["origin"] == r.ORIGIN_ASSERTION == spans[2]["rows"][0]["origin"]
        assert spans[2]["start_wall"] == 1047.0 and spans[0]["end_wall"] <= spans[1]["start_wall"] <= spans[2]["start_wall"]


def test_an_unreadable_database_is_shown_as_incomplete_and_the_row_cap_is_announced(world):
    router = world.router_
    router.external = Offline(router.external)
    body = replay(world)
    assert body["partial"] is True and body["unavailable"] == ["external"]
    assert body["coverage_complete_until"] is None and all(v is None for v in body["coverage_end"].values())
    seen = {x["observed_at"] for x in rows(body, "bridge_mvv_01", origin=r.ORIGIN_RECEIVED)}
    assert seen and max(seen) < 1030.0, "only what the readable database holds; nothing after is implied"
    capped = replay(world, limit=3)
    assert capped["truncated"] is True and capped["channels"]["bridge_mvv_01"]["truncated"] is True
    assert len(rows(capped, "bridge_mvv_01", origin=r.ORIGIN_RECEIVED)) <= 3


def test_converting_the_time_zone_does_not_move_the_utc_window(world):
    a = replay(world, center="2001-09-09T01:50:30+00:00", seconds=60)
    b = replay(world, center="2001-09-09T09:50:30+08:00", seconds=60)
    c = replay(world, center="2001-09-08T20:50:30-05:00", seconds=60)
    assert (a["start"], a["end"]) == (b["start"], b["end"]) == (c["start"], c["end"])
    assert rows(a, "bridge_mvv_01") == rows(b, "bridge_mvv_01")
