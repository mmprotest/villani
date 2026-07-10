"""Pure report projections for capability list and explain commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from villani_ops.core.backend import Backend

from ..protocol import ClassificationSnapshot
from .ingest import SCORER_VERSION
from .models import CapabilitySnapshot, EmpiricalBackendInput, ProfileKey
from .optimizer import optimize_sequence
from .scoring import resolve_empirical_score


def version_inputs(configuration: Mapping[str, Any]) -> tuple[str, str, str]:
    capabilities = configuration.get("capabilities")
    values = capabilities if isinstance(capabilities, Mapping) else {}
    return (
        str(values.get("classifier_version") or "task_classifier_v1"),
        str(values.get("verifier_version") or "villani_ops_verifier_pipeline_v1"),
        str(values.get("scorer_version") or SCORER_VERSION),
    )


def profile_key_for(
    backend: Backend,
    classification: ClassificationSnapshot,
    configuration: Mapping[str, Any],
) -> ProfileKey:
    classifier, verifier, scorer = version_inputs(configuration)
    explicit_classifier = classification.metadata.get("classifier_version")
    return ProfileKey(
        backend_name=backend.name,
        provider=backend.provider,
        model=backend.model,
        task_category=classification.category,
        difficulty=classification.difficulty,
        risk=classification.risk,
        classifier_version=(
            str(explicit_classifier) if explicit_classifier else classifier
        ),
        verifier_version=verifier,
        scorer_version=scorer,
    )


def backend_score_rows(
    backends: Mapping[str, Backend],
    snapshot: CapabilitySnapshot | None,
    *,
    minimum_empirical_samples: int = 20,
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
            profile is not None and profile.sample_count >= minimum_empirical_samples
        )
        rows.append(
            {
                "backend_name": backend.name,
                "provider": backend.provider,
                "model": backend.model,
                "static_capability_score": backend.capability_score,
                "static_score_source": backend.capability_score_source,
                "empirical_status": "sufficient_data" if sufficient else "insufficient_data",
                "empirical_capability_score": (
                    int(100 * profile.wilson_lower_bound) if sufficient and profile else None
                ),
                "conservative_success_probability": (
                    profile.wilson_lower_bound if sufficient and profile else None
                ),
                "sample_count": profile.sample_count if profile else 0,
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
    resolutions = []
    optimizer_inputs = []
    for backend in sorted(backends.values(), key=lambda item: item.name):
        if "coding" not in backend.roles:
            continue
        resolution = resolve_empirical_score(
            snapshot,
            profile_key_for(backend, classification, configuration),
            static_capability_score=backend.capability_score,
            minimum_empirical_samples=minimum,
        )
        resolutions.append(resolution)
        if eligible_backend_names is None or backend.name in eligible_backend_names:
            optimizer_inputs.append(
                EmpiricalBackendInput(
                    backend_name=backend.name,
                    conservative_success_probability=resolution.conservative_success_probability,
                    mean_actual_attempt_cost=resolution.mean_actual_attempt_cost,
                    sufficient_probability_data=resolution.empirical_status == "sufficient_data",
                    profile_version=(
                        resolution.selected_profile_key.scorer_version
                        if resolution.selected_profile_key
                        else None
                    ),
                    profile_digest=resolution.selected_profile_digest,
                    sample_count=resolution.selected_sample_count,
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
            "target_success_probability": target,
            "backend_scores": [value.model_dump(mode="json") for value in resolutions],
            "optimization": optimization.model_dump(mode="json"),
            "path_used": (
                "empirical_sequence_v1"
                if optimization.optimizer_status == "empirical"
                else "bootstrap_v1"
            ),
        },
    }
