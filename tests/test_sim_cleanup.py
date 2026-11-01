"""The removal of a simulated installation (修复轮16, user 2026-11-01): after a demonstration or a test the computer must keep
nothing of it - no database instance, service, task, data or open port - so that simulated data can never be mistaken for the
site's.  What matters as much: it never touches a real installation, never deletes a folder it did not make, and a second run
after an interruption finishes the job."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "simulator"))
import cleanup  # noqa: E402

FIELD_SOURCES = [{"id": "mb_field", "kind": "modbus", "host": "127.0.0.1", "port": 15020},
                 {"id": "ua_field", "kind": "opcua", "endpoint": "opc.tcp://127.0.0.1:14840/htsg/"}]  # fmt: skip


class Commands:
    """sc, schtasks and powershell, recorded; every service and the task exist until deleted."""

    def __init__(self):
        self.calls, self.services, self.task = [], {"HTSG-PostgreSQL-primary", "HTSG-PostgreSQL-emergency"}, True

    def __call__(self, args, **_):
        self.calls.append(args)
        code, out = 0, ""
        if args[0] == "sc":
            name = args[2]
            if args[1] == "query":
                code, out = (0, "STATE : 1 STOPPED") if name in self.services else (1060, "")
            elif args[1] == "delete":
                self.services.discard(name)
        elif args[0] == "schtasks":
            if args[1] == "/Delete":
                self.task = False
            elif args[1] == "/Query":
                code = 0 if self.task else 1
        return subprocess.CompletedProcess(args, code, out, "")


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A simulated installation: the package, the app, the two data disks with their clusters."""
    package = tmp_path / "HTSG-SIM"
    (package / "sim").mkdir(parents=True)
    (package / "production.json").write_text(json.dumps({"sources": FIELD_SOURCES}), encoding="utf-8")
    monkeypatch.setattr(cleanup, "HERE", package / "sim")
    monkeypatch.setattr(cleanup, "PACKAGE", package)
    monkeypatch.setattr(cleanup, "PLAN", package / "sim" / ".local" / "cleanup-plan.json")
    monkeypatch.setattr(cleanup, "listening", lambda port: False)
    root = tmp_path / "HTSG"
    app = root / "app"
    (app / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "deploy_databases.py", app / "scripts")
    (app / "config").mkdir()
    (app / "config" / "production.json").write_text(json.dumps({"sources": FIELD_SOURCES, "storage": {}}), encoding="utf-8")
    disks = {name: tmp_path / f"disk-{name}" for name in ("primary", "emergency")}
    for name, disk in disks.items():
        (disk / "HTSG" / name / "pgdata").mkdir(parents=True)
        (disk / "HTSG" / name / "pgdata" / "PG_VERSION").write_text("18")
    (app / ".local").mkdir()
    (app / ".local" / "deploy.json").write_text(json.dumps(
        {"volumes": {n: str(d) for n, d in disks.items()}, "pg_bin": str(root / "pgsql" / "bin"), "config": "config/production.json"}
    ), encoding="utf-8")  # fmt: skip
    (root / "pgsql" / "bin").mkdir(parents=True)
    (root / "install.log").write_text("log")
    (package / "sim" / ".local").mkdir()
    (package / "sim" / ".local" / "simulation-secret.txt").write_text("token")
    return {"root": root, "disks": disks, "package": package}


def test_everything_the_simulated_installation_made_is_removed_and_checked(site):
    commands = Commands()
    plan = cleanup.load_or_make_plan(site["root"])
    left = cleanup.Cleaner(plan, run=commands).clean()
    assert left == [], left
    assert not site["root"].exists(), "app, database software and the install log, then the empty root"
    assert not any((d / "HTSG").exists() for d in site["disks"].values()), "the data and the emptied HTSG folder"
    assert not (site["package"] / "sim" / ".local" / "simulation-secret.txt").exists()
    assert commands.services == set() and commands.task is False
    order = [" ".join(map(str, c[:2])) for c in commands.calls]
    first_service_delete = order.index("sc delete")
    kill_all = next(i for i, c in enumerate(commands.calls) if c[0] == "powershell" and f"'{site['root']}\\'" in c[-1])
    assert kill_all > first_service_delete, "a service set to restart is stopped as a service before anything is killed"


def test_a_real_installation_is_refused_and_nothing_is_touched(site):
    app_config = site["root"] / "app" / "config" / "production.json"
    real = [{"id": "ua_field", "kind": "opcua", "endpoint": "opc.tcp://192.168.1.20:4840/"}]
    app_config.write_text(json.dumps({"sources": real}), encoding="utf-8")
    with pytest.raises(cleanup.Stop, match="可能是真实现场"):
        cleanup.load_or_make_plan(site["root"])
    assert not cleanup.PLAN.exists() and (site["root"] / "app").exists()
    assert cleanup.main(["--root", str(site["root"]), "--yes"]) == 1


def test_a_data_folder_with_other_files_is_kept_and_reported(site):
    stranger = site["disks"]["emergency"] / "HTSG" / "emergency" / "someone-elses.txt"
    stranger.write_text("keep me")
    left = cleanup.Cleaner(cleanup.load_or_make_plan(site["root"]), run=Commands()).clean()
    assert stranger.read_text() == "keep me"
    assert any("本程序以外的文件" in item for item in left) and any("仍在" in item for item in left)


def test_a_second_run_after_the_program_folder_is_gone_finishes_from_the_saved_plan(site):
    plan = cleanup.load_or_make_plan(site["root"])
    assert cleanup.PLAN.is_file()
    shutil.rmtree(site["root"] / "app")  # an interrupted first run: the program is gone, the data is not
    again = cleanup.load_or_make_plan(site["root"])
    assert again == plan, "the saved plan still knows where the data is"
    assert cleanup.Cleaner(again, run=Commands()).clean() == []
    assert not any((d / "HTSG").exists() for d in site["disks"].values())


def test_a_dry_run_and_a_missing_confirmation_change_nothing(site):
    before = sorted(p for p in site["root"].parent.rglob("*") if ".local" not in p.parts or "cleanup-plan.json" not in p.name)
    assert cleanup.main(["--root", str(site["root"]), "--dry-run"]) == 0
    assert cleanup.main(["--root", str(site["root"])], ask=lambda _: "好") in (1, 2)  # cancelled, or not elevated: nothing done
    after = sorted(p for p in site["root"].parent.rglob("*") if ".local" not in p.parts or "cleanup-plan.json" not in p.name)
    assert after == before
