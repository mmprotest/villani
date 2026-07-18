"""Explicit, local-first PT9 human outcomes and supervision accounting."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ..durable_io import append_jsonl_durable, read_jsonl_tolerant, write_json_atomic
from .models import (
    BinaryVerificationDecision,
    HumanOutcome,
    MoneyAccounting,
    SupervisionMetrics,
    canonical_digest,
)


def make_human_outcome(
    *,
    run_id: str,
    outcome: str,
    attempt_id: str | None = None,
    review_minutes: float | None = None,
    full_trace_opened: bool | None = None,
    correction_summary: str | None = None,
    linked_reference: str | None = None,
    notes: str | None = None,
    imported_from: str = "explicit_cli",
    actor: str = "local_user",
    recorded_at: datetime | None = None,
) -> HumanOutcome:
    timestamp = recorded_at or datetime.now(timezone.utc)
    value = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "outcome": outcome,
        "review_minutes": review_minutes,
        "full_trace_opened": full_trace_opened,
        "correction_summary": correction_summary,
        "linked_reference": linked_reference,
        "notes": notes,
        "imported_from": imported_from,
        "actor": actor,
        "recorded_at": timestamp.isoformat(),
    }
    return HumanOutcome(
        outcome_id="hout_" + canonical_digest(value).removeprefix("sha256:"),
        run_id=run_id,
        attempt_id=attempt_id,
        recorded_at=timestamp,
        outcome=outcome,  # type: ignore[arg-type]
        review_minutes=review_minutes,
        review_time_accounting_status=(
            "complete" if review_minutes is not None else "unknown"
        ),
        full_trace_opened=full_trace_opened,
        full_trace_accounting_status=(
            "complete" if full_trace_opened is not None else "unknown"
        ),
        correction_summary=correction_summary,
        linked_reference=linked_reference,
        imported_from=imported_from,  # type: ignore[arg-type]
        actor=actor,
        notes=notes,
    )


def append_human_outcome(path: str | Path, outcome: HumanOutcome) -> bool:
    """Append once by stable id; history is never rewritten or passively gathered."""

    destination = Path(path)
    existing = load_human_outcomes(destination)
    by_id = {item.outcome_id: item for item in existing}
    prior = by_id.get(outcome.outcome_id)
    if prior is not None:
        if prior != outcome:
            raise ValueError("human outcome id already exists with different content")
        return False
    append_jsonl_durable(destination, outcome)
    return True


def load_human_outcomes(path: str | Path) -> tuple[HumanOutcome, ...]:
    source = Path(path)
    if not source.is_file():
        return ()
    return tuple(
        HumanOutcome.model_validate(item) for item in read_jsonl_tolerant(source)
    )


def _sum_money(values: Sequence[MoneyAccounting], source: str) -> MoneyAccounting:
    applicable = [item for item in values if item.accounting_status != "not_applicable"]
    if not applicable:
        return MoneyAccounting(
            amount=None,
            currency=None,
            accounting_status="not_applicable",
            source=source,
        )
    currencies = {
        str(item.currency).upper() for item in applicable if item.currency is not None
    }
    if len(currencies) > 1:
        return MoneyAccounting(
            amount=None,
            currency=None,
            accounting_status="unknown",
            source=f"{source}_mixed_currency",
        )
    known = [item.amount for item in applicable if item.amount is not None]
    unknown = any(
        item.accounting_status in {"unknown", "partial"} or item.amount is None
        for item in applicable
    )
    if known:
        return MoneyAccounting(
            amount=sum(float(item) for item in known),
            currency=next(iter(currencies), "USD"),
            accounting_status="partial" if unknown else "complete",
            source=source,
        )
    return MoneyAccounting(
        amount=None,
        currency=None,
        accounting_status="unknown",
        source=source,
    )


def build_supervision_metrics(
    *,
    run_id: str,
    outcomes: Sequence[HumanOutcome],
    decisions: Sequence[BinaryVerificationDecision] = (),
    evidence_expansion_count: int = 0,
    application_without_full_trace_count: int | None = None,
    full_trace_accounting_status: str | None = None,
    review_cost_per_minute: float | None = None,
    execution_cost: MoneyAccounting | None = None,
    calculated_at: datetime | None = None,
) -> SupervisionMetrics:
    explicit_minutes = [
        item.review_minutes
        for item in outcomes
        if item.review_time_accounting_status == "complete"
        and item.review_minutes is not None
    ]
    review_unknown = not outcomes or any(
        item.review_time_accounting_status == "unknown" for item in outcomes
    )
    if explicit_minutes:
        review_minutes = sum(explicit_minutes)
        review_status = "partial" if review_unknown else "complete"
    else:
        review_minutes = None
        review_status = "unknown"

    trace_complete = bool(outcomes) and all(
        item.full_trace_accounting_status == "complete"
        and item.full_trace_opened is not None
        for item in outcomes
    )
    derived_application_without_trace = sum(
        item.outcome == "accepted_as_is" and item.full_trace_opened is False
        for item in outcomes
    )
    if application_without_full_trace_count is None:
        application_without_full_trace_count = (
            derived_application_without_trace if trace_complete else 0
        )
    if full_trace_accounting_status is None:
        full_trace_accounting_status = "complete" if trace_complete else "unknown"

    verification_cost = _sum_money(
        [item.verification_cost for item in decisions],
        "binary_verification_decisions",
    )
    if review_minutes is not None and review_cost_per_minute is not None:
        review_cost = MoneyAccounting(
            amount=review_minutes * review_cost_per_minute,
            currency="USD",
            accounting_status="partial" if review_unknown else "complete",
            source="explicit_review_minutes_times_configured_rate",
        )
    else:
        review_cost = MoneyAccounting(
            amount=None,
            currency=None,
            accounting_status="unknown",
            source=(
                "review_minutes_unknown"
                if review_minutes is None
                else "review_cost_rate_unconfigured"
            ),
        )
    total_parts = [verification_cost, review_cost]
    if execution_cost is not None:
        total_parts.insert(0, execution_cost)
    else:
        total_parts.insert(
            0,
            MoneyAccounting(
                amount=None,
                currency=None,
                accounting_status="unknown",
                source="execution_cost_not_supplied",
            ),
        )
    total = _sum_money(total_parts, "accepted_change_cost_components")
    timestamp = calculated_at or datetime.now(timezone.utc)
    source_ids = sorted(item.outcome_id for item in outcomes)
    identity = {
        "run_id": run_id,
        "outcome_ids": source_ids,
        "decision_ids": sorted(item.decision_id for item in decisions),
        "evidence_expansion_count": evidence_expansion_count,
        "application_without_full_trace_count": application_without_full_trace_count,
        "full_trace_accounting_status": full_trace_accounting_status,
    }
    return SupervisionMetrics(
        metrics_id="smet_" + canonical_digest(identity).removeprefix("sha256:"),
        run_id=run_id,
        calculated_at=timestamp,
        eligible_outcome_count=len(outcomes),
        evidence_expansion_count=evidence_expansion_count,
        explicit_review_minutes=review_minutes,
        review_time_accounting_status=review_status,  # type: ignore[arg-type]
        application_without_full_trace_count=application_without_full_trace_count,
        full_trace_accounting_status=full_trace_accounting_status,  # type: ignore[arg-type]
        correction_count=sum(
            item.outcome == "corrected_before_use" for item in outcomes
        ),
        false_acceptance_count=sum(
            item.outcome in {"false_acceptance", "reverted", "reopened_defect"}
            for item in outcomes
        ),
        false_rejection_count=sum(
            item.outcome == "false_rejection" for item in outcomes
        ),
        verification_cost=verification_cost,
        review_cost=review_cost,
        total_accepted_change_cost=total,
        source_outcome_ids=source_ids,
    )


def persist_supervision_metrics(
    run_directory: str | Path, metrics: SupervisionMetrics
) -> Path:
    destination = Path(run_directory) / "supervision-metrics.json"
    write_json_atomic(destination, metrics)
    return destination
