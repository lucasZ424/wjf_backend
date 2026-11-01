"""修复轮15 (用户 11-01): curves are coloured by kind of quantity, and a stretch beyond a limit stands out.  The replay chart
colours a value beyond its channel's high or low limit with the page's own rule (htsg/static/replay.js); it must judge a
value exactly as the fault tracker does - strictly beyond, equal is normal - or the chart would show limits being crossed
where the backend says none were."""

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = """
import {beyondLimit} from "%s";
const cases = JSON.parse(process.argv[1]);
console.log(JSON.stringify(cases.map(([d, v]) => beyondLimit(d, v))));
"""


def judge(cases):
    node = shutil.which("node")
    assert node, "Node.js is required for the JavaScript checks"
    script = CHECK % (ROOT / "htsg/static/replay.js").as_uri()
    done = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(cases)], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def test_the_replay_chart_judges_a_limit_as_the_fault_tracker_does():
    wall = {"high": 490, "low": None}
    difference = {"high": 55, "low": -55}
    process = {"high": None, "low": 925}
    none = {"high": None, "low": None}
    assert judge([[wall, 490.1], [wall, 490], [wall, -1000]]) == [True, False, False], "strictly above a high limit"
    assert judge([[difference, -55.1], [difference, -55], [difference, 55.1]]) == [True, False, True], "either side"
    assert judge([[process, 924.9], [process, 3000]]) == [True, False], "a low limit alone"
    assert judge([[none, 1e9], [wall, None]]) == [False, False], "no limit, or no value: never coloured"


def test_the_chart_and_the_fault_tracker_agree_on_every_case():
    """The other side of the agreement: the same values through the backend's own judgment give the same answers."""
    from htsg.fault_tracker import FaultTracker

    rules = {"wall": {"high": 490, "low": None}, "difference": {"high": 55, "low": -55}, "process": {"high": None, "low": 925}}
    tracker = FaultTracker(rules)
    cases = [(name, v) for name, values in (("wall", (489.9, 490, 490.1)), ("difference", (-55.1, -55, 55, 55.1)),
                                            ("process", (924.9, 925, 925.1))) for v in values]  # fmt: skip
    backend = [tracker.direction(name, v) is not None for name, v in cases]
    assert judge([[rules[name], v] for name, v in cases]) == backend
