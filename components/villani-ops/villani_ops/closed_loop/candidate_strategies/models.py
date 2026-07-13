"""Versioned contracts for reliability strategies on one immutable coding task."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


StrategyName = Literal[
    "single_attempt",
    "sequential_escalation",
    "parallel_diverse_candidates",
    "adaptive_candidates",
]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateDimensions(FrozenModel):
    agent: str = "villani-code"
    backend_name: str | None = None
    model: str | None = None
    prompt_strategy_id: str = "direct"
    seed: int | None = None
    planning_mode: str = "default"
    tool_budget: int | None = Field(default=None, ge=1)

    @property
    def effective_fingerprint(self) -> str:
        # Only behavior-affecting dimensions acknowledged by the runner may
        # contribute. Requested seed/planning/tool-budget values are retained
        # for audit but are unsupported until a provider applies them.
        encoded = json.dumps(
            {
                "agent": self.agent,
                "backend_name": self.backend_name,
                "model": self.model,
                "prompt_strategy_id": self.prompt_strategy_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class CandidatePlan(FrozenModel):
    candidate_id: str
    ordinal: int = Field(ge=1)
    dimensions: CandidateDimensions
    effective_configuration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sandbox_id: str
    expected_success: float | None = Field(default=None, ge=0, le=1)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    repair_source_attempt_id: str | None = None

    @model_validator(mode="after")
    def validate_fingerprint(self) -> "CandidatePlan":
        if self.effective_configuration_sha256 != self.dimensions.effective_fingerprint:
            raise ValueError(
                "effective configuration fingerprint does not match dimensions"
            )
        if (
            self.repair_source_attempt_id is not None
            and not self.repair_source_attempt_id
        ):
            raise ValueError("repair source attempt id cannot be empty")
        return self


class ReliabilityStrategyConfiguration(FrozenModel):
    schema_version: Literal["villani.candidate_strategy.v1"] = (
        "villani.candidate_strategy.v1"
    )
    strategy: StrategyName = "single_attempt"
    stop_policy: Literal["stop_on_sufficient", "compare"] = "stop_on_sufficient"
    accepted_candidate_requirement: int = Field(default=1, ge=1)
    maximum_candidates: int = Field(default=1, ge=1)
    maximum_parallelism: int = Field(default=1, ge=1)
    minimum_marginal_expected_success: float = Field(default=0.0, ge=0, le=1)
    minimum_verifier_confidence: float = Field(default=0.0, ge=0, le=1)
    minimum_evidence_grade: Literal["none", "weak", "moderate", "strong"] = "none"
    repair_strategy: bool = False
    candidates: tuple[CandidateDimensions, ...] = ()
    expected_success_by_ordinal: tuple[float | None, ...] = ()
    estimated_cost_usd_by_ordinal: tuple[float | None, ...] = ()

    @model_validator(mode="after")
    def validate_strategy(self) -> "ReliabilityStrategyConfiguration":
        if self.accepted_candidate_requirement > self.maximum_candidates:
            raise ValueError(
                "accepted candidate requirement exceeds maximum candidates"
            )
        if self.maximum_parallelism > self.maximum_candidates:
            raise ValueError("maximum parallelism exceeds maximum candidates")
        if (
            self.strategy in {"single_attempt", "sequential_escalation"}
            and self.maximum_parallelism != 1
        ):
            raise ValueError(f"{self.strategy} requires maximum_parallelism=1")
        if self.strategy == "single_attempt" and self.maximum_candidates != 1:
            raise ValueError("single_attempt requires maximum_candidates=1")
        if (
            self.stop_policy == "stop_on_sufficient"
            and self.accepted_candidate_requirement != 1
        ):
            raise ValueError("stop_on_sufficient requires one accepted candidate")
        return self


class CandidateObservation(FrozenModel):
    candidate_id: str
    acceptance_eligible: bool
    verifier_confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_grade: Literal["none", "weak", "moderate", "strong"] = "none"
    actual_cost_usd: float | None = Field(default=None, ge=0)


class AdaptiveStopDecision(FrozenModel):
    stop: bool
    reason: str
    accepted_count: int = Field(ge=0)
    next_marginal_expected_success: float | None = Field(default=None, ge=0, le=1)
    remaining_attempt_budget: int = Field(ge=0)
    remaining_cost_budget_usd: float | None = Field(default=None, ge=0)
    avoided_attempts: int = Field(ge=0)
    estimated_avoided_spend_usd: float | None = Field(default=None, ge=0)
    actual_savings_usd: None = None


class ReliabilityAccounting(FrozenModel):
    schema_version: Literal["villani.reliability_accounting.v1"] = (
        "villani.reliability_accounting.v1"
    )
    strategy: StrategyName
    planned_attempts: int = Field(ge=0)
    started_attempts: int = Field(ge=0)
    completed_attempts: int = Field(ge=0)
    cancelled_attempts: int = Field(ge=0)
    avoided_attempts: int = Field(ge=0)
    estimated_avoided_spend_usd: float | None = Field(default=None, ge=0)
    actual_savings_usd: None = None
    diversity_claimed: bool
    distinct_effective_configurations: int = Field(ge=0)
    maximum_observed_concurrency: int = Field(ge=0)
    stop_reason: str | None = None
