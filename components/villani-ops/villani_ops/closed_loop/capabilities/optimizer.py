"""Deterministic empirical escalation-sequence enumeration."""

from __future__ import annotations

from itertools import permutations
from math import prod
from typing import cast
from typing import Iterable

from .models import (
    EmpiricalBackendInput,
    SequenceEvaluation,
    SequenceOptimizationResult,
)


OPTIMIZER_VERSION = "empirical_sequence_v2"
FORMULAS = {
    "expected_cost": "sum(cost_i * product(1 - p_j for every earlier j))",
    "success_probability": "1 - product(1 - p_i)",
    "expected_cost_to_success": "mean_attempt_cost / conservative_success_probability",
    "budget_constraint": "sum(cost_i) <= known_cost_budget",
    "expected_duration": "sum(duration_i * product(1 - p_j for every earlier j))",
    "effective_capability": "floor(100 * Wilson lower bound)",
}


def _fallback(
    inputs: tuple[EmpiricalBackendInput, ...],
    missing: Iterable[str],
    *,
    max_attempts: int,
    target: float,
    budget: float | None,
) -> SequenceOptimizationResult:
    return SequenceOptimizationResult(
        optimizer_status="bootstrap_fallback",
        optimizer_version=OPTIMIZER_VERSION,
        fallback_policy_version="bootstrap_v1",
        missing_inputs=tuple(sorted(set(missing))),
        target_success_probability=target,
        max_attempts=max_attempts,
        known_cost_budget=budget,
        input_backends=inputs,
        considered_sequences=(),
        chosen_sequence=(),
        total_enumerated_sequences=0,
        feasible_sequence_count=0,
        rejected_by_cost_budget=0,
        omitted_sequence_count=0,
        pruning_rule=None,
        pruned_backends=(),
        formulas=FORMULAS,
    )


def _rank(sequence: SequenceEvaluation) -> tuple[object, ...]:
    if sequence.reaches_target:
        return (
            0,
            sequence.expected_cost,
            sequence.expected_duration_ms,
            -sequence.success_probability,
            sequence.backends,
        )
    return (
        1,
        -sequence.success_probability,
        sequence.expected_cost,
        sequence.expected_duration_ms,
        sequence.backends,
    )


def optimize_sequence(
    inputs: Iterable[EmpiricalBackendInput],
    *,
    max_attempts: int,
    known_cost_budget: float | None = None,
    target_success_probability: float = 0.80,
    persisted_top_n: int = 100,
) -> SequenceOptimizationResult:
    """Enumerate eligible backend orders or explain an exact bootstrap fallback."""

    if max_attempts < 0:
        raise ValueError("max_attempts must not be negative")
    if known_cost_budget is not None and known_cost_budget < 0:
        raise ValueError("known_cost_budget must not be negative")
    if not 0 <= target_success_probability <= 1:
        raise ValueError("target_success_probability must be between zero and one")
    if persisted_top_n < 1:
        raise ValueError("persisted_top_n must be at least one")
    ordered_inputs = tuple(sorted(inputs, key=lambda item: item.backend_name))
    missing: list[str] = []
    if not ordered_inputs:
        missing.append("no_eligible_backends")
    for item in ordered_inputs:
        if (
            not item.sufficient_probability_data
            or item.conservative_success_probability is None
        ):
            missing.append(f"{item.backend_name}:insufficient_probability_data")
        if item.mean_actual_attempt_cost is None:
            missing.append(f"{item.backend_name}:mean_actual_attempt_cost")
        if item.effective_capability_score is None:
            missing.append(f"{item.backend_name}:effective_capability_score")
        if item.median_duration_ms is None and item.mean_duration_ms is None:
            missing.append(f"{item.backend_name}:observed_duration")
    if missing:
        return _fallback(
            ordered_inputs,
            missing,
            max_attempts=max_attempts,
            target=target_success_probability,
            budget=known_cost_budget,
        )

    candidates = list(ordered_inputs)
    pruning_rule: str | None = None
    pruned: tuple[str, ...] = ()
    if len(candidates) > 8:
        candidates.sort(
            key=lambda item: (
                (
                    item.mean_actual_attempt_cost
                    / item.conservative_success_probability
                    if item.conservative_success_probability
                    and item.mean_actual_attempt_cost is not None
                    else float("inf")
                ),
                item.backend_name,
            )
        )
        pruned = tuple(item.backend_name for item in candidates[8:])
        candidates = candidates[:8]
        pruning_rule = (
            "pruned_to_8_lowest_conservative_cost_to_success_then_backend_name"
        )

    by_name = {item.backend_name: item for item in candidates}
    evaluations: list[SequenceEvaluation] = []
    total_enumerated = 0
    rejected_by_budget = 0
    for length in range(1, min(max_attempts, len(candidates)) + 1):
        for backend_names in permutations(sorted(by_name), length):
            total_enumerated += 1
            sequence_inputs = [by_name[name] for name in backend_names]
            costs = [
                cast(float, item.mean_actual_attempt_cost) for item in sequence_inputs
            ]
            probabilities = [
                cast(float, item.conservative_success_probability)
                for item in sequence_inputs
            ]
            durations = [
                cast(
                    float,
                    item.median_duration_ms
                    if item.median_duration_ms is not None
                    else item.mean_duration_ms,
                )
                for item in sequence_inputs
            ]
            worst_case_cost = sum(costs)
            if known_cost_budget is not None and worst_case_cost > known_cost_budget:
                rejected_by_budget += 1
                continue
            expected_cost = sum(
                cost * prod(1.0 - earlier for earlier in probabilities[:index])
                for index, cost in enumerate(costs)
            )
            expected_duration = sum(
                duration * prod(1.0 - earlier for earlier in probabilities[:index])
                for index, duration in enumerate(durations)
            )
            success_probability = 1.0 - prod(1.0 - value for value in probabilities)
            evaluations.append(
                SequenceEvaluation(
                    backends=backend_names,
                    expected_cost=expected_cost,
                    success_probability=success_probability,
                    worst_case_cost=worst_case_cost,
                    reaches_target=success_probability >= target_success_probability,
                    expected_duration_ms=expected_duration,
                    worst_case_duration_ms=sum(durations),
                )
            )
    ranked = sorted(evaluations, key=_rank)
    chosen = ranked[0].backends if ranked else ()
    persisted = tuple(ranked[:persisted_top_n])
    return SequenceOptimizationResult(
        optimizer_status="empirical",
        optimizer_version=OPTIMIZER_VERSION,
        fallback_policy_version=None,
        missing_inputs=(),
        target_success_probability=target_success_probability,
        max_attempts=max_attempts,
        known_cost_budget=known_cost_budget,
        input_backends=ordered_inputs,
        considered_sequences=persisted,
        chosen_sequence=chosen,
        total_enumerated_sequences=total_enumerated,
        feasible_sequence_count=len(evaluations),
        rejected_by_cost_budget=rejected_by_budget,
        omitted_sequence_count=max(len(evaluations) - len(persisted), 0),
        pruning_rule=pruning_rule,
        pruned_backends=pruned,
        formulas=FORMULAS,
    )
