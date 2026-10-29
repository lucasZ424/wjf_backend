"""`htsg capacity`: the deletion period and the retention left after a deletion (修复轮09 09i, 终版 §13.4, §13.5 容量报告).

Why it matters: the site chooses its stop line by band from how many quantities it archives.  The estimate the command gives
must be the one the design states - 69 MB per archived quantity per day, measured at full scale with every value changing -
so that it reproduces the bands of §13.4 and does not keep reporting the old "one year" figure.  It is a scenario, never an
acceptance: the measured deletion period on the self-check page is what the stop line is adjusted by.
"""

import json
import sys

import pytest

from htsg import cli

TB = 10**12  # the bands are worked out on a decimal 2 TB disk


def estimate(monkeypatch, capsys, config, stop, disk_bytes):
    monkeypatch.setattr(sys, "argv", ["htsg", "capacity", "--config", config, "--disk-bytes", str(disk_bytes),
                                      "--cleanup-stop", str(stop)])  # fmt: skip
    cli.main()
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(("quantities", "stop", "period", "kept"), [(100, 0.80, 29, 232), (250, 0.65, 29, 75), (417, 0.50, 28, 35)])
def test_the_bands_of_the_design_are_reproduced(monkeypatch, capsys, tmp_path, quantities, stop, period, kept):
    config = tmp_path / "c.json"
    inputs = min(quantities, 200)  # the OPC UA capacity; the rest are derived quantities, archived the same way
    points = [{"id": f"p{n}", "source": "s", "unit": "x", "node_id": f"ns=2;i={n}"} for n in range(inputs)]
    derived = [{"id": f"d{n}", "inputs": ["p0"], "operation": "change", "unit": "x"} for n in range(quantities - inputs)]
    sources = [{"id": "s", "kind": "opcua", "enabled": False}]
    config.write_text(json.dumps({"sources": sources, "points": points, "derived": derived}))
    out = estimate(monkeypatch, capsys, str(config), stop, 2 * TB)
    assert out["archived_quantities"] == quantities
    assert round(out["days_between_deletions"]) == period and round(out["days_kept_after_deletion"]) == kept
    assert "scenario" in out["note"].lower() and "per_year" not in json.dumps(out), "no one-year figure any more"


def test_the_full_configuration_counts_inputs_and_derived_quantities(monkeypatch, capsys):
    out = estimate(monkeypatch, capsys, "config/research-sim.json", 0.50, 2 * TB)
    assert out["archived_quantities"] == 208 + 209
    assert out["bytes_per_quantity_per_day"] == 69 * 10**6
