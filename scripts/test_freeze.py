"""冻结用例，防止开发时误改，并列出冻结之后用例的变化（用户 2026-11-06 定的工作流，见 CLAUDE.md）。

    python scripts/test_freeze.py freeze            把 tests/ 拷一份快照，开始冻结（不需要提交）
    python scripts/test_freeze.py allow 文件名       裁决同意后，放开一个用例文件；报告里仍会列出它的变化
    python scripts/test_freeze.py report            列出增删的文件、被删或被改的断言、新增的断言
    python scripts/test_freeze.py close             结束冻结，删除快照
    python scripts/test_freeze.py hook              Claude Code 的钩子入口：编辑冻结的用例时拒绝

钩子只拦文件编辑工具，防的是误操作，不是安全边界；从命令行改的文件，report 一样查得出来。
"""

import difflib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
FROZEN = ROOT / ".local" / "test-freeze"
ALLOWED = "allowed.txt"
ASSERTION = re.compile(r"\b(assert|pytest\.raises)\b")


def sources(folder):
    return {p.relative_to(folder).as_posix(): p for p in folder.rglob("*.py") if "__pycache__" not in p.parts}


def allowed():
    path = FROZEN / ALLOWED
    return set(path.read_text(encoding="utf-8").split()) if path.exists() else set()


def freeze():
    if FROZEN.exists():
        print(f"已经在冻结中：{FROZEN}。要重新冻结，先 close。", file=sys.stderr)
        return 1
    files = sources(TESTS)
    for name, path in files.items():
        target = FROZEN / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    print(f"已冻结 {len(files)} 个用例文件。")
    return 0


def allow(name):
    if not FROZEN.exists():
        print("没有在冻结中。", file=sys.stderr)
        return 1
    if name not in sources(TESTS) and name not in sources(FROZEN):
        print(f"tests/ 下没有 {name}。", file=sys.stderr)
        return 1
    (FROZEN / ALLOWED).write_text("\n".join(sorted(allowed() | {name})) + "\n", encoding="utf-8")
    print(f"已放开 {name}。它的变化仍会列在报告里，裁决写进修复轮记录。")
    return 0


def changed_lines(before, after):
    removed, added = [], []
    for line in difflib.unified_diff(before, after, n=0, lineterm=""):
        if line.startswith(("---", "+++", "@@")):
            continue
        (removed if line.startswith("-") else added).append(line[1:].strip())
    return removed, added


def read_lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def report():
    if not FROZEN.exists():
        print("没有在冻结中。", file=sys.stderr)
        return 1
    old, new = sources(FROZEN), sources(TESTS)
    gone, born = sorted(set(old) - set(new)), sorted(set(new) - set(old))
    weakened, added_total, changed = [], 0, []
    for name in gone:
        weakened += [(name, line.strip()) for line in read_lines(old[name]) if ASSERTION.search(line)]
    for name in sorted(set(old) & set(new)):
        removed, added = changed_lines(read_lines(old[name]), read_lines(new[name]))
        if removed or added:
            changed.append(name)
        weakened += [(name, line) for line in removed if ASSERTION.search(line)]
        added_total += sum(1 for line in added if ASSERTION.search(line))
    added_total += sum(1 for name in born for line in read_lines(new[name]) if ASSERTION.search(line))
    print(f"冻结后变化：改动 {len(changed)} 个文件，删除 {len(gone)} 个，新增 {len(born)} 个；"
          f"被删或被改的断言 {len(weakened)} 条，新增断言 {added_total} 条。")  # fmt: skip
    for title, names in (("改动的文件", changed), ("删除的文件", gone), ("新增的文件", born), ("已放开的文件", sorted(allowed()))):
        if names:
            print(f"{title}：" + "、".join(names))
    if weakened:
        print("被删或被改的断言（每条都要有裁决）：")
        for name, line in weakened:
            print(f"  {name}: {line}")
    return 0


def close():
    if FROZEN.exists():
        shutil.rmtree(FROZEN)
    print("已结束冻结。")
    return 0


def hook():
    try:
        payload = json.loads(sys.stdin.read())
        target = Path(payload["tool_input"]["file_path"]).resolve()
        name = target.relative_to(TESTS.resolve()).as_posix()
    except (ValueError, KeyError, TypeError):
        return 0  # not a file under tests/, or not something this hook understands
    if not FROZEN.exists() or name in allowed():
        return 0
    print(f"tests/{name} 已冻结，开发时不能改。认为用例错了就提异议，引用终版或验收表的出处，裁决同意后再放开："
          f"python scripts/test_freeze.py allow {name}", file=sys.stderr)  # fmt: skip
    return 2


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = sys.argv[1:] if argv is None else argv
    commands = {"freeze": freeze, "report": report, "close": close, "hook": hook}
    if len(args) == 2 and args[0] == "allow":
        return allow(args[1])
    if len(args) == 1 and args[0] in commands:
        return commands[args[0]]()
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
