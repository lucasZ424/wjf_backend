"""Owned subprocess for kill/restart tests; secrets arrive only through stdin."""

import json
import sys
import time

from htsg.config import Settings
from htsg.engine import Engine
from htsg.storage import Store


def main():
    args = json.loads(sys.stdin.readline())
    config = Settings.model_validate(args["config"])
    store = Store(args["dsn"])

    faults = []

    def emit(kind, payload, priority):
        store.write(kind, payload)
        if kind == "fault":
            faults.append(payload)

    engine = Engine(config, emit, 1000, 0)
    for t in range(3 if args["stage"] == "PENDING" else 6):
        engine.receive("x", 10, 1000 + t, t)
        engine.tick(1000 + t, t)
    if args["stage"] == "RECOVERED":
        engine.receive("x", 2, 1006, 6)
    identity = faults[0]["id"] if faults else None
    conn = store.connect()
    conn.execute(
        "INSERT INTO source_sample(id,point_id,effective_time,payload) VALUES('uncommitted','x',1,'{}')"
    )
    print(json.dumps({"id": identity, "run_id": engine.run_id, "stage": args["stage"]}), flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
