"""Watch the RUNNING research lab while ONE scenario of the simulated field is triggered, and print the backend's answer as a timeline.

    python scripts/watch_lab.py --scenario disconnect_opcua --at 10 --seconds 75 --out DIR

The lab must be running (`scripts/research_lab.py run`).  This script only READS the backend (`/api/health`, `/api/replay`, with the
development login) and the simulator (`/status`); it starts the scenario through the same `trigger()` call as scripts/trigger_scenario.py,
`--at` seconds after it began watching.  A line is printed whenever a source's state, alarm or counts, a watched input's validity, quality
or status code, or the simulator's scenario or servers change; communication-log entries are printed as they appear and grow.  At the
end the history API is asked for the watched channels over the whole run (segments and rows: where the history is cut).

Times are seconds from the start of watching.  `POST /scenario` returning says only that the request was accepted; the line `SIMULATOR`
says when the simulator reported the scenario and its servers, which is when the injection can be said to have taken effect.
Nothing is written to the backend or to a device.  Not part of the candidate (scripts/ is outside the code digest).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import trigger_scenario as trig  # noqa: E402

from htsg.config import load_settings  # noqa: E402

INPUTS = "bridge_mvv_01,temp_inner_01,load_mw"
CHANNELS = "bridge_mvv_01,strain_01,strain_change_01,temp_inner_01,delta_t_01,load_mw"


class Lab:
    def __init__(self, backend: str, config, token: str):
        self.backend, self.sim, self.token = backend, config.simulation, token
        self.t0 = time.monotonic()
        self.t0_wall = time.time()
        self.lines, self.seen_log = [], {}

    def now(self) -> float:
        return time.monotonic() - self.t0

    def say(self, text: str):
        line = f"{self.now():+8.2f}  {text}"
        self.lines.append(line)
        print(line, flush=True)

    def get(self, url: str, headers: dict) -> dict:
        with urlopen(Request(url, headers=headers), timeout=5) as response:
            return json.load(response)

    def health(self) -> dict:
        return self.get(f"{self.backend}/api/health", {"Authorization": "Bearer admin:admin"})

    def simulator(self) -> dict:
        return self.get(f"http://{self.sim.control_host}:{self.sim.control_port}/status", {"Authorization": "Bearer " + self.token})

    def replay(self, points: str, seconds: float) -> dict:
        query = f"center={self.t0_wall + seconds / 2 - 5}&seconds={seconds + 10}&points={points}"
        return self.get(f"{self.backend}/api/replay?{query}", {"Authorization": "Bearer admin:admin"})


def picture(health: dict, sim: dict, watch: list[str], now_wall: float) -> tuple[str, str]:
    parts = []
    for sid, s in health["status"]["sources"].items():
        parts.append(f"{sid}={s['state']} g{s['conn_gen']} resync{s['resync_count']} cf{s['connect_failures']}{' ALARM' if s['alarm'] else ''}")
    rows = {r["point_id"]: r for r in health["communication"]["inputs"]}
    invalid = sum(1 for r in rows.values() if not r["valid"])
    parts.append(f"invalid_inputs={invalid}/{len(rows)}")
    shown = []
    for pid in watch:
        r = rows[pid]
        parts.append(f"{pid}={'ok' if r['valid'] else 'NO ' + str(r['reason'])} q={r['quality']} sc={r['status_code']}")
        shown.append(f"{pid}={r['value']} src_time={'-' if r['source_time'] is None else round(r['source_time'] - now_wall, 1)}")
    parts.append(f"SIMULATOR scenario={sim['scenario']} modbus={'up' if sim['servers']['modbus'] else 'DOWN'} opcua={'up' if sim['servers']['opcua'] else 'DOWN'}")
    return " | ".join(parts), "  ".join(shown)


def runs(response: dict, t0_wall: float) -> dict:
    rel = lambda wall: None if wall is None else round(wall - t0_wall, 2)  # noqa: E731
    out = {}
    for pid, channel in response.get("channels", {}).items():
        out[pid] = []
        for seg in channel.get("segments", []):
            found = []
            for row in seg.get("rows", []):
                state = "ok" if row["valid"] else str(row.get("quality") or row.get("reason") or row.get("status"))
                if row.get("status_code"):
                    state += f"/{row['status_code']}"
                if found and found[-1][3] == state:
                    found[-1][1], found[-1][2] = rel(row["time"]), found[-1][2] + 1
                else:
                    found.append([rel(row["time"]), rel(row["time"]), 1, state])
            out[pid].append({"seg": seg["seg"], "start": rel(seg["start_wall"]), "end": rel(seg["end_wall"]), "end_reason": seg["end_reason"], "rows": found})
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True, help="the scenario to trigger (see scripts/trigger_scenario.py --list)")
    parser.add_argument("--at", type=float, default=10, help="seconds of watching before the scenario is triggered")
    parser.add_argument("--seconds", type=float, default=75, help="how long to watch in all")
    parser.add_argument("--watch", default=INPUTS, help="input points shown on every line")
    parser.add_argument("--channels", default=CHANNELS, help="channels asked of the history API at the end")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, help="directory that receives timeline.txt and evidence.json")
    args = parser.parse_args(argv)
    config = load_settings(ROOT / trig.DEFAULT_CONFIG)
    if args.scenario not in {s.name for s in config.simulation.scenarios}:
        raise SystemExit(f"Unknown scenario {args.scenario!r}")
    token = (trig.LOCAL / "simulation-secret.txt").read_text().strip()
    lab = Lab(args.backend, config, token)
    watch, last, triggered, accepted = args.watch.split(","), None, False, None
    while lab.now() < args.seconds:
        if not triggered and lab.now() >= args.at:
            triggered = True
            lab.say(f"TRIGGER {args.scenario} (POST /scenario)")
            accepted = trig.trigger(config, token, args.scenario)
            lab.say(f"ACCEPTED by the control interface: scenario={accepted['scenario']} (not yet applied to the servers)")
        health, sim = lab.health(), lab.simulator()
        change, shown = picture(health, sim, watch, time.time())
        if change != last:
            lab.say(f"{change}  [{shown}]")
            last = change
        for entry in health["communication"]["log"]["entries"]:
            key = (entry["count"], entry["state"], entry["end_reason"])
            if lab.seen_log.get(entry["seq"]) != key:
                lab.seen_log[entry["seq"]] = key
                silent = entry["silent_seconds"]
                lab.say(
                    f"LOG #{entry['seq']} {entry['source_id']} {entry['event']} reason={entry['reason']} gen={entry['conn_gen']} scope={entry['scope']} "
                    f"count={entry['count']} state={entry['state']} end={entry['end_reason']} silent={silent if silent is None else round(silent, 2)}s detail={entry['detail']}"
                )
        time.sleep(0.4)
    history = runs(lab.replay(args.channels, args.seconds), lab.t0_wall)
    lab.say("HISTORY (rows as [first t, last t, count, state]; a new row is stored when the value or its state changes):")
    for pid, segments in history.items():
        for seg in segments:
            lab.say(f"  {pid} seg{seg['seg']} {seg['start']}..{seg['end']} {seg['end_reason']}: " + " ".join(str(r) for r in seg["rows"]))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "timeline.txt").write_text("\n".join(lab.lines) + "\n", encoding="utf-8")
        git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()  # noqa: E731
        evidence = {
            "command": " ".join(["watch_lab.py", *(sys.argv[1:] if argv is None else argv)]), "commit": git("rev-parse", "HEAD"),
            "dirty_candidate_files": git("status", "--porcelain", "--", "htsg", "config"), "started_wall": lab.t0_wall,
            "scenario": args.scenario, "accepted": accepted, "simulator_final": lab.simulator(), "history": history,
            "sources": lab.health()["status"]["sources"],
        }
        (args.out / "evidence.json").write_text(json.dumps(evidence, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"timeline.txt and evidence.json written to {args.out}")


if __name__ == "__main__":
    main()
