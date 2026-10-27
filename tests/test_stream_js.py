"""修复轮05 (终版 §13.3): the page's own rule for taking a reading (htsg/static/stream.js), run under Node on a real
reading of the backend.

Why it matters: the browser code decides whether a screen may show a number.  It must take a reading whole - an empty
value, a bad quality or an invalid state is a state like any other - and refuse anything that is not a reading before a
single value changes, so that a bad answer leaves the screen as it was and only the next good reading counts.
"""

import json
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from owner_support import notify, poll, small_config

from htsg import records as r
from htsg.auth import Accounts
from htsg.owner import Owner
from htsg.web import create_web_app

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).with_name("js") / "take_reading.mjs"


def run_node(tmp_path, payload):
    node = shutil.which("node")
    assert node, "Node.js is required for the JavaScript checks"
    path = tmp_path / "readings.json"
    path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    done = subprocess.run([node, str(SCRIPT), str(path)], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def a_reading(tmp_path):
    """A real reading with every kind of state in it: a valid input, one of bad quality, one never received, and a
    result that cannot be computed."""
    config = small_config()
    accounts = Accounts(tmp_path / "accounts.json")
    headers = {"Authorization": "Bearer " + accounts.create("viewer", "viewer")}
    owner = Owner(config, 1000.0, 100.0, run_id="js")
    for source in ("mb", "ua"):
        owner.begin_generation(source, 1, 1000.0, 100.0)
    owner.accept(notify([("ti", 500.0), ("to", 450.0, r.BAD, 0x80350000)], wall=1001.0, mono=101.0))
    owner.accept(poll(1, {"b1": 0.7}))  # b2 is never answered
    with TestClient(create_web_app(config=config, accounts=accounts, owner=owner)) as client:
        return client.get("/api/snapshot", headers=headers).json()


def test_a_reading_is_taken_whole_with_every_state_in_it_and_anything_else_is_refused_before_a_value_changes(tmp_path):
    good = a_reading(tmp_path)
    views = good["channels"]
    assert views["to"]["quality"] == r.BAD and not views["to"]["valid"], "the reading really holds a bad-quality input"
    assert views["b2"]["value"] is None and not views["b2"]["valid"], "... one never received"
    assert not views["dt"]["valid"], "... and a result that cannot be computed"
    without = lambda key: {k: v for k, v in good.items() if k != key}  # noqa: E731
    variants = {
        "not an object": [good],
        "no channels": without("channels"),
        "channels as a list": dict(good, channels=list(views.values())),
        "a channel without validity": dict(good, channels=dict(views, ti={k: v for k, v in views["ti"].items() if k != "valid"})),
        "a channel of no known kind": dict(good, channels=dict(views, ti=dict(views["ti"], kind="frame"))),
        "a channel that is not a state": dict(good, channels=dict(views, ti=None)),
        "no reading time": without("taken_time"),
        "a reading time that is not a number": dict(good, taken_time="now"),
        "no run": without("run_id"),
        "no status": without("status"),
    }  # fmt: skip
    out = run_node(tmp_path, {"good": good, "damaged": variants})
    assert out["taken"] == {"channels": views, "status": good["status"], "run": good["run_id"], "taken": good["taken_time"]}
    for name, verdict in out["damaged"].items():
        assert verdict == {"rejected": True, "unchanged": True}, name
