"""D44d: GET /api/health carries the communication state (终版 §15.1, §11 约束第3条).

Why it matters: the communication page and the self-check page read exactly this and nothing else.  A value that was
paired with another reading's timestamp would look like a frozen or fresh source for no reason, a connect-failure count
rebuilt from log lines would change when the log is trimmed, and a new path or a device probe would break the promise
that looking at the page changes nothing.

Dependence: 方案 D44d; 审核 §8.1 S3、§8.4 R1; 终版 §15.1 "本页活动时每5秒读取一次health" "不新增独立API路径".
"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from owner_support import Recorder, notify, poll, small_config

from htsg import records as r
from htsg.auth import Accounts
from htsg.config import load_settings
from htsg.owner import Owner
from htsg.web import create_web_app

WALL0, MONO0 = 1000.0, 100.0
ROOT = Path(__file__).resolve().parents[1]


def at(mono):
    return WALL0 + (mono - MONO0), mono


class Rig:
    """The web application around a real owner, driven by hand; no archive, no protocols."""

    def __init__(self, tmp_path, failures=None, negotiated=None):
        self.rec = Recorder()
        self.owner = Owner(small_config(), WALL0, MONO0, commit=self.rec.commit, run_id="run")
        self.accounts = Accounts(tmp_path / "accounts.json")
        self.tokens = {"viewer": self.accounts.create("viewer", "viewer"), "admin": self.accounts.create("admin", "admin")}
        self.negotiated = negotiated if negotiated is not None else {"ua": {"publish_interval": 1.0, "keepalive_count": 2}}
        lifecycle = SimpleNamespace(health={"fatal": None}, connect_failures=lambda: dict(failures or {}))
        self.app = create_web_app(
            config=self.owner.config, accounts=self.accounts, owner=self.owner,
            diagnostics=lambda: self.negotiated, lifecycle=lifecycle,
        )  # fmt: skip

    async def get(self, role="viewer"):
        headers = {"Authorization": "Bearer " + self.tokens[role]} if role else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://htsg") as client:
            return await client.get("/api/health", headers=headers)

    async def health(self, role="viewer"):
        response = await self.get(role)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


async def test_the_reading_needs_a_login_and_either_role_may_take_it(rig):
    assert (await rig.get(None)).status_code == 401, "no credential, no health"
    for role in ("viewer", "admin"):
        assert "communication" in await rig.health(role)


async def test_the_old_parts_are_unchanged_and_the_communication_state_is_added(rig):
    health = await rig.health()
    assert set(health) == {"status", "archive", "negotiated", "runtime", "communication"}
    assert health["negotiated"] == rig.negotiated, "the negotiated parameters keep their shape and carry nothing else"
    assert set(health["status"]) == {"sources", "faults"}
    assert set(health["communication"]) == {"taken_time", "log", "inputs"}
    log = health["communication"]["log"]
    assert {"scope", "limit", "evicted", "oldest_time", "diag_lost", "incomplete", "entries"} <= set(log)
    assert (log["limit"], log["evicted"], log["diag_lost"], log["incomplete"], log["entries"]) == (500, 0, 0, False, [])
    assert log["scope"] == "THIS_RUN_IN_MEMORY", "the page can say which run, and that nothing older is kept"


async def test_every_source_carries_its_resync_state_and_its_own_connect_failures(tmp_path):
    rig = Rig(tmp_path, failures={"mb": 4})
    health = await rig.health()
    for source_id, source in health["status"]["sources"].items():
        assert {"resync_count", "persistent_resync", "connect_failures"} <= set(source), source_id
    assert health["status"]["sources"]["mb"]["connect_failures"] == 4
    assert health["status"]["sources"]["ua"]["connect_failures"] == 0, "no attempt counted yet: zero, not unknown"
    for n in health["negotiated"].values():
        assert "connect_failures" not in n and "resync_count" not in n, "kept apart from the negotiated parameters"


async def test_the_connect_failure_count_is_the_acquirers_own_and_not_read_from_the_log_or_the_resyncs(tmp_path):
    rig = Rig(tmp_path, failures={"ua": 5})
    log = rig.owner.commlog
    for k in range(700):  # the log moves 200 entries out and loses diagnostics
        log.event("ua", 1, "NOTE", f"N{k}", *at(101.0 + k))
    log.diag_lost = 9
    health = await rig.health()
    assert health["communication"]["log"]["evicted"] == 200 and health["communication"]["log"]["diag_lost"] == 9
    ua = health["status"]["sources"]["ua"]
    assert ua["connect_failures"] == 5 and ua["resync_count"] == 0, "neither trimming nor lost diagnostics nor resyncs touch it"


async def test_the_log_arrives_with_its_aggregates_and_end_markers(rig):
    rig.owner.attempt_failed("ua", "CONNECT", "REFUSED", "refused", False, *at(101.0))
    rig.owner.attempt_failed("ua", "CONNECT", "REFUSED", "refused again", False, *at(102.0))
    rig.owner.attempt_ok("ua", "CONNECT", *at(103.0))
    (entry,) = (await rig.health())["communication"]["log"]["entries"]
    assert (entry["event"], entry["phase"], entry["conn_gen"], entry["count"]) == ("ATTEMPT_FAILED", "CONNECT", None, 2)
    assert (entry["state"], entry["end_reason"], entry["ended_time"]) == ("ENDED", "CONNECT_OK", at(103.0)[0])
    assert (entry["first_failure"], entry["last_failure"]) == (at(101.0)[0], at(102.0)[0])
    assert entry["ended_time"] != entry["last_failure"]


# ---------------------------------------------------------------------------- the input table


def by_point(health):
    return {row["point_id"]: row for row in health["communication"]["inputs"]}


async def test_the_table_has_a_row_for_every_input_and_empty_fields_for_what_was_never_observed(rig):
    rows = (await rig.health())["communication"]["inputs"]
    assert [row["point_id"] for row in rows] == [p.id for p in rig.owner.config.points], "every input, in configuration order"
    never = rows[0]
    assert never["observation_id"] is None and never["value"] is None and never["received_time"] is None
    assert never["source_time"] is None and never["quality"] is None, "empty, not 0 and not 'now'"
    assert never["valid"] is False and never["reason"] == "SOURCE_WAITING"


async def test_a_row_is_one_observation_whole_and_a_newer_observation_replaces_the_whole_row(rig):
    """S3: value, quality, receive time and source timestamp of a row never come from two different observations."""
    owner = rig.owner
    owner.begin_generation("ua", 1, *at(101.0))
    seen = []
    for k in range(25):  # a stream of changes between the polls: value k is stamped with source time 1e9 + k
        item = r.Item("ti", 500.0 + k, r.GOOD if k % 2 == 0 else r.UNCERTAIN, 0, 1e9 + k)
        owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, *at(102.0 + k), (item,), publish_seq=k + 1))
        if k % 6 == 0:  # the page polls now and then, not after every change
            row = by_point(await rig.health())["ti"]
            seen.append(row)
            assert row["value"] - 500.0 == row["source_time"] - 1e9, "the value and its OWN source timestamp"
            assert row["quality"] == (r.GOOD if (row["value"] - 500) % 2 == 0 else r.UNCERTAIN), "and its own quality"
            assert row["received_time"] == at(102.0 + (row["value"] - 500.0))[0], "and the time it was received"
            assert row["observation_id"] == owner.points["ti"].obs.id
    assert len({row["observation_id"] for row in seen}) == len(seen) == 5, "each poll shows the newest whole observation"


async def test_a_source_timestamp_is_shown_as_given_and_absent_where_the_protocol_has_none(rig):
    owner = rig.owner
    owner.begin_generation("ua", 1, *at(101.0))
    owner.begin_generation("mb", 1, *at(101.0))
    owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, *at(102.0), (r.Item("ti", 500.0, r.GOOD, 0, 12345.5), r.Item("to", 450.0, r.GOOD, 0, None))))
    owner.accept(poll(1, {"b1": 0.5}, wall=at(102.0)[0], mono=102.0))
    rows = by_point(await rig.health())
    assert rows["ti"]["source_time"] == 12345.5, "kept as the server gave it"
    assert rows["to"]["source_time"] is None, "the server gave none: unknown, nothing is made up"
    assert rows["b1"]["source_time"] is None and rows["b1"]["received_time"] == at(102.0)[0], "Modbus has no source timestamp"
    assert rows["ti"]["received_time"] == at(102.0)[0], "the receive time is its own column"


async def test_a_row_that_stays_the_same_across_polls_is_not_a_new_observation(rig):
    """The poll refreshes nothing: it is not a sample, so what it reads does not move on its own."""
    owner = rig.owner
    owner.begin_generation("ua", 1, *at(101.0))
    owner.accept(notify([("ti", 500.0)], gen=1, wall=at(102.0)[0], mono=102.0))
    first = by_point(await rig.health())["ti"]
    owner.tick(*at(103.0))
    second = by_point(await rig.health())["ti"]
    assert first == second


async def test_validity_is_the_owners_state_at_the_moment_of_the_response_and_the_value_stays_visible(rig):
    owner = rig.owner
    owner.begin_generation("ua", 1, *at(101.0))
    owner.accept(notify([("ti", 500.0)], gen=1, wall=at(102.0)[0], mono=102.0))
    row = by_point(await rig.health())["ti"]
    assert row["valid"] is True and row["reason"] is None
    owner.source_down("ua", 1, "CONNECTION", *at(103.0))
    row = by_point(await rig.health())["ti"]
    assert row["valid"] is False and row["reason"] == "SOURCE_RESYNC", "the cached value is shown, but not as usable"
    assert row["value"] == 500.0 and row["observation_id"], "and it is still that same observation"


async def test_a_recovery_in_the_same_subscription_does_not_turn_a_row_into_a_new_observation(rig):
    """The restatement belongs to the archive's recovery group; what the owner holds, and the table shows, is still the
    observation that was really received - with its own time - and it is usable again."""
    owner = rig.owner
    owner.begin_generation("ua", 1, *at(101.0))
    owner.begin_waiting("ua", 1, 101.0)
    owner.accept(notify([("ti", 500.0), ("to", 450.0)], gen=1, wall=at(102.0)[0], mono=102.0))
    original = by_point(await rig.health())["ti"]
    owner.tick(*at(105.0))  # 3 s quiet: expired
    assert by_point(await rig.health())["ti"]["valid"] is False
    owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, *at(105.5)))  # a keep-alive: same subscription again
    row = by_point(await rig.health())["ti"]
    assert row["valid"] is True
    assert {k: row[k] for k in row if k not in ("valid", "reason")} == {k: original[k] for k in original if k not in ("valid", "reason")}
    assert row["received_time"] == at(102.0)[0], "still the time it was really received, not the time of the keep-alive"


async def test_reading_the_health_creates_no_event_group_or_state_change(rig):
    owner = rig.owner
    owner.begin_generation("ua", 1, *at(101.0))
    owner.accept(notify([("ti", 500.0)], gen=1, wall=at(102.0)[0], mono=102.0))
    before = (len(owner.commlog.entries), owner.order, len(rig.rec.commits), owner.health.copy(), owner.channel_views())
    first = await rig.health()
    for _ in range(5):
        await rig.health("admin")
    after = (len(owner.commlog.entries), owner.order, len(rig.rec.commits), owner.health.copy(), owner.channel_views())
    assert before == after, "looking is not an event"
    assert (await rig.health())["communication"]["log"]["entries"] == first["communication"]["log"]["entries"]


# ---------------------------------------------------------------------------------- the interface


def build_documented_app(tmp_path):
    """The way scripts/export_openapi.py builds it."""
    config = load_settings(ROOT / "config" / "development.json")
    owner = Owner(config, 0.0, 0.0, run_id="openapi")
    return create_web_app(
        config=config, accounts=Accounts(tmp_path / "accounts.json"), owner=owner,
        archive_router=SimpleNamespace(),
    )  # fmt: skip


def test_no_path_was_added_and_the_old_communications_interface_did_not_come_back(tmp_path):
    paths = set(build_documented_app(tmp_path).openapi()["paths"])
    assert paths == {
        "/live", "/api/me", "/api/ui-config", "/api/health", "/api/admin/accounts", "/api/admin/accounts/{username}",
        "/api/admin/password", "/api/admin/audit", "/api/channels", "/api/faults", "/api/replay", "/api/replay/basis/{result_id}",
        "/api/snapshot",
    }  # fmt: skip  (/api/admin/password: the administrator's own password, 修复轮02; /api/snapshot: the live reading, 修复轮05)
    assert not [p for p in paths if "communication" in p or "cursor" in p]


def test_the_checked_in_openapi_description_matches_the_application(tmp_path):
    generated = build_documented_app(tmp_path).openapi()
    checked_in = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert generated == checked_in, "run scripts/export_openapi.py after changing an interface"
    schema = checked_in["paths"]["/api/health"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert {"status", "archive", "negotiated", "runtime", "communication"} <= set(schema["properties"])
    assert set(schema["properties"]["communication"]["properties"]) == {"taken_time", "log", "inputs"}
