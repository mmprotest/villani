"""Versioned deterministic bootstrap policy for the canonical closed loop."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from villani_ops.core.backend import Backend

from .capabilities.models import (
    CapabilitySnapshot,
    EmpiricalBackendInput,
    EmpiricalScoreResolution,
    SequenceOptimizationResult,
)
from .capabilities.optimizer import optimize_sequence
from .capabilities.report import profile_key_for
from .capabilities.scoring import resolve_empirical_score
from .costs import estimate_attempt_cost
from .interfaces import BackendOption, BudgetContext, PolicyContext, PolicyDecision
from .model_management import (
    capability_status,
    default_bootstrap_backend,
    is_local_backend,
    manual_override,
    route_basis,
)
from .policy_presets import (
    PUBLIC_POLICY_VERSION,
    configured_policy_preset,
    selection_preference,
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
    accepted_candidates_required: int = Field(default=1, ge=1)
    allow_constraint_violations: bool = False
    allow_no_change_retry: bool = False


class EmpiricalCapabilityConfiguration(BaseModel):
    model_config = ConfigDict(extra="ignore")

    minimum_empirical_samples: int = Field(default=20, ge=1)
    target_success_probability: float = Field(default=0.80, ge=0, le=1)
    # By default the routing confidence target is also the minimum Wilson
    # lower bound for empirical qualification. Operators can set a lower
    # explicit bound when empirical evidence should qualify earlier.
    minimum_empirical_wilson_lower_bound: float | None = Field(default=None, ge=0, le=1)
    persisted_sequence_top_n: int = Field(default=100, ge=1)
    classifier_version: str = Field(default="task_classifier_v1", min_length=1)
    verifier_version: str = Field(
        default="villani_ops_verifier_pipeline_v1", min_length=1
    )
    scorer_version: str = Field(default="empirical_wilson_v1", min_length=1)


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

    def _empirical_routing(
        self,
        context: PolicyContext,
        alternatives: tuple[BackendOption, ...],
        *,
        excluded_backends: set[str] | None = None,
    ) -> tuple[tuple[EmpiricalScoreResolution, ...], SequenceOptimizationResult]:
        excluded = excluded_backends or set()
        wilson_threshold = (
            self.empirical_configuration.minimum_empirical_wilson_lower_bound
            if self.empirical_configuration.minimum_empirical_wilson_lower_bound
            is not None
            else self.empirical_configuration.target_success_probability
        )
        resolutions: list[EmpiricalScoreResolution] = []
        inputs: list[EmpiricalBackendInput] = []
        options = {item.backend_name: item for item in alternatives}
        for backend in sorted(self.backends.values(), key=lambda item: item.name):
            if "coding" not in backend.roles:
                continue
            resolution = resolve_empirical_score(
                self.capability_snapshot,
                profile_key_for(
                    backend, context.classification, self.raw_configuration
                ),
                static_capability_score=backend.capability_score,
                minimum_empirical_samples=(
                    self.empirical_configuration.minimum_empirical_samples
                ),
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
                        resolution.empirical_status == "sufficient_data"
                        and resolution.conservative_success_probability is not None
                        and resolution.conservative_success_probability
                        >= wilson_threshold
                    ),
                    profile_version=(
                        resolution.selected_profile_key.scorer_version
                        if resolution.selected_profile_key is not None
                        else None
                    ),
                    profile_digest=resolution.selected_profile_digest,
                    sample_count=resolution.selected_sample_count,
                )
            )
        cost_budget = (
            context.budget.remaining_cost_usd
            if context.budget.cost_accounting_status == "complete"
            else None
        )
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

    def _alternatives(
        self, context: PolicyContext, minimum: float
    ) -> tuple[BackendOption, ...]:
        cost_cap_active = context.budget.cost_accounting_status != "not_applicable"
        wilson_threshold = (
            self.empirical_configuration.minimum_empirical_wilson_lower_bound
            if self.empirical_configuration.minimum_empirical_wilson_lower_bound
            is not None
            else self.empirical_configuration.target_success_probability
        )
        alternatives: list[BackendOption] = []
        for backend in sorted(self.backends.values(), key=lambda item: item.name):
            if "coding" not in backend.roles:
                continue
            estimate = estimate_attempt_cost(backend)
            empirical = resolve_empirical_score(
                self.capability_snapshot,
                profile_key_for(
                    backend, context.classification, self.raw_configuration
                ),
                static_capability_score=backend.capability_score,
                minimum_empirical_samples=self.empirical_configuration.minimum_empirical_samples,
            )
            static_eligible = backend.capability_score >= minimum
            empirical_eligible = bool(
                empirical.empirical_status == "sufficient_data"
                and empirical.capability_score_used >= minimum
                and (
                    empirical.conservative_success_probability is not None
                    and empirical.conservative_success_probability >= wilson_threshold
                )
            )
            reasons: list[str] = []
            bootstrap_default = (
                default_bootstrap_backend(self.raw_configuration) == backend.name
            )
            bootstrap_eligible = bool(bootstrap_default and backend.enabled)
            if not backend.enabled:
                reasons.append("backend is disabled")
            if not static_eligible and not empirical_eligible and not bootstrap_eligible:
                reasons.append(
                    f"static capability {backend.capability_score} and empirical qualification "
                    f"do not meet required {minimum:g}"
                )
            if cost_cap_active:
                if context.budget.cost_accounting_status != "complete":
                    reasons.append("remaining cost budget cannot be proven")
                elif estimate.accounting_status != "complete" or estimate.total is None:
                    reasons.append("estimated cost is unknown under an active cost cap")
                elif estimate.total > (context.budget.remaining_cost_usd or 0.0):
                    reasons.append("estimated cost exceeds remaining cost budget")
            alternatives.append(
                BackendOption(
                    backend_name=backend.name,
                    model=backend.model,
                    eligible=not reasons,
                    # Keep the configured score stable for deterministic
                    # ordering/reporting.  Empirical qualification is an
                    # additional eligibility signal, persisted below with its
                    # effective Wilson-derived score.
                    capability_score=float(backend.capability_score),
                    estimated_cost_usd=estimate.total,
                    cost_accounting_status=estimate.accounting_status,
                    rejection_reasons=tuple(reasons),
                    cost_components={
                        **estimate.as_dict(),
                        "static_eligible": static_eligible,
                        "empirical_eligible": empirical_eligible,
                        "bootstrap_eligible": bootstrap_eligible,
                        "bootstrap_default": bootstrap_default,
                        "manual_override": manual_override(backend),
                        "capability_status": capability_status(
                            backend,
                            self.raw_configuration,
                            self.capability_snapshot,
                        ).value,
                        "capability_score_source": empirical.score_source,
                        "effective_capability_score": empirical.capability_score_used,
                        "minimum_wilson_lower_bound": wilson_threshold,
                        "empirical_sample_count": empirical.selected_sample_count,
                        "empirical_wilson_lower_bound": empirical.conservative_success_probability,
                    },
                    cost_source=estimate.source,
                )
            )
        return tuple(alternatives)

    @staticmethod
    def _cost_order(options: list[BackendOption]) -> BackendOption:
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
                    -(option.capability_score or 0),
                    option.backend_name,
                ),
            )
        return min(
            options,
            key=lambda option: (
                option.capability_score
                if option.capability_score is not None
                else float("inf"),
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
                    -float(
                        option.cost_components.get("effective_capability_score")
                        or option.capability_score
                        or 0
                    ),
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
        if (
            self.public_policy_enabled
            and self.selection_preference in {"balanced", "local_first"}
        ):
            default = default_bootstrap_backend(self.raw_configuration)
            selected_default = next(
                (
                    option
                    for option in eligible
                    if option.backend_name == default
                    and option.cost_components.get("bootstrap_eligible") is True
                ),
                None,
            )
            if selected_default is not None:
                return selected_default
        return self._cost_order(eligible)

    @staticmethod
    def _next_higher(
        options: tuple[BackendOption, ...], current: BackendOption
    ) -> BackendOption | None:
        higher = [
            option
            for option in options
            if option.eligible
            and option.backend_name != current.backend_name
            and (option.capability_score or 0) > (current.capability_score or 0)
        ]
        if not higher:
            return None
        return min(
            higher,
            key=lambda option: (
                option.capability_score or 0,
                option.estimated_cost_usd is None,
                option.estimated_cost_usd
                if option.estimated_cost_usd is not None
                else float("inf"),
                option.backend_name,
            ),
        )

    @staticmethod
    def _budget_projection(
        budget: BudgetContext, chosen: BackendOption | None, action: str
    ) -> BudgetContext:
        if action not in {"attempt", "retry", "escalate"} or chosen is None:
            return budget
        verification_only = (
            action == "retry" and chosen.cost_source == "verification_retry"
        )
        if verification_only:
            return budget
        remaining_cost = budget.remaining_cost_usd
        cost_status = budget.cost_accounting_status
        if cost_status == "complete":
            if chosen.estimated_cost_usd is None:
                remaining_cost = None
                cost_status = "unknown"
            else:
                remaining_cost = max(
                    (remaining_cost or 0.0) - chosen.estimated_cost_usd, 0.0
                )
        return replace(
            budget,
            remaining_attempts=max(budget.remaining_attempts - 1, 0),
            remaining_cost_usd=remaining_cost,
            cost_accounting_status=cost_status,
        )

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
        empirical_resolutions: tuple[EmpiricalScoreResolution, ...] | None = None,
        optimization: SequenceOptimizationResult | None = None,
    ) -> PolicyDecision:
        if empirical_resolutions is None or optimization is None:
            empirical_resolutions, optimization = self._empirical_routing(
                context, alternatives
            )
        projection = self._budget_projection(context.budget, chosen, action)
        alternative_costs = {
            item.backend_name: {
                "components": dict(item.cost_components),
                "source": item.cost_source,
            }
            for item in alternatives
        }
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
                    key: item.cost_components.get(key)
                    for key in (
                        "static_eligible",
                        "empirical_eligible",
                        "bootstrap_eligible",
                        "bootstrap_default",
                        "manual_override",
                        "capability_status",
                        "capability_score_source",
                        "effective_capability_score",
                        "minimum_wilson_lower_bound",
                        "empirical_sample_count",
                        "empirical_wilson_lower_bound",
                    )
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
                "empirical_sequence_v1"
                if optimization.optimizer_status == "empirical"
                else "bootstrap_v1"
            ),
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
            backend = self.backends[chosen.backend_name]
            bootstrap_default_route = bool(
                self.public_policy_enabled
                and self.selection_preference in {"balanced", "local_first"}
                and optimization.optimizer_status != "empirical"
                and chosen.cost_components.get("bootstrap_default")
                and chosen.cost_components.get("bootstrap_eligible")
            )
            qualified_route = bool(
                chosen.cost_components.get("empirical_eligible")
                and not bootstrap_default_route
            )
            basis = route_basis(
                backend,
                self.raw_configuration,
                self.capability_snapshot,
                qualified_empirical_route=qualified_route,
            )
            details["route_provenance"] = {
                "basis": basis,
                "bootstrap_default": chosen.cost_components.get(
                    "bootstrap_default", False
                ),
                "manual_override": chosen.cost_components.get(
                    "manual_override", False
                ),
                "capability_status": chosen.cost_components.get(
                    "capability_status"
                ),
                "observed_sample_count": (
                    selected_resolution.selected_sample_count
                    if selected_resolution is not None
                    else 0
                ),
                "empirical_evidence_used": qualified_route,
                "policy_version": PUBLIC_POLICY_VERSION,
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
                "empirical_sequence_v1"
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

        chosen = (
            self._optimization_choice(alternatives, optimization)
            if self.selection_preference
            in {"balanced", "custom"}
            else None
        )
        empirical_budget_blocked = bool(
            optimization.optimizer_status == "empirical"
            and not optimization.chosen_sequence
        )
        if optimization.optimizer_status != "empirical" or chosen is None:
            chosen = self._choose(alternatives)
        violation = False
        if (
            chosen is None
            and not empirical_budget_blocked
            and self.configuration.allow_constraint_violations
        ):
            feasible = [
                option
                for option in alternatives
                if "backend is disabled" not in option.rejection_reasons
                and not any("cost" in reason for reason in option.rejection_reasons)
            ]
            if feasible:
                strongest = min(
                    feasible,
                    key=lambda option: (
                        -(option.capability_score or 0),
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
            )

        if not context.attempts:
            reason = "Selected the least expensive known-cost sufficient backend."
            if chosen.estimated_cost_usd is None:
                reason = "All eligible estimates are unknown; selected the smallest sufficient capability."
            if violation:
                reason = "No backend met the threshold; selected the strongest backend under an explicit constraint violation."
            elif optimization.optimizer_status == "empirical":
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
                reason = "Cheapest acceptable selected the lowest known-cost eligible route."
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
            tuple[EmpiricalScoreResolution, ...],
            SequenceOptimizationResult,
        ]:
            if remaining_optimization.optimizer_status == "empirical":
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

        same_failures = sum(
            1
            for item in context.attempts
            if item.backend_name == previous_attempt.backend_name
            and item.failure_category == failure
        )
        retry_available = same_failures <= self.configuration.max_same_backend_retries
        if failure == "infrastructure_failure":
            if retry_available and previous is not None and previous.eligible:
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="retry",
                    reason="Infrastructure failure does not change the capability diagnosis; retrying the same backend once.",
                    chosen=previous,
                    repeats=True,
                )
            others = tuple(
                item
                for item in alternatives
                if item.backend_name != previous_attempt.backend_name
            )
            replacement, route_scores, route_optimization = remaining_or_bootstrap(
                self._choose(others)
            )
            if replacement is not None:
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="escalate",
                    reason="Repeated infrastructure failure disabled the prior backend; choosing another configured eligible backend.",
                    chosen=replacement,
                    escalates=True,
                    metadata={"prior_backend_failed": True},
                    empirical_resolutions=route_scores,
                    optimization=route_optimization,
                )
            return self._decision(
                context,
                alternatives,
                minimum,
                rule,
                action="fail",
                reason="Infrastructure failure repeated and no alternative eligible backend is configured.",
            )
        if failure == "implementation_failure":
            if (
                retry_available
                and previous_attempt.material_progress
                and previous is not None
                and previous.eligible
            ):
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="retry",
                    reason="Implementation made material progress; retrying the same backend once.",
                    chosen=previous,
                    repeats=True,
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
                    reason="Implementation retry is unavailable or used; escalating to the next backend in the selected routing path.",
                    chosen=higher,
                    escalates=True,
                    empirical_resolutions=route_scores,
                    optimization=route_optimization,
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
                    reason="Verifier evidence indicates insufficient capability; escalating immediately through the selected routing path.",
                    chosen=higher,
                    escalates=True,
                    empirical_resolutions=route_scores,
                    optimization=route_optimization,
                )
        if failure == "no_change_failure":
            if (
                self.configuration.allow_no_change_retry
                and retry_available
                and previous is not None
                and previous.eligible
            ):
                return self._decision(
                    context,
                    alternatives,
                    minimum,
                    rule,
                    action="retry",
                    reason="Explicit policy permits one no-change retry.",
                    chosen=previous,
                    repeats=True,
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
                    reason="No-change failures escalate by default.",
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
            reason="No eligible retry or escalation remains.",
        )
