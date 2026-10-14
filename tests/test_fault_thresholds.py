"""D04: the research point table carries exactly the specified thresholds.

Why it matters: fault records are the reference points of history replay.  The expected
values are parsed from the specification tables themselves, so editing the configuration
alone cannot make these tests agree with a wrong value.
"""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from htsg.config import Settings, fault_rule, load_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def research():
    return load_settings(ROOT / "config" / "research-sim.json")


def expand(cell):
    """`temp_inner_01…96、temp_outer_01…96（金属壁温）` -> channel ids."""
    cell = re.sub(r"[（(].*?[）)]", "", cell)
    ids = []
    for token in re.split(r"[、,，]", cell):
        token = token.strip().strip("`")
        ranged = re.fullmatch(r"([a-z_]+)_(\d+)…(\d+)", token)
        if ranged:
            name, first, last = ranged.groups()
            width = len(first)
            ids += [f"{name}_{i:0{width}d}" for i in range(int(first), int(last) + 1)]
        elif token:
            ids.append(token)
    return ids


def number(cell):
    cell = cell.strip().replace("−", "-")
    return None if cell in ("—", "-", "") else float(cell)


def table(path, start_marker, end_marker):
    text = (ROOT / path).read_text(encoding="utf-8")
    body = text.split(start_marker, 1)[1].split(end_marker, 1)[0]
    rows = []
    for line in body.splitlines():
        if line.startswith("|") and not re.match(r"\|[\s:|-]+\|$", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows[1:]  # drop the header row


def expected_fault_rules():
    rules = {}
    for row in table("dev-docs/默认越限阈值与回差.md", '<a id="fault-defaults"></a>', "合计 304 路"):
        low, hl, high, hh = (number(c) for c in row[4:8])
        for channel in expand(row[0]):
            rules[channel] = (low, hl, high, hh)
    return rules


def expected_extremes():
    limits = {}
    for row in table("dev-docs/默认极值阈值.md", '<a id="extreme-defaults"></a>', "208 路源通道"):
        for channel in expand(row[0]):
            limits[channel] = (number(row[2]), number(row[3]))
    return limits


def channels(config):
    return {item.id: item for item in (*config.points, *config.derived)}


def test_channel_counts_are_8_modbus_200_opcua_and_209_derived(research):
    kinds = {s.id: s.kind for s in research.sources}
    modbus = [p for p in research.points if kinds[p.source] == "modbus"]
    opcua = [p for p in research.points if kinds[p.source] == "opcua"]
    assert (len(modbus), len(opcua), len(research.derived)) == (8, 200, 209)
    assert len(research.points) + len(research.derived) == 417


def test_exactly_304_channels_have_ordinary_thresholds_and_they_match_the_table(research):
    expected = expected_fault_rules()
    assert len(expected) == 304
    actual = {k: v for k, v in channels(research).items() if fault_rule(v)}
    assert set(actual) == set(expected)
    for channel, (low, hl, high, hh) in expected.items():
        rule = fault_rule(actual[channel])
        assert (rule["low"], rule["high"]) == (low, high), channel
        # a side without a threshold has no hysteresis either
        assert rule["hysteresis_low"] == (hl or 0.0), channel
        assert rule["hysteresis_high"] == (hh or 0.0), channel


def test_each_hysteresis_side_is_independent(research):
    feedwater = fault_rule(channels(research)["feedwater_temperature"])
    assert (feedwater["hysteresis_low"], feedwater["hysteresis_high"]) == (6, 1)
    assert fault_rule(channels(research)["waterwall_flow"])["high"] is None


def test_channels_without_ordinary_thresholds_are_the_specified_ones(research):
    quiet = {k for k, v in channels(research).items() if not fault_rule(v)}
    assert len(quiet) == 417 - 304
    assert {f"bridge_mvv_{i:02d}" for i in range(1, 9)} <= quiet
    assert {f"strain_change_{i:02d}" for i in range(1, 9)} <= quiet
    assert {f"stress_{i:02d}" for i in range(1, 97)} | {"thermal_stress_peak"} <= quiet


def test_all_417_extreme_limits_are_unchanged_and_independent_of_ordinary_ones(research):
    expected = expected_extremes()
    assert len(expected) == 417
    for channel, item in channels(research).items():
        assert (item.extreme_low, item.extreme_high) == expected[channel], channel
    # the ordinary band sits strictly inside the extreme band, so the two never replace each other
    for channel, item in channels(research).items():
        rule = fault_rule(item)
        if rule and rule["high"] is not None:
            assert rule["high"] < item.extreme_high, channel
        if rule and rule["low"] is not None:
            assert rule["low"] > item.extreme_low, channel


def base_point(**extra):
    return {"id": "a", "source": "s", "unit": "u", **extra}


def settings(points=(), derived=(), sources=({"id": "s", "kind": "modbus"},)):
    return Settings.model_validate({"sources": list(sources), "points": list(points), "derived": list(derived)})


@pytest.mark.parametrize(
    "extra",
    [
        {"hysteresis_high": 1},  # hysteresis with no high threshold
        {"low": 0, "hysteresis_low": 1, "hysteresis_high": 1},  # hysteresis_high without high
        {"low": 5, "high": 5},
        {"low": 0, "high": 10, "hysteresis_low": 5, "hysteresis_high": 5},  # no reset band left
        {"high": 10, "hysteresis_high": -1},
        {"high": 10, "hysteresis_high": 100, "extreme_low": -50},  # reset line below the extreme band
    ],
)
def test_bad_threshold_or_hysteresis_values_are_rejected(extra):
    with pytest.raises(ValidationError):
        settings([base_point(**extra)])


def test_derived_channels_accept_thresholds_and_hysteresis():
    config = settings(
        [base_point(), base_point(id="b")],
        [
            {
                "id": "d",
                "inputs": ["a", "b"],
                "operation": "difference",
                "unit": "u",
                "low": -5,
                "high": 5,
                "hysteresis_low": 1,
                "hysteresis_high": 2,
            }
        ],
    )
    assert fault_rule(config.derived[0]) == {
        "low": -5,
        "high": 5,
        "hysteresis_low": 1,
        "hysteresis_high": 2,
    }


def test_derived_channel_mixing_two_sources_is_not_rejected():
    # 终版 §2.1: no registration, subscription or start-up is refused because of a source combination.
    config = settings(
        [base_point(), base_point(id="b", source="t")],
        [{"id": "d", "inputs": ["a", "b"], "operation": "difference", "unit": "u", "high": 3}],
        sources=({"id": "s", "kind": "modbus"}, {"id": "t", "kind": "modbus"}),
    )
    assert config.derived[0].inputs == ("a", "b")
