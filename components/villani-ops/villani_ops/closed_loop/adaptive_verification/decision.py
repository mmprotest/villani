"""Fail-closed projection from verifier output into the PT9 binary contract."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..interfaces import AttemptContext, AttemptResult, Verification
from .models import (
    AdaptiveVerificationPlan,
    BinaryVerificationDecision,
    MoneyAccounting,
    RestrictedVerifierProvenance,
    VerificationNodeResult,
    canonical_digest,
)


def _verification_cost(
    usage: Sequence[Mapping[str, Any]], *, semantic_invoked: bool
) -> MoneyAccounting:
    if not semantic_invoked and not usage:
        return MoneyAccounting(
            amount=None,
            currency=None,
            accounting_status="not_applicable",
            source="semantic_verifier_not_invoked",
        )
    known: list[float] = []
    currencies: set[str] = set()
    unknown = False
    applicable = False
    for item in usage:
        status = str(item.get("cost_accounting_status") or "unknown")
        value = item.get("cost")
        if status == "not_applicable":
            continue
        applicable = True
        if status in {"complete", "partial"} and isinstance(value, (int, float)):
            known.append(float(value))
            currency = item.get("currency")
            if isinstance(currency, str) and currency:
                currencies.add(currency.upper())
            if status == "partial":
                unknown = True
        else:
            unknown = True
    if not applicable:
        return MoneyAccounting(
            amount=None,
            currency=None,
            accounting_status="not_applicable",
            source="verifier_usage_not_billable",
        )
    if len(currencies) > 1:
        return MoneyAccounting(
            amount=None,
            currency=None,
            accounting_status="unknown",
            source="mixed_verifier_currencies",
        )
    if known:
        return MoneyAccounting(
            amount=sum(known),
            currency=next(iter(currencies), "USD"),
            accounting_status="partial" if unknown else "complete",
            source="authoritative_verifier_usage",
        )
    return MoneyAccounting(
        amount=None,
        currency=None,
        accounting_status="unknown",
        source="verifier_cost_unavailable",
    )


def _node_disposition(plan: AdaptiveVerificationPlan, node_id: str) -> str:
    return next(item.disposition for item in plan.nodes if item.node_id == node_id)


def _result(
    node_id: str,
    status: str,
    reason: str,
    *,
    commands: Sequence[Sequence[str]] = (),
    evidence_paths: Sequence[str] = (),
) -> VerificationNodeResult:
    return VerificationNodeResult(
        node_id=node_id,
        status=status,  # type: ignore[arg-type]
        reason=reason,
        commands=[list(item) for item in commands],
        evidence_paths=sorted(set(str(item) for item in evidence_paths if item)),
    )


def _node_results(
    *,
    plan: AdaptiveVerificationPlan,
    attempt_result: AttemptResult,
    verification: Verification,
    semantic_status: str,
    independent_completed: bool,
) -> list[VerificationNodeResult]:
    metadata = verification.metadata
    evidence_path = str(metadata.get("verification_evidence_path") or "")
    repository_path = str(metadata.get("repository_validation_path") or "")
    focused_path = str(metadata.get("focused_probe_report_path") or "")
    eligibility = str(metadata.get("candidate_eligibility_status") or "unknown")
    repository_status = str(metadata.get("repository_validation_status") or "")
    focused_status = str(metadata.get("focused_probe_status") or "")
    required_commands = next(
        item.repository_commands
        for item in plan.nodes
        if item.node_id == "node_repository_validation"
    )
    changed_commands = next(
        item.repository_commands
        for item in plan.nodes
        if item.node_id == "node_changed_test_execution"
    )
    static_commands = next(
        item.repository_commands
        for item in plan.nodes
        if item.node_id == "node_static_checks"
    )

    diff_passed = eligibility == "eligible" and bool(attempt_result.patch)
    output = [
        _result(
            "node_diff_integrity",
            "passed" if diff_passed else "failed",
            (
                "The recorded candidate patch passed deterministic eligibility checks."
                if diff_passed
                else "The candidate patch failed deterministic eligibility or integrity checks."
            ),
            evidence_paths=[evidence_path],
        ),
        _result(
            "node_generated_artifact_exclusion",
            "passed" if diff_passed else "failed",
            (
                "Candidate quality evidence found no blocking generated artifact."
                if diff_passed
                else "Artifact exclusion could not be proved for an ineligible candidate."
            ),
            evidence_paths=[
                str(metadata.get("candidate_patch_quality_path") or ""),
                evidence_path,
            ],
        ),
    ]
    requirement_failures = [
        item.requirement_id
        for item in verification.requirement_results
        if item.outcome != "passed"
    ]
    output.append(
        _result(
            "node_requirement_mapping",
            "passed"
            if verification.requirement_results and not requirement_failures
            else "failed",
            (
                "Every extracted requirement is mapped to passing evidence."
                if verification.requirement_results and not requirement_failures
                else "One or more extracted requirements lack passing evidence."
            ),
            evidence_paths=[evidence_path],
        )
    )

    if repository_status == "passed":
        repository_node_status = "passed"
        repository_reason = "Authoritative repository validation passed."
    elif repository_status == "failed":
        repository_node_status = "failed"
        repository_reason = "Authoritative repository validation failed."
    elif repository_status == "infrastructure_error":
        repository_node_status = "infrastructure_error"
        repository_reason = "Repository validation encountered infrastructure failure."
    elif _node_disposition(plan, "node_repository_validation") == "required":
        repository_node_status = "unavailable"
        repository_reason = "Required repository validation was unavailable."
    else:
        repository_node_status = "not_applicable"
        repository_reason = "No repository validation command was required by policy."
    output.append(
        _result(
            "node_repository_validation",
            repository_node_status,
            repository_reason,
            commands=required_commands,
            evidence_paths=[repository_path],
        )
    )

    for node_id, commands, label in (
        ("node_changed_test_execution", changed_commands, "changed-test"),
        ("node_static_checks", static_commands, "static-check"),
    ):
        if commands and repository_status == "passed":
            status = "passed"
            reason = f"Repository-native {label} command evidence passed."
        elif commands and repository_status == "failed":
            status = "failed"
            reason = f"Repository-native {label} command evidence failed."
        elif commands and repository_status == "infrastructure_error":
            status = "infrastructure_error"
            reason = f"Repository-native {label} command infrastructure failed."
        elif commands:
            status = "unavailable"
            reason = f"Repository-native {label} command evidence was unavailable."
        else:
            status = "not_applicable"
            reason = f"No repository policy command required a separate {label} node."
        output.append(
            _result(
                node_id,
                status,
                reason,
                commands=commands,
                evidence_paths=[repository_path],
            )
        )

    pending_probes = bool(metadata.get("focused_probe_requests_pending"))
    if focused_status == "passed":
        focused_node_status = "passed"
        focused_reason = "The isolated focused behavior probe passed."
    elif focused_status == "failed":
        focused_node_status = "failed"
        focused_reason = "The isolated focused behavior probe failed."
    elif focused_status == "infrastructure_error":
        focused_node_status = "infrastructure_error"
        focused_reason = "The focused probe failed because of infrastructure."
    elif pending_probes:
        focused_node_status = "not_run"
        focused_reason = "A required focused probe is pending."
    else:
        focused_node_status = "not_applicable"
        focused_reason = (
            "Repository and semantic evidence did not require a focused probe."
        )
    output.append(
        _result(
            "node_focused_probe",
            focused_node_status,
            focused_reason,
            evidence_paths=[focused_path],
        )
    )

    output.append(
        _result(
            "node_semantic_verifier",
            "passed" if semantic_status == "passed" else "failed",
            (
                "Semantic verification returned a valid binary success."
                if semantic_status == "passed"
                else f"Semantic verification normalized to {semantic_status}."
            ),
            evidence_paths=[verification.raw_verifier_artifact or "", evidence_path],
        )
    )
    if plan.independent_verifier_required:
        independent_status = "passed" if independent_completed else "not_run"
        independent_reason = (
            "An independent semantic verifier confirmed the decision."
            if independent_completed
            else "The required independent semantic verifier did not confirm the decision."
        )
    else:
        independent_status = "not_applicable"
        independent_reason = "This risk tier did not require a second verifier."
    output.append(
        _result(
            "node_independent_second_verifier",
            independent_status,
            independent_reason,
            evidence_paths=[evidence_path],
        )
    )
    output.append(
        _result(
            "node_manual_review",
            "not_applicable" if verification.acceptance_eligible else "not_run",
            (
                "Automated proof completed; manual review was not required."
                if verification.acceptance_eligible
                else "Manual review is available for the exact unresolved decision."
            ),
        )
    )
    return output


def binary_decision_from_verification(
    *,
    plan: AdaptiveVerificationPlan,
    attempt_context: AttemptContext,
    attempt_result: AttemptResult,
    verification: Verification,
    decided_at: datetime | None = None,
) -> BinaryVerificationDecision:
    """Normalize every unclear/error path to zero and retain restricted audit data."""

    metadata = verification.metadata
    semantic_invoked = bool(metadata.get("semantic_verifier_invoked"))
    raw_semantic = str(metadata.get("semantic_verifier_status") or "").casefold()
    if (
        semantic_invoked
        and verification.outcome == "accepted"
        and raw_semantic
        in {
            "success",
            "passed",
        }
    ):
        semantic_status = "passed"
    elif not semantic_invoked:
        semantic_status = "not_invoked"
    elif verification.outcome == "unclear" or raw_semantic == "unclear":
        semantic_status = "unclear"
    elif verification.outcome == "error" or raw_semantic in {"error", ""}:
        semantic_status = "error"
    else:
        semantic_status = "failed"

    calls = metadata.get("verifier_calls")
    call_items = calls if isinstance(calls, list) else []
    independent_completed = bool(
        metadata.get("independent_verifier_completed")
        or (
            plan.independent_verifier_required
            and len(call_items) >= 2
            and verification.outcome == "accepted"
            and not bool(metadata.get("verifier_disagreement"))
        )
    )
    node_results = _node_results(
        plan=plan,
        attempt_result=attempt_result,
        verification=verification,
        semantic_status=semantic_status,
        independent_completed=independent_completed,
    )
    proved = sorted(
        item.requirement_id
        for item in verification.requirement_results
        if item.outcome == "passed"
    )
    not_proved = sorted(
        item.requirement_id
        for item in verification.requirement_results
        if item.outcome != "passed"
    )
    blockers = sorted(
        set(
            [
                item.reason
                for item in node_results
                if item.status
                in {"failed", "unavailable", "infrastructure_error", "not_run"}
                and not (
                    item.node_id == "node_manual_review"
                    or (
                        item.node_id == "node_independent_second_verifier"
                        and not plan.independent_verifier_required
                    )
                )
            ]
            + [
                str(item)
                for item in verification.risk_flags
                if "acceptance_blocker" in str(item).casefold()
            ]
        )
    )
    repository_status = str(metadata.get("repository_validation_status") or "")
    if repository_status == "infrastructure_error":
        infrastructure_status = "infrastructure_failure"
    elif (
        repository_status == "unavailable"
        and _node_disposition(plan, "node_repository_validation") == "required"
    ):
        infrastructure_status = "unavailable"
    else:
        infrastructure_status = "resolved"

    accepts = bool(
        verification.acceptance_eligible
        and verification.outcome == "accepted"
        and semantic_status == "passed"
        and not not_proved
        and not blockers
        and infrastructure_status == "resolved"
        and (not plan.independent_verifier_required or independent_completed)
    )
    computed_reason_code = str(metadata.get("computed_final_reason_code") or "")
    accepting_reason_codes = {
        "",
        "accepted",
        "proved_acceptable",
        "success",
        "verification_passed",
    }
    if accepts:
        reason_code = "proved_acceptable"
        reason = "Required deterministic, repository, requirement, and semantic proof passed."
    elif infrastructure_status != "resolved":
        reason_code = "verification_infrastructure_unresolved"
        reason = "Verification infrastructure did not produce authoritative evidence."
    elif plan.independent_verifier_required and not independent_completed:
        reason_code = "independent_verifier_required"
        reason = "Critical-risk acceptance requires an independent verifier result."
    elif computed_reason_code not in accepting_reason_codes:
        reason_code = computed_reason_code
        reason = verification.reason or "Acceptance-grade proof was incomplete."
    elif semantic_status in {"unclear", "error", "not_invoked"}:
        reason_code = f"semantic_{semantic_status}"
        reason = (
            "Semantic verification did not return acceptance-grade binary proof; "
            "the result normalized to zero."
        )
    else:
        reason_code = computed_reason_code or "requirements_not_proved"
        reason = verification.reason or "Acceptance-grade proof was incomplete."

    invocation_status = str(metadata.get("invocation_status") or "error")
    if invocation_status not in {
        "completed",
        "not_invoked",
        "malformed_output",
        "timeout",
        "error",
    }:
        invocation_status = "error"
    primary_identity = canonical_digest(
        {
            "verifier": verification.verifier,
            "version": metadata.get("verifier_version"),
        }
    )
    provenance = [
        RestrictedVerifierProvenance(
            verifier_role="semantic",
            verifier_identity_digest=primary_identity,
            invocation_status=invocation_status,  # type: ignore[arg-type]
            independent=False,
            artifact_path=verification.raw_verifier_artifact,
        )
    ]
    if independent_completed:
        second_call = call_items[-1] if call_items else {}
        provenance.append(
            RestrictedVerifierProvenance(
                verifier_role="independent_semantic",
                verifier_identity_digest=canonical_digest(
                    {
                        "backend": second_call.get("backend"),
                        "model": second_call.get("model"),
                    }
                ),
                invocation_status="completed",
                independent=True,
                artifact_path=str(
                    second_call.get("artifact_path")
                    or metadata.get("verification_evidence_path")
                    or ""
                )
                or None,
            )
        )

    normalized_from = (
        verification.outcome
        if verification.outcome in {"accepted", "rejected", "unclear", "error"}
        else "deterministic_failure"
    )
    timestamp = decided_at or datetime.now(timezone.utc)
    identity_value = {
        "plan_id": plan.plan_id,
        "run_id": attempt_context.run_id,
        "attempt_id": attempt_context.attempt_id,
        "decision": 1 if accepts else 0,
        "reason_code": reason_code,
        "requirements_proved": proved,
        "requirements_not_proved": not_proved,
        "blockers": blockers,
        "semantic_status": semantic_status,
        "independent_completed": independent_completed,
    }
    return BinaryVerificationDecision(
        decision_id="avd_" + canonical_digest(identity_value).removeprefix("sha256:"),
        run_id=attempt_context.run_id,
        attempt_id=attempt_context.attempt_id,
        plan_id=plan.plan_id,
        decided_at=timestamp,
        decision=1 if accepts else 0,
        reason_code=reason_code,
        reason=reason,
        requirements_proved=proved,
        requirements_not_proved=not_proved,
        blockers=blockers,
        infrastructure_status=infrastructure_status,  # type: ignore[arg-type]
        semantic_status=semantic_status,  # type: ignore[arg-type]
        independent_verifier_required=plan.independent_verifier_required,
        independent_verifier_completed=independent_completed,
        node_results=node_results,
        verifier_provenance=provenance,
        verification_cost=_verification_cost(
            verification.llm_usage, semantic_invoked=semantic_invoked
        ),
        normalized_from=normalized_from,  # type: ignore[arg-type]
    )
