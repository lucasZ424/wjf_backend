"""scripts/test_freeze.py: frozen test files cannot be edited by accident, and every weakened assertion is listed.

Why it matters (用户 2026-11-06): a test bent until the code passes checks nothing.  The guard has to stop the edit while the
code is being written, and the report has to show what changed however it was changed, or the rule rests on good intentions.
"""

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_freeze as guard  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("def test_a():\n    assert 1 + 1 == 2\n    assert 2 + 2 == 4\n", encoding="utf-8")
    (tests / "test_b.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(guard, "TESTS", tests)
    monkeypatch.setattr(guard, "FROZEN", tmp_path / ".local" / "test-freeze")
    return tests


def edit(monkeypatch, path):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(path)}})))
    return guard.main(["hook"])


def test_nothing_is_stopped_before_the_freeze(project, monkeypatch):
    """The test session writes its tests first; only the freeze starts the protection."""
    assert edit(monkeypatch, project / "test_a.py") == 0


def test_a_frozen_test_cannot_be_edited_and_the_refusal_says_what_to_do(project, monkeypatch, capsys):
    guard.main(["freeze"])
    assert edit(monkeypatch, project / "test_a.py") == 2, "exit code 2 is what blocks the tool call"
    assert "allow test_a.py" in capsys.readouterr().err
    assert edit(monkeypatch, project / "brand_new.py") == 2, "adding a file to tests/ is a change of the tests too"


def test_the_code_under_test_stays_editable_while_frozen(project, monkeypatch):
    guard.main(["freeze"])
    assert edit(monkeypatch, project.parent / "htsg" / "owner.py") == 0


def test_a_ruling_opens_one_file_and_no_other(project, monkeypatch):
    guard.main(["freeze"])
    guard.main(["allow", "test_a.py"])
    assert edit(monkeypatch, project / "test_a.py") == 0
    assert edit(monkeypatch, project / "test_b.py") == 2


def test_a_weakened_assertion_is_listed_even_if_the_file_was_changed_outside_the_guard(project, capsys):
    guard.main(["freeze"])
    (project / "test_a.py").write_text("def test_a():\n    assert 1 + 1 == 2\n    assert 2 + 2 > 0\n", encoding="utf-8")
    capsys.readouterr()
    guard.main(["report"])
    out = capsys.readouterr().out
    assert "被删或被改的断言 1 条，新增断言 1 条" in out
    assert "test_a.py: assert 2 + 2 == 4" in out, "the old expectation, as it stood"
    assert "assert 1 + 1 == 2" not in out, "an untouched assertion is not reported"


def test_a_deleted_test_file_counts_all_its_assertions_as_lost(project, capsys):
    guard.main(["freeze"])
    (project / "test_b.py").unlink()
    capsys.readouterr()
    guard.main(["report"])
    out = capsys.readouterr().out
    assert "删除 1 个" in out and "被删或被改的断言 1 条" in out and "test_b.py: assert True" in out


def test_an_unchanged_suite_reports_nothing(project, capsys):
    guard.main(["freeze"])
    capsys.readouterr()
    guard.main(["report"])
    assert "改动 0 个文件，删除 0 个，新增 0 个；被删或被改的断言 0 条，新增断言 0 条" in capsys.readouterr().out


def test_a_second_freeze_does_not_overwrite_the_baseline(project, capsys):
    """Freezing again after an edit would hide that edit."""
    guard.main(["freeze"])
    (project / "test_b.py").write_text("def test_b():\n    pass\n", encoding="utf-8")
    assert guard.main(["freeze"]) == 1
    capsys.readouterr()
    guard.main(["report"])
    assert "test_b.py: assert True" in capsys.readouterr().out


def test_closing_ends_the_protection(project, monkeypatch):
    guard.main(["freeze"])
    guard.main(["close"])
    assert edit(monkeypatch, project / "test_a.py") == 0
