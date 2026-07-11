"""Deterministic planning and adaptive stopping for candidate reliability."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .models import (
    AdaptiveStopDecision,
    CandidateDimensions,
    CandidateObservation,
    CandidatePlan,
    ReliabilityStrategyConfiguration,
)


_GRADES = {"none": 0, "weak": 1, "moderate": 2, "strong": 3}


def immutable_baseline_digest(
    repository: str | Path, task: str, success_criteria: str
) -> str:
    root = Path(repository).resolve()
    values: dict[str, Any] = {
        "repository": str(root),
        "task": task,
        "success_criteria": success_criteria,
    }
    try:
        if not root.exists() or not (root / ".git").exists():
            raise FileNotFoundError("repository has no local git metadata")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=10,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=10,
        )
        values.update({"head": head.stdout.strip() or None, "status": status.stdout})
    except (OSError, subprocess.SubprocessError):
        values.update({"head": None, "status": "unavailable"})
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def configuration_from_policy(
    configuration: Mapping[str, Any], *, maximum_attempts: int | None = None
) -> ReliabilityStrategyConfiguration:
    raw = configuration.get("candidate_reliability")
    values = dict(raw) if isinstance(raw, Mapping) else {}
    routing = configuration.get("routing")
    if "strategy" not in values and isinstance(routing, Mapping):
        route = routing.get("candidate_strategy")
        if route in {
            "single_attempt",
            "sequential_escalation",
            "parallel_diverse_candidates",
            "adaptive_candidates",
        }:
            values["strategy"] = route
    policy = configuration.get("policy")
    if isinstance(policy, Mapping):
        values.setdefault(
            "accepted_candidate_requirement",
            int(policy.get("accepted_candidates_required") or 1),
        )
    explicit = isinstance(raw, Mapping) and "strategy" in raw
    strategy = str(
        values.get("strategy")
        or (
            "sequential_escalation" if (maximum_attempts or 1) > 1 else "single_attempt"
        )
    )
    values["strategy"] = strategy
    if strategy == "single_attempt":
        values.setdefault("maximum_candidates", 1)
        values.setdefault("maximum_parallelism", 1)
    else:
        values.setdefault("maximum_candidates", int(maximum_attempts or 1))
        values.setdefault("maximum_parallelism", 1)
    requirement = int(values.get("accepted_candidate_requirement") or 1)
    if requirement > 1:
        if strategy == "single_attempt" and not explicit:
            values["strategy"] = "sequential_escalation"
            values["maximum_candidates"] = max(
                requirement, int(maximum_attempts or requirement)
            )
        values.setdefault("stop_policy", "compare")
    return ReliabilityStrategyConfiguration.model_validate(values)


def build_candidate_plans(
    config: ReliabilityStrategyConfiguration,
    *,
    baseline_sha256: str,
    default_dimensions: CandidateDimensions,
) -> tuple[CandidatePlan, ...]:
    dimensions = list(config.candidates)
    while len(dimensions) < config.maximum_candidates:
        ordinal = len(dimensions) + 1
        dimensions.append(
            default_dimensions.model_copy(
                update={
                    "seed": ordinal
                    if config.strategy
                    in {"parallel_diverse_candidates", "adaptive_candidates"}
                    else default_dimensions.seed
                }
            )
        )
    plans: list[CandidatePlan] = []
    for index, item in enumerate(dimensions[: config.maximum_candidates]):
        plans.append(
            CandidatePlan(
                candidate_id=f"candidate_{index + 1:03d}",
                ordinal=index + 1,
                dimensions=item,
                effective_configuration_sha256=item.effective_fingerprint,
                baseline_sha256=baseline_sha256,
                sandbox_id=f"sandbox_{index + 1:03d}",
                expected_success=(
                    config.expected_success_by_ordinal[index]
                    if index < len(config.expected_success_by_ordinal)
                    else None
                ),
                estimated_cost_usd=(
                    config.estimated_cost_usd_by_ordinal[index]
                    if index < len(config.estimated_cost_usd_by_ordinal)
                    else None
                ),
            )
        )
    return tuple(plans)


def diversity_summary(plans: tuple[CandidatePlan, ...]) -> tuple[bool, int]:
    distinct = len({item.effective_configuration_sha256 for item in plans})
    return distinct > 1, distinct


def adaptive_stop(
    config: ReliabilityStrategyConfiguration,
    plans: tuple[CandidatePlan, ...],
    observations: tuple[CandidateObservation, ...],
    *,
    remaining_attempt_budget: int,
    remaining_cost_budget_usd: float | None,
) -> AdaptiveStopDecision:
    qualifying = [
        item
        for item in observations
        if item.acceptance_eligible
        and (item.verifier_confidence or 0.0) >= config.minimum_verifier_confidence
        and _GRADES[item.evidence_grade] >= _GRADES[config.minimum_evidence_grade]
    ]
    remaining = plans[len(observations) :]
    avoided_spend_values = [item.estimated_cost_usd for item in remaining]
    avoided_spend = (
        sum(value for value in avoided_spend_values if value is not None)
        if remaining and all(value is not None for value in avoided_spend_values)
        else None
    )
    common = {
        "accepted_count": len(qualifying),
        "remaining_attempt_budget": max(remaining_attempt_budget, 0),
        "remaining_cost_budget_usd": remaining_cost_budget_usd,
        "avoided_attempts": len(remaining),
        "estimated_avoided_spend_usd": avoided_spend,
    }
    if len(qualifying) >= config.accepted_candidate_requirement:
        return AdaptiveStopDecision(
            stop=True, reason="accepted_candidate_requirement_met", **common
        )
    if not remaining or remaining_attempt_budget <= 0:
        return AdaptiveStopDecision(
            stop=True, reason="attempt_budget_exhausted", **common
        )
    next_plan = remaining[0]
    marginal = next_plan.expected_success
    if config.strategy == "adaptive_candidates" and (
        marginal is None or marginal < config.minimum_marginal_expected_success
    ):
        return AdaptiveStopDecision(
            stop=True,
            reason="marginal_expected_success_below_threshold",
            next_marginal_expected_success=marginal,
            **common,
        )
    if remaining_cost_budget_usd is not None and (
        next_plan.estimated_cost_usd is None
        or next_plan.estimated_cost_usd > remaining_cost_budget_usd
    ):
        return AdaptiveStopDecision(
            stop=True,
            reason="remaining_cost_budget_cannot_fund_next_candidate",
            next_marginal_expected_success=marginal,
            **common,
        )
    return AdaptiveStopDecision(
        stop=False,
        reason="additional_candidate_justified",
        next_marginal_expected_success=marginal,
        avoided_attempts=0,
        estimated_avoided_spend_usd=0.0 if avoided_spend is not None else None,
        **{
            key: value
            for key, value in common.items()
            if key not in {"avoided_attempts", "estimated_avoided_spend_usd"}
        },
    )
