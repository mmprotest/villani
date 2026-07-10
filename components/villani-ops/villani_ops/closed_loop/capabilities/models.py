"""Versioned data models for the local empirical capability registry."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileKey(StrictModel):
    backend_name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    task_category: str = Field(min_length=1)
    difficulty: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    classifier_version: str = Field(min_length=1)
    verifier_version: str = Field(min_length=1)
    scorer_version: str = Field(min_length=1)

    def sort_key(self) -> tuple[str, ...]:
        return (
            self.backend_name,
            self.provider,
            self.model,
            self.task_category,
            self.difficulty,
            self.risk,
            self.classifier_version,
            self.verifier_version,
            self.scorer_version,
        )

    def backoff_keys(self) -> tuple[tuple[str, ProfileKey], ...]:
        """Return the mandated fine-to-global lookup order."""

        return (
            ("category_difficulty_risk", self),
            ("category_difficulty", self.model_copy(update={"risk": "*"})),
            (
                "category",
                self.model_copy(update={"difficulty": "*", "risk": "*"}),
            ),
            (
                "global_backend_model",
                self.model_copy(
                    update={"task_category": "*", "difficulty": "*", "risk": "*"}
                ),
            ),
        )


class IncludedAttempt(StrictModel):
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    outcome: Literal["success", "verified_model_failure"]


class CapabilityProfile(StrictModel):
    key: ProfileKey
    included_attempts: list[IncludedAttempt]
    successes: int = Field(ge=0)
    verified_model_failures: int = Field(ge=0)
    sample_count: int = Field(ge=0)
    raw_success_rate: float = Field(ge=0, le=1)
    wilson_lower_bound: float = Field(ge=0, le=1)
    mean_actual_attempt_cost: float | None = Field(default=None, ge=0)
    median_actual_attempt_cost: float | None = Field(default=None, ge=0)
    mean_duration_ms: float | None = Field(default=None, ge=0)
    median_duration_ms: float | None = Field(default=None, ge=0)
    mean_input_tokens: float | None = Field(default=None, ge=0)
    mean_output_tokens: float | None = Field(default=None, ge=0)
    excluded_outcome_counts: dict[str, int]
    first_observed_at: str | None
    last_observed_at: str | None
    source_data_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_counts(self) -> CapabilityProfile:
        if self.successes + self.verified_model_failures != self.sample_count:
            raise ValueError("sample_count must equal successes plus verified failures")
        if len(self.included_attempts) != self.sample_count:
            raise ValueError("included_attempts must contain every denominator member")
        return self


class CapabilitySnapshot(StrictModel):
    schema_version: Literal["villani.capability_snapshot.v1"]
    scorer_version: str = Field(min_length=1)
    source_data_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    generated_at: str
    profiles: list[CapabilityProfile]
    excluded_outcome_counts: dict[str, int]
    source_run_count: int = Field(ge=0)
    source_attempt_count: int = Field(ge=0)


class RebuildResult(StrictModel):
    snapshot: CapabilitySnapshot
    changed: bool


class EmpiricalScoreResolution(StrictModel):
    backend_name: str
    static_capability_score: float = Field(ge=0)
    empirical_status: Literal[
        "sufficient_data", "insufficient_data", "no_matching_profile"
    ]
    empirical_capability_score: int | None = Field(default=None, ge=0, le=100)
    conservative_success_probability: float | None = Field(default=None, ge=0, le=1)
    mean_actual_attempt_cost: float | None = Field(default=None, ge=0)
    expected_cost_to_success: float | None = Field(default=None, ge=0)
    capability_score_used: float = Field(ge=0)
    score_source: Literal["static", "empirical"]
    selected_level: str | None
    selected_profile_key: ProfileKey | None
    selected_profile_digest: str | None
    selected_sample_count: int
    minimum_empirical_samples: int = Field(ge=1)
    backoff_evidence: list[dict[str, object]]


class EmpiricalBackendInput(StrictModel):
    backend_name: str = Field(min_length=1)
    conservative_success_probability: float | None = Field(default=None, ge=0, le=1)
    mean_actual_attempt_cost: float | None = Field(default=None, ge=0)
    sufficient_probability_data: bool
    profile_version: str | None
    profile_digest: str | None
    sample_count: int = Field(ge=0)


class SequenceEvaluation(StrictModel):
    backends: tuple[str, ...]
    expected_cost: float = Field(ge=0)
    success_probability: float = Field(ge=0, le=1)
    worst_case_cost: float = Field(ge=0)
    reaches_target: bool


class SequenceOptimizationResult(StrictModel):
    optimizer_status: Literal["empirical", "bootstrap_fallback"]
    optimizer_version: str
    fallback_policy_version: str | None
    missing_inputs: tuple[str, ...]
    target_success_probability: float = Field(ge=0, le=1)
    max_attempts: int = Field(ge=0)
    known_cost_budget: float | None = Field(default=None, ge=0)
    input_backends: tuple[EmpiricalBackendInput, ...]
    considered_sequences: tuple[SequenceEvaluation, ...]
    chosen_sequence: tuple[str, ...]
    total_enumerated_sequences: int = Field(ge=0)
    feasible_sequence_count: int = Field(ge=0)
    rejected_by_cost_budget: int = Field(ge=0)
    omitted_sequence_count: int = Field(ge=0)
    pruning_rule: str | None
    pruned_backends: tuple[str, ...]
    formulas: dict[str, str]

