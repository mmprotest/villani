"""Durable repository-specific qualification contracts.

The PT7 contracts deliberately separate immutable observations from derived
status.  An observation can be superseded or invalidated, but is never edited
or deleted.  Derived assessments always retain the exact repository, task,
agent-system, execution-environment, verification-policy, and software-version
context used to calculate them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)


QUALIFICATION_POLICY_VERSION: Literal["repository_qualification_v1"] = (
    "repository_qualification_v1"
)
QUALIFICATION_CONFIGURATION_SCHEMA_VERSION = (
    "villani.repository_qualification_configuration.v1"
)
QUALIFICATION_OBSERVATION_SCHEMA_VERSION: Literal[
    "villani.qualification_observation.v1"
] = "villani.qualification_observation.v1"
QUALIFICATION_INVALIDATION_SCHEMA_VERSION: Literal[
    "villani.qualification_invalidation.v1"
] = "villani.qualification_invalidation.v1"
QUALIFICATION_SNAPSHOT_SCHEMA_VERSION: Literal["villani.qualification_snapshot.v1"] = (
    "villani.qualification_snapshot.v1"
)
GATE_C_SCHEMA_VERSION: Literal["villani.gate_c.v1"] = "villani.gate_c.v1"

QualificationState: TypeAlias = Literal[
    "qualified", "provisional", "experimental", "unsupported"
]
BackoffLevel: TypeAlias = Literal[
    "exact_repository_task",
    "repository_category",
    "repository_wide",
    "compatible_repository_cohort",
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


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class StrictQualificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QualificationPolicy(StrictQualificationModel):
    policy_version: Literal["repository_qualification_v1"] = (
        QUALIFICATION_POLICY_VERSION
    )
    minimum_qualified_observations: int = Field(default=20, ge=20)
    provisional_maximum_observations: int = Field(default=19, ge=1)
    wilson_z: float = Field(default=1.959963984540054, gt=0)
    task_wilson_thresholds: dict[str, float] = Field(
        default_factory=lambda: {"low": 0.60, "medium": 0.70, "high": 0.80}
    )
    maximum_evidence_age_days: int = Field(default=180, ge=1)
    recent_reliability_window: int = Field(default=5, ge=1)
    approved_backoff_levels: list[BackoffLevel] = Field(
        default_factory=lambda: [
            "exact_repository_task",
            "repository_category",
            "repository_wide",
        ]
    )
    compatible_repository_cohorts: dict[str, list[str]] = Field(default_factory=dict)
    approved_repository_cohorts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_policy(self) -> "QualificationPolicy":
        if self.provisional_maximum_observations >= self.minimum_qualified_observations:
            raise ValueError(
                "provisional maximum must be below the qualified sample minimum"
            )
        if not self.task_wilson_thresholds:
            raise ValueError("at least one task Wilson threshold is required")
        if any(
            value < 0 or value > 1 for value in self.task_wilson_thresholds.values()
        ):
            raise ValueError("task Wilson thresholds must be between zero and one")
        if len(self.approved_backoff_levels) != len(set(self.approved_backoff_levels)):
            raise ValueError("approved backoff levels must be unique")
        for cohort, repositories in self.compatible_repository_cohorts.items():
            if (
                not cohort
                or len(repositories) < 2
                or len(repositories) != len(set(repositories))
            ):
                raise ValueError(
                    "compatible repository cohorts require a name and at least two unique repositories"
                )
        unknown = set(self.approved_repository_cohorts) - set(
            self.compatible_repository_cohorts
        )
        if unknown:
            raise ValueError(
                f"approved repository cohorts are undefined: {sorted(unknown)!r}"
            )
        return self


class QualificationTaskProfile(StrictQualificationModel):
    category: str = Field(min_length=1)
    difficulty: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    required_capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_capabilities(self) -> "QualificationTaskProfile":
        if self.required_capabilities != sorted(set(self.required_capabilities)):
            raise ValueError("required capabilities must be sorted and unique")
        return self


class QualificationSystemIdentity(StrictQualificationModel):
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    route_name: str = Field(min_length=1)
    harness_id: str = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_revision: str | None = None
    serving_engine: str | None = None
    serving_engine_version: str | None = None
    execution_provider: str = Field(min_length=1)
    execution_environment_fingerprint: str = Field(min_length=1)
    verification_policy_version: str = Field(min_length=1)
    software_versions: dict[str, str]
    identity_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_versions(self) -> "QualificationSystemIdentity":
        required = {
            "harness",
            "adapter",
            "protocol",
            "model",
            "execution_provider",
            "verification_policy",
        }
        missing = sorted(required - set(self.software_versions))
        if missing or any(
            not key or not value for key, value in self.software_versions.items()
        ):
            raise ValueError(
                f"complete non-empty software version identity is required; missing={missing!r}"
            )
        content = self.model_dump(mode="json", exclude={"identity_digest"})
        if self.identity_digest != _canonical_digest(content):
            raise ValueError(
                "identity_digest must address the complete qualification system identity"
            )
        return self


class QualificationArtifactReference(StrictQualificationModel):
    kind: str = Field(min_length=1)
    path: str = Field(min_length=1)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def safe_path(self) -> "QualificationArtifactReference":
        normalized = self.path.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute()
            or PureWindowsPath(self.path).is_absolute()
            or ".." in path.parts
        ):
            raise ValueError("qualification artifact paths must be safe and relative")
        return self


class QualificationObservation(StrictQualificationModel):
    schema_version: Literal["villani.qualification_observation.v1"] = (
        QUALIFICATION_OBSERVATION_SCHEMA_VERSION
    )
    observation_id: str = Field(pattern=r"^qobs_[0-9a-f]{64}$")
    recorded_at: UtcDateTime
    observed_at: UtcDateTime
    source_kind: Literal[
        "evaluation_trial", "imported_qualification_evidence", "canonical_run"
    ]
    source_suite_id: str | None = None
    source_suite_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_task_id: str = Field(min_length=1)
    source_task_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_trial_id: str = Field(min_length=1)
    source_review_id: str | None = None
    repository_id: str = Field(min_length=1)
    repository_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    repository_baseline_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_profile: QualificationTaskProfile
    profile_source: Literal[
        "authoritative_run_classification", "explicit_evaluation_profile"
    ]
    system: QualificationSystemIdentity
    baseline_valid: bool
    candidate_evidence_complete: bool
    authoritative_verification_complete: bool
    infrastructure_status: Literal["resolved", "excluded", "unresolved"]
    human_review_required: bool
    human_review_status: Literal["complete", "missing", "not_applicable"]
    corruption_detected: bool
    secret_issue_detected: bool
    target_repository_modified: Literal[False] = False
    proved_acceptable: bool | None = None
    accepted_as_is: bool | None = None
    successful: bool | None = None
    false_acceptance: bool = False
    false_rejection: bool = False
    later_rollback: bool = False
    reopened_defect: bool = False
    cost_amount: float | None = Field(default=None, ge=0)
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    cost_accounting_status: Literal["complete", "partial", "unknown", "not_applicable"]
    duration_ms: int | None = Field(default=None, ge=0)
    duration_accounting_status: Literal[
        "complete", "partial", "unknown", "not_applicable"
    ]
    review_minutes: float | None = Field(default=None, ge=0)
    eligible: bool
    exclusion_reason: str | None = None
    artifacts: list[QualificationArtifactReference] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_truth(self) -> "QualificationObservation":
        required_truth = bool(
            self.baseline_valid
            and self.candidate_evidence_complete
            and self.authoritative_verification_complete
            and self.infrastructure_status == "resolved"
            and (
                not self.human_review_required or self.human_review_status == "complete"
            )
            and not self.corruption_detected
            and not self.secret_issue_detected
        )
        if self.eligible != required_truth:
            raise ValueError("eligible must exactly reflect the PT7 evidence rules")
        if self.eligible and self.exclusion_reason is not None:
            raise ValueError("eligible evidence cannot have an exclusion reason")
        if not self.eligible and not self.exclusion_reason:
            raise ValueError("excluded evidence requires a persisted reason")
        if self.authoritative_verification_complete and self.proved_acceptable is None:
            raise ValueError("authoritative verification requires a binary result")
        if (
            not self.authoritative_verification_complete
            and self.proved_acceptable is not None
        ):
            raise ValueError("incomplete verification cannot claim acceptability")
        if (
            self.human_review_required
            and self.human_review_status == "complete"
            and self.accepted_as_is is None
        ):
            raise ValueError("completed required review must state accepted-as-is")
        if (
            not self.human_review_required
            and self.human_review_status != "not_applicable"
        ):
            raise ValueError("optional review must be explicitly not applicable")
        expected_success = None
        if self.eligible:
            expected_success = bool(
                self.proved_acceptable is True
                and (not self.human_review_required or self.accepted_as_is is True)
                and not self.false_acceptance
                and not self.later_rollback
                and not self.reopened_defect
            )
        if self.successful != expected_success:
            raise ValueError(
                "successful must be the binary proved-and-accepted-as-is result"
            )
        if self.cost_accounting_status == "complete":
            if self.cost_amount is None or self.cost_currency is None:
                raise ValueError("complete cost requires amount and currency")
        elif self.cost_accounting_status in {"unknown", "not_applicable"} and (
            self.cost_amount is not None or self.cost_currency is not None
        ):
            raise ValueError("unknown cost is null, never numeric zero")
        if self.duration_accounting_status == "complete" and self.duration_ms is None:
            raise ValueError("complete duration requires duration_ms")
        if (
            self.duration_accounting_status in {"unknown", "not_applicable"}
            and self.duration_ms is not None
        ):
            raise ValueError("unknown duration must be null")
        return self


class QualificationInvalidation(StrictQualificationModel):
    schema_version: Literal["villani.qualification_invalidation.v1"] = (
        QUALIFICATION_INVALIDATION_SCHEMA_VERSION
    )
    invalidation_id: str = Field(pattern=r"^qinv_[0-9a-f]{64}$")
    recorded_at: UtcDateTime
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    route_name: str = Field(min_length=1)
    repository_id: str | None = None
    reason: Literal[
        "explicit_disable",
        "capability_loss",
        "conformance_failure",
        "environment_prohibited",
        "version_incompatible",
        "model_identity_change",
        "provider_identity_change",
        "execution_environment_change",
        "verification_policy_change",
        "repository_lineage_divergence",
        "recent_reliability_breach",
        "false_acceptance",
        "operator_invalidation",
    ]
    severity: Literal["warning", "severe", "unsupported"]
    evidence_reference: str = Field(min_length=1)
    evidence_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    detail: str = Field(min_length=1)


class QualificationDistribution(StrictQualificationModel):
    known_count: int = Field(ge=0)
    unknown_count: int = Field(ge=0)
    minimum: float | None = Field(default=None, ge=0)
    median: float | None = Field(default=None, ge=0)
    p90: float | None = Field(default=None, ge=0)
    maximum: float | None = Field(default=None, ge=0)
    unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def known_values_are_complete(self) -> "QualificationDistribution":
        values = (self.minimum, self.median, self.p90, self.maximum)
        if self.known_count == 0 and any(value is not None for value in values):
            raise ValueError("an unknown distribution cannot invent numeric values")
        if self.known_count > 0 and any(value is None for value in values):
            raise ValueError("a known distribution requires all summary values")
        return self


class QualificationDriftFlag(StrictQualificationModel):
    code: str = Field(min_length=1)
    severity: Literal["warning", "severe", "unsupported"]
    detail: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class QualificationStatistics(StrictQualificationModel):
    sample_count: int = Field(ge=0)
    successes: int = Field(ge=0)
    failures: int = Field(ge=0)
    exclusions: dict[str, int]
    acceptance_rate: float | None = Field(default=None, ge=0, le=1)
    wilson_lower_bound: float | None = Field(default=None, ge=0, le=1)
    proved_acceptable_count: int = Field(ge=0)
    accepted_as_is_count: int = Field(ge=0)
    false_acceptance_count: int = Field(ge=0)
    false_rejection_count: int = Field(ge=0)
    false_case_ids: list[str]
    cost_distribution_by_currency: dict[str, QualificationDistribution]
    cost_unknown_count: int = Field(ge=0)
    accepted_change_cost_by_currency: dict[str, QualificationDistribution]
    accepted_change_cost_unknown_count: int = Field(ge=0)
    duration_distribution: QualificationDistribution
    review_minutes_distribution: QualificationDistribution
    last_evidence_at: UtcDateTime | None
    software_version_diversity: dict[str, list[str]]
    drift_flags: list[QualificationDriftFlag]

    @model_validator(mode="after")
    def validate_counts(self) -> "QualificationStatistics":
        if self.successes + self.failures != self.sample_count:
            raise ValueError("sample count must equal successes plus failures")
        if self.sample_count == 0 and (
            self.acceptance_rate is not None or self.wilson_lower_bound is not None
        ):
            raise ValueError("zero samples have unknown rates, not numeric zero")
        if self.sample_count > 0 and (
            self.acceptance_rate is None or self.wilson_lower_bound is None
        ):
            raise ValueError("nonzero samples require acceptance and Wilson rates")
        return self


class QualificationProfileKey(StrictQualificationModel):
    repository_id: str = Field(min_length=1)
    task_profile: QualificationTaskProfile
    system_identity_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    execution_environment_fingerprint: str = Field(min_length=1)
    verification_policy_version: str = Field(min_length=1)


class QualificationProfile(StrictQualificationModel):
    key: QualificationProfileKey
    observation_ids: list[str]
    statistics: QualificationStatistics
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class QualificationMigration(StrictQualificationModel):
    migration_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_digest: str | None = None
    status: Literal["not_present", "excluded", "complete"]
    exclusion_reason: str | None = None
    qualification_created: Literal[False] = False


class QualificationSnapshot(StrictQualificationModel):
    schema_version: Literal["villani.qualification_snapshot.v1"] = (
        QUALIFICATION_SNAPSHOT_SCHEMA_VERSION
    )
    generated_at: UtcDateTime
    policy: QualificationPolicy
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observation_count: int = Field(ge=0)
    invalidation_count: int = Field(ge=0)
    superseded_observation_count: int = Field(ge=0)
    profiles: list[QualificationProfile]
    exclusions: dict[str, int]
    migrations: list[QualificationMigration]


class QualificationBackoffEvidence(StrictQualificationModel):
    level: BackoffLevel
    repository_ids: list[str]
    cohort: str | None = None
    eligible_observation_count: int = Field(ge=0)
    selected: bool
    approved_for_qualification: bool
    rejection_reasons: list[str] = Field(default_factory=list)


class QualificationAssessment(StrictQualificationModel):
    schema_version: Literal["villani.qualification_assessment.v1"] = (
        "villani.qualification_assessment.v1"
    )
    policy_version: str = Field(min_length=1)
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    route_name: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    repository_head: str | None = None
    task_profile: QualificationTaskProfile
    state: QualificationState
    selected_level: BackoffLevel | None
    selected_cohort: str | None = None
    task_wilson_threshold: float = Field(ge=0, le=1)
    statistics: QualificationStatistics
    backoff_evidence: list[QualificationBackoffEvidence]
    automatic_eligible: bool
    provisional_fallback_eligible: bool
    manual_override_required: bool
    unsupported_reasons: list[str]
    caveat: str = Field(min_length=1)
    doctor_action: str = Field(min_length=1)
    evidence_action: str = Field(min_length=1)
    evaluated_at: UtcDateTime

    @model_validator(mode="after")
    def validate_state_eligibility(self) -> "QualificationAssessment":
        if self.state == "qualified" and not self.automatic_eligible:
            raise ValueError("qualified systems must be automatically eligible")
        if self.state != "qualified" and self.automatic_eligible:
            raise ValueError("only qualified systems are automatically eligible")
        if self.state == "provisional" and not self.provisional_fallback_eligible:
            raise ValueError("provisional systems must be explicit fallback candidates")
        if self.state != "provisional" and self.provisional_fallback_eligible:
            raise ValueError("only provisional systems can be provisional fallbacks")
        if self.state == "unsupported" and not self.unsupported_reasons:
            raise ValueError("unsupported systems require actionable reasons")
        return self


class QualificationScorecard(StrictQualificationModel):
    system_name: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    assessment: QualificationAssessment
    accepted_as_is: int = Field(ge=0)
    proved_acceptable: int = Field(ge=0)
    false_cases: int = Field(ge=0)
    known_cost: bool
    known_duration: bool
    known_review_time: bool
    failures: int = Field(ge=0)


class GateCCheck(StrictQualificationModel):
    check_id: str = Field(min_length=1)
    system_id: str | None = None
    status: Literal["pass", "fail", "insufficient_evidence"]
    actual: Any
    required: Any
    reason: str = Field(min_length=1)


class GateCReport(StrictQualificationModel):
    schema_version: Literal["villani.gate_c.v1"] = GATE_C_SCHEMA_VERSION
    gate: Literal["C"] = "C"
    generated_at: UtcDateTime
    repository_id: str = Field(min_length=1)
    repository_head: str | None = None
    task_profile: QualificationTaskProfile
    policy_version: str = Field(min_length=1)
    status: Literal["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"]
    checks: list[GateCCheck] = Field(min_length=1)
    scorecards: list[QualificationScorecard]
    unmatched_sample_warning: str | None = None
    evidence_snapshot_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_gate_status(self) -> "GateCReport":
        statuses = {item.status for item in self.checks}
        expected = (
            "FAIL"
            if "fail" in statuses
            else "INSUFFICIENT_EVIDENCE"
            if "insufficient_evidence" in statuses
            else "PASS"
        )
        if self.status != expected:
            raise ValueError(f"Gate C status must be {expected}")
        return self


__all__ = [
    "BackoffLevel",
    "GATE_C_SCHEMA_VERSION",
    "GateCCheck",
    "GateCReport",
    "QUALIFICATION_INVALIDATION_SCHEMA_VERSION",
    "QUALIFICATION_CONFIGURATION_SCHEMA_VERSION",
    "QUALIFICATION_OBSERVATION_SCHEMA_VERSION",
    "QUALIFICATION_POLICY_VERSION",
    "QUALIFICATION_SNAPSHOT_SCHEMA_VERSION",
    "QualificationArtifactReference",
    "QualificationAssessment",
    "QualificationBackoffEvidence",
    "QualificationDistribution",
    "QualificationDriftFlag",
    "QualificationInvalidation",
    "QualificationMigration",
    "QualificationObservation",
    "QualificationPolicy",
    "QualificationProfile",
    "QualificationProfileKey",
    "QualificationScorecard",
    "QualificationSnapshot",
    "QualificationState",
    "QualificationStatistics",
    "QualificationSystemIdentity",
    "QualificationTaskProfile",
    "UtcDateTime",
]
