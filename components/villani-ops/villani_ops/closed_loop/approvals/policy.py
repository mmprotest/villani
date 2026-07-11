"""Deterministic approval matching and scope validation."""

from __future__ import annotations
from datetime import datetime, timezone
from fnmatch import fnmatch
from .models import (
    ApprovalContext,
    ApprovalPolicy,
    ApprovalRecord,
    ApprovalRequirement,
    ApprovalValidation,
)


def _matches(values: tuple[str, ...], actual: str) -> bool:
    return not values or any(fnmatch(actual, p) for p in values)


def approval_requirements(
    policy: ApprovalPolicy, context: ApprovalContext
) -> tuple[ApprovalRequirement, ...]:
    output = []
    for rule in policy.rules:
        checks = (
            _matches(rule.risks, context.risk),
            _matches(rule.repositories, context.repository),
            not rule.paths
            or not context.paths
            or any(any(fnmatch(p, x) for x in rule.paths) for p in context.paths),
            not rule.tool_actions
            or bool(set(rule.tool_actions) & set(context.tool_actions)),
            not rule.evidence_gaps
            or bool(set(rule.evidence_gaps) & set(context.evidence_gaps)),
            rule.minimum_cost_usd is None
            or context.cost_usd is None
            or context.cost_usd >= rule.minimum_cost_usd,
            _matches(rule.materialization_types, context.materialization_type),
        )
        if all(checks):
            output.append(
                ApprovalRequirement(
                    rule_id=rule.rule_id,
                    policy_version=policy.policy_version,
                    reasons=(f"matched approval rule {rule.rule_id}",),
                )
            )
    return tuple(output)


def validate_approval(
    record: ApprovalRecord,
    requirement: ApprovalRequirement,
    context: ApprovalContext,
    *,
    now: datetime | None = None,
    required_authoritative_failure: bool = False,
) -> ApprovalValidation:
    current, failures = now or datetime.now(timezone.utc), []
    if required_authoritative_failure:
        failures.append(
            "approval cannot override a failed required authoritative verifier"
        )
    if record.decision != "approved":
        failures.append("approval decision is not approved")
    if record.expires_at <= current:
        failures.append("approval is expired")
    if record.policy_version != requirement.policy_version:
        failures.append("approval policy version does not match")
    if record.run_id != context.run_id or record.attempt_id != context.attempt_id:
        failures.append("approval run or attempt scope does not match")
    if record.scope.repository != context.repository:
        failures.append("approval repository scope does not match")
    if record.scope.materialization_type != context.materialization_type:
        failures.append("approval materialization scope does not match")
    if not set(context.paths).issubset(record.scope.paths):
        failures.append("approval path scope does not cover delivery")
    if not set(context.tool_actions).issubset(record.scope.tool_actions):
        failures.append("approval tool-action scope does not cover delivery")
    if context.cost_usd is not None and (
        record.scope.maximum_cost_usd is None
        or record.scope.maximum_cost_usd < context.cost_usd
    ):
        failures.append("approval cost scope does not cover delivery")
    return ApprovalValidation(
        valid=not failures, reason="approved" if not failures else "; ".join(failures)
    )
