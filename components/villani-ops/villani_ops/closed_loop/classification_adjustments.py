"""Typed, auditable policy adjustments for classifier output."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .interfaces import Classification


_LEVEL = {"low": 0, "easy": 0, "medium": 1, "high": 2, "hard": 2}


class ClassificationAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: Literal["difficulty", "risk", "confidence"]
    before: str | float
    after: str | float
    rule_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    authority: Literal["configured_policy", "schema_normalization"]
    timestamp: datetime


def apply_classification_policy(
    raw: Classification,
    configuration: Mapping[str, Any],
    *,
    timestamp: datetime,
) -> tuple[Classification, tuple[ClassificationAdjustment, ...], str]:
    """Derive effective classification solely from explicit configured rules."""

    policy_value = configuration.get("classification_policy")
    policy = policy_value if isinstance(policy_value, Mapping) else {}
    version = str(policy.get("version") or "classification-policy-v1")
    values: dict[str, Any] = {
        "difficulty": raw.difficulty,
        "risk": raw.risk,
        "confidence": raw.confidence,
    }
    adjustments: list[ClassificationAdjustment] = []

    for field, key in (("difficulty", "difficulty_floor"), ("risk", "risk_floor")):
        floor = policy.get(key)
        if not isinstance(floor, str) or floor not in _LEVEL:
            continue
        before = str(values[field])
        if _LEVEL[floor] > _LEVEL[before]:
            values[field] = floor
            adjustments.append(
                ClassificationAdjustment(
                    field=field,
                    before=before,
                    after=floor,
                    rule_id=f"{key}.v1",
                    reason=f"Configured {field} floor is {floor}.",
                    policy_version=version,
                    authority="configured_policy",
                    timestamp=timestamp,
                )
            )

    rules = policy.get("adjustments")
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, Mapping):
            continue
        field = str(rule.get("field") or "")
        target = rule.get("after")
        if field not in values or target is None or target == values[field]:
            continue
        before = values[field]
        if field in {"difficulty", "risk"}:
            if not isinstance(target, str) or target not in _LEVEL:
                raise ValueError(f"invalid configured {field} adjustment")
            if _LEVEL[target] < _LEVEL[str(before)] and rule.get("allow_reduction") is not True:
                raise ValueError(f"{field} reduction requires allow_reduction=true")
        elif not isinstance(target, (int, float)) or not 0 <= float(target) <= 1:
            raise ValueError("confidence adjustment must be between zero and one")
        rule_id = str(rule.get("rule_id") or "")
        reason = str(rule.get("reason") or "")
        if not rule_id or not reason:
            raise ValueError("classification adjustment requires rule_id and reason")
        values[field] = target
        adjustments.append(
            ClassificationAdjustment(
                field=field,  # type: ignore[arg-type]
                before=before,
                after=target,
                rule_id=rule_id,
                reason=reason,
                policy_version=version,
                authority="configured_policy",
                timestamp=timestamp,
            )
        )

    effective = Classification(
        difficulty=values["difficulty"],
        risk=values["risk"],
        category=raw.category,
        required_capabilities=raw.required_capabilities,
        estimated_attempts_needed=raw.estimated_attempts_needed,
        needs_tests=raw.needs_tests,
        confidence=float(values["confidence"]),
        reasoning_summary=raw.reasoning_summary,
        signals=raw.signals,
        metadata=raw.metadata,
    )
    return effective, tuple(adjustments), version
