"""Versioned deterministic bootstrap policy for the canonical closed loop."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from villani_ops.core.backend import Backend

from .costs import estimate_attempt_cost
from .interfaces import BackendOption, BudgetContext, PolicyContext, PolicyDecision


class BootstrapPolicyConfiguration(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = "bootstrap_v1"
    easy_min_capability: float = Field(default=20, ge=0)
    medium_min_capability: float = Field(default=50, ge=0)
    hard_min_capability: float = Field(default=80, ge=0)
    economy_confidence_threshold: float = Field(default=0.80, ge=0, le=1)
    conservative_confidence_threshold: float = Field(default=0.65, ge=0, le=1)
    max_same_backend_retries: int = Field(default=1, ge=0)
    verifier_retry_limit: int = Field(default=1, ge=0)
    accepted_candidates_required: int = Field(default=1, ge=1)
    allow_constraint_violations: bool = False
    allow_no_change_retry: bool = False


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
            value
            if isinstance(value, Backend)
            else Backend.model_validate(value)
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
        or classification.confidence
        < configuration.conservative_confidence_threshold
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
    ) -> None:
        self.backends = dict(backends)
        self.configuration = BootstrapPolicyConfiguration.model_validate(
            _policy_mapping(configuration or {})
        )
        if self.configuration.version != "bootstrap_v1":
            raise ValueError("bootstrap policy requires version 'bootstrap_v1'")

    @classmethod
    def from_configuration(
        cls, configuration: Mapping[str, Any]
    ) -> BootstrapPolicyEngine:
        return cls(configured_backends(configuration), configuration)

    def _alternatives(
        self, context: PolicyContext, minimum: float
    ) -> tuple[BackendOption, ...]:
        cost_cap_active = context.budget.cost_accounting_status != "not_applicable"
        alternatives: list[BackendOption] = []
        for backend in sorted(self.backends.values(), key=lambda item: item.name):
            if "coding" not in backend.roles:
                continue
            estimate = estimate_attempt_cost(backend)
            reasons: list[str] = []
            if not backend.enabled:
                reasons.append("backend is disabled")
            if backend.capability_score < minimum:
                reasons.append(
                    f"capability {backend.capability_score} is below required {minimum:g}"
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
                    capability_score=float(backend.capability_score),
                    estimated_cost_usd=estimate.total,
                    cost_accounting_status=estimate.accounting_status,
                    rejection_reasons=tuple(reasons),
                    cost_components=estimate.as_dict(),
                    cost_source=estimate.source,
                )
            )
        return tuple(alternatives)

    @staticmethod
    def _choose(options: tuple[BackendOption, ...]) -> BackendOption | None:
        eligible = [option for option in options if option.eligible]
        if not eligible:
            return None
        known = [
            option
            for option in eligible
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
            eligible,
            key=lambda option: (
                option.capability_score if option.capability_score is not None else float("inf"),
                option.backend_name,
            ),
        )

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
        verification_only = action == "retry" and chosen.cost_source == "verification_retry"
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
    ) -> PolicyDecision:
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
        return PolicyDecision(
            action=action,  # type: ignore[arg-type]
            reason=reason,
            considered_backends=alternatives,
            chosen_backend=chosen.backend_name if chosen else None,
            chosen_model=chosen.model if chosen else None,
            policy_version="bootstrap_v1",
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

        if len(context.eligible_candidate_ids) >= self.configuration.accepted_candidates_required:
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
                context, alternatives, minimum, rule,
                action="exhaust", reason="Attempt budget exhausted."
            )
        if (
            context.budget.remaining_wall_time_ms is not None
            and context.budget.remaining_wall_time_ms <= 0
        ):
            return self._decision(
                context, alternatives, minimum, rule,
                action="exhaust", reason="Wall-time budget exhausted."
            )
        if (
            context.budget.cost_accounting_status == "complete"
            and context.budget.remaining_cost_usd is not None
            and context.budget.remaining_cost_usd <= 0
            and context.budget.actual_attempts_used > 0
        ):
            return self._decision(
                context, alternatives, minimum, rule,
                action="exhaust", reason="Cost budget exhausted."
            )

        chosen = self._choose(alternatives)
        violation = False
        if chosen is None and self.configuration.allow_constraint_violations:
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
                reason="No configured coding backend is eligible under capability and budget constraints.",
            )

        if not context.attempts:
            reason = "Selected the least expensive known-cost sufficient backend."
            if chosen.estimated_cost_usd is None:
                reason = "All eligible estimates are unknown; selected the smallest sufficient capability."
            if violation:
                reason = "No backend met the threshold; selected the strongest backend under an explicit constraint violation."
            return self._decision(
                context, alternatives, minimum, rule,
                action="attempt", reason=reason, chosen=chosen,
                metadata={"constraint_violation": violation},
            )

        previous_attempt = context.attempts[-1]
        previous = next(
            (item for item in alternatives if item.backend_name == previous_attempt.backend_name),
            None,
        )
        failure = previous_attempt.failure_category
        latest_verification = (
            context.verifications[-1] if context.verifications else None
        )
        if latest_verification and latest_verification.attempt_id == previous_attempt.attempt_id:
            failure = latest_verification.failure_category or failure

        if failure == "materialization_failure":
            return self._decision(
                context, alternatives, minimum, rule,
                action="fail", reason="Materialization failure is terminal."
            )
        if failure == "verification_failure":
            retries = latest_verification.verifier_retry_count if latest_verification else 0
            if retries < self.configuration.verifier_retry_limit and previous is not None:
                verification_option = replace(previous, cost_source="verification_retry")
                return self._decision(
                    context, alternatives, minimum, rule,
                    action="retry",
                    reason="Retrying verification once without rerunning the coding attempt.",
                    chosen=verification_option,
                    repeats=True,
                    metadata={"retry_scope": "verification"},
                )
            higher = self._next_higher(alternatives, previous) if previous else None
            if higher is not None:
                return self._decision(
                    context, alternatives, minimum, rule,
                    action="escalate",
                    reason="Verification remained ineligible after its retry; continuing with the next higher-capability backend.",
                    chosen=higher, escalates=True,
                )
            return self._decision(
                context, alternatives, minimum, rule,
                action="exhaust", reason="Verification failed twice and no further eligible backend exists."
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
                    context, alternatives, minimum, rule,
                    action="retry",
                    reason="Infrastructure failure does not change the capability diagnosis; retrying the same backend once.",
                    chosen=previous, repeats=True,
                )
            others = tuple(
                item for item in alternatives
                if item.backend_name != previous_attempt.backend_name
            )
            replacement = self._choose(others)
            if replacement is not None:
                return self._decision(
                    context, alternatives, minimum, rule,
                    action="escalate",
                    reason="Repeated infrastructure failure disabled the prior backend; choosing another configured eligible backend.",
                    chosen=replacement, escalates=True,
                    metadata={"prior_backend_failed": True},
                )
            return self._decision(
                context, alternatives, minimum, rule,
                action="fail", reason="Infrastructure failure repeated and no alternative eligible backend is configured."
            )
        if failure == "implementation_failure":
            if (
                retry_available
                and previous_attempt.material_progress
                and previous is not None
                and previous.eligible
            ):
                return self._decision(
                    context, alternatives, minimum, rule,
                    action="retry",
                    reason="Implementation made material progress; retrying the same backend once.",
                    chosen=previous, repeats=True,
                )
            higher = self._next_higher(alternatives, previous) if previous else None
            if higher is not None:
                return self._decision(
                    context, alternatives, minimum, rule,
                    action="escalate",
                    reason="Implementation retry is unavailable or used; escalating to the next higher-capability backend.",
                    chosen=higher, escalates=True,
                )
        if failure == "capability_failure":
            higher = self._next_higher(alternatives, previous) if previous else None
            if higher is not None:
                return self._decision(
                    context, alternatives, minimum, rule,
                    action="escalate",
                    reason="Verifier evidence explicitly indicates insufficient capability; escalating immediately.",
                    chosen=higher, escalates=True,
                )
        if failure == "no_change_failure":
            if (
                self.configuration.allow_no_change_retry
                and retry_available
                and previous is not None
                and previous.eligible
            ):
                return self._decision(
                    context, alternatives, minimum, rule,
                    action="retry", reason="Explicit policy permits one no-change retry.",
                    chosen=previous, repeats=True,
                )
            higher = self._next_higher(alternatives, previous) if previous else None
            if higher is not None:
                return self._decision(
                    context, alternatives, minimum, rule,
                    action="escalate", reason="No-change failures escalate by default.",
                    chosen=higher, escalates=True,
                )

        return self._decision(
            context,
            alternatives,
            minimum,
            rule,
            action="exhaust",
            reason="No eligible retry or escalation remains.",
        )
