"""Deterministic, repository-policy-driven adaptive verification planning."""

from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Mapping, Sequence

from .models import (
    AdaptiveVerificationPlan,
    AdaptiveVerificationPolicy,
    VerificationPlanNode,
    canonical_digest,
)


_SECURITY_SIGNALS = (
    "security",
    "credential",
    "secret",
    "permission",
    "authorization",
    "authentication",
    "encryption",
)
_DESTRUCTIVE_SIGNALS = (
    "destructive",
    "data loss",
    "delete data",
    "drop data",
    "irreversible",
    "overwrite data",
)
_MIGRATION_SIGNALS = (
    "data migration",
    "schema migration",
    "migrate stored",
    "backfill",
)
_HISTORICAL_CRITICAL_SIGNALS = {
    "false_acceptance",
    "reopened_defect",
    "verification_policy_breach",
}
_HISTORICAL_ELEVATED_SIGNALS = {
    "verifier_disagreement",
    "false_rejection",
    "unclear_verdict",
    "missing_evidence",
}

SEMANTIC_CONTEXT_ALLOWLIST = sorted(
    {
        "attempt_id",
        "candidate_patch",
        "changed_files",
        "repository_validation",
        "requirements",
        "run_id",
        "schema_version",
        "success_criteria",
        "task_prompt",
    }
)
SEMANTIC_CONTEXT_EXCLUDED = sorted(
    {
        "competing_candidates",
        "cost",
        "execution_environment_identity",
        "harness_identity",
        "model_identity",
        "provider_identity",
        "qualification",
        "route",
    }
)


def policy_from_configuration(
    configuration: Mapping[str, Any],
) -> AdaptiveVerificationPolicy:
    """Load the additive PT9 block while keeping older configuration readable."""

    value = configuration.get("adaptive_verification")
    if not isinstance(value, Mapping):
        return AdaptiveVerificationPolicy()
    mapped: dict[str, Any] = {}
    aliases = {
        "standard_patch_line_limit": "standard_patch_line_limit",
        "elevated_patch_line_limit": "elevated_patch_line_limit",
        "standard_changed_file_limit": "standard_changed_file_limit",
        "elevated_changed_file_limit": "elevated_changed_file_limit",
        "sensitive_paths": "configured_sensitive_paths",
        "generated_artifact_paths": "configured_generated_artifact_paths",
        "require_independent_verifier_for_critical": (
            "require_independent_verifier_for_critical"
        ),
        "require_manual_review_when_proof_impossible": (
            "require_manual_review_when_proof_impossible"
        ),
        "minimum_independent_verifier_capability": (
            "minimum_independent_verifier_capability"
        ),
        "historical_disagreement_window": "historical_disagreement_window",
    }
    for source, destination in aliases.items():
        if source in value:
            mapped[destination] = value[source]
    return AdaptiveVerificationPolicy.model_validate(mapped)


def _repository_commands(configuration: Mapping[str, Any]) -> list[list[str]]:
    configured = configuration.get("repository_validation_commands")
    if not isinstance(configured, list):
        return []
    commands: list[list[str]] = []
    for item in configured:
        if not isinstance(item, Mapping):
            continue
        argv = item.get("argv")
        if (
            isinstance(argv, list)
            and argv
            and all(isinstance(argument, str) and argument for argument in argv)
        ):
            commands.append(list(argv))
    return commands


def _explicit_commands(configuration: Mapping[str, Any], kind: str) -> list[list[str]]:
    configured = configuration.get("repository_validation_commands")
    if not isinstance(configured, list):
        return []
    commands: list[list[str]] = []
    for item in configured:
        if not isinstance(item, Mapping) or item.get("verification_kind") != kind:
            continue
        argv = item.get("argv")
        if (
            isinstance(argv, list)
            and argv
            and all(isinstance(argument, str) and argument for argument in argv)
        ):
            commands.append(list(argv))
    return commands


def _matches_configured_path(path: str, patterns: Sequence[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(
        fnmatchcase(normalized.casefold(), pattern.replace("\\", "/").casefold())
        for pattern in patterns
    )


def _risk_tier(
    *,
    task: str,
    success_criteria: str,
    classification: Mapping[str, Any],
    changed_files: Sequence[str],
    patch_lines: int,
    qualification_state: str,
    historical_failure_modes: Sequence[str],
    policy: AdaptiveVerificationPolicy,
) -> tuple[str, list[str]]:
    rank = 0
    reasons: set[str] = set()

    explicit_risk = str(classification.get("risk") or "").casefold()
    if explicit_risk == "high":
        rank = 2
        reasons.add("explicit_high_risk")
    elif explicit_risk == "medium":
        rank = max(rank, 1)
        reasons.add("explicit_medium_risk")
    elif explicit_risk == "low":
        reasons.add("explicit_low_risk")
    else:
        rank = max(rank, 1)
        reasons.add("task_risk_unknown")

    text = f"{task}\n{success_criteria}".casefold()
    if any(signal in text for signal in _SECURITY_SIGNALS):
        rank = 2
        reasons.add("security_implication")
    if any(signal in text for signal in _DESTRUCTIVE_SIGNALS):
        rank = 2
        reasons.add("destructive_implication")
    if any(signal in text for signal in _MIGRATION_SIGNALS):
        rank = 2
        reasons.add("data_migration_indicator")

    if any(
        _matches_configured_path(path, policy.configured_sensitive_paths)
        for path in changed_files
    ):
        rank = 2
        reasons.add("configured_sensitive_path_changed")

    if patch_lines > policy.elevated_patch_line_limit:
        rank = 2
        reasons.add("critical_diff_scope")
    elif patch_lines > policy.standard_patch_line_limit:
        rank = max(rank, 1)
        reasons.add("elevated_diff_scope")
    if len(set(changed_files)) > policy.elevated_changed_file_limit:
        rank = 2
        reasons.add("critical_file_scope")
    elif len(set(changed_files)) > policy.standard_changed_file_limit:
        rank = max(rank, 1)
        reasons.add("elevated_file_scope")

    historical = {item.casefold() for item in historical_failure_modes}
    if historical.intersection(_HISTORICAL_CRITICAL_SIGNALS):
        rank = 2
        reasons.add("historical_verification_breach")
    elif historical.intersection(_HISTORICAL_ELEVATED_SIGNALS):
        rank = max(rank, 1)
        reasons.add("historical_verifier_uncertainty")

    if qualification_state in {"experimental", "unsupported", "unknown"}:
        rank = max(rank, 1)
        reasons.add("qualification_uncertain")
    elif qualification_state == "provisional":
        rank = max(rank, 1)
        reasons.add("qualification_provisional")

    return ("standard", "elevated", "critical")[rank], sorted(reasons)


def build_adaptive_verification_plan(
    *,
    run_id: str,
    attempt_id: str,
    task: str,
    success_criteria: str,
    classification: Mapping[str, Any],
    changed_files: Sequence[str],
    candidate_patch: str,
    requirement_ids: Sequence[str],
    policy_configuration: Mapping[str, Any],
    qualification_state: str = "unknown",
    historical_failure_modes: Sequence[str] = (),
    created_at: datetime | None = None,
) -> AdaptiveVerificationPlan:
    """Build the minimum deterministic graph warranted by pre-verification facts."""

    policy = policy_from_configuration(policy_configuration)
    repository_commands = _repository_commands(policy_configuration)
    changed_test_commands = _explicit_commands(
        policy_configuration, "changed_test_execution"
    )
    static_commands = _explicit_commands(policy_configuration, "static_check")
    changed = sorted(set(str(item) for item in changed_files if str(item)))
    requirements = sorted(set(str(item) for item in requirement_ids if str(item)))
    historical = sorted(
        set(str(item) for item in historical_failure_modes if str(item))
    )
    normalized_qualification = (
        qualification_state
        if qualification_state
        in {"qualified", "provisional", "experimental", "unsupported"}
        else "unknown"
    )
    tier, reasons = _risk_tier(
        task=task,
        success_criteria=success_criteria,
        classification=classification,
        changed_files=changed,
        patch_lines=len(candidate_patch.splitlines()),
        qualification_state=normalized_qualification,
        historical_failure_modes=historical,
        policy=policy,
    )
    independent = bool(
        tier == "critical" and policy.require_independent_verifier_for_critical
    )

    nodes = [
        VerificationPlanNode(
            node_id="node_diff_integrity",
            kind="diff_integrity",
            disposition="required",
            reason="The recorded candidate diff must be internally consistent and scoped.",
            evidence_requirements=["recorded_candidate_patch"],
        ),
        VerificationPlanNode(
            node_id="node_generated_artifact_exclusion",
            kind="generated_artifact_exclusion",
            disposition="required",
            reason="Probe-only and configured generated artifacts must be excluded.",
            depends_on=["node_diff_integrity"],
            evidence_requirements=["artifact_exclusion_report"],
        ),
        VerificationPlanNode(
            node_id="node_requirement_mapping",
            kind="requirement_mapping",
            disposition="required",
            reason="Every extracted requirement needs acceptance-grade evidence.",
            depends_on=["node_diff_integrity"],
            evidence_requirements=requirements,
        ),
        VerificationPlanNode(
            node_id="node_repository_validation",
            kind="repository_validation",
            disposition="required" if repository_commands else "conditional",
            reason=(
                "Run repository-discovered validation commands in the candidate environment."
                if repository_commands
                else "No explicit repository validation command was available; proof must come from other authoritative evidence."
            ),
            depends_on=["node_diff_integrity"],
            repository_commands=repository_commands,
            evidence_requirements=["authoritative_repository_validation"],
        ),
        VerificationPlanNode(
            node_id="node_changed_test_execution",
            kind="changed_test_execution",
            disposition="required" if changed_test_commands else "conditional",
            reason=(
                "Run repository-policy commands explicitly tagged for changed tests."
                if changed_test_commands
                else "No generic repository-policy command was tagged for changed-test execution."
            ),
            depends_on=["node_repository_validation"],
            repository_commands=changed_test_commands,
            evidence_requirements=["changed_test_evidence"],
        ),
        VerificationPlanNode(
            node_id="node_static_checks",
            kind="static_checks",
            disposition="required" if static_commands else "conditional",
            reason=(
                "Run repository-policy commands explicitly tagged as static checks."
                if static_commands
                else "No generic repository-policy command was tagged as a static check."
            ),
            depends_on=["node_repository_validation"],
            repository_commands=static_commands,
            evidence_requirements=["static_check_evidence"],
        ),
        VerificationPlanNode(
            node_id="node_focused_probe",
            kind="focused_probe",
            disposition="conditional",
            reason="Add only a precise probe for behavior not proved by repository-native validation.",
            depends_on=["node_requirement_mapping", "node_repository_validation"],
            evidence_requirements=["unproved_requirement_only"],
        ),
        VerificationPlanNode(
            node_id="node_semantic_verifier",
            kind="semantic_verifier",
            disposition="required",
            reason="Semantic verification is mandatory before acceptance.",
            depends_on=[
                "node_diff_integrity",
                "node_generated_artifact_exclusion",
                "node_requirement_mapping",
            ],
            evidence_requirements=["binary_semantic_verdict"],
            estimated_model_calls=1,
        ),
        VerificationPlanNode(
            node_id="node_independent_second_verifier",
            kind="independent_second_verifier",
            disposition="required" if independent else "conditional",
            reason=(
                "Critical risk requires an independent semantic verifier."
                if independent
                else "A second verifier is reserved for disagreement, unclear evidence, or elevated historical risk."
            ),
            depends_on=["node_semantic_verifier"],
            evidence_requirements=["independent_binary_verdict"],
            estimated_model_calls=1 if independent else 0,
        ),
        VerificationPlanNode(
            node_id="node_manual_review",
            kind="manual_review",
            disposition="conditional",
            reason="Request only the exact unresolved decision when automated proof is impossible.",
            depends_on=["node_semantic_verifier"],
            evidence_requirements=["explicit_human_outcome_if_invoked"],
            estimated_model_calls=0,
        ),
    ]

    input_value = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "task_digest": canonical_digest(task),
        "criteria_digest": canonical_digest(success_criteria),
        "candidate_diff_digest": canonical_digest(candidate_patch),
        "classification": dict(classification),
        "changed_files": changed,
        "requirement_ids": requirements,
        "qualification_state": normalized_qualification,
        "historical_failure_modes": historical,
        "repository_commands": repository_commands,
        "policy": policy.model_dump(mode="json"),
        "risk_tier": tier,
        "risk_reasons": reasons,
    }
    input_digest = canonical_digest(input_value)
    plan_id = "avp_" + input_digest.removeprefix("sha256:")
    return AdaptiveVerificationPlan(
        plan_id=plan_id,
        run_id=run_id,
        attempt_id=attempt_id,
        policy_digest=canonical_digest(policy.model_dump(mode="json")),
        created_at=created_at or datetime.now(timezone.utc),
        risk_tier=tier,  # type: ignore[arg-type]
        risk_reasons=reasons,
        task_digest=canonical_digest(task),
        criteria_digest=canonical_digest(success_criteria),
        candidate_diff_digest=canonical_digest(candidate_patch),
        changed_files=changed,
        requirement_ids=requirements,
        qualification_state=normalized_qualification,  # type: ignore[arg-type]
        historical_failure_modes=historical,
        nodes=nodes,
        independent_verifier_required=independent,
        manual_review_if_unresolved=policy.require_manual_review_when_proof_impossible,
        semantic_context_allowlist=SEMANTIC_CONTEXT_ALLOWLIST,
        semantic_context_excluded=SEMANTIC_CONTEXT_EXCLUDED,
        deterministic_input_digest=input_digest,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
