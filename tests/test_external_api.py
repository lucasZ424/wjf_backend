"""An available external archive must remain readable when both internal stores fail."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from htsg.api import create_app
from htsg.auth import Accounts
from htsg.runtime import Runtime


@pytest.mark.parametrize(
    "unavailable,status", [(["primary", "emergency"], 200), (["primary", "emergency", "external"], 503)]
)
def test_history_checks_all_configured_stores(settings, tmp_path, unavailable, status):
    accounts = Accounts(tmp_path / "accounts.json")
    token = accounts.create("viewer", "viewer")
    router = SimpleNamespace(stores=[("primary", None), ("emergency", None), ("external", None)])
    router.merged = lambda *args, **kwargs: {
        "items": [],
        "partial": True,
        "unavailable_instances": unavailable,
    }
    runtime = Runtime(settings, router)
    try:
        # No lifespan: this test isolates API availability semantics from database connectivity.
        client = TestClient(create_app(runtime, accounts))
        response = client.get("/api/history?start=1&end=2", headers={"Authorization": "Bearer " + token})
        assert response.status_code == status
        if status == 200:
            assert response.json()["partial"]
    finally:
        runtime.io.shutdown(wait=True)
        runtime.read_io.shutdown(wait=True)
        runtime.calc_io.shutdown(wait=True)


def test_last_archive_time_includes_external_after_restart(settings):
    router = SimpleNamespace(
        stores=[("primary", None), ("emergency", None), ("external", None)],
        mode="EXTERNAL",
        bytes_written=0,
        development_growth=0,
        health={
            "primary_metrics": {"last_frame_saved_at": 10},
            "external_metrics": {"last_frame_saved_at": 20},
        },
    )
    runtime = Runtime(settings, router)
    try:
        archive = runtime.status()["history_archive"]
        assert archive["last_success_at"] == 20
        assert not archive["suspended"], "External storage continues normal frame archival"
    finally:
        runtime.io.shutdown(wait=True)
        runtime.read_io.shutdown(wait=True)
        runtime.calc_io.shutdown(wait=True)
