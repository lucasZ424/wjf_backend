"""Deployment smoke test (方案 D48): the delivered start path on a throw-away database - start, stop, restart.

    python scripts/smoke.py [--seconds 20] [--out docs/evidence/d48]

The backend is started the way an operator starts it, `scripts/run.ps1`, against an independent simulated field,
on databases that are created here and dropped here.  It checks that the files served are the files of this
working tree, that the configuration it runs is the one on disk, that a normal stop ends the run as NORMAL and a
restart on the same database continues the history without a duplicate or an invented value, and that nothing
else (the existing development and research databases, `.local`) is touched.  Short smoke: not field acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure  # noqa: E402
import verify_resilience as resilience  # noqa: E402

from htsg.archive import SCHEMA_GENERATION  # noqa: E402
from htsg.auth import Accounts  # noqa: E402
from htsg.simulation import FieldSimulator  # noqa: E402

ROOT = measure.ROOT


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(paths) -> str:
    """One digest over a set of files: their names and contents, in name order."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def running_code() -> list[Path]:
    """The files loaded by the backend and by the separate simulated-field process used in delivery validation."""
    code = (
        "import sys, htsg.cli, htsg.runtime, htsg.web, htsg.archive, htsg.archive_router, htsg.modbus, htsg.ua, "
        "htsg.owner, htsg.simulation, htsg.sim_ua; "
        "print(chr(10).join(sorted(m.__file__ for n, m in sys.modules.items() if n.startswith('htsg') and getattr(m, '__file__', None))))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [Path(line) for line in out.split() if line]


def candidate() -> dict:
    """What this working tree is: commit, whether tracked files are clean, and digests of the code that runs, the
    schema, the pages and the configuration - the evidence of one candidate is only evidence if these are the same."""
    run = lambda *args: subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()  # noqa: E731
    files = running_code()
    return {
        "commit": run("rev-parse", "HEAD"), "clean": run("status", "--porcelain", "--untracked-files=no") == "",
        "code_files": len(files), "code_sha256": tree_digest(files), "schema_sha256": sha256(ROOT / "htsg/archive_schema.sql"),
        "pages_sha256": tree_digest(ROOT.joinpath("htsg/static").glob("*")), "configs_sha256": tree_digest(ROOT.joinpath("config").glob("*.json")),
        "schema_generation": SCHEMA_GENERATION,
    }  # fmt: skip


class Launched(resilience.Backend):
    """The backend started through `scripts/run.ps1` (the Windows start path), not by calling Python directly."""

    def __init__(self, work, config_path, databases, accounts):
        super().__init__(work, config_path, databases, accounts)
        self.command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/run.ps1"),
            "-Config", str(config_path), "-Port", str(self.port), "-Database", str(databases), "-Accounts", str(accounts),
        ]  # fmt: skip

    def start(self):
        log = (self.work / f"backend-{uuid4().hex[:6]}.log").open("w")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(self.command, cwd=self.work, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)

    async def stop_and_wait(self, timeout=60) -> str:
        """Ctrl+Break to the process group, then wait until the web port is closed (the Python child may outlive PowerShell)."""
        try:
            os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
        except (OSError, AttributeError) as error:
            return f"NOT_VERIFIED: {type(error).__name__}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                await asyncio.to_thread(resilience.fetch, f"http://127.0.0.1:{self.port}/live", "", 1)
            except OSError:
                return "STOPPED"
            await asyncio.sleep(0.3)
        return f"NOT_VERIFIED: still answering {timeout} s after Ctrl+Break"


def runs_of(store):
    with psycopg.connect(store.dsn) as conn:
        return [(row[0], row[1], row[2]) for row in conn.execute("SELECT run_id, ended_wall IS NOT NULL, end_reason FROM run ORDER BY started_wall").fetchall()]


async def smoke(seconds: float) -> dict:
    report = {"candidate": candidate()}
    dbs = measure.Databases("htsg_smoke_")
    work = Path(os.environ.get("TEMP", ".")) / ("htsg-smoke-" + uuid4().hex)
    work.mkdir(parents=True)
    config = measure.make_config(stats_interval=60)
    primary, emergency = dbs.make("primary", config.timing), dbs.make("emergency", config.timing)
    config_path = work / "config.json"
    config_path.write_text(json.dumps(config.model_dump(mode="json")), encoding="utf-8")
    (work / "database.json").write_text(json.dumps({"primary": primary.dsn, "emergency": emergency.dsn}), encoding="utf-8")
    accounts = Accounts(work / "accounts.json")
    viewer = accounts.create("viewer", "viewer")
    accounts.create("admin", "admin")
    field = FieldSimulator(config)
    await field.start()
    backends = []
    try:
        for n, label in enumerate(("first start on a new database", "restart on the same database")):
            backend = Launched(work, config_path, work / "database.json", work / "accounts.json")
            backends.append(backend)
            backend.start()
            await backend.until_ready(viewer, timeout=90)
            base = f"http://127.0.0.1:{backend.port}"
            served = await asyncio.to_thread(lambda: urlopen(base + "/static/app.js", timeout=5).read())
            ui = await asyncio.to_thread(resilience.fetch, base + "/api/ui-config", viewer)
            await asyncio.sleep(seconds)
            after = await asyncio.to_thread(resilience.fetch, base + "/api/health", viewer)
            body = await asyncio.to_thread(resilience.fetch, base + f"/api/replay?center={time.time()}&seconds=7200&points=bridge_mvv_01", viewer, 30)
            raw = [x for s in body["channels"]["bridge_mvv_01"]["segments"] for x in s["rows"] if x["origin"] == "RECEIVED"]
            report[label] = {
                "page_served_is_this_tree": hashlib.sha256(served).hexdigest() == sha256(ROOT / "htsg/static/app.js"),
                "config_id_is_the_one_on_disk": ui["config_id"] == config.config_id,
                "archive_sources": {k: v["archive"] for k, v in after["archive"]["sources"].items()},
                "groups_committed": after["archive"]["queue"]["committed"], "queue_failures": after["archive"]["queue"]["failures"],
                "negotiated_ua": after["negotiated"].get("ua_field"), "accounts": after["runtime"]["accounts"],
                "history": {"raw_rows": len(raw), "duplicates": len(raw) - len({x["id"] for x in raw}), "in_order": [x["time"] for x in raw] == sorted(x["time"] for x in raw), "partial": body["partial"], "gaps": len(body["gaps"])},
            }
            report[label]["stop"] = await backend.stop_and_wait()
            await asyncio.sleep(2)  # the process finishes writing the end of the run after its port is closed
            report[label]["runs_in_database"] = runs_of(primary)
            backend.ensure_gone()  # Ctrl+Break also drops the PowerShell wrapper into its debugger prompt: it must not linger
        report["logs_in"] = str(work)
    except BaseException:
        for log in sorted(work.glob("backend-*.log")):  # the logs are removed with the folder: show what they said
            print(f"--- {log.name}", log.read_text(errors="replace")[-2000:], sep="\n", file=sys.stderr)
        raise
    finally:
        for backend in backends:
            backend.ensure_gone()
        await field.stop()
        dbs.drop()
        shutil.rmtree(work, ignore_errors=True)
    return report


async def main(args):
    report = await smoke(args.seconds)
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"smoke-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--out", default="")
    asyncio.run(main(parser.parse_args()))
