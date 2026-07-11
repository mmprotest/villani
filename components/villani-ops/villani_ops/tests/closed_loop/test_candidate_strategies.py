from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from villani_ops.closed_loop.candidate_strategies import (
    CandidateDimensions,
    CandidateObservation,
    CandidateScheduler,
    ReliabilityStrategyConfiguration,
    adaptive_stop,
    build_candidate_plans,
    diversity_summary,
)
from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.interfaces import ClosedLoopRunRequest
from villani_ops.tests.closed_loop.fakes import (
    FakeClassifier,
    FakeMaterializer,
    FakeSelector,
    FixedNow,
    StableIds,
    accepted_verification,
    attempt,
    backend,
    policy,
)


BASELINE = "a" * 64


def _configuration(**updates: Any) -> ReliabilityStrategyConfiguration:
    values: dict[str, Any] = {
        "strategy": "parallel_diverse_candidates",
        "stop_policy": "stop_on_sufficient",
        "accepted_candidate_requirement": 1,
        "maximum_candidates": 4,
        "maximum_parallelism": 2,
    }
    values.update(updates)
    return ReliabilityStrategyConfiguration.model_validate(values)


def _plans(configuration: ReliabilityStrategyConfiguration):
    return build_candidate_plans(
        configuration,
        baseline_sha256=BASELINE,
        default_dimensions=CandidateDimensions(backend_name="backend", model="model"),
    )


def test_parallel_stop_on_sufficient_bounds_concurrency_and_cancels_independently(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    active = maximum = 0
    lock = threading.Lock()

    def generate(plan: Any, cancellation: threading.Event) -> str:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            if plan.ordinal == 1:
                time.sleep(0.02)
                return "sufficient"
            while not cancellation.wait(0.01):
                pass
            return "cancelled-cleanly"
        finally:
            with lock:
                active -= 1

    def verify(plan: Any, result: str) -> CandidateObservation:
        return CandidateObservation(
            candidate_id=plan.candidate_id,
            acceptance_eligible=result == "sufficient",
            verifier_confidence=0.99,
            evidence_grade="strong" if result == "sufficient" else "none",
        )

    executions, accounting = CandidateScheduler(
        configuration, journal_path=tmp_path / "schedule.jsonl"
    ).execute(
        _plans(configuration),
        generate=generate,
        verify=verify,
        remaining_attempt_budget=4,
    )

    assert maximum <= 2
    assert accounting.maximum_observed_concurrency <= 2
    assert (
        sum(
            bool(item.observation and item.observation.acceptance_eligible)
            for item in executions
        )
        == 1
    )
    assert accounting.cancelled_attempts == 1
    assert accounting.avoided_attempts == 2


def test_comparison_policy_collects_required_eligible_candidates(
    tmp_path: Path,
) -> None:
    configuration = _configuration(
        stop_policy="compare",
        accepted_candidate_requirement=2,
        maximum_candidates=3,
    )

    executions, accounting = CandidateScheduler(
        configuration, journal_path=tmp_path / "schedule.jsonl"
    ).execute(
        _plans(configuration),
        generate=lambda plan, _cancel: plan.candidate_id,
        verify=lambda plan, _result: CandidateObservation(
            candidate_id=plan.candidate_id,
            acceptance_eligible=True,
            verifier_confidence=0.99,
            evidence_grade="strong",
        ),
        remaining_attempt_budget=3,
    )

    eligible = [
        item
        for item in executions
        if item.observation and item.observation.acceptance_eligible
    ]
    assert len(eligible) == 2
    assert accounting.started_attempts == 2
    assert accounting.avoided_attempts == 1


def test_identical_effective_configurations_never_claim_diversity() -> None:
    dimensions = CandidateDimensions(backend_name="same", model="same")
    configuration = _configuration(
        maximum_candidates=2,
        maximum_parallelism=2,
        candidates=(dimensions, dimensions),
    )
    claimed, distinct = diversity_summary(_plans(configuration))
    assert claimed is False
    assert distinct == 1


@pytest.mark.parametrize(
    ("strategy", "maximum_candidates", "expected_started"),
    [("single_attempt", 1, 1), ("sequential_escalation", 2, 2)],
)
def test_single_and_sequential_strategies_are_strictly_serial(
    tmp_path: Path,
    strategy: str,
    maximum_candidates: int,
    expected_started: int,
) -> None:
    configuration = ReliabilityStrategyConfiguration.model_validate(
        {
            "strategy": strategy,
            "stop_policy": "stop_on_sufficient",
            "accepted_candidate_requirement": 1,
            "maximum_candidates": maximum_candidates,
            "maximum_parallelism": 1,
        }
    )
    calls: list[int] = []

    def verify(plan: Any, _result: Any) -> CandidateObservation:
        return CandidateObservation(
            candidate_id=plan.candidate_id,
            acceptance_eligible=(
                strategy == "single_attempt" or plan.ordinal == maximum_candidates
            ),
            verifier_confidence=0.99,
            evidence_grade=(
                "strong"
                if strategy == "single_attempt" or plan.ordinal == maximum_candidates
                else "none"
            ),
        )

    _, accounting = CandidateScheduler(
        configuration, journal_path=tmp_path / f"{strategy}.jsonl"
    ).execute(
        _plans(configuration),
        generate=lambda plan, _cancel: calls.append(plan.ordinal) or plan.ordinal,
        verify=verify,
        remaining_attempt_budget=maximum_candidates,
    )
    assert calls == list(range(1, expected_started + 1))
    assert accounting.maximum_observed_concurrency == 1


def test_adaptive_stopping_uses_marginal_success_budget_confidence_and_evidence() -> (
    None
):
    configuration = _configuration(
        strategy="adaptive_candidates",
        maximum_candidates=3,
        maximum_parallelism=1,
        minimum_marginal_expected_success=0.10,
        minimum_verifier_confidence=0.90,
        minimum_evidence_grade="strong",
        expected_success_by_ordinal=(0.8, 0.05, 0.01),
        estimated_cost_usd_by_ordinal=(1.0, 2.0, 3.0),
    )
    plans = _plans(configuration)
    decision = adaptive_stop(
        configuration,
        plans,
        (
            CandidateObservation(
                candidate_id=plans[0].candidate_id,
                acceptance_eligible=True,
                verifier_confidence=0.89,
                evidence_grade="strong",
            ),
        ),
        remaining_attempt_budget=2,
        remaining_cost_budget_usd=10.0,
    )
    assert decision.stop is True
    assert decision.reason == "marginal_expected_success_below_threshold"
    assert decision.avoided_attempts == 2
    assert decision.estimated_avoided_spend_usd == 5.0
    assert decision.actual_savings_usd is None


@pytest.mark.parametrize(
    "events",
    [
        ["scheduling_started"],
        ["candidate_started"],
        ["candidate_started", "candidate_completed"],
        ["candidate_started", "cancellation_requested"],
        ["candidate_started", "candidate_verified", "selection_ready"],
    ],
)
def test_recovery_at_every_scheduler_boundary_never_duplicates_candidate(
    tmp_path: Path, events: list[str]
) -> None:
    configuration = _configuration(maximum_candidates=2, maximum_parallelism=1)
    plans = _plans(configuration)
    journal = tmp_path / "schedule.jsonl"
    for event in events:
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "schema_version": "villani.candidate_schedule_event.v1",
                        "event": event,
                        "candidate_id": (
                            plans[0].candidate_id
                            if event not in {"scheduling_started", "selection_ready"}
                            else None
                        ),
                        "ordinal": 1,
                        "payload": {},
                    }
                )
                + "\n"
            )
    calls: list[str] = []
    CandidateScheduler(configuration, journal_path=journal).execute(
        plans,
        generate=lambda plan, _cancel: calls.append(plan.candidate_id) or "result",
        verify=lambda plan, _result: CandidateObservation(
            candidate_id=plan.candidate_id,
            acceptance_eligible=True,
            verifier_confidence=1.0,
            evidence_grade="strong",
        ),
        remaining_attempt_budget=2,
    )
    assert calls.count(plans[0].candidate_id) <= 1
    if "candidate_started" in events:
        assert plans[0].candidate_id not in calls


class _ConcurrentRunner:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()
        self.contexts: list[Any] = []

    def run(self, context: Any):
        self.contexts.append(context)
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            if context.ordinal == 1:
                time.sleep(0.02)
            else:
                time.sleep(0.03)
            return replace(
                attempt(),
                patch=attempt().patch.replace(
                    "+first", f"+candidate-{context.ordinal}"
                ),
            )
        finally:
            with self.lock:
                self.active -= 1


class _AcceptingVerifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def verify(self, context: Any, _result: Any):
        self.calls.append(context.attempt_id)
        return accepted_verification()


def test_controller_parallel_comparison_persists_dimensions_and_selects_only_eligible(
    tmp_path: Path,
) -> None:
    option = backend("parallel")
    runner = _ConcurrentRunner()
    verifier = _AcceptingVerifier()
    controller = ClosedLoopController(
        classifier=FakeClassifier(),
        policy_engine=type(
            "OneDecisionPolicy",
            (),
            {"decide": lambda self, context: policy("attempt", backend_option=option)},
        )(),
        attempt_runner=runner,
        verifier=verifier,
        selector=FakeSelector(selected_attempt_id="attempt_001"),
        materializer=FakeMaterializer(),
        now=FixedNow(),
        id_factory=StableIds(),
    )
    result = controller.run(
        ClosedLoopRunRequest(
            task="one immutable task",
            repository_path=tmp_path / "repository",
            success_criteria="two candidates verify",
            runs_root=tmp_path / "runs",
            max_attempts=3,
            policy_configuration={
                "candidate_reliability": {
                    "strategy": "parallel_diverse_candidates",
                    "stop_policy": "compare",
                    "accepted_candidate_requirement": 2,
                    "maximum_candidates": 3,
                    "maximum_parallelism": 2,
                    "candidates": [
                        {"prompt_strategy_id": "direct"},
                        {"prompt_strategy_id": "plan-first"},
                        {"prompt_strategy_id": "test-first"},
                    ],
                }
            },
        )
    )
    assert result.terminal_state == "COMPLETED"
    assert runner.maximum == 2
    selection = json.loads((result.run_directory / "selection.json").read_text())
    assert selection["eligible_candidate_ids"] == ["attempt_001", "attempt_002"]
    attempts = [
        json.loads(
            (
                result.run_directory / "attempts" / attempt_id / "attempt.json"
            ).read_text()
        )
        for attempt_id in selection["eligible_candidate_ids"]
    ]
    assert len({item["metadata"]["baseline_sha256"] for item in attempts}) == 1
    assert (
        len({item["metadata"]["effective_configuration_sha256"] for item in attempts})
        == 2
    )
    accounting = json.loads(
        (result.run_directory / "reliability_accounting.json").read_text()
    )
    assert accounting["actual_savings_usd"] is None
    assert accounting["maximum_observed_concurrency"] == 2
