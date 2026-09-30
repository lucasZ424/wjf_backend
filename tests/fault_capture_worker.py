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

    def emit(kind, payload, priority):
        if kind == "capture_plan":
            if args["stage"] == "PRE":
                return True
            plan = payload["plan"]
            store.write_batch(plan.batch(0, plan.count))
            return None
        store.write(kind, payload)

    engine = Engine(config, emit, 1000, 0)
    engine.receive("x", 2, 1000, 0)
    engine.receive("y", 2, 1000, 0)
    engine.receive("x", 10, 1000.1, 0.1)
    if args["stage"] == "POST":
        engine.receive("x", 2, 1001, 1)
        engine.tick(1006, 6)
        assert engine.active["p"].phase == "POST_CAPTURE"
    event = engine.active["p"]
    conn = store.connect()
    conn.execute(
        "INSERT INTO source_sample(id,point_id,effective_time,payload) VALUES('uncommitted','x',1,'{}')"
    )
    print(json.dumps({"id": event.id, "run_id": engine.run_id, "stage": args["stage"]}), flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
