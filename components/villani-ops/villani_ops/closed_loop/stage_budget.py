"""Stage-aware cost and wall-time reservation for deterministic routing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .interfaces import BudgetContext
from .protocol import AccountingStatus


class StrictRoutingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StageReserveConfiguration(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verification_fraction: float = Field(default=0.10, ge=0, le=1)
    strong_escalation_fraction: float = Field(default=0.30, ge=0, le=1)
    final_validation_fraction: float = Field(default=0.10, ge=0, le=1)
    selection_fraction: float = Field(default=0.05, ge=0, le=1)
    verification_duration_seconds: float | None = Field(default=None, ge=0)
    final_validation_duration_seconds: float | None = Field(default=None, ge=0)
    selection_duration_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_total_fraction(self) -> StageReserveConfiguration:
        total = (
            self.verification_fraction
            + self.strong_escalation_fraction
            + self.final_validation_fraction
            + self.selection_fraction
        )
        if total > 1:
            raise ValueError("stage reserve fractions must sum to at most one")
        return self


class StageReserveAmount(StrictRoutingModel):
    fraction: float = Field(ge=0, le=1)
    cost_usd: float | None = Field(default=None, ge=0)
    wall_time_ms: float | None = Field(default=None, ge=0)
    cost_source: str
    duration_source: str


class BudgetAfterAction(StrictRoutingModel):
    remaining_cost_usd: float | None = Field(default=None, ge=0)
    remaining_wall_time_ms: float | None = Field(default=None, ge=0)
    cost_accounting_status: AccountingStatus
    duration_accounting_status: AccountingStatus


class StageBudgetProjection(StrictRoutingModel):
    action: str
    chosen_backend: str | None
    projected_action_cost: float | None = Field(default=None, ge=0)
    projected_action_wall_time: float | None = Field(default=None, ge=0)
    projected_action_cost_source: str
    projected_action_duration_source: str
    verification_reserve: StageReserveAmount
    escalation_reserve: StageReserveAmount
    final_validation_reserve: StageReserveAmount
    selection_reserve: StageReserveAmount
    required_reserve_cost: float | None = Field(default=None, ge=0)
    required_reserve_wall_time: float | None = Field(default=None, ge=0)
    budget_after_action: BudgetAfterAction
    reserve_satisfied: bool
    accounting_status: Literal[
        "complete", "partial", "unknown", "not_applicable"
    ]
    missing_inputs: list[str]


def _reserve(
    *,
    fraction: float,
    remaining_cost: float | None,
    remaining_wall: int | None,
    explicit_cost: float | None,
    explicit_wall: float | None,
    cost_source: str,
    duration_source: str,
) -> StageReserveAmount:
    fractional_cost = (
        remaining_cost * fraction if remaining_cost is not None else None
    )
    fractional_wall = (
        float(remaining_wall) * fraction if remaining_wall is not None else None
    )
    cost = (
        max(fractional_cost or 0.0, explicit_cost or 0.0)
        if fractional_cost is not None or explicit_cost is not None
        else None
    )
    wall = (
        max(fractional_wall or 0.0, explicit_wall or 0.0)
        if fractional_wall is not None or explicit_wall is not None
        else None
    )
    return StageReserveAmount(
        fraction=fraction,
        cost_usd=cost,
        wall_time_ms=wall,
        cost_source=(
            cost_source if explicit_cost is not None else "configured_fraction"
        ),
        duration_source=(
            duration_source if explicit_wall is not None else "configured_fraction"
        ),
    )


def project_stage_budget(
    *,
    budget: BudgetContext,
    action: str,
    chosen_backend: str | None,
    projected_action_cost: float | None,
    projected_action_wall_time: float | None,
    action_cost_source: str,
    action_duration_source: str,
    verification_cost: float | None,
    verification_wall_time: float | None,
    escalation_cost: float | None,
    escalation_wall_time: float | None,
    final_validation_cost: float | None,
    final_validation_wall_time: float | None,
    selection_cost: float | None,
    selection_wall_time: float | None,
    configuration: StageReserveConfiguration,
    requires_escalation_reserve: bool,
    missing_inputs: list[str] | None = None,
) -> StageBudgetProjection:
    """Project an action while preserving required downstream stage reserves."""

    missing = list(missing_inputs or [])
    constrained_action = action in {"attempt", "retry", "escalate"}
    cost_active = budget.cost_accounting_status != "not_applicable"
    wall_active = budget.duration_accounting_status != "not_applicable"
    remaining_cost = (
        budget.remaining_cost_usd
        if budget.cost_accounting_status == "complete"
        else None
    )
    remaining_wall = (
        budget.remaining_wall_time_ms
        if budget.duration_accounting_status == "complete"
        else None
    )
    escalation_fraction = (
        configuration.strong_escalation_fraction
        if requires_escalation_reserve
        else 0.0
    )
    verification = _reserve(
        fraction=configuration.verification_fraction,
        remaining_cost=remaining_cost,
        remaining_wall=remaining_wall,
        explicit_cost=verification_cost,
        explicit_wall=verification_wall_time,
        cost_source="configured_or_observed_verification",
        duration_source="configured_or_observed_verification",
    )
    escalation = _reserve(
        fraction=escalation_fraction,
        remaining_cost=remaining_cost,
        remaining_wall=remaining_wall,
        explicit_cost=escalation_cost if requires_escalation_reserve else 0.0,
        explicit_wall=(
            escalation_wall_time if requires_escalation_reserve else 0.0
        ),
        cost_source="strong_backend_projection",
        duration_source="strong_backend_projection",
    )
    final_validation = _reserve(
        fraction=configuration.final_validation_fraction,
        remaining_cost=remaining_cost,
        remaining_wall=remaining_wall,
        explicit_cost=final_validation_cost,
        explicit_wall=final_validation_wall_time,
        cost_source="configured_final_validation",
        duration_source="configured_final_validation",
    )
    selection = _reserve(
        fraction=configuration.selection_fraction,
        remaining_cost=remaining_cost,
        remaining_wall=remaining_wall,
        explicit_cost=selection_cost,
        explicit_wall=selection_wall_time,
        cost_source="configured_selection",
        duration_source="configured_selection",
    )
    reserves = (verification, escalation, final_validation, selection)
    required_cost = (
        sum(item.cost_usd or 0.0 for item in reserves)
        if remaining_cost is not None
        else None
    )
    required_wall = (
        sum(item.wall_time_ms or 0.0 for item in reserves)
        if remaining_wall is not None
        else None
    )

    if constrained_action and projected_action_cost is None:
        missing.append("projected_action_cost")
    if constrained_action and projected_action_wall_time is None:
        missing.append("projected_action_wall_time")
    if requires_escalation_reserve and escalation_cost is None:
        missing.append("projected_strong_escalation_cost")
    if requires_escalation_reserve and escalation_wall_time is None:
        missing.append("projected_strong_escalation_wall_time")
    if verification_cost is None:
        missing.append("projected_verification_cost")
    if verification_wall_time is None:
        missing.append("projected_verification_wall_time")
    if cost_active and remaining_cost is None:
        missing.append("remaining_cost_budget")
    if wall_active and remaining_wall is None:
        missing.append("remaining_wall_time_budget")

    cost_ok = True
    if cost_active:
        cost_ok = bool(
            remaining_cost is not None
            and (not constrained_action or projected_action_cost is not None)
            and remaining_cost
            >= (projected_action_cost or 0.0) + (required_cost or 0.0)
        )
    wall_ok = True
    if wall_active:
        wall_ok = bool(
            remaining_wall is not None
            and (not constrained_action or projected_action_wall_time is not None)
            and remaining_wall
            >= (projected_action_wall_time or 0.0) + (required_wall or 0.0)
        )
    after_cost = (
        max(remaining_cost - (projected_action_cost or 0.0), 0.0)
        if remaining_cost is not None
        and (projected_action_cost is not None or not constrained_action)
        else None
    )
    after_wall = (
        max(float(remaining_wall) - (projected_action_wall_time or 0.0), 0.0)
        if remaining_wall is not None
        and (projected_action_wall_time is not None or not constrained_action)
        else None
    )
    accounting_status: Literal[
        "complete", "partial", "unknown", "not_applicable"
    ]
    if not cost_active and not wall_active:
        accounting_status = "not_applicable"
    elif cost_ok and wall_ok and not missing:
        accounting_status = "complete"
    elif remaining_cost is None and cost_active or remaining_wall is None and wall_active:
        accounting_status = "unknown"
    else:
        accounting_status = "partial"
    return StageBudgetProjection(
        action=action,
        chosen_backend=chosen_backend,
        projected_action_cost=projected_action_cost,
        projected_action_wall_time=projected_action_wall_time,
        projected_action_cost_source=action_cost_source,
        projected_action_duration_source=action_duration_source,
        verification_reserve=verification,
        escalation_reserve=escalation,
        final_validation_reserve=final_validation,
        selection_reserve=selection,
        required_reserve_cost=required_cost,
        required_reserve_wall_time=required_wall,
        budget_after_action=BudgetAfterAction(
            remaining_cost_usd=after_cost,
            remaining_wall_time_ms=after_wall,
            cost_accounting_status=budget.cost_accounting_status,
            duration_accounting_status=budget.duration_accounting_status,
        ),
        reserve_satisfied=cost_ok and wall_ok,
        accounting_status=accounting_status,
        missing_inputs=sorted(set(missing)),
    )


__all__ = [
    "BudgetAfterAction",
    "StageBudgetProjection",
    "StageReserveAmount",
    "StageReserveConfiguration",
    "project_stage_budget",
]
