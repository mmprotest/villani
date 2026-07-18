"""Explicit estimated and actual accounting for closed-loop coding attempts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, TypeAlias

from villani_ops.core.backend import Backend


CostAccountingStatus: TypeAlias = Literal["complete", "partial", "unknown"]


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    input_token_cost: float | None
    output_token_cost: float | None
    compute_time_cost: float | None
    fixed_cost: float | None
    total: float | None
    currency: str
    accounting_status: CostAccountingStatus
    source: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _token_component_enabled(backend: Backend) -> bool:
    if backend.billing_mode == "token":
        return True
    if backend.billing_mode != "hybrid":
        return False
    return bool(
        backend.estimated_input_tokens is not None
        or backend.estimated_output_tokens is not None
        or backend.input_cost_per_million > 0
        or backend.output_cost_per_million > 0
    )


def _calculate(
    backend: Backend,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    duration_seconds: float | None,
    started: bool,
    source: str,
) -> CostBreakdown:
    input_cost: float | None = None
    output_cost: float | None = None
    compute_cost: float | None = None
    fixed_cost: float | None = None
    expected: list[str] = []
    values: dict[str, float | None] = {}

    if _token_component_enabled(backend):
        expected.extend(("input_token_cost", "output_token_cost"))
        input_cost = (
            input_tokens / 1_000_000 * backend.input_cost_per_million
            if input_tokens is not None
            else None
        )
        output_cost = (
            output_tokens / 1_000_000 * backend.output_cost_per_million
            if output_tokens is not None
            else None
        )
        values["input_token_cost"] = input_cost
        values["output_token_cost"] = output_cost

    compute_enabled = backend.billing_mode == "compute_time" or (
        backend.billing_mode == "hybrid" and backend.compute_cost_per_hour is not None
    )
    if compute_enabled:
        expected.append("compute_time_cost")
        compute_cost = (
            duration_seconds / 3600 * backend.compute_cost_per_hour
            if duration_seconds is not None
            and backend.compute_cost_per_hour is not None
            else None
        )
        values["compute_time_cost"] = compute_cost

    fixed_enabled = backend.billing_mode == "fixed" or (
        backend.billing_mode == "hybrid" and backend.fixed_cost_per_attempt is not None
    )
    if fixed_enabled:
        expected.append("fixed_cost")
        fixed_cost = backend.fixed_cost_per_attempt if started else None
        values["fixed_cost"] = fixed_cost

    known: list[float] = []
    for name in expected:
        value = values.get(name)
        if value is not None:
            known.append(value)
    if expected and len(known) == len(expected):
        status: CostAccountingStatus = "complete"
    elif known:
        status = "partial"
    else:
        status = "unknown"
    total = float(sum(known)) if known else None
    return CostBreakdown(
        input_token_cost=input_cost,
        output_token_cost=output_cost,
        compute_time_cost=compute_cost,
        fixed_cost=fixed_cost,
        total=total,
        currency=backend.currency,
        accounting_status=status,
        source=source,
    )


def estimate_attempt_cost(backend: Backend) -> CostBreakdown:
    """Calculate a pre-attempt estimate using only configured estimates."""

    return _calculate(
        backend,
        input_tokens=backend.estimated_input_tokens,
        output_tokens=backend.estimated_output_tokens,
        duration_seconds=backend.estimated_duration_seconds,
        started=True,
        source="configured_estimate",
    )


def actual_attempt_cost(
    backend: Backend,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    duration_seconds: float | None,
    started: bool = True,
) -> CostBreakdown:
    """Calculate actual cost from captured usage and duration telemetry."""

    return _calculate(
        backend,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_seconds=duration_seconds,
        started=started,
        source="captured_telemetry_and_backend_config",
    )


def provider_reported_attempt_cost(
    backend: Backend,
    *,
    amount: float,
    currency: str,
    source: str,
) -> CostBreakdown:
    """Preserve a provider-authoritative attempt total without re-estimating it."""

    if amount < 0:
        raise ValueError("provider-reported cost cannot be negative")
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3:
        raise ValueError("provider-reported cost requires a three-letter currency")
    return CostBreakdown(
        input_token_cost=None,
        output_token_cost=None,
        compute_time_cost=None,
        fixed_cost=None,
        total=float(amount),
        currency=normalized_currency,
        accounting_status="complete",
        source=source,
    )
