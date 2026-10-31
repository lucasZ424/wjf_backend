"""tools/pointsheet: the points table in Excel, turned into the deployment configuration (2026-10-30, user request).

Why it matters: the site's tag names and the number of OPC UA points are not known yet - maybe 40 of the 200, the rest
kept as headroom.  The internal ids stay fixed, so the derived values, the stations and the fault templates keep their
references; a row whose address is left empty is simply not connected, and what depends on it goes with it - named, never
silently.  A table with a mistake in a connected row produces no configuration at all: what reaches the backend has passed
the same rules as `htsg check`.
"""

import json
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

from htsg.config import Settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pointsheet"))
import pointsheet  # noqa: E402

RESEARCH = pointsheet.load_json(ROOT / "config/research-sim.json")
EXAMPLE = pointsheet.load_json(ROOT / "config/production.example.json")


def make(tmp_path, base=RESEARCH, keep=True):
    path = tmp_path / "点表.xlsx"
    pointsheet.template(base, path, keep_addresses=keep)
    return path


def edit(path, title, change):
    """change(header_index, row_values_by_header) -> dict of header: new value, for each data row."""
    book = load_workbook(path)
    ws = book[title]
    headers = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        values = {h: c.value for h, c in zip(headers, row)}
        for header, value in (change(values) or {}).items():
            row[headers.index(header)].value = value
    book.save(path)


def append(path, title, values: dict):
    book = load_workbook(path)
    ws = book[title]
    headers = [c.value for c in ws[1]]
    ws.append([values.get(h) for h in headers])
    book.save(path)


def test_a_table_filled_from_the_configuration_gives_back_the_very_same_configuration(tmp_path):
    """Nothing is lost on the way: the configuration id - a hash of all of it - is the same."""
    config, report = pointsheet.convert(RESEARCH, make(tmp_path))
    assert Settings.model_validate(config).config_id == Settings.model_validate(RESEARCH).config_id
    assert report == ["接入 208 个点（Modbus 8，OPC UA 200）；未接入 0 个"]


def test_forty_opcua_points_and_seven_bridges_connected_the_rest_is_headroom(tmp_path):
    """16 temperature pairs and the 8 process points (40 OPC UA points); bridge 8 left out.  Before, the derived values and
    the stations written for 208 inputs refused any smaller table."""
    path = make(tmp_path)
    wanted = {f"temp_{side}_{n:02d}" for side in ("inner", "outer") for n in range(1, 17)} | set(RESEARCH["ui"]["process_points"])
    edit(path, "OPC UA点", lambda v: None if v["内部编号"] in wanted else {"NodeId": None})
    edit(path, "Modbus点", lambda v: {"地址": None} if v["内部编号"] == "bridge_mvv_08" else None)
    config, report = pointsheet.convert(RESEARCH, path)
    settings = Settings.model_validate(config)
    ua = [p for p in settings.points if p.source == "ua_field"]
    assert len(ua) == 40 and len(settings.points) == 47
    ids = {d.id for d in settings.derived}
    assert {f"delta_t_{n:02d}" for n in range(1, 17)} <= ids and "delta_t_17" not in ids and "stress_96" not in ids
    assert "strain_08" not in ids and "strain_change_08" not in ids and "strain_07" in ids
    peak = next(d for d in settings.derived if d.id == "thermal_stress_peak")
    assert peak.inputs == tuple(f"stress_{n:02d}" for n in range(1, 17)), "the peak of what is there, not dropped"
    assert [s.id for s in settings.ui.stations] == [f"S0{n}" for n in range(1, 8)], "station 8 needs bridge 8"
    assert {s.point for s in settings.simulation.signals} == {p.id for p in settings.points}
    assert any("去掉界面测站（引用了未接入的点）：S08" == line for line in report)
    assert any(line.startswith("去掉 ") and "delta_t_17" in line for line in report), "what goes is named"
    assert set(settings.ui.point_labels) <= {p.id for p in settings.points} | ids


@pytest.mark.parametrize(
    ("title", "pid", "change", "message"),
    [
        ("OPC UA点", "temp_inner_01", {"NodeId": "ns=x;s=T01"}, "OPC UA点 第3行 temp_inner_01：NodeId"),
        ("OPC UA点", "temp_inner_01", {"单位": None}, "OPC UA点 第3行 temp_inner_01：单位"),
        ("Modbus点", "bridge_mvv_02", {"数据类型": "float"}, "Modbus点 第3行 bridge_mvv_02：数据类型"),
        ("Modbus点", "bridge_mvv_02", {"地址": "4x0002"}, "Modbus点 第3行 地址：应为整数"),
        ("Modbus点", "bridge_mvv_02", {"上限": 1.0, "下限": 2.0}, "low must be less than high"),
        ("Modbus点", "bridge_mvv_02", {"数据源": "ua_field"}, "数据源 ua_field 是 opcua"),
        ("OPC UA点", "temp_inner_01", {"内部编号": "temp_outer_01"}, "内部编号与 OPC UA点 第2行 重复"),
    ],
    ids=["nodeid", "unit-missing", "data-type", "address-text", "limits", "wrong-sheet", "duplicate"],
)
def test_a_mistake_in_a_connected_row_names_the_sheet_row_and_column_and_writes_nothing(tmp_path, title, pid, change, message):
    path = make(tmp_path)
    edit(path, title, lambda v: change if v["内部编号"] == pid else None)
    out = tmp_path / "site.json"
    code = pointsheet.main(["convert", "--base", str(ROOT / "config/research-sim.json"), "--sheet", str(path), "--out", str(out)])
    assert code == 1 and not out.exists(), "no configuration from a table with a mistake"
    with pytest.raises(pointsheet.SheetError) as raised:
        pointsheet.convert(RESEARCH, path)
    assert any(message in line for line in raised.value.problems), raised.value.problems


def test_an_empty_template_of_the_site_gives_a_valid_configuration_with_the_unused_sources_switched_off(tmp_path):
    """The production example with no address filled in: nothing connected is a valid state, and a source with no point is
    not left enabled to poll nothing."""
    path = make(tmp_path, EXAMPLE, keep=False)
    edit(path, "数据源", lambda v: {"启用": "是"})
    config, report = pointsheet.convert(EXAMPLE, path)
    settings = Settings.model_validate(config)
    assert settings.points == () and not any(s.enabled for s in settings.sources)
    assert [d.id for d in settings.derived] == ["thermal_stress"], "the unconfigured placeholder needs no input"
    assert "数据源 bridge 没有接入的点：改为不启用" in report


def test_a_point_added_below_the_template_is_connected_and_the_simulated_site_sends_it(tmp_path):
    path = make(tmp_path)
    edit(path, "OPC UA点", lambda v: {"NodeId": None} if v["内部编号"] == "furnace_pressure" else None)  # 200 at most
    append(path, "OPC UA点", {"内部编号": "drum_level", "数据源": "ua_field", "单位": "mm", "显示名称": "汽包水位 LT-101",
                             "NodeId": "ns=2;s=drum_level", "极小值": -300, "极大值": 300})  # fmt: skip
    config, report = pointsheet.convert(RESEARCH, path)
    settings = Settings.model_validate(config)
    assert settings.points[-1].id == "drum_level" and settings.ui.point_labels["drum_level"] == "汽包水位 LT-101"
    signal = next(s for s in settings.simulation.signals if s.point == "drum_level")
    assert signal.base == 0.0 and any("drum_level" in line for line in report)


def test_the_display_name_column_becomes_the_page_label_and_the_meaning_column_goes_nowhere(tmp_path):
    path = make(tmp_path)
    edit(path, "OPC UA点", lambda v: {"显示名称": "TE-1001A", "含义": "随便写"} if v["内部编号"] == "temp_inner_01" else None)
    config, _ = pointsheet.convert(RESEARCH, path)
    assert config["ui"]["point_labels"]["temp_inner_01"] == "TE-1001A"
    assert "随便写" not in json.dumps(config, ensure_ascii=False)


PRODUCTION_BASE = pointsheet.load_json(ROOT / "config/production.base.json")


def test_the_production_base_carries_the_same_derived_values_stations_and_model_as_the_simulated_site():
    """The platform at the site computes what the drill computes: only the mode, the addresses, the simulation and the
    storage differ.  A derived value or a station changed in one and not in the other fails here."""
    research = Settings.model_validate(RESEARCH)
    base = Settings.model_validate(PRODUCTION_BASE)
    assert base.mode == "production" and not base.simulation.enabled and not any(s.enabled for s in base.sources)
    assert base.derived == research.derived and base.models == research.models and base.ui == research.ui
    strip = lambda p: p.model_copy(update={"address": None, "node_id": ""})  # noqa: E731
    assert [strip(p) for p in base.points] == [strip(p) for p in research.points]
    assert all(p.address is None and p.node_id == "" for p in base.points), "the site's addresses come from its points table"


def test_one_points_table_gives_the_platform_and_the_simulated_site_the_same_points_and_derived_values(tmp_path):
    """The drill: one table, converted on the production base for the platform and on the research base for the simulated
    site.  The two must agree on every point and derived value, or the platform would wait for data the site never sends."""
    path = make(tmp_path)
    wanted = {f"temp_{side}_{n:02d}" for side in ("inner", "outer") for n in range(1, 17)} | set(RESEARCH["ui"]["process_points"])
    edit(path, "OPC UA点", lambda v: None if v["内部编号"] in wanted else {"NodeId": None})
    platform, _ = pointsheet.convert(PRODUCTION_BASE, path)
    site, _ = pointsheet.convert(RESEARCH, path)
    platform, site = Settings.model_validate(platform), Settings.model_validate(site)
    assert platform.mode == "production" and not platform.simulation.enabled
    assert platform.points == site.points and platform.derived == site.derived and platform.sources == site.sources
    assert platform.ui.stations == site.ui.stations
