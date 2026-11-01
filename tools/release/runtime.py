"""免安装运行环境：把构建机上的 Python 3.13 x64 拷成一个独立文件夹，再把离线依赖直接装进去（修复轮15，用户 2026-11-01）。

现场只拷贝这个文件夹：不装 Python、不建 venv、不跑 pip、不写注册表。文件夹里的 `python313._pth` 让它只认自己的标准库与
site-packages，现场电脑上另装的 Python、PYTHONPATH、用户目录里的包都影响不到它。发布包、点表工具包都用这里构建。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 运行用不到的部分：文档、测试、图形界面（tkinter、IDLE）、头文件与链接库、构建机上装过的第三方包
SKIP_TOP = {"Doc", "Scripts", "include", "libs", "share", "tcl", "NEWS.txt"}
SKIP_LIB = {"test", "idlelib", "tkinter", "turtledemo", "site-packages", "ensurepip", "venv"}
PTH = "python313._pth"
CHECK = (  # the program's own imports, and that nothing outside the runtime is on the path
    "import sys, htsg.web, htsg.simulation, uvicorn, psycopg, psycopg_binary, asyncua, pymodbus.server, openpyxl;"
    "print('ok' if all(p.startswith(sys.base_prefix) for p in sys.path) and 'site' not in sys.modules else sys.path)"
)


def base_python() -> Path:
    """The installed Python this build runs on, which must be the one the wheels were chosen for."""
    if sys.version_info[:2] != (3, 13) or sys.maxsize <= 2**32:
        raise SystemExit("构建运行环境需要用 Python 3.13 x64 运行本构建脚本")
    return Path(sys.base_prefix)


def ignore(directory: str, names: list[str]) -> set[str]:
    here = Path(directory)
    skipped = {n for n in names if n == "__pycache__" or n.endswith(".pdb")}
    if here == base_python():
        skipped |= SKIP_TOP & set(names)
    elif here == base_python() / "Lib":
        skipped |= SKIP_LIB & set(names)
    return skipped


def build_runtime(dest: Path, wheels: Path, requirements: list[Path], extra_wheels: list[Path]) -> Path:
    """`dest` becomes a self-contained Python with the pinned requirements and `extra_wheels` installed, checked by importing them."""
    shutil.copytree(base_python(), dest, ignore=ignore)
    site = dest / "Lib" / "site-packages"
    site.mkdir()
    # No "import site": with it, the user's own site-packages (%APPDATA%\Python\Python313) joins the path; no dependency
    # needs a .pth file, so the path is these four folders and nothing on the site computer can change it.
    (dest / PTH).write_text("python313.zip\nDLLs\nLib\nLib\\site-packages\n", encoding="ascii")
    pip = [sys.executable, "-m", "pip", "install", "--no-index", "--find-links", str(wheels), "--no-deps",
           "--target", str(site), "--disable-pip-version-check", "--no-warn-script-location"]  # fmt: skip
    for req in requirements:
        subprocess.run([*pip, "-r", str(req)], check=True, capture_output=True)
    if extra_wheels:
        subprocess.run([*pip, *map(str, extra_wheels)], check=True, capture_output=True)
    shutil.rmtree(site / "bin", ignore_errors=True)  # console scripts point at the build machine's Python
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    done = subprocess.run([str(dest / "python.exe"), "-c", CHECK], env=env, cwd=dest.anchor, capture_output=True, text=True)
    if done.returncode or done.stdout.strip() != "ok":
        raise SystemExit(f"运行环境自检失败：{done.stderr.strip() or done.stdout.strip()}")
    return dest


def copy_vc_runtime(into: Path) -> list[str]:
    """The Microsoft C++ runtime files the PostgreSQL binaries load (postgres and its ICU libraries), app-local: a clean Windows
    has none of them, and the zip of PostgreSQL brings none."""
    names = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    into.mkdir(parents=True, exist_ok=True)
    for name in names:
        if not (system / name).is_file():
            raise SystemExit(f"构建机上没有 {system / name}：装好 Microsoft Visual C++ 2015-2022 运行库后再构建")
        shutil.copy2(system / name, into / name)
    return list(names)
