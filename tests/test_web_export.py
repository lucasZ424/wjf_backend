"""D41: CSV of the shown replay (终版 §14).

Why it matters: the file leaves the system and is read in a spreadsheet by someone who never saw the page.  It must
be exactly what was on the screen, say how it was made, keep a held value from posing as a new sample, and survive
commas, quotes, line breaks and formula-looking text.
"""

import csv
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

from browser_support import sign_in

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).with_name("js") / "build_csv.mjs"
CENTER = "1970-01-01T08:17:10"  # wall 1030 seen from Asia/Shanghai


def parse(text):
    """(preface as a dict list, header, data rows) of the file text, without its byte-order mark."""
    lines = list(csv.reader(io.StringIO(text, newline="")))
    split = lines.index([])
    return lines[:split], lines[split + 1], lines[split + 2 :]


# ----------------------------------------------------------------------------- the file format, under Node


def test_the_csv_survives_commas_quotes_line_breaks_and_formula_looking_text(tmp_path):
    node = shutil.which("node")
    assert node, "Node.js is required for the JavaScript checks"
    row = lambda **kw: {  # noqa: E731
        "kind": "raw", "time": 1000.0, "observed_at": 1000.0, "origin": "RECEIVED", "value": 1.0, "valid": True,
        "quality": "GOOD", "status_code": 0, "extreme": False, "batch_seq": 1, "item_seq": 0, "held": False,
        "until": None, "ended_by": None, "run": "run, \"1\"", "seg": 1, **kw,
    }  # fmt: skip
    nasty = 'He said "hi", then\nleft'
    payload = {
        "result": {
            "query_time": 2000.0, "center": 1000.0, "start": 990.0, "end": 1010.0, "seconds": 20, "limit": 10000,
            "coverage_complete_until": None, "unavailable": ["external"], "gaps": [],
            "channels": {"x": {
                "source_id": "s", "truncated": False,
                "segments": [{"run_id": "run, \"1\"", "seg": 1, "rows": [
                    row(value=-0.5), row(value=None, valid=False, quality="BAD", status_code=0x80350000, batch_seq=2),
                    row(value=0.0, batch_seq=3, held=True),
                ]}],
            }},
        },
        "request": {"points": ["x"], "chart": "x"}, "fault": None, "timezone": "Asia/Shanghai",
        "labels": {"x": nasty}, "units": {"x": "=1+1"},
    }  # fmt: skip
    path = tmp_path / "in.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    done = subprocess.run([node, str(SCRIPT), str(path)], capture_output=True, text=True, encoding="utf-8", timeout=60,
                          cwd=ROOT, env={**os.environ, "TZ": "Asia/Shanghai"})  # fmt: skip
    assert done.returncode == 0, done.stderr[-1500:]
    meta, header, rows = parse(done.stdout)
    facts = {m[0]: m[1] for m in meta if len(m) == 2}
    assert facts["通道名称"] == nasty, "comma, quote and line break come back exactly"
    assert facts["单位"] == "'=1+1", "text that a spreadsheet would evaluate is made inert"
    assert facts["时区"] == "Asia/Shanghai (UTC+08:00)" and facts["中心时间"] == "1970-01-01T08:16:40.000+08:00"
    assert facts["不可读的数据库"] == "external" and facts["覆盖终点"] == "未知"
    assert len(rows) == 3 and all(len(r) == len(header) for r in rows)
    col = {name: header.index(name) for name in header}
    assert rows[0][col["值"]] == "-0.5", "a negative number is a number, not text to be neutralised"
    assert rows[1][col["值"]] == "" and rows[1][col["是否有效"]] == "否", "an invalid value is empty, never 0"
    assert rows[2][col["值"]] == "0" and rows[2][col["保持值"]] == "是"
    assert rows[0][col["有效段"]] == 'run, "1"/1'


# --------------------------------------------------------------------------------- the real page, real file


def open_history(page, rig):
    sign_in(page, rig)
    page.get_by_role("button", name="历史查询").click()
    page.locator("#replay-settings").wait_for()


def query(page, center=CENTER, seconds=100, points=("temp_inner_06",)):
    page.locator("#replay-center").fill(center)
    page.locator("#replay-center").dispatch_event("change")
    page.locator("#replay-seconds").fill(str(seconds))
    page.locator("#replay-seconds").dispatch_event("change")
    page.locator("#replay-points").select_option(list(points))
    page.locator("#replay-run").click()
    page.locator("#replay-meta").wait_for()


def export(page, tmp_path):
    with page.expect_download() as download:
        page.locator("#replay-export").click()
    path = tmp_path / download.value.suggested_filename
    download.value.save_as(path)
    raw = path.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "UTF-8 with a byte-order mark, so a spreadsheet reads the Chinese correctly"
    return download.value.suggested_filename, parse(raw[3:].decode("utf-8"))


def shown(page):
    return [r.locator("td").all_inner_texts() for r in page.locator("#replay-records tbody tr").all()], [
        r.get_attribute("class") for r in page.locator("#replay-records tbody tr").all()
    ]


def test_the_file_holds_exactly_the_rows_on_screen_and_the_settings_that_made_them(page, replay_rig, tmp_path):
    requests = []
    page.on("request", lambda r: requests.append(r.url) if "/api/replay" in r.url else None)
    open_history(page, replay_rig)
    query(page, points=("temp_inner_06", "temp_inner_07"))
    asked = len(requests)
    name, (meta, header, rows) = export(page, tmp_path)
    assert len(requests) == asked, "exporting asks the database for nothing"
    assert name.endswith(".csv") and "temp_inner_06" in name
    facts = {m[0]: m[1] for m in meta if len(m) == 2}
    assert facts["时区"].startswith("Asia/Shanghai") and facts["中心时间"] == "1970-01-01T08:17:10.000+08:00"
    assert facts["窗口长度(秒)"] == "100" and facts["每通道行数上限"] == "10000" and facts["是否达到行数上限"] == "否"
    assert facts["查询通道"] == "temp_inner_06 temp_inner_07" and facts["导出通道"] == "temp_inner_06"
    assert facts["查询时刻"].endswith("+08:00") and float(facts["查询时刻(UTC秒)"]) > 1.6e9
    gaps = [m[1] for m in meta if m[0] == "归档缺口"]
    assert any("ua_field" in g and "QUEUE_FULL" in g and "1970-01-01T08:17:07" in g for g in gaps), gaps
    cells, classes = shown(page)
    assert len(rows) == len(cells) >= 5
    col = {n: header.index(n) for n in header}
    for file_row, table_row, css in zip(rows, cells, classes, strict=True):
        assert f"{file_row[col['批次号']]}·{file_row[col['批内序号']]}" == table_row[2]
        assert file_row[col["来源"]] == table_row[5]
        assert (file_row[col["是否有效"]] == "否") == ("bad" in css.split()), "the red rows are the invalid rows"
        if file_row[col["是否有效"]] == "是":
            assert round(float(file_row[col["值"]]), 2) == float(table_row[3])
        else:
            assert file_row[col["值"]] == "" and table_row[3] == "—"
    bad = [r for r in rows if r[col["是否有效"]] == "否"]
    assert len(bad) == 1 and "BAD" in bad[0][col["质量/状态"]] and "0x80350000" in bad[0][col["质量/状态"]]
    # the other channel on screen gives the other file, again equal to its table
    page.locator("#replay-focus").select_option("temp_inner_07")
    page.wait_for_function("document.querySelector('#replay-records') && document.querySelector('#replay-records').textContent.includes('状态重申')")
    _, (_, _, rows7) = export(page, tmp_path)
    assert len(rows7) == len(shown(page)[0]) and {r[0] for r in rows7} == {"temp_inner_07"}
    marks = [r[header.index("缺口标记")] for r in rows7]
    assert marks.count("缺口后重申") == 1, "the restatement that opens the segment after the gap says so"
    assert page.errors == []


def test_a_held_value_is_exported_as_held_with_its_own_time_never_as_a_new_sample(page, replay_rig, tmp_path):
    open_history(page, replay_rig)
    query(page, center="1970-01-01T08:17:13", seconds=4, points=("temp_outer_01",))  # [1031, 1035]
    _, (_, header, rows) = export(page, tmp_path)
    (row,) = rows
    col = {n: header.index(n) for n in header}
    assert row[col["保持值"]] == "是" and row[col["来源"]] == "状态重申"
    assert float(row[col["生效时间(UTC秒)"]]) == 1029.0 and float(row[col["原观测时间(UTC秒)"]]) == 1001.0
    assert float(row[col["生效时间(UTC秒)"]]) != 1031.0, "not stamped as the window start"
    assert row[col["有效至"]].startswith("1970-01-01T08:17:16") and row[col["终止原因"]] == "至有效段结束"
    assert row[col["缺口标记"]] == "", "the gap lies before this window, so this result does not mention it"


def test_the_file_says_when_the_row_limit_was_reached_and_follows_what_was_shown_not_the_inputs(page, replay_rig, tmp_path):
    page.route("**/api/replay*", lambda route: route.continue_(url=route.request.url + "&limit=3"))
    open_history(page, replay_rig)
    query(page, points=("bridge_mvv_01",))
    page.locator("#replay-center").fill("1970-01-01T09:00:01")  # edited after the query, not asked again
    page.locator("#replay-center").dispatch_event("change")
    _, (meta, header, rows) = export(page, tmp_path)
    facts = {m[0]: m[1] for m in meta if len(m) == 2}
    assert facts["是否达到行数上限"] == "是" and facts["达到上限的通道"] == "bridge_mvv_01"
    assert len(rows) <= 3
    assert facts["中心时间"] == "1970-01-01T08:17:10.000+08:00", "the file describes the result that was shown"


def test_there_is_nothing_to_export_before_a_result_exists(page, replay_rig):
    open_history(page, replay_rig)
    assert page.locator("#replay-export").count() == 0
