"""HTSG 一键安装（修复轮15，用户 2026-11-01：傻瓜式安装）。双击发布包里的“安装.cmd”即运行本程序（自动请求管理员权限）。

做的事，依次：核对发布包 → 拷程序到 <根目录>\\app → 解压数据库软件到 <根目录>\\pgsql（带上它需要的 VC 运行库）→ 放入配置
（点表工具或模拟数据源包生成的 production.json）→ 选两块数据盘 → 建库、建管理员 → 注册数据库服务与开机任务 → 等后端起来、
打开浏览器。

可以重复运行：已经做完的步骤跳过，中途断电或关窗后再双击一次，从没做完的那一步接着做。它不删除任何数据，也不覆盖已有
的配置、库或账户；装有另一版本时停下，按《安装与运维说明》第7节升级。

无人值守（管理员 PowerShell，发布包目录里）：
    .\\runtime\\python.exe setup\\install.py --root D:\\HTSG --config D:\\点表\\production.json --primary D: --emergency E: --yes
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import shutil
import string
import subprocess
import sys
import time
import webbrowser
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

PACKAGE = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8000/"
PARTIAL = ".partial"  # a folder this program was still filling when it stopped; it holds nothing else and is redone


class Stop(Exception):
    """A step cannot go on: the message says why and what to do."""


def say(text: str = ""):
    print(text, flush=True)
    log = Installer.log_path
    if log is not None:
        with log.open("a", encoding="utf-8") as f:
            f.write(text + "\n")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except AttributeError:  # not Windows
        return False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(package: Path) -> int:
    """Every file the package lists is there with its SHA-256; the count, or Stop."""
    sums = package / "SHA256SUMS"
    if not sums.is_file():
        raise Stop(f"{package} 里没有 SHA256SUMS：这不是完整的发布包，重新拷贝。")
    bad = []
    lines = [line for line in sums.read_text(encoding="utf-8").splitlines() if line]
    for line in lines:
        digest, rel = line.split("  ", 1)
        path = package / rel
        if not path.is_file() or sha256(path) != digest:
            bad.append(rel)
    if bad:
        raise Stop(f"发布包有 {len(bad)} 个文件缺失或内容不对（例如 {bad[0]}）：不要安装，重新拷贝发布包。")
    return len(lines)


def fixed_drives() -> list[tuple[str, int]]:
    """(letter "D:", free bytes) of every local fixed disk."""
    found = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if mask >> i & 1 and ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 3:  # DRIVE_FIXED
            try:
                found.append((f"{letter}:", shutil.disk_usage(f"{letter}:\\").free))
            except OSError:
                continue
    return found


class Installer:
    log_path: Path | None = None

    def __init__(self, root: Path, *, config=None, primary=None, emergency=None, unattended=False, browser=True,
                 package=PACKAGE, ask=input, drives=fixed_drives, run=subprocess.run, reachable=None):  # fmt: skip
        self.root, self.package = root, package
        self.app, self.pgsql = root / "app", root / "pgsql"
        self.config, self.primary, self.emergency = config, primary, emergency
        self.unattended, self.browser = unattended, browser
        self.ask, self.drives, self.run = ask, drives, run
        self.reachable = reachable or self.http_up

    # ------------------------------------------------------------------ steps

    def program(self):
        """The package's files into <root>\\app; an app of the same build is kept, another build stops the install."""
        mine = json.loads((self.package / "VERSION.json").read_text(encoding="utf-8"))
        if self.app.resolve() == self.package.resolve():
            say("程序目录就是本发布包，不用拷贝。")
            return
        if self.app.exists():
            there = self.app / "VERSION.json"
            theirs = json.loads(there.read_text(encoding="utf-8")) if there.is_file() else {}
            if theirs.get("commit") != mine["commit"]:
                raise Stop(f"{self.app} 里已装有另一版本（{theirs.get('version', '不明')}）。本程序不覆盖已装的程序："
                           "升级按《安装与运维说明》第7节做。")  # fmt: skip
            say(f"程序已在 {self.app}（同一版本 {mine['version']}），跳过拷贝。")
            return
        partial = self.app.with_name(self.app.name + PARTIAL)
        shutil.rmtree(partial, ignore_errors=True)
        say(f"拷贝程序到 {self.app} ……")
        shutil.copytree(self.package, partial, ignore=shutil.ignore_patterns(".local", "production.json"))
        partial.rename(self.app)

    def database_software(self):
        """PostgreSQL from the package's zip into <root>\\pgsql, with the C++ runtime files it loads beside its programs."""
        if not (self.pgsql / "bin" / "postgres.exe").is_file():
            zips = sorted((self.package / "installers").glob("postgresql-*.zip"))
            if not zips:
                raise Stop("发布包里没有 installers\\postgresql-*.zip。")
            partial = self.pgsql.with_name(self.pgsql.name + PARTIAL)
            shutil.rmtree(partial, ignore_errors=True)
            say(f"解压数据库软件 {zips[-1].name} 到 {self.pgsql} ……")
            with zipfile.ZipFile(zips[-1]) as z:
                z.extractall(partial)
            inner = partial / "pgsql"
            (inner if inner.is_dir() else partial).rename(self.pgsql)
            shutil.rmtree(partial, ignore_errors=True)
        else:
            say(f"数据库软件已在 {self.pgsql}，跳过解压。")
        for dll in sorted((self.package / "installers" / "vc").glob("*.dll")):
            target = self.pgsql / "bin" / dll.name
            if not target.exists():
                shutil.copy2(dll, target)

    def configuration(self):
        """config\\production.json in the app: one already there is kept; otherwise the given file, one beside the package,
        or the one the user drags into the window.  Checked by the program either way."""
        target = self.app / "config" / "production.json"
        if target.is_file():
            say(f"配置已在 {target}，沿用。")
        else:
            source = self.config or next(
                (p for p in (self.package / "production.json", self.package.parent / "production.json") if p.is_file()), None
            )
            while source is None or not Path(source).is_file():
                if source is not None:
                    say(f"找不到文件：{source}")
                if self.unattended:
                    raise Stop("没有配置文件：用 --config 指定点表工具（或模拟数据源包）生成的 production.json。")
                source = self.ask("把配置文件 production.json（点表工具或模拟数据源包生成的）拖进本窗口，然后按回车：").strip().strip('"')
                source = Path(source) if source else None
            shutil.copy2(source, target)
            say(f"配置已放入 {target}（来自 {source}）。")
        self.python("核对配置", "-m", "htsg.cli", "check", "--config", "config/production.json")

    def volumes(self):
        """.local\\deploy.json: the two disks of the databases; one already written is kept."""
        target = self.app / ".local" / "deploy.json"
        if target.is_file():
            say(f"部署文件已在 {target}，沿用。")
            return
        drives = sorted(self.drives(), key=lambda d: -d[1])
        if not self.primary or not self.emergency:
            if self.unattended:
                raise Stop("没有指定数据盘：用 --primary 与 --emergency 给两块不同的盘，例如 --primary D: --emergency E:。")
            say("本机的本地硬盘（剩余空间）：" + "，".join(f"{d} {free / 1024**3:.0f} GB" for d, free in drives))
            say("正常库放剩余空间最大的那块盘；应急库放另一块盘（正常库所在盘出问题时，新数据写到应急库）。")
        self.primary = self.primary or self.pick("正常库放哪块盘", next((d for d, _ in drives), "D:"))
        others = [d for d, _ in drives if d.upper() != self.primary.upper()]
        self.emergency = self.emergency or self.pick("应急库放哪块盘", others[0] if others else "")
        if self.primary.upper().rstrip("\\") == self.emergency.upper().rstrip("\\"):
            raise Stop("正常库和应急库不能放在同一块盘上。")
        target.parent.mkdir(parents=True, exist_ok=True)
        deploy = {"volumes": {"primary": self.primary, "emergency": self.emergency},
                  "pg_bin": (self.pgsql / "bin").as_posix(), "config": "config/production.json"}  # fmt: skip
        target.write_text(json.dumps(deploy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        say(f"部署文件已写入 {target}：正常库 {self.primary}，应急库 {self.emergency}。")

    def pick(self, question: str, default: str) -> str:
        while True:
            answer = self.ask(f"{question}？直接回车用 {default or '（无）'}：").strip() or default
            if len(answer) == 1 and answer.isalpha():
                answer += ":"
            if answer:
                return answer.upper() if len(answer) == 2 else answer

    def databases(self):
        for action, what in (("plan", "检查部署条件"), ("install", "建库与管理员账户"), ("register", "注册数据库服务与开机任务"),
                             ("status", "查看状态")):  # fmt: skip
            self.python(what, "scripts/deploy_databases.py", action)

    def started(self):
        say(f"等后端启动（最多 5 分钟）：{URL}")
        deadline = time.monotonic() + 300
        while not self.reachable():
            if time.monotonic() > deadline:
                raise Stop("5 分钟内后端没有响应。看 <程序目录>\\.local 下的日志，或按《安装与运维说明》第6节排查；"
                           "重新双击“安装.cmd”会接着检查。")  # fmt: skip
            time.sleep(3)
        if self.browser:
            webbrowser.open(URL)

    # ------------------------------------------------------------------ helpers

    def python(self, what: str, *args: str):
        say(f"—— {what}")
        python = self.app / "runtime" / "python.exe"
        done = self.run([str(python), *args], cwd=self.app)
        if done.returncode:
            raise Stop(f"“{what}”没有通过（见上面的输出）。改正后重新双击“安装.cmd”，做完的步骤会跳过。")

    @staticmethod
    def http_up() -> bool:
        try:
            with urlopen(URL, timeout=3):
                return True
        except (URLError, OSError):
            return False

    def install(self):
        say(f"HTSG 一键安装：发布包 {self.package}，安装到 {self.root}")
        if not str(self.root).isascii() or " " in str(self.root):
            raise Stop(f"安装目录 {self.root} 只能用英文字母、数字（不能有中文或空格，PostgreSQL 处理不了）：用 --root 换一个。")
        say(f"核对发布包：{verify(self.package)} 个文件全部一致。")
        self.root.mkdir(parents=True, exist_ok=True)
        self.program()
        self.database_software()
        self.configuration()
        self.volumes()
        self.databases()
        self.started()
        say()
        say("安装完成。")
        say(f"  网页：{URL}（本机）")
        say(f"  程序目录：{self.app}")
        say(f"  管理员初始口令：{self.app / '.local' / 'initial-credentials.json'}（只给业主，登录后在“系统管理与权限”页加只读账户）")
        say("  接着按《安装与运维说明》第5节做安装后自检。断电或重启后数据库与后端会自己起来，不用再运行本程序。")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="HTSG 一键安装")
    parser.add_argument("--root", type=Path, default=Path("D:/HTSG"), help="安装根目录，默认 D:\\HTSG")
    parser.add_argument("--config", type=Path, help="点表工具或模拟数据源包生成的 production.json")
    parser.add_argument("--primary", help="正常库的盘，例如 D:")
    parser.add_argument("--emergency", help="应急库的盘，例如 E:（与正常库不同）")
    parser.add_argument("--yes", action="store_true", help="不提问：缺什么就停下并说明")
    parser.add_argument("--no-browser", action="store_true", help="装完不打开浏览器")
    args = parser.parse_args(argv)
    if not is_admin():
        say("需要管理员权限：双击“安装.cmd”（会弹出授权），或在以管理员身份运行的 PowerShell 里运行本程序。")
        return 2
    args.root.mkdir(parents=True, exist_ok=True)
    Installer.log_path = args.root / "install.log"
    say(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} ====")
    try:
        Installer(args.root, config=args.config, primary=args.primary, emergency=args.emergency, unattended=args.yes,
                  browser=not args.no_browser).install()  # fmt: skip
    except Stop as stop:
        say(f"\n安装停在这一步：{stop}")
        return 1
    except KeyboardInterrupt:
        say("\n已中断。重新双击“安装.cmd”会从没做完的步骤接着做。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
