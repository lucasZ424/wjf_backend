"""Child process for crash tests: runs the owner + router write path and dies abruptly (no cleanup).

usage: crash_worker.py <mode> <run_id> <kill_at_group>
  modes: before_tx | in_tx | after_commit | none
DSNs arrive through the environment so credentials never appear on a command line.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from owner_support import small_config  # noqa: E402
from test_archive_router import Disks  # noqa: E402

from htsg import records as r  # noqa: E402
from htsg.archive import ArchiveStore  # noqa: E402
from htsg.archive_router import ArchiveRouter  # noqa: E402
from htsg.owner import Owner  # noqa: E402


def main():
    mode, run_id, kill_at = sys.argv[1], sys.argv[2], int(sys.argv[3])
    dsns = json.loads(os.environ["HTSG_CRASH_DSNS"])
    base_wall = float(os.environ.get("HTSG_CRASH_WALL", "1000"))
    cfg = small_config()
    primary, emergency = ArchiveStore(dsns["primary"]), ArchiveStore(dsns["emergency"])
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=Disks())
    router.initialize(run_id)
    router.bind_run(run_id, cfg.config_id, base_wall)

    groups = []
    owner = Owner(cfg, base_wall, 100.0, commit=groups.append, publish=lambda p: None, run_id=run_id)
    owner.start(base_wall, 100.0)
    owner.begin_generation("ua", 1, base_wall, 100.0)
    for n in range(1, 6):
        wall, mono = base_wall + n, 100.0 + n
        batch = r.InputBatch(
            "ua", 1, r.KIND_UA_NOTIFY, wall, mono,
            (r.Item("ti", 480.0 + n), r.Item("to", 450.0)), publish_seq=n,
        )  # fmt: skip
        owner.accept(batch)
        owner.tick(wall, mono)

    original = primary._write_members
    for index, group in enumerate(groups, start=1):
        if index == kill_at and mode == "before_tx":
            os._exit(9)
        if index == kill_at and mode == "in_tx":

            def die_inside(conn, g, original=original):
                original(conn, g)  # every INSERT has been issued, COMMIT has not
                os._exit(9)

            primary._write_members = die_inside
        result = router.write_group(group)
        assert result.durable, result
        if index == kill_at and mode == "after_commit":
            os._exit(9)  # durable, but the owner / writer never learned it
    sys.exit(0)


if __name__ == "__main__":
    main()
