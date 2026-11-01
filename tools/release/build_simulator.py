"""构建模拟数据源包：在部署机上起模拟现场、触发场景；自带运行环境，解压即用（修复轮15）。

    python tools/release/build_simulator.py --ref <提交号或标签> --out D:/HTSG-release

和发布包一样只认已提交的版本（`git archive` 导出）。包内：双击入口“1-启动模拟现场（演示）”“1-启动模拟现场（稳态）”
“2-触发场景”；免安装运行环境 `runtime\\`（与发布包同一构建方式）；`sim\\sim.py`、模拟现场配置 `sim\\sim-field.json`（即该
版本的 `config/research-sim.json`）与触发文件示例；后端配置 `production.json`（正式模式，点表与数据源取模拟现场的，项目
名称、存储与其余部分取 `config/production.base.json`，构建时按该版本的程序校验，安装时交给发布包的安装程序）；使用说明、
`verify.ps1` 与 SHA-256 清单。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_release import copy_files, export_ref, program_runtime, seal  # noqa: E402

FROM_SOURCE = {
    "tools/simulator/sim.py": "sim/sim.py",
    "config/research-sim.json": "sim/sim-field.json",
    "tools/simulator/start-demo.cmd": "1-启动模拟现场（演示）.cmd",
    "tools/simulator/start-steady.cmd": "1-启动模拟现场（稳态）.cmd",
    "tools/simulator/trigger.cmd": "2-触发场景.cmd",
    "tools/simulator/模拟数据源使用说明.md": "模拟数据源使用说明.md",
    "tools/release/verify.ps1": "verify.ps1",
}


def backend_config(source: Path) -> dict:
    """The production configuration the backend runs against the simulated field, checked by the exported program."""
    field = json.loads((source / "config/research-sim.json").read_text(encoding="utf-8"))
    base = json.loads((source / "config/production.base.json").read_text(encoding="utf-8"))
    out = {k: v for k, v in field.items() if k not in ("simulation", "dev_admin_login")}
    out.update(mode="production", project=base["project"], storage=base["storage"])
    for key, value in base.items():
        out.setdefault(key, value)
    sys.path.insert(0, str(source))
    from htsg.config import Settings

    Settings.model_validate(out)
    return out


def build(ref: str, out: Path) -> Path:
    work = Path(tempfile.mkdtemp(prefix="htsg-sim-"))
    try:
        source = work / "source"
        commit, _ = export_ref(ref, source)
        version = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        name = f"HTSG-SIM-{version}-{commit[:8]}"
        package = out / name
        if package.exists():
            raise SystemExit(f"{package} already exists; nothing was changed")
        config = backend_config(source)
        package.mkdir(parents=True)
        copy_files(source, package, FROM_SOURCE)
        program_runtime(source, work, package / "runtime")
        (package / "production.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (package / "sim/trigger.json").write_text('{"scenario": "freeze"}\n', encoding="utf-8")
        seal(package, out, {
            "kind": "simulated field", "version": version, "commit": commit, "ref": ref,
            "built": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "needs": f"HTSG {version} 发布包装在同一台电脑上（安装时用本包的 production.json）",
        })  # fmt: skip
        return package
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建模拟数据源包")
    parser.add_argument("--ref", required=True, help="已提交的标签或提交号")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    print(f"模拟数据源包：{build(args.ref, args.out)}（及同名 .zip）")


if __name__ == "__main__":
    main()
