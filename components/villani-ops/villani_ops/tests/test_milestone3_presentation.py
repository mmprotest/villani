from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from villani_ops.closed_loop.presentation import (
    FAILURE_CATALOG,
    build_run_presentation,
    failure_experience,
    progress_lines_for_event,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
VALID_RUN = (
    REPOSITORY_ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"
)


def _bundle(tmp_path: Path, name: str = "run") -> Path:
    destination = tmp_path / name
    shutil.copytree(VALID_RUN, destination)
    return destination


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _append_repository_validation(
    bundle: Path,
    *,
    attempt_id: str = "attempt_002",
    exit_code: int = 0,
) -> None:
    event = {
        "schema_version": "villani.event.v1",
        "event_id": "evt_repository_validation",
        "sequence": 25,
        "timestamp": "2026-07-10T00:00:21Z",
        "trace_id": "trace_protocol_fixture",
        "run_id": "run_protocol_fixture",
        "attempt_id": attempt_id,
        "parent_event_id": "evt_020",
        "source": "verifier",
        "event_type": "command_completed" if exit_code == 0 else "command_failed",
        "payload": {
            "command_role": "repository_validation",
            "argv": ["python", "-m", "pytest", "-q"],
            "exit_code": exit_code,
            "run_id": "run_protocol_fixture",
            "attempt_id": attempt_id,
            "worktree_path": ".worktrees/attempt_002",
            "baseline_sha256": "a" * 64,
            "candidate_state": "post_mutation",
            "validation_id": "repository_validation_001",
        },
    }
    with (bundle / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def _terminal(bundle: Path, state_name: str, reason: str) -> None:
    state = _read(bundle / "state.json")
    state.update(
        {
            "state": state_name,
            "terminal": True,
            "metadata": {"terminal_reason": reason},
        }
    )
    _write(bundle / "state.json", state)
    manifest = _read(bundle / "manifest.json")
    manifest["final_state"] = state_name
    _write(bundle / "manifest.json", manifest)


def test_first_attempt_success_is_answer_first_and_authoritative(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    manifest = _read(bundle / "manifest.json")
    manifest["attempt_ids"] = ["attempt_002"]
    _write(bundle / "manifest.json", manifest)
    attempt = _read(bundle / "attempts" / "attempt_002" / "attempt.json")
    attempt["ordinal"] = 1
    _write(bundle / "attempts" / "attempt_002" / "attempt.json", attempt)
    _append_repository_validation(bundle)

    presentation = build_run_presentation(bundle)

    assert presentation["outcome"] == "ACCEPTED"
    assert presentation["changed"]["files"] == ["calculator.py"]
    assert presentation["confidence"]["acceptance_eligible"] is True
    assert presentation["validation"]["checks_passed"] == 1
    assert presentation["validation"]["authority"] == "executed_repository_validation"
    assert len(presentation["attempts"]) == 1
    assert "controller_state" not in presentation


def test_role_label_without_canonical_validation_identity_is_not_authoritative(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    _append_repository_validation(bundle)
    events_path = bundle / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    malformed = json.loads(lines[-1])
    malformed["payload"].pop("baseline_sha256")
    lines[-1] = json.dumps(malformed)
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    presentation = build_run_presentation(bundle)

    assert presentation["validation"]["checks_passed"] == 0
    assert presentation["validation"]["authority"] == "none"


def test_escalation_success_and_multiple_attempts_are_explained(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _append_repository_validation(bundle)

    presentation = build_run_presentation(bundle)

    assert presentation["outcome"] == "ACCEPTED"
    assert len(presentation["attempts"]) == 2
    assert any("Escalated" in item for item in presentation["recovery"])
    assert any("Selected attempt 2" in item for item in presentation["recovery"])


def test_exhausted_run_explains_missing_evidence_and_preserved_patch(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    _terminal(
        bundle, "EXHAUSTED", "attempt budget exhausted without authoritative evidence"
    )

    presentation = build_run_presentation(bundle)

    assert presentation["outcome"] == "EXHAUSTED"
    assert presentation["failure"]["code"] == "no_authoritative_evidence"
    assert presentation["failure"]["patch_preserved"] is True
    assert presentation["failure"]["missing_evidence"]
    assert presentation["next_actions"]


def test_heuristic_only_result_remains_ineligible(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    verification_path = bundle / "verification" / "attempt_002.json"
    verification = _read(verification_path)
    verification.update(
        {
            "outcome": "unclear",
            "acceptance_eligible": False,
            "reason": "Heuristic evidence only; no authoritative evidence is present.",
            "metadata": {"verification_mode": "heuristic_only"},
        }
    )
    _write(verification_path, verification)
    _terminal(bundle, "EXHAUSTED", "heuristic evidence only; no authoritative evidence")

    presentation = build_run_presentation(bundle)

    assert presentation["confidence"]["acceptance_eligible"] is False
    assert presentation["confidence"]["label"] == "not acceptance eligible"
    assert presentation["failure"]["code"] == "no_authoritative_evidence"


def test_verifier_escalation_is_visible_without_a_second_coding_charge(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    verification_path = bundle / "verification" / "attempt_002.json"
    verification = _read(verification_path)
    verification["metadata"] = {
        "verifier_calls": [
            {"backend": "local-reviewer", "cost": 0.01},
            {"backend": "expert-reviewer", "cost": 0.02},
        ]
    }
    _write(verification_path, verification)

    presentation = build_run_presentation(bundle)

    assert "Escalated across 2 verifier routes" in presentation["recovery"]
    assert len(presentation["attempts"]) == 2


def test_presentation_redacts_secrets(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    task_path = bundle / "task.json"
    task = _read(task_path)
    task["instruction"] = "Fix it with api_key=super-secret-value"
    task["metadata"] = {"authorization": "Bearer private-token"}
    _write(task_path, task)
    verification_path = bundle / "verification" / "attempt_002.json"
    verification = _read(verification_path)
    verification["reason"] = "Accepted with api_key=super-secret-value"
    _write(verification_path, verification)

    encoded = json.dumps(build_run_presentation(bundle))

    assert "super-secret-value" not in encoded
    assert "private-token" not in encoded
    assert "REDACTED" in encoded


def test_synchronization_pending_preserves_local_authority(tmp_path: Path) -> None:
    presentation = build_run_presentation(
        _bundle(tmp_path), synchronization_state="SYNC PENDING"
    )

    assert presentation["synchronization_state"] == "SYNC PENDING"
    assert any(
        "canonical local run" in item for item in presentation["remaining_risks"]
    )


def test_synchronization_failure_has_complete_recovery_experience(
    tmp_path: Path,
) -> None:
    presentation = build_run_presentation(
        _bundle(tmp_path), synchronization_state="SYNC FAILED"
    )

    assert presentation["outcome"] == "ACCEPTED"
    failure = presentation["synchronization_failure"]
    assert failure["code"] == "synchronization_failure"
    assert failure["what_villani_tried"]
    assert failure["missing_evidence"]
    assert failure["patch_status"]
    assert failure["next_action"]
    assert any(
        item["label"] == "Retry synchronization"
        for item in presentation["next_actions"]
    )


def test_repository_change_before_apply_is_a_specific_safe_failure(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    _terminal(
        bundle,
        "FAILED",
        "repository changed after verification and before materialization",
    )
    state = _read(bundle / "state.json")
    state["failure"] = {
        "code": "repository_changed_before_materialization",
        "message": "repository changed before materialization",
        "details": {},
    }
    _write(bundle / "state.json", state)

    presentation = build_run_presentation(bundle)

    assert (
        presentation["failure"]["code"] == "repository_changed_before_materialization"
    )
    assert presentation["failure"]["patch_preserved"] is True
    assert presentation["patch"]["applied"] is False


def test_unknown_cost_is_null_and_never_rendered_as_zero(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    manifest = _read(bundle / "manifest.json")
    manifest["total_cost_usd"] = None
    manifest["cost_accounting_status"] = "unknown"
    manifest["stage_metrics"] = {}
    _write(bundle / "manifest.json", manifest)

    cost = build_run_presentation(bundle)["cost"]

    assert cost["coding"] is None
    assert cost["verification"] is None
    assert cost["total"] is None
    assert cost["accounting_status"] == "unknown"


def test_zero_file_change_is_explicit(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    materialization_path = bundle / "materialization.json"
    materialization = _read(materialization_path)
    materialization["changed_files"] = []
    _write(materialization_path, materialization)

    presentation = build_run_presentation(bundle)

    assert presentation["changed"]["file_count"] == 0
    assert presentation["changed"]["zero_file_change"] is True
    assert any(
        "no file changes" in item.lower() for item in presentation["remaining_risks"]
    )


def test_rerun_lineage_exposes_previous_and_current_runs(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    manifest = _read(bundle / "manifest.json")
    metadata = dict(manifest.get("metadata", {}))
    metadata["lineage"] = {
        "relationship": "rerun",
        "parent_run_id": "run_previous",
        "root_run_id": "run_previous",
        "cost_accounting": "new_run_only",
    }
    manifest["metadata"] = metadata
    _write(bundle / "manifest.json", manifest)

    presentation = build_run_presentation(bundle)

    assert presentation["run_id"] == "run_protocol_fixture"
    assert presentation["lineage"]["parent_run_id"] == "run_previous"
    assert presentation["lineage"]["cost_accounting"] == "new_run_only"


@pytest.mark.parametrize("code", sorted(FAILURE_CATALOG))
def test_every_terminal_failure_has_a_complete_recovery_experience(code: str) -> None:
    value = failure_experience(code, attempts=2, patch_preserved=True)

    assert value["what_failed"]
    assert value["what_villani_tried"]
    assert value["missing_evidence"]
    assert value["patch_status"]
    assert value["next_action"]


def test_raw_event_names_are_opt_in() -> None:
    event = {
        "event_type": "classification_completed",
        "payload": {
            "effective_classification": {"difficulty": "medium", "risk": "low"}
        },
    }

    normal = progress_lines_for_event(event)
    verbose = progress_lines_for_event(event, include_raw=True)

    assert normal[0]["message"] == "Classified as medium difficulty, low risk"
    assert "raw_event_type" not in normal[0]
    assert verbose[0]["raw_event_type"] == "classification_completed"
