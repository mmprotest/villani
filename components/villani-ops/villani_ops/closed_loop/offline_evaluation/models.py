from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExperimentArm(FrozenModel):
    name: str = Field(min_length=1)
    option_id: str | None = None
    probability: float = Field(gt=0, le=1)
    is_control: bool = False


class ExperimentConstraints(FrozenModel):
    minimum_capability_score: float = Field(default=0, ge=0)
    maximum_cost_usd: float | None = Field(default=None, ge=0)
    allowed_residencies: tuple[str, ...] = ()
    allowed_option_ids: tuple[str, ...] = ()
    security_sensitive: bool = False


class OptionEligibilityInput(FrozenModel):
    option_id: str
    capability_score: float = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    residencies: tuple[str, ...] = ()
    security_approved: bool = False
    user_allowed: bool = True


class ExperimentDefinition(FrozenModel):
    schema_version: Literal["villani.experiment.v1"] = "villani.experiment.v1"
    experiment_id: str
    experiment_version: str
    mode: Literal["holdout", "shadow_only", "bounded_exploration"]
    salt: str = Field(min_length=16)
    arms: tuple[ExperimentArm, ...]
    policy_snapshot: dict[str, Any]
    constraints: ExperimentConstraints = ExperimentConstraints()

    @model_validator(mode="after")
    def valid_probabilities(self) -> "ExperimentDefinition":
        if not self.arms:
            raise ValueError("experiment requires at least one arm")
        if abs(sum(arm.probability for arm in self.arms) - 1.0) > 1e-9:
            raise ValueError("arm probabilities must sum to one")
        if self.mode in {"holdout", "shadow_only"} and not any(
            arm.is_control for arm in self.arms
        ):
            raise ValueError(
                "holdout and shadow-only experiments require a control arm"
            )
        return self


class AssignmentEligibility(FrozenModel):
    eligible: bool
    reasons: tuple[str, ...] = ()


class ExperimentAssignment(FrozenModel):
    schema_version: Literal["villani.experiment_assignment.v1"] = (
        "villani.experiment_assignment.v1"
    )
    experiment_id: str
    experiment_version: str
    arm: str
    unit_id: str
    eligibility: AssignmentEligibility
    assignment_probability: float = Field(gt=0, le=1)
    propensity: float = Field(gt=0, le=1)
    randomization_seed: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_snapshot: dict[str, Any]
    policy_snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    timestamp: datetime
    mode: Literal["holdout", "shadow_only", "bounded_exploration"]
    controls_live_execution: Literal[False] = False


class EvaluationObservation(FrozenModel):
    unit_id: str
    segment: str
    logged_arm: str
    target_arm: str
    success: bool | None
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    propensity: float | None = Field(default=None, gt=0, le=1)
    target_probability: float = Field(ge=0, le=1)
    assignment_provenance: dict[str, Any] | None
    censored_reason: str | None = None
    logged_outcome_prediction: float | None = Field(default=None, ge=0, le=1)
    target_outcome_prediction: float | None = Field(default=None, ge=0, le=1)
    outcome_model_inputs: dict[str, Any] | None = None
    backend_version: str
    task_features: dict[str, float | str]


class ConfidenceInterval(FrozenModel):
    estimate: float | None
    lower: float | None
    upper: float | None
    confidence: float = 0.95
    sample_count: int
    status: Literal["available", "insufficient_samples", "invalid"]


class SegmentCalibration(FrozenModel):
    segment: str
    sample_count: int
    predicted_success: float | None
    observed_success: float | None
    absolute_error: float | None


class OfflineEvaluationReport(FrozenModel):
    schema_version: Literal["villani.offline_evaluation.v1"] = (
        "villani.offline_evaluation.v1"
    )
    evaluator_version: Literal["offline_evaluator_v1"] = "offline_evaluator_v1"
    raw_count: int
    observed_count: int
    censored_count: int
    missing_propensity_count: int
    direct_success: ConfidenceInterval
    direct_cost: ConfidenceInterval
    direct_latency: ConfidenceInterval
    inverse_propensity_success: ConfidenceInterval
    doubly_robust_success: ConfidenceInterval
    calibration: tuple[SegmentCalibration, ...]
    minimum_sample_size: int
    causal_savings_claim_valid: bool
    refusal_reasons: tuple[str, ...]
    assignment_provenance_complete: bool


class DriftSignal(FrozenModel):
    name: str
    baseline_value: Any
    current_value: Any
    magnitude: float | None
    threshold: float
    drifted: bool


class DriftReport(FrozenModel):
    schema_version: Literal["villani.drift_report.v1"] = "villani.drift_report.v1"
    monitor_version: Literal["offline_drift_v1"] = "offline_drift_v1"
    signals: tuple[DriftSignal, ...]
    drifted: bool


class SegmentPolicyChoice(FrozenModel):
    segment: str
    option_id: str | None
    sample_count: int
    conservative_success: float | None
    expected_cost_usd: float | None
    status: Literal["selected", "insufficient_samples", "no_safe_option"]


class OptimizedPolicy(FrozenModel):
    schema_version: Literal["villani.segmented_policy.v1"] = (
        "villani.segmented_policy.v1"
    )
    optimizer_version: Literal["transparent_segmented_v1"] = "transparent_segmented_v1"
    choices: tuple[SegmentPolicyChoice, ...]
    controls_live_execution: Literal[False] = False
