"""Matched-evidence Gate D evaluation for adaptive verification."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .models import GateDArm, GateDCheck, GateDReport, canonical_digest


def _check(name: str, status: str, reason: str) -> GateDCheck:
    return GateDCheck(check=name, status=status, reason=reason)  # type: ignore[arg-type]


def _known_cost(arm: GateDArm) -> float | None:
    return (
        arm.total_cost.amount
        if arm.total_cost.accounting_status == "complete"
        and arm.total_cost.amount is not None
        else None
    )


def _known_duration(arm: GateDArm) -> int | None:
    return (
        arm.elapsed_duration.duration_ms
        if arm.elapsed_duration.accounting_status == "complete"
        and arm.elapsed_duration.duration_ms is not None
        else None
    )


def evaluate_gate_d(
    *,
    arms: Iterable[GateDArm],
    generated_at: datetime | None = None,
    evidence_references: Iterable[str] = (),
) -> GateDReport:
    by_strategy = {item.strategy: item for item in arms}
    missing = {
        "strongest_only",
        "accepted_change_optimizer",
        "optimizer_plus_adaptive",
    } - set(by_strategy)
    if missing:
        raise ValueError(f"Gate D arms are missing: {sorted(missing)!r}")
    strongest = by_strategy["strongest_only"]
    optimizer = by_strategy["accepted_change_optimizer"]
    adaptive = by_strategy["optimizer_plus_adaptive"]
    checks: list[GateDCheck] = []
    warnings: list[str] = []

    case_sets = {tuple(item.case_ids) for item in by_strategy.values()}
    if not adaptive.case_ids:
        checks.append(
            _check(
                "matched_founder_cases",
                "insufficient_evidence",
                "No eligible frozen founder case was recorded for all three arms.",
            )
        )
    elif len(case_sets) != 1:
        checks.append(
            _check(
                "matched_founder_cases",
                "insufficient_evidence",
                "The three policy arms do not contain the same frozen case ids.",
            )
        )
        warnings.append("Unmatched samples are not ranked.")
    else:
        checks.append(
            _check(
                "matched_founder_cases",
                "pass",
                f"All arms contain the same {len(adaptive.case_ids)} frozen cases.",
            )
        )

    if not adaptive.case_ids:
        checks.append(
            _check(
                "accepted_as_is_no_regression",
                "insufficient_evidence",
                "Accepted-as-is reliability cannot be compared without eligible cases.",
            )
        )
    elif adaptive.accepted_as_is < max(
        strongest.accepted_as_is, optimizer.accepted_as_is
    ):
        checks.append(
            _check(
                "accepted_as_is_no_regression",
                "fail",
                "Adaptive verification reduced accepted-as-is outcomes on matched cases.",
            )
        )
    else:
        checks.append(
            _check(
                "accepted_as_is_no_regression",
                "pass",
                "Adaptive verification did not reduce accepted-as-is outcomes.",
            )
        )

    if not adaptive.case_ids:
        checks.append(
            _check(
                "zero_false_acceptance",
                "insufficient_evidence",
                "Zero false acceptance cannot be evidenced by an empty evaluation.",
            )
        )
    elif adaptive.false_acceptances:
        checks.append(
            _check(
                "zero_false_acceptance",
                "fail",
                f"Adaptive verification recorded {adaptive.false_acceptances} false acceptance(s).",
            )
        )
    else:
        checks.append(
            _check(
                "zero_false_acceptance",
                "pass",
                "Adaptive verification recorded zero false acceptance on matched cases.",
            )
        )

    adaptive_cost = _known_cost(adaptive)
    optimizer_cost = _known_cost(optimizer)
    strongest_cost = _known_cost(strongest)
    adaptive_duration = _known_duration(adaptive)
    optimizer_duration = _known_duration(optimizer)
    strongest_duration = _known_duration(strongest)
    comparable_cost = all(
        item is not None for item in (adaptive_cost, optimizer_cost, strongest_cost)
    )
    comparable_duration = all(
        item is not None
        for item in (adaptive_duration, optimizer_duration, strongest_duration)
    )
    lower_cost = bool(
        comparable_cost
        and adaptive_cost is not None
        and optimizer_cost is not None
        and strongest_cost is not None
        and adaptive_cost < min(optimizer_cost, strongest_cost)
    )
    lower_duration = bool(
        comparable_duration
        and adaptive_duration is not None
        and optimizer_duration is not None
        and strongest_duration is not None
        and adaptive_duration < min(optimizer_duration, strongest_duration)
    )
    if lower_cost or lower_duration:
        checks.append(
            _check(
                "lower_cost_or_time",
                "pass",
                "Adaptive verification lowered a fully accounted cost or elapsed-time measure.",
            )
        )
    elif comparable_cost or comparable_duration:
        checks.append(
            _check(
                "lower_cost_or_time",
                "fail",
                "Adaptive verification did not lower comparable total cost or elapsed time.",
            )
        )
    else:
        checks.append(
            _check(
                "lower_cost_or_time",
                "insufficient_evidence",
                "Cost and elapsed-time accounting are not complete enough for comparison.",
            )
        )
        warnings.append("Unknown cost or duration remains unranked.")

    review_known = all(
        item.review_time_accounting_status == "complete"
        and item.review_minutes is not None
        for item in by_strategy.values()
    )
    if review_known and adaptive.review_minutes is not None:
        comparison = [
            float(strongest.review_minutes),  # type: ignore[arg-type]
            float(optimizer.review_minutes),  # type: ignore[arg-type]
        ]
        if adaptive.review_minutes < min(comparison):
            checks.append(
                _check(
                    "lower_review_burden",
                    "pass",
                    "Adaptive verification reduced explicit review minutes.",
                )
            )
        else:
            checks.append(
                _check(
                    "lower_review_burden",
                    "fail",
                    "Adaptive verification did not reduce explicit review minutes.",
                )
            )
    else:
        checks.append(
            _check(
                "lower_review_burden",
                "insufficient_evidence",
                "Explicit review minutes are unknown for at least one arm.",
            )
        )

    checks.append(
        _check(
            "explainability",
            "pass" if adaptive.explainable_routes else "fail",
            (
                "Every adaptive route and verification expansion is explainable."
                if adaptive.explainable_routes
                else "Adaptive verification evidence lacks a complete explanation."
            ),
        )
    )
    checks.append(
        _check(
            "safe_fallback",
            "pass" if adaptive.safe_fallback else "fail",
            (
                "Unresolved proof falls back safely to review or rejection."
                if adaptive.safe_fallback
                else "An unresolved path does not fail closed."
            ),
        )
    )

    statuses = {item.status for item in checks}
    if "fail" in statuses:
        status = "FAIL"
    elif "insufficient_evidence" in statuses:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "PASS"
    timestamp = generated_at or datetime.now(timezone.utc)
    serialized_arms = [
        by_strategy[name].model_dump(mode="json")
        for name in (
            "strongest_only",
            "accepted_change_optimizer",
            "optimizer_plus_adaptive",
        )
    ]
    identity = {
        "arms": serialized_arms,
        "checks": [item.model_dump(mode="json") for item in checks],
        "status": status,
    }
    return GateDReport(
        gate_id="gated_" + canonical_digest(identity).removeprefix("sha256:"),
        generated_at=timestamp,
        status=status,  # type: ignore[arg-type]
        arms=[
            by_strategy[name]
            for name in (
                "strongest_only",
                "accepted_change_optimizer",
                "optimizer_plus_adaptive",
            )
        ],
        checks=checks,
        warnings=sorted(set(warnings)),
        evidence_references=sorted(set(str(item) for item in evidence_references)),
        next_milestone_permitted=status == "PASS",
    )
