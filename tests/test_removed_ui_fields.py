"""The history windows of the old in-page charts are removed from the configuration (修复轮07, user ruling 2026-10-28;
终版 §18.1).

Why it matters: a field nothing reads tells whoever edits the site configuration that it does something.  Like the
removed push fields (修复轮05), a configuration still carrying it is refused by name and never silently ignored, so a
configuration written for the old pages shows itself when it is checked.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from htsg.config import Settings, load_settings

ROOT = Path(__file__).resolve().parents[1]
BASE = {"sources": [{"id": "s", "kind": "modbus"}], "points": [], "derived": []}


def test_a_configuration_still_carrying_the_history_windows_is_refused_by_name():
    with pytest.raises(ValidationError) as refused:
        Settings.model_validate({**BASE, "ui": {"history_windows": [60, 300, 900]}})
    assert "ui.history_windows" in str(refused.value) and "Extra inputs are not permitted" in str(refused.value)


def test_the_research_configuration_no_longer_carries_them_and_still_loads():
    path = ROOT / "config" / "research-sim.json"
    assert "history_windows" not in json.loads(path.read_text(encoding="utf-8"))["ui"]
    config = load_settings(path)
    assert config.ui.stations and config.ui.process_points and config.ui.point_labels, "what the pages read is still there"
