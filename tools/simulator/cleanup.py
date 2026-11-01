"""清除模拟安装：把用模拟数据源包装起来的 HTSG 连同它产生的一切从这台电脑上去掉（修复轮16，用户 2026-11-01）。

    runtime\\python.exe sim\\cleanup.py                 列出要清除的东西，输入“清除”确认后执行（“3-清除模拟安装”运行的就是它）
    runtime\\python.exe sim\\cleanup.py --dry-run       只列出，什么都不动
    runtime\\python.exe sim\\cleanup.py --root E:\\HTSG  装在别的根目录时

清除：开机任务 HTSG-Backend；后端与看守程序（先放 STOP 让后端正常停下）；两个数据库服务；两块盘上的数据目录
（<盘>\\HTSG\\primary、emergency，只删本程序建的）；程序目录、数据库软件、安装日志；模拟现场进程与它的控制令牌。
最后逐项核对端口、服务、任务、目录都已不在。

只在装的是**模拟配置**时动手：已安装配置的数据源必须与本包 production.json 的完全一致，否则拒绝——真实现场的数据不能
用它删掉。可以重复运行：动手前把要清除的清单存在 sim\\.local\\cleanup-plan.json，程序目录已删、中途断电后再运行，
照这份清单接着清。本包文件夹本身不删（正在运行），清除完成后手动删掉即可。
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

HERE = Path(__file__).resolve().parent  # <包>\sim
PACKAGE = HERE.parent
PLAN = HERE / ".local" / "cleanup-plan.json"
TASK = "HTSG-Backend"
FIELD_PORTS = (14840, 14841, 15020)  # 模拟现场：OPC UA、控制接口、Modbus
BACKEND_PORT = 8000
CONFIRM = "清除"


class Stop(Exception):
    """Cannot go on: the message says why."""


def say(text: str = ""):
    print(text, flush=True)


def deploy_module(app: Path):
    """The installed program's own deployment script: its paths and service names are the ones the install used."""
    spec = importlib.util.spec_from_file_location("htsg_deploy", app / "scripts" / "deploy_databases.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look themselves up there
    spec.loader.exec_module(module)
    return module


def sources_of(config: dict) -> list:
    return sorted(json.dumps({k: s.get(k) for k in ("id", "kind", "host", "port", "endpoint")}, sort_keys=True)
                  for s in config.get("sources", []))  # fmt: skip


def make_plan(root: Path) -> dict:
    """What there is to remove, read from the installation - refused unless it was installed with this package's configuration."""
    app = root / "app"
    installed = app / "config" / "production.json"
    if not installed.is_file():
        raise Stop(f"{installed} 不存在：这里没有装 HTSG，或者已经清除过（没有存下的清单）。")
    ours = json.loads((PACKAGE / "production.json").read_text(encoding="utf-8"))
    there = json.loads(installed.read_text(encoding="utf-8-sig"))
    if sources_of(there) != sources_of(ours):
        raise Stop(f"{root} 装的不是本模拟包的配置（数据源不同）：这可能是真实现场，本工具不清除它的任何东西。")
    roles = []
    deploy_file = app / ".local" / "deploy.json"
    if deploy_file.is_file():
        deploy = deploy_module(app)
        inp = deploy.read_input(deploy_file)
        for name in deploy.ROLES:
            if name in inp["volumes"]:
                volume = deploy.normalize(inp["volumes"][name])
                folder = ntpath.join(volume, inp["folder"], name)
                roles.append({"name": name, "folder": folder, "parent": ntpath.dirname(folder), "service": f"HTSG-PostgreSQL-{name}",
                              "port": inp["ports"][name]})  # fmt: skip
    return {"root": str(root), "roles": roles}


def load_or_make_plan(root: Path) -> dict:
    if PLAN.is_file():
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        if Path(plan["root"]) == root:
            return plan
    plan = make_plan(root)
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ours(folder: Path) -> bool:
    """A data folder the deployment made: nothing in it but its cluster (pgdata) and its start log - or nothing at all."""
    return folder.is_dir() and {p.name for p in folder.iterdir()} <= {"pgdata", "deploy-start.log"}


def remove_tree(path: Path):
    def writable(func, name, _):  # files PostgreSQL leaves read-only
        os.chmod(name, stat.S_IWRITE)
        func(name)

    shutil.rmtree(path, onexc=writable)


class Cleaner:
    def __init__(self, plan: dict, run=subprocess.run):
        self.plan, self.run = plan, run
        self.root = Path(plan["root"])

    def items(self) -> list[str]:
        out = [f"开机任务 {TASK}", f"后端与看守程序（{self.root / 'app'} 下运行的进程）"]
        for role in self.plan["roles"]:
            out += [f"数据库服务 {role['service']}", f"数据目录 {role['folder']}（含数据库实例与全部模拟数据）"]
        out += [f"程序目录 {self.root / 'app'}", f"数据库软件 {self.root / 'pgsql'}", f"安装日志 {self.root / 'install.log'}",
                f"模拟现场进程与控制令牌 {HERE / '.local'}"]  # fmt: skip
        return out

    def sc(self, *args):
        return self.run(["sc", *args], capture_output=True, text=True)

    def stop_backend(self):
        local = self.root / "app" / ".local"
        if local.is_dir():
            (local / "STOP").write_text("", encoding="utf-8")
            say("已放 STOP，等后端正常停下（最多 90 秒）……")
            deadline = time.monotonic() + 90
            while listening(BACKEND_PORT) and time.monotonic() < deadline:
                time.sleep(2)
        self.run(["schtasks", "/End", "/TN", TASK], capture_output=True)
        self.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True)
        self.kill_under(self.root / "app")  # a backend that did not stop by itself; the databases are stopped as services next

    def kill_under(self, folder: Path):
        """Every process whose program lies under `folder`, except this one (it runs from the simulated-field package)."""
        script = (f"Get-Process | Where-Object {{ $_.Path -and $_.Path.StartsWith('{folder}\\', 'OrdinalIgnoreCase') "
                  f"-and $_.Id -ne {os.getpid()} }} | Stop-Process -Force")  # fmt: skip
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

    def remove_data(self) -> list[str]:
        kept = []
        for role in self.plan["roles"]:
            folder = Path(role["folder"])
            if folder.exists():
                if not ours(folder):
                    kept.append(f"{folder} 里有本程序以外的文件，没有删，请人工查看")
                    continue
                remove_tree(folder)
            parent = Path(role["parent"])
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        return kept

    def remove_program(self):
        for name in ("app", "pgsql", "app.partial", "pgsql.partial"):
            if (self.root / name).exists():
                remove_tree(self.root / name)
        (self.root / "install.log").unlink(missing_ok=True)
        if self.root.is_dir() and not any(self.root.iterdir()):
            self.root.rmdir()

    def remove_field(self):
        self.kill_under(PACKAGE)  # the simulated field runs on this package's runtime; this cleaner is spared
        token = HERE / ".local" / "simulation-secret.txt"
        token.unlink(missing_ok=True)

    def check(self) -> list[str]:
        left = []
        ports = [BACKEND_PORT, *FIELD_PORTS, *(r["port"] for r in self.plan["roles"])]
        left += [f"端口 {p} 仍在监听" for p in ports if listening(p)]
        left += [f"数据库服务 {r['service']} 仍在" for r in self.plan["roles"] if not self.sc("query", r["service"]).returncode]
        if self.run(["schtasks", "/Query", "/TN", TASK], capture_output=True).returncode == 0:
            left.append(f"开机任务 {TASK} 仍在")
        left += [f"{p} 仍在" for p in (self.root / "app", self.root / "pgsql") if p.exists()]
        left += [f"{r['folder']} 仍在" for r in self.plan["roles"] if Path(r["folder"]).exists()]
        return left

    def clean(self) -> list[str]:
        self.stop_backend()
        say("开机任务、后端已停")
        self.remove_services()
        say("数据库服务已删")
        kept = self.remove_data()
        say("数据目录已删")
        self.remove_program()
        say("程序目录、数据库软件、安装日志已删")
        self.remove_field()
        say("模拟现场已停，控制令牌已删")
        return kept + self.check()


def main(argv=None, ask=input) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path("D:/HTSG"), help="安装根目录，默认 D:\\HTSG")
    parser.add_argument("--dry-run", action="store_true", help="只列出要清除的东西，什么都不动")
    parser.add_argument("--yes", action="store_true", help="不要求输入“清除”确认")
    args = parser.parse_args(argv)
    try:
        plan = load_or_make_plan(args.root)
    except Stop as stop:
        say(f"不清除：{stop}")
        return 1
    cleaner = Cleaner(plan)
    say("将清除：")
    for item in cleaner.items():
        say(f"  - {item}")
    if args.dry_run:
        say("（只列出，什么都没动）")
        return 0
    if not ctypes.windll.shell32.IsUserAnAdmin():
        say("需要管理员权限：双击“3-清除模拟安装”（会弹出授权），或在以管理员身份运行的 PowerShell 里运行。")
        return 2
    if not args.yes and ask(f"这些删除不能撤销。确认请输入“{CONFIRM}”后回车，其他任意输入取消：").strip() != CONFIRM:
        say("已取消，什么都没动。")
        return 1
    left = cleaner.clean()
    if left:
        say("\n还有没清掉的（可以再运行一次；仍不行请人工处理）：")
        for item in left:
            say(f"  - {item}")
        return 1
    PLAN.unlink(missing_ok=True)
    say(f"\n清除完成：模拟安装、数据、数据库实例与服务、开机任务都已去掉，相关端口已释放。本包文件夹 {PACKAGE} 可以手动删掉。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
