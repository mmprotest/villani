from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


RunMode = Literal["observe", "recommend", "enforce"]


class TaskRoute(FrozenModel):
    agent_adapter: str
    backend_name: str
    model: str
    execution_provider: str
    maximum_attempts: int = Field(ge=1)
    candidate_strategy: str
    verifier_graph_version: str
    escalation_sequence: tuple[str, ...]


class ControlledAlternative(FrozenModel):
    route: TaskRoute
    eligible: bool
    constraints: dict[str, Any]
    rejection_reasons: tuple[str, ...]
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    expected_success: float | None = Field(default=None, ge=0, le=1)
    expected_latency_ms: float | None = Field(default=None, ge=0)
    uncertainty: float = Field(ge=0, le=1)


class CircuitBreakerState(FrozenModel):
    open: bool
    reasons: tuple[str, ...]
    metrics: dict[str, Any]


class GuardedRoutingDecision(FrozenModel):
    schema_version: Literal["villani.guarded_routing_decision.v1"] = (
        "villani.guarded_routing_decision.v1"
    )
    decision_id: str
    run_id: str
    decision_sequence: int
    mode: RunMode
    policy_source: Literal[
        "active_policy", "last_known_good_policy", "bootstrap_policy", "fail_closed"
    ]
    policy_version: str
    resolved_scope_precedence: tuple[str, ...]
    alternatives: tuple[ControlledAlternative, ...]
    recommended_route: TaskRoute | None
    execution_route: TaskRoute | None
    experiment_assignment: dict[str, Any] | None
    budget_before: dict[str, Any]
    evidence_summary: dict[str, Any]
    actual_spend_usd: float | None
    expected_marginal_value_usd: float | None
    circuit_breakers: CircuitBreakerState
    final_reason: str
    input_digest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timestamp: datetime
    controls_execution: bool
