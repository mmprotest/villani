"""Persisted user-facing delivery and approval state.

This module is deliberately presentation-neutral.  It records the selected
patch, the evidence a person needs to review it, and the authority decision
that permits (or blocks) repository mutation.  Candidate eligibility remains
owned by the verifier and selector.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .event_writer import redact_data
from .protocol import (
    AccountingStatus,
    AttemptSnapshot,
    FailureDetail,
    SelectionSnapshot,
    VerificationSnapshot,
)


WORKFLOW_VERSION = "villani.delivery_workflow.v1"
DeliveryMode = Literal["suggest", "approve", "apply", "branch", "pull-request"]
DeliveryState = Literal[
    "selected",
    "awaiting_approval",
    "approved",
    "suggested",
    "applied",
    "branched",
    "pull_request_created",
    "rejected",
    "rerun_requested",
    "timed_out",
    "failed",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalWorkflow(_FrozenModel):
    required: bool
    status: Literal[
        "not_required",
        "pending",
        "approved",
        "rejected",
        "rerun_requested",
        "timed_out",
    ]
    request_id: str | None = None
    requested_at: datetime | None = None
    deadline: datetime | None = None
    timeout_policy: Literal["reject", "suggest", "fail"] = "reject"
    authenticated_required: bool = False
    allow_candidate_change: bool = False
    actor: str | None = None
    authentication_type: str | None = None
    decided_at: datetime | None = None
    reason: str | None = None


class PatchReview(_FrozenModel):
    files_changed: tuple[str, ...] = ()
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    validation_evidence: tuple[dict[str, Any], ...] = ()
    verifier_authority: str
    candidate_comparison: tuple[dict[str, Any], ...] = ()
    remaining_risks: tuple[str, ...] = ()
    cost: dict[str, Any]
    unrelated_change_warnings: tuple[str, ...] = ()
    sensitive_file_warnings: tuple[str, ...] = ()


class DeliveryRecord(_FrozenModel):
    schema_version: Literal["villani.delivery_state.v1"] = "villani.delivery_state.v1"
    workflow_version: Literal["villani.delivery_workflow.v1"] = WORKFLOW_VERSION
    delivery_id: str
    run_id: str
    trace_id: str
    selection_id: str
    selected_attempt_id: str
    mode: DeliveryMode
    state: DeliveryState
    requested_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    repository_path: str
    repository_modified: bool = False
    target_worktree_modified: bool = False
    patch_artifact: str
    patch_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    changed_files: tuple[str, ...] = ()
    review: PatchReview
    authority: dict[str, Any]
    approval: ApprovalWorkflow
    result: dict[str, Any] = Field(default_factory=dict)
    failure: FailureDetail | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def delivery_configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    value = configuration.get("delivery")
    return dict(value) if isinstance(value, Mapping) else {}


def workflow_enabled(configuration: Mapping[str, Any]) -> bool:
    return (
        delivery_configuration(configuration).get("workflow_version")
        == WORKFLOW_VERSION
    )


def configured_delivery_mode(configuration: Mapping[str, Any]) -> DeliveryMode:
    delivery = delivery_configuration(configuration)
    value = delivery.get("mode")
    if value in {"suggest", "approve", "apply", "branch", "pull-request"}:
        return value
    kind = str(delivery.get("materialization_type") or "local_patch_apply")
    return {
        "patch_export": "suggest",
        "local_branch": "branch",
        "local_branch_commit": "branch",
        "pull_request": "pull-request",
    }.get(kind, "apply")  # type: ignore[return-value]


def materialization_type_for_mode(mode: DeliveryMode) -> str:
    return {
        "suggest": "patch_export",
        "approve": "local_patch_apply",
        "apply": "local_patch_apply",
        "branch": "local_branch",
        "pull-request": "pull_request",
    }[mode]


def successful_delivery_state(mode: DeliveryMode) -> DeliveryState:
    return {
        "suggest": "suggested",
        "approve": "applied",
        "apply": "applied",
        "branch": "branched",
        "pull-request": "pull_request_created",
    }[mode]  # type: ignore[return-value]


def _captured_files(attempt: AttemptSnapshot, patch: str) -> tuple[str, ...]:
    captured = attempt.metadata.get("changed_files")
    if isinstance(captured, (list, tuple)) and all(
        isinstance(item, str)
        and item
        and not Path(item).is_absolute()
        and ".." not in Path(item).parts
        for item in captured
    ):
        return tuple(sorted(set(captured)))
    paths = re.findall(r"^(?:\+\+\+ b/|--- a/)(.+)$", patch, re.MULTILINE)
    return tuple(sorted({item for item in paths if item != "/dev/null"}))


def patch_statistics(patch: str) -> tuple[int, int]:
    insertions = sum(
        line.startswith("+") and not line.startswith("+++")
        for line in patch.splitlines()
    )
    deletions = sum(
        line.startswith("-") and not line.startswith("---")
        for line in patch.splitlines()
    )
    return int(insertions), int(deletions)


_SENSITIVE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "secrets.yml",
    "secrets.yaml",
}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".kdbx"}


def _sensitive_warnings(paths: Sequence[str]) -> tuple[str, ...]:
    warnings: list[str] = []
    for value in paths:
        path = Path(value)
        lowered = path.name.lower()
        if lowered in _SENSITIVE_NAMES or path.suffix.lower() in _SENSITIVE_SUFFIXES:
            warnings.append(f"Sensitive-file pattern matched: {value}")
    return tuple(warnings)


def _unrelated_warnings(
    attempt: AttemptSnapshot, paths: Sequence[str]
) -> tuple[str, ...]:
    warnings: list[str] = []
    for key in ("unrelated_change_warnings", "scope_warnings"):
        value = attempt.metadata.get(key)
        if isinstance(value, (list, tuple)):
            warnings.extend(str(item) for item in value if str(item))
    for value in paths:
        parts = {part.lower() for part in Path(value).parts}
        if ".git" in parts or ".villani" in parts or "runs" in parts:
            warnings.append(f"Villani/internal-state path requires review: {value}")
    return tuple(dict.fromkeys(warnings))


def build_patch_review(
    *,
    attempt: AttemptSnapshot,
    verification: VerificationSnapshot,
    selection: SelectionSnapshot,
    patch: str,
    total_cost: float | None,
    accounting_status: AccountingStatus,
    currency: str,
) -> PatchReview:
    files = _captured_files(attempt, patch)
    insertions, deletions = patch_statistics(patch)
    authority = str(
        verification.metadata.get("authority_source")
        or verification.metadata.get("verification_mode")
        or "normalized acceptance-grade verifier"
    )
    validation = tuple(
        {
            "evidence_id": item.evidence_id,
            "kind": item.kind,
            "summary": item.summary,
            "artifact_path": item.artifact_path,
        }
        for item in verification.success_evidence
    )
    comparison = tuple(item.model_dump(mode="json") for item in selection.rankings)
    risks = tuple(
        dict.fromkeys(
            [*verification.risk_flags]
            + [item.summary for item in verification.missing_evidence]
        )
    )
    return PatchReview(
        files_changed=files,
        insertions=insertions,
        deletions=deletions,
        validation_evidence=tuple(redact_data(item) for item in validation),
        verifier_authority=authority,
        candidate_comparison=tuple(redact_data(item) for item in comparison),
        remaining_risks=risks,
        cost={
            "value": total_cost,
            "accounting_status": accounting_status,
            "currency": currency,
        },
        unrelated_change_warnings=_unrelated_warnings(attempt, files),
        sensitive_file_warnings=_sensitive_warnings(files),
    )


def automatic_authority(
    configuration: Mapping[str, Any],
    verification: VerificationSnapshot,
    *,
    risk: str | None,
) -> dict[str, Any]:
    delivery = delivery_configuration(configuration)
    policy_value = delivery.get("authority_policy")
    policy = dict(policy_value) if isinstance(policy_value, Mapping) else {}
    reasons: list[str] = []
    if not policy:
        reasons.append("no automatic-delivery authority policy is configured")
    if not bool(policy.get("allow_automatic", False)):
        reasons.append("automatic delivery is not permitted by policy")
    if bool(policy.get("require_acceptance_eligible", True)) and not (
        verification.outcome == "accepted" and verification.acceptance_eligible
    ):
        reasons.append("acceptance-grade verifier evidence is absent")
    allowed_risks = policy.get("allowed_risks")
    if isinstance(allowed_risks, (list, tuple)) and risk not in allowed_risks:
        reasons.append(f"risk {risk or 'unknown'} is outside automatic-delivery policy")
    required_authorities = policy.get("allowed_authority_sources")
    observed_authority = str(
        verification.metadata.get("authority_source")
        or verification.metadata.get("verification_mode")
        or "normalized_verifier"
    )
    if isinstance(required_authorities, (list, tuple)) and required_authorities:
        if observed_authority not in required_authorities:
            reasons.append(
                "verifier authority source is not permitted for automatic delivery"
            )
    return {
        "policy_version": str(policy.get("policy_version") or "unconfigured"),
        "required": "acceptance-grade evidence plus configured automatic authority",
        "observed": observed_authority,
        "permitted": not reasons,
        "reasons": reasons or ["configured authority requirements are satisfied"],
    }


def patch_digest(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()
