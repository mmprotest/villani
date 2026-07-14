from __future__ import annotations

import json
from pathlib import Path

from villani_ops.execution_environment.validation_discovery import (
    CONFIRMATION_THRESHOLD,
    confirmed_command,
    discover_repository_validation,
    parse_manual_command,
)


def test_package_metadata_discovers_advisory_exact_argv(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )

    result = discover_repository_validation(tmp_path)

    suggestion = result["suggestions"][0]
    assert suggestion["argv"] == ["npm", "test"]
    assert suggestion["advisory_only"] is True
    assert suggestion["authoritative"] is False
    assert result["authority"] == "none_until_confirmed_command_execution"
    assert result["metadata"]["language_routing_applied"] is False


def test_conventional_tests_directory_is_low_confidence_and_requires_confirmation(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()

    suggestion = discover_repository_validation(tmp_path)["suggestions"][0]

    assert suggestion["confidence"] < CONFIRMATION_THRESHOLD
    assert suggestion["requires_confirmation"] is True
    assert suggestion["argv"] == ["python", "-m", "pytest", "-q"]


def test_root_unittest_declaration_is_discovered_without_language_routing(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_calculator.py").write_text(
        "import unittest\n\nclass CalculatorTests(unittest.TestCase):\n    pass\n",
        encoding="utf-8",
    )

    result = discover_repository_validation(tmp_path)

    suggestion = result["suggestions"][0]
    assert suggestion["argv"] == ["python", "-m", "unittest", "-q"]
    assert suggestion["requires_confirmation"] is False
    assert suggestion["authoritative"] is False
    assert result["metadata"]["language_routing_applied"] is False


def test_manual_override_records_boundary_but_not_authority() -> None:
    argv = parse_manual_command('python -m pytest "tests/unit tests" -q')
    value = confirmed_command(
        argv,
        source="manual_override",
        confidence=1.0,
        confirmed_by="test",
    )

    assert value["argv"] == ["python", "-m", "pytest", "tests/unit tests", "-q"]
    assert value["confirmed"] is True
    assert value["authoritative"] is False
    assert value["authority_begins"] == "on_structured_repository_validation_execution"
