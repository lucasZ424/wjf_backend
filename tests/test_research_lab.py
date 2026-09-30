import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict

from htsg.cli import deployment_lock
from scripts import research_lab as lab


@pytest.fixture
def isolated_lab(tmp_path, monkeypatch):
    local = tmp_path / ".local/research"
    local.mkdir(parents=True)
    original = {"primary": "dbname=original port=55432", "emergency": "dbname=original port=55433"}
    (tmp_path / ".local/database.json").write_text(json.dumps(original))
    old = {role: f"dbname=htsg_research_{'a' * 32} port={port}" for role, port in
           (("primary", 55432), ("emergency", 55433))}
    (local / "database.json").write_text(json.dumps(old))
    # Existing account bytes and token files must survive switching the experiment.
    (local / "accounts.json").write_text(json.dumps({"admin": {}, "viewer": {}}))
    (local / "initial-credentials.json").write_text("existing-private-credentials")
    (local / "simulation-secret.txt").write_text("existing-secret")
    monkeypatch.setattr(lab, "ROOT", tmp_path)
    monkeypatch.setattr(lab, "LOCAL", local)
    state = SimpleNamespace(used=0, created=[], fail_second=False, cluster_path="D:/HTSG-dev/primary")

    class Connection:
        def __init__(self, dsn):
            self.dsn = conninfo_to_dict(dsn)
            self.row = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def execute(self, query):
            if str(query).startswith("Composed"):
                if state.fail_second and state.created:
                    raise RuntimeError("second creation failed")
                state.created.append(str(query))
            elif query == "SHOW data_directory":
                self.row = (state.cluster_path,)
            elif "current_database()" in query:
                self.row = (state.cluster_path, self.dsn["dbname"])
            elif "pg_total_relation_size" in query:
                self.row = (state.used,)
            else:
                pytest.fail(f"Unexpected query: {query}")
            return self

        def fetchone(self):
            return self.row

    monkeypatch.setattr(lab.psycopg, "connect", lambda dsn, **kwargs: Connection(dsn))
    config_path = Path(__file__).resolve().parents[1] / "config/research-sim.json"
    return local, state, config_path


def test_new_lab_archives_binding_and_preserves_credentials(isolated_lab):
    local, state, config_path = isolated_lab
    original = (local / "database.json").read_bytes()
    account_bytes = (local / "accounts.json").read_bytes()
    lab.prepare(config_path, new_lab=True)
    assert len(state.created) == 2  # Both instances get fresh databases; old pairs are never dropped.
    archives = list((local / "archives").glob("*/database.json"))
    assert len(archives) == 1 and archives[0].read_bytes() == original
    assert (local / "database.json").read_bytes() != original
    assert (local / "accounts.json").read_bytes() == account_bytes
    assert (local / "initial-credentials.json").read_text() == "existing-private-credentials"
    assert (local / "simulation-secret.txt").read_text() == "existing-secret"


def test_failure_preserves_active_binding(isolated_lab):
    local, state, config_path = isolated_lab
    original = (local / "database.json").read_bytes()
    state.fail_second = True
    with pytest.raises(RuntimeError, match="second creation"):
        lab.prepare(config_path, new_lab=True)
    assert (local / "database.json").read_bytes() == original


def test_preflight_checks_both_clusters_before_creating(isolated_lab):
    _, state, config_path = isolated_lab
    state.cluster_path = "C:/database"
    with pytest.raises(SystemExit, match="outside D"):
        lab.prepare(config_path, new_lab=True)
    assert state.created == []


def test_unresolved_account_changes_cannot_be_silently_discarded(isolated_lab):
    local, state, config_path = isolated_lab
    (local / "accounts.pending").write_text("unresolved")
    with pytest.raises(SystemExit, match="Reconcile pending"):
        lab.prepare(config_path, new_lab=True)
    assert state.created == []


@pytest.mark.parametrize("used, expected", [
    (5 * 1024**3, "budget exhausted"),
    (5 * 1024**3 - 8 * 1024**2, "below the estimate"),
])
def test_existing_usage_warns_without_replacing_or_raising_budget(isolated_lab, capsys, used, expected):
    local, state, config_path = isolated_lab
    state.used = used  # Summed over both database instances.
    original = (local / "database.json").read_bytes()
    config = lab.prepare(config_path)
    assert expected in capsys.readouterr().out
    assert config.storage.development_budget_bytes == 10 * 1024**3
    assert (local / "database.json").read_bytes() == original
    assert state.created == []


def test_launcher_keeps_same_deployment_lock_through_backend_start(isolated_lab, monkeypatch):
    _, _, config_path = isolated_lab
    lock_path = lab.ROOT / ".local/backend.lock"
    monkeypatch.setattr(lab.sys, "argv", ["research_lab", "run", "--config", str(config_path)])
    observed = []

    def run(*_):
        with pytest.raises(SystemExit, match="Another backend"):
            with deployment_lock(lock_path):
                pytest.fail("The running experiment must exclude new-lab")
        observed.append(True)

    monkeypatch.setattr(lab, "run", run)
    lab.main()
    assert observed == [True]
    with deployment_lock(lock_path):
        pass  # Normal shutdown releases the lock for the next explicit experiment.
