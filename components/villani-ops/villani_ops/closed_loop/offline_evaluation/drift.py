from __future__ import annotations

from collections import Counter
from statistics import fmean
from typing import Any, Iterable

from .models import DriftReport, DriftSignal, EvaluationObservation


def _relative(baseline: float, current: float) -> float:
    return abs(current - baseline) / max(abs(baseline), 1e-9)


def _categorical_distance(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = Counter(left), Counter(right)
    total_a, total_b = sum(a.values()), sum(b.values())
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a[key] / total_a - b[key] / total_b) for key in keys)


def monitor_drift(
    baseline: tuple[EvaluationObservation, ...],
    current: tuple[EvaluationObservation, ...],
    *,
    threshold: float = 0.2,
) -> DriftReport:
    if not baseline or not current:
        raise ValueError(
            "drift monitoring requires non-empty baseline and current samples"
        )
    signals: list[DriftSignal] = []
    feature_names = sorted(
        set().union(*(row.task_features for row in baseline + current))
    )
    for name in feature_names:
        left = [row.task_features.get(name) for row in baseline]
        right = [row.task_features.get(name) for row in current]
        numeric = all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in left + right
        )
        magnitude = (
            _relative(fmean(left), fmean(right))  # type: ignore[arg-type]
            if numeric
            else _categorical_distance(map(str, left), map(str, right))
        )
        signals.append(
            DriftSignal(
                name=f"task_feature:{name}",
                baseline_value=left,
                current_value=right,
                magnitude=magnitude,
                threshold=threshold,
                drifted=magnitude > threshold,
            )
        )

    def metric(name: str, getter: Any) -> None:
        left = [value for row in baseline if (value := getter(row)) is not None]
        right = [value for row in current if (value := getter(row)) is not None]
        magnitude = _relative(fmean(left), fmean(right)) if left and right else None
        signals.append(
            DriftSignal(
                name=name,
                baseline_value=fmean(left) if left else None,
                current_value=fmean(right) if right else None,
                magnitude=magnitude,
                threshold=threshold,
                drifted=magnitude is None or magnitude > threshold,
            )
        )

    signals.append(
        DriftSignal(
            name="backend_versions",
            baseline_value=sorted({row.backend_version for row in baseline}),
            current_value=sorted({row.backend_version for row in current}),
            magnitude=_categorical_distance(
                (row.backend_version for row in baseline),
                (row.backend_version for row in current),
            ),
            threshold=threshold,
            drifted={row.backend_version for row in baseline}
            != {row.backend_version for row in current},
        )
    )
    metric(
        "success_rate",
        lambda row: float(row.success) if row.success is not None else None,
    )
    metric("cost", lambda row: row.cost_usd)
    metric("latency", lambda row: row.latency_ms)
    metric(
        "calibration",
        lambda row: (
            abs(float(row.success) - row.logged_outcome_prediction)
            if row.success is not None and row.logged_outcome_prediction is not None
            else None
        ),
    )
    return DriftReport(
        signals=tuple(signals), drifted=any(item.drifted for item in signals)
    )
