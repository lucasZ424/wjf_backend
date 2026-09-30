"""Small real HTTP + database end-to-end check; never fills disks."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx


async def main(duration):
    credentials = json.loads(Path(".local/initial-credentials.json").read_text())
    headers = {"Authorization": "Bearer " + credentials["viewer"]}
    server = subprocess.Popen(
        [sys.executable, "-m", "htsg.cli", "run", "--port", "18080"],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:18080", timeout=5) as client:
            for _ in range(60):
                try:
                    if (await client.get("/live")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Server startup failed; inspect .local/backend.log")
            for _ in range(duration):
                await asyncio.sleep(1)
                assert (await client.get("/live")).status_code == 200
            current = (await client.get("/api/current", headers=headers)).json()
            assert current["frame"] and current["mode"] == "development"
            assert current["frame"]["derived_results"]["thermal_stress"]["value"] is None
            history = await client.get(
                "/api/history", headers=headers, params={"start": time.time() - 20, "end": time.time() + 1}
            )
            assert history.status_code == 200 and history.json()["items"]
            health = (await client.get("/api/health", headers=headers)).json()
            assert health["runtime"]["last_write"] is not None
            events = (await client.get("/api/events", headers=headers)).json()
            assert events["items"]
            report = {
                "passed": True,
                "duration_seconds": round(time.monotonic() - started, 2),
                "frame_id": current["frame"]["id"],
                "history_rows": len(history.json()["items"]),
                "events": len(events["items"]),
                "archive_dropped": health["runtime"]["archive_dropped"],
                "database_location": "D:/HTSG-dev",
                "limits": "Short smoke, not field or endurance acceptance",
            }
            Path(".local/smoke-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report))
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=4, choices=range(4, 121))
    asyncio.run(main(parser.parse_args().duration))
