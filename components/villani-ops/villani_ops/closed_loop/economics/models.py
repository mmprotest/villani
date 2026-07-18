"""Versioned contracts for conservative accepted-change economics.

The contracts keep qualification (whether a route may be used) separate from
economics (how eligible routes compare).  Unknown money and time are always
explicit, and a partial objective can never masquerade as a fully accounted
total.
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

from ..qualification.models import (
    QualificationDistribution,
    QualificationTaskProfile,
)


ROUTE_POLICY_SCHEMA_VERSION: Literal["villani.route_policy.v1"] = (
    "villani.route_policy.v1"
)
ROUTE_PLAN_SCHEMA_VERSION: Literal["villani.route_plan.v1"] = "villani.route_plan.v1"
ECONOMICS_OBSERVATION_SCHEMA_VERSION: Literal["villani.economics_observation.v1"] = (
    "villani.economics_observation.v1"
)
ECONOMICS_SNAPSHOT_SCHEMA_VERSION: Literal["villani.economics_snapshot.v1"] = (
    "villani.economics_snapshot.v1"
)
ROUTE_POLICY_EVALUATION_SCHEMA_VERSION: Literal[
    "villani.route_policy_evaluation.v1"
] = "villani.route_policy_evaluation.v1"
ROUTE_POLICY_PUBLICATION_SCHEMA_VERSION: Literal[
    "villani.route_policy_publication.v1"
] = "villani.route_policy_publication.v1"
ONLINE_EVIDENCE_UPDATE_SCHEMA_VERSION: Literal["villani.online_evidence_update.v1"] = (
    "villani.online_evidence_update.v1"
)
ACCEPTED_CHANGE_POLICY_VERSION = "accepted_change_economics_v1"
ECONOMICS_CONFIGURATION_SCHEMA_VERSION = (
    "villani.accepted_change_economics_configuration.v1"
)

AccountingStatus: TypeAlias = Literal[
    "complete", "partial", "unknown", "not_applicable"
]
QualificationState: TypeAlias = Literal[
    "qualified", "provisional", "experimental", "unsupported"
]
RouteStrategy: TypeAlias = Literal[
    "accepted_change_optimizer",
    "strongest_only",
    "cheapest_qualified",
    "forced",
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


class StrictEconomicsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MoneyEstimate(StrictEconomicsModel):
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    accounting_status: AccountingStatus
    source: str = Field(min_length=1)
    sample_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_accounting(self) -> "MoneyEstimate":
        if self.accounting_status in {"complete", "partial"}:
            if self.amount is None or self.currency is None:
                raise ValueError("known or partial money requires amount and currency")
        elif self.amount is not None or self.currency is not None:
            raise ValueError("unknown or not-applicable money must remain null")
        return self


class DurationEstimate(StrictEconomicsModel):
    duration_ms: float | None = Field(default=None, ge=0)
    accounting_status: AccountingStatus
    source: str = Field(min_length=1)
    sample_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_accounting(self) -> "DurationEstimate":
        if self.accounting_status in {"complete", "partial"}:
            if self.duration_ms is None:
                raise ValueError("known or partial duration requires duration_ms")
        elif self.duration_ms is not None:
            raise ValueError("unknown or not-applicable duration must remain null")
        return self


class RouteConstraints(StrictEconomicsModel):
    local_only: bool = False
    prefer_local: bool = False
    allowed_providers: list[str] = Field(default_factory=list)
    preferred_provider: str | None = None
    excluded_systems: list[str] = Field(default_factory=list)
    forced_system: str | None = None
    strongest_only: bool = False
    maximum_known_cost_usd: float | None = Field(default=None, ge=0)
    allowed_permission_profiles: list[str] = Field(default_factory=list)
    allow_experimental_forced: bool = False

    @model_validator(mode="after")
    def normalized_lists(self) -> "RouteConstraints":
        for name in (
            "allowed_providers",
            "excluded_systems",
            "allowed_permission_profiles",
        ):
            value = getattr(self, name)
            if value != sorted(set(value)):
                raise ValueError(f"{name} must be sorted and unique")
        if self.strongest_only and self.forced_system:
            raise ValueError("strongest_only and forced_system are mutually exclusive")
        return self


class RoutePolicy(StrictEconomicsModel):
    schema_version: Literal["villani.route_policy.v1"] = ROUTE_POLICY_SCHEMA_VERSION
    policy_version: str = Field(default=ACCEPTED_CHANGE_POLICY_VERSION, min_length=1)
    strategy: RouteStrategy = "accepted_change_optimizer"
    objective_version: Literal["total_accepted_change_v1"] = "total_accepted_change_v1"
    conservative_cost_statistic: Literal["p90", "median"] = "p90"
    conservative_duration_statistic: Literal["p90", "median"] = "p90"
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    human_review_cost_per_minute: float | None = Field(default=None, ge=0)
    latency_penalty_per_second: float | None = Field(default=None, ge=0)
    allow_provisional_fallback: bool = True
    require_complete_objective_for_comparison: bool = True
    constraints: RouteConstraints = Field(default_factory=RouteConstraints)


class AcceptedChangeObjective(StrictEconomicsModel):
    objective_version: Literal["total_accepted_change_v1"] = "total_accepted_change_v1"
    execution_cost: MoneyEstimate
    verification_cost: MoneyEstimate
    human_review_cost: MoneyEstimate
    retry_escalation_cost: MoneyEstimate
    latency_penalty: MoneyEstimate
    conservative_acceptance_probability: float | None = Field(default=None, gt=0, le=1)
    probability_source: str = Field(min_length=1)
    known_numerator_cost: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    accounting_status: AccountingStatus
    unknown_components: list[str]
    expected_accepted_change_cost: float | None = Field(default=None, ge=0)
    partial_expected_known_cost: float | None = Field(default=None, ge=0)
    expected_duration: DurationEstimate

    @model_validator(mode="after")
    def validate_objective(self) -> "AcceptedChangeObjective":
        if self.accounting_status == "complete":
            if (
                self.known_numerator_cost is None
                or self.currency is None
                or self.conservative_acceptance_probability is None
                or self.expected_accepted_change_cost is None
                or self.unknown_components
            ):
                raise ValueError("complete objective requires a complete numeric total")
            if self.partial_expected_known_cost is not None:
                raise ValueError("complete objective cannot also be partial")
        elif self.accounting_status == "partial":
            if self.known_numerator_cost is None or not self.unknown_components:
                raise ValueError(
                    "partial objective requires a known subtotal and unknowns"
                )
            if self.expected_accepted_change_cost is not None:
                raise ValueError("partial objective cannot claim a full expected total")
        elif self.accounting_status in {"unknown", "not_applicable"}:
            if (
                self.known_numerator_cost is not None
                or self.expected_accepted_change_cost is not None
                or self.partial_expected_known_cost is not None
            ):
                raise ValueError("unknown objective cannot contain numeric totals")
        return self


class RouteCandidateInput(StrictEconomicsModel):
    backend_name: str = Field(min_length=1)
    route_name: str = Field(min_length=1)
    system_id: str | None = Field(default=None, pattern=r"^asys_[0-9a-f]{64}$")
    harness: str = Field(min_length=1)
    model: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    local: bool
    permission_profile: str = Field(min_length=1)
    availability: Literal["available", "unavailable", "rate_limited", "unknown"]
    qualification_state: QualificationState
    qualification_level: str | None = None
    qualification_policy_version: str = Field(min_length=1)
    qualification_sample_count: int = Field(ge=0)
    conservative_acceptance_probability: float | None = Field(default=None, ge=0, le=1)
    task_probability_threshold: float = Field(ge=0, le=1)
    false_acceptance_count: int = Field(ge=0)
    drift_flags: list[str] = Field(default_factory=list)
    capability_score: float = Field(ge=0)
    execution_cost: MoneyEstimate
    verification_cost: MoneyEstimate
    human_review_cost: MoneyEstimate
    retry_escalation_cost: MoneyEstimate
    duration: DurationEstimate
    latency_penalty: MoneyEstimate
    reserve_satisfied: bool
    reserve_evidence: dict[str, Any]
    input_rejection_reasons: list[str] = Field(default_factory=list)


class RouteConsideration(StrictEconomicsModel):
    backend_name: str
    route_name: str
    system_id: str | None
    harness: str
    model: str
    provider: str
    local: bool
    permission_profile: str
    availability: str
    qualification_state: QualificationState
    qualification_level: str | None
    qualification_sample_count: int
    conservative_acceptance_probability: float | None
    task_probability_threshold: float
    capability_score: float
    eligible: bool
    rejection_reasons: list[str]
    unknowns: list[str]
    objective: AcceptedChangeObjective


class RouteSequenceEconomics(StrictEconomicsModel):
    systems: list[str]
    conservative_success_probability: float | None = Field(default=None, ge=0, le=1)
    expected_cost_before_acceptance: float | None = Field(default=None, ge=0)
    expected_accepted_change_cost: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    expected_duration_ms: float | None = Field(default=None, ge=0)
    accounting_status: AccountingStatus
    unknowns: list[str]


class RoutePlan(StrictEconomicsModel):
    schema_version: Literal["villani.route_plan.v1"] = ROUTE_PLAN_SCHEMA_VERSION
    plan_id: str = Field(pattern=r"^rplan_[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    repository_head: str | None = None
    task_profile: QualificationTaskProfile
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_cutoff: UtcDateTime | None = None
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    systems_considered: list[RouteConsideration]
    selected_first_system: str | None
    ordered_fallbacks: list[str]
    sequence_economics: RouteSequenceEconomics
    reserves: dict[str, Any]
    constraints: RouteConstraints
    selection_mode: Literal[
        "accepted_change_optimizer",
        "sparse_strongest_evidence",
        "strongest_only",
        "cheapest_qualified",
        "provisional_fallback",
        "forced",
        "no_safe_route",
        "sequential_retry",
        "sequential_escalation",
    ]
    forced_choice: bool
    automatic_policy_metrics_eligible: bool
    unknowns: list[str]
    explanation: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_selected_route(self) -> "RoutePlan":
        eligible = {
            item.route_name for item in self.systems_considered if item.eligible
        }
        if (
            self.selected_first_system is not None
            and self.selected_first_system not in eligible
        ):
            raise ValueError("selected first system must be an eligible consideration")
        if any(item not in eligible for item in self.ordered_fallbacks):
            raise ValueError("fallback systems must be eligible considerations")
        if self.forced_choice == self.automatic_policy_metrics_eligible:
            raise ValueError(
                "forced choices are excluded from automatic policy metrics"
            )
        return self


class EconomicsObservation(StrictEconomicsModel):
    schema_version: Literal["villani.economics_observation.v1"] = (
        ECONOMICS_OBSERVATION_SCHEMA_VERSION
    )
    observation_id: str = Field(pattern=r"^eobs_[0-9a-f]{64}$")
    recorded_at: UtcDateTime
    observed_at: UtcDateTime
    source_run_id: str = Field(min_length=1)
    source_route_plan_id: str = Field(pattern=r"^rplan_[0-9a-f]{64}$")
    qualification_observation_id: str = Field(pattern=r"^qobs_[0-9a-f]{64}$")
    repository_id: str = Field(min_length=1)
    task_profile: QualificationTaskProfile
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    system_identity_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    route_name: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    forced_choice: bool
    qualification_eligible: bool
    authoritative_verification_complete: bool
    infrastructure_status: Literal["resolved", "excluded", "unresolved"]
    proved_acceptable: bool | None
    accepted_as_is: bool | None
    false_acceptance: bool = False
    eligible_for_profile: bool
    eligible_for_automatic_policy_metrics: bool
    exclusion_reason: str | None = None
    execution_cost: MoneyEstimate
    verification_cost: MoneyEstimate
    human_review_cost: MoneyEstimate
    retry_escalation_cost: MoneyEstimate
    duration: DurationEstimate
    review_minutes: float | None = Field(default=None, ge=0)
    attempt_count: int = Field(ge=1)
    escalation_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_eligibility(self) -> "EconomicsObservation":
        eligible = bool(
            self.qualification_eligible
            and self.authoritative_verification_complete
            and self.infrastructure_status == "resolved"
            and not self.false_acceptance
        )
        if self.eligible_for_profile != eligible:
            raise ValueError(
                "economics profile eligibility must match verified evidence"
            )
        automatic = eligible and not self.forced_choice
        if self.eligible_for_automatic_policy_metrics != automatic:
            raise ValueError(
                "forced or excluded outcomes cannot train automatic policy"
            )
        if eligible and self.exclusion_reason is not None:
            raise ValueError(
                "eligible economics evidence cannot have an exclusion reason"
            )
        if not eligible and not self.exclusion_reason:
            raise ValueError("excluded economics evidence requires a reason")
        return self


class EconomicsProfileKey(StrictEconomicsModel):
    repository_id: str
    task_profile: QualificationTaskProfile
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    system_identity_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    route_name: str


class EconomicsProfile(StrictEconomicsModel):
    key: EconomicsProfileKey
    observation_ids: list[str]
    sample_count: int = Field(ge=0)
    successes: int = Field(ge=0)
    failures: int = Field(ge=0)
    exclusions: dict[str, int]
    cost_distributions: dict[str, dict[str, QualificationDistribution]]
    cost_unknown_counts: dict[str, int]
    duration_distribution: QualificationDistribution
    review_minutes_distribution: QualificationDistribution
    attempt_count_distribution: QualificationDistribution
    escalation_count_distribution: QualificationDistribution
    false_acceptance_count: int = Field(ge=0)
    last_evidence_at: UtcDateTime | None
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_counts(self) -> "EconomicsProfile":
        if self.successes + self.failures != self.sample_count:
            raise ValueError(
                "economics sample count must equal successes plus failures"
            )
        return self


class EconomicsSnapshot(StrictEconomicsModel):
    schema_version: Literal["villani.economics_snapshot.v1"] = (
        ECONOMICS_SNAPSHOT_SCHEMA_VERSION
    )
    generated_at: UtcDateTime
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observation_count: int = Field(ge=0)
    profiles: list[EconomicsProfile]
    exclusions: dict[str, int]


class HistoricalSystemOutcome(StrictEconomicsModel):
    route_name: str
    accepted_as_is: bool | None
    proved_acceptable: bool | None
    false_acceptance: bool
    eligible: bool
    total_cost: MoneyEstimate
    duration: DurationEstimate
    review_minutes: float | None = Field(default=None, ge=0)
    escalation_count: int | None = Field(default=None, ge=0)


class HistoricalRouteCase(StrictEconomicsModel):
    case_id: str
    decision_at: UtcDateTime
    repository_id: str
    repository_head: str | None
    task_profile: QualificationTaskProfile
    candidates: list[RouteCandidateInput]
    candidate_evidence_cutoffs: dict[str, UtcDateTime]
    outcomes: list[HistoricalSystemOutcome]
    forced_system: str | None = None


class StrategyMetrics(StrictEconomicsModel):
    strategy: RouteStrategy
    case_count: int = Field(ge=0)
    accepted_as_is: int = Field(ge=0)
    proved_acceptable: int = Field(ge=0)
    false_acceptance: int = Field(ge=0)
    failures: int = Field(ge=0)
    total_cost: MoneyEstimate
    elapsed_duration: DurationEstimate
    review_minutes: float | None = Field(default=None, ge=0)
    escalation_count: int | None = Field(default=None, ge=0)
    regret: MoneyEstimate
    unknown_input_rate: float = Field(ge=0, le=1)
    unmatched_outcome_count: int = Field(ge=0)


class PolicyChoiceComparison(StrictEconomicsModel):
    case_id: str
    active_choice: str | None
    proposed_choice: str | None
    active_probability: float | None = Field(default=None, ge=0, le=1)
    proposed_probability: float | None = Field(default=None, ge=0, le=1)
    reliability_non_decreasing: bool | None
    active_false_acceptance_exposure: bool | None
    proposed_false_acceptance_exposure: bool | None
    evidence_cutoff: UtcDateTime


class RoutePolicyEvaluation(StrictEconomicsModel):
    schema_version: Literal["villani.route_policy_evaluation.v1"] = (
        ROUTE_POLICY_EVALUATION_SCHEMA_VERSION
    )
    evaluation_id: str = Field(pattern=r"^rpeval_[0-9a-f]{64}$")
    generated_at: UtcDateTime
    active_policy_version: str
    active_policy_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    proposed_policy_version: str
    proposed_policy_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    point_in_time_replay: Literal[True] = True
    frozen_case_count: int = Field(ge=0)
    comparisons: list[PolicyChoiceComparison]
    strategy_metrics: list[StrategyMetrics]
    conservative_reliability_non_decreasing: bool
    false_acceptance_exposure_non_increasing: bool
    safe_to_publish: bool
    rejection_reasons: list[str]
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_safety(self) -> "RoutePolicyEvaluation":
        expected = bool(
            self.frozen_case_count > 0
            and self.conservative_reliability_non_decreasing
            and self.false_acceptance_exposure_non_increasing
            and not self.rejection_reasons
        )
        if self.safe_to_publish != expected:
            raise ValueError("safe_to_publish must be derived from fail-closed checks")
        return self


class RoutePolicyPublication(StrictEconomicsModel):
    schema_version: Literal["villani.route_policy_publication.v1"] = (
        ROUTE_POLICY_PUBLICATION_SCHEMA_VERSION
    )
    publication_id: str = Field(pattern=r"^rpub_[0-9a-f]{64}$")
    published_at: UtcDateTime
    policy: RoutePolicy
    policy_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluation_id: str = Field(pattern=r"^rpeval_[0-9a-f]{64}$")
    evaluation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prior_policy_version: str | None
    state: Literal["active", "rolled_back"]
    deterministic: Literal[True] = True
    authored_by_llm: Literal[False] = False


class OnlineEvidenceUpdateReport(StrictEconomicsModel):
    """Auditable outcome of the future-only economics update projection."""

    schema_version: Literal["villani.online_evidence_update.v1"] = (
        ONLINE_EVIDENCE_UPDATE_SCHEMA_VERSION
    )
    run_id: str = Field(min_length=1)
    recorded_at: UtcDateTime
    status: Literal["skipped", "recorded", "excluded", "failed"]
    qualification_observation_id: str | None = Field(
        default=None, pattern=r"^qobs_[0-9a-f]{64}$"
    )
    economics_observation_id: str | None = Field(
        default=None, pattern=r"^eobs_[0-9a-f]{64}$"
    )
    profile_updated: bool
    automatic_policy_metrics_eligible: bool
    reasons: list[str]

    @model_validator(mode="after")
    def validate_outcome(self) -> "OnlineEvidenceUpdateReport":
        if self.status == "recorded":
            if (
                self.qualification_observation_id is None
                or self.economics_observation_id is None
                or not self.profile_updated
            ):
                raise ValueError(
                    "recorded updates require both observation identifiers"
                )
        elif self.profile_updated or self.automatic_policy_metrics_eligible:
            raise ValueError("non-recorded updates cannot alter automatic profiles")
        if self.status in {"skipped", "excluded", "failed"} and not self.reasons:
            raise ValueError("non-recorded updates require an explicit reason")
        return self


__all__ = [
    "ACCEPTED_CHANGE_POLICY_VERSION",
    "AcceptedChangeObjective",
    "AccountingStatus",
    "DurationEstimate",
    "ECONOMICS_CONFIGURATION_SCHEMA_VERSION",
    "ECONOMICS_OBSERVATION_SCHEMA_VERSION",
    "ECONOMICS_SNAPSHOT_SCHEMA_VERSION",
    "EconomicsObservation",
    "EconomicsProfile",
    "EconomicsProfileKey",
    "EconomicsSnapshot",
    "HistoricalRouteCase",
    "HistoricalSystemOutcome",
    "MoneyEstimate",
    "ONLINE_EVIDENCE_UPDATE_SCHEMA_VERSION",
    "OnlineEvidenceUpdateReport",
    "PolicyChoiceComparison",
    "ROUTE_PLAN_SCHEMA_VERSION",
    "ROUTE_POLICY_EVALUATION_SCHEMA_VERSION",
    "ROUTE_POLICY_PUBLICATION_SCHEMA_VERSION",
    "ROUTE_POLICY_SCHEMA_VERSION",
    "RouteCandidateInput",
    "RouteConsideration",
    "RouteConstraints",
    "RoutePlan",
    "RoutePolicy",
    "RoutePolicyEvaluation",
    "RoutePolicyPublication",
    "RouteSequenceEconomics",
    "RouteStrategy",
    "StrategyMetrics",
    "UtcDateTime",
    "canonical_digest",
]
