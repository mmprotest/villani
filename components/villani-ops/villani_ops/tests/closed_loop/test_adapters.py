from __future__ import annotations

import json
import shutil
import subprocess
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import pytest

from villani_ops.closed_loop.adapters import (
    EvidenceSelectorAdapter,
    PatchMaterializerAdapter,
    VillaniCodeAttemptAdapter,
    VillaniVerifierAdapter,
)
from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.durable_io import read_jsonl_tolerant
from villani_ops.closed_loop.interfaces import (
    ClosedLoopRunRequest,
    EligibleCandidate,
    MaterializationContext,
    Selection,
)
from villani_ops.closed_loop.protocol import AttemptSnapshot, VerificationSnapshot
from villani_ops.core.backend import Backend
from villani_ops.runners.base import RunnerContext, RunnerResult
from villani_ops.tests.closed_loop.fakes import (
    FakeClassifier,
    FakeMonotonic,
    FakePolicyEngine,
    FixedNow,
    StableIds,
    backend,
    policy,
)


def _repository_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "integration" / "fixtures" / "closed_loop_m4").is_dir():
            return parent
    raise AssertionError("repository root not found")


FIXTURE = (
    _repository_root()
    / "integration"
    / "fixtures"
    / "closed_loop_m4"
    / "tiny_repo"
)


@pytest.fixture(autouse=True)
def _forbid_model_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("M4 default tests must not contact a model endpoint")

    monkeypatch.setattr(httpx, "post", forbidden)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )


def _tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    shutil.copytree(FIXTURE, repo)
    _git(repo, "init")
    _git(repo, "config", "user.email", "m4@example.invalid")
    _git(repo, "config", "user.name", "M4 Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "baseline")
    return repo


class InjectedVillaniCodeRunner:
    name = "villani-code"

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = deque(steps)
        self.calls: list[RunnerContext] = []
        self.worktree_observations: list[bool] = []

    def run(self, context: RunnerContext) -> RunnerResult:
        self.calls.append(context)
        self.worktree_observations.append(
            Path(context.repo_path).is_dir()
            and (Path(context.repo_path) / ".git").is_dir()
        )
        step = self.steps.popleft()
        value = step.get("value")
        if value is not None:
            (Path(context.repo_path) / "example.txt").write_text(
                str(value), encoding="utf-8"
            )
        trace_dir: Path | None = None
        if not step.get("missing_trace"):
            trace_dir = Path(context.run_dir) / "villani_code_debug" / "trace"
            trace_dir.mkdir(parents=True)
            timestamp = f"2026-07-10T00:00:{len(self.calls):02d}Z"
            secret = str(step.get("secret") or "")
            quality = step.get("quality", "strong")
            command = (
                "python -m pytest -q tests/test_example.py"
                if quality == "strong"
                else "cat example.txt"
            )
            stdout = "1 passed" if quality == "strong" else "example inspected"
            if secret:
                stdout += f" Authorization: Bearer {secret}"
            (trace_dir / "session_meta.json").write_text(
                json.dumps(
                    {
                        "run_id": context.env["VILLANI_RUN_ID"],
                        "objective": context.task_instruction,
                        "repo": context.repo_path,
                        "model": context.backend.model,
                        "provider": context.backend.provider,
                        "created_at": timestamp,
                    }
                ),
                encoding="utf-8",
            )
            (trace_dir / "commands.jsonl").write_text(
                json.dumps(
                    {
                        "event_id": f"command-{len(self.calls)}",
                        "ts": timestamp,
                        "command": command,
                        "cwd": context.repo_path,
                        "exit_code": 0,
                        "stdout": stdout,
                        "stderr": "",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (trace_dir / "tool_calls.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "tool_call_id": f"tool-command-{len(self.calls)}",
                                "tool_name": "exec_command",
                                "tool_category": "command",
                                "started_at": timestamp,
                                "status": "completed",
                                "args": {"command": command},
                                "result_summary": stdout,
                            }
                        ),
                        json.dumps(
                            {
                                "tool_call_id": f"tool-write-{len(self.calls)}",
                                "tool_name": "Write",
                                "tool_category": "file_mutation",
                                "started_at": timestamp,
                                "status": "completed",
                                "args": {"file_path": "example.txt"},
                                "result_summary": f"wrote example.txt {secret}",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (trace_dir / "model_responses.jsonl").write_text(
                json.dumps(
                    {
                        "event_id": f"model-{len(self.calls)}",
                        "ts": timestamp,
                        "text": f"Completed candidate {secret}",
                        "usage": {"input_tokens": 11, "output_tokens": 5},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (trace_dir / "patches.jsonl").write_text(
                json.dumps(
                    {
                        "event_id": f"patch-{len(self.calls)}",
                        "ts": timestamp,
                        "file_path": "example.txt",
                        "ok": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (trace_dir / "validations.jsonl").write_text("", encoding="utf-8")
            summary = {
                "status": "completed",
                "duration_ms": 25,
                "changed_files": ["example.txt"] if value is not None else [],
                "tokens_input": 11,
                "tokens_output": 5,
            }
            (trace_dir / "summary.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )
            (trace_dir / "final_summary.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )
        secret = str(step.get("secret") or "")
        return RunnerResult(
            exit_code=int(step.get("exit_code", 0)),
            stdout=f"runner stdout {secret}",
            stderr=f"runner stderr {secret}" if secret else "",
            input_tokens=11,
            output_tokens=5,
            total_tokens=16,
            total_cost=0.01,
            debug_artifact_dir=(
                str(trace_dir.parent) if trace_dir is not None else None
            ),
            resolved_trace_dir=str(trace_dir) if trace_dir is not None else None,
            duration_ms=25,
            model_requests=1,
            total_tool_calls=2,
            tool_calls_by_name={"exec_command": 1, "Write": 1},
            total_file_writes=1 if value is not None else 0,
            commands_executed=1,
            token_accounting_status="verified",
            telemetry={"Authorization": f"Bearer {secret}" if secret else None},
        )


def _accepted_raw(**kwargs: Any) -> dict[str, Any]:
    value = (Path(kwargs["repo_dir"]) / "example.txt").read_text(encoding="utf-8")
    strong = "second" in value or "changed" in value
    evidence = (
        "End-to-end runtime validation passed against the changed file."
        if strong
        else "Implementation appears correct from source inspection."
    )
    return {
        "result": 1,
        "verdict": "success",
        "confidence": 0.9,
        "recommendedAction": "accept",
        "reason": "The selected behavior has direct evidence.",
        "criticalRequirementCovered": True,
        "criticalRequirementCoverageProven": True,
        "requirementResults": [
            {
                "id": "criterion",
                "requirement": "The known file is changed correctly.",
                "status": "satisfied",
                "evidence": ["behavioral-evidence"],
                "risks": [],
            }
        ],
        "successEvidence": [evidence],
        "failureEvidence": [],
        "missingEvidence": [],
        "riskFlags": [],
        "traceDir": str(kwargs["trace_dir"]),
    }


def _rejected_raw(**kwargs: Any) -> dict[str, Any]:
    return {
        "result": 0,
        "verdict": "failure",
        "confidence": 0.95,
        "recommendedAction": "reject",
        "reason": "The candidate failed deterministic verification.",
        "criticalRequirementCovered": False,
        "criticalRequirementCoverageProven": False,
        "requirementResults": [
            {
                "id": "criterion",
                "requirement": "The known file is changed correctly.",
                "status": "unsatisfied",
                "evidence": [],
                "risks": ["not demonstrated"],
            }
        ],
        "successEvidence": [],
        "failureEvidence": ["Behavior was not demonstrated."],
        "missingEvidence": ["Direct behavior evidence is missing."],
        "riskFlags": ["acceptance_blocker:verification_failed"],
    }


class SequenceVerifier:
    def __init__(self, results: list[Any]) -> None:
        self.results = deque(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        value = self.results.popleft()
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(**kwargs)
        return value


def _request(
    tmp_path: Path,
    repo: Path,
    *,
    max_attempts: int = 3,
    policy_configuration: dict[str, Any] | None = None,
) -> ClosedLoopRunRequest:
    return ClosedLoopRunRequest(
        task="Change example.txt through the real M4 adapter path.",
        repository_path=repo,
        success_criteria="example.txt contains the selected candidate value.",
        runs_root=tmp_path / "runs",
        max_attempts=max_attempts,
        policy_configuration=policy_configuration or {"version": "m4_test"},
    )


def _controller(
    decisions: list[Any],
    runner: InjectedVillaniCodeRunner,
    raw_verifier: Any,
    *,
    selector: EvidenceSelectorAdapter | None = None,
    materializer: PatchMaterializerAdapter | None = None,
    secret: str = "test-api-key",
) -> ClosedLoopController:
    coding_backend = Backend(
        name="real",
        provider="local",
        model="fake-model",
        api_key=secret,
    )
    return ClosedLoopController(
        classifier=FakeClassifier(),
        policy_engine=FakePolicyEngine(decisions),
        attempt_runner=VillaniCodeAttemptAdapter(
            backends={"real": coding_backend}, runner=runner
        ),
        verifier=VillaniVerifierAdapter(raw_verifier=raw_verifier),
        selector=selector or EvidenceSelectorAdapter(),
        materializer=materializer or PatchMaterializerAdapter(),
        now=FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
    )


def _attempt_policy(action: str = "attempt") -> Any:
    return policy(action, backend_option=backend("real"))


def test_real_adapter_path_isolates_captures_verifies_selects_and_applies(
    tmp_path: Path,
) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner([{"value": "changed\n"}])
    controller = _controller(
        [_attempt_policy(), policy("select")], runner, None
    )

    result = controller.run(_request(tmp_path, repo))

    assert result.terminal_state == "COMPLETED"
    assert result.selected_attempt_id == "attempt_001"
    assert (repo / "example.txt").read_text(encoding="utf-8") == "changed\n"
    attempt_dir = result.run_directory / "attempts" / "attempt_001"
    for name in ("worktree.json", "attempt.json", "patch.diff", "stdout.log", "stderr.log"):
        assert (attempt_dir / name).is_file()
    assert runner.worktree_observations == [True]
    assert not (attempt_dir / "worktree").exists()
    worktree_metadata = json.loads(
        (attempt_dir / "worktree.json").read_text(encoding="utf-8")
    )
    assert worktree_metadata["retained"] is False
    assert worktree_metadata["cleanup_status"] == "removed"
    assert (attempt_dir / "patch.diff").read_text(encoding="utf-8").strip()
    assert runner.calls[0].env["VILLANI_RUN_ID"] == result.run_id
    assert runner.calls[0].env["VILLANI_TRACE_ID"].startswith("trace_")
    assert runner.calls[0].env["VILLANI_ATTEMPT_ID"] == "attempt_001"
    assert Path(runner.calls[0].run_dir) == attempt_dir
    assert (result.run_directory / "verification" / "raw" / "attempt_001.json").is_file()
    assert not (tmp_path / ".villani-ops" / "orchestrations").exists()


def test_two_eligible_candidates_are_ranked_deterministically_by_evidence(
    tmp_path: Path,
) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner(
        [
            {"value": "first\n", "quality": "weak"},
            {"value": "second\n", "quality": "strong"},
        ]
    )
    controller = _controller(
        [_attempt_policy(), _attempt_policy(), policy("select")],
        runner,
        _accepted_raw,
    )

    result = controller.run(_request(tmp_path, repo))

    assert result.terminal_state == "COMPLETED"
    assert result.selected_attempt_id == "attempt_002"
    selection = json.loads(
        (result.run_directory / "selection.json").read_text(encoding="utf-8")
    )
    assert [row["attempt_id"] for row in selection["rankings"]] == [
        "attempt_002",
        "attempt_001",
    ]


def test_ineligible_candidate_with_higher_llm_advisory_score_cannot_win(
    tmp_path: Path,
) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner(
        [{"value": "first\n"}, {"value": "second\n"}]
    )
    raw = SequenceVerifier([_rejected_raw, _accepted_raw])
    selector = EvidenceSelectorAdapter(
        advisory_comparator=lambda candidates: {
            "selectedCandidateId": "attempt_001",
            "score": 999,
            "reason": "Advisory preferred the ineligible candidate.",
        }
    )
    controller = _controller(
        [_attempt_policy(), _attempt_policy("retry"), policy("select")],
        runner,
        raw,
        selector=selector,
    )

    result = controller.run(_request(tmp_path, repo))

    assert result.terminal_state == "COMPLETED"
    assert result.selected_attempt_id == "attempt_002"
    selection = json.loads(
        (result.run_directory / "selection.json").read_text(encoding="utf-8")
    )
    assert selection["advisory_comparison"]["used_for_final_decision"] is False
    assert selection["selected_candidate_ids"] == ["attempt_002"]


def test_missing_villani_code_trace_is_rejected(tmp_path: Path) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner(
        [{"value": "changed\n", "missing_trace": True}]
    )
    controller = _controller(
        [_attempt_policy(), policy("exhaust")], runner, _accepted_raw
    )

    result = controller.run(_request(tmp_path, repo))

    verification = json.loads(
        (result.run_directory / "verification" / "attempt_001.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.terminal_state == "EXHAUSTED"
    assert verification["acceptance_eligible"] is False
    assert "missing_compatible_trace" in verification["reason"]
    assert (repo / "example.txt").read_text(encoding="utf-8") == "original\n"


def test_empty_patch_is_rejected_for_code_change_task(tmp_path: Path) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner([{"value": None}])
    raw = SequenceVerifier([_accepted_raw])
    controller = _controller(
        [_attempt_policy(), policy("exhaust")], runner, raw
    )

    result = controller.run(_request(tmp_path, repo))

    verification = json.loads(
        (result.run_directory / "verification" / "attempt_001.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.terminal_state == "EXHAUSTED"
    assert verification["metadata"]["normalized_without_verifier"] is True
    assert not raw.calls


def test_verifier_malformed_output_is_rejected(tmp_path: Path) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner([{"value": "changed\n"}])
    controller = _controller(
        [_attempt_policy(), policy("exhaust")], runner, lambda **kwargs: "bad"
    )

    result = controller.run(_request(tmp_path, repo))

    verification = json.loads(
        (result.run_directory / "verification" / "attempt_001.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.terminal_state == "EXHAUSTED"
    assert verification["outcome"] == "error"
    assert verification["acceptance_eligible"] is False
    assert verification["metadata"]["invocation_status"] == "malformed_output"


def test_verifier_timeout_is_rejected(tmp_path: Path) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner([{"value": "changed\n"}])
    timeout = subprocess.TimeoutExpired("verifier", 1)
    raw = SequenceVerifier([timeout])
    controller = _controller(
        [_attempt_policy(), policy("exhaust")], runner, raw
    )

    result = controller.run(_request(tmp_path, repo))

    verification = json.loads(
        (result.run_directory / "verification" / "attempt_001.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.terminal_state == "EXHAUSTED"
    assert verification["acceptance_eligible"] is False
    assert verification["metadata"]["invocation_status"] == "timeout"


def test_candidate_exit_127_is_infrastructure_failure_and_not_accepted(
    tmp_path: Path,
) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner(
        [{"value": "changed\n", "exit_code": 127}]
    )
    controller = _controller(
        [_attempt_policy(), policy("exhaust")], runner, _accepted_raw
    )

    result = controller.run(_request(tmp_path, repo))

    attempt_snapshot = json.loads(
        (
            result.run_directory
            / "attempts"
            / "attempt_001"
            / "attempt.json"
        ).read_text(encoding="utf-8")
    )
    verification = json.loads(
        (result.run_directory / "verification" / "attempt_001.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.terminal_state == "EXHAUSTED"
    assert attempt_snapshot["metadata"]["failure_classification"] == "infrastructure_failure"
    assert verification["acceptance_eligible"] is False
    assert "runner_nonzero_exit" in verification["reason"]


def test_patch_path_outside_selected_attempt_directory_is_rejected(
    tmp_path: Path,
) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner([{"value": "changed\n"}])
    controller = _controller(
        [_attempt_policy(), policy("select")], runner, _accepted_raw
    )
    result = controller.run(_request(tmp_path, repo))
    attempt_snapshot = AttemptSnapshot.model_validate_json(
        (
            result.run_directory
            / "attempts"
            / "attempt_001"
            / "attempt.json"
        ).read_text(encoding="utf-8")
    )
    verification = VerificationSnapshot.model_validate_json(
        (result.run_directory / "verification" / "attempt_001.json").read_text(
            encoding="utf-8"
        )
    )
    bad_attempt = attempt_snapshot.model_copy(update={"patch_path": "outside.patch"})
    candidate = EligibleCandidate(
        attempt=bad_attempt,
        verification=verification,
        patch=(result.run_directory / "final.patch").read_text(encoding="utf-8"),
    )
    materializer = PatchMaterializerAdapter()

    materialization = materializer.materialize(
        Selection(
            selected_attempt_id="attempt_001",
            strategy="test",
            reason="test",
        ),
        MaterializationContext(
            run_id=result.run_id,
            trace_id=attempt_snapshot.trace_id,
            repository_path=str(repo),
            selected_candidate=candidate,
            policy_configuration={},
            run_directory=result.run_directory,
        ),
    )

    assert materialization.status == "failed"
    assert "outside the canonical attempt directory" in materialization.failure.message


def test_failed_apply_produces_failed_without_completion_claim(
    tmp_path: Path,
) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner([{"value": "changed\n"}])

    def failed_apply(repo_path: Path, patch_path: Path) -> dict[str, Any]:
        raise RuntimeError("git apply --check failed in injected safe apply")

    controller = _controller(
        [_attempt_policy(), policy("select")],
        runner,
        _accepted_raw,
        materializer=PatchMaterializerAdapter(apply_service=failed_apply),
    )

    result = controller.run(_request(tmp_path, repo))

    assert result.terminal_state == "FAILED"
    assert (repo / "example.txt").read_text(encoding="utf-8") == "original\n"
    materialization = json.loads(
        (result.run_directory / "materialization.json").read_text(encoding="utf-8")
    )
    assert materialization["status"] == "failed"
    assert "run_completed" not in {
        event["event_type"]
        for event in read_jsonl_tolerant(result.run_directory / "events.jsonl")
    }


def test_only_selected_patch_changes_target_repository(tmp_path: Path) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner(
        [
            {"value": "first\n", "quality": "weak"},
            {"value": "second\n", "quality": "strong"},
        ]
    )
    controller = _controller(
        [_attempt_policy(), _attempt_policy(), policy("select")],
        runner,
        _accepted_raw,
    )

    result = controller.run(_request(tmp_path, repo))

    assert result.selected_attempt_id == "attempt_002"
    assert (repo / "example.txt").read_text(encoding="utf-8") == "second\n"
    assert "first" not in (repo / "example.txt").read_text(encoding="utf-8")
    assert (result.run_directory / "attempts" / "attempt_001" / "patch.diff").is_file()
    assert (result.run_directory / "attempts" / "attempt_002" / "patch.diff").is_file()


def test_canonical_events_include_translated_model_tool_and_command_events(
    tmp_path: Path,
) -> None:
    repo = _tiny_repo(tmp_path)
    runner = InjectedVillaniCodeRunner([{"value": "changed\n"}])
    controller = _controller(
        [_attempt_policy(), policy("select")], runner, _accepted_raw
    )

    result = controller.run(_request(tmp_path, repo))

    events = read_jsonl_tolerant(result.run_directory / "events.jsonl")
    event_types = {event["event_type"] for event in events}
    assert {"model_call_completed", "tool_call_completed", "command_completed"} <= event_types
    translated = next(
        event for event in events if event["event_type"] == "model_call_completed"
    )
    assert translated["source"] == "villani_code"
    assert translated["payload"]["source_event_id"].startswith("model-")


def test_runner_configuration_secret_never_appears_under_run_directory(
    tmp_path: Path,
) -> None:
    repo = _tiny_repo(tmp_path)
    secret = "sk-m4-super-secret-value"
    runner = InjectedVillaniCodeRunner([{"value": "changed\n", "secret": secret}])
    controller = _controller(
        [_attempt_policy(), policy("select")],
        runner,
        _accepted_raw,
        secret=secret,
    )
    request = _request(
        tmp_path,
        repo,
        policy_configuration={
            "version": "m4_test",
            "runner_env": {"Authorization": f"Bearer {secret}"},
        },
    )

    result = controller.run(request)

    assert result.terminal_state == "COMPLETED"
    for path in result.run_directory.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


def test_runner_secret_never_appears_after_failed_run(tmp_path: Path) -> None:
    repo = _tiny_repo(tmp_path)
    secret = "opaque-canary-failed-run-71c4"
    runner = InjectedVillaniCodeRunner(
        [{"value": "changed\n", "secret": secret, "exit_code": 127}]
    )
    controller = _controller(
        [_attempt_policy(), policy("exhaust")],
        runner,
        _accepted_raw,
        secret=secret,
    )

    result = controller.run(_request(tmp_path, repo))

    assert result.terminal_state == "EXHAUSTED"
    for path in result.run_directory.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
