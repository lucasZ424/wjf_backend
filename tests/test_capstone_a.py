"""Capstone A: the data contract is executable (方案第 3 节).

Full-scale configuration loaded into a fresh PostgreSQL database; identities stay distinguishable;
configuration references resolve; wrong threshold or version input fails loudly.
"""

from pathlib import Path

import psycopg
import pytest
from legacy_support import make_legacy
from pydantic import ValidationError

from htsg import records as r
from htsg.archive import LegacyDatabaseError
from htsg.config import Settings, fault_rule, load_settings

ROOT = Path(__file__).resolve().parents[1]
FULL = load_settings(ROOT / "config" / "research-sim.json")


def put(conn, o: r.Observation, group="run1/g1"):
    data = {name: getattr(o, name) for name in o.__slots__}
    data["group_id"], data["bucket"] = group, 1
    conn.execute(
        f"INSERT INTO observation({','.join(data)}) VALUES({','.join(['%s'] * len(data))})", list(data.values())
    )


def test_full_scale_configuration_loads_and_each_result_reference_resolves(archive_db_factory):
    store = archive_db_factory()
    store.initialize(FULL)
    assert (len(FULL.points), len(FULL.derived)) == (208, 209)
    assert sum(1 for x in (*FULL.points, *FULL.derived) if fault_rule(x)) == 304
    with store.session() as conn:
        stored = conn.execute("SELECT payload FROM config_version WHERE id=%s", (FULL.config_id,)).fetchone()
        rebuilt = Settings.model_validate(stored["payload"])
        assert rebuilt.config_id == FULL.config_id, "the stored snapshot must reproduce the exact configuration"
        for table in ("result", "fault_record"):
            assert conn.execute(
                f"SELECT count(*) AS n FROM {table} t LEFT JOIN config_version c ON c.id=t.config_id WHERE c.id IS NULL"
            ).fetchone()["n"] == 0


def test_two_values_of_one_point_at_one_instant_and_a_state_assertion_stay_distinguishable(archive_db_factory):
    store = archive_db_factory()
    store.initialize(FULL)
    base = dict(
        run_id="run1", source_id="ua_field", conn_gen=1, seg=1, batch_seq=4, point_id="temp_inner_01",
        quality=r.GOOD, status_code=0, received_wall=1000.5, received_mono=20.0,
    )  # fmt: skip
    first = r.Observation(id=r.observation_id("run1", "ua_field", 4, 0), item_seq=0, value=480.0, **base)
    second = r.Observation(id=r.observation_id("run1", "ua_field", 4, 1), item_seq=1, value=495.0, **base)
    assertion = r.Observation(
        id=r.assertion_id("run1", "ua_field", 2, "temp_inner_01"),
        **{**base, "seg": 2, "batch_seq": 4, "conn_gen": 1},
        item_seq=-1,
        value=495.0,
        origin=r.ORIGIN_ASSERTION,
        orig_id=second.id,
        asserted_wall=1030.0,
        asserted_mono=50.0,
    )  # fmt: skip
    with store.session() as conn:
        for o in (first, second, assertion):
            put(conn, o)
        rows = conn.execute(
            "SELECT id,origin,orig_id,received_wall,effective_wall FROM observation "
            "WHERE point_id='temp_inner_01' ORDER BY effective_wall,batch_seq,item_seq"
        ).fetchall()
    assert len({x["id"] for x in rows}) == 3
    assert [x["origin"] for x in rows] == ["RECEIVED", "RECEIVED", "STATE_ASSERTION"]
    assert [x["effective_wall"] for x in rows] == [1000.5, 1000.5, 1030.0]
    assert rows[2]["orig_id"] == second.id and rows[2]["received_wall"] == 1000.5


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: {**p, "low": 600.0, "high": 490.0},  # inverted threshold
        lambda p: {**p, "hysteresis_high": 600.0},  # hysteresis larger than the band
        lambda p: {**p, "hysteresis_low": 1.0},  # hysteresis on a side without a threshold
    ],
)
def test_wrong_threshold_input_fails_validation_instead_of_loading(mutate):
    payload = FULL.model_dump(mode="json")
    payload["points"] = [mutate(p) if p["id"] == "temp_inner_01" else p for p in payload["points"]]
    with pytest.raises(ValidationError):
        Settings.model_validate(payload)


def test_a_result_naming_an_unknown_configuration_version_is_refused(archive_db_factory):
    store = archive_db_factory()
    store.initialize(FULL)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), store.session() as conn:
        conn.execute(
            "INSERT INTO fault_record(id,group_id,run_id,source_id,seg,batch_seq,point_id,direction,confirmed_wall,"
            "confirmed_mono,basis_kind,basis_id,basis_wall,config_id,rule) "
            "VALUES('f','g','r','ua_field',1,1,'p','HIGH',1,1,'OBSERVATION','o',1,'wrong-version','{}')"
        )


def test_legacy_database_stays_exactly_as_it_was(archive_db_factory):
    legacy = archive_db_factory(prepare=False)
    make_legacy(legacy, FULL)
    query = "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' ORDER BY 1,2"
    with legacy.session() as conn:
        before = conn.execute(query).fetchall()
    with pytest.raises(LegacyDatabaseError):
        legacy.initialize(FULL)
    with legacy.session() as conn:
        assert conn.execute(query).fetchall() == before
