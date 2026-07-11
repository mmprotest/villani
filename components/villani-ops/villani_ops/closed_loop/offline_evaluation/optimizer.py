from __future__ import annotations

from collections import defaultdict
from math import sqrt
from statistics import fmean
from typing import Protocol

from .models import EvaluationObservation, OptimizedPolicy, SegmentPolicyChoice


class PolicyOptimizer(Protocol):
    def optimize(
        self, records: tuple[EvaluationObservation, ...]
    ) -> OptimizedPolicy: ...


class SegmentedPolicyOptimizer:
    """Transparent conservative estimates; no learned neural routing."""

    def __init__(
        self, *, minimum_samples: int = 5, minimum_success: float = 0.5
    ) -> None:
        self.minimum_samples = minimum_samples
        self.minimum_success = minimum_success

    def optimize(self, records: tuple[EvaluationObservation, ...]) -> OptimizedPolicy:
        grouped: dict[tuple[str, str], list[EvaluationObservation]] = defaultdict(list)
        for row in records:
            if row.success is not None:
                grouped[(row.segment, row.logged_arm)].append(row)
        segments = sorted({row.segment for row in records})
        choices: list[SegmentPolicyChoice] = []
        for segment in segments:
            candidates = []
            max_count = 0
            for (candidate_segment, arm), rows in grouped.items():
                if candidate_segment != segment:
                    continue
                max_count = max(max_count, len(rows))
                if len(rows) < self.minimum_samples:
                    continue
                rate = fmean(float(row.success) for row in rows)
                conservative = max(
                    0.0, rate - 1.96 * sqrt(rate * (1 - rate) / len(rows))
                )
                costs = [row.cost_usd for row in rows if row.cost_usd is not None]
                if conservative >= self.minimum_success and len(costs) == len(rows):
                    candidates.append(
                        (fmean(costs), -conservative, arm, len(rows), conservative)
                    )
            if not candidates:
                choices.append(
                    SegmentPolicyChoice(
                        segment=segment,
                        option_id=None,
                        sample_count=max_count,
                        conservative_success=None,
                        expected_cost_usd=None,
                        status="insufficient_samples"
                        if max_count < self.minimum_samples
                        else "no_safe_option",
                    )
                )
                continue
            cost, _, arm, count, conservative = min(candidates)
            choices.append(
                SegmentPolicyChoice(
                    segment=segment,
                    option_id=arm,
                    sample_count=count,
                    conservative_success=conservative,
                    expected_cost_usd=cost,
                    status="selected",
                )
            )
        return OptimizedPolicy(choices=tuple(choices))
