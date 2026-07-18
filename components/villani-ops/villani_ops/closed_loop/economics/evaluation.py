"""Point-in-time replay and frozen-policy comparison for PT8."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .models import (
    DurationEstimate,
    HistoricalRouteCase,
    HistoricalSystemOutcome,
    MoneyEstimate,
    PolicyChoiceComparison,
    RoutePolicy,
    RoutePolicyEvaluation,
    StrategyMetrics,
    canonical_digest,
)
from .planner import plan_route


def _policy_with_strategy(policy: RoutePolicy, strategy: str) -> RoutePolicy:
    constraints = policy.constraints.model_copy(
        update={
            "forced_system": None,
            "strongest_only": strategy == "strongest_only",
        }
    )
    return policy.model_copy(update={"strategy": strategy, "constraints": constraints})


def _eligible_candidates(case: HistoricalRouteCase):
    """Discard any candidate whose evidence was not available at decision time."""

    return [
        item
        for item in case.candidates
        if (cutoff := case.candidate_evidence_cutoffs.get(item.route_name)) is not None
        and cutoff <= case.decision_at
    ]


def _choice(
    case: HistoricalRouteCase,
    policy: RoutePolicy,
    *,
    strategy: str | None = None,
) -> str | None:
    selected = _policy_with_strategy(policy, strategy) if strategy else policy
    constraints = selected.constraints
    if strategy == "forced":
        if case.forced_system is None:
            return None
        constraints = constraints.model_copy(
            update={
                "forced_system": case.forced_system,
                "strongest_only": False,
                "allow_experimental_forced": True,
            }
        )
        selected = selected.model_copy(update={"strategy": "forced"})
    plan = plan_route(
        run_id=f"replay:{case.case_id}",
        repository_id=case.repository_id,
        repository_head=case.repository_head,
        task_profile=case.task_profile,
        candidates=_eligible_candidates(case),
        policy=selected,
        constraints=constraints,
        evidence_cutoff=case.decision_at,
    )
    return plan.selected_first_system


def _outcome(
    case: HistoricalRouteCase, route_name: str | None
) -> HistoricalSystemOutcome | None:
    if route_name is None:
        return None
    return next((item for item in case.outcomes if item.route_name == route_name), None)


def _aggregate_metrics(
    cases: tuple[HistoricalRouteCase, ...],
    policy: RoutePolicy,
    strategy: str,
) -> StrategyMetrics:
    outcomes: list[HistoricalSystemOutcome] = []
    unmatched = 0
    unknown_input = 0
    regrets: list[float] = []
    for case in cases:
        candidates = _eligible_candidates(case)
        if not candidates or any(
            item.conservative_acceptance_probability is None
            or item.execution_cost.accounting_status != "complete"
            for item in candidates
        ):
            unknown_input += 1
        selected = _choice(case, policy, strategy=strategy)
        outcome = _outcome(case, selected)
        if outcome is None or not outcome.eligible:
            unmatched += 1
            continue
        outcomes.append(outcome)
        if (
            outcome.total_cost.amount is not None
            and outcome.total_cost.currency == policy.currency
        ):
            comparable = [
                item.total_cost.amount
                for item in case.outcomes
                if item.eligible
                and item.proved_acceptable is True
                and item.accepted_as_is is not False
                and item.total_cost.amount is not None
                and item.total_cost.currency == policy.currency
            ]
            if comparable:
                regrets.append(outcome.total_cost.amount - min(comparable))

    known_costs = [
        item.total_cost.amount
        for item in outcomes
        if item.total_cost.accounting_status == "complete"
        and item.total_cost.amount is not None
        and item.total_cost.currency == policy.currency
    ]
    known_durations = [
        item.duration.duration_ms
        for item in outcomes
        if item.duration.accounting_status == "complete"
        and item.duration.duration_ms is not None
    ]
    reviews = [
        item.review_minutes for item in outcomes if item.review_minutes is not None
    ]
    escalations = [
        item.escalation_count for item in outcomes if item.escalation_count is not None
    ]
    complete_cost = bool(outcomes) and len(known_costs) == len(outcomes)
    complete_duration = bool(outcomes) and len(known_durations) == len(outcomes)
    return StrategyMetrics(
        strategy=strategy,  # type: ignore[arg-type]
        case_count=len(cases),
        accepted_as_is=sum(item.accepted_as_is is True for item in outcomes),
        proved_acceptable=sum(item.proved_acceptable is True for item in outcomes),
        false_acceptance=sum(item.false_acceptance for item in outcomes),
        failures=sum(
            item.proved_acceptable is False or item.accepted_as_is is False
            for item in outcomes
        ),
        total_cost=MoneyEstimate(
            amount=sum(float(value) for value in known_costs)
            if complete_cost
            else None,
            currency=policy.currency if complete_cost else None,
            accounting_status="complete" if complete_cost else "unknown",
            source="frozen_point_in_time_replay",
            sample_count=len(known_costs),
        ),
        elapsed_duration=DurationEstimate(
            duration_ms=(
                sum(float(value) for value in known_durations)
                if complete_duration
                else None
            ),
            accounting_status="complete" if complete_duration else "unknown",
            source="frozen_point_in_time_replay",
            sample_count=len(known_durations),
        ),
        review_minutes=(
            sum(float(value) for value in reviews)
            if len(reviews) == len(outcomes) and outcomes
            else None
        ),
        escalation_count=(
            sum(int(value) for value in escalations)
            if len(escalations) == len(outcomes) and outcomes
            else None
        ),
        regret=MoneyEstimate(
            amount=sum(regrets) if len(regrets) == len(cases) and cases else None,
            currency=policy.currency if len(regrets) == len(cases) and cases else None,
            accounting_status=(
                "complete" if len(regrets) == len(cases) and cases else "unknown"
            ),
            source="measurable_successful_outcomes_only",
            sample_count=len(regrets),
        ),
        unknown_input_rate=(unknown_input / len(cases) if cases else 1.0),
        unmatched_outcome_count=unmatched,
    )


def evaluate_route_policy(
    cases: Iterable[HistoricalRouteCase],
    *,
    active_policy: RoutePolicy,
    proposed_policy: RoutePolicy,
    generated_at: datetime | None = None,
) -> RoutePolicyEvaluation:
    """Compare policies using only evidence available at each historical decision."""

    rows = tuple(sorted(cases, key=lambda item: (item.decision_at, item.case_id)))
    comparisons: list[PolicyChoiceComparison] = []
    refusal: list[str] = []
    for case in rows:
        active_choice = _choice(case, active_policy)
        proposed_choice = _choice(case, proposed_policy)
        available = {item.route_name: item for item in _eligible_candidates(case)}
        active_candidate = available.get(active_choice or "")
        proposed_candidate = available.get(proposed_choice or "")
        active_probability = (
            active_candidate.conservative_acceptance_probability
            if active_candidate is not None
            else None
        )
        proposed_probability = (
            proposed_candidate.conservative_acceptance_probability
            if proposed_candidate is not None
            else None
        )
        reliability = (
            proposed_probability >= active_probability
            if proposed_probability is not None and active_probability is not None
            else None
        )
        active_outcome = _outcome(case, active_choice)
        proposed_outcome = _outcome(case, proposed_choice)
        comparisons.append(
            PolicyChoiceComparison(
                case_id=case.case_id,
                active_choice=active_choice,
                proposed_choice=proposed_choice,
                active_probability=active_probability,
                proposed_probability=proposed_probability,
                reliability_non_decreasing=reliability,
                active_false_acceptance_exposure=(
                    active_outcome.false_acceptance if active_outcome else None
                ),
                proposed_false_acceptance_exposure=(
                    proposed_outcome.false_acceptance if proposed_outcome else None
                ),
                evidence_cutoff=case.decision_at,
            )
        )
    reliability_ok = bool(comparisons) and all(
        item.reliability_non_decreasing is True for item in comparisons
    )
    false_exposure_ok = bool(comparisons) and all(
        item.proposed_false_acceptance_exposure is False
        or item.proposed_false_acceptance_exposure
        == item.active_false_acceptance_exposure
        for item in comparisons
    )
    if not rows:
        refusal.append("no frozen historical cases")
    if not reliability_ok:
        refusal.append("conservative reliability would decrease or cannot be proven")
    if not false_exposure_ok:
        refusal.append("false-acceptance exposure would increase or cannot be proven")
    strategies = (
        "strongest_only",
        "cheapest_qualified",
        "accepted_change_optimizer",
        "forced",
    )
    metrics = [
        _aggregate_metrics(rows, proposed_policy, strategy) for strategy in strategies
    ]
    source = {
        "cases": [item.model_dump(mode="json") for item in rows],
        "active_policy": active_policy.model_dump(mode="json"),
        "proposed_policy": proposed_policy.model_dump(mode="json"),
    }
    now = generated_at or datetime.now(timezone.utc)
    content = {
        "generated_at": now.isoformat(),
        "source": source,
        "comparisons": [item.model_dump(mode="json") for item in comparisons],
        "metrics": [item.model_dump(mode="json") for item in metrics],
        "rejection_reasons": sorted(set(refusal)),
    }
    return RoutePolicyEvaluation(
        evaluation_id="rpeval_" + canonical_digest(content).removeprefix("sha256:"),
        generated_at=now,
        active_policy_version=active_policy.policy_version,
        active_policy_digest=canonical_digest(active_policy.model_dump(mode="json")),
        proposed_policy_version=proposed_policy.policy_version,
        proposed_policy_digest=canonical_digest(
            proposed_policy.model_dump(mode="json")
        ),
        frozen_case_count=len(rows),
        comparisons=comparisons,
        strategy_metrics=metrics,
        conservative_reliability_non_decreasing=reliability_ok,
        false_acceptance_exposure_non_increasing=false_exposure_ok,
        safe_to_publish=not refusal,
        rejection_reasons=sorted(set(refusal)),
        source_digest=canonical_digest(source),
    )


__all__ = ["evaluate_route_policy"]
