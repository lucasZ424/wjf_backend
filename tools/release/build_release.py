"""构建现场发布包：只放运行需要的文件，带每个文件的 SHA-256 清单。

    python tools/release/build_release.py --ref v1.0.0 --out D:/HTSG-release [--python-installer python-3.13.x-amd64.exe]
    python tools/release/build_release.py --working-tree --out D:/HTSG-release        # 未提交快照，只供演练

正式构建只认已提交的版本（标签或提交号）：源码由 `git archive` 从该版本导出，工作区里未提交的改动、被忽略的测试、
证据与开发文件都不会进包。`--working-tree` 用当前工作区（同样遵守 .gitignore）做一个临时快照，不改任何分支；包名与
VERSION.json 标明“未提交快照”，不得交付现场。

包内：程序 wheel 与只含运行依赖的离线依赖包、点表工具、部署与看守脚本、生产基础配置与空模板、安装与运维说明、
校验脚本，以及（给了就带上的）Python 与 PostgreSQL 安装包。PostgreSQL 默认取开发盘上的 postgresql-18.6.zip 及其校验文件。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCAL_WHEELS = Path("D:/HTSG-dev/wheelhouse")
POSTGRESQL_ZIP = Path("D:/HTSG-dev/postgresql-18.6.zip")
# 从源码导出里取的文件：发布包的全部程序以外内容（白名单，其余一律不进包）
FROM_SOURCE = {
    "scripts/deploy_databases.py": "scripts/deploy_databases.py",
    "scripts/supervise.py": "scripts/supervise.py",
    "tools/pointsheet/pointsheet.py": "tools/pointsheet/pointsheet.py",
    "tools/pointsheet/README.md": "tools/pointsheet/README.md",
    "tools/pointsheet/requirements.txt": "tools/pointsheet/requirements.txt",
    "config/production.base.json": "config/production.base.json",
    "config/production.template.json": "config/production.template.json",
    "requirements.runtime.lock": "requirements.runtime.lock",
    "tools/release/安装与运维说明.md": "安装与运维说明.md",
    "tools/release/verify.ps1": "verify.ps1",
}


def git(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, **kwargs)


def export_ref(ref: str, into: Path) -> tuple[str, bool]:
    """(commit, snapshot) after the source of `ref` - or of the working tree - is written into `into`."""
    if ref is None:  # the working tree as `git add -A` would see it, through a throw-away index: no branch, no ref changes
        index = Path(tempfile.mkdtemp(prefix="htsg-index-")) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        git("read-tree", "HEAD", env=env)
        git("add", "-A", env=env)
        tree = git("write-tree", env=env, text=True).stdout.strip()
        archive = git("archive", "--format=tar", tree).stdout
        commit, snapshot = git("rev-parse", "HEAD", text=True).stdout.strip(), True
        shutil.rmtree(index.parent, ignore_errors=True)
    else:
        commit = git("rev-parse", "--verify", ref + "^{commit}", text=True).stdout.strip()
        archive = git("archive", "--format=tar", commit).stdout
        snapshot = False
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return commit, snapshot


def build_wheel(source: Path, out: Path) -> Path:
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out)], cwd=source, check=True,
                   capture_output=True)  # fmt: skip
    (wheel,) = out.glob("htsg_backend-*.whl")
    return wheel


def fetch_wheels(requirements: list[Path], into: Path):
    """The pinned wheels for Windows x64, Python 3.13: from the local offline store first, the index for what it lacks."""
    common = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:", "--platform", "win_amd64",
              "--python-version", "3.13", "--implementation", "cp", "-d", str(into)]  # fmt: skip
    for req in requirements:
        local = subprocess.run([*common, "--no-index", "--find-links", str(LOCAL_WHEELS), "-r", str(req)], capture_output=True)
        if local.returncode:
            subprocess.run([*common, "--find-links", str(LOCAL_WHEELS), "-r", str(req)], check=True, capture_output=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_sums(root: Path) -> Path:
    lines = [f"{sha256(p)}  {p.relative_to(root).as_posix()}" for p in sorted(root.rglob("*")) if p.is_file() and p.name != "SHA256SUMS"]
    target = root / "SHA256SUMS"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def build(ref: str | None, out: Path, python_installer: Path | None, postgresql_zip: Path | None) -> Path:
    work = Path(tempfile.mkdtemp(prefix="htsg-release-"))
    try:
        source = work / "source"
        commit, snapshot = export_ref(ref, source)
        version = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        name = f"HTSG-{version}-{commit[:8]}" + ("-SNAPSHOT" if snapshot else "")
        package = out / name
        if package.exists():
            raise SystemExit(f"{package} already exists; nothing was changed")
        missing = [s for s in FROM_SOURCE if not (source / s).is_file()]
        if missing:
            raise SystemExit(f"the source of {ref or 'the working tree'} lacks {', '.join(missing)}")
        package.mkdir(parents=True)
        for src, dst in FROM_SOURCE.items():
            (package / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / src, package / dst)
        wheels = package / "wheelhouse"
        wheels.mkdir()
        build_wheel(source, wheels)
        fetch_wheels([source / "requirements.runtime.lock", source / "tools/pointsheet/requirements.txt"], wheels)
        installers = package / "installers"
        for given in (python_installer, postgresql_zip):
            if given is not None:
                installers.mkdir(exist_ok=True)
                shutil.copy2(given, installers / given.name)
                beside = given.with_name("download.sha256")  # the PostgreSQL download keeps its own checksum beside it
                if given == postgresql_zip and beside.exists():
                    shutil.copy2(beside, installers / "postgresql.download.sha256")
        (package / "VERSION.json").write_text(json.dumps({
            "version": version, "commit": commit, "ref": ref, "snapshot": snapshot,
            "built": time.strftime("%Y-%m-%d %H:%M:%S %z"), "python": "3.13 x64",
            "python_installer": python_installer.name if python_installer else None,
            "postgresql": postgresql_zip.name if postgresql_zip else None,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")  # fmt: skip
        write_sums(package)
        shutil.make_archive(str(package), "zip", root_dir=out, base_dir=name)
        return package
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建现场发布包")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--ref", help="要交付的标签或提交号")
    which.add_argument("--working-tree", action="store_true", help="未提交快照，只供演练，不得交付现场")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python-installer", type=Path, help="Python 3.13 x64 安装程序（不给则包内不带，现场另备）")
    parser.add_argument("--postgresql-zip", type=Path, default=POSTGRESQL_ZIP if POSTGRESQL_ZIP.exists() else None)
    args = parser.parse_args(argv)
    for given in (args.python_installer, args.postgresql_zip):
        if given is not None and not given.is_file():
            raise SystemExit(f"{given} not found")
    package = build(None if args.working_tree else args.ref, args.out, args.python_installer, args.postgresql_zip)
    print(f"发布包：{package}（及同名 .zip）")
    if args.working_tree:
        print("这是未提交快照：只供演练，不得交付现场。")
    if args.python_installer is None:
        print("包内没有 Python 安装程序：现场需另备 Python 3.13 x64。")


if __name__ == "__main__":
    main()
