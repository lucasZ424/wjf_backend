"""The one-click install (修复轮15, 用户 11-01): a site engineer double-clicks one file.  What matters is that a second run
after a power cut or a closed window picks up where the first stopped, that nothing installed or configured is ever
overwritten, and that a step that cannot go on says so instead of half-installing."""

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "release"))
sys.path.insert(0, str(ROOT / "tools" / "pointsheet"))

import install  # noqa: E402
from install import Installer, Stop, verify  # noqa: E402


def make_package(tmp_path, commit="abc123"):
    package = tmp_path / "HTSG-1.0.0-abc123"
    (package / "runtime").mkdir(parents=True)
    (package / "runtime" / "python.exe").write_bytes(b"stand-in")
    (package / "config").mkdir()
    (package / "config" / "production.base.json").write_text("{}", encoding="utf-8")
    (package / "VERSION.json").write_text(json.dumps({"version": "1.0.0", "commit": commit}), encoding="utf-8")
    installers = package / "installers"
    (installers / "vc").mkdir(parents=True)
    (installers / "vc" / "vcruntime140.dll").write_bytes(b"vc")
    with zipfile.ZipFile(installers / "postgresql-18.6-windows-x64-binaries.zip", "w") as z:
        z.writestr("pgsql/bin/postgres.exe", b"pg")
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(package).as_posix()}"
             for p in sorted(package.rglob("*")) if p.is_file()]  # fmt: skip
    (package / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return package


class Runs:
    """Stands in for the program's own steps (configuration check, deploy_databases): records them, fails when told."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, args, cwd):
        self.calls.append(args[1:])
        return subprocess.CompletedProcess(args, 1 if self.fail and self.fail in args else 0)


def installer(tmp_path, package, answers=(), run=None, **kw):
    answers = list(answers)
    kw.setdefault("drives", lambda: [("D:", 900 * 1024**3), ("C:", 100 * 1024**3), ("E:", 400 * 1024**3)])
    return Installer(tmp_path / "HTSG", package=package, ask=lambda _: answers.pop(0), run=run or Runs(),
                     reachable=lambda: True, browser=False, **kw)  # fmt: skip


@pytest.fixture(autouse=True)
def no_log(monkeypatch):
    monkeypatch.setattr(Installer, "log_path", None)


def test_a_package_with_a_changed_or_missing_file_is_not_installed(tmp_path):
    package = make_package(tmp_path)
    assert verify(package) > 0
    (package / "config" / "production.base.json").write_text('{"tampered": 1}', encoding="utf-8")
    with pytest.raises(Stop, match="重新拷贝"):
        verify(package)


def test_a_full_run_installs_in_order_and_a_second_run_redoes_nothing(tmp_path):
    package = make_package(tmp_path)
    (package.parent / "production.json").write_text("{}", encoding="utf-8")  # beside the package, as the guide says
    runs = Runs()
    installer(tmp_path, package, answers=["", ""], run=runs).install()
    app = tmp_path / "HTSG" / "app"
    assert (app / "runtime" / "python.exe").is_file() and (app / "config" / "production.json").is_file()
    assert (tmp_path / "HTSG" / "pgsql" / "bin" / "postgres.exe").read_bytes() == b"pg"
    assert (tmp_path / "HTSG" / "pgsql" / "bin" / "vcruntime140.dll").is_file(), "PostgreSQL needs it on a clean Windows"
    deploy = json.loads((app / ".local" / "deploy.json").read_text(encoding="utf-8"))
    assert deploy["volumes"] == {"primary": "D:", "emergency": "E:"}, "defaults: the most free space, then the next disk"
    assert [c[-1] for c in runs.calls] == ["config/production.json", "plan", "install", "register", "status"]
    # the second run: the same program, configuration and deployment file are kept as they are; nothing is asked
    (app / "config" / "production.json").write_text('{"site": "edited"}', encoding="utf-8")
    again = Runs()
    installer(tmp_path, package, run=again).install()
    assert (app / "config" / "production.json").read_text(encoding="utf-8") == '{"site": "edited"}'
    assert [c[-1] for c in again.calls] == ["config/production.json", "plan", "install", "register", "status"]


def test_an_interrupted_copy_is_redone_and_another_installed_version_is_never_overwritten(tmp_path):
    package = make_package(tmp_path)
    partial = tmp_path / "HTSG" / ("app" + install.PARTIAL)
    partial.mkdir(parents=True)
    (partial / "half-copied").write_text("x", encoding="utf-8")
    installer(tmp_path, package).program()
    assert not partial.exists() and not (tmp_path / "HTSG" / "app" / "half-copied").exists()
    newer = make_package(tmp_path / "newer", commit="def456")
    with pytest.raises(Stop, match="第7节"):
        installer(tmp_path, newer).program()
    assert json.loads((tmp_path / "HTSG" / "app" / "VERSION.json").read_text(encoding="utf-8"))["commit"] == "abc123"


def test_without_a_configuration_an_unattended_run_stops_and_an_attended_one_asks(tmp_path):
    package = make_package(tmp_path)
    one = installer(tmp_path, package, unattended=True)
    one.program()
    with pytest.raises(Stop, match="--config"):
        one.configuration()
    dragged = tmp_path / "site" / "production.json"
    dragged.parent.mkdir()
    dragged.write_text('{"from": "point sheet"}', encoding="utf-8")
    asked = installer(tmp_path, package, answers=[str(tmp_path / "missing.json"), f'"{dragged}"'])
    asked.configuration()  # a wrong path is asked again; the quotes Windows adds to a dragged file are taken off
    assert (tmp_path / "HTSG" / "app" / "config" / "production.json").read_text(encoding="utf-8") == '{"from": "point sheet"}'


def test_an_install_folder_postgresql_cannot_use_is_refused_before_anything_is_copied(tmp_path):
    package = make_package(tmp_path)
    chinese = Installer(tmp_path / "监测", package=package, ask=input, run=Runs(), reachable=lambda: True, browser=False)
    with pytest.raises(Stop, match="英文字母"):
        chinese.install()
    assert not (tmp_path / "监测").exists()


def test_the_two_databases_never_share_a_disk(tmp_path):
    package = make_package(tmp_path)
    one = installer(tmp_path, package, primary="D:", emergency="d:")
    one.program()
    with pytest.raises(Stop, match="同一块盘"):
        one.volumes()
    assert not (tmp_path / "HTSG" / "app" / ".local" / "deploy.json").exists()


def test_a_failing_step_stops_the_install_and_says_how_to_go_on(tmp_path):
    package = make_package(tmp_path)
    (package.parent / "production.json").write_text("{}", encoding="utf-8")
    runs = Runs(fail="register")
    with pytest.raises(Stop, match="重新双击"):
        installer(tmp_path, package, answers=["E:", "D:"], run=runs).install()
    assert [c[-1] for c in runs.calls][-1] == "register", "nothing after the step that failed"


def test_the_point_sheet_launcher_never_overwrites_a_sheet_and_keeps_the_old_configuration_on_a_bad_sheet(tmp_path, monkeypatch):
    import launch

    monkeypatch.setattr(launch, "SHEET", tmp_path / "点表.xlsx")
    monkeypatch.setattr(launch, "OUT", tmp_path / "production.json")
    launch.SHEET.write_bytes(b"filled in")
    with pytest.raises(SystemExit, match="不覆盖"):
        launch.template()
    assert launch.SHEET.read_bytes() == b"filled in"
    launch.OUT.write_text('{"old": 1}', encoding="utf-8")
    monkeypatch.setattr(launch.pointsheet, "main", lambda argv: 1)  # the sheet has errors
    with pytest.raises(SystemExit, match="没有动"):
        launch.convert(str(launch.SHEET))
    assert launch.OUT.read_text(encoding="utf-8") == '{"old": 1}'
