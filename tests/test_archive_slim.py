"""修复轮14: every stored value is one narrow row of `sample`.

Why it matters: the bytes per row decide how long the disk holds the history.  Slimming must not cost what the replay
needs: the state of a value, the OPC UA status code, the order of the values of one batch, and the right channel when
the two databases number their channels each on its own.  A row this program cannot fully read must never break the
replay (the one-year rule).
"""

import pytest
from owner_support import small_config

from htsg import history as h
from htsg import records as r
from htsg.archive import stored_rows

CONFIG = small_config()
T0 = 1_800_000_000.0


def obs(point, value, wall, item, *, source="ua", quality=r.GOOD, status=None, extreme=False, seg=1, asserted=None):
    return r.Observation(
        id=f"run/{source}/{point}/{wall}/{item}", run_id="run", source_id=source, conn_gen=1, seg=seg, batch_seq=1,
        item_seq=item, point_id=point, value=value, quality=quality, status_code=status, received_wall=wall,
        received_mono=wall, extreme=extreme, asserted_wall=asserted,
    )  # fmt: skip


def res(point, value, wall, *, source="ua", status=r.RESULT_OK, reason=None, extreme=False):
    return r.Result(
        id=f"run/{source}/{point}/{wall}/r", run_id="run", source_id=source, conn_gen=1, seg=1, batch_seq=1,
        point_id=point, value=value, status=status, reason=reason, config_id=CONFIG.config_id, at_wall=wall,
        extreme=extreme,
    )  # fmt: skip


def seg_start(wall, source="ua"):
    return r.StateRecord(f"run/{source}/start", "run", source, 1, "SEG_START", wall, wall, {"conn_gen": 1, "reason": "CONNECT"})


def group(order, observations=(), results=(), states=(), source="ua"):
    return r.CommitGroup(
        f"run/g{order}", order, "run", source, r.GROUP_BATCH, T0 + order, tuple(observations), tuple(results),
        tuple(states), bucket=1,
    )  # fmt: skip


def written(store, *groups):
    store.bind_run("run", CONFIG.config_id, T0)
    for g in groups:
        assert store.write_group(g).status == "COMMITTED", g.group_id
    return store


def rows(store, sql, *args):
    with store.session() as conn:
        return conn.execute(sql, args).fetchall()


def replay(stores, start, end, channels=None):
    return h.query(stores, channels or {"ti": "ua"}, start, end)


def shown(result, point="ti"):
    return [x for span in result["channels"][point]["segments"] for x in span["rows"]]


def test_a_stored_value_is_the_nine_narrow_columns_and_nothing_per_row_names_it(archive_db_factory):
    """The byte count of 修复轮14 holds only if no text, id or JSON column comes back into the row."""
    store = archive_db_factory()
    columns = rows(
        store,
        "SELECT column_name,data_type FROM information_schema.columns WHERE table_name='sample' ORDER BY ordinal_position",
    )
    assert [(c["column_name"], c["data_type"]) for c in columns] == [
        ("t", "double precision"), ("value", "double precision"), ("bucket", "integer"), ("run", "integer"),
        ("seg", "integer"), ("ua_status", "integer"), ("channel", "smallint"), ("state", "smallint"), ("item", "smallint"),
    ]  # fmt: skip
    assert rows(store, "SELECT to_regclass('observation') AS o, to_regclass('result') AS r") == [{"o": None, "r": None}]


def test_every_quality_and_result_state_reads_back_as_it_was_and_an_unknown_one_never_fails():
    """The replay names the quality or the reason from the code alone: a code that read back as another would show a
    wrong state, and a quality or code the table does not know must neither be refused nor break a read."""
    for code, key, _ in r.STATE_CODES:
        for extreme in (False, True):
            if code < r.DERIVED_FROM:
                back = r.read_state(r.input_state(key, extreme))
                assert (back["kind"], back["quality"], back["extreme"]) == ("raw", key if code else "UNKNOWN", extreme)
            else:
                status = r.RESULT_OK if key == r.RESULT_OK else r.RESULT_NA
                back = r.read_state(r.result_state(status, None if status == r.RESULT_OK else key, extreme))
                assert (back["status"], back["reason"], back["extreme"]) == (
                    status, None if status == r.RESULT_OK else key, extreme
                )  # fmt: skip
    assert r.read_state(r.input_state("SOMETHING_NEW", False))["quality"] == "UNKNOWN"
    assert r.read_state(r.result_state(r.RESULT_NA, "division by zero", False))["reason"] == "CALC_ERROR", "free text: 计算出错"
    assert r.read_state(9)["quality"] == "UNKNOWN", "an unknown code in the input range"
    assert (r.read_state(99)["status"], r.read_state(99)["reason"]) == (r.RESULT_NA, "UNKNOWN_STATE"), "in the result range"


def test_the_opc_ua_status_code_keeps_its_high_bit(archive_db_factory):
    """Bad and Uncertain codes have the top bit set: stored as a signed int4 they must read back unsigned, exactly."""
    bad = 0x80AB0000
    store = written(archive_db_factory(), group(1, [obs("ti", 5.0, T0 + 1, 0, quality=r.BAD, status=bad)], states=[seg_start(T0)]))
    assert rows(store, "SELECT ua_status FROM sample") == [{"ua_status": bad - 2**32}]
    (row,) = shown(replay([("primary", store)], T0, T0 + 10))
    assert row["status_code"] == bad and row["quality"] == r.BAD
    assert rows(store, "SELECT ua_status,state FROM sample_view") == [{"ua_status": "0x80AB0000", "state": "不良"}]


def test_two_values_of_one_point_in_one_batch_keep_their_order_and_the_last_one_holds(archive_db_factory):
    """One OPC UA batch may carry the same point twice with one receipt time (终版 §10.2): only the order inside the batch
    tells which value ended the batch, and that is the value the replay must hold on."""
    wall = T0 + 1
    store = written(archive_db_factory(), group(1, [obs("ti", 2.0, wall, 1), obs("ti", 1.0, wall, 0)], states=[seg_start(T0)]))
    assert [x["value"] for x in shown(replay([("primary", store)], T0, T0 + 10))] == [1.0, 2.0]
    (held,) = shown(replay([("primary", store)], T0 + 5, T0 + 10))
    assert held["value"] == 2.0 and held["held"], "the batch-end value, not the one in the middle"


def test_each_database_numbers_its_channels_on_its_own_and_the_replay_still_names_each_value_right(archive_db_factory):
    """The two databases meet the channels in different orders, so the same number means different channels in each:
    the replay must read every database through its own table, or values land on the wrong channel."""
    primary, emergency = archive_db_factory(), archive_db_factory("emergency")
    written(primary, group(1, [obs("ti", 10.0, T0 + 1, 0), obs("to", 20.0, T0 + 1, 1)], states=[seg_start(T0)]))
    written(emergency, group(2, [obs("to", 21.0, T0 + 3, 0), obs("ti", 11.0, T0 + 3, 1)]))
    numbers = [rows(s, "SELECT no,point_id FROM channel ORDER BY no") for s in (primary, emergency)]
    assert [x["point_id"] for x in numbers[0]] != [x["point_id"] for x in numbers[1]], "the case this test is about"
    result = replay([("primary", primary), ("emergency", emergency)], T0, T0 + 10, {"ti": "ua", "to": "ua"})
    assert [x["value"] for x in shown(result, "ti")] == [10.0, 11.0]
    assert [x["value"] for x in shown(result, "to")] == [20.0, 21.0]


def test_a_group_written_again_adds_no_row(archive_db_factory):
    """Rows carry no id: only the group's own record keeps a retried group from being stored twice."""
    store = archive_db_factory()
    g = group(1, [obs("ti", 1.0, T0 + 1, 0)], [res("dt", 3.0, T0 + 1)], [seg_start(T0)])
    written(store, g)
    assert store.write_group(g).status == "ALREADY"
    assert rows(store, "SELECT count(*) AS n FROM sample") == [{"n": 2}]
    assert rows(store, "SELECT records FROM commit_group") == [{"records": stored_rows(g)}] == [{"records": 2}]


def test_a_baseline_row_and_a_restatement_fit_the_order_column(archive_db_factory):
    """A baseline record orders after everything (2**31-1) and a restatement before (-1): written as they are, the
    baseline would overflow the smallint and every baseline group - and with it the rolling deletion - would fail."""
    store = written(
        archive_db_factory(),
        group(1, [obs("ti", 1.0, T0 + 1, 2**31 - 1, asserted=T0 + 2), obs("to", 2.0, T0 + 1, -1, asserted=T0 + 2)],
              states=[seg_start(T0)]),
    )  # fmt: skip
    assert sorted(x["item"] for x in rows(store, "SELECT item FROM sample")) == [-1, 32767]
    assert rows(store, "SELECT t FROM sample WHERE item=32767") == [{"t": T0 + 2}], "filed at the time it was written again"


def test_a_row_the_program_cannot_name_is_left_out_and_an_unknown_state_reads_as_unknown(archive_db_factory):
    """A row written by another version, or damaged by hand, must not break the replay of everything else."""
    store = written(archive_db_factory(), group(1, [obs("ti", 1.0, T0 + 1, 0)], states=[seg_start(T0)]))
    with store.session() as conn:
        ti = conn.execute("SELECT no FROM channel WHERE point_id='ti'").fetchone()["no"]
        conn.execute("INSERT INTO sample SELECT t+1,9,bucket,run,seg,0,%s,9,0 FROM sample", (ti,))
        conn.execute("INSERT INTO sample SELECT t+2,8,bucket,run,seg,0,999,1,0 FROM sample WHERE value=1")
    values = [(x["value"], x["quality"]) for x in shown(replay([("primary", store)], T0, T0 + 10))]
    assert values == [(1.0, r.GOOD), (9.0, "UNKNOWN")]


@pytest.mark.parametrize("extreme", [False, True])
def test_the_view_says_each_row_in_words(archive_db_factory, extreme):
    """A technician reads `sample_view` without this program: point, source, state and the physical-limit mark."""
    store = written(
        archive_db_factory(),
        group(1, [obs("ti", 1.0, T0 + 1, 0, extreme=extreme)], [res("dt", None, T0 + 1, status=r.RESULT_NA,
              reason="INPUT_UNAVAILABLE")], [seg_start(T0)]),
    )  # fmt: skip
    view = rows(store, "SELECT point_id,source_id,state,beyond_limits,run_id FROM sample_view ORDER BY point_id")
    assert view == [
        {"point_id": "dt", "source_id": "ua", "state": "不可用：输入不可用", "beyond_limits": False, "run_id": "run"},
        {"point_id": "ti", "source_id": "ua", "state": "良好", "beyond_limits": extreme, "run_id": "run"},
    ]
