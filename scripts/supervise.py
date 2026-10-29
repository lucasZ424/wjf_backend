"""Keep the backend running with nobody at the machine (项目硬规则：断电、断联之后以最简单、稳定的方式重新投入运行).

    python scripts/supervise.py [--input .local/deploy.json] [--port 8000]

The task scheduler starts it at boot and again every five minutes (scripts/deploy_databases.py register); a copy that finds another
one running exits at once.  For as long as it lives it
  * starts every HTSG PostgreSQL service of the deployment that is not running - at once and every minute after, so the database of
    a disk that came late (one that was not ready at boot) is started once the disk is there;
  * waits until the normal database answers, at most three minutes: the backend also runs without it and writes the archive once it
    answers, but the close-out of a crashed run is only done by a start that can read every database;
  * runs the backend, and starts it again whenever it ends, for whatever reason: after 5 s, doubling while it keeps ending quickly up
    to 60 s, back to 5 s once a run has lasted ten minutes.
A backend that already owns the deployment (one left behind by a supervisor that was killed) is waited for, never doubled.
The file .local/STOP stops the backend the normal way (Ctrl+Break: its run ends NORMAL) and ends the supervisor; while the file exists
nothing is started.  Delete it to resume: the next five-minute trigger starts the supervisor again.
Its own log is .local/supervisor.log (bounded); the backend keeps writing .local/backend.log, its console goes to .local/backend-console.log.
"""

from __future__ import annotations

import argparse
import logging
import msvcrt
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

LOCAL = ROOT / ".local"
WAIT_DB = 180.0  # seconds a start waits for the normal database
FIRST_DELAY, MAX_DELAY = 5.0, 60.0  # between a backend's end and its next start
STABLE = 600.0  # a run that lasted this long starts the delays over
SERVICE_CHECK = 60.0  # seconds between two looks at the database services
STOP_GRACE = 60.0  # seconds the backend gets to stop the normal way before it is ended
log = logging.getLogger("htsg.supervisor")


def lock(path: Path):
    """An exclusive lock on byte 0 of `path`, held while the returned handle is open (the system lets go when the process ends);
    None when another process holds it.  The backend locks .local/backend.lock the same way."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - held open on purpose
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def held_elsewhere(path: Path) -> bool:
    handle = lock(path)
    if handle is None:
        return True
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    handle.close()
    return False


def service_state(name: str) -> str:
    from deploy_databases import service_state as state

    return state(name)


def start_service(name: str):
    subprocess.run(["sc", "start", name], capture_output=True)


def ensure_services(services, seen: dict):
    """Start what is stopped; say so only when a service's state changes, not every minute."""
    for name in services:
        state = service_state(name)
        if state == "STOPPED":
            start_service(name)
            state = service_state(name)
        if seen.get(name) != state:
            (log.info if state in ("RUNNING", "START_PENDING") else log.warning)("service %s: %s", name, state)
            seen[name] = state


def database_answers(dsn: str) -> bool:
    import psycopg

    try:
        psycopg.connect(dsn, connect_timeout=3).close()
    except psycopg.Error:
        return False
    return True


def pause(seconds: float, stop_file: Path):
    """Sleep, but notice the STOP file within a second."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop_file.exists():
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def wait_for_database(dsn: str | None, stop_file: Path):
    if dsn is None:
        return
    deadline = time.monotonic() + WAIT_DB
    while not database_answers(dsn):
        if time.monotonic() >= deadline:
            log.warning("the normal database did not answer within %.0f s; the backend starts without it", WAIT_DB)
            return
        if stop_file.exists():
            return
        pause(2.0, stop_file)


def stop_backend(proc: subprocess.Popen):
    """Ctrl+Break first (the backend stops its acquisition, writes what it has and ends its run NORMAL); ended if it does not."""
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        proc.wait(timeout=STOP_GRACE)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        proc.wait()


def watch(proc: subprocess.Popen, services, seen: dict, stop_file: Path) -> int | None:
    """The backend's exit code, or None when it was stopped because of the STOP file."""
    last_check = time.monotonic()
    while True:
        try:
            return proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        if stop_file.exists():
            log.info("STOP found: stopping the backend")
            stop_backend(proc)
            return None
        if time.monotonic() - last_check >= SERVICE_CHECK:
            ensure_services(services, seen)
            last_check = time.monotonic()


def supervise(command: list[str], services, primary_dsn: str | None, *, stop_file: Path, backend_lock: Path,
              console: Path, max_runs: int | None = None) -> int:  # fmt: skip
    """Run the backend again and again until the STOP file appears.  The number of runs started is returned."""
    seen: dict = {}
    runs, next_delay, waiting_logged = 0, FIRST_DELAY, False
    while not stop_file.exists():
        ensure_services(services, seen)
        if held_elsewhere(backend_lock):  # a backend nobody supervises: let it be, look again later
            if not waiting_logged:
                log.warning("another backend owns %s; waiting for it to end", backend_lock)
                waiting_logged = True
            pause(SERVICE_CHECK, stop_file)
            continue
        waiting_logged = False
        wait_for_database(primary_dsn, stop_file)
        if stop_file.exists():
            break
        started = time.monotonic()
        with open(console, "w", encoding="utf-8") as out:  # one run's console at a time: bounded
            proc = subprocess.Popen(
                command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )  # fmt: skip
            runs += 1
            log.info("backend started (pid %s, start %s)", proc.pid, runs)
            code = watch(proc, services, seen, stop_file)
        if code is None:
            break
        ran = time.monotonic() - started
        delay = FIRST_DELAY if ran >= STABLE else next_delay
        next_delay = min(delay * 2, MAX_DELAY)
        log.warning("backend ended with code %s after %.0f s; next start in %.0f s", code, ran, delay)
        if max_runs is not None and runs >= max_runs:
            break
        pause(delay, stop_file)
    log.info("supervisor ends (STOP present: %s)", stop_file.exists())
    return runs


def main(argv=None):
    from deploy_databases import read_input

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=LOCAL / "deploy.json", help="the deployment file; default .local/deploy.json")
    parser.add_argument("--accounts", type=Path, default=LOCAL / "accounts.json")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    LOCAL.mkdir(exist_ok=True)
    handler = RotatingFileHandler(LOCAL / "supervisor.log", maxBytes=1024**2, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])
    own = lock(LOCAL / "supervisor.lock")
    if own is None:
        return  # another supervisor is at work
    stop_file = LOCAL / "STOP"
    if stop_file.exists():
        log.info("STOP present: nothing is started")
        return
    inp = read_input(args.input)
    services = [f"HTSG-PostgreSQL-{role}" for role in inp["volumes"]]
    from deploy_databases import read_connections

    primary = (read_connections(inp["database_file"]) or {}).get("primary")
    command = [
        sys.executable, "-m", "htsg.cli", "run", "--config", str(inp["config"]), "--database", str(inp["database_file"]),
        "--accounts", str(args.accounts), "--port", str(args.port),
    ]  # fmt: skip
    log.info("supervisor starts: services %s", ", ".join(services))
    supervise(command, services, primary, stop_file=stop_file, backend_lock=LOCAL / "backend.lock", console=LOCAL / "backend-console.log")


if __name__ == "__main__":
    main()
