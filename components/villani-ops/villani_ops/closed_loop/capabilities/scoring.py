"""Conservative empirical scoring and deterministic sparse-data backoff."""

from __future__ import annotations

import math

from .models import (
    CapabilitySnapshot,
    EmpiricalScoreResolution,
    ProfileKey,
)


WILSON_Z_95 = 1.959963984540054


def wilson_lower_bound(successes: int, sample_count: int) -> float:
    """Return the two-sided 95% Wilson score interval's lower endpoint."""

    if sample_count < 0 or successes < 0 or successes > sample_count:
        raise ValueError("successes must be between zero and sample_count")
    if sample_count == 0:
        return 0.0
    probability = successes / sample_count
    z_squared = WILSON_Z_95 * WILSON_Z_95
    denominator = 1.0 + z_squared / sample_count
    centre = probability + z_squared / (2.0 * sample_count)
    margin = WILSON_Z_95 * math.sqrt(
        probability * (1.0 - probability) / sample_count
        + z_squared / (4.0 * sample_count * sample_count)
    )
    return max(0.0, (centre - margin) / denominator)


def expected_cost_to_success(
    mean_attempt_cost: float | None,
    conservative_success_probability: float | None,
    *,
    sufficient: bool,
) -> float | None:
    """Return mean cost / conservative probability only for known inputs."""

    if (
        not sufficient
        or mean_attempt_cost is None
        or conservative_success_probability is None
        or conservative_success_probability <= 0
    ):
        return None
    return mean_attempt_cost / conservative_success_probability


def resolve_empirical_score(
    snapshot: CapabilitySnapshot | None,
    requested_key: ProfileKey,
    *,
    static_capability_score: float,
    minimum_empirical_samples: int = 20,
) -> EmpiricalScoreResolution:
    """Resolve a profile using the exact mandated sparse-data backoff order."""

    if minimum_empirical_samples < 1:
        raise ValueError("minimum_empirical_samples must be at least one")
    profiles = (
        {profile.key.sort_key(): profile for profile in snapshot.profiles}
        if snapshot is not None
        else {}
    )
    evidence: list[dict[str, object]] = []
    any_profile = False
    for level, key in requested_key.backoff_keys():
        profile = profiles.get(key.sort_key())
        sample_count = profile.sample_count if profile is not None else 0
        evidence.append(
            {
                "level": level,
                "profile_key": key.model_dump(mode="json"),
                "sample_count": sample_count,
                "minimum_empirical_samples": minimum_empirical_samples,
                "sufficient": bool(
                    profile is not None
                    and profile.sample_count >= minimum_empirical_samples
                ),
            }
        )
        if profile is None:
            continue
        any_profile = True
        if profile.sample_count < minimum_empirical_samples:
            continue
        lower_bound = profile.wilson_lower_bound
        empirical_score = math.floor(100.0 * lower_bound)
        return EmpiricalScoreResolution(
            backend_name=requested_key.backend_name,
            static_capability_score=static_capability_score,
            empirical_status="sufficient_data",
            empirical_capability_score=empirical_score,
            conservative_success_probability=lower_bound,
            mean_actual_attempt_cost=profile.mean_actual_attempt_cost,
            expected_cost_to_success=expected_cost_to_success(
                profile.mean_actual_attempt_cost, lower_bound, sufficient=True
            ),
            capability_score_used=float(empirical_score),
            score_source="empirical",
            selected_level=level,
            selected_profile_key=profile.key,
            selected_profile_digest=profile.source_data_digest,
            selected_sample_count=profile.sample_count,
            minimum_empirical_samples=minimum_empirical_samples,
            backoff_evidence=evidence,
        )
    return EmpiricalScoreResolution(
        backend_name=requested_key.backend_name,
        static_capability_score=static_capability_score,
        empirical_status=(
            "insufficient_data" if any_profile else "no_matching_profile"
        ),
        empirical_capability_score=None,
        conservative_success_probability=None,
        mean_actual_attempt_cost=None,
        expected_cost_to_success=None,
        capability_score_used=static_capability_score,
        score_source="static",
        selected_level=None,
        selected_profile_key=None,
        selected_profile_digest=None,
        selected_sample_count=max(
            (int(str(item["sample_count"])) for item in evidence), default=0
        ),
        minimum_empirical_samples=minimum_empirical_samples,
        backoff_evidence=evidence,
    )
