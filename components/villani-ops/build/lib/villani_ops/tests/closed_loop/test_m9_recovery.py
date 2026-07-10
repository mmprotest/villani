from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.adapters.git_isolation import repository_identity
from villani_ops.closed_loop.adapters.patch_materializer import PatchMaterializerAdapter
from villani_ops.closed_loop.durable_io import read_jsonl_tolerant
from villani_ops.closed_loop.interfaces import (
    BackendOption,
    ClosedLoopRunRequest,
    Materialization,
    PolicyDecision,
)
from villani_ops.closed_loop.run_store import RunStore
from villani_ops.materialize import apply_patch_safely
from villani_ops.tests.closed_loop.fakes import (
    FakeAttemptRunner,
    FakeClassifier,
    FakeMaterializer,
    FakeMonotonic,
    FakeSelector,
    FakeVerifier,
    FixedNow,
    StableIds,
    accepted_verification,
    attempt,
)
from villani_ops.tests.closed_loop.fakes import PATCH_ONE


class InjectedCrash(BaseException):
    pass


class CrashOnce:
    def __init__(self, boundary: str) -> None:
        self.boundary = boundary
        self.triggered = False

    def __call__(self, boundary: str) -> None:
        if boundary == self.boundary and not self.triggered:
            self.triggered = True
            raise InjectedCrash(boundary)


class RecoveryPolicy:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.option = BackendOption(
            backend_name="fake",
            model="fake-model",
            eligible=True,
            capability_score=50,
            estimated_cost_usd=1.0,
            cost_accounting_status="complete",
        )

    def decide(self, context: Any) -> PolicyDecision:
        self.calls.append(context)
        if context.eligible_candidate_ids:
            return PolicyDecision(
                action="select",
                reason="select verified candidate",
                considered_backends=(self.option,),
                policy_version="recovery_fixture_v1",
            )
        if context.budget.remaining_attempts <= 0:
            return PolicyDecision(
                action="exhaust",
                reason="attempt budget exhausted",
                considered_backends=(self.option,),
                policy_version="recovery_fixture_v1",
            )
        action = "retry" if context.attempts else "attempt"
        return PolicyDecision(
            action=action,
            reason="run or retry deterministic fixture backend",
            considered_backends=(self.option,),
            chosen_backend=self.option.backend_name,
            chosen_model=self.option.model,
            policy_version="recovery_fixture_v1",
            repeats_prior_backend=bool(context.attempts),
        )


def _request(tmp_path: Path) -> ClosedLoopRunRequest:
    return ClosedLoopRunRequest(
        task="recover the deterministic fixture",
        repository_path=tmp_path / "repo",
        success_criteria="the deterministic fixture verifies",
        runs_root=tmp_path / "runs",
        max_attempts=2,
        policy_configuration={
            "policy": {"version": "bootstrap_v1", "verifier_retry_limit": 1}
        },
    )


def _dependencies() -> dict[str, Any]:
    return {
        "classifier": FakeClassifier(),
        "policy": RecoveryPolicy(),
        "runner": FakeAttemptRunner([attempt(), attempt()]),
        "verifier": FakeVerifier(
            [accepted_verification(), accepted_verification()]
        ),
        "selector": FakeSelector(),
        "materializer": FakeMaterializer(),
        "now": FixedNow(),
        "monotonic": FakeMonotonic(),
        "ids": StableIds(),
    }


def _controller(
    dependencies: dict[str, Any], injector: CrashOnce | None = None
) -> ClosedLoopController:
    return ClosedLoopController(
        classifier=dependencies["classifier"],
        policy_engine=dependencies["policy"],
        attempt_runner=dependencies["runner"],
        verifier=dependencies["verifier"],
        selector=dependencies["selector"],
        materializer=dependencies["materializer"],
        now=dependencies["now"],
        monotonic=dependencies["monotonic"],
        id_factory=dependencies["ids"],
        failure_injector=injector,
    )


def _bundle_hashes(run_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(run_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize(
    "boundary",
    [
        "after_run_creation",
        "after_classification_start",
        "after_classification_snapshot",
        "after_policy_decision",
        "after_attempt_start",
        "after_runner_return",
        "after_attempt_snapshot",
        "after_verification_start",
        "after_verification_snapshot",
        "after_selection_snapshot",
    ],
)
def test_resume_is_idempotent_at_committed_boundaries(
    tmp_path: Path, boundary: str
) -> None:
    dependencies = _dependencies()
    crashing = _controller(dependencies, CrashOnce(boundary))
    with pytest.raises(InjectedCrash):
        crashing.run(_request(tmp_path))

    resumed = _controller(dependencies).resume("run_test_001", tmp_path / "runs")
    assert resumed.terminal_state == "COMPLETED"
    run_dir = resumed.run_directory
    events = read_jsonl_tolerant(run_dir / "events.jsonl")
    assert any(event["event_type"].startswith("recovery_") for event in events)

    runner_attempt_ids = [call.attempt_id for call in dependencies["runner"].calls]
    assert len(runner_attempt_ids) == len(set(runner_attempt_ids))
    assert len(dependencies["verifier"].calls) <= 1
    assert len(dependencies["selector"].calls) == 1
    assert len(dependencies["materializer"].calls) == 1

    before = _bundle_hashes(run_dir)
    call_counts = (
        len(dependencies["classifier"].calls),
        len(dependencies["policy"].calls),
        len(dependencies["runner"].calls),
        len(dependencies["verifier"].calls),
        len(dependencies["selector"].calls),
        len(dependencies["materializer"].calls),
    )
    second = _controller(dependencies).resume("run_test_001", tmp_path / "runs")
    assert second.terminal_state == "COMPLETED"
    assert _bundle_hashes(run_dir) == before
    assert call_counts == (
        len(dependencies["classifier"].calls),
        len(dependencies["policy"].calls),
        len(dependencies["runner"].calls),
        len(dependencies["verifier"].calls),
        len(dependencies["selector"].calls),
        len(dependencies["materializer"].calls),
    )


def test_resume_repairs_one_truncated_final_event_line(tmp_path: Path) -> None:
    dependencies = _dependencies()
    with pytest.raises(InjectedCrash):
        _controller(dependencies, CrashOnce("after_run_creation")).run(
            _request(tmp_path)
        )
    events_path = tmp_path / "runs" / "run_test_001" / "events.jsonl"
    with events_path.open("ab") as handle:
        handle.write(b'{"schema_version":"villani.event.v1"')

    result = _controller(dependencies).resume("run_test_001", tmp_path / "runs")
    assert result.terminal_state == "COMPLETED"
    events = read_jsonl_tolerant(events_path)
    repairs = [
        event for event in events if event["event_type"] == "recovery_truncated_jsonl_repaired"
    ]
    assert len(repairs) == 1
    assert repairs[0]["payload"]["evidence"]["files"] == ["events.jsonl"]


def test_terminal_resume_invokes_no_dependency_and_mutates_no_bundle(tmp_path: Path) -> None:
    dependencies = _dependencies()
    completed = _controller(dependencies).run(_request(tmp_path))
    assert completed.terminal_state == "COMPLETED"
    before = _bundle_hashes(completed.run_directory)
    calls = {
        name: len(dependencies[name].calls)
        for name in ("classifier", "policy", "runner", "verifier", "selector", "materializer")
    }
    result = _controller(dependencies).resume("run_test_001", tmp_path / "runs")
    assert result.terminal_state == "COMPLETED"
    assert _bundle_hashes(completed.run_directory) == before
    assert calls == {
        name: len(dependencies[name].calls)
        for name in ("classifier", "policy", "runner", "verifier", "selector", "materializer")
    }


def test_recovery_lock_rejects_a_concurrent_resume(tmp_path: Path) -> None:
    first = RunStore(tmp_path / "runs", "run_locked")
    second = RunStore(tmp_path / "runs", "run_locked")
    first.runs_root.mkdir(parents=True)
    with first.recovery_lock():
        with pytest.raises(RuntimeError, match="already held"):
            with second.recovery_lock():
                pass


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True
    )
    assert completed.returncode == 0, completed.stderr


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    (repo / "example.txt").write_text("old\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "m9@example.invalid")
    _git(repo, "config", "user.name", "M9 Recovery")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _materialization_dependencies(repo: Path) -> dict[str, Any]:
    dependencies = _dependencies()
    base_attempt = attempt(patch=PATCH_ONE)
    dependencies["runner"] = FakeAttemptRunner(
        [
            replace(
                base_attempt,
                metadata={
                    "worktree": {
                        "source_repository": repository_identity(repo),
                        "isolated": True,
                    }
                },
            )
        ]
    )
    return dependencies


def _materialization_request(tmp_path: Path, repo: Path) -> ClosedLoopRunRequest:
    request = _request(tmp_path)
    return replace(request, repository_path=repo, max_attempts=1)


def test_resume_materialization_before_apply_runs_safe_apply_once(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    dependencies = _materialization_dependencies(repo)
    applies: list[tuple[Path, Path]] = []

    def counted_apply(target: Path, patch: Path) -> dict[str, Any]:
        applies.append((target, patch))
        return apply_patch_safely(target, patch)

    dependencies["materializer"] = PatchMaterializerAdapter(
        apply_service=counted_apply
    )
    with pytest.raises(InjectedCrash):
        _controller(dependencies, CrashOnce("after_materialization_start")).run(
            _materialization_request(tmp_path, repo)
        )
    assert applies == []
    assert (repo / "example.txt").read_text(encoding="utf-8") == "old\n"

    result = _controller(dependencies).resume("run_test_001", tmp_path / "runs")
    assert result.terminal_state == "COMPLETED"
    assert len(applies) == 1
    assert (repo / "example.txt").read_text(encoding="utf-8") == "first\n"
    assert len(dependencies["runner"].calls) == 1
    assert len(dependencies["verifier"].calls) == 1
    assert len(dependencies["selector"].calls) == 1


class CrashAfterApplyMaterializer:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.apply_count = 0

    def materialize(self, selection: Any, context: Any) -> Materialization:
        self.calls.append((selection, context))
        patch = context.run_directory / context.selected_candidate.attempt.patch_path
        applied = apply_patch_safely(Path(context.repository_path), patch)
        self.apply_count += 1
        assert applied["exit_code"] == 0
        raise InjectedCrash("after_patch_apply_before_snapshot")


def test_resume_after_patch_apply_never_applies_selected_patch_twice(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    dependencies = _materialization_dependencies(repo)
    crashing_materializer = CrashAfterApplyMaterializer()
    dependencies["materializer"] = crashing_materializer
    with pytest.raises(InjectedCrash):
        _controller(dependencies).run(_materialization_request(tmp_path, repo))
    assert crashing_materializer.apply_count == 1
    assert (repo / "example.txt").read_text(encoding="utf-8") == "first\n"

    # Recovery proves reverse-apply succeeds and finalizes without invoking a
    # materializer or a second normal apply.
    replacement_materializer = FakeMaterializer()
    dependencies["materializer"] = replacement_materializer
    result = _controller(dependencies).resume("run_test_001", tmp_path / "runs")
    assert result.terminal_state == "COMPLETED"
    assert crashing_materializer.apply_count == 1
    assert replacement_materializer.calls == []
    assert (repo / "example.txt").read_text(encoding="utf-8") == "first\n"
    materialization = json.loads(
        (result.run_directory / "materialization.json").read_text(encoding="utf-8")
    )
    assert materialization["metadata"]["recovered_already_applied"] is True


def test_resume_after_atomic_snapshot_replace_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from villani_ops.closed_loop import durable_io

    dependencies = _dependencies()
    original = durable_io.os.replace
    injected = False

    def crash_replace(source: Any, destination: Any) -> None:
        nonlocal injected
        if Path(destination).name == "classification.json" and not injected:
            injected = True
            raise InjectedCrash("atomic_snapshot_replace")
        original(source, destination)

    monkeypatch.setattr(durable_io.os, "replace", crash_replace)
    with pytest.raises(InjectedCrash):
        _controller(dependencies).run(_request(tmp_path))
    monkeypatch.setattr(durable_io.os, "replace", original)
    result = _controller(dependencies).resume("run_test_001", tmp_path / "runs")
    assert result.terminal_state == "COMPLETED"
    assert len(dependencies["runner"].calls) == 1


def test_resume_after_jsonl_append_exception_with_complete_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from villani_ops.closed_loop import run_store

    dependencies = _dependencies()
    original = run_store.append_jsonl_durable
    injected = False

    def append_then_crash(path: Any, value: Any) -> None:
        nonlocal injected
        original(path, value)
        if value.get("event_type") == "classification_started" and not injected:
            injected = True
            raise InjectedCrash("jsonl_append")

    monkeypatch.setattr(run_store, "append_jsonl_durable", append_then_crash)
    with pytest.raises(InjectedCrash):
        _controller(dependencies).run(_request(tmp_path))
    monkeypatch.setattr(run_store, "append_jsonl_durable", original)
    result = _controller(dependencies).resume("run_test_001", tmp_path / "runs")
    assert result.terminal_state == "COMPLETED"
    events = read_jsonl_tolerant(result.run_directory / "events.jsonl")
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert len(dependencies["runner"].calls) == 1
