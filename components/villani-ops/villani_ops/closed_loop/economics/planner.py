"""Deterministic conservative routing for total cost per accepted change."""

from __future__ import annotations

from datetime import datetime
from math import prod
from typing import Iterable, Literal, cast

from ..qualification.models import QualificationTaskProfile
from .models import (
    AcceptedChangeObjective,
    MoneyEstimate,
    RouteCandidateInput,
    RouteConsideration,
    RouteConstraints,
    RoutePlan,
    RoutePolicy,
    RouteSequenceEconomics,
    canonical_digest,
)


DEFAULT_EXPLANATION = (
    "Villani chose the route most likely to produce a proven change at the lowest "
    "total cost."
)
_COMPONENTS = (
    "execution_cost",
    "verification_cost",
    "human_review_cost",
    "retry_escalation_cost",
    "latency_penalty",
)


def _money_zero(currency: str, source: str) -> MoneyEstimate:
    return MoneyEstimate(
        amount=0.0,
        currency=currency,
        accounting_status="complete",
        source=source,
    )


def calculate_objective(
    candidate: RouteCandidateInput,
    *,
    policy: RoutePolicy,
) -> AcceptedChangeObjective:
    """Calculate a full or explicitly partial accepted-change objective."""

    components = {name: getattr(candidate, name) for name in _COMPONENTS}
    known: list[MoneyEstimate] = []
    unknowns: list[str] = []
    for name, component in components.items():
        if component.accounting_status == "not_applicable":
            continue
        if component.accounting_status in {"complete", "partial"}:
            known.append(component)
            if component.accounting_status == "partial":
                unknowns.append(f"{name}:partial")
        else:
            unknowns.append(name)
    currencies = {item.currency for item in known if item.currency is not None}
    if len(currencies) > 1:
        unknowns.append("incomparable_currencies")
    currency = next(iter(currencies), policy.currency) if len(currencies) <= 1 else None
    subtotal = sum(item.amount or 0.0 for item in known) if known else None
    probability = candidate.conservative_acceptance_probability
    probability_source = (
        "repository_qualification_wilson_lower_bound"
        if probability is not None
        else "unknown"
    )
    if probability is None:
        unknowns.append("conservative_acceptance_probability")

    if not known and unknowns:
        status: Literal["complete", "partial", "unknown", "not_applicable"] = "unknown"
    elif not known:
        status = "not_applicable"
    elif unknowns:
        status = "partial"
    else:
        status = "complete"
    full = (
        subtotal / probability
        if status == "complete" and subtotal is not None and probability
        else None
    )
    partial = (
        subtotal / probability
        if status == "partial" and subtotal is not None and probability
        else None
    )
    return AcceptedChangeObjective(
        execution_cost=candidate.execution_cost,
        verification_cost=candidate.verification_cost,
        human_review_cost=candidate.human_review_cost,
        retry_escalation_cost=candidate.retry_escalation_cost,
        latency_penalty=candidate.latency_penalty,
        conservative_acceptance_probability=probability,
        probability_source=probability_source,
        known_numerator_cost=subtotal,
        currency=currency,
        accounting_status=status,
        unknown_components=sorted(set(unknowns)),
        expected_accepted_change_cost=full,
        partial_expected_known_cost=partial,
        expected_duration=candidate.duration,
    )


def _strength(candidate: RouteCandidateInput) -> tuple[float, int, float, str]:
    return (
        candidate.conservative_acceptance_probability or -1.0,
        candidate.qualification_sample_count,
        candidate.capability_score,
        candidate.route_name,
    )


def _constraints_rejections(
    candidate: RouteCandidateInput,
    *,
    constraints: RouteConstraints,
    forced: bool,
    qualified_available: bool,
) -> list[str]:
    reasons = list(candidate.input_rejection_reasons)
    references = {candidate.route_name, candidate.backend_name}
    if candidate.system_id:
        references.add(candidate.system_id)
    if candidate.availability == "unavailable":
        reasons.append("system is unavailable")
    elif candidate.availability == "rate_limited":
        reasons.append("system is temporarily rate limited")
    if not candidate.reserve_satisfied:
        reasons.append("required downstream reserves are not satisfied")
    if constraints.local_only and not candidate.local:
        reasons.append("local-only privacy constraint")
    if (
        constraints.allowed_providers
        and candidate.provider not in constraints.allowed_providers
    ):
        reasons.append("provider is outside the allowed provider set")
    if references.intersection(constraints.excluded_systems):
        reasons.append("system is explicitly excluded")
    if (
        constraints.allowed_permission_profiles
        and candidate.permission_profile not in constraints.allowed_permission_profiles
    ):
        reasons.append("permission profile is not allowed")
    if candidate.false_acceptance_count:
        reasons.append("known false acceptance quarantines this profile")

    if forced:
        if not constraints.forced_system or constraints.forced_system not in references:
            reasons.append("another system was explicitly forced")
        elif candidate.qualification_state == "unsupported":
            reasons.append("unsupported systems cannot be forced")
        elif (
            candidate.qualification_state == "experimental"
            and not constraints.allow_experimental_forced
        ):
            reasons.append("experimental forced use was not acknowledged")
    elif qualified_available:
        if candidate.qualification_state != "qualified":
            reasons.append("automatic routing may choose only qualified systems")
    elif candidate.qualification_state != "provisional":
        reasons.append(
            "no qualified system exists and this is not a provisional fallback"
        )
    return sorted(set(reasons))


def _objective_rank(
    pair: tuple[RouteCandidateInput, AcceptedChangeObjective],
) -> tuple[float, float, float, str]:
    candidate, objective = pair
    return (
        objective.expected_accepted_change_cost
        if objective.expected_accepted_change_cost is not None
        else float("inf"),
        -(candidate.conservative_acceptance_probability or 0.0),
        objective.expected_duration.duration_ms
        if objective.expected_duration.duration_ms is not None
        else float("inf"),
        candidate.route_name,
    )


def _cheapest_rank(
    pair: tuple[RouteCandidateInput, AcceptedChangeObjective],
) -> tuple[float, float, str]:
    candidate, _objective = pair
    return (
        candidate.execution_cost.amount
        if candidate.execution_cost.accounting_status == "complete"
        and candidate.execution_cost.amount is not None
        else float("inf"),
        -(candidate.conservative_acceptance_probability or 0.0),
        candidate.route_name,
    )


def _sequence_economics(
    ordered: list[tuple[RouteCandidateInput, AcceptedChangeObjective]],
) -> RouteSequenceEconomics:
    systems = [candidate.route_name for candidate, _ in ordered]
    unknowns: list[str] = []
    if not ordered:
        return RouteSequenceEconomics(
            systems=[],
            conservative_success_probability=None,
            expected_cost_before_acceptance=None,
            expected_accepted_change_cost=None,
            currency=None,
            expected_duration_ms=None,
            accounting_status="unknown",
            unknowns=["no_safe_route"],
        )
    probabilities = [
        candidate.conservative_acceptance_probability for candidate, _ in ordered
    ]
    if any(value is None for value in probabilities):
        unknowns.append("sequence_probability")
    currencies = {
        objective.currency for _, objective in ordered if objective.currency is not None
    }
    if len(currencies) > 1:
        unknowns.append("sequence_currency")
    complete_cost = (
        all(
            objective.accounting_status == "complete"
            and objective.known_numerator_cost is not None
            for _, objective in ordered
        )
        and len(currencies) == 1
    )
    if not complete_cost:
        unknowns.append("sequence_total_cost")
    complete_duration = all(
        objective.expected_duration.accounting_status == "complete"
        and objective.expected_duration.duration_ms is not None
        for _, objective in ordered
    )
    if not complete_duration:
        unknowns.append("sequence_duration")
    known_probabilities = [value for value in probabilities if value is not None]
    probability = (
        1.0 - prod(1.0 - value for value in known_probabilities)
        if len(known_probabilities) == len(probabilities)
        else None
    )
    expected_cost: float | None = None
    if complete_cost and probability is not None:
        expected_cost = sum(
            cast(float, objective.known_numerator_cost)
            * prod(1.0 - value for value in known_probabilities[:index])
            for index, (_candidate, objective) in enumerate(ordered)
        )
    expected_duration: float | None = None
    if complete_duration and probability is not None:
        expected_duration = sum(
            float(cast(int, objective.expected_duration.duration_ms))
            * prod(1.0 - value for value in known_probabilities[:index])
            for index, (_candidate, objective) in enumerate(ordered)
        )
    status: Literal["complete", "partial", "unknown", "not_applicable"] = (
        "complete"
        if not unknowns
        else "partial"
        if expected_cost is not None
        else "unknown"
    )
    return RouteSequenceEconomics(
        systems=systems,
        conservative_success_probability=probability,
        expected_cost_before_acceptance=expected_cost,
        expected_accepted_change_cost=(
            expected_cost / probability
            if expected_cost is not None and probability
            else None
        ),
        currency=next(iter(currencies)) if len(currencies) == 1 else None,
        expected_duration_ms=expected_duration,
        accounting_status=status,
        unknowns=sorted(set(unknowns)),
    )


def plan_route(
    *,
    run_id: str,
    repository_id: str,
    repository_head: str | None,
    task_profile: QualificationTaskProfile,
    candidates: Iterable[RouteCandidateInput],
    policy: RoutePolicy,
    constraints: RouteConstraints | None = None,
    evidence_cutoff: datetime | None = None,
    reserves: dict[str, object] | None = None,
    sequential_selection: str | None = None,
    sequential_mode: Literal["sequential_retry", "sequential_escalation"] | None = None,
) -> RoutePlan:
    """Create an explainable plan without using task text or future outcomes."""

    ordered_candidates = sorted(candidates, key=lambda item: item.route_name)
    selected_constraints = constraints or policy.constraints
    forced = selected_constraints.forced_system is not None
    qualified_available = any(
        item.qualification_state == "qualified" for item in ordered_candidates
    )
    if not qualified_available and not policy.allow_provisional_fallback and not forced:
        qualified_available = True  # rejects every non-qualified candidate below

    evaluated: list[tuple[RouteCandidateInput, AcceptedChangeObjective, list[str]]] = []
    for candidate in ordered_candidates:
        objective = calculate_objective(candidate, policy=policy)
        reasons = _constraints_rejections(
            candidate,
            constraints=selected_constraints,
            forced=forced,
            qualified_available=qualified_available,
        )
        if selected_constraints.maximum_known_cost_usd is not None:
            if objective.known_numerator_cost is None or objective.currency != "USD":
                reasons.append("maximum known cost cannot be proven in USD")
            elif (
                objective.known_numerator_cost
                > selected_constraints.maximum_known_cost_usd
            ):
                reasons.append("known route cost exceeds the configured maximum")
        evaluated.append((candidate, objective, sorted(set(reasons))))

    eligible = [
        (candidate, objective)
        for candidate, objective, reasons in evaluated
        if not reasons
    ]
    preferred = (
        [
            item
            for item in eligible
            if item[0].provider == selected_constraints.preferred_provider
        ]
        if selected_constraints.preferred_provider
        else []
    )
    selection_pool = preferred or eligible
    if selected_constraints.prefer_local:
        local_preferred = [item for item in selection_pool if item[0].local]
        selection_pool = local_preferred or selection_pool

    selection_mode: str
    selected: tuple[RouteCandidateInput, AcceptedChangeObjective] | None = None
    if sequential_selection is not None:
        selected = next(
            (item for item in eligible if item[0].route_name == sequential_selection),
            None,
        )
        selection_mode = sequential_mode or "sequential_escalation"
    elif forced:
        selected = next(
            (
                item
                for item in eligible
                if selected_constraints.forced_system
                in {
                    item[0].route_name,
                    item[0].backend_name,
                    item[0].system_id,
                }
            ),
            None,
        )
        selection_mode = "forced"
    elif not selection_pool:
        selection_mode = "no_safe_route"
    elif not qualified_available:
        selected = max(selection_pool, key=lambda item: _strength(item[0]))
        selection_mode = "provisional_fallback"
    elif selected_constraints.strongest_only or policy.strategy == "strongest_only":
        selected = max(selection_pool, key=lambda item: _strength(item[0]))
        selection_mode = "strongest_only"
    elif policy.strategy == "cheapest_qualified":
        cheapest = sorted(selection_pool, key=_cheapest_rank)
        if cheapest and cheapest[0][0].execution_cost.accounting_status == "complete":
            selected = cheapest[0]
            selection_mode = "cheapest_qualified"
        else:
            selected = max(selection_pool, key=lambda item: _strength(item[0]))
            selection_mode = "sparse_strongest_evidence"
    else:
        currencies = {item[1].currency for item in selection_pool}
        complete = (
            bool(selection_pool)
            and all(
                objective.accounting_status == "complete"
                and objective.expected_accepted_change_cost is not None
                for _, objective in selection_pool
            )
            and len(currencies) == 1
        )
        if complete:
            selected = sorted(selection_pool, key=_objective_rank)[0]
            selection_mode = "accepted_change_optimizer"
        else:
            selected = max(selection_pool, key=lambda item: _strength(item[0]))
            selection_mode = "sparse_strongest_evidence"

    fallbacks: list[tuple[RouteCandidateInput, AcceptedChangeObjective]] = []
    if selected is not None:
        selected_strength = _strength(selected[0])
        stronger = [
            item
            for item in eligible
            if item[0].route_name != selected[0].route_name
            and item[0].qualification_state == "qualified"
            and _strength(item[0]) > selected_strength
        ]
        fallbacks = sorted(stronger, key=lambda item: _strength(item[0]))

    considerations: list[RouteConsideration] = []
    all_unknowns: list[str] = []
    for candidate, objective, reasons in evaluated:
        unknowns = list(objective.unknown_components)
        if candidate.availability == "unknown":
            unknowns.append("availability")
        all_unknowns.extend(f"{candidate.route_name}:{item}" for item in unknowns)
        considerations.append(
            RouteConsideration(
                backend_name=candidate.backend_name,
                route_name=candidate.route_name,
                system_id=candidate.system_id,
                harness=candidate.harness,
                model=candidate.model,
                provider=candidate.provider,
                local=candidate.local,
                permission_profile=candidate.permission_profile,
                availability=candidate.availability,
                qualification_state=candidate.qualification_state,
                qualification_level=candidate.qualification_level,
                qualification_sample_count=candidate.qualification_sample_count,
                conservative_acceptance_probability=(
                    candidate.conservative_acceptance_probability
                ),
                task_probability_threshold=candidate.task_probability_threshold,
                capability_score=candidate.capability_score,
                eligible=not reasons,
                rejection_reasons=reasons,
                unknowns=sorted(set(unknowns)),
                objective=objective,
            )
        )

    sequence = ([selected] if selected else []) + fallbacks
    sequence_economics = _sequence_economics(sequence)
    all_unknowns.extend(sequence_economics.unknowns)
    explanation = DEFAULT_EXPLANATION
    if selection_mode == "sparse_strongest_evidence":
        explanation += (
            " Comparable total-cost inputs are incomplete, so it fell back to the "
            "strongest qualified evidence."
        )
    elif selection_mode == "provisional_fallback":
        explanation += (
            " No qualified system exists, so it selected the strongest eligible "
            "provisional fallback and labeled it Provisional."
        )
    elif selection_mode == "forced":
        explanation = (
            "Villani recorded the explicit forced system; this choice is excluded "
            "from automatic-policy quality metrics."
        )
    elif selection_mode == "no_safe_route":
        explanation = "No safe economically rational route remains under the recorded constraints."
    elif (
        selected_constraints.prefer_local and selected is not None and selected[0].local
    ):
        explanation += (
            " It applied the configured Local first preference among eligible routes."
        )

    payload = {
        "run_id": run_id,
        "repository_id": repository_id,
        "repository_head": repository_head,
        "task_profile": task_profile.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
        "constraints": selected_constraints.model_dump(mode="json"),
        "candidates": [item.model_dump(mode="json") for item in ordered_candidates],
        "evidence_cutoff": evidence_cutoff.isoformat()
        if evidence_cutoff is not None
        else None,
        "sequential_selection": sequential_selection,
        "sequential_mode": sequential_mode,
    }
    input_digest = canonical_digest(payload)
    plan_without_id = {
        **payload,
        "policy_digest": canonical_digest(policy.model_dump(mode="json")),
        "systems_considered": [item.model_dump(mode="json") for item in considerations],
        "selected_first_system": selected[0].route_name if selected else None,
        "ordered_fallbacks": [item[0].route_name for item in fallbacks],
        "sequence_economics": sequence_economics.model_dump(mode="json"),
        "reserves": reserves or {},
        "selection_mode": selection_mode,
        "forced_choice": forced,
        "automatic_policy_metrics_eligible": not forced,
        "unknowns": sorted(set(all_unknowns)),
        "explanation": explanation,
        "input_digest": input_digest,
    }
    plan_id = "rplan_" + canonical_digest(plan_without_id).removeprefix("sha256:")
    return RoutePlan(
        plan_id=plan_id,
        run_id=run_id,
        repository_id=repository_id,
        repository_head=repository_head,
        task_profile=task_profile,
        policy_version=policy.policy_version,
        policy_digest=canonical_digest(policy.model_dump(mode="json")),
        evidence_cutoff=evidence_cutoff,
        input_digest=input_digest,
        systems_considered=considerations,
        selected_first_system=selected[0].route_name if selected else None,
        ordered_fallbacks=[item[0].route_name for item in fallbacks],
        sequence_economics=sequence_economics,
        reserves=dict(reserves or {}),
        constraints=selected_constraints,
        selection_mode=selection_mode,  # type: ignore[arg-type]
        forced_choice=forced,
        automatic_policy_metrics_eligible=not forced,
        unknowns=sorted(set(all_unknowns)),
        explanation=explanation,
    )


def with_latency_penalty(
    candidate: RouteCandidateInput,
    *,
    policy: RoutePolicy,
) -> RouteCandidateInput:
    """Derive latency cost only when both duration and an explicit rate exist."""

    if policy.latency_penalty_per_second is None:
        penalty = MoneyEstimate(
            amount=None,
            currency=None,
            accounting_status="not_applicable",
            source="latency_penalty_disabled",
        )
    elif candidate.duration.duration_ms is None:
        penalty = MoneyEstimate(
            amount=None,
            currency=None,
            accounting_status="unknown",
            source="duration_unknown",
        )
    else:
        penalty = _money_zero(policy.currency, "configured_latency_penalty")
        penalty = penalty.model_copy(
            update={
                "amount": (
                    candidate.duration.duration_ms
                    / 1000.0
                    * policy.latency_penalty_per_second
                )
            }
        )
    return candidate.model_copy(update={"latency_penalty": penalty})


__all__ = [
    "DEFAULT_EXPLANATION",
    "calculate_objective",
    "plan_route",
    "with_latency_penalty",
]
