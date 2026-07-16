"""Pure report projections for capability list and explain commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from villani_ops.core.backend import Backend

from ..protocol import ClassificationSnapshot
from .effective import resolve_effective_capability
from .models import CapabilitySnapshot, EmpiricalBackendInput
from .optimizer import optimize_sequence


def backend_score_rows(
    backends: Mapping[str, Backend],
    snapshot: CapabilitySnapshot | None,
    *,
    minimum_empirical_samples: int = 20,
    minimum_empirical_wilson_lower_bound: float = 0.0,
) -> list[dict[str, Any]]:
    """Return static values plus the most informative global empirical profile."""

    profiles = snapshot.profiles if snapshot is not None else []
    rows: list[dict[str, Any]] = []
    for backend in sorted(backends.values(), key=lambda item: item.name):
        matches = [
            profile
            for profile in profiles
            if profile.key.backend_name == backend.name
            and profile.key.provider == backend.provider
            and profile.key.model == backend.model
            and profile.key.task_category == "*"
            and profile.key.difficulty == "*"
            and profile.key.risk == "*"
        ]
        profile = max(matches, key=lambda item: item.sample_count, default=None)
        sufficient = bool(
            profile is not None
            and profile.sample_count >= minimum_empirical_samples
            and profile.wilson_lower_bound >= minimum_empirical_wilson_lower_bound
        )
        rows.append(
            {
                "backend_name": backend.name,
                "provider": backend.provider,
                "model": backend.model,
                "static_capability_score": backend.capability_score,
                "static_score_source": backend.capability_score_source,
                "empirical_status": "sufficient_data"
                if sufficient
                else "insufficient_data",
                "empirical_capability_score": (
                    int(100 * profile.wilson_lower_bound)
                    if sufficient and profile
                    else None
                ),
                "conservative_success_probability": (
                    profile.wilson_lower_bound if sufficient and profile else None
                ),
                "sample_count": profile.sample_count if profile else 0,
                "minimum_wilson_lower_bound": minimum_empirical_wilson_lower_bound,
                "mean_actual_attempt_cost": (
                    profile.mean_actual_attempt_cost if profile else None
                ),
                "profile_digest": profile.source_data_digest if profile else None,
            }
        )
    return rows


def explain_routing(
    backends: Mapping[str, Backend],
    classification: ClassificationSnapshot,
    snapshot: CapabilitySnapshot | None,
    configuration: Mapping[str, Any],
    *,
    eligible_backend_names: set[str] | None = None,
    max_attempts: int = 3,
    known_cost_budget: float | None = None,
) -> dict[str, Any]:
    capabilities = configuration.get("capabilities")
    values = capabilities if isinstance(capabilities, Mapping) else {}
    minimum = int(values.get("minimum_empirical_samples", 20))
    target = float(values.get("target_success_probability", 0.80))
    configured_threshold = values.get("minimum_empirical_wilson_lower_bound")
    wilson_threshold = (
        float(configured_threshold) if configured_threshold is not None else None
    )
    resolutions = []
    optimizer_inputs = []
    for backend in sorted(backends.values(), key=lambda item: item.name):
        if "coding" not in backend.roles:
            continue
        resolution = resolve_effective_capability(
            backend,
            classification,
            snapshot,
            configuration,
        )
        resolutions.append(resolution)
        if eligible_backend_names is None or backend.name in eligible_backend_names:
            optimizer_inputs.append(
                EmpiricalBackendInput(
                    backend_name=backend.name,
                    conservative_success_probability=(
                        resolution.conservative_success_probability
                    ),
                    mean_actual_attempt_cost=resolution.mean_actual_attempt_cost,
                    sufficient_probability_data=(
                        resolution.capability_provenance == "qualified_empirical"
                        and resolution.conservative_success_probability is not None
                        and (
                            wilson_threshold is None
                            or resolution.conservative_success_probability
                            >= wilson_threshold
                        )
                    ),
                    profile_version=(
                        resolution.selected_profile_key.scorer_version
                        if resolution.selected_profile_key
                        else None
                    ),
                    profile_digest=resolution.selected_profile_digest,
                    sample_count=resolution.empirical_sample_count,
                    effective_capability_score=(
                        resolution.effective_capability_score
                    ),
                    mean_duration_ms=resolution.mean_duration_ms,
                    median_duration_ms=resolution.median_duration_ms,
                    profile_level=resolution.selected_level,
                    task_category_profile=(
                        resolution.selected_profile_key.task_category
                        if resolution.selected_profile_key
                        else None
                    ),
                    difficulty_profile=(
                        resolution.selected_profile_key.difficulty
                        if resolution.selected_profile_key
                        else None
                    ),
                    risk_profile=(
                        resolution.selected_profile_key.risk
                        if resolution.selected_profile_key
                        else None
                    ),
                    execution_environment_profile=(
                        backend.execution_environment
                    ),
                    probability_source=(
                        "wilson_lower_bound"
                        if resolution.capability_provenance
                        == "qualified_empirical"
                        else "missing"
                    ),
                    cost_source=(
                        "actual_profile_mean"
                        if resolution.mean_actual_attempt_cost is not None
                        else "missing"
                    ),
                    fallback_assumptions=tuple(resolution.missing_inputs),
                )
            )
    optimization = optimize_sequence(
        optimizer_inputs,
        max_attempts=max_attempts,
        known_cost_budget=known_cost_budget,
        target_success_probability=target,
    )
    return {
        "classification": classification.model_dump(mode="json"),
        "bootstrap": {
            "policy_version": "bootstrap_v1",
            "static_scores": {
                backend.name: backend.capability_score
                for backend in sorted(backends.values(), key=lambda item: item.name)
                if "coding" in backend.roles
            },
        },
        "empirical": {
            "snapshot_profile_digest": snapshot.profile_digest if snapshot else None,
            "minimum_empirical_samples": minimum,
            "minimum_empirical_wilson_lower_bound": wilson_threshold,
            "target_success_probability": target,
            "backend_scores": [
                value.model_dump(mode="json") for value in resolutions
            ],
            "optimization": optimization.model_dump(mode="json"),
            "path_used": (
                "empirical_sequence_v2"
                if optimization.optimizer_status == "empirical"
                else "bootstrap_fallback"
            ),
        },
    }
