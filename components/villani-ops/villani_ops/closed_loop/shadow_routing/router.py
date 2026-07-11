from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from ..protocol import ClassificationSnapshot
from .models import (
    CapabilityCatalogSnapshot,
    ShadowOption,
    ShadowRecommendation,
    TaskFeatures,
)


class ShadowRouter:
    """Pure advisory scorer; deliberately does not implement ``PolicyEngine``."""

    policy_version = "shadow_router_v1"

    def recommend(
        self,
        *,
        run_id: str,
        decision_sequence: int,
        features: TaskFeatures,
        catalog: CapabilityCatalogSnapshot,
        classification: ClassificationSnapshot,
        timestamp: datetime,
        historical_by_backend: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ShadowRecommendation:
        required = set(features.required_tools_capabilities.value or [])
        eligible: list[ShadowOption] = []
        rejected: list[ShadowOption] = []
        history = historical_by_backend or {}
        for option in catalog.options:
            reasons: list[str] = []
            if not option.enabled:
                reasons.append("backend_disabled")
            if "coding" not in option.roles:
                reasons.append("coding_role_missing")
            missing = sorted(required - set(option.capabilities))
            if missing and option.capabilities:
                reasons.extend(f"missing_capability:{item}" for item in missing)
            observed = history.get(option.backend_name, {})
            success = observed.get("verified_success_rate")
            if (
                not isinstance(success, (int, float))
                or isinstance(success, bool)
                or not 0 <= success <= 1
            ):
                success = None
            value = ShadowOption(
                option_id=option.option_id,
                backend_name=option.backend_name,
                agent_adapter=option.agent_adapter,
                estimated_cost_usd=option.estimated_cost_usd,
                expected_success=success,
                rejection_reasons=tuple(reasons),
            )
            (rejected if reasons else eligible).append(value)
        ranked = sorted(
            eligible,
            key=lambda item: (
                (item.estimated_cost_usd / item.expected_success)
                if item.estimated_cost_usd is not None and item.expected_success
                else float("inf"),
                item.estimated_cost_usd
                if item.estimated_cost_usd is not None
                else float("inf"),
                -(item.expected_success or 0.0),
                item.backend_name,
            ),
        )
        chosen = ranked[0] if ranked else None
        uncertainty = (
            1.0
            if chosen is None or chosen.expected_success is None
            else round(1.0 - chosen.expected_success, 6)
        )
        return ShadowRecommendation(
            recommendation_id=f"shadow_{decision_sequence:03d}",
            run_id=run_id,
            decision_sequence=decision_sequence,
            policy_version=self.policy_version,
            task_features_version=features.feature_set_version,
            capability_snapshot_id=catalog.snapshot_id,
            eligible_options=tuple(
                sorted(eligible, key=lambda item: item.backend_name)
            ),
            rejected_options=tuple(
                sorted(rejected, key=lambda item: item.backend_name)
            ),
            chosen_strategy=chosen.option_id if chosen else None,
            expected_cost_usd=chosen.estimated_cost_usd if chosen else None,
            expected_success=chosen.expected_success if chosen else None,
            uncertainty=uncertainty,
            timestamp=timestamp,
        )
