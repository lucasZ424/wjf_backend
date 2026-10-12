"""A database as the frame-based generation left it.

`legacy_schema.sql` is that generation's schema, kept as a fixture so the new generation can be shown to recognise
such a database, refuse it, and leave it exactly as it was - without the old code that created it.
"""

from pathlib import Path

from psycopg.types.json import Jsonb


def make_legacy(store, config, accounts=None):
    """Lay down the frame-based schema in `store`'s database, with a configuration snapshot and optional accounts."""
    with store.session() as conn:
        conn.execute(Path(__file__).with_name("legacy_schema.sql").read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO config_snapshot(id,payload) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (config.config_id, Jsonb(config.model_dump())),
        )
        for name, user in (accounts or {}).items():
            conn.execute(
                "INSERT INTO account(username,role,salt,digest,active) VALUES(%s,%s,%s,%s,%s)",
                (name, user["role"], user["salt"], user["digest"], user["active"]),
            )
