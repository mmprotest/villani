from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from villani_ops.cli import unified
from villani_ops.closed_loop.interfaces import ClosedLoopRunResult
from villani_ops.closed_loop.presentation import build_run_presentation
from villani_ops.closed_loop.run_summary import (
    RunSummary,
    persist_run_summary,
)
from villani_ops.closed_loop.schema_validation import (
    ProtocolValidationError,
    validate_protocol_document,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
VALID_RUN = (
    REPOSITORY_ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"
)


def _bundle(tmp_path: Path) -> Path:
    destination = tmp_path / "run_protocol_fixture"
    shutil.copytree(VALID_RUN, destination)
    return destination


def _terminal_text(bundle: Path, monkeypatch) -> str:
    output = io.StringIO()
    monkeypatch.setattr(
        unified,
        "console",
        Console(file=output, width=220, color_system=None, force_terminal=False),
    )
    unified._print_terminal_summary(
        ClosedLoopRunResult(
            run_id="run_protocol_fixture",
            terminal_state="COMPLETED",
            selected_attempt_id="attempt_002",
            run_directory=bundle,
            actual_known_cost_usd=0.05,
            accounting_status="complete",
            failure_or_exhaustion_reason=None,
        )
    )
    return output.getvalue()


def test_cli_web_projection_reports_and_artifact_share_one_summary(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = _bundle(tmp_path)
    summary = persist_run_summary(bundle)
    presentation = build_run_presentation(bundle)
    terminal = _terminal_text(bundle, monkeypatch)

    artifact = json.loads((bundle / "run-summary.json").read_text(encoding="utf-8"))
    validate_protocol_document(artifact)
    assert RunSummary.model_validate(artifact) == summary
    assert presentation["canonical_summary"] == artifact
    assert presentation["validation"]["checks_passed"] == artifact["checks"]["passed"]
    assert presentation["validation"]["checks_failed"] == artifact["checks"]["failed"]
    assert (
        presentation["validation"]["focused_probes_unavailable"]
        == artifact["focused_probes"]["unavailable"]
    )
    assert (
        presentation["validation"]["requirements_proved"]
        == artifact["requirements"]["proved"]
    )
    assert "Checks and tests:" in terminal
    assert "1 passed; 0 failed; 0 not run; 0 unavailable (complete)" in terminal
    assert "Requirement coverage:" in terminal
    assert "1 proved; 0 not proved (complete)" in terminal
    for report_name in ("final_report.md", "selection_report.md"):
        report = (bundle / report_name).read_text(encoding="utf-8")
        assert "Repository checks: passed 1, failed 0, not run 0, unavailable 0." in report
        assert "Focused probes: passed 0, failed 0, not run 0, unavailable 0." in report
        assert "Requirements: proved 1, not proved 0." in report
        assert "Final acceptance: accepted (`accepted`)" in report


def test_unknown_accounting_and_counts_are_never_rendered_as_zero(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = _bundle(tmp_path)
    value = json.loads((bundle / "run-summary.json").read_text(encoding="utf-8"))
    value["checks"] = {
        "passed": None,
        "failed": None,
        "not_run": None,
        "unavailable": None,
        "accounting_status": "unknown",
    }
    value["focused_probes"] = dict(value["checks"])
    value["requirements"] = {
        "proved": None,
        "not_proved": None,
        "accounting_status": "unknown",
    }
    value["accounting"] = {
        "known": False,
        "accounting_status": "unknown",
        "total_cost": None,
        "currency": None,
    }
    (bundle / "run-summary.json").write_text(json.dumps(value), encoding="utf-8")

    presentation = build_run_presentation(bundle)
    terminal = _terminal_text(bundle, monkeypatch)

    assert presentation["validation"]["checks_passed"] is None
    assert (
        "Unknown passed; Unknown failed; Unknown not run; "
        "Unknown unavailable (unknown)"
    ) in terminal
    assert "Known cost:" in terminal
    assert "Unknown (unknown)" in terminal


def test_persist_replaces_a_stale_projection_after_source_evidence_changes(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    stale = json.loads((bundle / "run-summary.json").read_text(encoding="utf-8"))
    stale["checks"] = {
        "passed": None,
        "failed": None,
        "not_run": None,
        "unavailable": None,
        "accounting_status": "unknown",
    }
    validation_relative = "attempts/attempt_002/repository-validation.json"
    stale["source_artifacts"].append(validation_relative)
    validation_path = bundle / validation_relative
    validation_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "authoritative": True,
                "commands": [{"status": "passed", "exit_code": 0}],
            }
        ),
        encoding="utf-8",
    )
    summary_path = bundle / "run-summary.json"
    summary_path.write_text(json.dumps(stale), encoding="utf-8")
    newer = summary_path.stat().st_mtime_ns + 1_000_000_000
    os.utime(validation_path, ns=(newer, newer))

    refreshed = persist_run_summary(bundle)

    assert refreshed.checks.passed == 1
    stored = json.loads((bundle / "run-summary.json").read_text(encoding="utf-8"))
    assert stored["checks"]["passed"] == 1


def test_unknown_count_contract_rejects_numeric_zero() -> None:
    value = json.loads((VALID_RUN / "run-summary.json").read_text(encoding="utf-8"))
    value["checks"]["accounting_status"] = "unknown"

    with pytest.raises(ProtocolValidationError):
        validate_protocol_document(value)
