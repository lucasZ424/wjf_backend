"""Prepare isolated databases on existing D: clusters, then run the research demo."""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from htsg.auth import Accounts
from htsg.cli import deployment_lock
from htsg.config import load_settings
from htsg.research_config import estimate_budget

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/research"


def prepare(config_path, *, new_lab=False):
    config = load_settings(config_path)
    if not config.simulation.enabled or config.mode != "development":
        raise SystemExit("This launcher requires a development simulation configuration")
    roles = ("primary", "emergency", "external") if config.storage.external_enabled else ("primary", "emergency")
    LOCAL.mkdir(parents=True, exist_ok=True)
    destination = LOCAL / "database.json"
    if new_lab and (LOCAL / "accounts.pending").exists():
        raise SystemExit("Reconcile pending account changes with the current database before new-lab")
    if new_lab or not destination.exists():
        original = json.loads((ROOT / ".local/database.json").read_text())
        if any(not original.get(role) for role in roles):
            raise SystemExit("Each enabled storage role requires a database binding before preparation")
        name = "htsg_research_" + uuid4().hex
        databases = {}
        # Check every enabled cluster before creating databases; never delete the old lab.
        for role in roles:
            with psycopg.connect(make_conninfo(original[role], dbname="postgres"), connect_timeout=3) as conn:
                data_path = conn.execute("SHOW data_directory").fetchone()[0]
                if not data_path.replace("\\", "/").upper().startswith("D:/"):
                    raise SystemExit("Refusing database cluster outside D:")
        for role in roles:
            with psycopg.connect(
                make_conninfo(original[role], dbname="postgres"), autocommit=True, connect_timeout=3
            ) as conn:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            databases[role] = make_conninfo(original[role], dbname=name)
        if destination.exists():
            archive = LOCAL / "archives" / uuid4().hex
            archive.mkdir(parents=True)
            (archive / "database.json").write_bytes(destination.read_bytes())
            print(f"Old database binding archived at {archive}; old databases were retained.")
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(databases), encoding="utf-8")
        temporary.replace(destination)
    databases = json.loads(destination.read_text())
    if any(not databases.get(role) for role in roles):
        raise SystemExit("Existing lab lacks an enabled storage binding; configure it before starting")
    if databases.get("external") and "external" not in roles:
        roles = (*roles, "external")  # Retained archives still count toward the development cap.
    used_bytes = 0
    for role in roles:
        with psycopg.connect(databases[role], connect_timeout=3) as conn:
            path, name = conn.execute(
                "SELECT current_setting('data_directory'),current_database()"
            ).fetchone()
            if (
                not path.replace("\\", "/").upper().startswith("D:/")
                or not name.startswith("htsg_research_")
                or len(name) != 46
            ):
                raise SystemExit("Refusing a non-D: or non-research database")
            used_bytes += conn.execute(
                "SELECT coalesce(sum(pg_total_relation_size(relid)),0)::bigint FROM pg_stat_user_tables"
            ).fetchone()[0]
    accounts = Accounts(LOCAL / "accounts.json")
    credentials = {}
    for role in ("admin", "viewer"):
        if role not in accounts.users:
            credentials[role] = accounts.create(role, role)
    if credentials:
        path = LOCAL / "initial-credentials.json"
        old = json.loads(path.read_text()) if path.exists() else {}
        path.write_text(json.dumps({**old, **credentials}), encoding="utf-8")
    secret_path = LOCAL / "simulation-secret.txt"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    estimate = estimate_budget(config)
    remaining = max(0, config.storage.development_budget_bytes - used_bytes)
    print(json.dumps({**estimate, "existing_bytes": used_bytes, "remaining_bytes": remaining}))
    if remaining == 0:
        print("WARNING: Development database budget exhausted. History can be viewed, "
              "but new recall events cannot be archived. Stop the backend and run new-lab.")
    elif remaining < estimate["validation_bytes_estimate"]:
        print("WARNING: Remaining database budget is below the estimate for one full recall cycle. "
              "Archive may stop during the cycle. Stop the backend and run new-lab for a fresh lab.")
    else:
        print("Remaining budget exceeds the startup estimate only; runtime storage guards still apply.")
    print("Research databases prepared on D:. Credentials: .local/research/initial-credentials.json")
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "new-lab"))
    parser.add_argument("--config", default=str(ROOT / "config/research-sim.json"))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    config_path = str(Path(args.config).resolve())
    with deployment_lock(ROOT / ".local/backend.lock"):
        config = prepare(config_path, new_lab=args.command == "new-lab")
        if args.command != "run":
            return
        run(config_path, config, args.port)


def run(config_path, config, port):
    os.chdir(ROOT)
    os.environ[config.simulation.token_env] = (LOCAL / "simulation-secret.txt").read_text().strip()
    log = (LOCAL / "simulation.log").open("w", encoding="utf-8")
    simulator = subprocess.Popen(
        [sys.executable, "-m", "htsg.simulation", "--config", config_path],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        ready = False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if simulator.poll() is not None:
                raise SystemExit("Simulator exited; inspect .local/research/simulation.log")
            try:
                request = Request(
                    f"http://{config.simulation.control_host}:{config.simulation.control_port}/status",
                    headers={"Authorization": "Bearer " + os.environ[config.simulation.token_env]},
                )
                with urlopen(request, timeout=1) as response:
                    status = json.load(response)
                if status.get("config_id") != config.config_id:
                    raise SystemExit("Simulation configuration mismatch")
                time.sleep(0.3)
                if simulator.poll() is not None:
                    raise SystemExit("Simulator exited during readiness check")
                ready = True
                break
            except OSError:
                time.sleep(0.2)
        if not ready:
            raise SystemExit("Simulator startup timed out; inspect .local/research/simulation.log")
        sys.argv = [
            "htsg",
            "run",
            "--config",
            config_path,
            "--database",
            str(LOCAL / "database.json"),
            "--accounts",
            str(LOCAL / "accounts.json"),
            "--port",
            str(port),
        ]
        from htsg.cli import main as run_backend

        run_backend(deployment_locked=True)
    finally:
        simulator.terminate()
        try:
            simulator.wait(timeout=5)
        except subprocess.TimeoutExpired:
            simulator.kill()
            simulator.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    main()
