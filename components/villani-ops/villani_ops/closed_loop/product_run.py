"""Shared, fail-closed product presentation for canonical Villani runs.

The controller remains the only acceptance and delivery authority.  This module
projects persisted controller artifacts into the versioned contract consumed by
both the CLI and Console.  It deliberately does not inspect route identity,
cost, or competing candidates when deciding whether a result was proved.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from .durable_io import read_jsonl_tolerant, write_json_atomic
from .protocol import AccountingStatus, StrictProtocolModel
from .run_summary import canonical_run_summary
from .adaptive_verification.models import CompactReviewPackage


ProductStage = Literal["Understanding", "Working", "Checking", "Ready"]
ProductVerdict = Literal[
    "Ready to apply", "Needs review", "Could not prove", "Cancelled"
]


class ProductRunIdentity(StrictProtocolModel):
    run_id: str = Field(min_length=1)
    trace_id: str | None = None


class ProductTaskSummary(StrictProtocolModel):
    task: str = Field(min_length=1)
    success_criteria: str | None = None
    repository: str | None = None


class ProductStageTransition(StrictProtocolModel):
    sequence: int = Field(ge=1)
    timestamp: str = Field(min_length=1)
    stage: ProductStage
    sentence: str = Field(min_length=1)


class ProductEvidenceCounts(StrictProtocolModel):
    passed: int | None = Field(default=None, ge=0)
    failed: int | None = Field(default=None, ge=0)
    not_run: int | None = Field(default=None, ge=0)
    unavailable: int | None = Field(default=None, ge=0)
    accounting_status: Literal["complete", "unknown"]

    @model_validator(mode="after")
    def validate_counts(self) -> "ProductEvidenceCounts":
        values = (self.passed, self.failed, self.not_run, self.unavailable)
        if self.accounting_status == "complete" and any(
            value is None for value in values
        ):
            raise ValueError("complete check accounting requires every count")
        if self.accounting_status == "unknown" and any(
            value is not None for value in values
        ):
            raise ValueError("unknown check accounting requires null counts")
        return self


class ProductRequirementCounts(StrictProtocolModel):
    proved: int | None = Field(default=None, ge=0)
    not_proved: int | None = Field(default=None, ge=0)
    accounting_status: Literal["complete", "unknown"]

    @model_validator(mode="after")
    def validate_counts(self) -> "ProductRequirementCounts":
        values = (self.proved, self.not_proved)
        if self.accounting_status == "complete" and any(
            value is None for value in values
        ):
            raise ValueError("complete requirement accounting requires every count")
        if self.accounting_status == "unknown" and any(
            value is not None for value in values
        ):
            raise ValueError("unknown requirement accounting requires null counts")
        return self


class ProductCost(StrictProtocolModel):
    value: float | None = Field(default=None, ge=0)
    currency: str | None = None
    accounting_status: AccountingStatus

    @model_validator(mode="after")
    def validate_cost(self) -> "ProductCost":
        if self.accounting_status == "complete" and self.value is None:
            raise ValueError("complete cost accounting requires a value")
        if (
            self.accounting_status in {"unknown", "not_applicable"}
            and self.value is not None
        ):
            raise ValueError("unknown or not-applicable cost must remain null")
        if self.value is None and self.currency is not None:
            raise ValueError("unknown cost cannot claim a currency")
        if self.value is not None and not self.currency:
            raise ValueError("known cost requires a currency")
        return self


class ProductDuration(StrictProtocolModel):
    value_ms: int | None = Field(default=None, ge=0)
    accounting_status: AccountingStatus

    @model_validator(mode="after")
    def validate_duration(self) -> "ProductDuration":
        if self.accounting_status == "complete" and self.value_ms is None:
            raise ValueError("complete duration accounting requires a value")
        if (
            self.accounting_status in {"unknown", "not_applicable"}
            and self.value_ms is not None
        ):
            raise ValueError("unknown or not-applicable duration must remain null")
        return self


class ProductAgentSystem(StrictProtocolModel):
    name: str
    backend: str | None = None
    model: str | None = None


class ProductRoleInfrastructureFailure(StrictProtocolModel):
    stage: Literal["classification", "coding", "verification", "selection"]
    role: Literal["classification", "coding", "verification", "selection"]
    agent_system_id: str = Field(min_length=1)
    safe_error_summary: str = Field(min_length=1)
    target_repository_modified: bool
    partial_patch_preserved: bool
    automatic_fallback_performed: bool
    exact_repair_action: str = Field(min_length=1)
    evidence_path: str = Field(min_length=1)


class ProductRoleExecution(StrictProtocolModel):
    role: Literal["classification", "coding", "verification", "selection"]
    label: Literal["Understand task", "Write code", "Verify result", "Choose candidate"]
    agent_system_id: str = Field(min_length=1)
    system_name: str = Field(min_length=1)
    driver: str = Field(min_length=1)
    model: str | None = None
    invocation_count: int = Field(ge=0)
    status: Literal["recorded", "succeeded", "infrastructure_failure", "not_invoked"]
    evidence_artifact: str = Field(min_length=1)
    infrastructure_failure: ProductRoleInfrastructureFailure | None = None


class ProductEscalationSummary(StrictProtocolModel):
    attempts: int = Field(ge=0)
    retries: int = Field(ge=0)
    escalations: int = Field(ge=0)
    summary: str = Field(min_length=1)


class ProductAction(StrictProtocolModel):
    id: Literal[
        "apply_change",
        "create_branch",
        "open_pull_request",
        "cancel",
        "retry",
        "review_evidence",
    ]
    label: str = Field(min_length=1)
    method: Literal["GET", "POST"]
    href: str = Field(min_length=1)


class ProductEvidenceLink(StrictProtocolModel):
    label: str = Field(min_length=1)
    href: str = Field(min_length=1)
    artifact: str = Field(min_length=1)


class ProductRecoveryAction(StrictProtocolModel):
    label: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    href: str | None = None


class ProductTargetState(StrictProtocolModel):
    modified: bool | None = None
    accounting_status: Literal["known", "unknown"]
    statement: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_modified(self) -> "ProductTargetState":
        if self.accounting_status == "known" and self.modified is None:
            raise ValueError("known target state requires a boolean")
        if self.accounting_status == "unknown" and self.modified is not None:
            raise ValueError("unknown target state requires null")
        return self


class ProductProofPackage(StrictProtocolModel):
    status: Literal["ready_to_apply", "needs_review"]
    risk_tier: Literal["standard", "elevated", "critical"]
    why_villani_trusts_it: str = Field(min_length=1)
    unresolved_decision: str | None = None
    artifact: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_status(self) -> "ProductProofPackage":
        if self.status == "ready_to_apply" and self.unresolved_decision is not None:
            raise ValueError(
                "ready proof packages cannot retain an unresolved decision"
            )
        if self.status == "needs_review" and not self.unresolved_decision:
            raise ValueError(
                "needs-review proof packages require an unresolved decision"
            )
        return self


class ProductRun(StrictProtocolModel):
    schema_version: Literal["villani.product_run.v1"]
    run_identity: ProductRunIdentity
    task_summary: ProductTaskSummary
    current_stage: ProductStage
    stage_sentence: str = Field(min_length=1)
    stage_transitions: list[ProductStageTransition]
    final_verdict: ProductVerdict | None
    verdict_reason: str | None
    change_summary: str = Field(min_length=1)
    changed_files: list[str]
    checks_summary: ProductEvidenceCounts
    requirement_summary: ProductRequirementCounts
    cost: ProductCost
    duration: ProductDuration
    agent_system: ProductAgentSystem
    role_executions: list[ProductRoleExecution] = Field(default_factory=list)
    escalation_summary: ProductEscalationSummary
    available_actions: list[ProductAction]
    evidence_links: list[ProductEvidenceLink]
    recovery_action: ProductRecoveryAction | None
    technical_detail_references: list[str]
    target_repository: ProductTargetState
    proof_package: ProductProofPackage | None = None
    last_event_sequence: int = Field(ge=1)
    updated_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_delivery_actions(self) -> "ProductRun":
        delivery_ids = {"apply_change", "create_branch", "open_pull_request"}
        if self.final_verdict != "Ready to apply" and any(
            action.id in delivery_ids for action in self.available_actions
        ):
            raise ValueError("delivery actions require the Ready to apply verdict")
        if self.final_verdict is not None and self.current_stage != "Ready":
            raise ValueError("a final verdict requires the Ready stage")
        if (
            self.proof_package is not None
            and self.proof_package.status == "ready_to_apply"
            and self.final_verdict != "Ready to apply"
        ):
            raise ValueError(
                "a ready proof package requires the Ready to apply verdict"
            )
        return self


_STATE_STAGE: dict[str, ProductStage] = {
    "CREATED": "Understanding",
    "CLASSIFYING": "Understanding",
    "CLASSIFIED": "Understanding",
    "POLICY_SELECTED": "Understanding",
    "ATTEMPT_RUNNING": "Working",
    "ATTEMPT_COMPLETED": "Working",
    "REJECTED": "Working",
    "ESCALATING": "Working",
    "VERIFYING": "Checking",
    "VERIFIED": "Checking",
    "SELECTING": "Checking",
    "MATERIALIZING": "Checking",
    "AWAITING_APPROVAL": "Ready",
    "COMPLETED": "Ready",
    "EXHAUSTED": "Ready",
    "FAILED": "Ready",
    "CANCELLED": "Ready",
}

_DEFAULT_SENTENCE: dict[ProductStage, str] = {
    "Understanding": "Understanding the task and choosing a safe route.",
    "Working": "Working in an isolated copy of the repository.",
    "Checking": "Checking the change against the task and recorded evidence.",
    "Ready": "The run is ready for your next decision.",
}


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _role_execution_rows(run_directory: Path) -> list[ProductRoleExecution]:
    planned_index = _read(
        run_directory / "agent-systems" / "invocations" / "index.json"
    )
    planned_roles = _mapping(planned_index.get("roles"))
    actual_index = _read(
        run_directory / "agent-systems" / "role-invocations" / "index.json"
    )
    actual_items = actual_index.get("invocations")
    actual = actual_items if isinstance(actual_items, list) else []
    labels = {
        "classification": "Understand task",
        "coding": "Write code",
        "verification": "Verify result",
        "selection": "Choose candidate",
    }
    rows: list[ProductRoleExecution] = []
    for role in ("classification", "coding", "verification", "selection"):
        planned_reference = _mapping(planned_roles.get(role))
        planned_path = str(planned_reference.get("path") or "")
        planned = _read(run_directory / planned_path) if planned_path else {}
        role_actual = [
            _mapping(item)
            for item in actual
            if isinstance(item, Mapping) and item.get("role") == role
        ]
        if not planned and not role_actual:
            continue
        driver = str(
            (role_actual[-1].get("driver") if role_actual else None)
            or planned.get("driver")
            or planned.get("system_kind")
            or "internal"
        )
        display = {
            "codex": "Codex CLI",
            "claude_code": "Claude Code",
            "api": "API",
            "internal_runner": "Villani",
        }.get(driver, driver.replace("_", " ").title())
        failed = any(
            item.get("infrastructure_state") != "succeeded" for item in role_actual
        )
        status = (
            "infrastructure_failure"
            if failed
            else "succeeded"
            if role_actual
            else "not_invoked"
            if driver in {"codex", "claude_code"}
            else "recorded"
        )
        latest = role_actual[-1] if role_actual else {}
        failure_document: dict[str, Any] = {}
        artifact_links = latest.get("artifact_links")
        if isinstance(artifact_links, list):
            failure_reference = next(
                (
                    str(item)
                    for item in artifact_links
                    if str(item).endswith("/infrastructure-failure.json")
                ),
                "",
            )
            if failure_reference:
                failure_document = _read(run_directory / failure_reference)
        rows.append(
            ProductRoleExecution(
                role=role,  # type: ignore[arg-type]
                label=labels[role],
                agent_system_id=str(
                    latest.get("agent_system_id")
                    or planned.get("agent_system_id")
                    or planned_reference.get("agent_system_id")
                    or "unavailable"
                ),
                system_name=display,
                driver=driver,
                model=(
                    str(latest.get("resolved_model") or latest.get("configured_model"))
                    if latest.get("resolved_model") or latest.get("configured_model")
                    else str(planned.get("model"))
                    if planned.get("model")
                    else None
                ),
                invocation_count=len(role_actual),
                status=status,  # type: ignore[arg-type]
                evidence_artifact=(
                    "agent-systems/role-invocations/index.json"
                    if role_actual
                    else planned_path or "agent-systems/role-bindings.json"
                ),
                infrastructure_failure=(
                    ProductRoleInfrastructureFailure.model_validate(
                        {
                            key: value
                            for key, value in failure_document.items()
                            if key != "schema_version"
                        }
                    )
                    if failed and failure_document
                    else None
                ),
            )
        )
    return rows


def _public_text(value: object, fallback: str) -> str:
    text = str(value or fallback)
    replacements = (
        ("acceptance eligible", "proved acceptable"),
        ("acceptance-eligible", "proved acceptable"),
        ("canonical truth", "recorded evidence"),
        ("verifier authority", "verification"),
        ("raw classification", "task assessment"),
        ("effective classification", "task assessment"),
        ("materialization", "applying the change"),
        ("materialized", "applied"),
        ("exhausted", "could not prove"),
    )
    for internal, public in replacements:
        text = text.replace(internal, public).replace(internal.title(), public.title())
    text = re.sub(
        r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    return text


def _stage_sentence(event_type: str, stage: ProductStage) -> str:
    if event_type == "retry_selected":
        return "The first route could not prove the change. Retrying."
    if event_type == "escalation_selected":
        return "Retrying with a stronger qualified route."
    if event_type in {
        "verification_retry_started",
        "repository_validation_retry_started",
        "focused_probe_execution_started",
    }:
        return "Verification needs another check."
    if event_type == "classification_completed":
        return "The task is understood and a safe route is being selected."
    if event_type == "attempt_started":
        return "The agent system is working in an isolated repository copy."
    if event_type in {"verification_started", "repository_validation_started"}:
        return "Verification is checking the change and its evidence."
    if event_type == "approval_requested":
        return "The proved change is ready for your decision."
    if event_type == "run_cancelled":
        return "The run was cancelled safely and its evidence was preserved."
    return _DEFAULT_SENTENCE[stage]


def project_product_stage(
    event: Mapping[str, Any], current_stage: ProductStage | None = None
) -> tuple[ProductStage, str]:
    """Project one canonical event using the same rules as the durable view."""

    payload = _mapping(event.get("payload"))
    projected_state = str(payload.get("to_state") or "")
    stage = _STATE_STAGE.get(projected_state)
    if projected_state == "POLICY_SELECTED" and current_stage in {
        "Working",
        "Checking",
    }:
        stage = "Working"
    if stage is None and event.get("event_type") == "run_created":
        stage = "Understanding"
    selected = stage or current_stage or "Understanding"
    return selected, _stage_sentence(str(event.get("event_type") or ""), selected)


def _stage_projection(
    events: list[dict[str, Any]], state_name: str
) -> tuple[ProductStage, str, list[ProductStageTransition]]:
    transitions: list[ProductStageTransition] = []
    current: ProductStage | None = None
    sentence = _DEFAULT_SENTENCE["Understanding"]
    for event in events:
        payload = _mapping(event.get("payload"))
        projected_state = str(payload.get("to_state") or "")
        has_stage = (
            projected_state in _STATE_STAGE or event.get("event_type") == "run_created"
        )
        if not has_stage:
            if current is not None and str(event.get("event_type") or "") in {
                "retry_selected",
                "escalation_selected",
                "verification_retry_started",
                "repository_validation_retry_started",
                "focused_probe_execution_started",
            }:
                _unchanged_stage, sentence = project_product_stage(event, current)
            continue
        stage, candidate_sentence = project_product_stage(event, current)
        if stage != current:
            transitions.append(
                ProductStageTransition(
                    sequence=max(int(event.get("sequence") or 1), 1),
                    timestamp=str(event.get("timestamp") or "unknown"),
                    stage=stage,
                    sentence=candidate_sentence,
                )
            )
            current = stage
        sentence = candidate_sentence
    final_stage = _STATE_STAGE.get(state_name, current or "Understanding")
    if final_stage != current:
        last = events[-1] if events else {}
        transitions.append(
            ProductStageTransition(
                sequence=max(int(last.get("sequence") or 1), 1),
                timestamp=str(last.get("timestamp") or "unknown"),
                stage=final_stage,
                sentence=_DEFAULT_SENTENCE[final_stage],
            )
        )
        sentence = _DEFAULT_SENTENCE[final_stage]
    if not transitions:
        transitions.append(
            ProductStageTransition(
                sequence=1,
                timestamp="unknown",
                stage=final_stage,
                sentence=_DEFAULT_SENTENCE[final_stage],
            )
        )
    return final_stage, sentence, transitions


def _selected_truth(
    run_directory: Path,
    manifest: Mapping[str, Any],
    summary_decision: bool,
) -> tuple[str | None, bool, dict[str, Any]]:
    selected = manifest.get("selected_attempt_id")
    attempt_id = str(selected) if isinstance(selected, str) and selected else None
    selection = _read(run_directory / "selection.json")
    selected_ids = selection.get("selected_candidate_ids")
    eligible_ids = selection.get("eligible_candidate_ids")
    selected_by_controller = bool(
        attempt_id
        and isinstance(selected_ids, list)
        and selected_ids == [attempt_id]
        and isinstance(eligible_ids, list)
        and attempt_id in eligible_ids
    )
    verification = (
        _read(run_directory / "verification" / f"{attempt_id}.json")
        if attempt_id
        else {}
    )
    requirement_values = verification.get("requirement_results")
    requirement_rows = (
        requirement_values if isinstance(requirement_values, list) else []
    )
    success_values = verification.get("success_evidence")
    success_rows = success_values if isinstance(success_values, list) else []
    missing_values = verification.get("missing_evidence")
    missing_rows = missing_values if isinstance(missing_values, list) else ["unknown"]
    legacy_self_contained_proof = bool(
        requirement_rows
        and success_rows
        and not missing_rows
        and all(
            isinstance(row, Mapping)
            and row.get("outcome") in {"passed", "not_applicable"}
            and (
                row.get("outcome") == "not_applicable" or bool(row.get("evidence_ids"))
            )
            for row in requirement_rows
        )
    )
    proved = bool(
        (summary_decision or legacy_self_contained_proof)
        and selected_by_controller
        and verification.get("acceptance_eligible") is True
        and verification.get("outcome") == "accepted"
        and verification.get("recommended_action") == "accept"
    )
    return attempt_id, proved, verification


def _changed_files(
    run_directory: Path,
    attempt_id: str | None,
    materialization: Mapping[str, Any],
    delivery: Mapping[str, Any],
) -> list[str]:
    values: list[object] = []
    for source in (
        materialization.get("changed_files"),
        _mapping(delivery.get("review")).get("files_changed"),
    ):
        if isinstance(source, list):
            values.extend(source)
    if not values and attempt_id:
        attempt = _read(run_directory / "attempts" / attempt_id / "attempt.json")
        source = _mapping(attempt.get("metadata")).get("changed_files")
        if isinstance(source, list):
            values.extend(source)
    return sorted({str(value) for value in values if isinstance(value, str) and value})


def _combined_checks(
    summary: Any,
) -> tuple[
    int | None,
    int | None,
    int | None,
    int | None,
    Literal["complete", "unknown"],
]:
    groups = (summary.checks, summary.focused_probes)
    if any(group.accounting_status != "complete" for group in groups):
        return None, None, None, None, "unknown"
    return (
        sum(group.passed or 0 for group in groups),
        sum(group.failed or 0 for group in groups),
        sum(group.not_run or 0 for group in groups),
        sum(group.unavailable or 0 for group in groups),
        "complete",
    )


_DELIVERY_FAILURES = {
    "repository_changed_before_materialization",
    "target_drift",
    "delivery_conflict",
    "pull_request_failed",
    "branch_delivery_failed",
    "materialization_failed",
}


def _product_failure_experience(failure_code: str, reason: str) -> dict[str, Any]:
    # Import lazily because schema validation imports this contract while the
    # event writer (which presentation redaction uses) is still initializing.
    from .presentation import failure_experience, infer_failure_code

    inferred = failure_code or infer_failure_code(None, reason)
    public_code = {
        "no_backend": "no_usable_agent",
        "model_not_loaded": "unavailable_model",
        "verifier_unavailable": "verification_infrastructure_failure",
        "no_authoritative_evidence": "no_acceptable_candidate",
        "repository_changed_before_materialization": "target_drift",
        "patch_conflict": "delivery_conflict",
        "service_offline": "service_interruption",
        "user_cancelled": "cancellation",
    }.get(inferred, inferred)
    experience = failure_experience(public_code)
    if public_code == "unknown_failure" and reason:
        experience["what_failed"] = _public_text(
            reason, str(experience.get("what_failed") or "The run stopped safely.")
        )
    return experience


def _verdict(
    state_name: str,
    proved: bool,
    failure_code: str,
    reason: str,
) -> tuple[ProductVerdict | None, str | None]:
    if state_name == "CANCELLED":
        return "Cancelled", _public_text(reason, "The run was cancelled safely.")
    if state_name == "AWAITING_APPROVAL" and proved:
        return "Ready to apply", "Verification proved the selected change acceptable."
    if state_name == "COMPLETED":
        if proved:
            return (
                "Ready to apply",
                "Verification proved the selected change acceptable.",
            )
        return (
            "Needs review",
            "The run completed without sufficient proof for delivery.",
        )
    if state_name == "EXHAUSTED":
        return (
            "Could not prove",
            "Villani could not prove the change with sufficient recorded evidence before the safe stop.",
        )
    if state_name == "FAILED":
        if proved or failure_code in _DELIVERY_FAILURES:
            return "Needs review", _public_text(
                reason, "The proved change could not be delivered safely."
            )
        return "Could not prove", _public_text(
            reason, "Villani could not gather sufficient recorded evidence."
        )
    return None, None


def _target_state(
    state_name: str, materialization: Mapping[str, Any], delivery: Mapping[str, Any]
) -> ProductTargetState:
    if isinstance(delivery.get("target_worktree_modified"), bool):
        modified = bool(delivery.get("target_worktree_modified"))
    elif isinstance(delivery.get("repository_modified"), bool):
        modified = bool(delivery.get("repository_modified"))
    elif materialization.get("status") == "succeeded":
        metadata = _mapping(materialization.get("metadata"))
        if isinstance(metadata.get("target_worktree_modified"), bool):
            modified = bool(metadata.get("target_worktree_modified"))
        else:
            modified = False
    elif state_name in {
        "CREATED",
        "CLASSIFYING",
        "CLASSIFIED",
        "POLICY_SELECTED",
        "ATTEMPT_RUNNING",
        "ATTEMPT_COMPLETED",
        "VERIFYING",
        "VERIFIED",
        "REJECTED",
        "ESCALATING",
        "SELECTING",
        "AWAITING_APPROVAL",
        "EXHAUSTED",
        "CANCELLED",
    }:
        modified = False
    else:
        return ProductTargetState(
            modified=None,
            accounting_status="unknown",
            statement="Whether the target repository was modified could not be established.",
        )
    return ProductTargetState(
        modified=modified,
        accounting_status="known",
        statement=(
            "The target repository was modified."
            if modified
            else "The target repository was not modified."
        ),
    )


def _actions(
    run_id: str,
    verdict: ProductVerdict | None,
    state_name: str,
    delivery: Mapping[str, Any],
) -> list[ProductAction]:
    encoded = run_id
    if verdict is None:
        return [
            ProductAction(
                id="cancel",
                label="Cancel",
                method="POST",
                href=f"/v1/console/runs/{encoded}/cancel",
            )
        ]
    if verdict == "Ready to apply" and state_name == "AWAITING_APPROVAL":
        materialization_type = str(
            delivery.get("materialization_type")
            or delivery.get("requested_materialization_type")
            or "local_patch_apply"
        )
        action_id: Literal["apply_change", "create_branch", "open_pull_request"]
        label: str
        if materialization_type == "local_branch":
            action_id, label = "create_branch", "Create branch"
        elif materialization_type == "pull_request":
            action_id, label = "open_pull_request", "Open pull request"
        else:
            action_id, label = "apply_change", "Apply change"
        return [
            ProductAction(
                id=action_id,
                label=label,
                method="POST",
                href=f"/v1/console/runs/{encoded}/approval",
            ),
            ProductAction(
                id="review_evidence",
                label="Review evidence",
                method="GET",
                href=f"/console/runs/{encoded}/replay",
            ),
        ]
    if verdict in {"Could not prove", "Cancelled", "Needs review"}:
        return [
            ProductAction(
                id="retry",
                label="Start again",
                method="GET",
                href=f"/console?rerun={encoded}",
            ),
            ProductAction(
                id="review_evidence",
                label="Review evidence",
                method="GET",
                href=f"/console/runs/{encoded}/replay",
            ),
        ]
    return [
        ProductAction(
            id="review_evidence",
            label="Review evidence",
            method="GET",
            href=f"/console/runs/{encoded}/replay",
        )
    ]


def build_product_run(run_directory: str | Path) -> ProductRun:
    """Build the shared product view from a current or legacy canonical bundle."""

    run_directory = Path(run_directory).resolve()
    manifest = _read(run_directory / "manifest.json")
    state = _read(run_directory / "state.json")
    task = _read(run_directory / "task.json")
    events = (
        read_jsonl_tolerant(run_directory / "events.jsonl")
        if (run_directory / "events.jsonl").is_file()
        else []
    )
    run_id = str(manifest.get("run_id") or state.get("run_id") or run_directory.name)
    state_name = str(state.get("state") or manifest.get("final_state") or "CREATED")
    summary = canonical_run_summary(run_directory)
    attempt_id, proved, verification = _selected_truth(
        run_directory, manifest, summary.acceptance.decision
    )
    delivery = _mapping(_mapping(manifest.get("metadata")).get("delivery"))
    if not delivery:
        delivery = _read(run_directory / "delivery.json")
    materialization = _read(run_directory / "materialization.json")
    failure = _mapping(state.get("failure"))
    failure_code = str(failure.get("code") or "")
    reason = str(
        failure.get("message")
        or _mapping(state.get("metadata")).get("terminal_reason")
        or summary.acceptance.reason
        or ""
    )
    failure_detail = _product_failure_experience(failure_code, reason)
    public_reason = (
        str(failure_detail.get("what_failed") or reason)
        if state_name in {"FAILED", "CANCELLED"}
        else reason
    )
    verdict, verdict_reason = _verdict(state_name, proved, failure_code, public_reason)
    stage, sentence, stage_transitions = _stage_projection(events, state_name)
    if verdict is not None:
        sentence = verdict_reason or _DEFAULT_SENTENCE["Ready"]
    changed_files = _changed_files(run_directory, attempt_id, materialization, delivery)
    checks_passed, checks_failed, checks_not_run, checks_unavailable, checks_status = (
        _combined_checks(summary)
    )
    attempt_ids = manifest.get("attempt_ids")
    attempts = len(attempt_ids) if isinstance(attempt_ids, list) else 0
    retries = sum(event.get("event_type") == "retry_selected" for event in events)
    escalations = sum(
        event.get("event_type") == "escalation_selected" for event in events
    )
    agent_attempt = (
        _read(run_directory / "attempts" / attempt_id / "attempt.json")
        if attempt_id
        else {}
    )
    if not agent_attempt and isinstance(attempt_ids, list) and attempt_ids:
        agent_attempt = _read(
            run_directory / "attempts" / str(attempt_ids[-1]) / "attempt.json"
        )
    cost_known = bool(
        summary.accounting.known and summary.accounting.total_cost is not None
    )
    duration_value = manifest.get("run_wall_clock_duration_ms")
    duration_status = str(
        manifest.get("run_wall_clock_duration_accounting_status") or "unknown"
    )
    if duration_status not in {"complete", "partial", "unknown", "not_applicable"}:
        duration_status = "unknown"
    if not isinstance(duration_value, int) or duration_value < 0:
        duration_value = None
        if duration_status == "complete":
            duration_status = "unknown"
    if verdict is None and events:
        try:
            started_at = datetime.fromisoformat(
                str(events[0].get("timestamp") or "").replace("Z", "+00:00")
            )
            duration_value = max(
                int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
                0,
            )
            duration_status = "partial"
        except (TypeError, ValueError):
            pass
    technical = [
        name
        for name in (
            "manifest.json",
            "state.json",
            "events.jsonl",
            "selection.json",
            f"verification/{attempt_id}.json" if attempt_id else None,
            "run-summary.json"
            if (run_directory / "run-summary.json").is_file()
            else None,
            (
                "agent-systems/role-invocations/index.json"
                if (
                    run_directory / "agent-systems" / "role-invocations" / "index.json"
                ).is_file()
                else None
            ),
        )
        if name and (run_directory / name).is_file()
    ]
    proof_package: ProductProofPackage | None = None
    proof_artifact = (
        f"verification/{attempt_id}-review-package.json" if attempt_id else None
    )
    if proof_artifact and (run_directory / proof_artifact).is_file():
        try:
            compact = CompactReviewPackage.model_validate(
                _read(run_directory / proof_artifact)
            )
        except (TypeError, ValueError):
            compact = None
        if compact is not None and (
            (compact.status == "ready_to_apply" and verdict == "Ready to apply")
            or compact.status == "needs_review"
        ):
            proof_package = ProductProofPackage(
                status=compact.status,
                risk_tier=compact.risk_tier,
                why_villani_trusts_it=_public_text(
                    compact.why_villani_trusts_it,
                    "Villani preserved the verification evidence.",
                ),
                unresolved_decision=(
                    _public_text(compact.unresolved_decision, "Review is required.")
                    if compact.unresolved_decision
                    else None
                ),
                artifact=proof_artifact,
            )
            technical.append(proof_artifact)
    actions = _actions(run_id, verdict, state_name, delivery)
    recovery = None
    if verdict in {"Could not prove", "Cancelled", "Needs review"}:
        exact_recovery = (
            str(failure_detail.get("next_action") or "")
            if state_name in {"FAILED", "EXHAUSTED", "CANCELLED"}
            else ""
        )
        recovery = ProductRecoveryAction(
            label=actions[0].label,
            instruction=(
                exact_recovery
                or (
                    "Review the recorded evidence, resolve the stated issue, then start again."
                    if verdict != "Cancelled"
                    else "Start again when you are ready; the cancelled run remains inspectable."
                )
            ),
            href=actions[0].href,
        )
    raw_cost_status = summary.accounting.accounting_status
    cost_status = cast(
        AccountingStatus,
        raw_cost_status
        if raw_cost_status in {"complete", "partial", "unknown", "not_applicable"}
        else "unknown",
    )
    raw_last_sequence = state.get("last_sequence") or (
        events[-1].get("sequence") if events else 1
    )
    try:
        last_event_sequence = max(int(str(raw_last_sequence)), 1)
    except ValueError:
        last_event_sequence = 1
    return ProductRun(
        schema_version="villani.product_run.v1",
        run_identity=ProductRunIdentity(
            run_id=run_id,
            trace_id=(
                str(manifest.get("trace_id")) if manifest.get("trace_id") else None
            ),
        ),
        task_summary=ProductTaskSummary(
            task=str(task.get("instruction") or "Task instruction was not recorded."),
            success_criteria=(
                str(task.get("success_criteria"))
                if task.get("success_criteria")
                else None
            ),
            repository=(
                str(task.get("repository_path"))
                if task.get("repository_path")
                else None
            ),
        ),
        current_stage=stage,
        stage_sentence=_public_text(sentence, _DEFAULT_SENTENCE[stage]),
        stage_transitions=stage_transitions,
        final_verdict=verdict,
        verdict_reason=verdict_reason,
        change_summary=(
            f"{len(changed_files)} file{'s' if len(changed_files) != 1 else ''} changed in the selected candidate."
            if changed_files
            else "No file changes were recorded."
        ),
        changed_files=changed_files,
        checks_summary=ProductEvidenceCounts(
            passed=checks_passed,
            failed=checks_failed,
            not_run=checks_not_run,
            unavailable=checks_unavailable,
            accounting_status=checks_status,
        ),
        requirement_summary=ProductRequirementCounts(
            proved=summary.requirements.proved,
            not_proved=summary.requirements.not_proved,
            accounting_status=summary.requirements.accounting_status,
        ),
        cost=ProductCost(
            value=summary.accounting.total_cost if cost_known else None,
            currency=summary.accounting.currency if cost_known else None,
            accounting_status=cost_status,
        ),
        duration=ProductDuration(
            value_ms=duration_value,
            accounting_status=cast(AccountingStatus, duration_status),
        ),
        agent_system=ProductAgentSystem(
            name="Villani agent system",
            backend=(
                str(agent_attempt.get("backend_name"))
                if agent_attempt.get("backend_name")
                else None
            ),
            model=(
                str(agent_attempt.get("model")) if agent_attempt.get("model") else None
            ),
        ),
        role_executions=_role_execution_rows(run_directory),
        escalation_summary=ProductEscalationSummary(
            attempts=attempts,
            retries=retries,
            escalations=escalations,
            summary=(
                "No retry or escalation was needed."
                if not retries and not escalations
                else f"Villani made {retries} retr{'y' if retries == 1 else 'ies'} and {escalations} escalation{'s' if escalations != 1 else ''}."
            ),
        ),
        available_actions=actions,
        evidence_links=[
            ProductEvidenceLink(
                label="Recorded evidence",
                href=f"/console/runs/{run_id}/replay",
                artifact="events.jsonl",
            ),
            *(
                [
                    ProductEvidenceLink(
                        label="Compact proof package",
                        href=f"/console/runs/{run_id}/replay",
                        artifact=proof_artifact,
                    )
                ]
                if proof_package is not None and proof_artifact is not None
                else []
            ),
        ],
        recovery_action=recovery,
        technical_detail_references=technical,
        target_repository=_target_state(state_name, materialization, delivery),
        proof_package=proof_package,
        last_event_sequence=last_event_sequence,
        updated_at=str(
            state.get("updated_at")
            or manifest.get("updated_at")
            or (events[-1].get("timestamp") if events else "unknown")
        ),
    )


def persist_product_run(run_directory: str | Path) -> ProductRun:
    product = build_product_run(run_directory)
    write_json_atomic(Path(run_directory) / "product-run.json", product)
    return product


__all__ = [
    "ProductRun",
    "ProductStage",
    "ProductVerdict",
    "build_product_run",
    "persist_product_run",
    "project_product_stage",
]
