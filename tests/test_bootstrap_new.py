"""A new pair of development databases at the current archive generation (修复轮09, 终版 §13.6 结构版本).

Why it matters: a new archive generation is never laid over an old database - it goes into NEW databases, and the
connection file is switched only after they have been checked.  So the new pair must not touch the databases the
development backend uses now, nor their connection file, and a name that already exists is refused instead of being
taken for a new one.
"""

import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from htsg.archive import SCHEMA_GENERATION
from scripts import bootstrap_db

CREDENTIALS = Path(__file__).resolve().parents[1] / ".local/database.json"


def drop(conf, name):
    for role in ("primary", "emergency"):
        with psycopg.connect(make_conninfo(conf[role], dbname="postgres"), autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_a_new_pair_is_built_beside_the_current_one_and_written_to_its_own_connection_file(tmp_path):
    conf = json.loads(CREDENTIALS.read_text())
    before = CREDENTIALS.read_bytes()
    name = "htsg_test_" + uuid4().hex
    out = tmp_path / f"database-{name}.json"
    try:
        bootstrap_db.new_databases(conf, name, out)
        written = json.loads(out.read_text())
        assert set(written) == {"primary", "emergency", "password"}
        for role in ("primary", "emergency"):
            assert conninfo_to_dict(written[role])["dbname"] == name
            assert conninfo_to_dict(written[role])["port"] == conninfo_to_dict(conf[role])["port"], "the same cluster"
            with psycopg.connect(written[role]) as conn:
                row = conn.execute("SELECT value FROM htsg_meta WHERE key='schema_generation'").fetchone()
            assert row[0] == str(SCHEMA_GENERATION), "at the current generation"
        assert CREDENTIALS.read_bytes() == before, "the connection file in use is left as it is"
        with pytest.raises(SystemExit, match="already exists"):
            bootstrap_db.new_databases(conf, name, tmp_path / "again.json")
        assert not (tmp_path / "again.json").exists()
    finally:
        drop(conf, name)


@pytest.mark.parametrize("name", ["htsg", "postgres", "HTSG_new", "htsg-new", "x" * 64])
def test_the_name_in_use_and_names_that_are_not_plain_new_ones_are_refused(tmp_path, name):
    conf = json.loads(CREDENTIALS.read_text())
    with pytest.raises(SystemExit, match="name"):
        bootstrap_db.new_databases(conf, name, tmp_path / "out.json")
    assert not (tmp_path / "out.json").exists()


def test_a_database_of_another_generation_is_kept_and_the_way_to_a_new_pair_is_given(tmp_path, archive_db_factory):
    """Old databases are kept, their deletion is the user's: `build` must point at `new`, never tell anyone to drop one."""
    old = archive_db_factory(prepare=False)
    with old.session() as conn:
        conn.execute("CREATE TABLE htsg_meta (key text PRIMARY KEY, value text NOT NULL)")
        conn.execute("INSERT INTO htsg_meta VALUES ('schema_generation','5')")
    (tmp_path / "database.json").write_text(json.dumps({"primary": old.dsn, "emergency": old.dsn}), encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        bootstrap_db.build(tmp_path)
    said = str(refused.value)
    assert "new --name htsg_" in said and "Drop" not in said, said
