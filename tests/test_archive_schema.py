"""D05/D06: the typed-record archive schema and its initialisation (终版 §5, §10, §13.2, §16.1).

Real PostgreSQL, new throw-away databases only.
"""

import psycopg
import pytest
from legacy_support import make_legacy

from htsg import records as r
from htsg.archive import SCHEMA_GENERATION, LegacyDatabaseError, SchemaError
from htsg.config import Settings

CONFIG = Settings.model_validate({"sources": [{"id": "s", "kind": "modbus"}]})


@pytest.fixture
def store(archive_db_factory):
    store = archive_db_factory()
    store.initialize(CONFIG)
    return store


def observation_row(**changes):
    row = dict(
        id="run1/ua/b1/i0", group_id="run1/g1", run_id="run1", source_id="ua", conn_gen=1, seg=1,
        batch_seq=1, item_seq=0, point_id="p", value=1.0, quality="GOOD", status_code=0,
        received_wall=1000.0, received_mono=5.0, origin=r.ORIGIN_RECEIVED,
    )  # fmt: skip
    return {**row, **changes}


def insert_observation(conn, **changes):
    row = observation_row(**changes)
    columns = ",".join(row)
    conn.execute(f"INSERT INTO observation({columns}) VALUES({','.join(['%s'] * len(row))})", list(row.values()))


def test_status_code_holds_unsigned_32_bit_values(store):
    with store.session() as conn:
        insert_observation(conn, status_code=0xFFFFFFFF)
        insert_observation(conn, id="run1/ua/b1/i1", item_seq=1, status_code=0x80350000)
        codes = [x["status_code"] for x in conn.execute("SELECT status_code FROM observation ORDER BY id")]
    assert codes == [0xFFFFFFFF, 0x80350000]


def test_several_records_for_one_point_at_one_instant_are_all_stored_in_batch_order(store):
    with store.session() as conn:
        for n, value in enumerate((1.0, 2.0, 3.0)):
            insert_observation(conn, id=f"run1/ua/b1/i{n}", item_seq=n, value=value)
        rows = conn.execute(
            "SELECT value FROM observation WHERE point_id='p' ORDER BY effective_wall,batch_seq,item_seq"
        ).fetchall()
    assert [x["value"] for x in rows] == [1.0, 2.0, 3.0]


def test_stable_id_is_unique_so_a_retried_group_cannot_duplicate_history(store):
    with store.session() as conn:
        insert_observation(conn)
        conn.execute(
            "INSERT INTO observation(id,group_id,run_id,source_id,conn_gen,seg,batch_seq,item_seq,point_id,"
            "quality,received_wall,received_mono,origin) "
            "VALUES('run1/ua/b1/i0','g','r','s',1,1,1,0,'p','GOOD',1,1,'RECEIVED') ON CONFLICT(id) DO NOTHING"
        )
        assert conn.execute("SELECT count(*) AS n FROM observation").fetchone()["n"] == 1
    with pytest.raises(psycopg.errors.UniqueViolation), store.session() as conn:
        insert_observation(conn)


def test_history_time_of_an_assertion_is_its_assertion_time(store):
    with store.session() as conn:
        insert_observation(
            conn, id="run1/ua/a2/p", origin=r.ORIGIN_ASSERTION, orig_id="run1/ua/b1/i0",
            asserted_wall=2000.0, asserted_mono=50.0, seg=2,
        )  # fmt: skip
        row = conn.execute("SELECT received_wall,effective_wall FROM observation").fetchone()
    # the original observation time is kept, but history counts the value from the assertion time
    assert (row["received_wall"], row["effective_wall"]) == (1000.0, 2000.0)


def test_point_time_and_order_indexes_exist_and_no_frame_table_does(store):
    with store.session() as conn:
        indexes = {x["indexdef"] for x in conn.execute("SELECT indexdef FROM pg_indexes WHERE tablename='observation'")}
        tables = {x["table_name"] for x in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}  # fmt: skip
    assert any("(point_id, effective_wall, batch_seq, item_seq)" in d for d in indexes)
    assert any("(run_id, source_id, batch_seq, item_seq)" in d for d in indexes)
    assert not tables & {"frame", "frame_value", "event", "source_sample", "schema_version"}
    assert {"observation", "result", "state_event", "fault_record", "commit_group", "segment",
            "archive_gap", "watermark", "config_version", "run", "account", "management_log"} <= tables  # fmt: skip


def test_full_configuration_snapshot_is_queryable_by_config_id(store):
    with store.session() as conn:
        row = conn.execute("SELECT payload FROM config_version WHERE id=%s", (CONFIG.config_id,)).fetchone()
    assert row["payload"]["sources"][0]["id"] == "s"
    assert row["payload"]["timing"]["ua_data_validity"] == 3


def test_a_result_must_reference_a_stored_configuration(store):
    with pytest.raises(psycopg.errors.ForeignKeyViolation), store.session() as conn:
        conn.execute(
            "INSERT INTO result(id,group_id,run_id,source_id,conn_gen,seg,batch_seq,point_id,status,config_id,"
            "at_wall,origin) VALUES('x','g','r','s',1,1,1,'p','OK','no-such-config',1,'RECEIVED')"
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
    legacy = archive_db_factory()
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
    legacy = archive_db_factory()
    make_legacy(legacy, CONFIG, {"olduser": {"role": "admin", "salt": "s", "digest": "d", "active": True}})
    fresh = archive_db_factory()
    fresh.initialize(CONFIG)
    assert fresh.read_accounts() == {}, "legacy accounts are not copied into a new database"
    fresh.sync_accounts({"admin": {"role": "admin", "salt": "s", "digest": "d", "active": True}})
    assert set(fresh.read_accounts()) == {"admin"}
    fresh.audit({"id": "1", "time": 1.0, "actor": "admin", "action": "X", "target": "t", "result": "OK"})
    assert fresh.audit_history()["items"][0]["actor"] == "admin"
