"""Field deployment of the archive databases from volume names (scripts/deploy_databases.py).

Why it matters: on site only the disks are known - "the primary is D:, the emergency C:".  The script must turn
that into one cluster per role ON that disk and write the same folder into the configuration, so the disk the backend measures is
the disk the data is on.  It must refuse what would defeat the purpose (two roles on one disk, a missing disk) or destroy something
(a folder that holds other data, a connection file that names other databases), and a second run must change nothing.
"""

import json
import socket
import subprocess
from pathlib import Path

import psycopg
import pytest

from htsg.archive import SCHEMA_GENERATION
from htsg.auth import Accounts
from scripts import deploy_databases as deploy

ROOT = Path(__file__).resolve().parents[1]
PG_BIN = Path("D:/HTSG-dev/pgsql/bin")


@pytest.mark.parametrize(
    ("written", "expected"),
    [("D", "D:\\"), ("d:", "D:\\"), ("D:/", "D:\\"), ("F:\\", "F:\\"), (" e:/Data/ ", "E:\\Data"), ("C:/HTSG/../X", "C:\\X")],
)
def test_a_volume_may_be_written_as_the_site_writes_it(written, expected):
    assert deploy.normalize(written) == expected


@pytest.mark.parametrize("written", ["", "Data", "D:Data", "\\\\server\\share", "//server/share", "1:"])
def test_relative_network_and_malformed_paths_are_refused(written):
    with pytest.raises(ValueError):
        deploy.normalize(written)


def test_the_backend_task_runs_the_supervisor_as_system_at_boot_and_every_five_minutes_also_on_battery():
    """No logon and no stored password (SYSTEM), revived every five minutes without doubling, not stopped when a UPS reports a
    battery, at normal priority and without a time limit - the defaults of the task scheduler are the opposite for most of these."""
    import xml.etree.ElementTree as ET

    text = deploy.task_xml("C:/site/.venv/Scripts/python.exe", Path("D:/site/.local/deploy.json"))
    root = ET.fromstring(text.encode("utf-16"))
    t = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    value = lambda path: root.find(path, t).text  # noqa: E731
    assert value("t:Principals/t:Principal/t:UserId") == "S-1-5-18", "SYSTEM"
    assert root.find("t:Triggers/t:BootTrigger", t) is not None
    assert value("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval") == "PT5M"
    settings = {
        name: value(f"t:Settings/t:{name}")
        for name in ("MultipleInstancesPolicy", "DisallowStartIfOnBatteries", "StopIfGoingOnBatteries", "ExecutionTimeLimit", "Priority")
    }
    assert settings == {
        "MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false", "StopIfGoingOnBatteries": "false",
        "ExecutionTimeLimit": "PT0S", "Priority": "4",
    }  # fmt: skip
    assert value("t:Actions/t:Exec/t:Command") == "C:/site/.venv/Scripts/python.exe"
    arguments = value("t:Actions/t:Exec/t:Arguments")
    assert "supervise.py" in arguments and str(Path("D:/site/.local/deploy.json")) in arguments
    assert value("t:Actions/t:Exec/t:WorkingDirectory") == str(ROOT)


def site(tmp_path, volumes, **extra):
    config = tmp_path / "production.json"
    config.write_text((ROOT / "config/production.template.json").read_text(encoding="utf-8"), encoding="utf-8")
    raw = {"volumes": volumes, "pg_bin": str(PG_BIN), "config": str(config), "database_file": str(tmp_path / "database.json"), **extra}
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return deploy.read_input(path)


def free_ports(n):
    sockets = [socket.socket() for _ in range(n)]
    for s in sockets:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


def distinct_volumes(path):
    """Test volumes: every top folder under the test directory counts as a disk of its own."""
    real = deploy.volume(path)
    return {**real, "id": f"test-volume:{Path(path).name}", "mount": path}


def test_two_roles_on_one_disk_are_refused_because_the_emergency_database_would_fill_with_the_primary(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, _, problems, _ = deploy.plan(site(tmp_path, {"primary": str(tmp_path / "a"), "emergency": str(tmp_path / "b")}))
    assert any("在同一块盘上" in p for p in problems), problems


def test_a_disk_that_is_not_there_a_folder_with_other_data_and_a_foreign_connection_file_are_refused(tmp_path):
    (tmp_path / "b" / "HTSG" / "emergency" / "pgdata").mkdir(parents=True)
    (tmp_path / "b" / "HTSG" / "emergency" / "pgdata" / "someone-elses.txt").write_text("keep me")
    (tmp_path / "database.json").write_text(json.dumps({"primary": "postgresql://other@127.0.0.1:5432/x"}))
    volumes = {"primary": "Q:", "emergency": str(tmp_path / "b")}
    _, _, problems, _ = deploy.plan(site(tmp_path, volumes), distinct_volumes)
    text = "\n".join(problems)
    assert "Q:\\ 不存在" in text, text
    assert "不是空的，里面也不是本程序的数据库实例" in text, text
    assert "指向别的数据库" in text, text
    assert (tmp_path / "b" / "HTSG" / "emergency" / "pgdata" / "someone-elses.txt").read_text() == "keep me"


def test_a_problem_stops_install_before_anything_is_created(tmp_path):
    (tmp_path / "a").mkdir()
    inp = site(tmp_path, {"primary": str(tmp_path / "a"), "emergency": "Q:"})
    with pytest.raises(SystemExit):
        deploy.install(inp, distinct_volumes)
    assert not (tmp_path / "a" / "HTSG").exists() and not (tmp_path / "database.json").exists()
    assert json.loads((tmp_path / "production.json").read_text(encoding="utf-8"))["storage"]["primary_path"] == "D:/HTSG/primary"


@pytest.mark.skipif(not (PG_BIN / "initdb.exe").exists(), reason="needs the PostgreSQL 18 binaries of the development machine")
def test_install_builds_one_cluster_per_disk_writes_the_same_folders_into_the_config_and_a_second_run_changes_nothing(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    primary_port, emergency_port = free_ports(2)
    inp = site(
        tmp_path, {"primary": str(tmp_path / "a"), "emergency": str(tmp_path / "b")},
        ports={"primary": primary_port, "emergency": emergency_port},
    )  # fmt: skip
    folders = {"primary": tmp_path / "a" / "HTSG" / "primary", "emergency": tmp_path / "b" / "HTSG" / "emergency"}
    try:
        deploy.install(inp, distinct_volumes)
        storage = json.loads((tmp_path / "production.json").read_text(encoding="utf-8"))["storage"]
        for role, folder in folders.items():
            assert storage[f"{role}_path"] == str(folder).replace("\\", "/"), "the config names the folder the cluster is in"
            assert (folder / "pgdata" / "PG_VERSION").exists()
        assert len(list(tmp_path.glob("production.json.bak-*"))) == 1, "the previous configuration is kept"
        connections = json.loads((tmp_path / "database.json").read_text(encoding="utf-8"))
        assert set(connections) == {"primary", "emergency", "password"}
        handed = json.loads((tmp_path / "initial-credentials.json").read_text(encoding="utf-8"))
        local = Accounts(tmp_path / "accounts.json")
        assert list(local.users) == ["admin"] and local.verify(f"{handed['username']}:{handed['password']}")["role"] == "admin", (
            "the deployment makes the only account, the administrator, next to the connection file (修复轮02)"
        )
        for role, folder in folders.items():  # install stops what it started: the services take over; here pg_ctl does
            subprocess.run([str(PG_BIN / "pg_ctl.exe"), "-D", str(folder / "pgdata"), "-l", str(folder / "t.log"), "-w", "start"],
                           check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
            with psycopg.connect(connections[role], connect_timeout=10) as conn:
                generation = conn.execute("SELECT value FROM htsg_meta WHERE key='schema_generation'").fetchone()[0]
                data_dir = conn.execute("SHOW data_directory").fetchone()[0]
                accounts = conn.execute("SELECT count(*) FROM account").fetchone()[0]
                records = conn.execute("SELECT action, target FROM management_log").fetchall()
            assert generation == str(SCHEMA_GENERATION) and Path(data_dir) == folder / "pgdata"
            assert accounts == 0, "the account file is the only copy of the accounts (2026-10-30)"
            assert records == ([("CREATE_ACCOUNT", "admin")] if role == "primary" else []), "the administrator's creation is on record"
        before = {p: p.read_bytes() for p in (tmp_path / "database.json", tmp_path / "production.json", tmp_path / "accounts.json", tmp_path / "initial-credentials.json", tmp_path / "admin-fallback.json")}
        deploy.install(inp, distinct_volumes)  # clusters running, files in place: nothing to do
        assert {p: p.read_bytes() for p in before} == before, "a second run rewrites nothing"
        assert len(list(tmp_path.glob("production.json.bak-*"))) == 1
        _, facts, problems, _ = deploy.plan(inp, distinct_volumes)
        assert problems == [] and {f["state"] for f in facts.values()} == {"existing cluster"}
    finally:
        for folder in folders.values():
            if (folder / "pgdata" / "PG_VERSION").exists():
                subprocess.run([str(PG_BIN / "pg_ctl.exe"), "-D", str(folder / "pgdata"), "-w", "-m", "fast", "stop"],
                               capture_output=True)  # fmt: skip


def test_the_password_folder_keeps_the_access_initdb_needs_after_dropping_administrator_rights():
    """Rehearsal 2026-11-01: the one-click install runs elevated, and initdb drops its administrator rights before it reads
    the password file.  A folder from mkdtemp (Python 3.13) admits only SYSTEM, Administrators and its owner - the
    Administrators group when elevated - so the install stopped at the first cluster.  The folder must inherit the access of
    the temporary folder instead, which names the user."""
    import subprocess

    folder = deploy.password_folder()
    try:
        acl = subprocess.run(["icacls", str(folder)], capture_output=True).stdout.decode("utf-8", errors="replace")
        assert "(I)" in acl, f"inherited from the temporary folder, not a protected list of its own: {acl}"
    finally:
        folder.rmdir()
