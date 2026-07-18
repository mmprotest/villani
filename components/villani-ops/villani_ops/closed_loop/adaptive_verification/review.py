"""Compact review-package construction for ordinary and unresolved changes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from ..interfaces import AttemptContext, AttemptResult, Verification
from .models import (
    AdaptiveVerificationPlan,
    BinaryVerificationDecision,
    CompactReviewPackage,
    DurationAccounting,
    MoneyAccounting,
    ReviewCheck,
    canonical_digest,
)


def _attempt_cost(result: AttemptResult) -> MoneyAccounting:
    if (
        result.cost_accounting_status in {"complete", "partial"}
        and result.cost_usd is not None
    ):
        return MoneyAccounting(
            amount=float(result.cost_usd),
            currency="USD",
            accounting_status=result.cost_accounting_status,
            source="candidate_attempt_telemetry",
        )
    return MoneyAccounting(
        amount=None,
        currency=None,
        accounting_status=(
            "not_applicable"
            if result.cost_accounting_status == "not_applicable"
            else "unknown"
        ),
        source="candidate_attempt_cost_unavailable",
    )


def _attempt_duration(result: AttemptResult) -> DurationAccounting:
    if (
        result.duration_accounting_status in {"complete", "partial"}
        and result.duration_ms is not None
    ):
        return DurationAccounting(
            duration_ms=result.duration_ms,
            accounting_status=result.duration_accounting_status,
            source="candidate_attempt_telemetry",
        )
    return DurationAccounting(
        duration_ms=None,
        accounting_status=(
            "not_applicable"
            if result.duration_accounting_status == "not_applicable"
            else "unknown"
        ),
        source="candidate_attempt_duration_unavailable",
    )


def _check_status(value: str) -> str:
    if value == "infrastructure_error":
        return "infrastructure_error"
    if value in {"passed", "failed", "not_run", "unavailable"}:
        return value
    return "not_run"


def _changed_files(result: AttemptResult, plan: AdaptiveVerificationPlan) -> list[str]:
    raw = result.metadata.get("changed_files")
    if isinstance(raw, list):
        return sorted(set(str(item) for item in raw if str(item)))
    return list(plan.changed_files)


def build_compact_review_package(
    *,
    plan: AdaptiveVerificationPlan,
    decision: BinaryVerificationDecision,
    attempt_context: AttemptContext,
    attempt_result: AttemptResult,
    verification: Verification,
    created_at: datetime | None = None,
    full_evidence_href: str | None = None,
) -> CompactReviewPackage:
    """Present proof concisely without hiding the exact unresolved decision."""

    ready = decision.decision == 1
    checks: list[ReviewCheck] = []
    for item in decision.node_results:
        if item.status == "not_applicable" or item.node_id == "node_manual_review":
            continue
        checks.append(
            ReviewCheck(
                label=item.node_id.removeprefix("node_").replace("_", " "),
                status=_check_status(item.status),  # type: ignore[arg-type]
                evidence_path=item.evidence_paths[0] if item.evidence_paths else None,
            )
        )
    files = _changed_files(attempt_result, plan)
    change_summary = (
        f"{len(files)} file{'s' if len(files) != 1 else ''} changed in the preserved candidate."
        if files
        else "No file change was recorded in the preserved candidate."
    )
    unresolved = None
    if not ready:
        if decision.requirements_not_proved:
            unresolved = (
                "Decide whether the unproved requirements are acceptable: "
                + ", ".join(decision.requirements_not_proved)
            )
        elif decision.blockers:
            unresolved = decision.blockers[0]
        else:
            unresolved = decision.reason
    if ready:
        trust = (
            "Villani trusts this candidate because deterministic integrity checks, "
            "required repository evidence, requirement mapping, and semantic verification passed"
            + (
                ", including an independent verifier for critical risk."
                if plan.independent_verifier_required
                else "."
            )
        )
    else:
        trust = (
            "Villani does not claim the candidate is proved acceptable; delivery remains "
            "fail closed until the listed unresolved decision is resolved."
        )
    timestamp = created_at or datetime.now(timezone.utc)
    identity_value = {
        "run_id": attempt_context.run_id,
        "attempt_id": attempt_context.attempt_id,
        "decision_id": decision.decision_id,
        "status": "ready_to_apply" if ready else "needs_review",
        "files": files,
        "unresolved": unresolved,
    }
    return CompactReviewPackage(
        package_id="rvp_" + canonical_digest(identity_value).removeprefix("sha256:"),
        run_id=attempt_context.run_id,
        attempt_id=attempt_context.attempt_id,
        decision_id=decision.decision_id,
        created_at=timestamp,
        status="ready_to_apply" if ready else "needs_review",
        task=attempt_context.task,
        change_summary=change_summary,
        changed_files=files,
        requirements_proved=list(decision.requirements_proved),
        requirements_not_proved=list(decision.requirements_not_proved),
        checks=checks,
        risk_tier=plan.risk_tier,
        risk_flags=sorted(set(str(item) for item in verification.risk_flags)),
        known_cost=_attempt_cost(attempt_result),
        known_duration=_attempt_duration(attempt_result),
        why_villani_trusts_it=trust,
        unresolved_decision=unresolved,
        full_evidence_href=full_evidence_href
        or f"/console/runs/{attempt_context.run_id}/replay",
    )


def concise_check_lines(package: CompactReviewPackage) -> Sequence[str]:
    return tuple(f"{item.label}: {item.status}" for item in package.checks)
