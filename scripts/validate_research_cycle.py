"""Real-clock research capture verification against running loopback services.

Reads local credentials without printing them. Does not initialize/reset databases,
alter capacity budgets, inject engine observations or accelerate the clock.
"""

import argparse
import json
import math
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from htsg.config import load_settings  # noqa: E402


def request(base, path, token, body=None, params=None):
    url = base + path + ("?" + urlencode(params) if params else "")
    payload = json.dumps(body).encode() if body is not None else None
    with urlopen(
        Request(
            url,
            data=payload,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        ),
        timeout=15,
    ) as response:
        return json.load(response)


def all_pages(base, path, token, params=None):
    result = []
    query = {"limit": 1000, **(params or {})}
    for _ in range(200):
        page = request(base, path, token, params=query)
        assert not page["partial"], "history unavailable during validation"
        result.extend(page["items"])
        if not page.get("next_cursor"):
            assert not page.get("has_more"), "incomplete history pagination"
            return result
        query["cursor"] = page["next_cursor"]
    raise AssertionError("history exceeds bounded validation page budget")


def check_health(health, config):
    minimum_free = min(
        shutil.disk_usage(path).free for path in (config.storage.primary_path, config.storage.emergency_path)
    )
    assert minimum_free >= 1024**3, "D drive reserve below 1 GiB"
    for key in ("ingress_dropped", "archive_dropped", "write_failures"):
        assert health["runtime"].get(key, 0) == 0, f"runtime {key}"
    assert not health["runtime"].get("fatal_worker"), "fatal worker"
    assert health["routing"] == "PRIMARY", "unexpected storage routing"
    assert not health["history_archive"]["suspended"], "history archive suspended"
    assert health["queues"]["capture_bytes"] <= config.limits.capture_budget_bytes
    budget = health.get("storage_budget")
    if budget:
        budget["minimum_free_bytes"] = minimum_free
        assert budget["bytes_written"] < budget["budget_bytes"], "writer budget exhausted"
        assert budget["development_growth"] < budget["budget_bytes"], "database growth budget exhausted"
        if budget.get("minimum_free_bytes") is not None:
            assert budget["minimum_free_bytes"] >= 1024**3, "D drive reserve below 1 GiB"
    return budget


def check_frame(frame, config):
    assert frame and frame["config_id"] == config.config_id, "running configuration differs"
    models = {m.id: m for m in config.models}
    count = 0
    for derived in config.derived:
        if derived.operation != "thermal_stress":
            continue
        output = frame["derived_results"][derived.id]
        if output["status"] != "OK":
            continue
        inputs = [frame["values"][p]["value"] for p in derived.inputs]
        model = models[derived.model_id]
        # Independent direct arithmetic, not a call into the production calculator.
        expected = model.young_mpa * model.expansion_per_k * model.restraint * (inputs[0] - inputs[1])
        assert math.isclose(output["value"], expected, rel_tol=1e-10, abs_tol=1e-8)
        assert output["estimate"]["qualification"] == "ESTIMATED"
        count += 1
    assert count == sum(d.operation == "thermal_stress" for d in config.derived), "thermal inputs unavailable"
    return count


def check_ledger(communications, ledger):
    compared = []
    for source in communications["sources"]:
        assert source["connected"], f"source disconnected: {source['id']}"
        for point in source["points"][:2]:
            assert point["quality"] == "GOOD", f"point unavailable: {point['id']}"
            received = point["received_at"]
            candidates = [
                entry["values"][point["id"]]
                for entry in ledger["items"]
                if -0.05 <= received - entry["time"] <= 4
            ]
            assert any(math.isclose(point["value"], x["value"], abs_tol=1e-3) for x in candidates), (
                f"source ledger mismatch: {point['id']}"
            )
            compared.append(point["id"])
    return compared


def verify_event(base, token, event, profile, config):
    assert event["phase"] == "CLOSED" and event["archive_status"] == "SUBMITTED"
    assert event["close_reason"] == "RECOVERED", event["close_reason"]
    assert event["coverage"] == "OBSERVED", "event coverage is partial"
    assert math.isclose(event["triggered_at"] - event["start"], profile.pre, abs_tol=0.01)
    assert profile.post <= event["end"] - event["recovered_at"] < profile.post + 3
    assert profile.recovery <= event["post_mono"] - event["recovery_mono"] < profile.recovery + 3
    identity = event["id"]
    gaps = all_pages(base, f"/api/events/{identity}/gaps", token)
    assert not gaps, "event has recorded gaps"
    samples = [r["payload"] for r in all_pages(base, f"/api/events/{identity}/samples", token)]
    products = all_pages(base, f"/api/events/{identity}/products", token)
    assert products and all(not p["results_removed"] for p in products)
    ranges = {}
    for point in profile.source_points:
        rows = [r for r in samples if r["point_id"] == point]
        assert rows and len({r["id"] for r in rows}) == len(rows)
        first = min(r["effective_time"] for r in rows)
        last = max(r["effective_time"] for r in rows)
        assert first <= event["start"] + 3 and last >= event["end"] - 3, f"capture span missing: {point}"
        ranges[point] = {"count": len(rows), "first": first, "last": last}
    history = all_pages(
        base,
        "/api/history",
        token,
        {
            "event_id": identity,
            "start": event["start"],
            "end": event["end"],
        },
    )
    expected_points = set(profile.source_points) | set(profile.derived_points)
    assert {r["point_id"] for r in history} == expected_points
    frames = {}
    for row in history:
        frames.setdefault(row["id"], {"config_id": config.config_id, "values": {}, "derived_results": {}})[
            "values" if row["product"] == "source" else "derived_results"
        ][row["point_id"]] = row["payload"]
    checked = 0
    for frame in frames.values():
        for rule in config.derived:
            if rule.operation != "thermal_stress" or rule.id not in profile.derived_points:
                continue
            output = frame["derived_results"][rule.id]
            assert output["status"] == "OK", "event model unavailable"
            model = next(m for m in config.models if m.id == rule.model_id)
            inner, outer = [frame["values"][p]["value"] for p in rule.inputs]
            assert math.isclose(
                output["value"],
                model.young_mpa * model.expansion_per_k * model.restraint * (inner - outer),
                abs_tol=1e-8,
            )
            checked += 1
    return {
        "event": event,
        "source_ranges": ranges,
        "sample_count": len(samples),
        "product_count": len(products),
        "history_rows": len(history),
        "checked_stress_frames": checked,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/research-sim.json")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--local", default=".local/research")
    parser.add_argument("--output", default="docs/research-cycle-result.json")
    args = parser.parse_args()
    assert urlparse(args.base).hostname == "127.0.0.1", "validation only targets loopback"
    config = load_settings(args.config)
    local = Path(args.local)
    token = json.loads((local / "initial-credentials.json").read_text())["admin"]
    sim_token = (local / "simulation-secret.txt").read_text().strip()
    sim_base = f"http://{config.simulation.control_host}:{config.simulation.control_port}"
    profile = config.profiles[0]
    duration = config.simulation.validation_duration_seconds
    warmup = profile.pre + 5
    assert duration >= warmup + 30 + profile.recovery + profile.post + 20
    output = Path(args.output)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "config_id": config.config_id,
        "real_clock": True,
        "duration_requested": duration,
        "warmup_seconds": warmup,
        "status": "RUNNING",
        "checkpoints": [],
        "scope": "simulated protocols; not plant performance acceptance",
    }
    started = time.monotonic()
    trigger_time = None
    selected = None
    try:
        assert not request(args.base, "/api/current", token)["active_events"], "preexisting active event"
        request(args.base, "/api/simulation", token, {"scenario": "baseline"})
        last_checkpoint = -60
        while time.monotonic() - started < duration:
            elapsed = time.monotonic() - started
            health = request(args.base, "/api/health", token)
            budget = check_health(health, config)
            snapshot = request(args.base, "/api/current", token)
            model_count = check_frame(snapshot["frame"], config)
            if trigger_time is None and elapsed >= warmup:
                request(args.base, "/api/simulation", token, {"scenario": "thermal_step"})
                trigger_time = time.time()
                report["scenario_triggered_at"] = trigger_time
                print(f"THERMAL_STEP at elapsed {elapsed:.1f}s", flush=True)
            if trigger_time is not None:
                events = request(args.base, "/api/events", token, params={"limit": 20})["items"]
                selected = next(
                    (
                        x["payload"]
                        for x in events
                        if x["payload"]["triggered_at"] >= trigger_time - 1
                        and x["payload"]["profile_id"] == profile.id
                    ),
                    selected,
                )
            if elapsed - last_checkpoint >= 60:
                communications = request(args.base, "/api/communications", token)
                ledger = request(sim_base, "/ledger", sim_token)
                compared = check_ledger(communications, ledger)
                checkpoint = {
                    "elapsed_seconds": round(elapsed, 2),
                    "frame_time": snapshot["frame"]["window_end"],
                    "checked_models": model_count,
                    "source_ledger_points": compared,
                    "queues": health["queues"],
                    "storage_budget": budget,
                    "event_phase": selected["phase"] if selected else None,
                }
                report["checkpoints"].append(checkpoint)
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(
                    f"elapsed={elapsed:.0f}s phase={checkpoint['event_phase']} queues={health['queues']}",
                    flush=True,
                )
                last_checkpoint = elapsed
            if (
                not report.get("capture")
                and selected
                and selected["phase"] == "CLOSED"
                and selected["archive_status"] == "SUBMITTED"
            ):
                if all(health["queues"][k] == 0 for k in ("event", "capture_jobs")):
                    report["capture"] = verify_event(args.base, token, selected, profile, config)
                    # Retain the requested real-clock observation duration after closure.
            time.sleep(min(15, max(0.1, duration - (time.monotonic() - started))))
        assert report.get("capture"), "event did not close and archive within validation duration"
        report["final_health"] = request(args.base, "/api/health", token)
        check_health(report["final_health"], config)
        report["status"] = "PASSED"
    except Exception as error:
        report["status"] = "FAILED"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["ended_at"] = datetime.now(UTC).isoformat()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{report['status']}: {output}; elapsed={report['elapsed_seconds']}s", flush=True)
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
