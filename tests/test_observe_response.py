"""The anomaly-response observation tool (scripts/observe_response.py): one situation named in a file, never a sequence.

Why it matters: it is how an abnormal situation is made on the real protocol servers and read off the real backend, so it must refuse a
bad file or an unknown situation BEFORE any server or throw-away database is created, and every situation it lists must be one it can make.
"""

import json
import re
from pathlib import Path

import pytest

from scripts import observe_response as obs

SOURCE = Path(obs.__file__).read_text(encoding="utf-8")


def test_an_unknown_situation_is_refused_before_anything_starts(tmp_path):
    path = tmp_path / "observe.json"
    path.write_text(json.dumps({"situation": "meteor_strike"}))
    with pytest.raises(SystemExit, match="situation"):
        obs.read_config(path)


def test_a_missing_or_broken_file_says_what_is_wrong(tmp_path):
    with pytest.raises(SystemExit, match="No configuration"):
        obs.read_config(tmp_path / "nope.json")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(SystemExit, match="not valid JSON"):
        obs.read_config(tmp_path / "bad.json")


def test_every_listed_situation_is_made_by_the_tool():
    """A situation that is listed but never handled would run, print nothing special and look like a pass."""
    handled = set(re.findall(r'case == "(\w+)"|case in \(([^)]*)\)', SOURCE))
    names = {n for pair in handled for part in pair for n in re.findall(r"\w+", part)}
    assert set(obs.SITUATIONS) <= names, set(obs.SITUATIONS) - names
