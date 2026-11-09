"""换点表：把新的 production.json 用上，数据库和历史数据都不动（用户 2026-11-07）。双击程序目录里的“换点表.cmd”即运行本程序。

做的事，依次：找到新配置（程序目录里的 production.json；没有就请你把它的完整路径粘贴进窗口，管理员窗口拖不进文件）→ 校验，不通过就什么都不动，后端照常运行 → 数据盘路径
写成安装时选的盘 → 旧配置留底、换上新配置 → 让后端正常停下（.local\\STOP，文件或文件夹都算）→ 清掉 STOP（里面有东西就改名留着）
→ 启动后端，等它响应。配置没变、后端也在运行时什么都不做。

只换配置：数据库、账户、服务和开机任务都不动。它只在有人双击时运行，后端和看守程序都不依赖它。
"""

from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_databases import ROOT, TASK, plan, read_input, with_storage, write_atomically  # noqa: E402
from supervise import STOP_GRACE, answers, held_elsewhere  # noqa: E402

from htsg.config import Settings  # noqa: E402

LOCAL = ROOT / ".local"
LIVE = "http://127.0.0.1:8000/live"  # the port the task runs the supervisor with
STOP_WAIT = STOP_GRACE + 30  # the supervisor gives the backend STOP_GRACE seconds to stop, then ends it
START_WAIT = 300 + 180 + 30  # a start the task ignores (the old run not quite gone) waits for its five-minute trigger, then the supervisor up to 180 s for the normal database


class Stop(Exception):
    """Cannot go on: the message says why and what state things are in."""


def say(text: str = ""):
    print(text, flush=True)
    if LOCAL.is_dir():
        with (LOCAL / "change-config.log").open("a", encoding="utf-8") as f:
            f.write(text + "\n")


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def new_file(ask) -> Path:
    beside = ROOT / "production.json"
    if beside.is_file():
        say(f"新配置：{beside}")
        return beside
    answer = ask("把新的 production.json 的完整路径粘贴到本窗口，然后按回车（直接回车就退出，什么都不动）：").strip().strip('"')
    if not answer:
        raise Stop("没有给配置文件，什么都没动。")
    say(f"新配置：{answer}")
    return Path(answer)


def checked(source: Path, roles) -> tuple[dict, Settings]:
    """The new configuration with the installed disks, or Stop before anything was touched.  The base and the template lie in the
    config folder and pass validation with no point at all: a backend started on one of them would acquire nothing."""
    try:
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
        updated = with_storage(raw, roles)
        settings = Settings.model_validate(updated)
    except (OSError, ValueError, AttributeError) as error:
        reason = " ".join(str(error).splitlines()[:3])[:300]
        raise Stop(f"新配置不能用，什么都没动，后端照常运行：{reason}") from None
    enabled = {s.id for s in settings.sources if s.enabled}
    if settings.mode != "production" or not any(p.source in enabled for p in settings.points):
        raise Stop("这不是能用的现场配置（不是正式模式，或者没有接入任何点），什么都没动，后端照常运行。文件选对了吗？")
    if raw.get("storage") != updated["storage"]:
        say(f"新配置里的数据盘路径和安装时不同，已改成安装时的盘：{' '.join(role.folder for role in roles)}")
    return updated, settings


def put_in_place(target: Path, updated: dict) -> tuple[bool, Path | None]:
    """(changed, the copy of the old file).  A current file that cannot be read counts as different."""
    try:
        if json.loads(target.read_text(encoding="utf-8-sig")) == updated:
            return False, None
    except (OSError, ValueError):
        pass
    backup = target.with_name(f"{target.name}.bak-{stamp()}") if target.is_file() else None
    if backup:
        shutil.copy2(target, backup)
    write_atomically(target, json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
    return True, backup


def running() -> bool:
    return held_elsewhere(LOCAL / "backend.lock") or held_elsewhere(LOCAL / "supervisor.lock")


def stop_backend(marker: Path, wait: float):
    """Through the supervisor, the normal way; both locks free means nothing is left that could start a backend meanwhile."""
    if not running():
        return
    if not marker.exists():
        marker.write_text("", encoding="utf-8")
    say(f"让后端正常停下（最多 {wait:.0f} 秒，不要关这个窗口）……")
    deadline = time.monotonic() + wait
    while running():
        if time.monotonic() > deadline:
            raise Stop(f"后端 {wait:.0f} 秒内没有停下，还在用旧配置运行；新配置已放好，它下次启动时生效。重启电脑可以让它生效。")
        time.sleep(1)


def clear_marker(marker: Path):
    """A marker with something in it is renamed, not deleted: whoever made it may want it back."""
    if not marker.exists():
        return
    try:
        if (marker.is_dir() and any(marker.iterdir())) or (marker.is_file() and marker.stat().st_size):
            kept = marker.with_name(f"stop-kept-{stamp()}")
            marker.rename(kept)
            say(f"STOP 里有内容，改名留着：{kept}")
        elif marker.is_dir():
            marker.rmdir()
        else:
            marker.unlink()
    except OSError as error:
        raise Stop(f"{marker} 清不掉（{error}）：手工删掉它，5 分钟内后端会自己启动。") from None
    say("STOP 已清掉。")


def change(ask=input, run=subprocess.run, reachable=answers, stop_wait: float = STOP_WAIT, start_wait: float = START_WAIT):
    if not (LOCAL / "deploy.json").is_file():
        raise Stop(f"{ROOT} 不是安装好的程序目录（没有 .local\\deploy.json）。这个程序要放在安装目录（默认 D:\\HTSG\\app）里运行。")
    inp = read_input(LOCAL / "deploy.json")
    source = new_file(ask)
    updated, settings = checked(source, plan(inp)[0])
    say(f"新配置有效：{len(settings.points)} 个点；配置编号 {settings.config_id}")
    changed, backup = put_in_place(inp["config"], updated)
    say(f"已换上新配置，旧配置留底：{backup}" if changed else "新配置和现在用的一样，不用换。")
    marker = LOCAL / "STOP"
    if changed or marker.exists() or not held_elsewhere(LOCAL / "backend.lock"):
        try:
            stop_backend(marker, stop_wait)
        finally:
            clear_marker(marker)
        if run(["schtasks", "/Run", "/TN", TASK], capture_output=True).returncode:
            raise Stop(f"开机任务 {TASK} 启动不了。配置已放好；双击发布包里的“安装.cmd”可以把任务补回来。")
    say(f"等后端响应（通常很快，最多等 {start_wait / 60:.0f} 分钟）……")
    deadline = time.monotonic() + start_wait
    while not reachable(LIVE):
        if time.monotonic() > deadline:
            where = f"旧配置留底在 {backup}。" if backup else ""
            raise Stop(f"{start_wait:.0f} 秒内后端没有响应。看 {LOCAL}\\supervisor.log 和 backend.log 的最后几行。{where}")
        time.sleep(3)
    if source == ROOT / "production.json":  # used up: a later run must not take this old file for a new one
        try:
            source.rename(source.with_name(f"{source.name}.used-{stamp()}"))
        except OSError as error:
            say(f"提示：{source} 没能改名（{error}）；下次换点表前先把它删掉。")
    say()
    say("完成：后端正在用这份配置运行。刷新网页后，到“水冷壁应变场”页核对读数，到“系统自检”页核对写入。")


def main() -> int:
    if not is_admin():
        say("需要管理员权限：双击“换点表.cmd”（会弹出授权），或在以管理员身份运行的 PowerShell 里运行本程序。")
        return 2
    say(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 换点表 ====")
    try:
        change()
    except Stop as stop:
        say(f"\n没有完成：{stop}")
        return 1
    except OSError as error:
        say(f"\n没有完成：{error}")
        return 1
    except KeyboardInterrupt:
        say("\n已中断。STOP 已清掉的话，5 分钟内后端会自己启动；重新双击“换点表.cmd”可以接着做。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
