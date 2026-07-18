"""Strict ingestion of founder/evaluation trials into PT7 observations."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from villani_ops.evaluation_lab.models import EvaluationTrial
from villani_ops.evaluation_lab.reviews import latest_reviews, load_reviews
from villani_ops.evaluation_lab.workspace import (
    contains_secret,
    load_suite,
    load_task,
)

from ..agent_systems.models import AgentSystemIdentity
from ..protocol import ClassificationSnapshot
from .models import (
    QualificationArtifactReference,
    QualificationObservation,
    QualificationTaskProfile,
)
from .policy import task_profile
from .repository import canonical_digest, qualification_system_identity


MAXIMUM_QUALIFICATION_ARTIFACT_BYTES = 8 * 1024 * 1024


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return f"sha256:{value.hexdigest()}"


def _safe_artifact(
    root: Path, path: Path, *, kind: str
) -> QualificationArtifactReference:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(
            "qualification evidence artifact is outside its suite"
        ) from error
    if not resolved.is_file():
        raise ValueError(f"qualification evidence artifact is missing: {relative}")
    if resolved.stat().st_size > MAXIMUM_QUALIFICATION_ARTIFACT_BYTES:
        raise ValueError(
            f"qualification evidence artifact exceeds the bound: {relative}"
        )
    return QualificationArtifactReference(
        kind=kind,
        path=relative.as_posix(),
        digest=_digest(resolved),
    )


def _trial_path(root: Path, trial_id: str) -> Path:
    if not trial_id or PurePosixPath(trial_id).name != trial_id:
        raise ValueError("trial ID must be one safe path segment")
    return root / "trials" / trial_id / "trial.json"


def _normalize_harness(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    aliases = {
        "claude": "claude-code",
        "claude-code-cli": "claude-code",
        "villani": "villani-code",
    }
    return aliases.get(normalized, normalized)


def _validate_trial_identity(
    trial: EvaluationTrial, identity: AgentSystemIdentity
) -> None:
    recorded = trial.agent_system
    mismatches: list[str] = []
    if _normalize_harness(recorded.harness) != _normalize_harness(
        identity.harness.harness_id
    ):
        mismatches.append("harness")
    if recorded.harness_version != identity.harness.version:
        mismatches.append("harness_version")
    if (
        recorded.model is not None
        and recorded.model != identity.model_provider.model_id
    ):
        mismatches.append("model")
    if (
        recorded.provider is not None
        and recorded.provider != identity.model_provider.provider
    ):
        mismatches.append("provider")
    if recorded.execution_provider != identity.execution.execution_provider:
        mismatches.append("execution_provider")
    if identity.model_provider.serving_engine is not None and (
        recorded.serving_engine != identity.model_provider.serving_engine
    ):
        mismatches.append("serving_engine")
    if identity.model_provider.serving_engine_version is not None and (
        recorded.serving_engine_version
        != identity.model_provider.serving_engine_version
    ):
        mismatches.append("serving_engine_version")
    if mismatches:
        raise ValueError(
            "evaluation trial does not match the complete configured agent system: "
            + ", ".join(mismatches)
        )


def _classification_from_run(
    trial: EvaluationTrial, runs_root: Path | None
) -> QualificationTaskProfile | None:
    if trial.run_id is None or runs_root is None:
        return None
    path = runs_root / trial.run_id / "classification.json"
    if not path.is_file():
        return None
    classification = ClassificationSnapshot.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if classification.run_id != trial.run_id:
        raise ValueError("classification run identity does not match the trial")
    return task_profile(
        classification.category,
        classification.difficulty,
        classification.risk,
        classification.required_capabilities,
    )


def _explicit_profile(
    *,
    category: str | None,
    difficulty: str | None,
    risk: str | None,
    required_capabilities: Iterable[str],
) -> QualificationTaskProfile:
    if not category or not difficulty or not risk:
        raise ValueError(
            "missing authoritative run classification; provide category, difficulty, and risk explicitly"
        )
    return task_profile(category, difficulty, risk, required_capabilities)


def _artifact_references(
    root: Path,
    trial: EvaluationTrial,
    *,
    human_review_present: bool,
) -> tuple[list[QualificationArtifactReference], bool, bool]:
    required = [
        (root / "suite.json", "evaluation_suite"),
        (root / "tasks" / trial.task_id / "task.json", "evaluation_task"),
        (_trial_path(root, trial.trial_id), "evaluation_trial"),
    ]
    if human_review_present:
        required.append((root / "human-reviews.jsonl", "human_review_ledger"))
    trial_root = root / "trials" / trial.trial_id
    for reference in trial.artifact_references:
        normalized = reference.replace("\\", "/").strip("/")
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe evaluation artifact reference: {reference!r}")
        candidate = root / path
        if not candidate.exists():
            candidate = trial_root / path
        required.append((candidate, "trial_evidence"))
    artifacts: list[QualificationArtifactReference] = []
    complete = True
    secret_issue = False
    seen: set[str] = set()
    for path, kind in required:
        try:
            artifact = _safe_artifact(root, path, kind=kind)
        except (OSError, ValueError):
            complete = False
            continue
        if artifact.path in seen:
            continue
        seen.add(artifact.path)
        artifacts.append(artifact)
        try:
            if contains_secret(path.read_bytes()):
                secret_issue = True
        except OSError:
            complete = False
    return (
        sorted(artifacts, key=lambda item: (item.path, item.kind)),
        complete,
        secret_issue,
    )


def observation_from_evaluation_trial(
    suite_directory: str | Path,
    *,
    trial_id: str,
    identity: AgentSystemIdentity,
    category: str | None = None,
    difficulty: str | None = None,
    risk: str | None = None,
    required_capabilities: Iterable[str] = (),
    runs_root: str | Path | None = None,
) -> QualificationObservation:
    """Create one immutable observation without granting qualification itself."""

    root = Path(suite_directory).expanduser().resolve()
    suite = load_suite(root)
    path = _trial_path(root, trial_id)
    trial = EvaluationTrial.model_validate_json(path.read_text(encoding="utf-8"))
    if trial.trial_id != trial_id or trial.suite_id != suite.suite_id:
        raise ValueError("evaluation trial identity does not match its suite path")
    task = load_task(root, trial.task_id)
    if task.suite_id != suite.suite_id or task.content_digest != trial.task_digest:
        raise ValueError("evaluation task identity or digest does not match the trial")
    if suite.content_digest != trial.suite_digest:
        raise ValueError("evaluation suite digest does not match the trial")
    _validate_trial_identity(trial, identity)
    review = latest_reviews(load_reviews(root)).get(trial.trial_id)
    profile = _classification_from_run(
        trial,
        Path(runs_root).expanduser().resolve() if runs_root is not None else None,
    )
    profile_source = "authoritative_run_classification"
    if profile is None:
        profile = _explicit_profile(
            category=category,
            difficulty=difficulty,
            risk=risk,
            required_capabilities=required_capabilities,
        )
        profile_source = "explicit_evaluation_profile"

    artifacts, artifacts_complete, secret_issue = _artifact_references(
        root,
        trial,
        human_review_present=review is not None,
    )
    baseline_valid = bool(
        suite.status == "frozen"
        and suite.evidence_kind == "real_founder_work"
        and suite.disclosure_complete
        and task.frozen
        and task.evidence_kind == "real_founder_work"
        and task.evidence_eligible
        and task.source_snapshot.restore_verified
        and task.immutable_baseline_digest == trial.baseline_digest
        and trial.baseline_digest == trial.baseline_restore_digest
    )
    candidate_complete = bool(
        trial.status == "completed"
        and trial.evidence_eligible
        and artifacts_complete
        and trial.artifact_references
    )
    verification_complete = bool(
        trial.verification_status == "complete" and trial.proved_acceptable is not None
    )
    infrastructure_status = (
        "resolved"
        if trial.status == "completed" and trial.verification_status == "complete"
        else "excluded"
        if trial.status in {"excluded", "interrupted"}
        or trial.verification_status == "infrastructure_failure"
        else "unresolved"
    )
    human_status = "complete" if review is not None else "missing"
    corruption = not artifacts_complete
    eligible = bool(
        baseline_valid
        and candidate_complete
        and verification_complete
        and infrastructure_status == "resolved"
        and human_status == "complete"
        and not corruption
        and not secret_issue
    )
    if eligible:
        exclusion_reason = None
    elif secret_issue:
        exclusion_reason = "secret_issue"
    elif corruption:
        exclusion_reason = "corrupted_or_missing_artifact"
    elif not baseline_valid:
        exclusion_reason = "invalid_or_unfrozen_baseline"
    elif not candidate_complete:
        exclusion_reason = trial.exclusion_reason or "incomplete_candidate_evidence"
    elif infrastructure_status != "resolved":
        exclusion_reason = trial.exclusion_reason or "infrastructure_failure"
    elif not verification_complete:
        exclusion_reason = "authoritative_verification_missing"
    else:
        exclusion_reason = "required_human_review_missing"

    accepted_as_is = review.outcome == "accepted_as_is" if review is not None else None
    false_acceptance = bool(
        trial.false_acceptance is True
        or (review is not None and review.false_acceptance)
    )
    false_rejection = bool(
        trial.false_rejection is True or (review is not None and review.false_rejection)
    )
    later_rollback = bool(review is not None and review.later_rollback)
    reopened_defect = bool(review is not None and review.reopened_defect)
    successful = (
        bool(
            trial.proved_acceptable is True
            and accepted_as_is is True
            and not false_acceptance
            and not later_rollback
            and not reopened_defect
        )
        if eligible
        else None
    )
    system = qualification_system_identity(
        identity,
        environment_fingerprint=trial.agent_system.environment_fingerprint,
    )
    cost_known = (
        trial.total_cost.accounting_status == "complete"
        and trial.total_cost.value is not None
        and trial.total_cost.currency is not None
    )
    duration_known = (
        trial.duration.accounting_status == "complete"
        and trial.duration.value_ms is not None
    )
    recorded_at = (
        review.created_at
        if review is not None
        else trial.completed_at or trial.started_at
    )
    if recorded_at is None:
        raise ValueError("qualification evidence requires an observed timestamp")
    payload: dict[str, Any] = {
        "schema_version": "villani.qualification_observation.v1",
        "recorded_at": recorded_at,
        "observed_at": review.created_at if review is not None else recorded_at,
        "source_kind": "evaluation_trial",
        "source_suite_id": suite.suite_id,
        "source_suite_digest": suite.content_digest,
        "source_task_id": task.task_id,
        "source_task_digest": trial.task_digest,
        "source_trial_id": trial.trial_id,
        "source_review_id": review.review_id if review is not None else None,
        "repository_id": task.source_snapshot.repository_identity,
        "repository_commit": task.source_snapshot.resolved_commit,
        "repository_baseline_digest": task.immutable_baseline_digest,
        "task_profile": profile,
        "profile_source": profile_source,
        "system": system,
        "baseline_valid": baseline_valid,
        "candidate_evidence_complete": candidate_complete,
        "authoritative_verification_complete": verification_complete,
        "infrastructure_status": infrastructure_status,
        "human_review_required": True,
        "human_review_status": human_status,
        "corruption_detected": corruption,
        "secret_issue_detected": secret_issue,
        "target_repository_modified": trial.target_repository_modified,
        "proved_acceptable": trial.proved_acceptable if verification_complete else None,
        "accepted_as_is": accepted_as_is,
        "successful": successful,
        "false_acceptance": false_acceptance,
        "false_rejection": false_rejection,
        "later_rollback": later_rollback,
        "reopened_defect": reopened_defect,
        "cost_amount": trial.total_cost.value if cost_known else None,
        "cost_currency": trial.total_cost.currency if cost_known else None,
        "cost_accounting_status": (
            "complete" if cost_known else trial.total_cost.accounting_status
        ),
        "duration_ms": trial.duration.value_ms if duration_known else None,
        "duration_accounting_status": (
            "complete" if duration_known else trial.duration.accounting_status
        ),
        "review_minutes": review.review_minutes if review is not None else None,
        "eligible": eligible,
        "exclusion_reason": exclusion_reason,
        "artifacts": artifacts,
    }
    identity_payload = {
        key: (value.model_dump(mode="json") if hasattr(value, "model_dump") else value)
        for key, value in payload.items()
        if key not in {"recorded_at"}
    }
    observation_id = "qobs_" + canonical_digest(identity_payload).removeprefix(
        "sha256:"
    )
    return QualificationObservation(**payload, observation_id=observation_id)


__all__ = [
    "MAXIMUM_QUALIFICATION_ARTIFACT_BYTES",
    "observation_from_evaluation_trial",
]
