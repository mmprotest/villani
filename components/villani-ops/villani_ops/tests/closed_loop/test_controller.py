from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.durable_io import read_jsonl_tolerant
from villani_ops.closed_loop.interfaces import (
    ClosedLoopRunRequest,
    DependencyFailure,
    Materialization,
)
from villani_ops.closed_loop.schema_validation import (
    validate_jsonl_event_stream,
    validate_protocol_document,
)
from villani_ops.closed_loop.state_machine import (
    ALLOWED_TRANSITIONS,
    ClosedLoopStateMachine,
    IllegalTransitionError,
    TerminalStateTransitionError,
)
from villani_ops.tests.closed_loop.fakes import (
    PATCH_ONE,
    PATCH_TWO,
    FakeAttemptRunner,
    FakeClassifier,
    FakeMaterializer,
    FakeMonotonic,
    FakePolicyEngine,
    FakeSelector,
    FakeVerifier,
    FixedNow,
    MutatingFakePolicyEngine,
    StableIds,
    accepted_verification,
    attempt,
    backend,
    policy,
    rejected_verification,
    verifier_error_marked_eligible,
)


@pytest.fixture(autouse=True)
def _forbid_external_processes_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("focused M3 tests must not use processes or network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _request(
    tmp_path: Path,
    *,
    max_attempts: int = 3,
    max_cost: float | None = None,
    max_wall_time: float | None = None,
    task: str = "Implement the deterministic fake change.",
    success_criteria: str = "The deterministic fake test passes.",
) -> ClosedLoopRunRequest:
    return ClosedLoopRunRequest(
        task=task,
        repository_path=tmp_path / "unused-target-repository",
        success_criteria=success_criteria,
        runs_root=tmp_path / "runs",
        max_attempts=max_attempts,
        max_cost=max_cost,
        max_wall_time=max_wall_time,
        policy_configuration={"version": "fake_v1", "collect_candidates": 1},
    )


def _controller(
    decisions: list[Any],
    attempts: list[Any],
    verifications: list[Any],
    *,
    selector: FakeSelector | None = None,
    materializer: FakeMaterializer | None = None,
    monotonic: FakeMonotonic | None = None,
    policy_engine: FakePolicyEngine | None = None,
) -> tuple[ClosedLoopController, dict[str, Any]]:
    classifier = FakeClassifier()
    policy_dependency = policy_engine or FakePolicyEngine(decisions)
    runner = FakeAttemptRunner(attempts)
    verifier = FakeVerifier(verifications)
    selector_dependency = selector or FakeSelector()
    materializer_dependency = materializer or FakeMaterializer()
    dependencies = {
        "classifier": classifier,
        "policy": policy_dependency,
        "runner": runner,
        "verifier": verifier,
        "selector": selector_dependency,
        "materializer": materializer_dependency,
    }
    return (
        ClosedLoopController(
            classifier=classifier,
            policy_engine=policy_dependency,
            attempt_runner=runner,
            verifier=verifier,
            selector=selector_dependency,
            materializer=materializer_dependency,
            now=FixedNow(),
            monotonic=monotonic or FakeMonotonic(),
            id_factory=StableIds(),
        ),
        dependencies,
    )


def _events(run_directory: Path) -> list[dict[str, Any]]:
    return read_jsonl_tolerant(run_directory / "events.jsonl")


def test_first_attempt_accepted_and_materialized(tmp_path: Path) -> None:
    low = backend("low")
    controller, dependencies = _controller(
        [policy("attempt", backend_option=low), policy("select")],
        [attempt(patch=PATCH_ONE)],
        [accepted_verification()],
    )

    result = controller.run(_request(tmp_path))

    assert result.terminal_state == "COMPLETED"
    assert result.selected_attempt_id == "attempt_001"
    assert result.actual_known_cost_usd == 1.0
    assert result.accounting_status == "complete"
    assert (result.run_directory / "final.patch").read_text(
        encoding="utf-8"
    ) == PATCH_ONE
    assert (result.run_directory / "final_report.md").is_file()
    assert len(dependencies["classifier"].calls) == 1
    assert len(dependencies["runner"].calls) == 1
    assert len(dependencies["verifier"].calls) == 1
    assert len(dependencies["materializer"].calls) == 1


def test_rejected_attempt_retries_same_backend_then_accepts(tmp_path: Path) -> None:
    low = backend("low")
    controller, dependencies = _controller(
        [
            policy("attempt", backend_option=low),
            policy("retry", backend_option=low),
            policy("select"),
        ],
        [attempt(patch=PATCH_ONE), attempt(patch=PATCH_TWO)],
        [rejected_verification(), accepted_verification()],
    )

    result = controller.run(_request(tmp_path))

    assert result.terminal_state == "COMPLETED"
    assert result.selected_attempt_id == "attempt_002"
    assert [call.backend_name for call in dependencies["runner"].calls] == [
        "low",
        "low",
    ]
    assert [call.attempt_id for call in dependencies["runner"].calls] == [
        "attempt_001",
        "attempt_002",
    ]
    assert "retry_selected" in {
        event["event_type"] for event in _events(result.run_directory)
    }


def test_capability_rejection_escalates_backend_then_accepts(
    tmp_path: Path,
) -> None:
    low = backend("low", capability=20)
    high = backend("high", capability=90)
    controller, dependencies = _controller(
        [
            policy("attempt", backend_option=low),
            policy("escalate", backend_option=high),
            policy("select"),
        ],
        [attempt(patch=PATCH_ONE), attempt(patch=PATCH_TWO)],
        [rejected_verification(capability=True), accepted_verification()],
    )

    result = controller.run(_request(tmp_path))

    assert result.terminal_state == "COMPLETED"
    assert [call.backend_name for call in dependencies["runner"].calls] == [
        "low",
        "high",
    ]
    transitions = [
        event["payload"].get("to_state") for event in _events(result.run_directory)
    ]
    assert "ESCALATING" in transitions
    assert "escalation_selected" in {
        event["event_type"] for event in _events(result.run_directory)
    }


def test_attempt_budget_exhausts_without_materialization(tmp_path: Path) -> None:
    low = backend("low")
    controller, dependencies = _controller(
        [
            policy("attempt", backend_option=low),
            policy("retry", backend_option=low),
        ],
        [attempt(patch=PATCH_ONE)],
        [rejected_verification()],
    )

    result = controller.run(_request(tmp_path, max_attempts=1))

    assert result.terminal_state == "EXHAUSTED"
    assert result.failure_or_exhaustion_reason == "attempt budget exhausted"
    assert len(dependencies["runner"].calls) == 1
    assert not dependencies["materializer"].calls
    assert not (result.run_directory / "materialization.json").exists()


def test_cost_budget_exhausts_before_unaffordable_attempt(tmp_path: Path) -> None:
    expensive = backend("expensive", estimated_cost=2.0)
    controller, dependencies = _controller(
        [policy("attempt", backend_option=expensive)],
        [attempt(cost=2.0)],
        [],
    )

    result = controller.run(_request(tmp_path, max_cost=1.0))

    assert result.terminal_state == "EXHAUSTED"
    assert result.failure_or_exhaustion_reason == (
        "cost budget exhausted before unaffordable attempt"
    )
    assert result.actual_known_cost_usd is None
    assert result.accounting_status == "unknown"
    assert not dependencies["runner"].calls
    assert not dependencies["materializer"].calls


def test_wall_time_budget_exhausts(tmp_path: Path) -> None:
    low = backend("low")
    controller, dependencies = _controller(
        [policy("attempt", backend_option=low)],
        [attempt()],
        [],
        monotonic=FakeMonotonic([0.0, 0.0, 2.0]),
    )

    result = controller.run(_request(tmp_path, max_wall_time=1.0))

    assert result.terminal_state == "EXHAUSTED"
    assert result.failure_or_exhaustion_reason == "wall-time budget exhausted"
    assert not dependencies["runner"].calls
    assert not dependencies["materializer"].calls


def test_verifier_error_is_never_eligible(tmp_path: Path) -> None:
    low = backend("low")
    controller, dependencies = _controller(
        [policy("attempt", backend_option=low), policy("exhaust")],
        [attempt()],
        [verifier_error_marked_eligible()],
    )

    result = controller.run(_request(tmp_path))

    verification = json.loads(
        (result.run_directory / "verification" / "attempt_001.json").read_text(
            encoding="utf-8"
        )
    )
    assert verification["outcome"] == "error"
    assert verification["acceptance_eligible"] is False
    assert result.terminal_state == "EXHAUSTED"
    assert not dependencies["selector"].calls
    assert not dependencies["materializer"].calls


def test_selector_cannot_choose_ineligible_candidate(tmp_path: Path) -> None:
    low = backend("low")
    selector = FakeSelector(selected_attempt_id="attempt_001")
    controller, dependencies = _controller(
        [
            policy("attempt", backend_option=low),
            policy("retry", backend_option=low),
            policy("select"),
        ],
        [attempt(patch=PATCH_ONE), attempt(patch=PATCH_TWO)],
        [rejected_verification(), accepted_verification()],
        selector=selector,
    )

    result = controller.run(_request(tmp_path))

    assert result.terminal_state == "FAILED"
    assert "not acceptance eligible" in (result.failure_or_exhaustion_reason or "")
    assert [candidate.attempt.attempt_id for candidate in selector.calls[0][0]] == [
        "attempt_002"
    ]
    assert not dependencies["materializer"].calls


def test_materialization_failure_ends_failed(tmp_path: Path) -> None:
    low = backend("low")
    materializer = FakeMaterializer(
        Materialization(
            status="failed",
            final_patch=None,
            final_report="",
            failure=DependencyFailure(
                code="fake_apply_failed",
                message="The deterministic fake apply failed.",
            ),
        )
    )
    controller, dependencies = _controller(
        [policy("attempt", backend_option=low), policy("select")],
        [attempt()],
        [accepted_verification()],
        materializer=materializer,
    )

    result = controller.run(_request(tmp_path))

    assert result.terminal_state == "FAILED"
    assert result.selected_attempt_id == "attempt_001"
    assert len(dependencies["materializer"].calls) == 1
    snapshot = json.loads(
        (result.run_directory / "materialization.json").read_text(encoding="utf-8")
    )
    assert snapshot["status"] == "failed"
    assert not (result.run_directory / "final.patch").exists()


def test_illegal_transition_fails_closed() -> None:
    machine = ClosedLoopStateMachine()

    with pytest.raises(IllegalTransitionError):
        machine.transition("COMPLETED")

    assert machine.state == "CREATED"
    assert ALLOWED_TRANSITIONS["CREATED"] == frozenset({"CLASSIFYING"})


def test_terminal_state_cannot_transition() -> None:
    machine = ClosedLoopStateMachine("COMPLETED")

    with pytest.raises(TerminalStateTransitionError):
        machine.transition("FAILED")

    assert machine.state == "COMPLETED"


def test_event_sequences_are_strictly_monotonic(tmp_path: Path) -> None:
    low = backend("low")
    controller, _ = _controller(
        [policy("attempt", backend_option=low), policy("select")],
        [attempt()],
        [accepted_verification()],
    )
    result = controller.run(_request(tmp_path))

    events = validate_jsonl_event_stream(result.run_directory / "events.jsonl")
    sequences = [event.sequence for event in events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert len(sequences) == len(set(sequences))


def test_task_and_success_criteria_are_preserved_verbatim(tmp_path: Path) -> None:
    task = "  Keep leading whitespace.\nUnicode: café 日本語\n\nKeep trailing.  "
    criteria = "Line one.\r\nLine two with  two spaces.\n"
    controller, _ = _controller(
        [policy("exhaust", reason="No attempt requested by the fake policy.")],
        [],
        [],
    )

    result = controller.run(_request(tmp_path, task=task, success_criteria=criteria))

    snapshot = json.loads(
        (result.run_directory / "task.json").read_text(encoding="utf-8")
    )
    assert snapshot["instruction"] == task
    assert snapshot["success_criteria"] == criteria


def test_dependency_cannot_mutate_controller_state(tmp_path: Path) -> None:
    low = backend("low")
    mutating_policy = MutatingFakePolicyEngine(
        [policy("attempt", backend_option=low), policy("select")]
    )
    controller, _ = _controller(
        [],
        [attempt()],
        [accepted_verification()],
        policy_engine=mutating_policy,
    )

    result = controller.run(_request(tmp_path))

    assert result.terminal_state == "COMPLETED"
    state = json.loads(
        (result.run_directory / "state.json").read_text(encoding="utf-8")
    )
    assert state["state"] == "COMPLETED"
    assert [call.state for call in mutating_policy.calls] == ["FAILED", "FAILED"]


def test_run_bundle_matches_protocol_schemas(tmp_path: Path) -> None:
    low = backend("low")
    controller, _ = _controller(
        [policy("attempt", backend_option=low), policy("select")],
        [attempt()],
        [accepted_verification()],
    )
    result = controller.run(_request(tmp_path))

    protocol_paths = [
        result.run_directory / "manifest.json",
        result.run_directory / "task.json",
        result.run_directory / "classification.json",
        result.run_directory / "state.json",
        result.run_directory / "attempts" / "attempt_001" / "attempt.json",
        result.run_directory / "verification" / "attempt_001.json",
        result.run_directory / "selection.json",
        result.run_directory / "materialization.json",
    ]
    for path in protocol_paths:
        validate_protocol_document(json.loads(path.read_text(encoding="utf-8")))
    for decision in read_jsonl_tolerant(
        result.run_directory / "policy_decisions.jsonl"
    ):
        validate_protocol_document(decision)
    validate_jsonl_event_stream(result.run_directory / "events.jsonl")

    required_artifacts = {
        "manifest.json",
        "task.json",
        "classification.json",
        "state.json",
        "events.jsonl",
        "policy_decisions.jsonl",
        "candidate_evidence_matrix.json",
        "selection.json",
        "selection_report.md",
        "materialization.json",
        "final.patch",
        "final_report.md",
    }
    assert required_artifacts <= {path.name for path in result.run_directory.iterdir()}
    controller_source = Path(
        __import__("villani_ops.closed_loop.controller", fromlist=["__file__"]).__file__
    ).read_text(encoding="utf-8")
    assert "villani_ops.agentic" not in controller_source
    assert "villani_ops.adaptive" not in controller_source
    assert "verifier_parallel" not in controller_source
    assert "graph" not in controller_source
