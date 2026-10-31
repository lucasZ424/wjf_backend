"""点表 Excel 与部署配置 JSON 的互转（部署时用；后端运行时不引用本工具）。

    python tools/pointsheet/pointsheet.py template --base config/research-sim.json --out 点表.xlsx [--keep-addresses]
    python tools/pointsheet/pointsheet.py convert  --base config/research-sim.json --sheet 点表.xlsx --out config/site.json

template：由基础配置生成点表模板。基础配置里的每个输入点一行，内部编号固定（派生量、界面测站、故障模板都按它引用），
现场地址一列留空待填；`--keep-addresses` 保留基础配置里的地址（模拟现场演练时，删掉不接入那几行的地址即可）。

convert：读回填好的点表，生成部署配置。规则：
  * 填了现场地址（Modbus 的“地址”、OPC UA 的“NodeId”）的行接入；地址留空的行不接入，这是正常情况，不报错。
  * 接入的行缺必填项或填错（类型、字序、NodeId 格式、阈值关系……），按“表名 第几行 哪一列”报错，不生成文件。
  * 依赖未接入点的派生量、界面测站与模拟信号随之去掉；取最大值、平均、求和这类多输入派生量只去掉缺的输入，一个都不剩
    才去掉。去掉了什么，逐条打印。
  * 没有接入点的数据源改为不启用，并打印。
  * 最后按后端自己的配置规则整份校验（与 `htsg check` 相同），通过才写出文件。
基础配置里点表以外的部分（时限、存储、模型、项目、模拟场景……）原样保留。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from htsg.config import Point, Settings, Source, check_node_id  # noqa: E402

MULTI_INPUT = ("maximum", "mean", "sum")  # 多输入派生量：缺的输入去掉，剩一个以上就保留
YES, NO = "是", "否"

# (表头, 配置字段, 类型)；类型：text / int / float / bool / floats（分号分隔的多个数）
SOURCE_COLUMNS = [
    ("编号", "id", "text"), ("类型", "kind", "text"), ("启用", "enabled", "bool"), ("主机", "host", "text"),
    ("端口", "port", "int"), ("端点", "endpoint", "text"), ("用户名环境变量", "username_env", "text"),
    ("口令环境变量", "password_env", "text"), ("安全策略", "security", "text"),
]  # fmt: skip
COMMON_HEAD = [("内部编号", "id", "text"), ("含义", None, "text"), ("数据源", "source", "text"), ("单位", "unit", "text"),
               ("显示名称", None, "text")]  # fmt: skip
LIMIT_COLUMNS = [
    ("比例", "scale", "float"), ("偏移", "offset", "float"), ("无效值", "invalid_values", "floats"),
    ("下限", "low", "float"), ("下限回差", "hysteresis_low", "float"), ("上限", "high", "float"),
    ("上限回差", "hysteresis_high", "float"), ("极小值", "extreme_low", "float"), ("极大值", "extreme_high", "float"),
]  # fmt: skip
MODBUS_COLUMNS = [
    *COMMON_HEAD, ("从站号", "device_id", "int"), ("寄存器类型", "register_kind", "text"), ("地址", "address", "int"),
    ("数据类型", "data_type", "text"), ("字节序", "byte_order", "text"), ("字序", "word_order", "text"), *LIMIT_COLUMNS,
]  # fmt: skip
UA_COLUMNS = [*COMMON_HEAD, ("NodeId", "node_id", "text"), *LIMIT_COLUMNS]
SHEETS = {"modbus": ("Modbus点", MODBUS_COLUMNS, "地址"), "opcua": ("OPC UA点", UA_COLUMNS, "NodeId")}
SOURCE_SHEET = "数据源"
CHOICES = {
    "类型": ["modbus", "opcua"], "启用": [YES, NO], "寄存器类型": ["holding", "input"],
    "数据类型": ["uint16", "int16", "uint32", "int32", "float32", "float64"], "字节序": ["big", "little"], "字序": ["big", "little"],
}  # fmt: skip
GUIDE = [
    "本表由 tools/pointsheet/pointsheet.py template 生成；填好后用 convert 生成部署配置。",
    "每行一个输入点。“内部编号”是系统内部的固定编号，派生量、界面测站、故障模板都按它引用：已有的行不要改编号。",
    "接入：填“地址”（Modbus 点）或“NodeId”（OPC UA 点）。不接入：这两列留空即可，是正常情况，不报错。",
    "依赖未接入点的派生量与界面测站会被自动去掉，转换时逐条列出。",
    "“显示名称”可填现场位号或中文名，只用于页面显示；“含义”只是说明，不进配置。",
    "数值列留空表示用默认值（比例1、偏移0、无阈值）；“无效值”可填多个，用分号分隔。",
    "可以在表尾加新行接入模板里没有的点：内部编号用字母、数字、下划线，不要和已有编号重复。新点没有派生量和测站。",
    "“数据源”表的“编号”要与点所在行的“数据源”一致；OPC UA 的用户名、口令只填环境变量名，不填明文。",
]


class SheetError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


# ------------------------------------------------------------------------------------------------ template


def _cell(value):
    if isinstance(value, bool):
        return YES if value else NO
    if isinstance(value, (list, tuple)):
        return ";".join(str(v) for v in value) if value else None
    return value


def template(base: dict, out: Path, keep_addresses: bool = False):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    Settings.model_validate(base)  # 基础配置本身先要有效
    labels = base.get("ui", {}).get("point_labels", {})
    kinds = {s["id"]: s["kind"] for s in base.get("sources", [])}
    book = Workbook()
    guide = book.active
    guide.title = "说明"
    for line in GUIDE:
        guide.append([line])
    guide.column_dimensions["A"].width = 120
    bold, fill = Font(bold=True), PatternFill("solid", fgColor="FFF2CC")

    def sheet(title, columns, rows, key_header=None):
        ws = book.create_sheet(title)
        ws.append([header for header, _, _ in columns])
        for row in rows:
            ws.append(row)
        for k, (header, _, _) in enumerate(columns, start=1):
            cell = ws.cell(1, k)
            cell.font = bold
            if header == key_header:
                cell.fill = fill
            letter = cell.column_letter
            ws.column_dimensions[letter].width = 30 if header in ("含义", "显示名称", "NodeId", "端点") else 12
            if header in CHOICES:
                rule = DataValidation(type="list", formula1='"' + ",".join(CHOICES[header]) + '"', allow_blank=True)
                ws.add_data_validation(rule)
                rule.add(f"{letter}2:{letter}2000")
        ws.freeze_panes = "B2"

    sheet(SOURCE_SHEET, SOURCE_COLUMNS, [[_cell(src.get(key)) for _, key, _ in SOURCE_COLUMNS] for src in base.get("sources", [])])
    for kind, (title, columns, key_header) in SHEETS.items():
        rows = []
        for point in base.get("points", []):
            if kinds.get(point["source"]) != kind:
                continue
            values = []
            for header, key, _ in columns:
                if header in ("含义", "显示名称"):
                    values.append(labels.get(point["id"]))
                elif key in ("address", "node_id") and not keep_addresses:
                    values.append(None)
                else:
                    values.append(_cell(point.get(key)))
            rows.append(values)
        sheet(title, columns, rows, key_header)
    out.parent.mkdir(parents=True, exist_ok=True)
    book.save(out)


# ------------------------------------------------------------------------------------------------- reading


def _parse(value, kind: str):
    """单元格 -> 配置值；None 表示留空。格式不对抛 ValueError（文字给填表的人看）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if kind == "text":
        return str(value).strip() if not isinstance(value, float) or not value.is_integer() else str(int(value))
    if kind == "bool":
        text = str(value).strip()
        if text in (YES, "true", "True", "1"):
            return True
        if text in (NO, "false", "False", "0"):
            return False
        raise ValueError(f"应填“{YES}”或“{NO}”，现为 {value!r}")
    try:
        if kind == "int":
            number = float(str(value).strip()) if not isinstance(value, (int, float)) else value
            if isinstance(number, float) and not number.is_integer():
                raise ValueError
            return int(number)
        if kind == "float":
            return float(str(value).strip()) if not isinstance(value, (int, float)) else float(value)
        if kind == "floats":
            parts = str(value).replace("；", ";").replace(",", ";").split(";")
            return [float(p) for p in parts if p.strip()]
    except ValueError:
        what = {"int": "整数", "float": "数值", "floats": "数值（多个用分号分隔）"}[kind]
        raise ValueError(f"应为{what}，现为 {value!r}") from None
    raise AssertionError(kind)


def _rows(book, title: str, columns):
    """(行号, {表头: 单元格}) for each non-empty row; missing headers are an error."""
    if title not in book.sheetnames:
        raise SheetError([f"缺少工作表“{title}”"])
    ws = book[title]
    headers = [c.value for c in ws[1]]
    missing = [h for h, _, _ in columns if h not in headers]
    if missing:
        raise SheetError([f"{title}：缺少列 {'、'.join(missing)}"])
    at = {h: headers.index(h) for h, _, _ in columns}
    for number, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        cells = {h: (row[i] if i < len(row) else None) for h, i in at.items()}
        if any(v is not None and str(v).strip() for v in cells.values()):
            yield number, cells


def _fields(title, number, cells, columns, problems):
    out = {}
    for header, key, kind in columns:
        if key is None:
            continue
        try:
            value = _parse(cells[header], kind)
        except ValueError as error:
            problems.append(f"{title} 第{number}行 {header}：{error}")
            continue
        if value is not None:
            out[key] = value
    return out


def _model_problems(error: ValidationError, title, number, columns, name):
    headers = {key: header for header, key, _ in columns if key}
    found = []
    for item in error.errors():
        field = item["loc"][0] if item["loc"] else None
        where = headers.get(field, "") if field else ""
        message = str(item.get("msg", "")).removeprefix("Value error, ")
        found.append(f"{title} 第{number}行 {name}{('：' + where) if where else ''}：{message}")
    return found


def read_sheet(path: Path):
    from openpyxl import load_workbook

    book = load_workbook(path, data_only=True)
    problems, sources, points, labels, not_connected = [], [], [], {}, []
    for number, cells in _rows(book, SOURCE_SHEET, SOURCE_COLUMNS):
        raw = _fields(SOURCE_SHEET, number, cells, SOURCE_COLUMNS, problems)
        try:
            sources.append(Source.model_validate(raw).model_dump(exclude_defaults=True) | {"id": raw.get("id"), "kind": raw.get("kind")})
        except ValidationError as error:
            problems += _model_problems(error, SOURCE_SHEET, number, SOURCE_COLUMNS, raw.get("id", ""))
    kinds = {s["id"]: s["kind"] for s in sources}
    seen: dict[str, str] = {}
    for kind, (title, columns, key_header) in SHEETS.items():
        for number, cells in _rows(book, title, columns):
            name = _parse(cells["内部编号"], "text") or ""
            where = f"{title} 第{number}行"
            if not name:
                problems.append(f"{where}：内部编号为空")
                continue
            if name in seen:
                problems.append(f"{where} {name}：内部编号与 {seen[name]} 重复")
                continue
            seen[name] = where
            if _parse(cells[key_header], "text") is None:
                not_connected.append(name)  # 地址留空：不接入，正常情况
                continue
            raw = _fields(title, number, cells, columns, problems)
            if raw.get("source") not in kinds:
                problems.append(f"{where} {name}：数据源 {raw.get('source')!r} 不在“{SOURCE_SHEET}”表里")
            elif kinds[raw["source"]] != kind:
                problems.append(f"{where} {name}：数据源 {raw['source']} 是 {kinds[raw['source']]}，不能放在“{title}”表")
            try:
                point = Point.model_validate(raw)
            except ValidationError as error:
                problems += _model_problems(error, title, number, columns, name)
                continue
            if kind == "opcua":
                try:
                    check_node_id(point)
                except ValueError as error:
                    problems.append(f"{where} {name}：NodeId：{str(error).split(': ', 1)[-1]}")
                    continue
            points.append(point.model_dump(exclude_defaults=True) | {"id": point.id, "source": point.source, "unit": point.unit})
            shown = _parse(cells["显示名称"], "text")
            if shown:
                labels[name] = shown
    if problems:
        raise SheetError(problems)
    return sources, points, labels, not_connected


# ------------------------------------------------------------------------------------------------- convert


def convert(base: dict, sheet: Path) -> tuple[dict, list[str]]:
    """(新配置, 报告行)。点表有错时抛 SheetError，不产生配置。"""
    sources, points, labels, not_connected = read_sheet(sheet)
    kinds = {s["id"]: s["kind"] for s in sources}
    count = {kind: sum(1 for p in points if kinds[p["source"]] == kind) for kind in SHEETS}
    report = [f"接入 {len(points)} 个点（Modbus {count['modbus']}，OPC UA {count['opcua']}）；未接入 {len(not_connected)} 个"]
    if not_connected:
        report.append("未接入：" + "、".join(not_connected))
    order = {p["id"]: n for n, p in enumerate(base.get("points", []))}
    points.sort(key=lambda p: order.get(p["id"], len(order)))  # 基础配置的顺序（页面列表按它排），新加的点在后
    config = json.loads(json.dumps(base))  # 基础配置的其余部分原样保留
    used = {p["source"] for p in points}
    for source in sources:
        if source.get("enabled") and source["id"] not in used:
            source["enabled"] = False
            report.append(f"数据源 {source['id']} 没有接入的点：改为不启用")
    config["sources"], config["points"] = sources, points
    known = {p["id"] for p in points}
    derived, dropped, reduced = [], [], []
    for item in base.get("derived", []):
        inputs = item.get("inputs", [])
        present = [x for x in inputs if x in known]
        if len(present) == len(inputs):
            derived.append(item)
        elif item["operation"] in MULTI_INPUT and present:
            derived.append({**item, "inputs": present})
            reduced.append(f"{item['id']}（{len(present)}/{len(inputs)} 个输入）")
        else:
            dropped.append(item["id"])
            continue
        known.add(item["id"])
    config["derived"] = derived
    if dropped:
        report.append(f"去掉 {len(dropped)} 个派生量（缺输入）：" + "、".join(dropped))
    if reduced:
        report.append("多输入派生量只用接入的输入：" + "、".join(reduced))
    ui = config.get("ui")
    if ui is not None:
        stations = [s for s in ui.get("stations", []) if all(
            s.get(k) in known for k in ("strain", "temperature", "inner_temperature", "stress", "delta_temperature")
        ) and s.get("strain_change") in (None, *known)]  # fmt: skip
        gone = [s["id"] for s in ui.get("stations", []) if s not in stations]
        if gone:
            report.append("去掉界面测站（引用了未接入的点）：" + "、".join(gone))
        ui["stations"] = stations
        process = [x for x in ui.get("process_points", []) if x in known]
        if len(process) != len(ui.get("process_points", [])):
            report.append("去掉工况量：" + "、".join(x for x in ui.get("process_points", []) if x not in known))
        ui["process_points"] = process
        ui["point_labels"] = {k: v for k, v in {**ui.get("point_labels", {}), **labels}.items() if k in known}
    simulation = config.get("simulation")
    if simulation and simulation.get("enabled"):
        point_ids = {p["id"] for p in points}
        signals = [s for s in simulation.get("signals", []) if s["point"] in point_ids]
        missing = sorted(point_ids - {s["point"] for s in signals})
        for pid in missing:  # 模板以外新加的点：模拟器给一个恒定值，只为让模拟现场能把它送出来
            point = next(p for p in points if p["id"] == pid)
            low, high = point.get("extreme_low"), point.get("extreme_high")
            signals.append({"point": pid, "base": (low + high) / 2 if low is not None and high is not None else 0.0})
        if missing:
            report.append("模拟现场为新加的点给恒定值（极值中点，无极值时为0）：" + "、".join(missing))
        simulation["signals"] = signals
        for scenario in simulation.get("scenarios", []):
            scenario["points"] = [x for x in scenario.get("points", []) if x in point_ids]
            scenario["offsets"] = {k: v for k, v in scenario.get("offsets", {}).items() if k in point_ids}
    try:
        Settings.model_validate(config)
    except ValidationError as error:
        raise SheetError(["生成的配置没有通过后端的配置校验：" + "; ".join(
            str(e.get("msg", "")).removeprefix("Value error, ") for e in error.errors()
        )]) from None  # fmt: skip
    return config, report


def main(argv=None):
    parser = argparse.ArgumentParser(description="点表 Excel 与部署配置 JSON 的互转")
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("template", help="由基础配置生成点表模板")
    t.add_argument("--base", type=Path, required=True)
    t.add_argument("--out", type=Path, required=True)
    t.add_argument("--keep-addresses", action="store_true", help="保留基础配置里的地址与 NodeId")
    c = sub.add_parser("convert", help="由填好的点表生成部署配置")
    c.add_argument("--base", type=Path, required=True)
    c.add_argument("--sheet", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    base = load_json(args.base)
    if args.command == "template":
        template(base, args.out, args.keep_addresses)
        print(f"模板已写入 {args.out}")
        return 0
    try:
        config, report = convert(base, args.sheet)
    except SheetError as error:
        print("点表有错，没有生成配置：", file=sys.stderr)
        for line in error.problems:
            print("  " + line, file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for line in report:
        print(line)
    print(f"配置已写入 {args.out}（已按后端配置规则校验通过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
