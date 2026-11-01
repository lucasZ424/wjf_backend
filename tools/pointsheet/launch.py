"""点表工具包的双击入口（修复轮15）：“1-生成点表模板”“2-生成配置”运行的就是本文件，文件都放在本文件旁边。

    runtime\\python.exe launch.py template          在旁边生成空白 点表.xlsx（已有就不动，免得盖掉填好的表）
    runtime\\python.exe launch.py convert [点表]    读点表（不给则用旁边的 点表.xlsx），在旁边生成 production.json；
                                                   旧的 production.json 先改名留底。按后端自己的配置规则整份校验，通过才写出。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # the bundled runtime does not put the script's folder on the path

import pointsheet  # noqa: E402

BASE = HERE / "production.base.json"
SHEET = HERE / "点表.xlsx"
OUT = HERE / "production.json"


def template():
    if SHEET.exists():
        raise SystemExit(f"{SHEET} 已经有了，不覆盖（它可能已填好）。要新模板，先把它改名或挪走。")
    pointsheet.main(["template", "--base", str(BASE), "--out", str(SHEET)])
    print(f"已生成点表模板：{SHEET}\n交现场填写：接入的点填地址或 NodeId，不接入的留空。填好后拖到“2-生成配置”上。")


def convert(sheet: str | None):
    path = Path(sheet.strip('"')) if sheet else SHEET
    if not path.is_file():
        raise SystemExit(f"找不到点表 {path}：把填好的点表拖到“2-生成配置”上，或放在本目录并命名为 点表.xlsx。")
    fresh = OUT.with_name("production.new.json")
    if pointsheet.main(["convert", "--base", str(BASE), "--sheet", str(path), "--out", str(fresh)]):
        raise SystemExit("\n点表改正后再拖一次；旧的 production.json 没有动。")
    if OUT.exists():  # only once the new one passed: a failed conversion leaves the old file where it was
        kept = OUT.with_name(f"production.{time.strftime('%Y%m%d-%H%M%S')}.json")
        OUT.rename(kept)
        print(f"旧配置改名留底：{kept.name}")
    fresh.rename(OUT)
    print(f"\n已生成配置：{OUT}\n安装时把它放在发布包旁边，或在安装程序提示时拖进窗口。")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["template"]:
        template()
    elif args[:1] == ["convert"]:
        convert(args[1] if len(args) > 1 else None)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
