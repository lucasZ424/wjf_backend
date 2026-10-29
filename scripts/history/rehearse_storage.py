"""Storage switching rehearsal on the real disks (修复轮06 演练, user 2026-10-27).

The archive router alone, with the site's own rules (production mode), writes synthetic whole groups into three real
PostgreSQL clusters - the normal database on D:, the emergency database on C:, the removable one on E: - that
`scripts/deploy_databases.py install` made from `.local/rehearsal/deploy.json`.  Every run creates a fresh database in
each cluster (nothing earlier is touched or deleted).  Each role's "disk" has a budget (500 MB) instead of the drive's
real size: the reading the router asks for is the size of THIS run's database files against the budget, taken only
after the real path has been read - so a disk that is pulled out is gone here exactly as it is at the site.  None of
this is in the product: the router has always taken its disk readings from outside.

    python scripts/rehearse_storage.py plain    normal to 95 % -> removable; removable to 95 % -> emergency
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import namedtuple
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from htsg import records as r  # noqa: E402
from htsg.archive import ArchiveStore  # noqa: E402
from htsg.archive_router import ArchiveRouter  # noqa: E402
from htsg.config import load_settings  # noqa: E402

HERE = ROOT / ".local" / "rehearsal"
BUDGET = 500 * 1024**2
ROLES = ("primary", "emergency", "external")
Usage = namedtuple("Usage", "total used free")


class Budgeted:
    """The router's disk reading: the real path first (a pulled-out disk raises here), then this run's database files
    in `<storage path>/pgdata/base/<oid>` against the budget.  Measured at most every half second."""

    def __init__(self, folders: dict[str, Path], budget: int):
        self.folders, self.budget, self.seen = folders, budget, {}

    def __call__(self, path: str) -> Usage:
        shutil.disk_usage(path)  # the disk and the folder are really there, or OSError - as at the site
        when, used = self.seen.get(path, (0.0, 0))
        if time.monotonic() - when > 0.5:
            used = sum(f.stat().st_size for f in self.folders[path].iterdir() if f.is_file())
            self.seen[path] = (time.monotonic(), used)
        return Usage(self.budget, used, max(0, self.budget - used))


def fresh_databases(connections: dict, stamp: str) -> dict[str, tuple[str, int]]:
    """A new database per role in its own cluster: (connection string, database oid)."""
    out = {}
    for role in ROLES:
        base = conninfo_to_dict(connections[role])
        name = f"htsg_rehearsal_{stamp}"
        with psycopg.connect(make_conninfo(**{**base, "dbname": "postgres"}), autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            oid = conn.execute("SELECT oid FROM pg_database WHERE datname=%s", (name,)).fetchone()[0]
        out[role] = (make_conninfo(**{**base, "dbname": name}), oid)
    return out


def group(run: str, order: int, point: str, n: int = 2000) -> r.CommitGroup:
    """One whole group of `n` observations of one input, with its segment start and a watermark."""
    obs = tuple(
        r.Observation(
            id=r.observation_id(run, "ua_field", order, i), run_id=run, source_id="ua_field", conn_gen=1, seg=1,
            batch_seq=order, item_seq=i, point_id=point, value=480.0 + (order * n + i) % 97 * 0.01, quality=r.GOOD,
            status_code=0, received_wall=1_800_000_000.0 + order, received_mono=100.0 + order,
        )
        for i in range(n)
    )  # fmt: skip
    states = (
        r.StateRecord(r.state_id(run, "ua_field", order * 10), run, "ua_field", 1, "SEG_START", 1_800_000_000.0, 100.0,
                      {"conn_gen": 1, "reason": "CONNECT"}),
        r.StateRecord(r.state_id(run, "ua_field", order * 10 + 1), run, "ua_field", 1, "WATERMARK", 1_800_000_000.0 + order,
                      100.0 + order, {"seg": 1, "covered_wall": 1_800_000_000.0 + order}),
    )  # fmt: skip
    return r.CommitGroup(r.group_id(run, order), order, run, "ua_field", r.GROUP_BATCH, 1_800_000_000.0 + order, obs, (), states, ())


def where_groups_are(stores) -> dict[str, dict[str, int]]:
    """group id -> {database: observation rows}; also half-written groups would show as fewer rows than written."""
    found = {}
    for role, store in stores.items():
        try:
            with store.session() as conn:
                for row in conn.execute("SELECT group_id, count(*) AS n FROM observation GROUP BY group_id").fetchall():
                    found.setdefault(row["group_id"], {})[role] = row["n"]
        except psycopg.Error as error:
            found.setdefault("?", {})[role] = repr(error)[:80]
    return found


def main(args):
    connections = json.loads((HERE / "database.json").read_text(encoding="utf-8"))
    config = load_settings(HERE / "config.json")
    assert config.mode == "production" and config.storage.external_enabled, "the rehearsal runs the site's rules"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    databases = fresh_databases(connections, stamp)
    paths = {"primary": config.storage.primary_path, "emergency": config.storage.emergency_path, "external": config.storage.external_path}
    folders = {paths[role]: Path(paths[role]) / "pgdata" / "base" / str(databases[role][1]) for role in ROLES}
    stores = {role: ArchiveStore(databases[role][0], config.timing) for role in ROLES}
    for store in stores.values():  # what deploy_databases.py install does for its own database: this archive's schema
        store.initialize(config)
    router = ArchiveRouter(config, stores["primary"], stores["emergency"], stores["external"], disk_usage=Budgeted(folders, BUDGET))
    router.initialize()
    run = f"rehearsal-{stamp}"
    router.bind_run(run, config.config_id, time.time())
    point = next(p.id for p in config.points if p.source == "ua_field")
    log, order, mode, landed, sizes = [], 0, None, {role: 0 for role in ROLES}, {}
    started = time.monotonic()

    def say(text):
        line = f"{time.strftime('%H:%M:%S')} {text}"
        print(line, flush=True)
        log.append(line)

    def reading():
        out = {}
        for role in ROLES:
            try:
                out[role] = round(router.disk_usage(paths[role]).used / BUDGET * 100, 1)
            except OSError:
                out[role] = "gone"
        return out

    say(f"run {run}; budget {BUDGET // 1024**2} MB per disk; databases htsg_rehearsal_{stamp}; disks {reading()}")
    try:
        while order < 6000:  # a cap, whatever happens
            order += 1
            g = group(run, order, point)
            sizes[g.group_id] = len(g.observations)
            while True:  # one group until it is durable: same ids, as the writer queue does
                result = router.write_group(g)
                if result.durable:
                    landed[result.target] = landed.get(result.target, 0) + 1
                    break
                say(f"group {order}: {result.status} {result.reason}; target {router.mode}; health {dict(router.health)}")
                time.sleep(1.0)
            if router.mode != mode:
                say(f"group {order}: writing to {router.mode} ({result.target}); disks {reading()}")
                mode = router.mode
            if args.what == "plain" and router.mode == "EMERGENCY" and landed.get("emergency", 0) >= 5:
                break
    except KeyboardInterrupt:
        say("stopped by the operator")
    placed = where_groups_are(stores)
    spread = {g: w for g, w in placed.items() if g != "?" and len(w) != 1}
    torn = {g: w for g, w in placed.items() if g != "?" and any(n != sizes.get(g) for n in w.values())}
    report = {
        "run": run, "what": args.what, "seconds": round(time.monotonic() - started, 1), "groups_written": order,
        "landed": landed, "disks_percent_of_budget": reading(), "final_mode": router.mode, "health": dict(router.health),
        "groups_in_more_than_one_database": len(spread), "groups_not_whole": len(torn),
        "unreadable": placed.get("?", {}), "log": log,
    }
    out = HERE / f"rehearsal-{args.what}-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "log"}, ensure_ascii=False, indent=2))
    print(f"report: {out}")
    for store in stores.values():
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=("plain",))
    main(parser.parse_args())
    sys.stdout.flush()
    os._exit(0)
