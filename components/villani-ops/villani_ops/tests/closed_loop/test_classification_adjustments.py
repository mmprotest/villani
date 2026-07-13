from datetime import datetime, timezone

import pytest

from villani_ops.closed_loop.classification_adjustments import apply_classification_policy
from villani_ops.closed_loop.interfaces import Classification


NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def raw(**updates):
    values = {"difficulty": "easy", "risk": "low", "category": "maintenance"}
    values.update(updates)
    return Classification(**values)


def test_no_adjustment_preserves_raw_value():
    effective, adjustments, version = apply_classification_policy(raw(), {}, timestamp=NOW)
    assert effective == raw()
    assert adjustments == ()
    assert version == "classification-policy-v1"


def test_configured_floors_are_auditable_and_promote_only():
    effective, adjustments, _ = apply_classification_policy(
        raw(),
        {"classification_policy": {"version": "floor-2", "difficulty_floor": "hard", "risk_floor": "medium"}},
        timestamp=NOW,
    )
    assert (effective.difficulty, effective.risk) == ("hard", "medium")
    assert [item.rule_id for item in adjustments] == ["difficulty_floor.v1", "risk_floor.v1"]
    assert all(item.policy_version == "floor-2" for item in adjustments)


def test_silent_demotion_is_forbidden():
    with pytest.raises(ValueError, match="reduction requires"):
        apply_classification_policy(
            raw(difficulty="hard"),
            {"classification_policy": {"adjustments": [{"field": "difficulty", "after": "easy", "rule_id": "reduce", "reason": "configured"}]}},
            timestamp=NOW,
        )


def test_explicit_permitted_reduction_and_confidence_change_are_recorded():
    effective, adjustments, _ = apply_classification_policy(
        raw(difficulty="hard", confidence=0.8),
        {"classification_policy": {"adjustments": [
            {"field": "difficulty", "after": "medium", "allow_reduction": True, "rule_id": "reviewed-reduction", "reason": "Operator policy permits one level."},
            {"field": "confidence", "after": 0.6, "rule_id": "confidence-cap", "reason": "Classifier calibration cap."},
        ]}},
        timestamp=NOW,
    )
    assert effective.difficulty == "medium"
    assert effective.confidence == 0.6
    assert [item.field for item in adjustments] == ["difficulty", "confidence"]
