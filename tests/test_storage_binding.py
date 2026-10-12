from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest

from htsg.archive_router import ExternalBindingError
from htsg.archive_router import validate_external_binding as check_binding
from htsg.config import Settings, Storage


def validate_external_binding(config, external, primary, emergency):
    return check_binding(external, config, primary, emergency)


class Store:
    def __init__(self, path, name):
        self.row = {"path": path, "name": name}
        self._local = SimpleNamespace()
        self.connection = SimpleNamespace(execute=self.execute)
        self.queries = 0
        self.offline = False

    def execute(self, _):
        self.queries += 1
        return SimpleNamespace(fetchone=lambda: self.row)

    @contextmanager
    def session(self):
        if self.offline:
            raise psycopg.OperationalError("offline")
        yield self.connection


def setup(mode="development"):
    external_path = "D:/external" if mode == "development" else "E:/external"
    config = Settings(mode=mode, storage=Storage(external_enabled=True, external_path=external_path))
    external = Store(external_path, "external")
    primary = Store("D:/primary", "main")
    emergency = Store("D:/emergency" if mode == "development" else "C:/emergency", "main")
    return config, external, primary, emergency


def test_development_can_share_cluster_but_not_database():
    args = setup()
    args[1].row["path"] = args[2].row["path"]
    validate_external_binding(*args)
    args[1].connection = SimpleNamespace(execute=args[1].execute)
    args[1].row["name"] = "main"
    with pytest.raises(ExternalBindingError, match="distinct"):
        validate_external_binding(*args)


def test_declared_free_volume_cannot_authorize_database_on_another_drive():
    args = setup("production")
    args[1].row["path"] = "D:/actual"
    with pytest.raises(ExternalBindingError, match="configured volume"):
        validate_external_binding(*args)


def test_production_checks_actual_peer_volumes():
    args = setup("production")
    args[2].row["path"] = "E:/misconfigured-primary"
    with pytest.raises(ExternalBindingError, match="separate volume"):
        validate_external_binding(*args)


def test_offline_check_never_caches_success_and_can_recover():
    args = setup()
    args[2].offline = True
    with pytest.raises(psycopg.OperationalError):
        validate_external_binding(*args)
    assert not hasattr(args[1]._local, "binding_verified_connection")
    args[2].offline = False
    validate_external_binding(*args)


def test_cached_success_is_rechecked_on_connection_replacement():
    args = setup()
    validate_external_binding(*args)
    validate_external_binding(*args)
    assert args[1].queries == 1
    args[1].connection = SimpleNamespace(execute=args[1].execute)
    args[1].row["path"] = "C:/wrong-after-reconnect"
    with pytest.raises(ExternalBindingError):
        validate_external_binding(*args)
