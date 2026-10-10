"""D38: the page's own message rules (htsg/static/stream.js) against real messages of the publisher.

Why it matters: the browser code is what decides whether a screen may show a number.  Run under Node with the very
messages the Python publisher produces, it must end exactly where the owner is, and it must refuse any damaged
message whole - before a single value changes.
"""

import json
import shutil
import subprocess
from pathlib import Path

from test_publisher import Rig, story

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).with_name("js") / "apply_messages.mjs"


def run_node(tmp_path, payload):
    node = shutil.which("node")
    assert node, "Node.js is required for the JavaScript checks"
    path = tmp_path / "messages.json"
    path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    done = subprocess.run([node, str(SCRIPT), str(path)], capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def test_the_page_rules_end_exactly_where_the_owner_is_and_refuse_every_damaged_message(tmp_path):
    rig = Rig()
    received = []
    rig.publisher.subscribe(received.append)  # one client: the snapshot, then everything after it
    story(rig, join_before_every_step=False)
    snapshot = rig.owner.snapshot()
    # damaged variants of one real multi-group message, each to be applied on the state just before it
    rig2 = Rig()
    rig2.step(100.5)
    base = {"type": "snapshot", **rig2.owner.snapshot()}
    for at_s in (101.00, 101.02, 101.04):
        rig2.ua(at_s, [("ti", 480.0 + at_s)])
    rig2.step(101.2)
    good = rig2.sent[-1]
    first, middle, last = good["groups"]
    gone = {k: v for k, v in last["channels"].items()}
    gone["no_such_channel"] = next(iter(gone.values()))
    variants = {
        "wrong last": dict(good, last=last["seq"] + 1),
        "wrong first": dict(good, first=first["seq"] - 1),
        "group missing inside": dict(good, groups=[first, last]),
        "out of order": dict(good, groups=[first, last, middle]),
        "group missing before": dict(good, groups=[middle, last], first=middle["seq"]),
        "empty": dict(good, groups=[], first=0, last=0),
        "unknown channel in the last group": dict(good, groups=[first, middle, dict(last, channels=gone)]),
        "a result with no validity": dict(good, groups=[first, middle, dict(last, channels={"dt": {"kind": "derived"}})]),
        "group without channels": dict(good, groups=[first, middle, {k: v for k, v in last.items() if k != "channels"}]),
        "heartbeat from a sender that is ahead": {"type": "heartbeat", "seq": base["pub_seq"] + 9, "status": {}},
        "unknown message": {"type": "invented"},
    }  # fmt: skip
    payload = {
        "messages": received,
        "damaged": {name: {"base": base, "message": message} for name, message in variants.items()},
    }
    out = run_node(tmp_path, payload)
    assert out["channels"] == json.loads(json.dumps(snapshot["channels"])), "the page ends where the owner is"
    assert out["seq"] == snapshot["pub_seq"] and out["groups"] >= 20
    for name, verdict in out["damaged"].items():
        assert verdict == {"rejected": True, "unchanged": True}, name
