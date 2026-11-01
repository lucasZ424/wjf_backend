"""The supervisor that keeps the backend running with nobody at the machine (scripts/supervise.py).

Why it matters: after a power cut or a crash the system must come back by itself (项目硬规则).  The supervisor must start the backend
again whenever it ends - with growing pauses while it keeps ending, so a backend that cannot start does not spin - never start a second
backend next to one that already owns the deployment, start the database services that are not running (a disk that was not ready at
boot), and stop everything the normal way and start nothing while the maintenance file STOP is there.
"""

import logging
import msvcrt
import signal
import subprocess
import sys
import threading
import time

import pytest

from scripts import supervise as sup

QUICK = [sys.executable, "-c", "import sys; sys.exit(3)"]
LONG = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def fast(monkeypatch, tmp_path):
    """Short settings, no services, and pauses that are recorded instead of slept."""
    monkeypatch.setattr(sup, "FIRST_DELAY", 1.0)
    monkeypatch.setattr(sup, "MAX_DELAY", 8.0)
    monkeypatch.setattr(sup, "STOP_GRACE", 10.0)
    pauses = []
    monkeypatch.setattr(sup, "pause", lambda seconds, stop_file: pauses.append(seconds))
    files = {"stop_file": tmp_path / "STOP", "backend_lock": tmp_path / "backend.lock", "console": tmp_path / "console.log"}
    return pauses, files


def test_a_backend_that_keeps_ending_is_started_again_with_growing_pauses(fast):
    pauses, files = fast
    assert sup.supervise(QUICK, [], None, max_runs=5, **files) == 5
    assert pauses == [1.0, 2.0, 4.0, 8.0], "doubling up to the maximum (5 s to 60 s in the real settings)"


def test_a_run_that_lasted_starts_the_pauses_over(fast, monkeypatch):
    pauses, files = fast
    monkeypatch.setattr(sup, "STABLE", 0.0)  # every run counts as one that lasted
    sup.supervise(QUICK, [], None, max_runs=3, **files)
    assert pauses == [1.0, 1.0]


def test_the_stop_file_ends_the_backend_and_the_supervisor_and_nothing_starts_while_it_is_there(fast):
    _, files = fast
    timer = threading.Timer(1.0, files["stop_file"].write_text, args=("maintenance",))
    timer.start()
    began = time.monotonic()
    assert sup.supervise(LONG, [], None, **files) == 1
    assert time.monotonic() - began < 20, "the backend was stopped, not waited out"
    assert sup.supervise(QUICK, [], None, **files) == 0, "while STOP is there nothing is started"


def test_a_backend_that_already_owns_the_deployment_is_waited_for_and_never_doubled(fast, monkeypatch):
    pauses, files = fast
    other = sup.lock(files["backend_lock"])  # a backend left behind by a supervisor that was ended
    assert other is not None

    def pause(seconds, stop_file):
        pauses.append(seconds)
        if len(pauses) == 3:  # that backend ends
            other.seek(0)
            msvcrt.locking(other.fileno(), msvcrt.LK_UNLCK, 1)
            other.close()

    monkeypatch.setattr(sup, "pause", pause)
    console = files["console"]
    assert sup.supervise(QUICK, [], None, max_runs=1, **files) == 1
    assert pauses[:3] == [sup.SERVICE_CHECK] * 3, "it waited while the other one held the deployment"
    assert console.exists(), "and started its own once it was free"


def test_the_backend_starts_without_the_database_once_the_wait_is_over(fast, monkeypatch):
    _, files = fast
    asked = []
    monkeypatch.setattr(sup, "WAIT_DB", 0.3)
    monkeypatch.setattr(sup, "database_answers", lambda dsn: asked.append(dsn) or False)
    assert sup.supervise(QUICK, [], "postgresql://nobody@127.0.0.1:1/x", max_runs=1, **files) == 1
    assert asked, "the database was asked before the start"


def test_stopped_database_services_are_started_every_round_and_a_change_is_logged_once(monkeypatch, caplog):
    states = {"HTSG-PostgreSQL-primary": "RUNNING", "HTSG-PostgreSQL-emergency": "STOPPED"}
    started = []
    ready = {"now": False}
    monkeypatch.setattr(sup, "service_state", lambda name: states[name])

    def start(name):
        started.append(name)
        if ready["now"]:
            states[name] = "RUNNING"

    monkeypatch.setattr(sup, "start_service", start)
    seen = {}
    with caplog.at_level(logging.INFO, logger="htsg.supervisor"):
        sup.ensure_services(list(states), seen)  # its disk is not ready yet: the database cannot start
        sup.ensure_services(list(states), seen)
        ready["now"] = True
        sup.ensure_services(list(states), seen)
    assert started == ["HTSG-PostgreSQL-emergency"] * 3, "tried every round, the running one is left alone"
    assert seen == {"HTSG-PostgreSQL-primary": "RUNNING", "HTSG-PostgreSQL-emergency": "RUNNING"}
    emergency = [r.getMessage() for r in caplog.records if "emergency" in r.getMessage()]
    assert emergency == ["service HTSG-PostgreSQL-emergency: STOPPED", "service HTSG-PostgreSQL-emergency: RUNNING"]


def free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_backend_that_never_becomes_ready_is_stopped_after_the_start_up_limit_and_started_again(fast, monkeypatch, caplog):
    """A start that hangs (the event loop's loopback socket pair never connected, 修复轮10) leaves a process alive with no
    port: waiting for it to end would wait for ever.  Past the limit it is handled like a backend that ended."""
    pauses, files = fast
    monkeypatch.setattr(sup, "STARTUP_READY", 2.0)
    monkeypatch.setattr(sup, "STOP_GRACE", 2.0)
    caplog.set_level(logging.WARNING, logger=sup.log.name)
    began = time.monotonic()
    assert sup.supervise(LONG, [], None, max_runs=2, ready_url=f"http://127.0.0.1:{free_port()}/live", **files) == 2
    assert time.monotonic() - began < 30, "each hung start was ended, not waited out"
    assert sum("仍未就绪" in r.getMessage() for r in caplog.records) == 2 and pauses, "said so, then the pause"


def test_a_backend_that_answers_in_time_is_left_running_however_long_it_runs(fast, monkeypatch, caplog):
    """Only the start is watched: once it answered, a backend is never stopped for not answering later."""
    pauses, files = fast
    monkeypatch.setattr(sup, "STARTUP_READY", 2.0)
    caplog.set_level(logging.WARNING, logger=sup.log.name)
    port = free_port()
    serve = (
        "import http.server, threading, time, sys\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "    def log_message(self, *a): pass\n"
        f"s = http.server.HTTPServer(('127.0.0.1', {port}), H)\n"
        "threading.Thread(target=s.serve_forever, daemon=True).start()\n"
        "time.sleep(4); s.shutdown()\n"  # answers for a while, then goes silent while still alive
        "time.sleep(3); sys.exit(3)\n"
    )
    assert sup.supervise([sys.executable, "-c", serve], [], None, max_runs=1, ready_url=f"http://127.0.0.1:{port}/live", **files) == 1
    assert not any("仍未就绪" in r.getMessage() for r in caplog.records)
    assert any("退出码 3" in r.getMessage() for r in caplog.records), "it ran to its own end"


def test_a_hung_start_that_ignores_the_normal_stop_is_ended_after_the_grace_and_started_again_with_growing_pauses(
    fast, monkeypatch, caplog, tmp_path
):
    """The whole chain a start that hangs goes through (修复轮10), step by step on the real process: alive and not ready,
    the start-up limit, Ctrl+Break, the grace running out, the end by force, the process gone - and the next start after a
    pause that grows while it goes on.  The fake backend says when it ignores Ctrl+Break, and the signal waits for that:
    a slow start cannot turn the forced end into a normal one.  The process started is the virtual environment's launcher,
    which runs the interpreter as a child: the interpreter itself must be gone too."""
    pauses, files = fast
    monkeypatch.setattr(sup, "STARTUP_READY", 2.0)
    monkeypatch.setattr(sup, "STOP_GRACE", 2.0)
    caplog.set_level(logging.WARNING, logger=sup.log.name)
    marks, steps = tmp_path / "marks", {}
    marks.mkdir()

    class Watched(subprocess.Popen):
        """The real process, every stop step noted."""

        stopping = False

        def send_signal(self, sig):
            ready = marks / f"{self.pid}.ready"
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), "the fake backend never said it ignores Ctrl+Break"
            steps.setdefault(self.pid, []).append("ctrl-break" if sig == signal.CTRL_BREAK_EVENT else sig)
            self.stopping = True
            super().send_signal(sig)

        def wait(self, timeout=None):
            try:
                code = super().wait(timeout)
            except subprocess.TimeoutExpired:
                if self.stopping:
                    steps[self.pid].append("grace ran out")
                raise
            if self.stopping:
                steps[self.pid].append("gone")
            return code

        def kill(self):
            steps[self.pid].append("killed")
            super().kill()

    monkeypatch.setattr(sup.subprocess, "Popen", Watched)
    deaf = [sys.executable, "-c", "import os, pathlib, signal, sys, time; signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
            "[pathlib.Path(sys.argv[1], f'{n}.ready').write_text(str(os.getpid())) for n in {os.getpid(), os.getppid()}]; "
            "time.sleep(60)", str(marks)]  # fmt: skip  # named for the launcher and for the interpreter: which one is started varies
    assert sup.supervise(deaf, [], None, max_runs=3, ready_url=f"http://127.0.0.1:{free_port()}/live", **files) == 3
    assert list(steps.values()) == [["ctrl-break", "grace ran out", "killed", "gone"]] * 3, steps
    for started in steps:
        interpreter = int((marks / f"{started}.ready").read_text())
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {interpreter}", "/NH"], capture_output=True, text=True).stdout
        assert str(interpreter) not in listing, "the interpreter the launcher ran is gone with it"
    assert sum("仍未就绪" in r.getMessage() for r in caplog.records) == 3
    assert pauses == [1.0, 2.0], "started again each time, the pause doubling while the starts keep failing"
