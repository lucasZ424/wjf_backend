"""换点表：把点表工具新生成的 production.json 换到安装好的程序里（验收表：用户 2026-11-07 定 1–6 行，2026-11-10 确认并加 7–9 行）。

Why it matters: the point-sheet tool writes the storage paths of its base, D: for the normal database and C: for the emergency
one, while the installation put them on the disks the operator chose.  A new table copied in by hand therefore points the
archive at disks that are not its own, and the backend cannot write (现场 rc9, 用户 11-10).  Changing the table is also the one
routine operation that stops a running system on purpose, so it must leave it as it found it whenever it cannot finish: the
hard rule, a year without a crash, comes before the new table.

The script is `scripts/change_config.py`, the program behind “换点表.cmd” (现场总结第 6、8.1 节).  These tests drive its
`change()` the way the double-click does, in a throw-away installation folder: a real deployment file, real configurations
from the point-sheet tool, real lock files held as the backend and the supervisor hold them.  Only what needs administrator
rights on a real machine is stood in for: the scheduled task that starts the backend, and the backend's /live answer.
"""

import importlib
import json
import threading
import time

import pytest
from test_pointsheet import BASE, add, blank, bridge, pointsheet, wall

from scripts import change_config as cc

DEPLOY = importlib.import_module(cc.read_input.__module__)  # the module the script itself uses, by whatever name it imported it
SUPERVISE = importlib.import_module(cc.held_elsewhere.__module__)
MORE = ("TE-001", "TE-002", "TE-003")  # a new table: one wall more than the installed one


def generated(tmp_path, name, walls=("TE-001", "TE-002")):
    """A configuration as the point-sheet tool makes it: the storage paths of its base (D: and C:), not of any installation."""
    folder = tmp_path / name
    folder.mkdir()
    path = blank(folder)
    for tag in walls:
        wall(path, tag)
    bridge(path, 1)
    add(path, "OPC UA点", 类别="机组负荷", 显示名称="LOAD", 数据源="ua_field", NodeId="ns=2;s=LOAD")
    return pointsheet.convert(BASE, path)[0]


class Backend:
    """A backend and its supervisor as the script sees them: two lock files held, given up once a STOP marker appears; started
    again by the scheduled task; answering /live once started."""

    def __init__(self, local, running=True):
        self.local, self.saw_stop, self.started, self.calls = local, False, False, []
        self.handles = [SUPERVISE.lock(local / "backend.lock"), SUPERVISE.lock(local / "supervisor.lock")] if running else []
        assert all(self.handles), "the locks are taken as the backend takes them"
        self.watching = running
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self):
        while self.watching:
            if (self.local / "STOP").exists():
                self.saw_stop = True
                self.release()
                return
            time.sleep(0.05)

    def release(self):
        for handle in self.handles:
            handle.close()
        self.handles = []

    def run(self, command, **kw):  # stands in for subprocess.run: only the scheduled task is ever started this way
        self.calls.append(command)
        self.started = True
        return type("Done", (), {"returncode": 0})()

    def reachable(self, url):  # a running backend answers /live, and so does one the task started again
        return self.running or self.started

    @property
    def running(self):
        return bool(self.handles)

    def close(self):
        self.watching = False
        self.release()


class Site:
    """An installed program folder: `.local/deploy.json` names the two disks chosen at installation, `config/production.json` is
    what the installation wrote, with those disks in its storage paths."""

    def __init__(self, tmp_path, monkeypatch, *, emergency_root=None, deployed=True):
        self.app = tmp_path / "app"
        self.local = self.app / ".local"
        (self.app / "config").mkdir(parents=True)
        self.local.mkdir()
        self.roots = {"primary": tmp_path / "disk_d", "emergency": emergency_root or tmp_path / "disk_e"}
        for role, root in self.roots.items():
            if emergency_root is None or role == "primary":
                (root / "HTSG" / role).mkdir(parents=True)
                (root / "HTSG" / role / "history.bin").write_bytes(b"the archive of " + role.encode())  # must survive
        monkeypatch.setattr(cc, "ROOT", self.app)
        monkeypatch.setattr(cc, "LOCAL", self.local)
        monkeypatch.setattr(DEPLOY, "ROOT", self.app)  # read_input finds the configuration relative to the program folder
        self.config = self.app / "config" / "production.json"
        deploy = {"volumes": {r: str(p) for r, p in self.roots.items()}, "pg_bin": str(tmp_path / "pgsql" / "bin"), "config": "config/production.json"}
        record = self.local / "deploy.json" if deployed else tmp_path / "elsewhere-deploy.json"
        record.write_text(json.dumps(deploy), encoding="utf-8")
        roles = DEPLOY.plan(DEPLOY.read_input(record))[0]
        if not deployed:
            record.unlink()  # the installation's record is not there
        # what the installation wrote: the tool's file with the chosen disks (scripts/deploy_databases.py `with_storage`)
        self.installed = DEPLOY.with_storage(generated(tmp_path, "installed"), roles)
        self.config.write_text(json.dumps(self.installed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.before = self.config.read_bytes()

    def offer(self, config, beside=True):
        """Put a new configuration where the operator would: beside the program (the script finds it), or elsewhere."""
        path = self.app / "production.json" if beside else self.app.parent / "new-production.json"
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def current(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def backups(self):
        return sorted(self.config.parent.glob("production.json.bak-*"))

    def untouched(self):
        return self.config.read_bytes() == self.before and self.backups() == []

    def archive_untouched(self):
        return all((root / "HTSG" / role / "history.bin").read_bytes() == b"the archive of " + role.encode()
                   for role, root in self.roots.items() if (root / "HTSG" / role).exists())  # fmt: skip


def change(backend, ask=None, start_wait=5.0, asked=None):
    """What the double-click runs.  `asked` collects the prompts the operator was shown."""
    asked = [] if asked is None else asked

    def answer(prompt):
        asked.append(prompt)
        return ask(prompt) if ask else ""

    cc.change(ask=answer, run=backend.run, reachable=backend.reachable, stop_wait=20, start_wait=start_wait)
    return asked


@pytest.fixture
def site(tmp_path, monkeypatch):
    return Site(tmp_path, monkeypatch)


@pytest.fixture
def backend(site):
    running = Backend(site.local)
    yield running
    running.close()


def without_storage(config):
    return {k: v for k, v in config.items() if k != "storage"}


def test_a_valid_new_table_replaces_the_old_one_keeps_it_and_restarts_the_backend(site, backend, tmp_path):
    """行 1：换文件并留底旧文件，后端正常重启，页面能打开，历史数据还在。"""
    new = generated(tmp_path, "new", walls=MORE)
    site.offer(new)
    change(backend)
    assert without_storage(site.current()) == without_storage(new), "the new table is in use"
    (backup,) = site.backups()
    assert backup.read_bytes() == site.before, "the old one is kept as it was"
    assert backend.saw_stop and not backend.running, "the backend was stopped the normal way, through the STOP marker"
    assert not (site.local / "STOP").exists(), "and the marker is gone, or nothing would start again"
    assert backend.calls == [["schtasks", "/Run", "/TN", DEPLOY.TASK]], "started again by its scheduled task"
    assert backend.started and backend.reachable(cc.LIVE), "answering"
    assert site.archive_untouched(), "the databases and their history are not touched"


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(lambda new: "{ not json", id="not-json"),
        pytest.param(lambda new: json.dumps({**new, "points": [*new["points"], {"id": "x", "source": "nowhere", "unit": "u"}]}), id="fails-validation"),
    ],
)
def test_a_new_table_that_does_not_pass_changes_nothing_and_the_backend_runs_on(site, backend, tmp_path, broken):
    """行 2：说明原因，什么都不动，后端继续运行。Checked before anything is stopped: a bad file must not cost a running system."""
    (site.app / "production.json").write_text(broken(generated(tmp_path, "new")), encoding="utf-8")
    with pytest.raises(cc.Stop) as stop:
        change(backend)
    assert "什么都没动" in str(stop.value) and "后端照常运行" in str(stop.value), "it says why and that nothing was touched"
    assert site.untouched()
    assert not backend.saw_stop and backend.running and backend.calls == [], "never stopped"


def test_without_a_new_table_it_asks_for_the_path_and_an_empty_answer_leaves_everything_as_it_was(site, backend):
    """行 3：提示粘贴路径；不给就退出，什么都不动。"""
    asked = []
    with pytest.raises(cc.Stop):
        change(backend, asked=asked)
    assert len(asked) == 1 and "粘贴" in asked[0], "an administrator window takes no file dropped on it: the path is pasted"
    assert site.untouched()
    assert not backend.saw_stop and backend.running and backend.calls == []
    assert not (site.local / "STOP").exists()


def test_a_path_pasted_in_is_used(site, backend, tmp_path):
    """行 3：给了路径就用它（粘贴时常带着引号）。"""
    new = generated(tmp_path, "new", walls=MORE)
    pasted = site.offer(new, beside=False)
    change(backend, ask=lambda prompt: f'"{pasted}"')
    assert without_storage(site.current()) == without_storage(new)


@pytest.mark.parametrize(
    ("kind", "content"),
    [("file", None), ("file", "made by the operator"), ("folder", None), ("folder", "made by the operator")],
)
def test_a_stop_marker_left_behind_file_or_folder_is_cleared_and_what_was_in_it_is_kept(site, tmp_path, kind, content):
    """行 4：都能识别并清掉，里面的内容改名留底。The site made one as a folder; a marker the script cannot clear keeps the system stopped."""
    marker = site.local / "STOP"
    if kind == "file":
        marker.write_text(content or "", encoding="utf-8")
    else:
        marker.mkdir()
        if content:
            (marker / "note.txt").write_text(content, encoding="utf-8")
    stopped = Backend(site.local, running=False)  # a STOP in place: the backend is not running
    try:
        site.offer(generated(tmp_path, "new", walls=MORE))
        change(stopped)
    finally:
        stopped.close()
    assert not marker.exists()
    kept = list(site.local.glob("stop-kept-*"))
    if content:
        (copy,) = kept
        text = copy.read_text(encoding="utf-8") if copy.is_file() else (copy / "note.txt").read_text(encoding="utf-8")
        assert text == content, "what someone put in it is kept"
    else:
        assert kept == [], "an empty marker is simply removed"
    assert stopped.calls == [["schtasks", "/Run", "/TN", DEPLOY.TASK]], "and the backend is started"


def test_a_table_from_the_tool_gets_the_disks_chosen_at_installation(site, backend, tmp_path):
    """行 5（用户 11-10）：改成安装时的盘，和安装时写进 production.json 的路径一致。The tool's base says D: and C:."""
    new = generated(tmp_path, "new", walls=MORE)
    assert (new["storage"]["primary_path"], new["storage"]["emergency_path"]) == ("D:/HTSG/primary", "C:/HTSG/emergency")
    site.offer(new)
    change(backend)
    now = site.current()
    for key in ("primary_path", "emergency_path"):
        assert now["storage"][key] == site.installed["storage"][key], key
    rest = lambda storage: {k: v for k, v in storage.items() if not k.endswith("_path")}  # noqa: E731
    assert rest(now["storage"]) == rest(new["storage"]), "only the two paths: the rest of the storage settings are the new table's"


def test_when_the_backend_does_not_answer_in_time_it_says_so_with_the_logs_and_the_old_table(site, backend, tmp_path):
    """行 6：明确说失败，给出日志和旧配置的位置。A process that runs is not a page that opens."""
    site.offer(generated(tmp_path, "new", walls=MORE))
    backend.reachable = lambda url: False
    with pytest.raises(cc.Stop) as stop:
        change(backend, start_wait=0.1)
    said = str(stop.value)
    (backup,) = site.backups()
    assert "supervisor.log" in said and "backend.log" in said and str(backup) in said, said


def test_a_disk_that_cannot_be_checked_now_is_written_as_installed_and_nothing_is_refused_for_it(tmp_path, monkeypatch):
    """行 7（用户 11-10：不加额外检查，以免意外拒绝盘符）：照样按安装记录写路径。The disk may only be late, as after a power cut."""
    site = Site(tmp_path, monkeypatch, emergency_root=tmp_path / "not_ready")
    running = Backend(site.local)
    try:
        site.offer(generated(tmp_path, "new", walls=MORE))
        change(running)
    finally:
        running.close()
    assert site.current()["storage"]["emergency_path"] == site.installed["storage"]["emergency_path"]
    assert running.started


def test_the_same_table_again_with_the_backend_running_does_nothing(site, backend, tmp_path):
    """行 8：什么都不做。The tool's file of the same points differs only in its storage paths, which become the installed ones."""
    site.offer(generated(tmp_path, "same"))
    change(backend)
    assert site.untouched()
    assert not backend.saw_stop and backend.running and backend.calls == [], "no restart for nothing"


def test_outside_an_installed_program_folder_nothing_is_done(tmp_path, monkeypatch):
    """行 9：说明不是安装好的程序目录，什么都不动。Without the installation's record of its disks there is nothing to take them from."""
    site = Site(tmp_path, monkeypatch, deployed=False)
    running = Backend(site.local)
    asked = []
    try:
        site.offer(generated(tmp_path, "new", walls=MORE))
        with pytest.raises(cc.Stop) as stop:
            change(running, asked=asked)
        assert "deploy.json" in str(stop.value)
        assert site.untouched()
        assert not running.saw_stop and running.running and running.calls == []
    finally:
        running.close()
    assert asked == [], "it does not even ask for a file"
