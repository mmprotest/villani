from __future__ import annotations

from pathlib import Path

from villani_code.autonomy import TakeoverPlanner


def test_repo_summary_counts_meaningful_python_tests(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    summary = TakeoverPlanner(tmp_path).build_repo_summary()

    assert "has_tests=1" in summary
    tests_count = int(summary.split("tests=", 1)[1].split()[0])
    assert tests_count >= 1


def test_repo_summary_does_not_count_non_python_tests_dir_entries(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "data.txt").write_text("x", encoding="utf-8")

    summary = TakeoverPlanner(tmp_path).build_repo_summary()

    assert "has_tests=0" in summary
    tests_count = int(summary.split("tests=", 1)[1].split()[0])
    assert tests_count == 0
