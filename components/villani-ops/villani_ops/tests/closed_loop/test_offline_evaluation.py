from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from villani_ops.cli.unified import app
from villani_ops.closed_loop.offline_evaluation.assignment import assign_experiment
from villani_ops.closed_loop.offline_evaluation.evaluation import (
    evaluate_policy,
    validate_assignment_provenance,
)
from villani_ops.closed_loop.offline_evaluation.drift import monitor_drift
from villani_ops.closed_loop.offline_evaluation.optimizer import (
    SegmentedPolicyOptimizer,
)
from villani_ops.closed_loop.offline_evaluation.models import (
    EvaluationObservation,
    ExperimentArm,
    ExperimentConstraints,
    ExperimentDefinition,
    OptionEligibilityInput,
)


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _experiment(mode: str = "bounded_exploration") -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id="exp_1",
        experiment_version="v1",
        mode=mode,
        salt="fixture-salt-at-least-sixteen",
        arms=(
            ExperimentArm(name="control", probability=0.5, is_control=True),
            ExperimentArm(name="explore", option_id="safe", probability=0.5),
        ),
        policy_snapshot={"version": "shadow_v1"},
        constraints=ExperimentConstraints(
            minimum_capability_score=80,
            maximum_cost_usd=1,
            allowed_residencies=("au",),
            allowed_option_ids=("safe",),
            security_sensitive=True,
        ),
    )


def _safe() -> OptionEligibilityInput:
    return OptionEligibilityInput(
        option_id="safe",
        capability_score=90,
        estimated_cost_usd=0.5,
        residencies=("au",),
        security_approved=True,
    )


def test_assignment_is_reproducible_balanced_and_shadow_only_is_control() -> None:
    experiment = _experiment()
    options = {"safe": _safe()}
    first = assign_experiment(
        experiment, unit_id="stable-unit", options=options, timestamp=NOW
    )
    second = assign_experiment(
        experiment, unit_id="stable-unit", options=options, timestamp=NOW
    )
    assert first == second
    assert first.propensity == 0.5
    counts = Counter(
        assign_experiment(
            experiment, unit_id=f"unit-{index}", options=options, timestamp=NOW
        ).arm
        for index in range(10_000)
    )
    assert abs(counts["control"] / 10_000 - 0.5) < 0.03
    shadow = assign_experiment(
        _experiment("shadow_only"),
        unit_id="stable-unit",
        options=options,
        timestamp=NOW,
    )
    assert shadow.arm == "control"
    assert shadow.propensity == 1.0
    assert shadow.controls_live_execution is False


@pytest.mark.parametrize(
    "unsafe",
    [
        OptionEligibilityInput(
            option_id="safe",
            capability_score=20,
            estimated_cost_usd=0.5,
            residencies=("au",),
            security_approved=True,
        ),
        OptionEligibilityInput(
            option_id="safe",
            capability_score=90,
            estimated_cost_usd=2,
            residencies=("au",),
            security_approved=True,
        ),
        OptionEligibilityInput(
            option_id="safe",
            capability_score=90,
            estimated_cost_usd=0.5,
            residencies=("us",),
            security_approved=True,
        ),
        OptionEligibilityInput(
            option_id="safe",
            capability_score=90,
            estimated_cost_usd=0.5,
            residencies=("au",),
            security_approved=False,
        ),
        OptionEligibilityInput(
            option_id="safe",
            capability_score=90,
            estimated_cost_usd=0.5,
            residencies=("au",),
            security_approved=True,
            user_allowed=False,
        ),
    ],
)
def test_unsafe_options_have_zero_exploration_probability(
    unsafe: OptionEligibilityInput,
) -> None:
    assignments = [
        assign_experiment(
            _experiment(),
            unit_id=f"unit-{index}",
            options={"safe": unsafe},
            timestamp=NOW,
        )
        for index in range(500)
    ]
    assert {item.arm for item in assignments} == {"control"}
    assert {item.propensity for item in assignments} == {1.0}


def _observation(**updates) -> EvaluationObservation:
    value = {
        "unit_id": "unit",
        "segment": "small",
        "logged_arm": "control",
        "target_arm": "control",
        "success": True,
        "cost_usd": 1.0,
        "latency_ms": 100.0,
        "propensity": 0.5,
        "target_probability": 1.0,
        "assignment_provenance": {"experiment_id": "exp_1", "seed": "x"},
        "censored_reason": None,
        "logged_outcome_prediction": 0.7,
        "target_outcome_prediction": 0.7,
        "outcome_model_inputs": {"model_version": "explicit_v1", "segment": "small"},
        "backend_version": "backend-v1",
        "task_features": {"file_count": 10},
    }
    value.update(updates)
    return EvaluationObservation.model_validate(value)


def test_evaluation_refuses_unknown_provenance_and_invalid_causal_claim() -> None:
    censored = _observation(
        unit_id="censored",
        success=None,
        propensity=None,
        assignment_provenance=None,
        censored_reason="outcome_not_observed",
    )
    report = evaluate_policy(
        (censored,), minimum_sample_size=2, claim_causal_savings=True
    )
    assert "censored_data_without_propensity" in report.refusal_reasons
    assert "invalid_causal_savings_claim" in report.refusal_reasons
    assert report.causal_savings_claim_valid is False
    with pytest.raises(ValueError, match="publication refused"):
        validate_assignment_provenance((censored,))


def test_fixture_replay_cli_writes_raw_counts_confidence_intervals_and_dr(
    tmp_path: Path,
) -> None:
    fixture = (
        Path(__file__).resolve().parents[5]
        / "integration"
        / "fixtures"
        / "offline_evaluation"
        / "shadow_outcome_dataset.json"
    )
    json_output, markdown_output = tmp_path / "report.json", tmp_path / "report.md"
    result = CliRunner().invoke(
        app,
        [
            "evaluate",
            "replay",
            "--input",
            str(fixture),
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
            "--minimum-samples",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(json_output.read_text(encoding="utf-8"))
    assert report["evaluation"]["raw_count"] == 8
    assert report["evaluation"]["direct_success"]["lower"] is not None
    assert report["evaluation"]["doubly_robust_success"]["status"] == "available"
    assert "Raw observations: 8" in markdown_output.read_text(encoding="utf-8")
    assert report["controls_live_execution"] is False


def test_segmented_optimizer_and_drift_cover_required_signals() -> None:
    baseline = tuple(
        _observation(
            unit_id=f"base-{index}",
            logged_arm="economy",
            success=True,
            cost_usd=0.1,
            latency_ms=100,
            task_features={"file_count": 10},
        )
        for index in range(5)
    )
    current = tuple(
        _observation(
            unit_id=f"current-{index}",
            logged_arm="quality",
            success=False,
            cost_usd=0.4,
            latency_ms=300,
            backend_version="backend-v2",
            logged_outcome_prediction=0.9,
            task_features={"file_count": 30},
        )
        for index in range(5)
    )
    optimized = SegmentedPolicyOptimizer(
        minimum_samples=5, minimum_success=0.5
    ).optimize(baseline + current)
    assert optimized.choices[0].option_id == "economy"
    assert optimized.controls_live_execution is False
    drift = monitor_drift(baseline, current, threshold=0.2)
    names = {signal.name for signal in drift.signals}
    assert {
        "task_feature:file_count",
        "backend_versions",
        "success_rate",
        "cost",
        "latency",
        "calibration",
    } <= names
    assert drift.drifted is True
