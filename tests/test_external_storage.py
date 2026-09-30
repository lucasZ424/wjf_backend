from unittest.mock import MagicMock

import psycopg
import pytest

from htsg.storage import ExternalBindingError, Router


def make_router(settings):
    config = settings.model_copy(
        update={
            "storage": settings.storage.model_copy(
                update={
                    "external_enabled": True,
                    "external_path": "D:/test-external",
                }
            )
        }
    )
    stores = [MagicMock() for _ in range(3)]
    emergency = stores[1]
    state = {"mode": "PRIMARY", "selected": False}

    def mode(value=None):
        if value:
            state["mode"] = value
        return state["mode"]

    def selected(select=False):
        state["selected"] |= select
        return state["selected"]

    emergency.mode.side_effect = mode
    emergency.external_selected.side_effect = selected
    return Router(config, *stores), stores


def test_external_selection_survives_restart_and_primary_space_recovery(settings):
    router, stores = make_router(settings)
    assert router.route(0.949, 2 * 1024**3, 0.1) and router.mode == "PRIMARY"
    assert router.route(0.95, 2 * 1024**3, 0.1) and router.mode == "EXTERNAL"
    restarted = Router(router.config, *stores)
    assert restarted.route(0.1, 2 * 1024**3, 0.1) and restarted.mode == "EXTERNAL"


@pytest.mark.parametrize("external_used", [None, 0.95, 1.0])
def test_selected_external_missing_or_full_uses_emergency_never_primary(settings, external_used):
    router, stores = make_router(settings)
    router.route(0.95, 2 * 1024**3, 0.1)
    assert router.route(0.1, 2 * 1024**3, external_used) and router.mode == "EMERGENCY"
    assert not router.route(0.1, 100, external_used)
    assert router.route(0.1, 2 * 1024**3, 0.94) and router.mode == "EMERGENCY"
    assert router.route(0.1, 2 * 1024**3, 0.7) and router.mode == "EXTERNAL"


def test_external_database_failure_falls_back_and_recovery_resumes_external(settings):
    router, stores = make_router(settings)
    stores[2].probe_write.side_effect = psycopg.OperationalError("offline")
    router.route(0.95, 2 * 1024**3, 0.1)
    assert router.mode == "EMERGENCY"
    stores[2].probe_write.side_effect = None
    router.route(0.1, 2 * 1024**3, 0.1)
    assert router.mode == "EXTERNAL"


def test_external_binding_failure_never_probes_or_selects_it_for_writes(settings):
    router, stores = make_router(settings)
    stores[2].validate_external_binding.side_effect = ExternalBindingError("wrong volume")
    router.route(0.95, 2 * 1024**3, 0.1)
    assert router.mode == "EMERGENCY" and router.external_selected
    assert router.health["external"] == "ExternalBindingError"
    stores[2].probe_write.assert_not_called()


def test_disabling_external_writes_retains_queries_and_sticky_preference(settings):
    router, stores = make_router(settings)
    router.route(0.95, 2 * 1024**3, 0.1)
    config = router.config.model_copy(
        update={"storage": router.config.storage.model_copy(update={"external_enabled": False})}
    )
    restarted = Router(config, *stores)
    restarted.route(0.1, 2 * 1024**3, 0.1)
    assert restarted.mode == "EMERGENCY"
    assert len(restarted.stores) == 3
    for store in stores:
        store.query.return_value = []
    stores[2].query.side_effect = psycopg.OperationalError("offline")
    result = restarted.merged("events")
    assert result["partial"] and result["unavailable_instances"] == ["external"]


def test_paused_cleanup_never_deletes_but_keeps_collecting_metrics(settings, monkeypatch):
    router, stores = make_router(settings)
    monkeypatch.setattr("shutil.disk_usage", lambda _: MagicMock(total=100, free=1))
    for store in stores:
        store.metrics.return_value = {"measured_at": 0}
    router.maintain()
    assert router.health["cleaning"] is False
    for store in stores:
        store.cleanup.assert_not_called()
        store.metrics.assert_called_once()
