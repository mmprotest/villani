from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Mapping

from .models import (
    AssignmentEligibility,
    ExperimentAssignment,
    ExperimentArm,
    ExperimentDefinition,
    OptionEligibilityInput,
)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _eligible(
    arm: ExperimentArm,
    experiment: ExperimentDefinition,
    options: Mapping[str, OptionEligibilityInput],
) -> AssignmentEligibility:
    if arm.is_control or arm.option_id is None:
        return AssignmentEligibility(eligible=True)
    option = options.get(arm.option_id)
    if option is None:
        return AssignmentEligibility(eligible=False, reasons=("option_missing",))
    constraints = experiment.constraints
    reasons: list[str] = []
    if option.capability_score < constraints.minimum_capability_score:
        reasons.append("capability_below_minimum")
    if constraints.security_sensitive and not option.security_approved:
        reasons.append("security_not_approved")
    if constraints.maximum_cost_usd is not None and (
        option.estimated_cost_usd is None
        or option.estimated_cost_usd > constraints.maximum_cost_usd
    ):
        reasons.append("cost_constraint")
    if constraints.allowed_residencies and not set(option.residencies).intersection(
        constraints.allowed_residencies
    ):
        reasons.append("residency_constraint")
    if (
        constraints.allowed_option_ids
        and option.option_id not in constraints.allowed_option_ids
    ):
        reasons.append("user_option_constraint")
    if not option.user_allowed:
        reasons.append("user_disallowed")
    return AssignmentEligibility(eligible=not reasons, reasons=tuple(reasons))


def assign_experiment(
    experiment: ExperimentDefinition,
    *,
    unit_id: str,
    options: Mapping[str, OptionEligibilityInput],
    timestamp: datetime,
) -> ExperimentAssignment:
    seed = hashlib.sha256(f"{experiment.salt}:{unit_id}".encode("utf-8")).hexdigest()
    draw = int(seed[:16], 16) / float(2**64)
    control = next((arm for arm in experiment.arms if arm.is_control), None)
    eligible: list[tuple[ExperimentArm, AssignmentEligibility]] = []
    for arm in experiment.arms:
        status = _eligible(arm, experiment, options)
        if status.eligible:
            eligible.append((arm, status))
    if experiment.mode == "shadow_only":
        eligible = [(control, AssignmentEligibility(eligible=True))] if control else []
    if not eligible:
        raise ValueError("experiment has no safe eligible arm")
    total = sum(arm.probability for arm, _ in eligible)
    normalized = [(arm, status, arm.probability / total) for arm, status in eligible]
    cursor = 0.0
    chosen = normalized[-1]
    for candidate in normalized:
        cursor += candidate[2]
        if draw < cursor:
            chosen = candidate
            break
    arm, status, propensity = chosen
    return ExperimentAssignment(
        experiment_id=experiment.experiment_id,
        experiment_version=experiment.experiment_version,
        arm=arm.name,
        unit_id=unit_id,
        eligibility=status,
        assignment_probability=propensity,
        propensity=propensity,
        randomization_seed=seed,
        policy_snapshot=experiment.policy_snapshot,
        policy_snapshot_digest=_digest(experiment.policy_snapshot),
        timestamp=timestamp,
        mode=experiment.mode,
    )
