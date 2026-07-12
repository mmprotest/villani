from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.costs import actual_attempt_cost, estimate_attempt_cost
from villani_ops.closed_loop.durable_io import read_jsonl_tolerant
from villani_ops.closed_loop.failure_classification import classify_failure
from villani_ops.closed_loop.interfaces import (
    AttemptSummary,
    BudgetContext,
    Classification,
    ClosedLoopRunRequest,
    PolicyContext,
    VerificationSummary,
)
from villani_ops.closed_loop.policy import BootstrapPolicyEngine
from villani_ops.closed_loop.protocol import ClassificationSnapshot
from villani_ops.core.backend import Backend
from villani_ops.storage.files import FileStorage
from villani_ops.tests.closed_loop.fakes import (
    FakeAttemptRunner,
    FakeClassifier,
    FakeMaterializer,
    FakeSelector,
    FakeVerifier,
    StableIds,
    accepted_verification,
    attempt,
)


NOW = datetime(2026, 7, 10, tzinfo=timezone.utc)


def _backend(
    name: str,
    capability: int,
    *,
    fixed: float | None = None,
    billing_mode: str = "fixed",
) -> Backend:
    return Backend(
        name=name,
        provider="local",
        model=f"{name}-model",
        roles=["coding"],
        capability_score=capability,
        billing_mode=billing_mode,
        fixed_cost_per_attempt=fixed,
    )


def _classification(
    difficulty: str = "easy",
    risk: str = "low",
    confidence: float = 0.90,
) -> ClassificationSnapshot:
    return ClassificationSnapshot(
        schema_version="villani.classification.v1",
        classification_id="classification_001",
        run_id="run_001",
        task_id="task_001",
        classified_at=NOW,
        difficulty=difficulty,
        risk=risk,
        category="test",
        required_capabilities=[],
        estimated_attempts_needed=1,
        needs_tests=True,
        confidence=confidence,
        reasoning_summary="table",
        signals={},
        metadata={},
    )


def _budget(
    *,
    attempts: int = 3,
    cost: float | None = None,
    cost_status: str = "not_applicable",
    wall_ms: int | None = None,
    used: int = 0,
) -> BudgetContext:
    return BudgetContext(
        remaining_attempts=attempts,
        remaining_cost_usd=cost,
        cost_accounting_status=cost_status,  # type: ignore[arg-type]
        remaining_wall_time_ms=wall_ms,
        duration_accounting_status=(
            "complete" if wall_ms is not None else "not_applicable"
        ),
        actual_attempts_used=used,
        actual_cost_consumed_usd=0.0 if used else None,
        actual_cost_accounting_status="complete" if used else "unknown",
        actual_wall_time_ms=0,
    )


def _context(
    classification: ClassificationSnapshot,
    *,
    budget: BudgetContext | None = None,
    attempts: tuple[AttemptSummary, ...] = (),
    verifications: tuple[VerificationSummary, ...] = (),
) -> PolicyContext:
    return PolicyContext(
        run_id="run_001",
        trace_id="trace_001",
        state="CLASSIFIED" if not attempts else "REJECTED",
        classification=classification,
        attempts=attempts,
        verifications=verifications,
        eligible_candidate_ids=(),
        budget=budget or _budget(),
        policy_configuration={"version": "bootstrap_v1"},
    )


@pytest.mark.parametrize(
    ("difficulty", "risk", "confidence", "expected", "required"),
    [
        ("easy", "low", 0.90, "cheap", 20),
        ("medium", "low", 0.90, "medium", 50),
        ("hard", "low", 0.90, "hard", 80),
        ("easy", "high", 0.90, "hard", 80),
        ("easy", "low", 0.50, "hard", 80),
    ],
)
def test_bootstrap_threshold_routing_table(
    difficulty: str,
    risk: str,
    confidence: float,
    expected: str,
    required: int,
) -> None:
    engine = BootstrapPolicyEngine(
        {
            "cheap": _backend("cheap", 25, fixed=0.10),
            "medium": _backend("medium", 55, fixed=0.20),
            "hard": _backend("hard", 85, fixed=1.00),
        }
    )

    decision = engine.decide(_context(_classification(difficulty, risk, confidence)))

    assert decision.chosen_backend == expected
    assert decision.required_capability_score == required
    if difficulty == "medium":
        cheap = next(
            x for x in decision.considered_backends if x.backend_name == "cheap"
        )
        assert cheap.eligible is False


def test_unknown_local_cost_is_not_sorted_as_zero() -> None:
    engine = BootstrapPolicyEngine(
        {
            "unknown": _backend("unknown", 25, billing_mode="unknown"),
            "known": _backend("known", 25, fixed=0.50),
        }
    )
    decision = engine.decide(_context(_classification()))
    assert decision.chosen_backend == "known"
    unknown = next(
        x for x in decision.considered_backends if x.backend_name == "unknown"
    )
    assert unknown.estimated_cost_usd is None
    assert unknown.cost_accounting_status == "unknown"


def test_cost_cap_excludes_unknown_estimate() -> None:
    engine = BootstrapPolicyEngine(
        {
            "unknown": _backend("unknown", 25, billing_mode="unknown"),
            "known": _backend("known", 25, fixed=0.50),
        }
    )
    decision = engine.decide(
        _context(_classification(), budget=_budget(cost=1.0, cost_status="complete"))
    )
    unknown = next(
        x for x in decision.considered_backends if x.backend_name == "unknown"
    )
    assert unknown.eligible is False
    assert "unknown under an active cost cap" in " ".join(unknown.rejection_reasons)


def test_no_capable_backend_exhausts_without_constraint_violations() -> None:
    decision = BootstrapPolicyEngine(
        {"small": _backend("small", 40, fixed=0.1)}
    ).decide(_context(_classification("hard")))
    assert decision.action == "exhaust"
    assert decision.chosen_backend is None


def test_no_capable_backend_uses_strongest_with_explicit_violation() -> None:
    engine = BootstrapPolicyEngine(
        {
            "small": _backend("small", 40, fixed=0.1),
            "strongest": _backend("strongest", 70, fixed=1.0),
        },
        {"allow_constraint_violations": True},
    )
    decision = engine.decide(_context(_classification("hard")))
    assert decision.action == "attempt"
    assert decision.chosen_backend == "strongest"
    assert decision.metadata["constraint_violation"] is True
    chosen = next(
        x for x in decision.considered_backends if x.backend_name == "strongest"
    )
    assert any("constraint violated" in reason for reason in chosen.rejection_reasons)


def _failed_attempt(
    failure: str,
    *,
    attempt_id: str = "attempt_001",
    backend: str = "low",
    progress: bool = True,
) -> AttemptSummary:
    return AttemptSummary(
        attempt_id=attempt_id,
        backend_name=backend,
        exit_code=1,
        status="failed",
        cost_usd=0.1,
        cost_accounting_status="complete",
        failure_category=failure,
        material_progress=progress,
    )


def test_infrastructure_failure_retries_once_without_capability_change() -> None:
    engine = BootstrapPolicyEngine({"low": _backend("low", 25, fixed=0.1)})
    first = _failed_attempt("infrastructure_failure")
    retry = engine.decide(_context(_classification(), attempts=(first,)))
    assert retry.action == "retry"
    assert retry.chosen_backend == "low"
    assert retry.repeats_prior_backend is True
    second = _failed_attempt("infrastructure_failure", attempt_id="attempt_002")
    terminal = engine.decide(_context(_classification(), attempts=(first, second)))
    assert terminal.action == "fail"


def test_capability_failure_escalates_immediately() -> None:
    engine = BootstrapPolicyEngine(
        {"low": _backend("low", 25, fixed=0.1), "high": _backend("high", 80, fixed=1)}
    )
    decision = engine.decide(
        _context(_classification(), attempts=(_failed_attempt("capability_failure"),))
    )
    assert decision.action == "escalate"
    assert decision.chosen_backend == "high"


def test_implementation_failure_retries_once_then_escalates() -> None:
    engine = BootstrapPolicyEngine(
        {"low": _backend("low", 25, fixed=0.1), "high": _backend("high", 80, fixed=1)}
    )
    first = _failed_attempt("implementation_failure")
    assert (
        engine.decide(_context(_classification(), attempts=(first,))).action == "retry"
    )
    second = _failed_attempt("implementation_failure", attempt_id="attempt_002")
    escalated = engine.decide(_context(_classification(), attempts=(first, second)))
    assert escalated.action == "escalate"
    assert escalated.chosen_backend == "high"


def test_verifier_failure_retries_verifier_not_coding_attempt() -> None:
    engine = BootstrapPolicyEngine({"low": _backend("low", 25, fixed=0.1)})
    failed = _failed_attempt("verification_failure")
    verification = VerificationSummary(
        attempt_id="attempt_001",
        outcome="error",
        acceptance_eligible=False,
        recommended_action="retry_verifier",
        failure_category="verification_failure",
        verifier_retry_count=0,
    )
    decision = engine.decide(
        _context(_classification(), attempts=(failed,), verifications=(verification,))
    )
    assert decision.action == "retry"
    assert decision.metadata["retry_scope"] == "verification"
    assert decision.budget_projection_after.remaining_attempts == 3


@pytest.mark.parametrize(
    ("budget", "reason_fragment"),
    [
        (_budget(attempts=0), "Attempt budget"),
        (_budget(wall_ms=0), "Wall-time budget"),
    ],
)
def test_attempt_and_wall_time_budgets_stop_loop(
    budget: BudgetContext, reason_fragment: str
) -> None:
    decision = BootstrapPolicyEngine({"low": _backend("low", 25, fixed=0.1)}).decide(
        _context(_classification(), budget=budget)
    )
    assert decision.action == "exhaust"
    assert reason_fragment in decision.reason


def test_cost_budget_stops_before_unaffordable_attempt() -> None:
    decision = BootstrapPolicyEngine({"high": _backend("high", 80, fixed=2.0)}).decide(
        _context(_classification(), budget=_budget(cost=1.0, cost_status="complete"))
    )
    assert decision.action == "exhaust"
    option = decision.considered_backends[0]
    assert any(
        "exceeds remaining cost budget" in reason for reason in option.rejection_reasons
    )


def test_actual_api_token_cost_formula() -> None:
    backend = Backend(
        name="api",
        provider="openai",
        model="m",
        billing_mode="token",
        input_cost_per_million=2,
        output_cost_per_million=4,
    )
    cost = actual_attempt_cost(
        backend, input_tokens=1_000_000, output_tokens=500_000, duration_seconds=5
    )
    assert cost.input_token_cost == 2
    assert cost.output_token_cost == 2
    assert cost.total == 4
    assert cost.accounting_status == "complete"


def test_actual_local_compute_time_cost_formula() -> None:
    backend = Backend(
        name="local",
        provider="local",
        model="m",
        billing_mode="compute_time",
        compute_cost_per_hour=3.6,
    )
    cost = actual_attempt_cost(
        backend, input_tokens=None, output_tokens=None, duration_seconds=1000
    )
    assert cost.compute_time_cost == pytest.approx(1.0)
    assert cost.total == pytest.approx(1.0)


def test_hybrid_cost_sums_each_component_once() -> None:
    backend = Backend(
        name="hybrid",
        provider="local",
        model="m",
        billing_mode="hybrid",
        input_cost_per_million=1,
        output_cost_per_million=2,
        compute_cost_per_hour=3,
        fixed_cost_per_attempt=4,
    )
    cost = actual_attempt_cost(
        backend,
        input_tokens=1_000_000,
        output_tokens=500_000,
        duration_seconds=3600,
    )
    assert cost.total == 9
    assert cost.fixed_cost == 4
    assert cost.accounting_status == "complete"


def test_missing_telemetry_is_partial_or_unknown_never_zero() -> None:
    backend = Backend(
        name="api",
        provider="openai",
        model="m",
        billing_mode="token",
        input_cost_per_million=2,
        output_cost_per_million=4,
    )
    partial = actual_attempt_cost(
        backend, input_tokens=1_000, output_tokens=None, duration_seconds=None
    )
    unknown = actual_attempt_cost(
        backend, input_tokens=None, output_tokens=None, duration_seconds=None
    )
    assert partial.accounting_status == "partial"
    assert partial.total == pytest.approx(0.002)
    assert unknown.accounting_status == "unknown"
    assert unknown.total is None


def test_estimated_token_cost_uses_only_configured_estimates() -> None:
    backend = Backend(
        name="api",
        provider="openai",
        model="m",
        billing_mode="token",
        input_cost_per_million=2,
        output_cost_per_million=4,
        estimated_input_tokens=10_000,
        estimated_output_tokens=2_000,
    )
    assert estimate_attempt_cost(backend).total == pytest.approx(0.028)


def test_legacy_backend_billing_inference_and_validation() -> None:
    priced = Backend.model_validate(
        {
            "name": "old",
            "provider": "local",
            "model": "m",
            "input_cost_per_million": 1,
        }
    )
    zero = Backend.model_validate(
        {
            "name": "zero",
            "provider": "local",
            "model": "m",
            "input_cost_per_million": 0,
            "output_cost_per_million": 0,
        }
    )
    assert priced.billing_mode == "token"
    assert zero.billing_mode == "unknown"
    assert estimate_attempt_cost(zero).total is None
    explicit_zero = Backend(
        name="free",
        provider="local",
        model="m",
        billing_mode="token",
        input_cost_per_million=0,
        output_cost_per_million=0,
    )
    evaluated_zero = actual_attempt_cost(
        explicit_zero,
        input_tokens=10,
        output_tokens=5,
        duration_seconds=None,
    )
    assert evaluated_zero.total == 0
    assert evaluated_zero.accounting_status == "complete"
    with pytest.raises(ValidationError):
        Backend(name="bad", provider="local", model="m", compute_cost_per_hour=-1)


def test_legacy_backend_yaml_still_loads(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path / "workspace")
    storage.init_workspace()
    (storage.workspace / "backends.yaml").write_text(
        "backends:\n"
        "  - name: old-api\n"
        "    provider: openai\n"
        "    model: legacy\n"
        "    roles: [coding]\n"
        "    input_cost_per_million: 1.5\n",
        encoding="utf-8",
    )
    loaded = storage.load_backends()["old-api"]
    assert loaded.billing_mode == "token"
    assert loaded.capability_score_source == "user_configured"


def test_nonzero_exit_is_not_automatically_capability_failure() -> None:
    result = attempt(exit_code=1)
    assert (
        classify_failure(result, requires_file_changes=True) == "implementation_failure"
    )


class _OrderingClassifier(FakeClassifier):
    def classify(self, task: str, context: object) -> Classification:
        assert getattr(context, "classification_backend_name") == "classifier"
        assert getattr(context, "classification_backend_name") != "coder"
        return super().classify(task, context)


def test_controller_persists_classification_before_coding_routing_and_retries_verifier(
    tmp_path: Path,
) -> None:
    configuration = {
        "policy": {"version": "bootstrap_v1", "verifier_retry_limit": 1},
        "backends": {
            "classifier": {
                "provider": "local",
                "base_url": "http://127.0.0.1:8000/v1",
                "model": "classify-model",
                "roles": ["classification"],
                "capability_score": 90,
            },
            "coder": {
                "provider": "local",
                "base_url": "http://127.0.0.1:8000/v1",
                "model": "code-model",
                "roles": ["coding"],
                "capability_score": 25,
                "billing_mode": "fixed",
                "fixed_cost_per_attempt": 0.25,
            },
        },
    }
    classifier = _OrderingClassifier()
    runner = FakeAttemptRunner([attempt()])
    verifier = FakeVerifier(
        [RuntimeError("verifier endpoint timeout"), accepted_verification()]
    )
    controller = ClosedLoopController(
        classifier=classifier,
        attempt_runner=runner,
        verifier=verifier,
        selector=FakeSelector(),
        materializer=FakeMaterializer(),
        id_factory=StableIds(),
    )
    request = ClosedLoopRunRequest(
        task="change it",
        repository_path=tmp_path / "repo",
        success_criteria="tests pass",
        runs_root=tmp_path / "runs",
        max_attempts=3,
        policy_configuration=configuration,
    )

    result = controller.run(request)

    assert result.terminal_state == "COMPLETED"
    assert len(runner.calls) == 1
    assert len(verifier.calls) == 2
    events = read_jsonl_tolerant(result.run_directory / "events.jsonl")
    classification_sequence = next(
        row["sequence"]
        for row in events
        if row["event_type"] == "classification_completed"
    )
    policy_sequence = next(
        row["sequence"]
        for row in events
        if row["event_type"] == "policy_decision_started"
    )
    assert (result.run_directory / "classification.json").is_file()
    assert classification_sequence < policy_sequence
    decisions = read_jsonl_tolerant(result.run_directory / "policy_decisions.jsonl")
    assert decisions
    assert all(row["policy_version"] == "bootstrap_v1" for row in decisions)
    assert decisions[0]["considered_backends"][0]["backend_name"] == "coder"
    assert "alternative_costs" in decisions[0]["metadata"]
    verification = json.loads(
        (result.run_directory / "verification" / "attempt_001.json").read_text()
    )
    assert verification["metadata"]["verifier_retry_count"] == 1
    assert verification["metadata"]["coding_attempt_rerun_for_verification"] is False
