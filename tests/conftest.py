import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from htsg.config import Settings
from htsg.storage import Store


@pytest.fixture
def settings():
    return Settings.model_validate(
        {
            "sources": [{"id": "s", "kind": "simulator", "enabled": False}],
            "points": [
                {
                    "id": "x",
                    "source": "s",
                    "unit": "test",
                    "aggregation": "Mean",
                    "low": 0,
                    "high": 8,
                    "stale_after": 100,
                },
                {
                    "id": "y",
                    "source": "s",
                    "unit": "test",
                    "aggregation": "Last",
                    "high": 8,
                    "stale_after": 100,
                },
            ],
            "derived": [
                {"id": "delta", "inputs": ["x", "y"], "operation": "difference", "unit": "test"},
                {"id": "stress", "inputs": [], "operation": "unconfigured", "unit": "MPa"},
            ],
            "profiles": [
                {
                    "id": "p",
                    "trigger_points": ["x", "y"],
                    "source_points": ["x", "y"],
                    "pre": 10,
                    "recovery": 5,
                    "post": 2,
                }
            ],
        }
    )


@pytest.fixture
def databases(settings):
    credentials = json.loads(Path(".local/database.json").read_text())
    stores = []
    names = []
    for instance in ("primary", "emergency"):
        name = "htsg_test_" + uuid4().hex
        admin = make_conninfo(credentials[instance], dbname="postgres")
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        names.append((admin, name))
        store = Store(make_conninfo(credentials[instance], dbname=name))
        store.initialize(settings)
        stores.append(store)
    try:
        yield stores
    finally:
        for store in stores:
            store.close()
        for admin, name in names:
            assert name.startswith("htsg_test_") and len(name) == 42
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
