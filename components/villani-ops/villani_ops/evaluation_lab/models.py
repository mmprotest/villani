"""Versioned, fail-closed contracts for paired founder evaluations.

These models intentionally keep evaluator-only facts separate from the payload
shown to a coding runner.  In particular, expected solutions, future diffs,
hidden checks, arm identity, route identity, and cost never enter
``runner_payload`` or final verification inputs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value


def _serialize_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    AfterValidator(_require_utc),
    PlainSerializer(_serialize_utc, return_type=str, when_used="json"),
]


class StrictProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Digest = str
AccountingStatus = Literal["complete", "partial", "unknown", "not_applicable"]
Arm = Literal["direct", "villani"]


class EvaluationTaskReference(StrictProtocolModel):
    task_id: str = Field(min_length=1)
    task_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")


class LocalComputeConfiguration(StrictProtocolModel):
    measured_power_watts: float | None = Field(default=None, gt=0)
    electricity_price_per_kwh: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @model_validator(mode="after")
    def complete_measurement(self) -> "LocalComputeConfiguration":
        values = (
            self.measured_power_watts,
            self.electricity_price_per_kwh,
            self.currency,
        )
        if any(value is not None for value in values) and any(
            value is None for value in values
        ):
            raise ValueError(
                "local compute accounting requires measured power, electricity price, and currency"
            )
        return self


class EvaluationSuite(StrictProtocolModel):
    schema_version: Literal["villani.evaluation_suite.v1"] = (
        "villani.evaluation_suite.v1"
    )
    suite_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    suite_version: int = Field(ge=1)
    status: Literal["draft", "frozen"]
    created_at: UtcDateTime
    frozen_at: UtcDateTime | None = None
    randomization_seed: str = Field(min_length=16)
    task_versions: list[EvaluationTaskReference] = Field(default_factory=list)
    local_compute: LocalComputeConfiguration = Field(
        default_factory=LocalComputeConfiguration
    )
    evidence_kind: Literal["real_founder_work", "synthetic_fixture"]
    confidentiality: Literal["public", "internal", "confidential"]
    disclosure_complete: bool = False
    content_digest: Digest | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def frozen_contract(self) -> "EvaluationSuite":
        if self.status == "frozen" and (
            self.frozen_at is None or self.content_digest is None
        ):
            raise ValueError("a frozen suite requires frozen_at and content_digest")
        if self.status == "draft" and self.frozen_at is not None:
            raise ValueError("a draft suite cannot have frozen_at")
        if len({item.task_id for item in self.task_versions}) != len(
            self.task_versions
        ):
            raise ValueError("suite task identities must be unique")
        return self


class SourceSnapshot(StrictProtocolModel):
    repository_identity: str = Field(min_length=1)
    source_kind: Literal["git_commit"] = "git_commit"
    resolved_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    baseline_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    archive_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    archive_path: str = Field(min_length=1)
    included_paths: list[str]
    excluded_paths: list[str]
    file_count: int = Field(ge=1)
    restore_verified: bool

    @model_validator(mode="after")
    def coherent_manifest(self) -> "SourceSnapshot":
        if self.file_count != len(self.included_paths):
            raise ValueError("snapshot file count must match its included paths")
        if len(set(self.included_paths)) != len(self.included_paths):
            raise ValueError("snapshot included paths must be unique")
        for value in (*self.included_paths, *self.excluded_paths, self.archive_path):
            normalized = value.replace("\\", "/").strip("/")
            path = PurePosixPath(normalized)
            if not normalized or path.is_absolute() or ".." in path.parts:
                raise ValueError("snapshot paths must be safe relative paths")
        return self


class ValidationCommand(StrictProtocolModel):
    validation_id: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=900, ge=1)
    authoritative: bool = True
    visibility: Literal["runner_visible", "evaluator_only"] = "runner_visible"

    @model_validator(mode="after")
    def shell_free(self) -> "ValidationCommand":
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("validation argv must contain non-empty strings")
        return self


class SetupCommand(StrictProtocolModel):
    setup_id: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=900, ge=1)

    @model_validator(mode="after")
    def shell_free(self) -> "SetupCommand":
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("setup argv must contain non-empty strings")
        return self


class FileChangeRequirement(StrictProtocolModel):
    behavior: Literal["required", "optional", "forbidden"] = "required"
    allowed_path_prefixes: list[str] = Field(default_factory=list)
    forbidden_path_prefixes: list[str] = Field(default_factory=list)


class TaskProvenance(StrictProtocolModel):
    captured_at: UtcDateTime
    captured_by: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    later_context_present: bool = False


class EvaluatorOnlyMaterial(StrictProtocolModel):
    hidden_check_references: list[str] = Field(default_factory=list)
    future_context_references: list[str] = Field(default_factory=list)
    runner_expected_patch_present: Literal[False] = False


class EvaluationTask(StrictProtocolModel):
    schema_version: Literal["villani.evaluation_task.v1"] = (
        "villani.evaluation_task.v1"
    )
    task_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    task_version: int = Field(ge=1)
    immutable_baseline_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    source_snapshot: SourceSnapshot
    verbatim_task: str = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)
    authoritative_validation: list[ValidationCommand] = Field(min_length=1)
    allowed_setup: list[SetupCommand] = Field(default_factory=list)
    file_change_requirement: FileChangeRequirement
    provenance: TaskProvenance
    risk_labels: list[str] = Field(default_factory=list)
    category_labels: list[str] = Field(default_factory=list)
    secret_exclusions: list[str] = Field(default_factory=list)
    evaluator_only: EvaluatorOnlyMaterial = Field(
        default_factory=EvaluatorOnlyMaterial
    )
    confidentiality: Literal["public", "internal", "confidential"]
    evidence_kind: Literal["real_founder_work", "synthetic_fixture"]
    evidence_eligible: bool
    frozen: bool = False
    content_digest: Digest | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def immutable_snapshot_matches(self) -> "EvaluationTask":
        if self.immutable_baseline_digest != self.source_snapshot.baseline_digest:
            raise ValueError("task baseline digest must match its source snapshot")
        if not self.source_snapshot.restore_verified:
            raise ValueError("task source snapshot must have a proved restore")
        if not any(item.authoritative for item in self.authoritative_validation):
            raise ValueError("task requires at least one authoritative validation")
        if self.evidence_kind == "synthetic_fixture" and self.evidence_eligible:
            raise ValueError("synthetic fixtures cannot be founder evidence")
        if self.frozen and self.content_digest is None:
            raise ValueError("a frozen task requires a content digest")
        return self

    def runner_payload(self) -> dict[str, Any]:
        """Return the only task data a coding runner may receive."""

        return {
            "schema_version": "villani.evaluation_runner_task.v1",
            "baseline_digest": self.immutable_baseline_digest,
            "task": self.verbatim_task,
            "success_criteria": list(self.success_criteria),
            "allowed_setup": [item.model_dump(mode="json") for item in self.allowed_setup],
            "file_change_requirement": self.file_change_requirement.model_dump(
                mode="json"
            ),
            "validation": [
                item.model_dump(mode="json")
                for item in self.authoritative_validation
                if item.visibility == "runner_visible"
            ],
        }


class AccountingAmount(StrictProtocolModel):
    value: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    accounting_status: AccountingStatus
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def truthful_amount(self) -> "AccountingAmount":
        if self.accounting_status == "complete" and self.value is None:
            raise ValueError("complete accounting requires a value")
        if self.accounting_status in {"unknown", "not_applicable"} and self.value is not None:
            raise ValueError("unknown or inapplicable accounting requires null")
        if self.value is None and self.currency is not None:
            raise ValueError("unknown amount cannot claim a currency")
        if self.value is not None and self.currency is None:
            raise ValueError("known amount requires a currency")
        return self


class DurationAmount(StrictProtocolModel):
    value_ms: int | None = Field(default=None, ge=0)
    accounting_status: AccountingStatus
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def truthful_duration(self) -> "DurationAmount":
        if self.accounting_status == "complete" and self.value_ms is None:
            raise ValueError("complete duration requires a value")
        if self.accounting_status in {"unknown", "not_applicable"} and self.value_ms is not None:
            raise ValueError("unknown or inapplicable duration requires null")
        return self


class AgentSystemIdentity(StrictProtocolModel):
    product: str = Field(min_length=1)
    product_version: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    model: str | None = None
    provider: str | None = None
    serving_engine: str | None = None
    serving_engine_version: str | None = None
    execution_provider: str = Field(min_length=1)
    environment_fingerprint: str = Field(min_length=1)


class EvaluationTrial(StrictProtocolModel):
    schema_version: Literal["villani.evaluation_trial.v1"] = (
        "villani.evaluation_trial.v1"
    )
    trial_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    suite_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str = Field(min_length=1)
    task_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    arm: Arm
    repetition: int = Field(ge=1)
    randomized_order: int = Field(ge=1)
    order_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["planned", "running", "completed", "excluded", "interrupted"]
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    agent_system: AgentSystemIdentity
    run_id: str | None = None
    baseline_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_restore_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    execution_cost: AccountingAmount
    verification_cost: AccountingAmount
    local_compute_cost: AccountingAmount
    total_cost: AccountingAmount
    duration: DurationAmount
    proved_acceptable: bool | None = None
    verification_status: Literal["complete", "infrastructure_failure", "not_run"]
    human_outcome: Literal[
        "accepted_as_is", "accepted_after_correction", "rejected"
    ] | None = None
    correction_required: bool | None = None
    review_minutes: float | None = Field(default=None, ge=0)
    false_acceptance: bool | None = None
    false_rejection: bool | None = None
    exclusion_reason: str | None = None
    target_repository_modified: Literal[False] = False
    attempts: int = Field(ge=0)
    escalations: int = Field(ge=0)
    verifier_disagreement: bool | None = None
    configuration_mode: Literal["automatic", "manual"]
    artifact_references: list[str]
    evidence_eligible: bool

    @model_validator(mode="after")
    def terminal_truth(self) -> "EvaluationTrial":
        if self.baseline_digest != self.baseline_restore_digest:
            raise ValueError("trial baseline restore differs from the frozen baseline")
        if self.status == "completed" and (
            self.completed_at is None or self.verification_status == "not_run"
        ):
            raise ValueError("completed trial requires completion and verification")
        if self.verification_status == "complete" and self.proved_acceptable is None:
            raise ValueError("complete verification requires a binary result")
        if self.verification_status != "complete" and self.proved_acceptable is not None:
            raise ValueError("failed or missing verification cannot claim a result")
        if self.status == "excluded" and not self.exclusion_reason:
            raise ValueError("excluded trial requires a reason")
        return self


class HumanReview(StrictProtocolModel):
    schema_version: Literal["villani.human_review.v1"] = "villani.human_review.v1"
    review_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    created_at: UtcDateTime
    reviewer_id: str = Field(min_length=1)
    blinded: bool
    arm_revealed_during_review: bool
    outcome: Literal["accepted_as_is", "accepted_after_correction", "rejected"]
    correction_required: bool
    review_minutes: float = Field(ge=0)
    correction_summary: str | None = None
    severity: Literal["none", "low", "medium", "high", "critical"]
    false_acceptance: bool
    false_rejection: bool
    later_rollback: bool | None = None
    reopened_defect: bool | None = None
    amends_review_id: str | None = None
    artifact_references: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def correction_semantics(self) -> "HumanReview":
        if self.outcome == "accepted_after_correction" and (
            not self.correction_required or not self.correction_summary
        ):
            raise ValueError("accepted-after-correction requires a correction summary")
        if self.outcome == "accepted_as_is" and self.correction_required:
            raise ValueError("accepted-as-is cannot require correction")
        if self.blinded and self.arm_revealed_during_review:
            raise ValueError("a blinded review cannot reveal the arm")
        if self.false_acceptance and self.false_rejection:
            raise ValueError("a review cannot be both a false acceptance and false rejection")
        if self.false_acceptance and self.outcome == "accepted_as_is":
            raise ValueError("an accepted-as-is outcome cannot be a false acceptance")
        if self.false_rejection and self.outcome != "accepted_as_is":
            raise ValueError("a false rejection requires an accepted-as-is human outcome")
        return self


class ConfidenceInterval(StrictProtocolModel):
    method: str = Field(min_length=1)
    estimate: float | None = None
    lower: float | None = None
    upper: float | None = None
    confidence: float = Field(default=0.95, gt=0.5, lt=1)
    sample_count: int = Field(ge=0)
    status: Literal["available", "insufficient_evidence", "not_defined"]


class MetricValue(StrictProtocolModel):
    value: float | None = None
    numerator: int | float | None = None
    denominator: int | float | None = None
    unit: str | None = None
    accounting_status: Literal["complete", "partial", "unknown", "not_defined"]
    interval: ConfidenceInterval | None = None

    @model_validator(mode="after")
    def truthful_metric(self) -> "MetricValue":
        if self.accounting_status == "complete" and self.value is None:
            raise ValueError("a complete metric requires a value")
        if self.accounting_status in {"unknown", "not_defined"} and self.value is not None:
            raise ValueError("an unknown or undefined metric cannot claim a value")
        return self


class GateCheck(StrictProtocolModel):
    check_id: str = Field(min_length=1)
    status: Literal["pass", "fail", "insufficient_evidence"]
    actual: Any
    required: Any
    reason: str = Field(min_length=1)


class EvaluationReport(StrictProtocolModel):
    schema_version: Literal["villani.evaluation_report.v1"] = (
        "villani.evaluation_report.v1"
    )
    report_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    suite_digest: Digest = Field(pattern=r"^[a-f0-9]{64}$")
    generated_at: UtcDateTime
    evidence_kind: Literal["real_founder_work", "synthetic_fixture"]
    confidentiality: Literal["public", "internal", "confidential"]
    raw_counts: dict[str, int]
    reliability: dict[str, MetricValue]
    review_time: dict[str, MetricValue]
    cost: dict[str, MetricValue]
    supervision: dict[str, MetricValue]
    false_acceptance: dict[str, MetricValue]
    paired_task_deltas: list[dict[str, Any]]
    task_classes: list[dict[str, Any]]
    failure_modes: list[dict[str, Any]]
    missing_evidence: list[dict[str, Any]]
    confusion_matrix: dict[str, int | None]
    classification_metrics: dict[str, float | None]
    calibration: dict[str, Any]
    verifier_wrong_cases: list[dict[str, Any]]
    cost_decomposition: list[dict[str, Any]]
    route_decomposition: list[dict[str, Any]]
    trial_bundle_links: list[str]
    unknowns: list[dict[str, Any]]
    exclusions: list[dict[str, Any]]
    disclosures_complete: bool
    small_sample_significance_claimed: Literal[False] = False
    founder_gate_status: Literal["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"]
    founder_gate_checks: list[GateCheck]
