"""Repository-specific scorecards and the fail-closed PT7 Gate C."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from villani_ops.core.backend import Backend

from ..agent_systems.models import AgentSystemIdentity, CapabilityState
from .models import (
    GateCCheck,
    GateCReport,
    QualificationAssessment,
    QualificationPolicy,
    QualificationScorecard,
    QualificationTaskProfile,
)
from .policy import assess_qualification
from .repository import RepositoryQualificationContext, exact_conformance_status
from .store import QualificationStore, qualification_policy_from_configuration


REQUIRED_GATE_C_HARNESSES = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "villani-code": "Villani Code",
}


def _scorecard(
    identity: AgentSystemIdentity, assessment: QualificationAssessment
) -> QualificationScorecard:
    stats = assessment.statistics
    return QualificationScorecard(
        system_name=identity.harness.display_name,
        harness=f"{identity.harness.harness_id}@{identity.harness.version}",
        model=identity.model_provider.model_id,
        provider=identity.model_provider.provider,
        assessment=assessment,
        accepted_as_is=stats.accepted_as_is_count,
        proved_acceptable=stats.proved_acceptable_count,
        false_cases=len(stats.false_case_ids),
        known_cost=any(
            item.known_count > 0
            for item in stats.cost_distribution_by_currency.values()
        ),
        known_duration=stats.duration_distribution.known_count > 0,
        known_review_time=stats.review_minutes_distribution.known_count > 0,
        failures=stats.failures,
    )


def _system_checks(
    identity: AgentSystemIdentity,
    assessment: QualificationAssessment,
    policy: QualificationPolicy,
) -> list[GateCCheck]:
    checks: list[GateCCheck] = []
    sample_count = assessment.statistics.sample_count
    checks.append(
        GateCCheck(
            check_id="evidence_parity",
            system_id=identity.system_id,
            status="pass" if sample_count > 0 else "insufficient_evidence",
            actual={
                "sample_count": sample_count,
                "known_cost": bool(assessment.statistics.cost_distribution_by_currency),
                "known_duration_count": (
                    assessment.statistics.duration_distribution.known_count
                ),
                "unknown_duration_count": (
                    assessment.statistics.duration_distribution.unknown_count
                ),
            },
            required=(
                "At least one eligible observation with truthful known/unknown "
                "cost, duration, review, failure, and false-case accounting."
            ),
            reason=(
                "Comparable evidence is present and unknown accounting remains explicit."
                if sample_count > 0
                else "No eligible observation matches this complete system identity."
            ),
        )
    )
    identity_drift = [
        item
        for item in assessment.statistics.drift_flags
        if item.code
        in {
            "model_identity_change",
            "harness_incompatibility",
            "provider_identity_change",
            "execution_environment_change",
            "verification_policy_change",
        }
        and item.severity == "severe"
    ]
    checks.append(
        GateCCheck(
            check_id="exact_identity",
            system_id=identity.system_id,
            status=(
                "fail"
                if identity_drift
                else "pass"
                if sample_count > 0
                else "insufficient_evidence"
            ),
            actual={
                "system_id": identity.system_id,
                "identity_drift": [item.code for item in identity_drift],
            },
            required="Exact harness, model/provider, execution, policy, and software identity.",
            reason=(
                "Selected evidence has the exact complete identity."
                if sample_count > 0 and not identity_drift
                else "Material identity drift is present."
                if identity_drift
                else "Exact-identity evidence is absent."
            ),
        )
    )
    conformance = exact_conformance_status(identity)
    checks.append(
        GateCCheck(
            check_id="conformance",
            system_id=identity.system_id,
            status=(
                "pass"
                if conformance == "passed"
                else "fail"
                if conformance == "failed"
                else "insufficient_evidence"
            ),
            actual=conformance,
            required="passed for the exact configured harness protocol and model",
            reason=(
                "Exact conformance passed."
                if conformance == "passed"
                else "Exact conformance failed."
                if conformance == "failed"
                else "Exact conformance has not been proved."
            ),
        )
    )
    isolated = identity.capabilities.get("isolated_worktree")
    isolation_passed = bool(
        isolated is not None and isolated.state == CapabilityState.SUPPORTED
    )
    checks.append(
        GateCCheck(
            check_id="isolation",
            system_id=identity.system_id,
            status="pass" if isolation_passed else "fail",
            actual=isolated.state.value if isolated is not None else "absent",
            required="supported with evidence",
            reason=(
                "The system contract requires isolated candidate worktrees."
                if isolation_passed
                else "Worktree isolation is absent or unproved."
            ),
        )
    )
    unsupported_correct = bool(
        assessment.state not in {"unsupported", "experimental"}
        or not assessment.automatic_eligible
    )
    checks.append(
        GateCCheck(
            check_id="unsupported_behavior",
            system_id=identity.system_id,
            status="pass" if unsupported_correct else "fail",
            actual={
                "state": assessment.state,
                "automatic_eligible": assessment.automatic_eligible,
            },
            required="unsupported and experimental systems are never automatic routes",
            reason=(
                "Unsupported behavior fails closed."
                if unsupported_correct
                else "An unsupported system was exposed as an automatic route."
            ),
        )
    )
    selected_backoff = next(
        (item for item in assessment.backoff_evidence if item.selected),
        None,
    )
    qualified_requirements = bool(
        assessment.statistics.sample_count >= policy.minimum_qualified_observations
        and assessment.statistics.false_acceptance_count == 0
        and assessment.statistics.wilson_lower_bound is not None
        and assessment.statistics.wilson_lower_bound > assessment.task_wilson_threshold
        and selected_backoff is not None
        and selected_backoff.approved_for_qualification
        and not any(
            item.severity in {"severe", "unsupported"}
            for item in assessment.statistics.drift_flags
        )
        and not any(
            item.code == "stale_evidence" for item in assessment.statistics.drift_flags
        )
        and not assessment.unsupported_reasons
        and exact_conformance_status(identity) == "passed"
    )
    expected_state = (
        "unsupported"
        if assessment.unsupported_reasons
        else "experimental"
        if assessment.statistics.sample_count == 0
        or exact_conformance_status(identity) != "passed"
        or assessment.statistics.false_acceptance_count > 0
        or any(item.severity == "severe" for item in assessment.statistics.drift_flags)
        else "qualified"
        if qualified_requirements
        else "provisional"
    )
    state_correct = bool(
        assessment.state == expected_state
        and (assessment.state != "qualified" or assessment.automatic_eligible)
        and (assessment.state == "qualified" or not assessment.automatic_eligible)
        and (
            assessment.state != "provisional"
            or assessment.provisional_fallback_eligible
        )
        and (assessment.state != "experimental" or not assessment.automatic_eligible)
        and (assessment.state != "unsupported" or not assessment.automatic_eligible)
    )
    qualification_check_status = (
        "fail"
        if not state_correct
        else "pass"
        if assessment.state == "qualified"
        else "insufficient_evidence"
    )
    checks.append(
        GateCCheck(
            check_id="qualification_correctness",
            system_id=identity.system_id,
            status=qualification_check_status,
            actual={
                "state": assessment.state,
                "expected_state": expected_state,
                "sample_count": assessment.statistics.sample_count,
                "false_acceptance_count": (
                    assessment.statistics.false_acceptance_count
                ),
                "wilson_lower_bound": assessment.statistics.wilson_lower_bound,
                "threshold": assessment.task_wilson_threshold,
            },
            required=(
                f"PT7 policy: {policy.minimum_qualified_observations}+ approved "
                "observations, zero false acceptance, "
                "Wilson above threshold, valid conformance, and no severe drift"
            ),
            reason=(
                "The system is qualified under the versioned repository policy."
                if qualification_check_status == "pass"
                else "The derived state is safe but evidence is not yet sufficient for qualification."
                if qualification_check_status == "insufficient_evidence"
                else "The derived state violates the versioned qualification policy."
            ),
        )
    )
    return checks


def build_gate_c_report(
    *,
    identities: Iterable[AgentSystemIdentity],
    backends: Mapping[str, Backend],
    repository: RepositoryQualificationContext,
    requested_task: QualificationTaskProfile,
    configuration: Mapping[str, Any],
    store: QualificationStore,
    generated_at: datetime | None = None,
) -> GateCReport:
    now = generated_at or datetime.now(timezone.utc)
    policy = qualification_policy_from_configuration(configuration)
    system_identities = tuple(sorted(identities, key=lambda item: item.route_name))
    scorecards: list[QualificationScorecard] = []
    checks: list[GateCCheck] = []
    for identity in system_identities:
        backend = next(
            (
                value
                for value in backends.values()
                if value.name == identity.route_name
                or value.model == identity.model_provider.model_id
                and value.provider == identity.model_provider.provider
            ),
            None,
        )
        assessment = assess_qualification(
            identity=identity,
            repository=repository,
            requested_task=requested_task,
            configuration=configuration,
            store=store,
            backend_execution_selection=(
                backend.execution_environment if backend is not None else None
            ),
            policy=policy,
            evaluated_at=now,
        )
        scorecards.append(_scorecard(identity, assessment))
        checks.extend(_system_checks(identity, assessment, policy))

    present_harnesses = {identity.harness.harness_id for identity in system_identities}
    missing_harnesses = sorted(set(REQUIRED_GATE_C_HARNESSES) - present_harnesses)
    checks.append(
        GateCCheck(
            check_id="required_scorecards",
            status=("pass" if not missing_harnesses else "insufficient_evidence"),
            actual={
                "present": sorted(present_harnesses),
                "missing": missing_harnesses,
            },
            required={
                harness_id: display_name
                for harness_id, display_name in sorted(
                    REQUIRED_GATE_C_HARNESSES.items()
                )
            },
            reason=(
                "Repository-specific scorecards cover every PT7 system."
                if not missing_harnesses
                else "Gate C requires scorecards for every PT7 system; missing: "
                + ", ".join(missing_harnesses)
            ),
        )
    )

    states = {card.assessment.state for card in scorecards}
    automatic = [card for card in scorecards if card.assessment.automatic_eligible]
    nonqualified_automatic = [
        card
        for card in scorecards
        if card.assessment.state != "qualified" and card.assessment.automatic_eligible
    ]
    checks.append(
        GateCCheck(
            check_id="automatic_routing_eligibility",
            status="fail" if nonqualified_automatic else "pass",
            actual={
                "qualified_routes": [card.assessment.route_name for card in automatic],
                "nonqualified_automatic_routes": [
                    card.assessment.route_name for card in nonqualified_automatic
                ],
            },
            required="automatic routes are qualified; experimental routes require manual override",
            reason=(
                "Automatic routing eligibility is fail closed."
                if not nonqualified_automatic
                else "A non-qualified system was made automatically eligible."
            ),
        )
    )
    statuses = {item.status for item in checks}
    status = (
        "FAIL"
        if "fail" in statuses
        else "INSUFFICIENT_EVIDENCE"
        if "insufficient_evidence" in statuses
        else "PASS"
    )
    sample_counts = {card.assessment.statistics.sample_count for card in scorecards}
    selected_levels = {card.assessment.selected_level for card in scorecards}
    currencies = {
        currency
        for card in scorecards
        for currency in card.assessment.statistics.cost_distribution_by_currency
    }
    warning_parts: list[str] = []
    if len(sample_counts) > 1:
        warning_parts.append("sample counts are materially unmatched")
    if len(selected_levels) > 1:
        warning_parts.append("backoff levels differ")
    if len(currencies) > 1:
        warning_parts.append("known costs use different currencies")
    if any(count == 0 for count in sample_counts):
        warning_parts.append("one or more systems have no eligible sample")
    if "experimental" in states:
        warning_parts.append("experimental systems are not ranked")
    snapshot = store.rebuild(policy=policy, generated_at=now)
    return GateCReport(
        generated_at=now,
        repository_id=repository.repository_id,
        repository_head=repository.head,
        task_profile=requested_task,
        policy_version=policy.policy_version,
        status=status,
        checks=checks,
        scorecards=scorecards,
        unmatched_sample_warning=(
            "; ".join(warning_parts) + "." if warning_parts else None
        ),
        evidence_snapshot_digest=snapshot.snapshot_digest,
    )


__all__ = ["build_gate_c_report"]
