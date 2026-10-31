"""模拟数据源：在本机起一个独立的模拟现场（Modbus 与 OPC UA 服务），并按需触发一个异常场景。

    ..\\.venv\\Scripts\\python.exe sim\\sim.py start            启动模拟现场（前台运行，Ctrl+C 停止）
    ..\\.venv\\Scripts\\python.exe sim\\sim.py list             列出可触发的场景
    ..\\.venv\\Scripts\\python.exe sim\\sim.py trigger          触发 sim\\trigger.json 里写的那个场景
    ..\\.venv\\Scripts\\python.exe sim\\sim.py trigger freeze   直接触发名为 freeze 的场景

在程序目录（如 D:\\HTSG\\app）里运行，`sim\\` 放在 `.venv` 旁边。模拟现场只监听本机回环地址，后端须装在同一台电脑上，
用 `sim\\production.json` 作为配置（它的数据源指向这里的模拟服务）。一次只触发一个场景，持续它规定的时长后自动回到
基线；再触发一个会替换正在进行的那个。控制令牌第一次启动时生成，存在 `sim\\.local\\simulation-secret.txt`，不打印。
本工具不写任何真实设备，也不写后端或数据库。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
FIELD = HERE / "sim-field.json"
TOKEN = HERE / ".local" / "simulation-secret.txt"
TRIGGER = HERE / "trigger.json"


def field():
    from htsg.config import load_settings

    return load_settings(FIELD)


def token(create=False) -> str:
    if not TOKEN.exists():
        if not create:
            raise SystemExit(f"没有控制令牌 {TOKEN}：先运行 sim.py start 启动模拟现场。")
        TOKEN.parent.mkdir(parents=True, exist_ok=True)
        TOKEN.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return TOKEN.read_text(encoding="utf-8").strip()


def start():
    from htsg.simulation import main as run

    config = field()
    os.environ[config.simulation.token_env] = token(create=True)
    print(f"模拟现场启动：Modbus 与 OPC UA 服务在本机，控制端口 {config.simulation.control_port}。Ctrl+C 停止。", flush=True)
    sys.argv = [sys.argv[0], "--config", str(FIELD)]
    run()


def listing():
    print(f"{'场景名':<22}{'持续':>8}  说明")
    for s in field().simulation.scenarios:
        print(f"{s.name:<22}{'-' if s.kind == 'baseline' else f'{s.duration:g} 秒':>8}  {s.label}")


def chosen_name(name: str | None) -> str:
    if name:
        return name
    try:
        choice = json.loads(TRIGGER.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(f'没有 {TRIGGER}：写一个，例如 {{"scenario": "freeze"}}；或直接 sim.py trigger 场景名。') from None
    except json.JSONDecodeError as error:
        raise SystemExit(f"{TRIGGER} 不是有效的 JSON：{error}") from None
    if not isinstance(choice, dict) or set(choice) != {"scenario"} or not isinstance(choice["scenario"], str):
        raise SystemExit(f'{TRIGGER} 只能写一个场景名，例如 {{"scenario": "freeze"}}')
    return choice["scenario"]


def trigger(name: str | None):
    config = field()
    scenarios = {s.name: s for s in config.simulation.scenarios}
    name = chosen_name(name)
    if name not in scenarios:
        raise SystemExit(f"没有场景 {name!r}。可用的场景：{'、'.join(scenarios)}（sim.py list 看说明）。")
    sim = config.simulation
    request = Request(
        f"http://{sim.control_host}:{sim.control_port}/scenario",
        data=json.dumps({"scenario": name}).encode(),
        headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            json.load(response)
    except HTTPError as error:
        why = {401: "控制令牌不对（模拟现场是用另一份令牌启动的）", 422: "模拟现场不认识这个场景"}.get(error.code, "看模拟现场窗口的输出")
        raise SystemExit(f"模拟现场拒绝了（HTTP {error.code}）：{why}。") from None
    except OSError:
        raise SystemExit(f"连不上模拟现场 {sim.control_host}:{sim.control_port}：先用 sim.py start 启动它。") from None
    s = scenarios[name]
    print(f"已触发 {name}（{s.label}）" + ("。" if s.kind == "baseline" else f"，持续 {s.duration:g} 秒后自动回到基线。"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="启动模拟现场")
    sub.add_parser("list", help="列出场景")
    one = sub.add_parser("trigger", help="触发一个场景")
    one.add_argument("scenario", nargs="?", help="场景名；不给则读 sim\\trigger.json")
    args = parser.parse_args(argv)
    {"start": start, "list": listing}.get(args.command, lambda: trigger(args.scenario))()


if __name__ == "__main__":
    main()
