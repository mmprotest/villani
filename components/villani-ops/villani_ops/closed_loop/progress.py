"""Credible candidate-progress assessment from persisted attempt evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .interfaces import AttemptResult


class AttemptProgressAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    credible_progress: bool
    progress_score: float = Field(ge=0, le=1)
    relevant_patch_present: bool
    relevant_diff_ratio: float = Field(ge=0, le=1)
    validation_improvement_count: int = Field(ge=0)
    relevant_files_changed: int = Field(ge=0)
    irrelevant_files_changed: int = Field(ge=0)
    duplicate_read_ratio: float = Field(ge=0, le=1)
    repeated_failure_ratio: float = Field(ge=0, le=1)
    turns_after_last_progress: int = Field(ge=0)
    tokens_after_last_progress: int = Field(ge=0)
    reason_codes: list[str]
    actionable_feedback: bool = False
    materially_improved_patch_revision: bool = False
    candidate_quality_status: str = "unavailable"
    candidate_empty: bool = False
    irrelevant_patch_dominated: bool = False
    high_failure_repetition: bool = False


def empty_progress_assessment(
    reason_code: str = "progress_evidence_unavailable",
) -> AttemptProgressAssessment:
    """Return the fail-closed assessment used for legacy or incomplete evidence."""

    return AttemptProgressAssessment(
        credible_progress=False,
        progress_score=0.0,
        relevant_patch_present=False,
        relevant_diff_ratio=0.0,
        validation_improvement_count=0,
        relevant_files_changed=0,
        irrelevant_files_changed=0,
        duplicate_read_ratio=0.0,
        repeated_failure_ratio=0.0,
        turns_after_last_progress=0,
        tokens_after_last_progress=0,
        reason_codes=[reason_code],
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonnegative_int(value: object) -> int:
    if not isinstance(value, (str, bytes, int, float)):
        return 0
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return min(max(numerator / denominator, 0.0), 1.0)


def _verification_mapping(verification: object | None) -> Mapping[str, Any]:
    if verification is None:
        return {}
    if isinstance(verification, Mapping):
        return verification
    model_dump = getattr(verification, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="json")
        return value if isinstance(value, Mapping) else {}
    metadata = getattr(verification, "metadata", {})
    requirements = getattr(verification, "requirement_results", ())
    failures = getattr(verification, "failure_evidence", ())
    return {
        "metadata": metadata,
        "requirement_results": [
            {
                "outcome": getattr(item, "outcome", None),
                "requirement_id": getattr(item, "requirement_id", None),
            }
            for item in requirements
        ],
        "failure_evidence": [
            {"summary": getattr(item, "summary", None)} for item in failures
        ],
        "risk_flags": list(getattr(verification, "risk_flags", ())),
    }


def verification_actionable_correction(verification: object | None) -> bool:
    """Identify narrow correction evidence without interpreting task semantics."""

    value = _verification_mapping(verification)
    metadata = _mapping(value.get("metadata"))
    if not metadata and "metadata" not in value:
        metadata = value
    if metadata.get("actionable_correction") is True:
        return True
    if metadata.get("actionable_correction") is False:
        return False
    infrastructure_markers = {
        "infrastructure_error",
        "timed_out",
        "timeout",
        "error",
        "malformed_output",
    }
    if str(metadata.get("repository_validation_status") or "") in {
        "infrastructure_error"
    }:
        return False
    if str(metadata.get("focused_probe_status") or "") in infrastructure_markers:
        return False
    if str(metadata.get("invocation_status") or "") in infrastructure_markers:
        return False
    risk_flags = " ".join(str(item).lower() for item in value.get("risk_flags", []))
    if any(
        marker in risk_flags
        for marker in (
            "major_regression",
            "incorrect_task_interpretation",
            "capability_failure",
        )
    ):
        return False
    failed_requirements = [
        item
        for item in value.get("requirement_results", [])
        if isinstance(item, Mapping) and item.get("outcome") == "failed"
    ]
    if 1 <= len(failed_requirements) <= 2:
        return True
    repository_status = str(metadata.get("repository_validation_status") or "")
    if repository_status == "failed":
        return True
    reason_code = str(metadata.get("computed_final_reason_code") or "")
    return reason_code in {
        "focused_probe_failed",
        "critical_requirement_failed",
        "repository_validation_failed",
    }


def assess_attempt_progress(
    attempt: AttemptResult,
    verification: object | None = None,
) -> AttemptProgressAssessment:
    """Compute credible progress from patch, quality, validation, and efficiency data."""

    metadata = _mapping(attempt.metadata)
    runner_metrics = _mapping(metadata.get("runner_metrics"))
    telemetry = runner_metrics or _mapping(attempt.runner_telemetry)
    quality = _mapping(metadata.get("candidate_quality_report"))
    tracked = [str(item) for item in quality.get("tracked_files_changed", [])]
    relevant = [str(item) for item in quality.get("relevant_files_changed", [])]
    untracked = [str(item) for item in quality.get("untracked_files", [])]
    relevant_count = len(set(relevant))
    changed_count = len(set((*tracked, *untracked)))
    irrelevant_count = max(changed_count - relevant_count, 0)
    quality_status = str(quality.get("status") or "unavailable")
    quality_reasons = {str(item) for item in quality.get("reason_codes", [])}
    semantic_lines = _nonnegative_int(quality.get("semantic_lines_added")) + _nonnegative_int(
        quality.get("semantic_lines_removed")
    )
    line_ending_only = bool(
        _nonnegative_int(quality.get("line_ending_only_lines")) > 0
        and semantic_lines == 0
    )
    patch_present = bool(attempt.patch and attempt.patch.strip())
    candidate_empty = bool(
        not patch_present
        or "empty_patch" in quality_reasons
        or "only_villani_owned_files" in quality_reasons
        or "only_ignored_files" in quality_reasons
        or "scratch_only_candidate" in quality_reasons
    )
    relevant_patch = bool(
        patch_present
        and relevant_count > 0
        and semantic_lines > 0
        and not line_ending_only
        and quality_status != "ineligible"
    )
    relevant_diff_ratio = float(
        quality.get(
            "relevant_diff_ratio", metadata.get("relevant_diff_ratio", 0.0)
        )
        or 0.0
    )
    relevant_diff_ratio = min(max(relevant_diff_ratio, 0.0), 1.0)
    validation_improvements = _nonnegative_int(
        telemetry.get("validation_improvement_count")
    )
    patch_revisions = _nonnegative_int(telemetry.get("relevant_patch_revisions"))
    materially_improved = bool(
        patch_revisions > 1
        or metadata.get("materially_improved_patch_revision") is True
    )
    duplicate_reads = _nonnegative_int(telemetry.get("duplicate_file_reads"))
    total_reads = _nonnegative_int(telemetry.get("total_file_reads"))
    if total_reads == 0:
        total_reads = duplicate_reads + _nonnegative_int(
            telemetry.get("unique_files_read")
        )
    repeated_failures = _nonnegative_int(
        telemetry.get("repeated_command_failures")
    )
    failed_commands = _nonnegative_int(telemetry.get("commands_failed"))
    if failed_commands == 0:
        failed_commands = repeated_failures + _nonnegative_int(
            telemetry.get("unique_command_failures")
        )
    duplicate_ratio = _ratio(duplicate_reads, total_reads)
    repeated_ratio = _ratio(repeated_failures, failed_commands)
    turns_after = _nonnegative_int(telemetry.get("turns_after_last_relevant_progress"))
    tokens_after = _nonnegative_int(
        telemetry.get("tokens_after_last_relevant_progress")
    )
    actionable = verification_actionable_correction(verification)
    plausible_candidate = bool(
        patch_present
        and quality_status != "ineligible"
        and (relevant_count > 0 or relevant_diff_ratio > 0)
    )
    irrelevant_dominated = bool(
        patch_present
        and (
            relevant_count == 0
            or (changed_count > 0 and irrelevant_count > relevant_count)
            or (relevant_diff_ratio > 0 and relevant_diff_ratio < 0.25)
        )
    )
    high_repetition = repeated_ratio >= 0.50 and repeated_failures > 0
    trigger = bool(
        relevant_patch
        or validation_improvements > 0
        or (actionable and plausible_candidate)
        or materially_improved
    )
    blockers = bool(
        candidate_empty
        or line_ending_only
        or quality_status == "ineligible"
        or irrelevant_dominated
        or high_repetition
    )
    credible = trigger and not blockers

    score = 0.0
    score += 0.45 if relevant_patch else 0.0
    score += min(validation_improvements, 2) * 0.125
    score += 0.20 if actionable and plausible_candidate else 0.0
    score += 0.10 if materially_improved else 0.0
    score -= duplicate_ratio * 0.05
    score -= repeated_ratio * 0.20
    score -= 0.20 if irrelevant_dominated else 0.0
    score = min(max(score, 0.0), 1.0)

    reasons: list[str] = []
    if relevant_patch:
        reasons.append("relevant_tracked_patch")
    if validation_improvements:
        reasons.append("validation_improved")
    if actionable and plausible_candidate:
        reasons.append("actionable_verifier_feedback")
    if materially_improved:
        reasons.append("material_patch_revision")
    if candidate_empty:
        reasons.append("empty_or_non_candidate_patch")
    if line_ending_only:
        reasons.append("line_ending_only_change")
    if quality_status == "ineligible":
        reasons.append("candidate_quality_ineligible")
    if irrelevant_dominated:
        reasons.append("irrelevant_patch_dominated")
    if high_repetition:
        reasons.append("high_repeated_failure_ratio")
    if duplicate_ratio > 0:
        reasons.append("duplicate_reads_present")
    if not trigger:
        reasons.append("no_credible_progress_signal")

    return AttemptProgressAssessment(
        credible_progress=credible,
        progress_score=score,
        relevant_patch_present=relevant_patch,
        relevant_diff_ratio=relevant_diff_ratio,
        validation_improvement_count=validation_improvements,
        relevant_files_changed=relevant_count,
        irrelevant_files_changed=irrelevant_count,
        duplicate_read_ratio=duplicate_ratio,
        repeated_failure_ratio=repeated_ratio,
        turns_after_last_progress=turns_after,
        tokens_after_last_progress=tokens_after,
        reason_codes=reasons,
        actionable_feedback=actionable,
        materially_improved_patch_revision=materially_improved,
        candidate_quality_status=quality_status,
        candidate_empty=candidate_empty,
        irrelevant_patch_dominated=irrelevant_dominated,
        high_failure_repetition=high_repetition,
    )


__all__ = [
    "AttemptProgressAssessment",
    "assess_attempt_progress",
    "empty_progress_assessment",
    "verification_actionable_correction",
]
