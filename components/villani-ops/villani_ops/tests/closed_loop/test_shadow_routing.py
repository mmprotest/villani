from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.interfaces import Classification, ClosedLoopRunRequest
from villani_ops.closed_loop.protocol import ClassificationSnapshot
from villani_ops.closed_loop.shadow_routing import extract_task_features
from villani_ops.tests.closed_loop.fakes import (
    PATCH_ONE,
    FakeAttemptRunner,
    FakeClassifier,
    FakeMaterializer,
    FakeMonotonic,
    FakePolicyEngine,
    FakeSelector,
    FakeVerifier,
    FixedNow,
    StableIds,
    accepted_verification,
    attempt,
    backend,
    policy,
)


def _classification() -> ClassificationSnapshot:
    return ClassificationSnapshot(
        schema_version="villani.classification.v1",
        classification_id="classification_001",
        run_id="run_1",
        task_id="task_1",
        classified_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        difficulty="medium",
        risk="low",
        category="feature",
        required_capabilities=["python"],
        estimated_attempts_needed=1,
        needs_tests=True,
        confidence=0.9,
        reasoning_summary="test",
        signals={"likely_file_count": 2},
        metadata={},
        llm_usage=[],
    )


def test_task_features_are_deterministic_and_missingness_is_explicit(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('x')\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_x(): pass\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    arguments = dict(
        run_id="run_1",
        task="Add a Python feature",
        success_criteria="pytest passes",
        classification=_classification(),
    )

    first = extract_task_features(tmp_path, **arguments)
    second = extract_task_features(tmp_path, **arguments)

    assert first == second
    assert first.detected_languages.value == ["python"]
    assert first.repository_file_count.value == 3
    assert first.historical_aggregates.missing is True
    assert first.historical_aggregates.value is None
    assert all(
        value.extractor_version.endswith("_v1")
        for value in (
            first.repository_size_bytes,
            first.detected_languages,
            first.test_topology,
            first.requested_change_category,
            first.context_size_estimates,
        )
    )


def test_shadow_artifact_cannot_replace_production_policy_choice(
    tmp_path: Path,
) -> None:
    target = tmp_path / "repository"
    target.mkdir()
    (target / "example.txt").write_text("old\n", encoding="utf-8")
    production = backend("production-low")
    policy_engine = FakePolicyEngine(
        [policy("attempt", backend_option=production), policy("select")]
    )
    controller = ClosedLoopController(
        classifier=FakeClassifier(
            Classification(
                difficulty="easy",
                risk="low",
                category="test",
                required_capabilities=("python",),
                confidence=0.99,
            )
        ),
        policy_engine=policy_engine,
        attempt_runner=FakeAttemptRunner([attempt(patch=PATCH_ONE)]),
        verifier=FakeVerifier([accepted_verification()]),
        selector=FakeSelector(),
        materializer=FakeMaterializer(),
        now=FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
    )
    request = ClosedLoopRunRequest(
        task="Implement the change",
        repository_path=target,
        success_criteria="The test passes",
        runs_root=tmp_path / "runs",
        max_attempts=1,
        policy_configuration={
            "version": "fake_v1",
            "backends": {
                "shadow-cheap": {
                    "provider": "local",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "model": "shadow-model",
                    "roles": ["classification", "coding"],
                    "billing_mode": "fixed",
                    "fixed_cost_per_attempt": 0.1,
                    "capability_score": 90,
                    "metadata": {"capabilities": ["python"]},
                }
            },
        },
    )

    result = controller.run(request)

    decision = json.loads(
        (result.run_directory / "policy_decisions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    shadow = json.loads(
        (result.run_directory / "shadow_recommendations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert decision["chosen_backend"] == "production-low"
    assert shadow["advisory_only"] is True
    assert shadow["chosen_strategy"] == "shadow-cheap:shadow-model"
    assert shadow["chosen_strategy"] != decision["chosen_backend"]
    assert (
        policy_engine.calls[0].classification.classification_id == "classification_001"
    )
