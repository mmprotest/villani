from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeatureProvenance(FrozenModel):
    source_kind: Literal[
        "repository_snapshot", "task_input", "classification", "aggregate"
    ]
    source_id: str = Field(min_length=1)
    digest_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class FeatureValue(FrozenModel):
    extractor_name: str = Field(min_length=1)
    extractor_version: str = Field(min_length=1)
    value: Any = None
    missing: bool
    provenance: tuple[FeatureProvenance, ...]

    @model_validator(mode="after")
    def explicit_missingness(self) -> "FeatureValue":
        if self.missing != (self.value is None):
            raise ValueError("missing must be true exactly when value is null")
        return self


class TaskFeatures(FrozenModel):
    schema_version: Literal["villani.task_features.v1"] = "villani.task_features.v1"
    feature_set_version: Literal["task_features_v1"] = "task_features_v1"
    run_id: str = Field(min_length=1)
    repository_snapshot_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    repository_size_bytes: FeatureValue
    repository_file_count: FeatureValue
    detected_languages: FeatureValue
    detected_build_systems: FeatureValue
    dependency_lockfiles: FeatureValue
    test_topology: FeatureValue
    requested_change_category: FeatureValue
    estimated_change_radius: FeatureValue
    security_sensitive_paths: FeatureValue
    required_tools_capabilities: FeatureValue
    context_size_estimates: FeatureValue
    historical_aggregates: FeatureValue
    input_provenance: tuple[FeatureProvenance, ...]


class CapabilityOption(FrozenModel):
    option_id: str
    backend_name: str
    agent_adapter: str
    provider: str
    model: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    capability_score: float
    enabled: bool
    estimated_cost_usd: float | None
    cost_accounting_status: Literal["complete", "partial", "unknown"]
    context_limit: int | None


class CapabilityCatalogSnapshot(FrozenModel):
    schema_version: Literal["villani.capability_catalog.v1"] = (
        "villani.capability_catalog.v1"
    )
    catalog_version: Literal["backend_agent_catalog_v1"] = "backend_agent_catalog_v1"
    snapshot_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generated_at: datetime
    options: tuple[CapabilityOption, ...]
    input_provenance: tuple[FeatureProvenance, ...]


class ShadowOption(FrozenModel):
    option_id: str
    backend_name: str
    agent_adapter: str
    estimated_cost_usd: float | None
    expected_success: float | None = Field(default=None, ge=0, le=1)
    rejection_reasons: tuple[str, ...] = ()


class ShadowRecommendation(FrozenModel):
    schema_version: Literal["villani.shadow_recommendation.v1"] = (
        "villani.shadow_recommendation.v1"
    )
    recommendation_id: str
    run_id: str
    decision_sequence: int = Field(ge=1)
    policy_version: str
    task_features_version: str
    capability_snapshot_id: str
    eligible_options: tuple[ShadowOption, ...]
    rejected_options: tuple[ShadowOption, ...]
    chosen_strategy: str | None
    expected_cost_usd: float | None = Field(default=None, ge=0)
    expected_success: float | None = Field(default=None, ge=0, le=1)
    uncertainty: float = Field(ge=0, le=1)
    timestamp: datetime
    advisory_only: Literal[True] = True
