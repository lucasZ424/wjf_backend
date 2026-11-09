"""重装：把装好的 HTSG 连同数据库实例和全部数据彻底清掉，再用本发布包重新安装（用户 2026-11-07）。双击发布包里的“重装.cmd”即运行本程序。

    runtime\\python.exe setup\\reinstall.py --root E:\\HTSG      装在别的根目录时

依次：请你把这次要用的配置文件的完整路径粘贴进窗口（管理员窗口拖不进文件；直接回车表示只清除、不安装）→ 校验配置，不能用就什么都不动 → 列出要清除的 → 输入“清除全部数据”才动手 →
删开机任务、杀后端与看守程序、停并删两个数据库服务 → 删两块盘上的 HTSG 目录（数据库实例、全部数据、程序、配置、账户、日志，里面有别的东西也一起删，不留备份）→
逐项核对都已不在 → 用同样的两块盘和这份配置重新安装。

配置总是问：发布包旁边留着的旧 production.json 不算数，否则安装程序会悄悄用上它。
不能从要删的目录里运行（运行中的程序删不掉自己），要用目录外的发布包。
可以重复运行：动手前把清单存在本目录的 .local\\reinstall-plan.json，程序目录没了、中途出错或断电后再运行，照这份清单接着清。
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import ntpath
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent  # <发布包>\setup
PACKAGE = HERE.parent
PLAN = HERE / ".local" / "reinstall-plan.json"
TASK = "HTSG-Backend"
ROLES = ("primary", "emergency")
BACKEND_PORT = 8000
CONFIRM = "清除全部数据"


class Stop(Exception):
    """Cannot go on, and nothing was touched: the message says why."""


def say(text: str = ""):
    print(text, flush=True)


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def deploy_module(app: Path):
    """The installed program's own deployment script: its paths and service names are the ones the install used."""
    spec = importlib.util.spec_from_file_location("htsg_deploy", app / "scripts" / "deploy_databases.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look themselves up there
    spec.loader.exec_module(module)
    return module


def make_plan(root: Path, run) -> dict:
    """What there is to remove, read from the installation."""
    app = root / "app"
    deploy_file = app / ".local" / "deploy.json"
    if not deploy_file.is_file():
        if any(run(["sc", "query", f"HTSG-PostgreSQL-{name}"], capture_output=True, text=True).returncode == 0 for name in ROLES):
            raise Stop(f"数据库服务还在，但 {deploy_file} 不在了，不知道数据目录在哪，什么都没动。把这个文件恢复回来再运行，或者人工处理。")
        if not root.exists() and run(["schtasks", "/Query", "/TN", TASK], capture_output=True).returncode:
            raise Stop(f"{root} 里没有装 HTSG，或者已经清过，没有东西可清，什么都没动。要安装就双击“安装.cmd”。")
        return {"root": str(root), "roles": [], "volumes": {}}  # stopped before the disks were chosen: there is no database yet
    try:
        deploy = deploy_module(app)
        inp = deploy.read_input(deploy_file)
        roles = []
        for name in deploy.ROLES:
            if name in inp["volumes"]:
                folder = ntpath.join(deploy.normalize(inp["volumes"][name]), inp["folder"], name)
                roles.append({"name": name, "parent": ntpath.dirname(folder), "service": f"HTSG-PostgreSQL-{name}", "port": inp["ports"][name]})
    except (OSError, ValueError, SystemExit) as error:  # read_input says what is wrong with the file by SystemExit
        raise Stop(f"读不出部署文件 {deploy_file}，什么都没动：{error}") from None
    return {"root": str(root), "roles": roles, "volumes": inp["volumes"]}


def load_or_make_plan(root: Path, run) -> dict:
    """The installation says best where its data is.  Once its program folder is going or gone, the plan saved before the first
    removal does."""
    app = root / "app"
    if PLAN.is_file() and not ((app / ".local" / "deploy.json").is_file() and (app / "scripts" / "deploy_databases.py").is_file()):
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        if Path(plan["root"]) == root:
            return plan
    return make_plan(root, run)


def new_config(ask) -> Path | None:
    """The configuration to install, checked before anything is removed; None when only removing.  The base and the template pass
    validation with no point at all: an installation on one of them would acquire nothing."""
    from htsg.config import Settings

    answer = ask("把这次要用的配置文件（production.json）的完整路径粘贴到本窗口，然后按回车；直接回车表示只清除、不安装：").strip().strip('"')
    if not answer:
        return None
    path = Path(answer)
    try:
        settings = Settings.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as error:
        raise Stop(f"配置文件不能用，什么都没动：{' '.join(str(error).splitlines()[:3])[:300]}") from None
    enabled = {s.id for s in settings.sources if s.enabled}
    if settings.mode != "production" or not any(p.source in enabled for p in settings.points):
        raise Stop("这不是能用的现场配置（不是正式模式，或者没有接入任何点），什么都没动。文件选对了吗？")
    return path


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def remove_tree(path: Path):
    def writable(func, name, _):  # files PostgreSQL leaves read-only
        os.chmod(name, stat.S_IWRITE)
        func(name)

    for attempt in range(5):  # a process just ended, or a window open on the folder, lets go a moment later
        try:
            shutil.rmtree(path, onexc=writable)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(2)


class Cleaner:
    def __init__(self, plan: dict, run=subprocess.run):
        self.plan, self.run = plan, run
        self.root = Path(plan["root"])

    def folders(self) -> list[Path]:
        return [Path(p) for p in dict.fromkeys([str(self.root), *(r["parent"] for r in self.plan["roles"])])]

    def items(self) -> list[str]:
        out = [f"开机任务 {TASK}", f"后端与看守程序（{self.root / 'app'} 下运行的进程）"]
        out += [f"数据库服务 {r['service']}" for r in self.plan["roles"]]
        out += [f"目录 {folder}（数据库实例、全部历史数据、程序、配置、账户、日志，里面的东西全部删除）" for folder in self.folders()]
        return out

    def sc(self, *args):
        return self.run(["sc", *args], capture_output=True, text=True)

    def stop_backend(self):
        self.run(["schtasks", "/End", "/TN", TASK], capture_output=True)
        self.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True)
        self.kill_under(self.root / "app")  # the databases are stopped as services next

    def kill_under(self, folder: Path):
        """Every process whose program lies under `folder`, except this one.  In rounds: a postmaster still alive at once starts again
        a worker killed before it, and that one was in no list.  By the command line too, for our own programs only: the path of such
        an orphan cannot always be read, and PostgreSQL writes its own path with forward slashes."""
        slash = str(folder).replace("\\", "/")
        script = (f"foreach ($round in 1..5) {{ $found = Get-CimInstance Win32_Process | Where-Object {{ "
                  f"$_.ProcessId -notin @({os.getpid()}, $PID) -and ($_.ExecutablePath -like '{folder}\\*' -or "
                  f"($_.Name -in @('postgres.exe', 'pg_ctl.exe', 'python.exe') -and "
                  f"($_.CommandLine -like '*{folder}\\*' -or $_.CommandLine -like '*{slash}/*'))) }}; "
                  f"if (-not $found) {{ break }}; "
                  f"$found | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}; "
                  f"Start-Sleep -Milliseconds 500 }}")  # fmt: skip
        self.run(["powershell", "-NoProfile", "-Command", script], capture_output=True)

    def remove_services(self):
        for role in self.plan["roles"]:
            if self.sc("query", role["service"]).returncode:
                continue
            self.sc("stop", role["service"])
            deadline = time.monotonic() + 60
            while "STOPPED" not in self.sc("query", role["service"]).stdout and time.monotonic() < deadline:
                time.sleep(1)
            self.sc("delete", role["service"])
        self.kill_under(self.root)  # anything left (a cluster started by hand): only now, or a service would restart it

    def check(self) -> list[str]:
        left = []
        ports = [BACKEND_PORT, *(r["port"] for r in self.plan["roles"])]
        left += [f"端口 {p} 仍在监听" for p in ports if listening(p)]
        left += [f"数据库服务 {r['service']} 仍在" for r in self.plan["roles"] if not self.sc("query", r["service"]).returncode]
        if self.run(["schtasks", "/Query", "/TN", TASK], capture_output=True).returncode == 0:
            left.append(f"开机任务 {TASK} 仍在")
        left += [f"{folder} 仍在" for folder in self.folders() if folder.exists()]
        return left

    def clean(self) -> list[str]:
        self.stop_backend()
        say("开机任务、后端已停")
        self.remove_services()
        say("数据库服务已删")
        for folder in self.folders():
            remove_tree(folder)
        say("目录已删")
        return self.check()


def main(argv=None, ask=input, run=subprocess.run, admin=is_admin) -> int:
    parser = argparse.ArgumentParser(description="重装：彻底清除已安装的 HTSG，再用本发布包重新安装")
    parser.add_argument("--root", type=Path, default=Path("D:/HTSG"), help="安装根目录，默认 D:\\HTSG")
    root = parser.parse_args(argv).root.resolve()
    if root == Path(root.anchor) or PACKAGE.is_relative_to(root):
        say(f"不动手：本程序在 {root} 里面（或者这是整块盘），运行中的程序删不掉自己。用安装目录外的发布包（比如新版本发布包）里的“重装.cmd”。")
        return 1
    if not admin():
        say("需要管理员权限：双击“重装.cmd”（会弹出授权），或在以管理员身份运行的 PowerShell 里运行本程序。")
        return 2
    try:
        plan = load_or_make_plan(root, run)
        cleaner = Cleaner(plan, run)
        if any(PACKAGE.is_relative_to(folder) for folder in cleaner.folders()):
            raise Stop(f"本程序在要删的目录里，运行中的程序删不掉自己，什么都没动。把发布包放到 {'、'.join(map(str, cleaner.folders()))} 以外。")
        config = new_config(ask)
        say("\n将清除（不留备份）：")
        for item in cleaner.items():
            say(f"  - {item}")
        say(f"然后用 {config} 重新安装。" if config else "只清除，不安装。")
        answer = ask(f"这些删除不能撤销，历史数据找不回来。确认请输入“{CONFIRM}”后回车，其他任意输入取消：").strip()
    except Stop as stop:
        say(f"不动手：{stop}")
        return 1
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != CONFIRM:
        say("已取消，什么都没动。")
        return 1
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        left = cleaner.clean()
    except OSError as error:
        say(f"\n没有清完：{error}\n关掉占用这些文件的程序后，再双击一次，会接着清。")
        return 1
    except KeyboardInterrupt:
        say("\n已中断。再双击一次，会接着清。")
        return 1
    if left:
        say("\n还有没清掉的（处理后可以再运行一次）：")
        for item in left:
            say(f"  - {item}")
        return 1
    PLAN.unlink(missing_ok=True)
    say("\n清除完成：程序、数据库实例与服务、开机任务、全部数据都已去掉，相关端口已释放。")
    if not config:
        say("要安装时双击“安装.cmd”。")
        return 0
    say("开始安装……\n")
    command = [sys.executable, str(HERE / "install.py"), "--root", str(root), "--config", str(config)]
    if set(plan["volumes"]) == set(ROLES):  # the same two disks
        command += ["--primary", plan["volumes"]["primary"], "--emergency", plan["volumes"]["emergency"]]
    return run(command).returncode


if __name__ == "__main__":
    sys.exit(main())
