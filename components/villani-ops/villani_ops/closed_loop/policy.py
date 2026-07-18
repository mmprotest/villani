"""Versioned deterministic bootstrap policy for the canonical closed loop."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from villani_ops.core.backend import Backend

from .agent_systems.models import AgentSystemIdentity
from .capabilities.models import (
    CapabilitySnapshot,
    EffectiveCapability,
    EmpiricalBackendInput,
    SequenceOptimizationResult,
)
from .capabilities.effective import (
    CapabilityResolutionConfiguration,
    resolve_effective_capability,
)
from .capabilities.optimizer import optimize_sequence
from .costs import estimate_attempt_cost
from .economics import (
    DurationEstimate,
    EconomicsProfile,
    EconomicsStore,
    MoneyEstimate,
    RouteCandidateInput,
    RouteConstraints,
    RoutePlan,
    RoutePolicy,
    plan_route,
    route_policy_from_configuration,
    with_latency_penalty,
)
from .interfaces import (
    AttemptSummary,
    BackendOption,
    BudgetContext,
    PolicyContext,
    PolicyDecision,
)
from .model_management import (
    capability_status,
    default_bootstrap_backend,
    is_local_backend,
)
from .policy_presets import (
    PUBLIC_POLICY_VERSION,
    configured_policy_preset,
    selection_preference,
)
from .progress import AttemptProgressAssessment, empty_progress_assessment
from .qualification import (
    QualificationAssessment,
    QualificationStore,
    assess_qualification,
    repository_qualification_context,
    task_profile,
)
from .stage_budget import (
    StageBudgetProjection,
    StageReserveConfiguration,
    project_stage_budget,
)


class BootstrapPolicyConfiguration(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = "bootstrap_v1"
    easy_min_capability: float = Field(default=20, ge=0)
    medium_min_capability: float = Field(default=50, ge=0)
    hard_min_capability: float = Field(default=80, ge=0)
    economy_confidence_threshold: float = Field(default=0.80, ge=0, le=1)
    conservative_confidence_threshold: float = Field(default=0.65, ge=0, le=1)
    max_same_backend_retries: int = Field(default=1, ge=0)
    classifier_retry_limit: int = Field(default=1, ge=0)
    verifier_retry_limit: int = Field(default=1, ge=0)
    repository_validation_retry_limit: int = Field(default=1, ge=0)
    accepted_candidates_required: int = Field(default=1, ge=1)
    allow_constraint_violations: bool = False
    allow_no_change_retry: bool = False
    minimum_relevant_diff_ratio: float = Field(default=0.25, ge=0, le=1)
    maximum_repeated_failure_ratio: float = Field(default=0.50, ge=0, le=1)
    stage_reserves: StageReserveConfiguration = Field(
        default_factory=StageReserveConfiguration
    )


class EmpiricalCapabilityConfiguration(CapabilityResolutionConfiguration):
    model_config = ConfigDict(extra="ignore")

    persisted_sequence_top_n: int = Field(default=100, ge=1)


def _policy_mapping(configuration: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = configuration.get("policy")
    return nested if isinstance(nested, Mapping) else configuration


def configured_backends(configuration: Mapping[str, Any]) -> dict[str, Backend]:
    raw = configuration.get("backends")
    if isinstance(raw, Mapping):
        items = []
        for name, value in raw.items():
            if isinstance(value, Backend):
                items.append(value)
            elif isinstance(value, Mapping):
                items.append(Backend.model_validate({"name": str(name), **dict(value)}))
    elif isinstance(raw, list):
        items = [
            value if isinstance(value, Backend) else Backend.model_validate(value)
            for value in raw
            if isinstance(value, (Backend, Mapping))
        ]
    else:
        items = []
    return {backend.name: backend for backend in items}


def required_capability(
    context: PolicyContext, configuration: BootstrapPolicyConfiguration
) -> tuple[float, str]:
    classification = context.classification
    if (
        classification.risk == "high"
        or classification.difficulty == "hard"
        or classification.confidence < configuration.conservative_confidence_threshold
    ):
        return (
            configuration.hard_min_capability,
            "hard_threshold: high risk, hard difficulty, or confidence below "
            "conservative_confidence_threshold",
        )
    if (
        classification.risk == "medium"
        or classification.difficulty == "medium"
        or classification.confidence < configuration.economy_confidence_threshold
    ):
        return (
            configuration.medium_min_capability,
            "medium_threshold: medium risk/difficulty or economy confidence not met",
        )
    return (
        configuration.easy_min_capability,
        "easy_threshold: easy, low risk, and confidence at or above "
        "economy_confidence_threshold",
    )


class BootstrapPolicyEngine:
    """Select sufficient coding backends and deterministic next actions."""

    def __init__(
        self,
        backends: Mapping[str, Backend],
        configuration: Mapping[str, Any] | None = None,
        capability_snapshot: CapabilitySnapshot | None = None,
        qualification_store: QualificationStore | None = None,
        agent_system_by_backend: Mapping[str, AgentSystemIdentity] | None = None,
        economics_store: EconomicsStore | None = None,
        route_policy: RoutePolicy | None = None,
    ) -> None:
        self.backends = dict(backends)
        self.raw_configuration = dict(configuration or {})
        self.configuration = BootstrapPolicyConfiguration.model_validate(
            _policy_mapping(configuration or {})
        )
        capability_values = self.raw_configuration.get("capabilities")
        self.empirical_configuration = EmpiricalCapabilityConfiguration.model_validate(
            capability_values if isinstance(capability_values, Mapping) else {}
        )
        self.capability_snapshot = capability_snapshot
        self.qualification_store = qualification_store
        self.agent_system_by_backend = dict(agent_system_by_backend or {})
        self.economics_store = economics_store
        configured_route_policy = route_policy_from_configuration(
            self.raw_configuration
        )
        self.route_policy = route_policy or configured_route_policy
        self._qualification_cache: dict[
            tuple[str, str, str, tuple[str, ...]],
            dict[str, QualificationAssessment],
        ] = {}
        self.public_preset = configured_policy_preset(self.raw_configuration)
        self.selection_preference = selection_preference(self.raw_configuration)
        self.public_policy_enabled = isinstance(
            self.raw_configuration.get("public_policy"), Mapping
        )
        if self.configuration.version != "bootstrap_v1":
            raise ValueError("bootstrap policy requires version 'bootstrap_v1'")

    @classmethod
    def from_configuration(
        cls,
        configuration: Mapping[str, Any],
        capability_snapshot: CapabilitySnapshot | None = None,
    ) -> BootstrapPolicyEngine:
        return cls(
            configured_backends(configuration),
            configuration,
            capability_snapshot=capability_snapshot,
        )

    def _repository_qualifications(
        self, context: PolicyContext
    ) -> dict[str, QualificationAssessment]:
        if self.qualification_store is None or not self.agent_system_by_backend:
            return {}
        qualification = self.raw_configuration.get("qualification")
        values = qualification if isinstance(qualification, Mapping) else {}
        repository_path = values.get("repository_path")
        if not isinstance(repository_path, str) or not repository_path:
            return {}
        required = tuple(sorted(set(context.classification.required_capabilities)))
        cache_key = (
            context.classification.category,
            context.classification.difficulty,
            context.classification.risk,
            required,
        )
        cached = self._qualification_cache.get(cache_key)
        if cached is not None:
            return cached
        repository = repository_qualification_context(repository_path)
        requested = task_profile(
            context.classification.category,
            context.classification.difficulty,
            context.classification.risk,
            required,
        )
        resolved: dict[str, QualificationAssessment] = {}
        for backend_name, identity in sorted(self.agent_system_by_backend.items()):
            backend = self.backends.get(backend_name)
            if backend is None or "coding" not in backend.roles:
                continue
            resolved[backend_name] = assess_qualification(
                identity=identity,
                repository=repository,
                requested_task=requested,
                configuration=self.raw_configuration,
                store=self.qualification_store,
                backend_execution_selection=backend.execution_environment,
            )
        self._qualification_cache[cache_key] = resolved
        return resolved

    @staticmethod
    def _profile_money(
        profile: EconomicsProfile | None,
        component: str,
        *,
        currency: str,
        statistic: str,
    ) -> MoneyEstimate | None:
        if profile is None:
            return None
        distribution = profile.cost_distributions.get(component, {}).get(currency)
        if distribution is None or distribution.known_count == 0:
            return None
        amount = getattr(distribution, statistic)
        if amount is None:
            return None
        return MoneyEstimate(
            amount=amount,
            currency=currency,
            accounting_status=(
                "complete" if distribution.unknown_count == 0 else "partial"
            ),
            source=f"repository_economics_{statistic}",
            sample_count=distribution.known_count,
        )

    def _economics_profile(
        self,
        assessment: QualificationAssessment,
    ) -> EconomicsProfile | None:
        if self.economics_store is None:
            return None
        return self.economics_store.profile_for(
            repository_id=assessment.repository_id,
            task_profile=assessment.task_profile,
            system_id=assessment.system_id,
        )

    def _verification_cost_estimate(
        self,
        profile: EconomicsProfile | None,
        *,
        currency: str,
    ) -> MoneyEstimate:
        observed = self._profile_money(
            profile,
            "verification_cost",
            currency=currency,
            statistic=self.route_policy.conservative_cost_statistic,
        )
        if observed is not None:
            return observed
        economics = self.raw_configuration.get("economics")
        values = economics if isinstance(economics, Mapping) else {}
        explicit = values.get("verification_cost_usd")
        if explicit is not None:
            return MoneyEstimate(
                amount=float(explicit),
                currency="USD",
                accounting_status="complete",
                source="explicit_verification_cost",
            )
        verifier = self.raw_configuration.get("verifier")
        verifier_values = verifier if isinstance(verifier, Mapping) else {}
        if verifier_values.get("no_llm") is True:
            return MoneyEstimate(
                amount=None,
                currency=None,
                accounting_status="not_applicable",
                source="deterministic_verifier_no_model_charge",
            )
        return MoneyEstimate(
            amount=None,
            currency=None,
            accounting_status="unknown",
            source="verification_cost_unavailable",
        )

    def _review_cost_estimate(
        self,
        profile: EconomicsProfile | None,
        *,
        currency: str,
    ) -> MoneyEstimate:
        delivery = self.raw_configuration.get("delivery")
        delivery_values = delivery if isinstance(delivery, Mapping) else {}
        mode = str(
            delivery_values.get("mode") or delivery_values.get("default_mode") or ""
        )
        if mode not in {"approve", "review"}:
            return MoneyEstimate(
                amount=None,
                currency=None,
                accounting_status="not_applicable",
                source="human_review_not_required_by_delivery_policy",
            )
        observed = self._profile_money(
            profile,
            "human_review_cost",
            currency=currency,
            statistic=self.route_policy.conservative_cost_statistic,
        )
        if observed is not None:
            return observed
        rate = self.route_policy.human_review_cost_per_minute
        distribution = (
            profile.review_minutes_distribution if profile is not None else None
        )
        minutes = (
            getattr(distribution, self.route_policy.conservative_duration_statistic)
            if distribution is not None and distribution.known_count > 0
            else None
        )
        if rate is not None and minutes is not None:
            return MoneyEstimate(
                amount=rate * minutes,
                currency=self.route_policy.currency,
                accounting_status=(
                    "complete"
                    if distribution and distribution.unknown_count == 0
                    else "partial"
                ),
                source="observed_review_minutes_times_configured_rate",
                sample_count=distribution.known_count if distribution else 0,
            )
        return MoneyEstimate(
            amount=None,
            currency=None,
            accounting_status="unknown",
            source=(
                "human_review_rate_unavailable"
                if rate is None
                else "human_review_minutes_unavailable"
            ),
        )

    def _economics_candidate(
        self,
        option: BackendOption,
        assessment: QualificationAssessment,
    ) -> RouteCandidateInput:
        backend = self.backends[option.backend_name]
        identity = self.agent_system_by_backend[option.backend_name]
        profile = self._economics_profile(assessment)
        currency = backend.currency.upper()
        execution = self._profile_money(
            profile,
            "execution_cost",
            currency=currency,
            statistic=self.route_policy.conservative_cost_statistic,
        )
        if execution is None:
            execution = MoneyEstimate(
                amount=option.estimated_cost_usd,
                currency=(currency if option.estimated_cost_usd is not None else None),
                accounting_status=(
                    "complete" if option.estimated_cost_usd is not None else "unknown"
                ),
                source=option.cost_source,
            )
        retry = self._profile_money(
            profile,
            "retry_escalation_cost",
            currency=currency,
            statistic=self.route_policy.conservative_cost_statistic,
        ) or MoneyEstimate(
            amount=None,
            currency=None,
            accounting_status="unknown",
            source="retry_escalation_cost_unavailable",
        )
        duration_distribution = (
            profile.duration_distribution if profile is not None else None
        )
        duration_value = (
            getattr(
                duration_distribution,
                self.route_policy.conservative_duration_statistic,
            )
            if duration_distribution is not None
            and duration_distribution.known_count > 0
            else option.estimated_duration_ms
        )
        duration = DurationEstimate(
            duration_ms=duration_value,
            accounting_status=(
                "complete"
                if duration_value is not None
                and (
                    duration_distribution is None
                    or duration_distribution.unknown_count == 0
                )
                else "partial"
                if duration_value is not None
                else "unknown"
            ),
            source=(
                f"repository_economics_{self.route_policy.conservative_duration_statistic}"
                if duration_distribution is not None
                and duration_distribution.known_count > 0
                else option.cost_components.get(
                    "duration_source", "configured_estimate"
                )
            ),
            sample_count=(
                duration_distribution.known_count if duration_distribution else 0
            ),
        )
        readiness = identity.readiness
        availability = "unknown"
        if readiness is not None:
            availability = (
                "available"
                if readiness.installed
                and readiness.version_supported is not False
                and readiness.authentication_status in {"ready", "not_applicable"}
                else "unavailable"
            )
        economics = self.raw_configuration.get("economics")
        economics_values = economics if isinstance(economics, Mapping) else {}
        availability_values = economics_values.get("availability")
        if isinstance(availability_values, Mapping):
            configured = availability_values.get(
                identity.route_name, availability_values.get(option.backend_name)
            )
            if configured in {"available", "unavailable", "rate_limited", "unknown"}:
                availability = str(configured)
        candidate = RouteCandidateInput(
            backend_name=option.backend_name,
            route_name=identity.route_name,
            system_id=identity.system_id,
            harness=f"{identity.harness.harness_id}@{identity.harness.version}",
            model=identity.model_provider.model_id,
            provider=identity.model_provider.provider,
            local=is_local_backend(backend),
            permission_profile=identity.execution.permission_profile,
            availability=availability,  # type: ignore[arg-type]
            qualification_state=assessment.state,
            qualification_level=assessment.selected_level,
            qualification_policy_version=assessment.policy_version,
            qualification_sample_count=assessment.statistics.sample_count,
            conservative_acceptance_probability=assessment.statistics.wilson_lower_bound,
            task_probability_threshold=assessment.task_wilson_threshold,
            false_acceptance_count=assessment.statistics.false_acceptance_count,
            drift_flags=[item.code for item in assessment.statistics.drift_flags],
            capability_score=option.effective_capability_score
            or option.capability_score
            or 0.0,
            execution_cost=execution,
            verification_cost=self._verification_cost_estimate(
                profile, currency=currency
            ),
            human_review_cost=self._review_cost_estimate(profile, currency=currency),
            retry_escalation_cost=retry,
            duration=duration,
            latency_penalty=MoneyEstimate(
                amount=None,
                currency=None,
                accounting_status="unknown",
                source="pending_latency_projection",
            ),
            reserve_satisfied=bool(
                option.reserve_impact.get("reserve_satisfied", False)
            ),
            reserve_evidence=dict(option.reserve_impact),
            input_rejection_reasons=list(option.rejection_reasons),
        )
        return with_latency_penalty(candidate, policy=self.route_policy)

    def _setup_bootstrap_route(
        self,
        assessments: Mapping[str, QualificationAssessment] | None = None,
    ) -> str | None:
        """Return the setup-selected route while it still has no usable evidence.

        Guided setup records an explicit bootstrap policy rather than claiming that
        its selected system is qualified.  The exception is intentionally narrow:
        it applies only to the configured bootstrap default, only while that system
        remains Experimental, and only when no Qualified system is available.
        """

        setup = self.raw_configuration.get("setup")
        setup_values = setup if isinstance(setup, Mapping) else {}
        if setup_values.get("bootstrap_policy") is not True:
            return None
        backend_name = default_bootstrap_backend(self.raw_configuration)
        if not backend_name:
            return None
        if assessments:
            if any(item.state == "qualified" for item in assessments.values()):
                return None
            assessment = assessments.get(backend_name)
            if assessment is None or assessment.state != "experimental":
                return None
        identity = self.agent_system_by_backend.get(backend_name)
        return identity.route_name if identity is not None else backend_name

    def _route_constraints(
        self,
        assessments: Mapping[str, QualificationAssessment] | None = None,
    ) -> RouteConstraints:
        constraints = self.route_policy.constraints
        qualification = self.raw_configuration.get("qualification")
        qualification_values = (
            qualification if isinstance(qualification, Mapping) else {}
        )
        manual = qualification_values.get("manual_override")
        manual_values = manual if isinstance(manual, Mapping) else {}
        manual_route = str(manual_values.get("route_name") or "") or None
        setup_bootstrap_route = self._setup_bootstrap_route(assessments)
        forced = manual_route or constraints.forced_system or setup_bootstrap_route
        return constraints.model_copy(
            update={
                "forced_system": forced,
                "allow_experimental_forced": bool(
                    manual_values.get(
                        "allow_experimental",
                        constraints.allow_experimental_forced
                        or setup_bootstrap_route is not None,
                    )
                ),
                "strongest_only": bool(
                    constraints.strongest_only
                    or self.selection_preference == "strongest_eligible"
                )
                if not forced
                else False,
                "prefer_local": bool(
                    constraints.prefer_local
                    or self.selection_preference == "local_first"
                ),
            }
        )

    def _route_plan(
        self,
        context: PolicyContext,
        alternatives: tuple[BackendOption, ...],
        *,
        selected: BackendOption | None = None,
        sequential_mode: str | None = None,
    ) -> RoutePlan | None:
        assessments = self._repository_qualifications(context)
        if not assessments or not self.agent_system_by_backend:
            return None
        candidates = [
            self._economics_candidate(option, assessments[option.backend_name])
            for option in alternatives
            if option.backend_name in assessments
            and option.backend_name in self.agent_system_by_backend
        ]
        first = next(iter(assessments.values()))
        selected_route = None
        if selected is not None:
            identity = self.agent_system_by_backend.get(selected.backend_name)
            selected_route = (
                identity.route_name if identity is not None else selected.backend_name
            )
        evidence_cutoff = min(item.evaluated_at for item in assessments.values())
        plan_policy = self.route_policy
        if self.selection_preference == "strongest_eligible":
            plan_policy = plan_policy.model_copy(update={"strategy": "strongest_only"})
        elif self.selection_preference == "cheapest_acceptable":
            plan_policy = plan_policy.model_copy(
                update={"strategy": "cheapest_qualified"}
            )
        return plan_route(
            run_id=context.run_id,
            repository_id=first.repository_id,
            repository_head=first.repository_head,
            task_profile=first.task_profile,
            candidates=candidates,
            policy=plan_policy,
            constraints=self._route_constraints(assessments),
            evidence_cutoff=evidence_cutoff,
            reserves={
                "budget_before": {
                    "remaining_attempts": context.budget.remaining_attempts,
                    "remaining_cost_usd": context.budget.remaining_cost_usd,
                    "cost_accounting_status": context.budget.cost_accounting_status,
                    "remaining_wall_time_ms": context.budget.remaining_wall_time_ms,
                    "duration_accounting_status": context.budget.duration_accounting_status,
                },
                "by_system": {
                    item.backend_name: dict(item.reserve_impact)
                    for item in alternatives
                },
            },
            sequential_selection=selected_route,
            sequential_mode=sequential_mode,  # type: ignore[arg-type]
        )

    def _route_plan_choice(
        self,
        alternatives: tuple[BackendOption, ...],
        plan: RoutePlan | None,
    ) -> BackendOption | None:
        if plan is None or plan.selected_first_system is None:
            return None
        for option in alternatives:
            identity = self.agent_system_by_backend.get(option.backend_name)
            if (
                identity is not None
                and identity.route_name == plan.selected_first_system
            ):
                return option if option.eligible else None
        return None

    def _empirical_routing(
        self,
        context: PolicyContext,
        alternatives: tuple[BackendOption, ...],
        *,
        excluded_backends: set[str] | None = None,
    ) -> tuple[tuple[EffectiveCapability, ...], SequenceOptimizationResult]:
        excluded = excluded_backends or set()
        wilson_threshold = (
            self.empirical_configuration.minimum_empirical_wilson_lower_bound
        )
        resolutions: list[EffectiveCapability] = []
        inputs: list[EmpiricalBackendInput] = []
        options = {item.backend_name: item for item in alternatives}
        for backend in sorted(self.backends.values(), key=lambda item: item.name):
            if "coding" not in backend.roles:
                continue
            resolution = resolve_effective_capability(
                backend,
                context.classification,
                self.capability_snapshot,
                self.raw_configuration,
            )
            resolutions.append(resolution)
            option = options.get(backend.name)
            if option is None or not option.eligible or backend.name in excluded:
                continue
            inputs.append(
                EmpiricalBackendInput(
                    backend_name=backend.name,
                    conservative_success_probability=(
                        resolution.conservative_success_probability
                    ),
                    mean_actual_attempt_cost=resolution.mean_actual_attempt_cost,
                    sufficient_probability_data=(
                        resolution.capability_provenance == "qualified_empirical"
                        and resolution.conservative_success_probability is not None
                        and (
                            wilson_threshold is None
                            or resolution.conservative_success_probability
                            >= wilson_threshold
                        )
                    ),
                    profile_version=(
                        resolution.selected_profile_key.scorer_version
                        if resolution.selected_profile_key is not None
                        else None
                    ),
                    profile_digest=resolution.selected_profile_digest,
                    sample_count=resolution.empirical_sample_count,
                    effective_capability_score=(resolution.effective_capability_score),
                    mean_duration_ms=resolution.mean_duration_ms,
                    median_duration_ms=resolution.median_duration_ms,
                    profile_level=resolution.selected_level,
                    task_category_profile=(
                        resolution.selected_profile_key.task_category
                        if resolution.selected_profile_key
                        else None
                    ),
                    difficulty_profile=(
                        resolution.selected_profile_key.difficulty
                        if resolution.selected_profile_key
                        else None
                    ),
                    risk_profile=(
                        resolution.selected_profile_key.risk
                        if resolution.selected_profile_key
                        else None
                    ),
                    execution_environment_profile=backend.execution_environment,
                    probability_source=(
                        "wilson_lower_bound"
                        if resolution.capability_provenance == "qualified_empirical"
                        else "missing"
                    ),
                    cost_source=(
                        "actual_profile_mean"
                        if resolution.mean_actual_attempt_cost is not None
                        else "missing"
                    ),
                    fallback_assumptions=tuple(resolution.missing_inputs),
                )
            )
        cost_budget = (
            context.budget.remaining_cost_usd
            if context.budget.cost_accounting_status == "complete"
            else None
        )
        if cost_budget is not None:
            downstream_fraction = (
                self.configuration.stage_reserves.verification_fraction
                + self.configuration.stage_reserves.final_validation_fraction
                + self.configuration.stage_reserves.selection_fraction
            )
            cost_budget = max(cost_budget * (1.0 - downstream_fraction), 0.0)
        optimization = optimize_sequence(
            inputs,
            max_attempts=context.budget.remaining_attempts,
            known_cost_budget=cost_budget,
            target_success_probability=(
                self.empirical_configuration.target_success_probability
            ),
            persisted_top_n=self.empirical_configuration.persisted_sequence_top_n,
        )
        return tuple(resolutions), optimization

    @staticmethod
    def _optimization_choice(
        alternatives: tuple[BackendOption, ...],
        optimization: SequenceOptimizationResult,
    ) -> BackendOption | None:
        if (
            optimization.optimizer_status != "empirical"
            or not optimization.chosen_sequence
        ):
            return None
        name = optimization.chosen_sequence[0]
        return next(
            (
                item
                for item in alternatives
                if item.backend_name == name and item.eligible
            ),
            None,
        )

    @staticmethod
    def _confidence_rank(option: BackendOption) -> int:
        return {
            "low": 0,
            "medium": 1,
            "operator_override": 2,
            "high": 3,
        }.get(option.capability_confidence, 0)

    @classmethod
    def _effective_rank(cls, option: BackendOption) -> tuple[float, int, int]:
        provenance_rank = {
            "manual": 0,
            "bootstrap": 0,
            "observed": 1,
            "explicit_override": 2,
            "qualified_empirical": 3,
        }.get(option.capability_provenance, 0)
        return (
            float(
                option.effective_capability_score
                if option.effective_capability_score is not None
                else option.capability_score or 0
            ),
            cls._confidence_rank(option),
            provenance_rank,
        )

    @staticmethod
    def _projected_cost_and_duration(
        backend: Backend,
        capability: EffectiveCapability,
    ) -> tuple[float | None, str, float | None, str]:
        configured_cost = estimate_attempt_cost(backend)
        if configured_cost.accounting_status == "complete":
            cost = configured_cost.total
            cost_source = configured_cost.source
        elif capability.median_actual_attempt_cost is not None:
            cost = capability.median_actual_attempt_cost
            cost_source = "empirical_median_actual"
        elif capability.mean_actual_attempt_cost is not None:
            cost = capability.mean_actual_attempt_cost
            cost_source = "empirical_mean_actual"
        else:
            cost = None
            cost_source = configured_cost.source
        if capability.median_duration_ms is not None:
            duration = capability.median_duration_ms
            duration_source = "empirical_median_actual"
        elif capability.mean_duration_ms is not None:
            duration = capability.mean_duration_ms
            duration_source = "empirical_mean_actual"
        elif backend.estimated_duration_seconds is not None:
            duration = backend.estimated_duration_seconds * 1000.0
            duration_source = "configured_estimate"
        else:
            duration = None
            duration_source = "missing"
        return cost, cost_source, duration, duration_source

    def _verification_projection(self) -> tuple[float | None, float | None]:
        verifier = self.raw_configuration.get("verifier")
        values = verifier if isinstance(verifier, Mapping) else {}
        configured_duration = (
            self.configuration.stage_reserves.verification_duration_seconds
        )
        duration = (
            configured_duration * 1000.0 if configured_duration is not None else None
        )
        if bool(values.get("no_llm", True)):
            return 0.0, duration
        backend_name = values.get("backend")
        backend = self.backends.get(str(backend_name))
        if backend is None:
            return None, duration
        estimate = estimate_attempt_cost(backend)
        if duration is None and backend.estimated_duration_seconds is not None:
            duration = backend.estimated_duration_seconds * 1000.0
        return (
            estimate.total if estimate.accounting_status == "complete" else None,
            duration,
        )

    def _stage_projection(
        self,
        context: PolicyContext,
        chosen: BackendOption | None,
        action: str,
        alternatives: tuple[BackendOption, ...],
    ) -> StageBudgetProjection:
        verification_only = (
            action == "retry"
            and chosen is not None
            and chosen.cost_source
            in {"verification_retry", "repository_validation_retry"}
        )
        verification_cost, verification_wall = self._verification_projection()
        action_cost = (
            verification_cost
            if verification_only
            else chosen.estimated_cost_usd
            if chosen
            else 0.0
        )
        action_wall = (
            verification_wall
            if verification_only
            else chosen.estimated_duration_ms
            if chosen
            else 0.0
        )
        action_cost_source = (
            "verification_projection"
            if verification_only
            else chosen.cost_source
            if chosen
            else "none"
        )
        action_duration_source = (
            "verification_projection"
            if verification_only
            else str(chosen.cost_components.get("duration_source") or "missing")
            if chosen
            else "none"
        )
        higher = [
            option
            for option in alternatives
            if chosen is not None
            and bool(
                option.cost_components.get("capability_gate_eligible", option.eligible)
            )
            and option.backend_name != chosen.backend_name
            and self._effective_rank(option) > self._effective_rank(chosen)
        ]
        strongest = max(higher, key=self._effective_rank, default=None)
        final_duration = (
            self.configuration.stage_reserves.final_validation_duration_seconds
        )
        selection_duration = (
            self.configuration.stage_reserves.selection_duration_seconds
        )
        return project_stage_budget(
            budget=context.budget,
            action=action,
            chosen_backend=chosen.backend_name if chosen else None,
            projected_action_cost=action_cost,
            projected_action_wall_time=action_wall,
            action_cost_source=action_cost_source,
            action_duration_source=action_duration_source,
            verification_cost=verification_cost,
            verification_wall_time=verification_wall,
            escalation_cost=strongest.estimated_cost_usd if strongest else 0.0,
            escalation_wall_time=strongest.estimated_duration_ms if strongest else 0.0,
            final_validation_cost=0.0,
            final_validation_wall_time=(
                final_duration * 1000.0 if final_duration is not None else None
            ),
            selection_cost=0.0,
            selection_wall_time=(
                selection_duration * 1000.0 if selection_duration is not None else None
            ),
            configuration=self.configuration.stage_reserves,
            requires_escalation_reserve=strongest is not None,
            missing_inputs=[],
        )

    def _alternatives(
        self, context: PolicyContext, minimum: float
    ) -> tuple[BackendOption, ...]:
        cost_cap_active = context.budget.cost_accounting_status != "not_applicable"
        repository_qualifications = self._repository_qualifications(context)
        qualified_names = {
            name
            for name, assessment in repository_qualifications.items()
            if assessment.state == "qualified"
        }
        qualification_config = self.raw_configuration.get("qualification")
        qualification_values = (
            qualification_config if isinstance(qualification_config, Mapping) else {}
        )
        manual = qualification_values.get("manual_override")
        manual_values = manual if isinstance(manual, Mapping) else {}
        manual_route = str(manual_values.get("route_name") or "") or None
        allow_experimental = manual_values.get("allow_experimental") is True
        setup_bootstrap_route = self._setup_bootstrap_route(repository_qualifications)
        alternatives: list[BackendOption] = []
        for backend in sorted(self.backends.values(), key=lambda item: item.name):
            if "coding" not in backend.roles:
                continue
            capability = resolve_effective_capability(
                backend,
                context.classification,
                self.capability_snapshot,
                self.raw_configuration,
            )
            cost, cost_source, duration, duration_source = (
                self._projected_cost_and_duration(backend, capability)
            )
            reasons: list[str] = []
            bootstrap_default = (
                default_bootstrap_backend(self.raw_configuration) == backend.name
            )
            explicit_override = capability.override_applied
            identity = self.agent_system_by_backend.get(backend.name)
            route_name = identity.route_name if identity is not None else backend.name
            qualification_manual_selected = bool(
                manual_route is not None and manual_route in {backend.name, route_name}
            )
            qualification_bootstrap_selected = bool(
                manual_route is None
                and setup_bootstrap_route is not None
                and setup_bootstrap_route in {backend.name, route_name}
            )
            threshold_met = capability.effective_capability_score >= minimum
            bootstrap_bypass = bool(
                bootstrap_default
                and self.empirical_configuration.allow_bootstrap_threshold_bypass
            )
            manual_hard_blocked = bool(
                minimum >= self.configuration.hard_min_capability
                and capability.capability_provenance == "manual"
                and not self.empirical_configuration.allow_manual_hard_task_qualification
                and not qualification_manual_selected
            )
            if not backend.enabled:
                reasons.append("backend is disabled")
            repository_qualification = repository_qualifications.get(backend.name)
            if repository_qualifications:
                state = (
                    repository_qualification.state
                    if repository_qualification is not None
                    else "unsupported"
                )
                if manual_route is not None:
                    if manual_route not in {backend.name, route_name}:
                        reasons.append(
                            f"manual qualification override is scoped to {manual_route!r}"
                        )
                    elif state == "unsupported":
                        reasons.append(
                            "unsupported repository qualification cannot be overridden"
                        )
                    elif state == "experimental" and not allow_experimental:
                        reasons.append(
                            "experimental system requires --allow-experimental"
                        )
                elif qualification_bootstrap_selected and state == "experimental":
                    # Guided setup may perform a verifier-gated first attempt while
                    # still reporting Experimental.  This creates no qualification.
                    pass
                elif qualified_names:
                    if backend.name not in qualified_names:
                        reasons.append(
                            "automatic routing may choose only repository-qualified systems"
                        )
                elif state != "provisional":
                    reasons.append(
                        "no qualified system exists and this system is not an eligible provisional fallback"
                    )
            if manual_hard_blocked and not explicit_override:
                reasons.append(
                    "manual low-confidence estimate is not hard-task qualification "
                    "without an explicit override"
                )
            if (
                not threshold_met
                and not bootstrap_bypass
                and not explicit_override
                and not qualification_manual_selected
            ):
                reasons.append(
                    f"effective capability {capability.effective_capability_score:g} "
                    f"does not meet required {minimum:g} after uncertainty penalty "
                    f"{capability.uncertainty_penalty:g}"
                )
            if cost_cap_active:
                if context.budget.cost_accounting_status != "complete":
                    reasons.append("remaining cost budget cannot be proven")
                elif cost is None:
                    reasons.append("estimated cost is unknown under an active cost cap")
                elif cost > (context.budget.remaining_cost_usd or 0.0):
                    reasons.append("estimated cost exceeds remaining cost budget")
            alternatives.append(
                BackendOption(
                    backend_name=backend.name,
                    model=backend.model,
                    eligible=not reasons,
                    capability_score=capability.effective_capability_score,
                    estimated_cost_usd=cost,
                    cost_accounting_status=(
                        "complete" if cost is not None else "unknown"
                    ),
                    rejection_reasons=tuple(reasons),
                    cost_components={
                        "configured_capability_score": (
                            capability.configured_capability_score
                        ),
                        "effective_capability_score": (
                            capability.effective_capability_score
                        ),
                        "capability_provenance": capability.capability_provenance,
                        "capability_confidence": capability.capability_confidence,
                        "uncertainty_penalty": capability.uncertainty_penalty,
                        "qualification_status": capability.qualification_status,
                        "threshold_met": threshold_met,
                        "capability_gate_eligible": bool(
                            backend.enabled
                            and not manual_hard_blocked
                            and (
                                threshold_met
                                or bootstrap_bypass
                                or explicit_override
                                or qualification_manual_selected
                            )
                        ),
                        "static_eligible": backend.capability_score >= minimum,
                        "empirical_eligible": (
                            capability.capability_provenance == "qualified_empirical"
                            and threshold_met
                        ),
                        "bootstrap_eligible": bootstrap_bypass,
                        "bootstrap_default": bootstrap_default,
                        "manual_override": explicit_override,
                        "explicit_override": explicit_override,
                        "capability_status": capability_status(
                            backend,
                            self.raw_configuration,
                            self.capability_snapshot,
                        ).value,
                        "capability_score_source": capability.capability_provenance,
                        "empirical_sample_count": capability.empirical_sample_count,
                        "empirical_wilson_lower_bound": (
                            capability.empirical_wilson_lower_bound
                        ),
                        "selected_profile_level": capability.selected_level,
                        "selected_profile_key": (
                            capability.selected_profile_key.model_dump(mode="json")
                            if capability.selected_profile_key
                            else None
                        ),
                        "backoff_evidence": capability.backoff_evidence,
                        "duration_source": duration_source,
                        "repository_qualification": (
                            repository_qualification.model_dump(mode="json")
                            if repository_qualification is not None
                            else None
                        ),
                        "repository_qualification_state": (
                            repository_qualification.state
                            if repository_qualification is not None
                            else None
                        ),
                        "qualification_manual_override": bool(
                            qualification_manual_selected
                        ),
                        "qualification_bootstrap_override": bool(
                            qualification_bootstrap_selected
                        ),
                    },
                    cost_source=cost_source,
                    configured_capability_score=(
                        capability.configured_capability_score
                    ),
                    effective_capability_score=(capability.effective_capability_score),
                    capability_provenance=capability.capability_provenance,
                    capability_confidence=capability.capability_confidence,
                    uncertainty_penalty=capability.uncertainty_penalty,
                    empirical_sample_count=capability.empirical_sample_count,
                    empirical_wilson_lower_bound=(
                        capability.empirical_wilson_lower_bound
                    ),
                    qualification_status=capability.qualification_status,
                    override_applied=capability.override_applied,
                    estimated_duration_ms=duration,
                    duration_accounting_status=(
                        "complete" if duration is not None else "unknown"
                    ),
                )
            )
        preliminary = tuple(alternatives)
        projected: list[BackendOption] = []
        for option in preliminary:
            projection = self._stage_projection(context, option, "attempt", preliminary)
            reasons = list(option.rejection_reasons)
            if not projection.reserve_satisfied:
                reasons.append(
                    "stage reserve for verification, escalation, and final validation cannot be proven"
                )
            projected.append(
                replace(
                    option,
                    eligible=not reasons,
                    rejection_reasons=tuple(reasons),
                    reserve_impact=projection.model_dump(mode="json"),
                )
            )
        if repository_qualifications and not qualified_names and manual_route is None:
            provisional = [
                option
                for option in projected
                if option.eligible
                and option.cost_components.get("repository_qualification_state")
                == "provisional"
            ]
            strongest = max(provisional, key=self._effective_rank, default=None)
            if strongest is not None:
                projected = [
                    option
                    if not option.eligible
                    or option.cost_components.get("repository_qualification_state")
                    != "provisional"
                    or option.backend_name == strongest.backend_name
                    else replace(
                        option,
                        eligible=False,
                        rejection_reasons=(
                            *option.rejection_reasons,
                            "a stronger eligible provisional fallback is configured",
                        ),
                    )
                    for option in projected
                ]
        return tuple(projected)

    @classmethod
    def _cost_order(cls, options: list[BackendOption]) -> BackendOption:
        known = [
            option
            for option in options
            if option.cost_accounting_status == "complete"
            and option.estimated_cost_usd is not None
        ]
        if known:
            return min(
                known,
                key=lambda option: (
                    option.estimated_cost_usd,
                    -cls._effective_rank(option)[0],
                    -cls._effective_rank(option)[1],
                    -cls._effective_rank(option)[2],
                    option.backend_name,
                ),
            )
        return min(
            options,
            key=lambda option: (
                -cls._effective_rank(option)[0],
                -cls._effective_rank(option)[1],
                -cls._effective_rank(option)[2],
                option.backend_name,
            ),
        )

    def _choose(self, options: tuple[BackendOption, ...]) -> BackendOption | None:
        eligible = [option for option in options if option.eligible]
        if not eligible:
            return None
        if self.selection_preference == "strongest_eligible":
            return min(
                eligible,
                key=lambda option: (
                    -self._effective_rank(option)[0],
                    -self._effective_rank(option)[1],
                    -self._effective_rank(option)[2],
                    option.estimated_cost_usd is None,
                    option.estimated_cost_usd
                    if option.estimated_cost_usd is not None
                    else float("inf"),
                    option.backend_name,
                ),
            )
        if self.selection_preference == "local_first":
            local = [
                option
                for option in eligible
                if is_local_backend(self.backends[option.backend_name])
            ]
            eligible = local or eligible
        return self._cost_order(eligible)

    @classmethod
    def _next_higher(
        cls, options: tuple[BackendOption, ...], current: BackendOption
    ) -> BackendOption | None:
        higher = [
            option
            for option in options
            if option.eligible
            and option.backend_name != current.backend_name
            and cls._effective_rank(option) > cls._effective_rank(current)
        ]
        if not higher:
            return None
        return min(
            higher,
            key=lambda option: (
                cls._effective_rank(option),
                option.estimated_cost_usd is None,
                option.estimated_cost_usd
                if option.estimated_cost_usd is not None
                else float("inf"),
                option.backend_name,
            ),
        )

    @staticmethod
    def _budget_projection(
        budget: BudgetContext,
        chosen: BackendOption | None,
        action: str,
        stage_projection: StageBudgetProjection,
    ) -> BudgetContext:
        if action not in {"attempt", "retry", "escalate"} or chosen is None:
            return budget
        verification_only = action == "retry" and chosen.cost_source in {
            "verification_retry",
            "repository_validation_retry",
        }
        return replace(
            budget,
            remaining_attempts=(
                budget.remaining_attempts
                if verification_only
                else max(budget.remaining_attempts - 1, 0)
            ),
            remaining_cost_usd=(
                stage_projection.budget_after_action.remaining_cost_usd
            ),
            cost_accounting_status=(
                stage_projection.budget_after_action.cost_accounting_status
            ),
            remaining_wall_time_ms=(
                int(stage_projection.budget_after_action.remaining_wall_time_ms)
                if stage_projection.budget_after_action.remaining_wall_time_ms
                is not None
                else None
            ),
            duration_accounting_status=(
                stage_projection.budget_after_action.duration_accounting_status
            ),
        )

    @staticmethod
    def _attempt_progress(attempt: AttemptSummary) -> AttemptProgressAssessment:
        if not attempt.progress_assessment:
            return empty_progress_assessment()
        try:
            return AttemptProgressAssessment.model_validate(
                dict(attempt.progress_assessment)
            )
        except (TypeError, ValueError):
            return empty_progress_assessment("progress_evidence_malformed")

    def _implementation_retry_gate(
        self,
        context: PolicyContext,
        alternatives: tuple[BackendOption, ...],
        previous_attempt: AttemptSummary,
        previous: BackendOption | None,
        *,
        actionable_correction: bool,
    ) -> tuple[bool, str, tuple[str, ...], StageBudgetProjection]:
        """Apply every same-backend coding retry gate in one deterministic place."""

        assessment = self._attempt_progress(previous_attempt)
        projection = self._stage_projection(context, previous, "retry", alternatives)
        blockers: list[str] = []
        same_backend_attempts = sum(
            item.backend_name == previous_attempt.backend_name
            for item in context.attempts
        )
        if self.configuration.max_same_backend_retries < 1 or same_backend_attempts > 1:
            blockers.append("same_backend_retry_already_used")
        if previous is None or not previous.eligible:
            blockers.append("backend_no_longer_eligible")
        if not assessment.credible_progress:
            blockers.append("no_credible_progress")
        if not actionable_correction:
            blockers.append("actionable_correction_missing")
        if assessment.candidate_empty:
            blockers.append("empty_patch")
        if assessment.candidate_quality_status == "ineligible":
            blockers.append("candidate_ineligible")
        if assessment.irrelevant_patch_dominated or (
            assessment.relevant_patch_present
            and assessment.relevant_diff_ratio
            < self.configuration.minimum_relevant_diff_ratio
        ):
            blockers.append("irrelevant_patch_dominated")
        if (
            assessment.high_failure_repetition
            or assessment.repeated_failure_ratio
            >= self.configuration.maximum_repeated_failure_ratio
            > 0
        ):
            blockers.append("high_failure_repetition")
        if not projection.reserve_satisfied:
            blockers.append("budget_reserve_required")

        if not blockers:
            return (
                True,
                "retry_credible_local_progress",
                (),
                projection,
            )
        if "empty_patch" in blockers:
            reason_code = "escalate_empty_patch"
        elif (
            "candidate_ineligible" in blockers
            or "irrelevant_patch_dominated" in blockers
        ):
            reason_code = "escalate_candidate_ineligible"
        elif "high_failure_repetition" in blockers:
            reason_code = "escalate_high_failure_repetition"
        elif "budget_reserve_required" in blockers:
            reason_code = "escalate_budget_reserve_required"
        else:
            reason_code = "escalate_no_credible_progress"
        return False, reason_code, tuple(blockers), projection

    def _decision(
        self,
        context: PolicyContext,
        alternatives: tuple[BackendOption, ...],
        minimum: float,
        rule: str,
        *,
        action: str,
        reason: str,
        chosen: BackendOption | None = None,
        repeats: bool = False,
        escalates: bool = False,
        metadata: Mapping[str, Any] | None = None,
        empirical_resolutions: tuple[EffectiveCapability, ...] | None = None,
        optimization: SequenceOptimizationResult | None = None,
        route_plan: RoutePlan | None = None,
    ) -> PolicyDecision:
        if empirical_resolutions is None or optimization is None:
            empirical_resolutions, optimization = self._empirical_routing(
                context, alternatives
            )
        stage_projection = self._stage_projection(context, chosen, action, alternatives)
        projection = self._budget_projection(
            context.budget, chosen, action, stage_projection
        )
        alternative_costs = {
            item.backend_name: {
                "components": dict(item.cost_components),
                "source": item.cost_source,
                "estimated_cost_usd": item.estimated_cost_usd,
                "estimated_duration_ms": item.estimated_duration_ms,
                "duration_accounting_status": item.duration_accounting_status,
                "reserve_impact": dict(item.reserve_impact),
            }
            for item in alternatives
        }
        next_higher = (
            self._next_higher(alternatives, chosen) if chosen is not None else None
        )
        if route_plan is None:
            route_plan = self._route_plan(
                context,
                alternatives,
                selected=chosen,
                sequential_mode=(
                    "sequential_retry"
                    if repeats
                    else "sequential_escalation"
                    if escalates
                    else None
                ),
            )
        details = {
            "required_capability_score": minimum,
            "required_capability_rule": rule,
            "repeats_prior_backend": repeats,
            "escalates_from_prior_backend": escalates,
            "alternative_costs": alternative_costs,
            "capability_scores": {
                item.backend_name: item.model_dump(mode="json")
                for item in empirical_resolutions
            },
            "eligibility_by_backend": {
                item.backend_name: {
                    **{
                        key: item.cost_components.get(key)
                        for key in (
                            "static_eligible",
                            "empirical_eligible",
                            "bootstrap_eligible",
                            "bootstrap_default",
                            "manual_override",
                            "capability_status",
                            "capability_score_source",
                            "configured_capability_score",
                            "effective_capability_score",
                            "capability_provenance",
                            "capability_confidence",
                            "uncertainty_penalty",
                            "qualification_status",
                            "empirical_sample_count",
                            "empirical_wilson_lower_bound",
                            "selected_profile_level",
                            "explicit_override",
                        )
                    },
                    "eligible": item.eligible,
                    "rejection_reasons": list(item.rejection_reasons),
                    "estimated_cost_usd": item.estimated_cost_usd,
                    "estimated_duration_ms": item.estimated_duration_ms,
                    "reserve_impact": dict(item.reserve_impact),
                }
                for item in alternatives
            },
            "capability_snapshot": (
                {
                    "schema_version": self.capability_snapshot.schema_version,
                    "scorer_version": self.capability_snapshot.scorer_version,
                    "source_data_digest": self.capability_snapshot.source_data_digest,
                    "profile_digest": self.capability_snapshot.profile_digest,
                }
                if self.capability_snapshot is not None
                else None
            ),
            "empirical_optimizer": optimization.model_dump(mode="json"),
            "policy_path_used": (
                route_plan.policy_version
                if route_plan is not None
                else optimization.optimizer_version
                if optimization.optimizer_status == "empirical"
                else "bootstrap_fallback"
            ),
            "route_plan": (
                route_plan.model_dump(mode="json") if route_plan is not None else None
            ),
            "stage_budget_projection": stage_projection.model_dump(mode="json"),
            "credible_progress_assessment": (
                dict(context.attempts[-1].progress_assessment)
                if context.attempts
                else None
            ),
            "next_higher_backend": (
                next_higher.backend_name if next_higher is not None else None
            ),
            "retry_allowed": None,
            "retry_blockers": [],
            "policy_reason_code": None,
            "override_status": bool(chosen and chosen.override_applied),
            "public_policy": {
                "preset": self.public_preset,
                "version": PUBLIC_POLICY_VERSION,
                "selection_preference": self.selection_preference,
            },
            "budget_consumption": {
                "actual_attempts_used": context.budget.actual_attempts_used,
                "actual_known_cost_usd": context.budget.actual_cost_consumed_usd,
                "actual_cost_accounting_status": context.budget.actual_cost_accounting_status,
                "actual_wall_time_ms": context.budget.actual_wall_time_ms,
                "proposed_estimated_cost_usd": (
                    chosen.estimated_cost_usd if chosen is not None else None
                ),
            },
            **dict(metadata or {}),
        }
        if chosen is not None:
            selected_resolution = next(
                (
                    item
                    for item in empirical_resolutions
                    if item.backend_name == chosen.backend_name
                ),
                None,
            )
            repository_qualification = chosen.cost_components.get(
                "repository_qualification"
            )
            qualified_route = bool(
                isinstance(repository_qualification, Mapping)
                and repository_qualification.get("state") == "qualified"
            )
            constraint_override = bool(details.get("constraint_violation", False))
            basis = (
                "explicit_override"
                if constraint_override
                else "repository_accepted_change_economics"
                if route_plan is not None
                else chosen.capability_provenance
            )
            details["override_status"] = bool(
                chosen.override_applied or constraint_override
            )
            details["route_provenance"] = {
                "basis": basis,
                "bootstrap_default": chosen.cost_components.get(
                    "bootstrap_default", False
                ),
                "manual_override": chosen.cost_components.get("manual_override", False),
                "explicit_override": bool(
                    chosen.override_applied or constraint_override
                ),
                "constraint_override": constraint_override,
                "capability_status": chosen.cost_components.get("capability_status"),
                "observed_sample_count": (
                    selected_resolution.empirical_sample_count
                    if selected_resolution is not None
                    else 0
                ),
                "empirical_evidence_used": bool(
                    qualified_route
                    or (
                        selected_resolution is not None
                        and selected_resolution.capability_provenance
                        == "qualified_empirical"
                    )
                ),
                "policy_version": (
                    route_plan.policy_version
                    if route_plan is not None
                    else PUBLIC_POLICY_VERSION
                ),
                "route_plan_id": route_plan.plan_id if route_plan is not None else None,
                "repository_qualification_state": (
                    repository_qualification.get("state")
                    if isinstance(repository_qualification, Mapping)
                    else None
                ),
                "conservative_acceptance_probability": (
                    repository_qualification.get("statistics", {}).get(
                        "wilson_lower_bound"
                    )
                    if isinstance(repository_qualification, Mapping)
                    and isinstance(repository_qualification.get("statistics"), Mapping)
                    else None
                ),
                "configured_capability_score": chosen.configured_capability_score,
                "effective_capability_score": chosen.effective_capability_score,
                "uncertainty_penalty": chosen.uncertainty_penalty,
                "capability_confidence": chosen.capability_confidence,
            }
        else:
            details["route_provenance"] = None
        return PolicyDecision(
            action=action,  # type: ignore[arg-type]
            reason=reason,
            considered_backends=alternatives,
            chosen_backend=chosen.backend_name if chosen else None,
            chosen_model=chosen.model if chosen else None,
            policy_version=(
                route_plan.policy_version
                if route_plan is not None
                else optimization.optimizer_version
                if optimization.optimizer_status == "empirical"
                else "bootstrap_v1"
            ),
            classification_reference=context.classification.classification_id,
            required_capability_score=minimum,
            required_capability_rule=rule,
            repeats_prior_backend=repeats,
            escalates_from_prior_backend=escalates,
            budget_before=context.budget,
            budget_projection_after=projection,
            metadata=details,
        )

    def decide(self, context: PolicyContext) -> PolicyDecision:
        minimum, rule = required_capability(context, self.configuration)
        alternatives = self._alternatives(context, minimum)
        empirical_resolutions, optimization = self._empirical_routing(
            context, alternatives
        )

        if (
            len(context.eligible_candidate_ids)
            >= self.configuration.accepted_candidates_required
        ):
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="select",
                reason="Required acceptance-eligible candidate count has been reached.",
            )
        latest_verification = (
            context.verifications[-1] if context.verifications else None
        )
        previous_attempt = context.attempts[-1] if context.attempts else None
        if (
            previous_attempt is not None
            and latest_verification is not None
            and latest_verification.attempt_id == previous_attempt.attempt_id
            and latest_verification.repository_validation_status
            == "infrastructure_error"
        ):
            previous = next(
                (
                    item
                    for item in alternatives
                    if item.backend_name == previous_attempt.backend_name
                ),
                None,
            )
            if (
                latest_verification.repository_validation_retry_count
                < self.configuration.repository_validation_retry_limit
                and previous is not None
            ):
                validation_option = replace(
                    previous, cost_source="repository_validation_retry"
                )
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="retry",
                    reason=(
                        "Retrying repository validation against the preserved "
                        "candidate without rerunning coding."
                    ),
                    chosen=validation_option,
                    repeats=True,
                    metadata={"retry_scope": "repository_validation"},
                )
            higher = (
                self._next_higher(alternatives, previous)
                if previous is not None
                else None
            )
            if higher is not None:
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="escalate",
                    reason=(
                        "Repository validation still has environment confusion after "
                        "its candidate-only infrastructure retry; coding is not "
                        "repeated on the same backend and routing escalates."
                    ),
                    chosen=higher,
                    escalates=True,
                    metadata={
                        "policy_reason_code": (
                            "escalate_validation_environment_confusion"
                        ),
                        "retry_allowed": False,
                        "retry_blockers": [
                            "repository_validation_infrastructure_retry_exhausted"
                        ],
                    },
                )
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="fail",
                reason=(
                    "Repository validation remained unavailable after its "
                    "infrastructure retry and no stronger backend remains."
                ),
                metadata={
                    "policy_reason_code": ("escalate_validation_environment_confusion"),
                    "retry_allowed": False,
                },
            )
        if context.budget.remaining_attempts <= 0:
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="exhaust",
                reason="Attempt budget exhausted.",
            )
        if (
            context.budget.remaining_wall_time_ms is not None
            and context.budget.remaining_wall_time_ms <= 0
        ):
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="exhaust",
                reason="Wall-time budget exhausted.",
            )
        if (
            context.budget.cost_accounting_status == "complete"
            and context.budget.remaining_cost_usd is not None
            and context.budget.remaining_cost_usd <= 0
            and context.budget.actual_attempts_used > 0
        ):
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="exhaust",
                reason="Cost budget exhausted.",
            )

        economics_plan = self._route_plan(context, alternatives)
        chosen = (
            self._route_plan_choice(alternatives, economics_plan)
            if economics_plan is not None
            else self._optimization_choice(alternatives, optimization)
            if self.selection_preference
            in {"balanced", "custom", "accepted_change_optimizer"}
            else None
        )
        empirical_budget_blocked = bool(
            optimization.optimizer_status == "empirical"
            and not optimization.chosen_sequence
        )
        if economics_plan is None and (
            optimization.optimizer_status != "empirical" or chosen is None
        ):
            chosen = self._choose(alternatives)
        violation = False
        if (
            chosen is None
            and economics_plan is None
            and not empirical_budget_blocked
            and self.configuration.allow_constraint_violations
        ):
            feasible = [
                option
                for option in alternatives
                if "backend is disabled" not in option.rejection_reasons
                and not any(
                    marker in reason
                    for reason in option.rejection_reasons
                    for marker in ("cost", "wall-time", "stage reserve")
                )
            ]
            if feasible:
                strongest = min(
                    feasible,
                    key=lambda option: (
                        -self._effective_rank(option)[0],
                        -self._effective_rank(option)[1],
                        -self._effective_rank(option)[2],
                        option.estimated_cost_usd is None,
                        option.estimated_cost_usd
                        if option.estimated_cost_usd is not None
                        else float("inf"),
                        option.backend_name,
                    ),
                )
                chosen = replace(
                    strongest,
                    eligible=True,
                    rejection_reasons=(
                        *strongest.rejection_reasons,
                        "capability constraint violated by explicit policy configuration",
                    ),
                )
                alternatives = tuple(
                    chosen if item.backend_name == chosen.backend_name else item
                    for item in alternatives
                )
                violation = True
        if chosen is None:
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="exhaust",
                reason=(
                    "No empirical backend sequence fits the remaining known cost budget."
                    if empirical_budget_blocked
                    else "No configured coding backend is eligible under capability and budget constraints."
                ),
                empirical_resolutions=empirical_resolutions,
                optimization=optimization,
                route_plan=economics_plan,
            )

        if not context.attempts:
            reason = "Selected the least expensive known-cost sufficient backend."
            if economics_plan is not None:
                reason = economics_plan.explanation
            if chosen.estimated_cost_usd is None and economics_plan is None:
                reason = (
                    "All eligible cost estimates are unknown; conservatively selected "
                    "the strongest effective capability."
                )
            if violation:
                reason = "No backend met the threshold; selected the strongest backend under an explicit constraint violation."
            elif (
                economics_plan is None and optimization.optimizer_status == "empirical"
            ):
                reason = (
                    "Selected the first backend in the lowest-expected-cost empirical "
                    "sequence under the configured target probability."
                )
            if self.selection_preference == "strongest_eligible":
                reason = "Reliable preset selected the strongest eligible route."
            elif self.selection_preference == "local_first" and is_local_backend(
                self.backends[chosen.backend_name]
            ):
                reason = "Local first selected an eligible local route."
            elif self.selection_preference == "cheapest_acceptable":
                reason = (
                    "Cheapest acceptable selected the lowest known-cost eligible route."
                )
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="attempt",
                reason=reason,
                chosen=chosen,
                metadata={"constraint_violation": violation},
                empirical_resolutions=empirical_resolutions,
                optimization=optimization,
                route_plan=economics_plan,
            )

        previous_attempt = context.attempts[-1]
        previous = next(
            (
                item
                for item in alternatives
                if item.backend_name == previous_attempt.backend_name
            ),
            None,
        )
        failure = previous_attempt.failure_category
        latest_verification = (
            context.verifications[-1] if context.verifications else None
        )
        if (
            latest_verification
            and latest_verification.attempt_id == previous_attempt.attempt_id
        ):
            failure = latest_verification.failure_category or failure
        attempted_backend_names = {item.backend_name for item in context.attempts}
        remaining_resolutions, remaining_optimization = self._empirical_routing(
            context,
            alternatives,
            excluded_backends=attempted_backend_names,
        )
        empirical_remaining = self._optimization_choice(
            alternatives, remaining_optimization
        )

        def remaining_or_bootstrap(
            bootstrap: BackendOption | None,
        ) -> tuple[
            BackendOption | None,
            tuple[EffectiveCapability, ...],
            SequenceOptimizationResult,
        ]:
            if (
                remaining_optimization.optimizer_status == "empirical"
                and empirical_remaining is not None
                and (
                    previous is None
                    or self._effective_rank(empirical_remaining)
                    > self._effective_rank(previous)
                )
            ):
                return (
                    empirical_remaining,
                    remaining_resolutions,
                    remaining_optimization,
                )
            return bootstrap, remaining_resolutions, remaining_optimization

        if failure == "materialization_failure":
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="fail",
                reason="Materialization failure is terminal.",
            )
        if failure == "verification_failure":
            retries = (
                latest_verification.verifier_retry_count if latest_verification else 0
            )
            if (
                retries < self.configuration.verifier_retry_limit
                and previous is not None
            ):
                verification_option = replace(
                    previous, cost_source="verification_retry"
                )
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="retry",
                    reason="Retrying verification once without rerunning the coding attempt.",
                    chosen=verification_option,
                    repeats=True,
                    metadata={"retry_scope": "verification"},
                )
            higher, route_scores, route_optimization = remaining_or_bootstrap(
                self._next_higher(alternatives, previous) if previous else None
            )
            if higher is not None:
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="escalate",
                    reason="Verification remained ineligible after its retry; continuing with the next backend in the selected routing path.",
                    chosen=higher,
                    escalates=True,
                    empirical_resolutions=route_scores,
                    optimization=route_optimization,
                )
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="exhaust",
                reason="Verification failed twice and no further eligible backend exists.",
            )

        if failure == "infrastructure_failure":
            others = tuple(
                item
                for item in alternatives
                if item.backend_name != previous_attempt.backend_name
            )
            replacement, route_scores, route_optimization = remaining_or_bootstrap(
                self._next_higher(alternatives, previous)
                if previous is not None
                else self._choose(others)
            )
            if replacement is not None:
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="escalate",
                    reason=(
                        "The coding environment failed without credible candidate "
                        "progress, so the prior backend is not retried and routing "
                        "continues with a stronger eligible backend."
                    ),
                    chosen=replacement,
                    escalates=True,
                    metadata={
                        "prior_backend_failed": True,
                        "policy_reason_code": "escalate_no_credible_progress",
                        "retry_allowed": False,
                        "retry_blockers": ["infrastructure_failure"],
                    },
                    empirical_resolutions=route_scores,
                    optimization=route_optimization,
                )
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="fail",
                reason=(
                    "The coding environment failed and no stronger eligible backend "
                    "is available."
                ),
                metadata={
                    "policy_reason_code": "escalate_no_credible_progress",
                    "retry_allowed": False,
                    "retry_blockers": ["infrastructure_failure"],
                },
            )
        if failure == "implementation_failure":
            assessment = self._attempt_progress(previous_attempt)
            actionable = bool(
                assessment.actionable_feedback
                or (
                    latest_verification is not None
                    and latest_verification.attempt_id == previous_attempt.attempt_id
                    and latest_verification.actionable_correction
                )
            )
            immediate_capability_failure = bool(
                latest_verification is not None
                and latest_verification.attempt_id == previous_attempt.attempt_id
                and (
                    latest_verification.major_regression
                    or latest_verification.incorrect_task_interpretation
                )
            )
            retry_allowed, reason_code, retry_blockers, _retry_projection = (
                self._implementation_retry_gate(
                    context,
                    alternatives,
                    previous_attempt,
                    previous,
                    actionable_correction=actionable,
                )
            )
            if immediate_capability_failure:
                retry_allowed = False
                reason_code = "escalate_capability_failure"
                retry_blockers = (*retry_blockers, "capability_failure_evidence")
            if retry_allowed and previous is not None:
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="retry",
                    reason=(
                        "The candidate has a relevant narrow patch, measurable "
                        "credible progress, and an actionable local correction; "
                        "retrying the same backend while preserving downstream reserves."
                    ),
                    chosen=previous,
                    repeats=True,
                    metadata={
                        "policy_reason_code": "retry_credible_local_progress",
                        "retry_allowed": True,
                        "retry_blockers": [],
                    },
                )
            higher, route_scores, route_optimization = remaining_or_bootstrap(
                self._next_higher(alternatives, previous) if previous else None
            )
            if higher is not None:
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="escalate",
                    reason=(
                        "The prior backend was not retried because the candidate did "
                        f"not satisfy every credible-progress retry gate ({', '.join(retry_blockers)}); "
                        "routing continues with the next stronger eligible backend."
                    ),
                    chosen=higher,
                    escalates=True,
                    metadata={
                        "policy_reason_code": reason_code,
                        "retry_allowed": False,
                        "retry_blockers": list(retry_blockers),
                    },
                    empirical_resolutions=route_scores,
                    optimization=route_optimization,
                )
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="exhaust",
                reason=(
                    "The implementation did not satisfy every same-backend retry "
                    "gate and no stronger eligible backend remains."
                ),
                metadata={
                    "policy_reason_code": reason_code,
                    "retry_allowed": False,
                    "retry_blockers": list(retry_blockers),
                },
            )
        if failure == "capability_failure":
            higher, route_scores, route_optimization = remaining_or_bootstrap(
                self._next_higher(alternatives, previous) if previous else None
            )
            if higher is not None:
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="escalate",
                    reason=(
                        "Verifier evidence indicates a capability failure; escalating "
                        "immediately to the next stronger effective capability."
                    ),
                    chosen=higher,
                    escalates=True,
                    metadata={
                        "policy_reason_code": "escalate_capability_failure",
                        "retry_allowed": False,
                        "retry_blockers": ["capability_failure"],
                    },
                    empirical_resolutions=route_scores,
                    optimization=route_optimization,
                )
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="exhaust",
                reason="Capability failure was not retried and no stronger backend remains.",
                metadata={
                    "policy_reason_code": "escalate_capability_failure",
                    "retry_allowed": False,
                    "retry_blockers": ["capability_failure"],
                },
            )
        if failure == "no_change_failure":
            higher, route_scores, route_optimization = remaining_or_bootstrap(
                self._next_higher(alternatives, previous) if previous else None
            )
            if higher is not None:
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="escalate",
                    reason=(
                        "The candidate patch is empty, so the same backend is not "
                        "retried and routing escalates immediately."
                    ),
                    chosen=higher,
                    escalates=True,
                    metadata={
                        "policy_reason_code": "escalate_empty_patch",
                        "retry_allowed": False,
                        "retry_blockers": ["empty_patch"],
                    },
                    empirical_resolutions=route_scores,
                    optimization=route_optimization,
                )
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="exhaust",
                reason="The candidate patch is empty and no stronger backend remains.",
                metadata={
                    "policy_reason_code": "escalate_empty_patch",
                    "retry_allowed": False,
                    "retry_blockers": ["empty_patch"],
                },
            )

        return self._decision(
            context,
            alternatives,
            minimum,
            rule,
            action="exhaust",
            reason="No eligible retry or escalation remains.",
        )
