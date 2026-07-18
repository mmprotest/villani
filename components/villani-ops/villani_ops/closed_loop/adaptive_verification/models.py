"""Durable contracts for PT9 adaptive verification and supervision evidence.

These contracts deliberately separate planning, binary authority, review
presentation, human feedback, aggregate supervision metrics, and the Gate D
decision.  Unknown cost and time remain null and an unclear verifier result is
never representable as an accepting decision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)


ADAPTIVE_VERIFICATION_POLICY_VERSION: Literal["adaptive_verification_v1"] = (
    "adaptive_verification_v1"
)
ADAPTIVE_VERIFICATION_PLAN_SCHEMA_VERSION: Literal[
    "villani.adaptive_verification_plan.v1"
] = "villani.adaptive_verification_plan.v1"
BINARY_VERIFICATION_DECISION_SCHEMA_VERSION: Literal[
    "villani.binary_verification_decision.v1"
] = "villani.binary_verification_decision.v1"
REVIEW_PACKAGE_SCHEMA_VERSION: Literal["villani.review_package.v1"] = (
    "villani.review_package.v1"
)
HUMAN_OUTCOME_SCHEMA_VERSION: Literal["villani.human_outcome.v1"] = (
    "villani.human_outcome.v1"
)
SUPERVISION_METRICS_SCHEMA_VERSION: Literal["villani.supervision_metrics.v1"] = (
    "villani.supervision_metrics.v1"
)
GATE_D_SCHEMA_VERSION: Literal["villani.gate_d.v1"] = "villani.gate_d.v1"

AccountingStatus: TypeAlias = Literal[
    "complete", "partial", "unknown", "not_applicable"
]
RiskTier: TypeAlias = Literal["standard", "elevated", "critical"]
InfrastructureStatus: TypeAlias = Literal[
    "resolved", "infrastructure_failure", "unavailable"
]
NodeKind: TypeAlias = Literal[
    "repository_validation",
    "focused_probe",
    "changed_test_execution",
    "static_checks",
    "diff_integrity",
    "generated_artifact_exclusion",
    "requirement_mapping",
    "semantic_verifier",
    "independent_second_verifier",
    "manual_review",
]


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value


def _serialize_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    AfterValidator(_utc),
    PlainSerializer(_serialize_utc, return_type=str, when_used="json"),
]


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class StrictAdaptiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdaptiveVerificationPolicy(StrictAdaptiveModel):
    """Generic deterministic PT9 policy; repositories supply their commands."""

    policy_version: Literal["adaptive_verification_v1"] = (
        ADAPTIVE_VERIFICATION_POLICY_VERSION
    )
    standard_patch_line_limit: int = Field(default=200, ge=1)
    elevated_patch_line_limit: int = Field(default=600, ge=1)
    standard_changed_file_limit: int = Field(default=6, ge=1)
    elevated_changed_file_limit: int = Field(default=18, ge=1)
    configured_sensitive_paths: list[str] = Field(default_factory=list)
    configured_generated_artifact_paths: list[str] = Field(default_factory=list)
    require_semantic_verification: Literal[True] = True
    require_independent_verifier_for_critical: bool = True
    require_manual_review_when_proof_impossible: bool = True
    minimum_independent_verifier_capability: float = Field(default=80, ge=0, le=100)
    historical_disagreement_window: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def validate_thresholds(self) -> "AdaptiveVerificationPolicy":
        if self.standard_patch_line_limit >= self.elevated_patch_line_limit:
            raise ValueError("standard patch limit must be below elevated limit")
        if self.standard_changed_file_limit >= self.elevated_changed_file_limit:
            raise ValueError("standard file limit must be below elevated limit")
        for name in (
            "configured_sensitive_paths",
            "configured_generated_artifact_paths",
        ):
            values = getattr(self, name)
            if values != sorted(set(values)):
                raise ValueError(f"{name} must be sorted and unique")
            if any(not item.strip() for item in values):
                raise ValueError(f"{name} cannot contain empty patterns")
        return self


class VerificationPlanNode(StrictAdaptiveModel):
    node_id: str = Field(pattern=r"^node_[a-z0-9_]+$")
    kind: NodeKind
    disposition: Literal["required", "conditional", "omitted"]
    reason: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    repository_commands: list[list[str]] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)
    estimated_model_calls: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_node(self) -> "VerificationPlanNode":
        if self.depends_on != list(dict.fromkeys(self.depends_on)):
            raise ValueError("node dependencies must be unique")
        if any(
            not command or any(not argument for argument in command)
            for command in self.repository_commands
        ):
            raise ValueError("repository commands require non-empty argv")
        if self.disposition == "omitted" and self.repository_commands:
            raise ValueError("omitted nodes cannot contain commands")
        return self


class AdaptiveVerificationPlan(StrictAdaptiveModel):
    schema_version: Literal["villani.adaptive_verification_plan.v1"] = (
        ADAPTIVE_VERIFICATION_PLAN_SCHEMA_VERSION
    )
    plan_id: str = Field(pattern=r"^avp_[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    policy_version: Literal["adaptive_verification_v1"] = (
        ADAPTIVE_VERIFICATION_POLICY_VERSION
    )
    policy_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: UtcDateTime
    risk_tier: RiskTier
    risk_reasons: list[str]
    task_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    criteria_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_diff_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    changed_files: list[str]
    requirement_ids: list[str]
    qualification_state: Literal[
        "qualified", "provisional", "experimental", "unsupported", "unknown"
    ]
    historical_failure_modes: list[str]
    nodes: list[VerificationPlanNode] = Field(min_length=1)
    independent_verifier_required: bool
    manual_review_if_unresolved: bool
    semantic_context_allowlist: list[str]
    semantic_context_excluded: list[str]
    deterministic_input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> "AdaptiveVerificationPlan":
        for name in (
            "risk_reasons",
            "changed_files",
            "requirement_ids",
            "historical_failure_modes",
            "semantic_context_allowlist",
            "semantic_context_excluded",
        ):
            values = getattr(self, name)
            if values != sorted(set(values)):
                raise ValueError(f"{name} must be sorted and unique")
        node_ids = [item.node_id for item in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("verification plan node ids must be unique")
        known = set(node_ids)
        if any(set(item.depends_on) - known for item in self.nodes):
            raise ValueError(
                "verification plan dependencies must reference known nodes"
            )
        required_kinds = {
            item.kind for item in self.nodes if item.disposition == "required"
        }
        if "semantic_verifier" not in required_kinds:
            raise ValueError("semantic verification must always be required")
        if (
            self.independent_verifier_required
            and "independent_second_verifier" not in required_kinds
        ):
            raise ValueError("critical plans require an independent verifier node")
        forbidden = {"harness_identity", "route", "cost", "competing_candidates"}
        if forbidden.intersection(self.semantic_context_allowlist):
            raise ValueError(
                "semantic context exposes forbidden controller information"
            )
        if not forbidden.issubset(self.semantic_context_excluded):
            raise ValueError("semantic context exclusions must name all blind fields")
        return self


class VerificationNodeResult(StrictAdaptiveModel):
    node_id: str = Field(pattern=r"^node_[a-z0-9_]+$")
    status: Literal[
        "passed",
        "failed",
        "unavailable",
        "infrastructure_error",
        "not_run",
        "not_applicable",
    ]
    reason: str = Field(min_length=1)
    commands: list[list[str]] = Field(default_factory=list)
    evidence_paths: list[str] = Field(default_factory=list)


class RestrictedVerifierProvenance(StrictAdaptiveModel):
    """Identity retained for audit without entering semantic-verifier context."""

    verifier_role: Literal["semantic", "independent_semantic"]
    verifier_identity_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    invocation_status: Literal[
        "completed", "not_invoked", "malformed_output", "timeout", "error"
    ]
    independent: bool
    artifact_path: str | None = None


class MoneyAccounting(StrictAdaptiveModel):
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    accounting_status: AccountingStatus
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_accounting(self) -> "MoneyAccounting":
        if self.accounting_status in {"complete", "partial"}:
            if self.amount is None or self.currency is None:
                raise ValueError("known money requires amount and currency")
        elif self.amount is not None or self.currency is not None:
            raise ValueError("unknown money must remain null")
        return self


class DurationAccounting(StrictAdaptiveModel):
    duration_ms: int | None = Field(default=None, ge=0)
    accounting_status: AccountingStatus
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_accounting(self) -> "DurationAccounting":
        if self.accounting_status in {"complete", "partial"}:
            if self.duration_ms is None:
                raise ValueError("known duration requires duration_ms")
        elif self.duration_ms is not None:
            raise ValueError("unknown duration must remain null")
        return self


class BinaryVerificationDecision(StrictAdaptiveModel):
    schema_version: Literal["villani.binary_verification_decision.v1"] = (
        BINARY_VERIFICATION_DECISION_SCHEMA_VERSION
    )
    decision_id: str = Field(pattern=r"^avd_[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    plan_id: str = Field(pattern=r"^avp_[0-9a-f]{64}$")
    decided_at: UtcDateTime
    decision: Literal[0, 1]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    reason: str = Field(min_length=1)
    requirements_proved: list[str]
    requirements_not_proved: list[str]
    blockers: list[str]
    infrastructure_status: InfrastructureStatus
    semantic_status: Literal["passed", "failed", "unclear", "error", "not_invoked"]
    independent_verifier_required: bool
    independent_verifier_completed: bool
    node_results: list[VerificationNodeResult]
    verifier_provenance: list[RestrictedVerifierProvenance]
    verification_cost: MoneyAccounting
    normalized_from: Literal[
        "accepted", "rejected", "unclear", "error", "deterministic_failure"
    ]

    @model_validator(mode="after")
    def validate_binary_authority(self) -> "BinaryVerificationDecision":
        for name in ("requirements_proved", "requirements_not_proved", "blockers"):
            values = getattr(self, name)
            if values != sorted(set(values)):
                raise ValueError(f"{name} must be sorted and unique")
        if set(self.requirements_proved).intersection(self.requirements_not_proved):
            raise ValueError("a requirement cannot be both proved and not proved")
        if self.decision == 1:
            if self.semantic_status != "passed":
                raise ValueError("acceptance requires successful semantic verification")
            if self.infrastructure_status != "resolved":
                raise ValueError("acceptance requires resolved infrastructure")
            if self.requirements_not_proved or self.blockers:
                raise ValueError(
                    "acceptance cannot retain unproved requirements or blockers"
                )
            if (
                self.independent_verifier_required
                and not self.independent_verifier_completed
            ):
                raise ValueError("required independent verification did not complete")
            blocking_statuses = {
                "failed",
                "unavailable",
                "infrastructure_error",
                "not_run",
            }
            if any(item.status in blocking_statuses for item in self.node_results):
                raise ValueError("acceptance cannot contain an unresolved plan node")
        if (
            self.semantic_status in {"unclear", "error", "not_invoked"}
            and self.decision != 0
        ):
            raise ValueError(
                "unclear, error, and missing semantic results normalize to zero"
            )
        return self


class ReviewCheck(StrictAdaptiveModel):
    label: str = Field(min_length=1)
    status: Literal[
        "passed", "failed", "not_run", "unavailable", "infrastructure_error"
    ]
    evidence_path: str | None = None


class CompactReviewPackage(StrictAdaptiveModel):
    schema_version: Literal["villani.review_package.v1"] = REVIEW_PACKAGE_SCHEMA_VERSION
    package_id: str = Field(pattern=r"^rvp_[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    decision_id: str = Field(pattern=r"^avd_[0-9a-f]{64}$")
    created_at: UtcDateTime
    status: Literal["ready_to_apply", "needs_review"]
    task: str = Field(min_length=1)
    change_summary: str = Field(min_length=1)
    changed_files: list[str]
    requirements_proved: list[str]
    requirements_not_proved: list[str]
    checks: list[ReviewCheck]
    risk_tier: RiskTier
    risk_flags: list[str]
    known_cost: MoneyAccounting
    known_duration: DurationAccounting
    why_villani_trusts_it: str = Field(min_length=1)
    unresolved_decision: str | None = None
    full_evidence_href: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_review_state(self) -> "CompactReviewPackage":
        if self.status == "ready_to_apply":
            if self.requirements_not_proved or self.unresolved_decision is not None:
                raise ValueError("ready packages cannot contain unresolved proof")
            if any(item.status != "passed" for item in self.checks):
                raise ValueError("ready packages require every listed check to pass")
        elif not self.unresolved_decision:
            raise ValueError(
                "needs-review packages require the exact unresolved decision"
            )
        return self


class HumanOutcome(StrictAdaptiveModel):
    schema_version: Literal["villani.human_outcome.v1"] = HUMAN_OUTCOME_SCHEMA_VERSION
    outcome_id: str = Field(pattern=r"^hout_[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    attempt_id: str | None = None
    recorded_at: UtcDateTime
    outcome: Literal[
        "accepted_as_is",
        "corrected_before_use",
        "reverted",
        "reopened_defect",
        "false_acceptance",
        "false_rejection",
    ]
    review_minutes: float | None = Field(default=None, ge=0)
    review_time_accounting_status: Literal["complete", "unknown", "not_applicable"]
    full_trace_opened: bool | None = None
    full_trace_accounting_status: Literal["complete", "unknown", "not_applicable"]
    correction_summary: str | None = None
    linked_reference: str | None = None
    imported_from: Literal["explicit_cli", "explicit_local_file"]
    actor: str = Field(default="local_user", min_length=1)
    notes: str | None = None

    @model_validator(mode="after")
    def validate_review_time(self) -> "HumanOutcome":
        if (
            self.review_time_accounting_status == "complete"
            and self.review_minutes is None
        ):
            raise ValueError("complete review time requires minutes")
        if (
            self.review_time_accounting_status != "complete"
            and self.review_minutes is not None
        ):
            raise ValueError("unknown or not-applicable review time must remain null")
        if self.full_trace_accounting_status == "complete":
            if self.full_trace_opened is None:
                raise ValueError("complete full-trace accounting requires a boolean")
        elif self.full_trace_opened is not None:
            raise ValueError("unknown full-trace use must remain null")
        if self.outcome == "corrected_before_use" and not self.correction_summary:
            raise ValueError("corrected outcomes require a correction summary")
        if (
            self.outcome in {"reverted", "reopened_defect"}
            and not self.linked_reference
        ):
            raise ValueError("later adverse outcomes require an explicit reference")
        return self


class SupervisionMetrics(StrictAdaptiveModel):
    schema_version: Literal["villani.supervision_metrics.v1"] = (
        SUPERVISION_METRICS_SCHEMA_VERSION
    )
    metrics_id: str = Field(pattern=r"^smet_[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    policy_version: Literal["adaptive_verification_v1"] = (
        ADAPTIVE_VERIFICATION_POLICY_VERSION
    )
    calculated_at: UtcDateTime
    eligible_outcome_count: int = Field(ge=0)
    evidence_expansion_count: int = Field(ge=0)
    explicit_review_minutes: float | None = Field(default=None, ge=0)
    review_time_accounting_status: Literal[
        "complete", "partial", "unknown", "not_applicable"
    ]
    application_without_full_trace_count: int = Field(ge=0)
    full_trace_accounting_status: Literal["complete", "unknown", "not_applicable"]
    correction_count: int = Field(ge=0)
    false_acceptance_count: int = Field(ge=0)
    false_rejection_count: int = Field(ge=0)
    verification_cost: MoneyAccounting
    review_cost: MoneyAccounting
    total_accepted_change_cost: MoneyAccounting
    source_outcome_ids: list[str]

    @model_validator(mode="after")
    def validate_metrics(self) -> "SupervisionMetrics":
        if self.review_time_accounting_status in {"complete", "partial"}:
            if self.explicit_review_minutes is None:
                raise ValueError("known review time requires minutes")
        elif self.explicit_review_minutes is not None:
            raise ValueError("unknown review time must remain null")
        if (
            self.full_trace_accounting_status != "complete"
            and self.application_without_full_trace_count != 0
        ):
            raise ValueError("unknown full-trace use cannot claim an observed count")
        if self.source_outcome_ids != sorted(set(self.source_outcome_ids)):
            raise ValueError("source outcome ids must be sorted and unique")
        return self


class GateDArm(StrictAdaptiveModel):
    strategy: Literal[
        "strongest_only", "accepted_change_optimizer", "optimizer_plus_adaptive"
    ]
    case_ids: list[str]
    eligible_cases: int = Field(ge=0)
    accepted_as_is: int = Field(ge=0)
    false_acceptances: int = Field(ge=0)
    total_cost: MoneyAccounting
    elapsed_duration: DurationAccounting
    review_minutes: float | None = Field(default=None, ge=0)
    review_time_accounting_status: Literal["complete", "partial", "unknown"]
    explainable_routes: bool
    safe_fallback: bool

    @model_validator(mode="after")
    def validate_arm(self) -> "GateDArm":
        if self.case_ids != sorted(set(self.case_ids)):
            raise ValueError("case ids must be sorted and unique")
        if self.eligible_cases != len(self.case_ids):
            raise ValueError("eligible case count must equal case id count")
        if self.accepted_as_is > self.eligible_cases:
            raise ValueError("accepted-as-is count exceeds eligible cases")
        if self.review_time_accounting_status in {"complete", "partial"}:
            if self.review_minutes is None:
                raise ValueError("known review time requires minutes")
        elif self.review_minutes is not None:
            raise ValueError("unknown review time must remain null")
        return self


class GateDCheck(StrictAdaptiveModel):
    check: Literal[
        "matched_founder_cases",
        "accepted_as_is_no_regression",
        "zero_false_acceptance",
        "lower_cost_or_time",
        "lower_review_burden",
        "explainability",
        "safe_fallback",
    ]
    status: Literal["pass", "fail", "insufficient_evidence"]
    reason: str = Field(min_length=1)


class GateDReport(StrictAdaptiveModel):
    schema_version: Literal["villani.gate_d.v1"] = GATE_D_SCHEMA_VERSION
    gate_id: str = Field(pattern=r"^gated_[0-9a-f]{64}$")
    policy_version: Literal["adaptive_verification_v1"] = (
        ADAPTIVE_VERIFICATION_POLICY_VERSION
    )
    generated_at: UtcDateTime
    status: Literal["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"]
    arms: list[GateDArm]
    checks: list[GateDCheck]
    warnings: list[str]
    evidence_references: list[str]
    next_milestone_permitted: bool

    @model_validator(mode="after")
    def validate_gate(self) -> "GateDReport":
        strategies = [item.strategy for item in self.arms]
        expected = {
            "strongest_only",
            "accepted_change_optimizer",
            "optimizer_plus_adaptive",
        }
        if set(strategies) != expected or len(strategies) != len(expected):
            raise ValueError("Gate D requires exactly the three policy arms")
        check_names = [item.check for item in self.checks]
        expected_checks = {
            "matched_founder_cases",
            "accepted_as_is_no_regression",
            "zero_false_acceptance",
            "lower_cost_or_time",
            "lower_review_burden",
            "explainability",
            "safe_fallback",
        }
        if set(check_names) != expected_checks or len(check_names) != len(
            expected_checks
        ):
            raise ValueError("Gate D requires every normative check exactly once")
        statuses = {item.status for item in self.checks}
        if self.status == "PASS" and statuses != {"pass"}:
            raise ValueError("Gate D PASS requires every check to pass")
        if self.status == "FAIL" and "fail" not in statuses:
            raise ValueError("Gate D FAIL requires a failed check")
        if (
            self.status == "INSUFFICIENT_EVIDENCE"
            and "insufficient_evidence" not in statuses
        ):
            raise ValueError("insufficient Gate D requires an insufficient check")
        if self.next_milestone_permitted is not (self.status == "PASS"):
            raise ValueError("next milestone permission must follow Gate D status")
        return self
