"""D05/D06: the typed-record archive schema and its initialisation (终版 §5, §10, §13.2, §16.1).

Real PostgreSQL, new throw-away databases only.
"""

import psycopg
import pytest
from legacy_support import make_legacy

from htsg.archive import SCHEMA_GENERATION, LegacyDatabaseError, SchemaError
from htsg.config import Settings

CONFIG = Settings.model_validate({"sources": [{"id": "s", "kind": "modbus"}]})


@pytest.fixture
def store(archive_db_factory):
    store = archive_db_factory(prepare=False)
    store.initialize(CONFIG)
    store.prepare_bucket(1)
    return store

def test_the_one_index_of_a_stored_value_is_channel_and_time_and_no_older_table_is_left(store):
    """修复轮14: one index per row (the replay reads a channel over time); every further index costs bytes on every row."""
    with store.session() as conn:
        indexes = [x["indexdef"] for x in conn.execute("SELECT indexdef FROM pg_indexes WHERE tablename='sample'")]
        tables = {x["table_name"] for x in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}  # fmt: skip
    assert len(indexes) == 1 and "(channel, t)" in indexes[0]
    assert not tables & {"frame", "frame_value", "event", "source_sample", "schema_version", "observation", "result"}
    assert {"sample", "channel", "run_number", "state_code", "state_event", "fault_record", "commit_group", "segment",
            "archive_gap", "watermark", "config_version", "run", "account", "management_log"} <= tables  # fmt: skip


def test_full_configuration_snapshot_is_queryable_by_config_id(store):
    with store.session() as conn:
        row = conn.execute("SELECT payload FROM config_version WHERE id=%s", (CONFIG.config_id,)).fetchone()
    assert row["payload"]["sources"][0]["id"] == "s"
    assert row["payload"]["timing"]["ua_data_validity"] == 3


def test_a_fault_must_reference_a_stored_configuration(store):
    """The thresholds a fault was confirmed under stay findable: its configuration is kept for good."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation), store.session() as conn:
        conn.execute(
            "INSERT INTO fault_record(id,group_id,run_id,source_id,seg,batch_seq,point_id,direction,confirmed_wall,"
            "confirmed_mono,basis_kind,basis_id,basis_wall,config_id,rule) "
            "VALUES('f','g','r','s',1,1,'p','HIGH',1,1,'OBSERVATION','b',1,'no-such-config','{}')"
        )


def test_one_connection_generation_can_hold_several_segments(store):
    with store.session() as conn:
        conn.execute("INSERT INTO config_version(id,payload) VALUES('c','{}') ON CONFLICT DO NOTHING")
        for seg, reason in ((1, "START"), (2, "ARCHIVE_GAP"), (3, "RECOVERY")):
            conn.execute(
                "INSERT INTO segment(run_id,source_id,seg,conn_gen,start_wall,start_reason) VALUES('r','ua',%s,1,%s,%s)",
                (seg, 1000.0 + seg, reason),
            )
        rows = conn.execute("SELECT seg FROM segment WHERE conn_gen=1 ORDER BY seg").fetchall()
    assert [x["seg"] for x in rows] == [1, 2, 3]


def snapshot(store):
    with store.session() as conn:
        return {
            tuple(x.values())
            for x in conn.execute(
                "SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='public'"
            )
        }


def test_initialisation_is_idempotent(store):
    before = snapshot(store)
    store.initialize(CONFIG)
    store.initialize(CONFIG)
    assert snapshot(store) == before
    with store.session() as conn:
        assert conn.execute("SELECT count(*) AS n FROM config_version").fetchone()["n"] == 1
        assert conn.execute("SELECT value FROM htsg_meta WHERE key='schema_generation'").fetchone()["value"] == str(
            SCHEMA_GENERATION
        )


def test_a_legacy_frame_database_is_recognised_and_left_untouched(archive_db_factory):
    legacy = archive_db_factory(prepare=False)
    make_legacy(legacy, CONFIG)  # the frame-based generation
    before = snapshot(legacy)
    with pytest.raises(LegacyDatabaseError):
        legacy.initialize(CONFIG)
    assert snapshot(legacy) == before, "refusing must not add a single table or column"
    with legacy.session() as conn:
        assert conn.execute("SELECT to_regclass('public.htsg_meta') AS t").fetchone()["t"] is None


def test_a_foreign_schema_generation_is_refused(store):
    with store.session() as conn:
        conn.execute("UPDATE htsg_meta SET value='99' WHERE key='schema_generation'")
    with pytest.raises(SchemaError, match="99"):
        store.initialize(CONFIG)


def test_new_database_gets_accounts_through_the_normal_flow_not_by_migration(archive_db_factory):
    legacy = archive_db_factory(prepare=False)
    make_legacy(legacy, CONFIG, {"olduser": {"role": "admin", "salt": "s", "digest": "d", "active": True}})
    fresh = archive_db_factory(prepare=False)
    fresh.initialize(CONFIG)
    with fresh.session() as conn:
        assert conn.execute("SELECT count(*) AS n FROM account").fetchone()["n"] == 0, "legacy accounts are not copied"
    fresh.audit({"id": "1", "time": 1.0, "actor": "admin", "action": "X", "target": "t", "result": "OK"})
    assert fresh.audit_history()["items"][0]["actor"] == "admin"
