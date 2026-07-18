"""Deterministic PT7 statistics and append-only observation projection."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from statistics import median
from typing import Iterable

from ..capabilities.scoring import WILSON_Z_95
from .models import (
    QualificationDistribution,
    QualificationDriftFlag,
    QualificationObservation,
    QualificationStatistics,
)


def active_observations(
    observations: Iterable[QualificationObservation],
) -> tuple[list[QualificationObservation], int]:
    """Keep the newest immutable projection for each source trial."""

    grouped: dict[tuple[str, str, str], list[QualificationObservation]] = defaultdict(
        list
    )
    for observation in observations:
        grouped[
            (
                observation.source_kind,
                observation.source_suite_id or "",
                observation.source_trial_id,
            )
        ].append(observation)
    active: list[QualificationObservation] = []
    superseded = 0
    for key in sorted(grouped):
        values = sorted(
            grouped[key],
            key=lambda item: (item.recorded_at, item.observation_id),
        )
        active.append(values[-1])
        superseded += len(values) - 1
    return active, superseded


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def wilson_lower_bound(
    successes: int,
    sample_count: int,
    *,
    z: float = WILSON_Z_95,
) -> float:
    """Return the configured Wilson interval lower endpoint."""

    if sample_count < 0 or successes < 0 or successes > sample_count:
        raise ValueError("successes must be between zero and sample_count")
    if z <= 0:
        raise ValueError("z must be positive")
    if sample_count == 0:
        return 0.0
    probability = successes / sample_count
    z_squared = z * z
    denominator = 1.0 + z_squared / sample_count
    centre = probability + z_squared / (2.0 * sample_count)
    margin = z * math.sqrt(
        probability * (1.0 - probability) / sample_count
        + z_squared / (4.0 * sample_count * sample_count)
    )
    return max(0.0, (centre - margin) / denominator)


def distribution(
    values: Iterable[float | int | None], *, unit: str
) -> QualificationDistribution:
    total = list(values)
    materialized = [float(value) for value in total if value is not None]
    unknown = max(len(total) - len(materialized), 0)
    if not materialized:
        return QualificationDistribution(
            known_count=0,
            unknown_count=unknown,
            minimum=None,
            median=None,
            p90=None,
            maximum=None,
            unit=unit,
        )
    return QualificationDistribution(
        known_count=len(materialized),
        unknown_count=unknown,
        minimum=min(materialized),
        median=float(median(materialized)),
        p90=_percentile(materialized, 0.90),
        maximum=max(materialized),
        unit=unit,
    )


def qualification_statistics(
    observations: Iterable[QualificationObservation],
    *,
    drift_flags: Iterable[QualificationDriftFlag] = (),
    wilson_z: float = WILSON_Z_95,
) -> QualificationStatistics:
    rows = list(observations)
    eligible = [item for item in rows if item.eligible]
    successes = sum(item.successful is True for item in eligible)
    failures = len(eligible) - successes
    exclusions = Counter(
        item.exclusion_reason or "unspecified_exclusion"
        for item in rows
        if not item.eligible
    )
    rate = successes / len(eligible) if eligible else None
    wilson = (
        wilson_lower_bound(successes, len(eligible), z=wilson_z) if eligible else None
    )

    cost_by_currency: dict[str, list[float | None]] = defaultdict(list)
    accepted_cost_by_currency: dict[str, list[float | None]] = defaultdict(list)
    known_currencies = sorted(
        {
            str(item.cost_currency).upper()
            for item in eligible
            if item.cost_amount is not None and item.cost_currency is not None
        }
    )
    for currency in known_currencies:
        for item in eligible:
            if (
                item.cost_amount is not None
                and item.cost_currency is not None
                and item.cost_currency.upper() == currency
            ):
                cost_by_currency[currency].append(item.cost_amount)
                if item.successful:
                    accepted_cost_by_currency[currency].append(item.cost_amount)

    versions: dict[str, set[str]] = defaultdict(set)
    for item in eligible:
        for name, version in item.system.software_versions.items():
            versions[name].add(version)

    false_cases = sorted(
        item.observation_id
        for item in eligible
        if item.false_acceptance
        or item.false_rejection
        or item.later_rollback
        or item.reopened_defect
    )
    return QualificationStatistics(
        sample_count=len(eligible),
        successes=successes,
        failures=failures,
        exclusions=dict(sorted(exclusions.items())),
        acceptance_rate=rate,
        wilson_lower_bound=wilson,
        proved_acceptable_count=sum(
            item.proved_acceptable is True for item in eligible
        ),
        accepted_as_is_count=sum(item.accepted_as_is is True for item in eligible),
        false_acceptance_count=sum(
            item.false_acceptance or item.later_rollback or item.reopened_defect
            for item in eligible
        ),
        false_rejection_count=sum(item.false_rejection for item in eligible),
        false_case_ids=false_cases,
        cost_distribution_by_currency={
            currency: distribution(values, unit=currency)
            for currency, values in sorted(cost_by_currency.items())
        },
        cost_unknown_count=sum(
            item.cost_amount is None or item.cost_currency is None for item in eligible
        ),
        accepted_change_cost_by_currency={
            currency: distribution(values, unit=currency)
            for currency, values in sorted(accepted_cost_by_currency.items())
        },
        accepted_change_cost_unknown_count=sum(
            item.successful is True
            and (item.cost_amount is None or item.cost_currency is None)
            for item in eligible
        ),
        duration_distribution=distribution(
            [item.duration_ms for item in eligible], unit="ms"
        ),
        review_minutes_distribution=distribution(
            [item.review_minutes for item in eligible], unit="minutes"
        ),
        last_evidence_at=max((item.observed_at for item in eligible), default=None),
        software_version_diversity={
            name: sorted(values) for name, values in sorted(versions.items())
        },
        drift_flags=sorted(
            drift_flags,
            key=lambda item: (item.severity, item.code, item.detail),
        ),
    )


__all__ = [
    "WILSON_Z_95",
    "active_observations",
    "distribution",
    "qualification_statistics",
    "wilson_lower_bound",
]
