"""tools/pointsheet: the site's points table in Excel, turned into the deployment configuration (修复轮16, 修复轮18; 验收表 G).

Why it matters: the site engineer knows what each point is and where to subscribe it, not our internal naming.  The table
therefore carries site facts only - a category, a tag, an address - and the tool gives every point its internal id and builds
the strains, the stresses, the stations and the peak from them.  Nothing is pre-filled: a point that is not in the table is not
in the configuration.  A table with a mistake produces no configuration at all, and what reaches the backend has passed the same
rules as `htsg check`.
"""

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

from htsg.config import Settings
from htsg.simulation import FieldSimulator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pointsheet"))
import pointsheet  # noqa: E402

BASE = pointsheet.load_json(ROOT / "config/production.base.json")
RESEARCH = pointsheet.load_json(ROOT / "config/research-sim.json")
RETIRED = ("内壁温度", "外壁温度", "水冷壁流量", "主汽压力")


def blank(tmp_path, base=BASE):
    path = tmp_path / "点表.xlsx"
    pointsheet.template(base, path)
    book = load_workbook(path)
    sources = book["数据源"]
    heads = [c.value for c in sources[1]]
    for row in sources.iter_rows(min_row=2):
        cells = dict(zip(heads, row))
        cells["启用"].value = "是"
        if cells["类型"].value == "modbus":
            cells["主机"].value, cells["端口"].value = "127.0.0.1" if base is RESEARCH else "192.168.1.10", 15020 if base is RESEARCH else 502
        else:
            cells["端点"].value = "opc.tcp://127.0.0.1:14840/htsg/" if base is RESEARCH else "opc.tcp://192.168.1.20:4840/"
    book.save(path)
    return path


def add(path, title, **values):
    add_many(path, title, [values])


def add_many(path, title, rows):
    book = load_workbook(path)
    ws = book[title]
    heads = [c.value for c in ws[1]]
    for values in rows:
        ws.append([values.get(h) for h in heads])
    book.save(path)


def bridge(path, n, **extra):
    add(path, "Modbus点", 类别="电桥", 显示名称=f"SG-{n:02d}", 数据源="mb_field", 从站号=1, 地址=2 * (n - 1), 数据类型="float32", **extra)


def wall_row(tag, **extra):
    return {"类别": "壁温", "显示名称": tag, "数据源": "ua_field", "NodeId": f"ns=2;s={tag}", **extra}


def wall(path, tag, **extra):
    add(path, "OPC UA点", **wall_row(tag, **extra))


def site(tmp_path, walls=("TE-001", "TE-002"), bridges=2):
    path = blank(tmp_path)
    for n in range(1, bridges + 1):
        bridge(path, n)
    for tag in walls:
        wall(path, tag)
    add(path, "OPC UA点", 类别="机组负荷", 显示名称="LOAD", 数据源="ua_field", NodeId="ns=2;s=LOAD")
    return path


def test_the_blank_table_carries_no_point_and_offers_the_categories_of_the_new_table(tmp_path):
    """G1 (用户 11-05, 11-01): no default points, no pair number; the sources are named, the point sheets are only headers and
    choices - with the wall and the feedwater flow among them, and the old four gone."""
    assert BASE["points"] == [] and BASE["derived"] == [] and BASE["ui"]["stations"] == [], "the base has no default points"
    book = load_workbook(blank(tmp_path))
    for title in ("Modbus点", "OPC UA点"):
        ws = book[title]
        heads = [c.value for c in ws[1]]
        assert ws.max_row == 1, f"{title} has no pre-filled row"
        assert heads[0] == "类别" and "温度对号" not in heads and "内部编号" not in heads, "neither is the site's business"
        choices = [v.formula1 for v in ws.data_validations.dataValidation if "壁温" in (v.formula1 or "")]
        assert choices and "给水流量" in choices[0], "offered in the list"
        for old in RETIRED:
            assert old not in choices[0], old
    assert "壁温" in pointsheet.CATEGORIES and "给水流量" in pointsheet.CATEGORIES
    for old in RETIRED:
        assert old not in pointsheet.CATEGORIES, old
    assert [c.value for c in book["数据源"]["A"][1:]] == ["mb_field", "ua_field"]


def test_walls_are_numbered_in_the_order_of_the_table_and_nothing_is_derived_from_them(tmp_path):
    """G2 (用户 11-05): the number is the row's place, not something the tag says; and no difference, no stress."""
    path = blank(tmp_path)
    for tag in ("TE-Z", "TE-A", "TE-M"):
        wall(path, tag)
    config, _ = pointsheet.convert(BASE, path)
    settings = Settings.model_validate(config)
    assert [p.id for p in settings.points] == ["temp_wall_001", "temp_wall_002", "temp_wall_003"]
    assert [settings.ui.point_labels[p.id] for p in settings.points] == ["TE-Z", "TE-A", "TE-M"]
    assert settings.derived == ()


def test_each_bridge_gives_a_strain_a_change_a_stress_and_a_station_and_all_of_them_one_peak(tmp_path):
    """G3 (用户 11-05): the stress of a bridge is what the fourth page shows; a tool that forgot it leaves an empty panel."""
    config, report = pointsheet.convert(BASE, site(tmp_path, walls=("TE-001",)))
    settings = Settings.model_validate(config)
    assert [p.id for p in settings.points] == ["bridge_mvv_01", "bridge_mvv_02", "temp_wall_001", "load_mw"]
    derived = {d.id: d for d in settings.derived}
    assert len(derived) == 2 * 3 + 1
    assert derived["strain_02"].inputs == ("bridge_mvv_02",) and derived["strain_02"].operation == "bridge_strain"
    assert derived["strain_change_02"].inputs == ("strain_02",) and derived["strain_change_02"].operation == "change"
    assert derived["stress_02"].inputs == ("strain_02",) and derived["stress_02"].operation == "elastic_stress"
    assert derived["stress_02"].model_id == settings.models[0].id and derived["stress_02"].unit == "MPa"
    assert (derived["stress_02"].extreme_low, derived["stress_02"].extreme_high) == (-850, 850), "默认极值阈值 §1"
    assert derived["stress_02"].high is None and derived["stress_02"].low is None, "B10, Q7: no limit on a stress"
    assert derived["thermal_stress_peak"].operation == "absolute_peak"
    assert derived["thermal_stress_peak"].inputs == ("stress_01", "stress_02")
    assert (derived["strain_01"].low, derived["strain_01"].high) == (-690, 690), "默认越限阈值与回差 §2"
    stations = settings.ui.stations
    assert [(s.id, s.label, s.strain, s.strain_change) for s in stations] == [
        ("S01", "SG-01", "strain_01", "strain_change_01"),
        ("S02", "SG-02", "strain_02", "strain_change_02"),
    ]  # fmt: skip
    assert settings.ui.process_points == ("load_mw",)
    assert isinstance(report, list) and report


@pytest.mark.parametrize("category", RETIRED)
def test_an_old_category_is_named_by_sheet_and_row_says_to_take_the_new_template_and_gives_no_file(tmp_path, category):
    """G4 (用户 11-05): a table made for the old point table must not lose its rows in silence, nor half of them become points."""
    path = site(tmp_path, walls=("TE-001",))  # OPC UA row 2 is the wall, row 3 the load, so the bad one is row 4
    add(path, "OPC UA点", 类别=category, 显示名称="OLD", 数据源="ua_field", NodeId="ns=2;s=OLD")
    with pytest.raises(pointsheet.SheetError) as error:
        pointsheet.convert(BASE, path)
    line = next(p for p in error.value.problems if "OPC UA点" in p)
    assert "第4行" in line and category in line and "模板" in line and "新" in line, line
    out = tmp_path / "production.json"
    base = ROOT / "config/production.base.json"
    assert pointsheet.main(["convert", "--base", str(base), "--sheet", str(path), "--out", str(out)]) == 1
    assert not out.exists(), "no configuration at all"


def test_an_empty_cell_takes_the_category_default_and_none_takes_it_away(tmp_path):
    """G5, 用户 11-01 (修复轮16): a threshold left empty is the category's default; “无” removes it, with its hysteresis."""
    path = site(tmp_path, walls=("TE-001",))
    wall(path, "TE-002", 上限="无")
    wall(path, "TE-003", 上限=500)
    add(path, "OPC UA点", 类别="给水流量", 显示名称="FW", 数据源="ua_field", NodeId="ns=2;s=FW")
    add(path, "OPC UA点", 类别="其他", 显示名称="汽包水位", 数据源="ua_field", 单位="mm", NodeId="ns=2;s=LT101")
    config, report = pointsheet.convert(BASE, path)
    points = {p["id"]: p for p in config["points"]}
    assert points["temp_wall_001"]["high"] == 490 and points["temp_wall_001"]["hysteresis_high"] == 3 and points["temp_wall_001"]["unit"] == "degC"
    assert (points["temp_wall_001"]["extreme_low"], points["temp_wall_001"]["extreme_high"]) == (-50, 800)
    assert "high" not in points["temp_wall_002"] and "hysteresis_high" not in points["temp_wall_002"]
    assert points["temp_wall_003"]["high"] == 500 and points["temp_wall_003"]["hysteresis_high"] == 3
    flow = points["feedwater_flow"]
    assert (flow["low"], flow["hysteresis_low"], flow["unit"]) == (925, 90, "t/h") and "high" not in flow, "Q7"
    assert points["other_01"] == {"id": "other_01", "source": "ua_field", "unit": "mm", "node_id": "ns=2;s=LT101"}, "no default"
    assert any("壁温" in line and "默认值" in line for line in report), "the report says what it filled in"


def rows_of_walls(count):
    return [wall_row(f"TE-{n:03d}") for n in range(1, count + 1)]


def test_222_opc_ua_rows_give_a_configuration(tmp_path):
    """A2, G6: the capacity itself."""
    path = blank(tmp_path)
    add_many(path, "OPC UA点", rows_of_walls(222))
    config, _ = pointsheet.convert(BASE, path)
    assert len(Settings.model_validate(config).points) == 222


def test_223_opc_ua_rows_are_an_error_and_give_no_file(tmp_path):
    """G6, Q1: the tool answers as the backend would, before anything reaches the site."""
    path = blank(tmp_path)
    add_many(path, "OPC UA点", rows_of_walls(223))
    with pytest.raises(pointsheet.SheetError):
        pointsheet.convert(BASE, path)
    out = tmp_path / "production.json"
    assert pointsheet.main(["convert", "--base", str(ROOT / "config/production.base.json"), "--sheet", str(path), "--out", str(out)]) == 1
    assert not out.exists()


def test_a_row_without_address_is_not_connected_and_is_named(tmp_path):
    path = site(tmp_path, walls=("TE-001",))
    wall(path, "TE-002", NodeId=None)
    wall(path, "TE-003")
    config, report = pointsheet.convert(BASE, path)
    ids = {p["id"] for p in config["points"]}
    assert "temp_wall_001" in ids and "temp_wall_003" in ids and "TE-002" not in {config["ui"]["point_labels"].get(i) for i in ids}
    assert any("OPC UA点 第" in line and "TE-002" in line for line in report), "what is not connected is named"


@pytest.mark.parametrize(
    "rows, message",
    [
        ([("OPC UA点", {"类别": "汽包液位", "数据源": "ua_field", "NodeId": "ns=2;s=x"})], "类别：不认识“汽包液位”"),
        ([("OPC UA点", {"类别": "机组负荷", "数据源": "ua_field", "NodeId": "ns=2;s=y"})], "机组负荷已经在"),
        ([("OPC UA点", {"类别": "其他", "数据源": "ua_field", "NodeId": "ns=2;s=z"})], "其他"),
        ([("OPC UA点", {"类别": "工质温度", "数据源": "nowhere", "NodeId": "ns=2;s=w"})], "数据源：'nowhere' 不在"),
    ],
)
def test_a_mistake_names_the_sheet_and_row_and_produces_nothing(tmp_path, rows, message):
    path = site(tmp_path, walls=("TE-001",))
    for title, values in rows:
        add(path, title, **values)
    with pytest.raises(pointsheet.SheetError) as error:
        pointsheet.convert(BASE, path)
    assert any("OPC UA点 第" in line and message in line for line in error.value.problems), error.value.problems


def test_an_old_table_with_internal_ids_is_refused_with_a_hint(tmp_path):
    path = blank(tmp_path)
    book = load_workbook(path)
    book["OPC UA点"].cell(1, 1).value = "内部编号"
    book.save(path)
    with pytest.raises(pointsheet.SheetError, match="旧版点表"):
        pointsheet.convert(BASE, path)


def test_an_unused_source_is_switched_off_and_named(tmp_path):
    path = blank(tmp_path)
    wall(path, "TE-001")
    config, report = pointsheet.convert(BASE, path)
    assert {s["id"]: s["enabled"] for s in config["sources"]} == {"mb_field": False, "ua_field": True}
    assert "数据源 mb_field 没有接入的点：改为不启用" in report
    assert Settings.model_validate(config).ui.stations == ()


def test_a_research_base_gives_the_simulated_field_a_signal_for_every_point(tmp_path):
    """The same table drives a simulated site: points the research configuration knows keep their signals, the rest send
    a constant, and the simulator's plant model recognises the ids the tool gives (temp_wall_NNN, bridge_mvv_NN ...)."""
    path = blank(tmp_path, RESEARCH)
    bridge(path, 1)
    wall(path, "TE-001")
    add(path, "OPC UA点", 类别="其他", 显示名称="汽包水位", 数据源="ua_field", 单位="mm", NodeId="ns=2;s=drum")
    config, report = pointsheet.convert(RESEARCH, path)
    settings = Settings.model_validate(config)
    signals = {s.point: s for s in settings.simulation.signals}
    assert set(signals) == {p.id for p in settings.points}
    known = next(s for s in RESEARCH["simulation"]["signals"] if s["point"] == "temp_wall_001")
    assert (signals["temp_wall_001"].base, signals["temp_wall_001"].amplitude) == (known["base"], known["amplitude"])
    assert signals["other_01"].base == 0.0 and any("other_01" in line for line in report)
    field = FieldSimulator(settings)
    field.generate(1000)
    assert set(field.values) == {p.id for p in settings.points}, "and the field really sends them, no more and no fewer"
