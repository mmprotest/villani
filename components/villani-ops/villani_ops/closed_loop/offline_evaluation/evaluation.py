from __future__ import annotations

import random
from collections import defaultdict
from statistics import fmean
from typing import Callable, Iterable

from .models import (
    ConfidenceInterval,
    EvaluationObservation,
    OfflineEvaluationReport,
    SegmentCalibration,
)


def _interval(
    values: list[float],
    *,
    minimum: int,
    seed: int,
    statistic: Callable[[list[float]], float] = fmean,
) -> ConfidenceInterval:
    if len(values) < minimum:
        return ConfidenceInterval(
            estimate=None,
            lower=None,
            upper=None,
            sample_count=len(values),
            status="insufficient_samples",
        )
    estimate = statistic(values)
    rng = random.Random(seed)
    samples = sorted(
        statistic([values[rng.randrange(len(values))] for _ in values])
        for _ in range(1000)
    )
    return ConfidenceInterval(
        estimate=estimate,
        lower=samples[24],
        upper=samples[974],
        sample_count=len(values),
        status="available",
    )


def validate_assignment_provenance(records: Iterable[EvaluationObservation]) -> None:
    missing = [
        row.unit_id
        for row in records
        if row.assignment_provenance is None or row.propensity is None
    ]
    if missing:
        raise ValueError(
            "training/evaluation publication refused: unknown assignment provenance or propensity "
            + ",".join(sorted(missing)[:10])
        )


def evaluate_policy(
    records: Iterable[EvaluationObservation],
    *,
    minimum_sample_size: int = 5,
    bootstrap_seed: int = 17,
    claim_causal_savings: bool = False,
) -> OfflineEvaluationReport:
    rows = tuple(records)
    observed = [row for row in rows if row.success is not None]
    censored = [row for row in rows if row.success is None]
    missing_propensity = [row for row in rows if row.propensity is None]
    refusal: list[str] = []
    provenance_complete = not any(
        row.assignment_provenance is None or row.propensity is None for row in rows
    )
    if missing_propensity:
        refusal.append("missing_assignment_propensity")
    if censored and any(row.propensity is None for row in censored):
        refusal.append("censored_data_without_propensity")
    if claim_causal_savings and (not provenance_complete or censored):
        refusal.append("invalid_causal_savings_claim")
    direct_success = _interval(
        [float(row.success) for row in observed],
        minimum=minimum_sample_size,
        seed=bootstrap_seed,
    )
    direct_cost = _interval(
        [row.cost_usd for row in observed if row.cost_usd is not None],
        minimum=minimum_sample_size,
        seed=bootstrap_seed + 1,
    )
    direct_latency = _interval(
        [row.latency_ms for row in observed if row.latency_ms is not None],
        minimum=minimum_sample_size,
        seed=bootstrap_seed + 2,
    )
    ips_values = [
        float(row.success) * row.target_probability / row.propensity
        for row in observed
        if row.propensity is not None
    ]
    ips = _interval(ips_values, minimum=minimum_sample_size, seed=bootstrap_seed + 3)
    if any(row.propensity is None for row in observed):
        ips = ConfidenceInterval(
            estimate=None,
            lower=None,
            upper=None,
            sample_count=len(ips_values),
            status="invalid",
        )
    dr_rows = [
        row
        for row in observed
        if row.propensity is not None
        and row.logged_outcome_prediction is not None
        and row.target_outcome_prediction is not None
        and row.outcome_model_inputs
    ]
    dr_values = [
        row.target_outcome_prediction
        + row.target_probability
        / row.propensity
        * (float(row.success) - row.logged_outcome_prediction)
        for row in dr_rows
    ]
    dr = _interval(dr_values, minimum=minimum_sample_size, seed=bootstrap_seed + 4)
    if len(dr_rows) != len(observed):
        refusal.append("doubly_robust_model_inputs_incomplete")
    segments: dict[str, list[EvaluationObservation]] = defaultdict(list)
    for row in observed:
        segments[row.segment].append(row)
    calibration = []
    for segment, values in sorted(segments.items()):
        predictions = [
            row.logged_outcome_prediction
            for row in values
            if row.logged_outcome_prediction is not None
        ]
        predicted = fmean(predictions) if len(predictions) == len(values) else None
        actual = fmean(float(row.success) for row in values)
        calibration.append(
            SegmentCalibration(
                segment=segment,
                sample_count=len(values),
                predicted_success=predicted,
                observed_success=actual,
                absolute_error=abs(predicted - actual)
                if predicted is not None
                else None,
            )
        )
    return OfflineEvaluationReport(
        raw_count=len(rows),
        observed_count=len(observed),
        censored_count=len(censored),
        missing_propensity_count=len(missing_propensity),
        direct_success=direct_success,
        direct_cost=direct_cost,
        direct_latency=direct_latency,
        inverse_propensity_success=ips,
        doubly_robust_success=dr,
        calibration=tuple(calibration),
        minimum_sample_size=minimum_sample_size,
        causal_savings_claim_valid=not claim_causal_savings or not refusal,
        refusal_reasons=tuple(sorted(set(refusal))),
        assignment_provenance_complete=provenance_complete,
    )
