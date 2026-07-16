"""Deterministic capability resolution with provenance and uncertainty."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from villani_ops.core.backend import Backend

from ..protocol import ClassificationSnapshot
from .models import CapabilityProfile, CapabilitySnapshot, EffectiveCapability, ProfileKey


DEFAULT_SCORER_VERSION = "empirical_wilson_v1"


class CapabilityResolutionConfiguration(BaseModel):
    model_config = ConfigDict(extra="ignore")

    minimum_empirical_samples: int = Field(default=20, ge=1)
    target_success_probability: float = Field(default=0.80, ge=0, le=1)
    minimum_empirical_wilson_lower_bound: float | None = Field(
        default=None, ge=0, le=1
    )
    manual_uncertainty_penalty: float = Field(default=20, ge=0, le=100)
    bootstrap_uncertainty_penalty: float = Field(default=25, ge=0, le=100)
    observed_uncertainty_penalty_max: float = Field(default=15, ge=0, le=100)
    allow_manual_hard_task_qualification: bool = False
    allow_bootstrap_threshold_bypass: bool = False
    classifier_version: str = Field(default="task_classifier_v1", min_length=1)
    verifier_version: str = Field(
        default="villani_ops_verifier_pipeline_v1", min_length=1
    )
    scorer_version: str = Field(default=DEFAULT_SCORER_VERSION, min_length=1)


def _capability_mapping(configuration: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = configuration.get("capabilities")
    return nested if isinstance(nested, Mapping) else configuration


def capability_resolution_configuration(
    configuration: Mapping[str, Any],
) -> CapabilityResolutionConfiguration:
    return CapabilityResolutionConfiguration.model_validate(
        _capability_mapping(configuration)
    )


def version_inputs(configuration: Mapping[str, Any]) -> tuple[str, str, str]:
    values = capability_resolution_configuration(configuration)
    return (
        values.classifier_version,
        values.verifier_version,
        values.scorer_version,
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


def explicit_capability_override(backend: Backend) -> bool:
    """Return only an affirmative operator override, never an entered score."""

    source = backend.capability_score_source.strip().lower()
    return bool(
        backend.metadata.get("manual_capability_override")
        or backend.metadata.get("explicit_capability_override")
        or source in {"manual_override", "explicit_override"}
    )


def _bootstrap_backend_name(configuration: Mapping[str, Any]) -> str | None:
    management = configuration.get("model_management")
    values = management if isinstance(management, Mapping) else {}
    explicit = values.get("bootstrap_default")
    if isinstance(explicit, str) and explicit:
        return explicit
    setup = configuration.get("setup")
    setup_values = setup if isinstance(setup, Mapping) else {}
    legacy = setup_values.get("primary_backend")
    return legacy if isinstance(legacy, str) and legacy else None


def _is_bootstrap_backend(
    backend: Backend, configuration: Mapping[str, Any]
) -> bool:
    return bool(
        _bootstrap_backend_name(configuration) == backend.name
        or backend.metadata.get("bootstrap_default") is True
    )


def _profile_lookup(
    snapshot: CapabilitySnapshot | None,
) -> dict[tuple[str, ...], CapabilityProfile]:
    if snapshot is None:
        return {}
    return {profile.key.sort_key(): profile for profile in snapshot.profiles}


def resolve_effective_capability(
    backend: Backend,
    classification: ClassificationSnapshot,
    capability_snapshot: CapabilitySnapshot | None,
    routing_config: Mapping[str, Any],
) -> EffectiveCapability:
    """Resolve the conservative score that is authoritative for routing.

    Qualified profiles use the Wilson lower bound. Sparse observations remain
    provisional and can only lower a configured estimate. Manual and bootstrap
    estimates receive their configured uncertainty penalties. An operator
    override is explicit provenance, not an inference from a numeric score.
    """

    configuration = capability_resolution_configuration(routing_config)
    requested_key = profile_key_for(backend, classification, routing_config)
    profiles = _profile_lookup(capability_snapshot)
    evidence: list[dict[str, object]] = []
    first_observed: tuple[str, CapabilityProfile] | None = None
    qualified: tuple[str, CapabilityProfile] | None = None
    for level, key in requested_key.backoff_keys():
        profile = profiles.get(key.sort_key())
        if profile is not None and profile.sample_count > 0 and first_observed is None:
            first_observed = (level, profile)
        sufficient = bool(
            profile is not None
            and profile.sample_count >= configuration.minimum_empirical_samples
        )
        evidence.append(
            {
                "level": level,
                "profile_key": key.model_dump(mode="json"),
                "sample_count": profile.sample_count if profile else 0,
                "successes": profile.successes if profile else 0,
                "wilson_lower_bound": (
                    profile.wilson_lower_bound if profile else None
                ),
                "mean_actual_attempt_cost": (
                    profile.mean_actual_attempt_cost if profile else None
                ),
                "median_duration_ms": profile.median_duration_ms if profile else None,
                "minimum_empirical_samples": (
                    configuration.minimum_empirical_samples
                ),
                "sufficient": sufficient,
            }
        )
        if sufficient and profile is not None:
            qualified = (level, profile)
            break

    configured = float(max(min(backend.capability_score, 100), 0))
    selected = qualified or first_observed
    selected_level = selected[0] if selected else None
    profile = selected[1] if selected else None
    sample_count = profile.sample_count if profile else 0
    wilson = profile.wilson_lower_bound if profile else None
    missing: list[str] = []
    if profile is None:
        missing.append("no_matching_empirical_profile")
    elif qualified is None:
        missing.append("insufficient_empirical_samples")
    if profile is None or profile.mean_actual_attempt_cost is None:
        missing.append("mean_actual_attempt_cost")
    if profile is None or (
        profile.median_duration_ms is None and profile.mean_duration_ms is None
    ):
        missing.append("observed_duration")

    if explicit_capability_override(backend):
        return EffectiveCapability(
            backend_name=backend.name,
            configured_capability_score=configured,
            effective_capability_score=configured,
            capability_provenance="explicit_override",
            capability_confidence="operator_override",
            uncertainty_penalty=0,
            empirical_sample_count=sample_count,
            empirical_wilson_lower_bound=wilson,
            qualification_status="explicit_override",
            conservative_success_probability=(
                wilson if qualified is not None else None
            ),
            selected_level=selected_level,
            selected_profile_key=profile.key if profile else None,
            selected_profile_digest=profile.source_data_digest if profile else None,
            mean_actual_attempt_cost=(
                profile.mean_actual_attempt_cost if profile else None
            ),
            median_actual_attempt_cost=(
                profile.median_actual_attempt_cost if profile else None
            ),
            mean_duration_ms=profile.mean_duration_ms if profile else None,
            median_duration_ms=profile.median_duration_ms if profile else None,
            override_applied=True,
            backoff_evidence=evidence,
            missing_inputs=missing,
        )

    if qualified is not None and profile is not None:
        qualified_score = math.floor(100.0 * profile.wilson_lower_bound)
        return EffectiveCapability(
            backend_name=backend.name,
            configured_capability_score=configured,
            effective_capability_score=float(qualified_score),
            capability_provenance="qualified_empirical",
            capability_confidence="high",
            uncertainty_penalty=0,
            empirical_sample_count=profile.sample_count,
            empirical_wilson_lower_bound=profile.wilson_lower_bound,
            qualification_status="qualified",
            conservative_success_probability=profile.wilson_lower_bound,
            selected_level=selected_level,
            selected_profile_key=profile.key,
            selected_profile_digest=profile.source_data_digest,
            mean_actual_attempt_cost=profile.mean_actual_attempt_cost,
            median_actual_attempt_cost=profile.median_actual_attempt_cost,
            mean_duration_ms=profile.mean_duration_ms,
            median_duration_ms=profile.median_duration_ms,
            backoff_evidence=evidence,
            missing_inputs=missing,
        )

    if first_observed is not None and profile is not None:
        sample_fraction = min(
            profile.sample_count / configuration.minimum_empirical_samples, 1.0
        )
        observed_penalty = math.ceil(
            configuration.observed_uncertainty_penalty_max
            * (1.0 - sample_fraction)
        )
        provisional = math.floor(100.0 * profile.wilson_lower_bound)
        observed_score = max(
            min(configured, float(provisional)) - observed_penalty, 0.0
        )
        return EffectiveCapability(
            backend_name=backend.name,
            configured_capability_score=configured,
            effective_capability_score=observed_score,
            capability_provenance="observed",
            capability_confidence=(
                "medium" if sample_fraction >= 0.5 else "low"
            ),
            uncertainty_penalty=float(observed_penalty),
            empirical_sample_count=profile.sample_count,
            empirical_wilson_lower_bound=profile.wilson_lower_bound,
            qualification_status="provisional",
            conservative_success_probability=profile.wilson_lower_bound,
            selected_level=selected_level,
            selected_profile_key=profile.key,
            selected_profile_digest=profile.source_data_digest,
            mean_actual_attempt_cost=profile.mean_actual_attempt_cost,
            median_actual_attempt_cost=profile.median_actual_attempt_cost,
            mean_duration_ms=profile.mean_duration_ms,
            median_duration_ms=profile.median_duration_ms,
            backoff_evidence=evidence,
            missing_inputs=missing,
        )

    bootstrap = _is_bootstrap_backend(backend, routing_config)
    manual_penalty = (
        configuration.bootstrap_uncertainty_penalty
        if bootstrap
        else configuration.manual_uncertainty_penalty
    )
    return EffectiveCapability(
        backend_name=backend.name,
        configured_capability_score=configured,
        effective_capability_score=max(configured - manual_penalty, 0.0),
        capability_provenance="bootstrap" if bootstrap else "manual",
        capability_confidence="low",
        uncertainty_penalty=manual_penalty,
        empirical_sample_count=0,
        empirical_wilson_lower_bound=None,
        qualification_status="estimated",
        conservative_success_probability=None,
        backoff_evidence=evidence,
        missing_inputs=missing,
    )


__all__ = [
    "CapabilityResolutionConfiguration",
    "capability_resolution_configuration",
    "explicit_capability_override",
    "profile_key_for",
    "resolve_effective_capability",
    "version_inputs",
]
