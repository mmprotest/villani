from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from villani_ops.closed_loop.adapters.git_isolation import repository_identity
from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.delivery import (
    DeliveryError,
    DeliveryMaterializerAdapter,
    FakeGitProvider,
)
from villani_ops.closed_loop.interfaces import (
    ClosedLoopRunRequest,
    EligibleCandidate,
    MaterializationContext,
    Selection,
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
    StableIds,
    accepted_verification,
    attempt,
    backend,
    policy,
)


def _workflow_configuration(
    mode: str,
    *,
    authenticated_required: bool = False,
    allow_automatic: bool = False,
) -> dict[str, Any]:
    kinds = {
        "suggest": "patch_export",
        "approve": "local_patch_apply",
        "apply": "local_patch_apply",
        "branch": "local_branch",
        "pull-request": "pull_request",
    }
    return {
        "version": "fake_v1",
        "collect_candidates": 1,
        "delivery": {
            "workflow_version": "villani.delivery_workflow.v1",
            "mode": mode,
            "materialization_type": kinds[mode],
            "approval": {
                "timeout_seconds": 86_400,
                "timeout_policy": "reject",
                "authenticated_required": authenticated_required,
            },
            "authority_policy": {
                "policy_version": "test-authority-v1",
                "allow_automatic": allow_automatic,
                "require_acceptance_eligible": True,
                "allowed_risks": ["low"],
            },
        },
    }


def _request(tmp_path: Path, configuration: dict[str, Any]) -> ClosedLoopRunRequest:
    return ClosedLoopRunRequest(
        task="Apply the deterministic accepted patch.",
        repository_path=tmp_path / "target-repository",
        success_criteria="The accepted fake evidence remains authoritative.",
        runs_root=tmp_path / "runs",
        max_attempts=int(configuration.get("max_attempts", 1)),
        policy_configuration=configuration,
    )


def _controller(
    materializer: FakeMaterializer,
    *,
    now: FixedNow | None = None,
) -> ClosedLoopController:
    option = backend("fixture")
    return ClosedLoopController(
        classifier=FakeClassifier(),
        policy_engine=FakePolicyEngine(
            [policy("attempt", backend_option=option), policy("select")]
        ),
        attempt_runner=FakeAttemptRunner([attempt()]),
        verifier=FakeVerifier([accepted_verification()]),
        selector=FakeSelector(),
        materializer=materializer,
        now=now or FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
    )


def _document(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_suggest_preserves_patch_without_repository_mutation(tmp_path: Path) -> None:
    materializer = FakeMaterializer()
    result = _controller(materializer).run(
        _request(tmp_path, _workflow_configuration("suggest"))
    )

    delivery = _document(result.run_directory / "delivery.json")
    assert result.terminal_state == "COMPLETED"
    assert delivery["state"] == "suggested"
    assert delivery["repository_modified"] is False
    assert delivery["target_worktree_modified"] is False
    assert (result.run_directory / "delivery" / "selected.patch").read_text(
        encoding="utf-8"
    ) == PATCH_ONE
    assert len(materializer.calls) == 1


def test_apply_with_approval_persists_then_applies_after_restart(
    tmp_path: Path,
) -> None:
    materializer = FakeMaterializer()
    configuration = _workflow_configuration("approve")
    initial = _controller(materializer).run(_request(tmp_path, configuration))

    assert initial.terminal_state == "AWAITING_APPROVAL"
    assert not materializer.calls
    restarted = _controller(materializer).approval_action(
        initial.run_id,
        tmp_path / "runs",
        action="approve",
        actor="local-reviewer",
        authenticated=False,
        authentication_type="local_cli",
        reason="Evidence and patch reviewed.",
    )

    delivery = _document(initial.run_directory / "delivery.json")
    assert restarted.terminal_state == "COMPLETED"
    assert delivery["state"] == "applied"
    assert delivery["approval"]["status"] == "approved"
    assert delivery["target_worktree_modified"] is True
    assert len(materializer.calls) == 1
    assert list((initial.run_directory / "approval-records").glob("*.json"))
    assert (initial.run_directory / "approval-audit.jsonl").is_file()


def test_approval_rejection_preserves_patch_and_completes_delivery(
    tmp_path: Path,
) -> None:
    materializer = FakeMaterializer()
    initial = _controller(materializer).run(
        _request(tmp_path, _workflow_configuration("approve"))
    )
    rejected = _controller(materializer).approval_action(
        initial.run_id,
        tmp_path / "runs",
        action="reject",
        actor="reviewer",
        authenticated=False,
        authentication_type="local_cli",
        reason="Patch scope is broader than desired.",
    )

    delivery = _document(initial.run_directory / "delivery.json")
    assert rejected.terminal_state == "COMPLETED"
    assert delivery["state"] == "rejected"
    assert delivery["repository_modified"] is False
    assert not materializer.calls
    assert (initial.run_directory / "delivery" / "selected.patch").is_file()


def test_approval_rerun_request_is_audited_without_applying(tmp_path: Path) -> None:
    materializer = FakeMaterializer()
    initial = _controller(materializer).run(
        _request(tmp_path, _workflow_configuration("approve"))
    )

    requested = _controller(materializer).approval_action(
        initial.run_id,
        tmp_path / "runs",
        action="request_rerun",
        actor="reviewer",
        authenticated=False,
        authentication_type="local_cli",
        reason="Try a narrower candidate.",
    )

    delivery = _document(initial.run_directory / "delivery.json")
    assert requested.terminal_state == "COMPLETED"
    assert delivery["state"] == "rerun_requested"
    assert delivery["approval"]["status"] == "rerun_requested"
    assert delivery["repository_modified"] is False
    assert not materializer.calls
    assert '"action":"request_rerun"' in (
        initial.run_directory / "approval-audit.jsonl"
    ).read_text(encoding="utf-8")


def test_approval_timeout_policy_completes_without_repository_mutation(
    tmp_path: Path,
) -> None:
    materializer = FakeMaterializer()
    configuration = _workflow_configuration("approve")
    configuration["delivery"]["approval"]["timeout_seconds"] = 0
    now = FixedNow()
    initial = _controller(materializer, now=now).run(_request(tmp_path, configuration))
    now.current += timedelta(seconds=1)

    resumed = _controller(materializer, now=now).resume(
        initial.run_id, tmp_path / "runs"
    )

    delivery = _document(initial.run_directory / "delivery.json")
    assert resumed.terminal_state == "COMPLETED"
    assert delivery["state"] == "timed_out"
    assert delivery["approval"]["status"] == "timed_out"
    assert delivery["repository_modified"] is False
    assert delivery["target_worktree_modified"] is False
    assert not materializer.calls


def test_approver_can_choose_another_eligible_candidate_when_policy_allows(
    tmp_path: Path,
) -> None:
    materializer = FakeMaterializer()
    configuration = _workflow_configuration("approve")
    configuration["max_attempts"] = 2
    configuration["policy"] = {"accepted_candidates_required": 2}
    configuration["delivery"]["approval"]["allow_candidate_change"] = True
    option = backend("fixture")
    controller = ClosedLoopController(
        classifier=FakeClassifier(),
        policy_engine=FakePolicyEngine(
            [
                policy("attempt", backend_option=option),
                policy("retry", backend_option=option),
                policy("select"),
            ]
        ),
        attempt_runner=FakeAttemptRunner(
            [attempt(patch=PATCH_ONE), attempt(patch=PATCH_TWO)]
        ),
        verifier=FakeVerifier([accepted_verification(), accepted_verification()]),
        selector=FakeSelector(),
        materializer=materializer,
        now=FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
    )
    initial = controller.run(_request(tmp_path, configuration))

    changed = controller.approval_action(
        initial.run_id,
        tmp_path / "runs",
        action="choose_candidate",
        candidate_id="attempt_002",
        actor="reviewer",
        authenticated=False,
        authentication_type="local_cli",
        reason="The second eligible candidate has the preferred scope.",
    )

    delivery = _document(initial.run_directory / "delivery.json")
    selection = _document(initial.run_directory / "selection.json")
    assert changed.terminal_state == "AWAITING_APPROVAL"
    assert delivery["selected_attempt_id"] == "attempt_002"
    assert selection["selected_candidate_ids"] == ["attempt_002"]
    assert (initial.run_directory / "delivery" / "selected.patch").read_text(
        encoding="utf-8"
    ) == PATCH_TWO
    assert list((initial.run_directory / "selection-history").glob("*.json"))

    approved = controller.approval_action(
        initial.run_id,
        tmp_path / "runs",
        action="approve",
        actor="reviewer",
        authenticated=False,
        authentication_type="local_cli",
        reason="Approved the replacement candidate.",
    )
    assert approved.terminal_state == "COMPLETED"
    assert materializer.calls[0][0].selected_attempt_id == "attempt_002"


def test_automatic_application_requires_and_records_authority(tmp_path: Path) -> None:
    materializer = FakeMaterializer()
    result = _controller(materializer).run(
        _request(
            tmp_path,
            _workflow_configuration("apply", allow_automatic=True),
        )
    )

    delivery = _document(result.run_directory / "delivery.json")
    assert result.terminal_state == "COMPLETED"
    assert delivery["state"] == "applied"
    assert delivery["authority"]["permitted"] is True
    assert delivery["authority"]["policy_version"] == "test-authority-v1"


def test_automatic_application_fails_closed_without_authority(tmp_path: Path) -> None:
    materializer = FakeMaterializer()
    result = _controller(materializer).run(
        _request(tmp_path, _workflow_configuration("apply"))
    )

    delivery = _document(result.run_directory / "delivery.json")
    assert result.terminal_state == "FAILED"
    assert delivery["state"] == "failed"
    assert delivery["failure"]["code"] == "delivery_authority_insufficient"
    assert delivery["failure"]["details"]["patch_preserved"] is True
    assert not materializer.calls


def test_restart_while_awaiting_approval_does_not_duplicate_work_or_cost(
    tmp_path: Path,
) -> None:
    materializer = FakeMaterializer()
    initial = _controller(materializer).run(
        _request(tmp_path, _workflow_configuration("approve"))
    )
    resumed = _controller(materializer).resume(initial.run_id, tmp_path / "runs")

    assert resumed.terminal_state == "AWAITING_APPROVAL"
    assert resumed.actual_known_cost_usd == initial.actual_known_cost_usd
    assert not materializer.calls
    assert _document(initial.run_directory / "delivery.json")["state"] == (
        "awaiting_approval"
    )


def test_connected_approval_rejects_unauthenticated_action(tmp_path: Path) -> None:
    materializer = FakeMaterializer()
    initial = _controller(materializer).run(
        _request(
            tmp_path,
            _workflow_configuration("approve", authenticated_required=True),
        )
    )

    with pytest.raises(PermissionError, match="authenticated approval"):
        _controller(materializer).approval_action(
            initial.run_id,
            tmp_path / "runs",
            action="approve",
            actor="anonymous",
            authenticated=False,
            authentication_type="none",
            reason="not authenticated",
        )

    assert _document(initial.run_directory / "state.json")["state"] == (
        "AWAITING_APPROVAL"
    )
    audit = (initial.run_directory / "approval-audit.jsonl").read_text(encoding="utf-8")
    assert '"result":"denied"' in audit
    assert not materializer.calls


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _repository(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Villani Test")
    _git(path, "config", "user.email", "villani-test@example.invalid")
    (path / "example.txt").write_text("old\n", encoding="utf-8")
    _git(path, "add", "example.txt")
    _git(path, "commit", "-m", "baseline")
    return path.resolve()


def _materialization_fixture(
    tmp_path: Path,
    *,
    patch: str = PATCH_ONE,
    materialization_type: str,
    provider: FakeGitProvider | None = None,
    task: str = "Change the example.",
) -> tuple[DeliveryMaterializerAdapter, Selection, MaterializationContext, Path]:
    repo = _repository(tmp_path / "repo")
    run_dir = tmp_path / "runs" / "run_fixture"
    patch_path = run_dir / "attempts" / "attempt_001" / "patch.diff"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(patch, encoding="utf-8")
    digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    baseline = repository_identity(repo)
    attempt_value = SimpleNamespace(
        attempt_id="attempt_001",
        patch_sha256=digest,
        patch_path="attempts/attempt_001/patch.diff",
        metadata={
            "changed_files": ["example.txt"],
            "worktree": {"source_repository": baseline},
        },
    )
    candidate = EligibleCandidate(
        attempt=attempt_value,
        verification=SimpleNamespace(),
        patch=patch,
    )
    configuration = {
        "delivery": {
            "workflow_version": "villani.delivery_workflow.v1",
            "materialization_type": materialization_type,
            "branch": "villani/run-fixture",
            "remote": "origin",
        }
    }
    (run_dir / "task.json").write_text(
        json.dumps({"instruction": task}), encoding="utf-8"
    )
    (run_dir / "delivery.json").write_text(
        json.dumps(
            {
                "review": {
                    "files_changed": ["example.txt"],
                    "validation_evidence": [
                        {"summary": "All repository checks passed."}
                    ],
                    "verifier_authority": "repository_validation",
                    "cost": {
                        "value": 0.12,
                        "currency": "USD",
                        "accounting_status": "complete",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    context = MaterializationContext(
        run_id="run_fixture",
        trace_id="trace_fixture",
        repository_path=str(repo),
        selected_candidate=candidate,
        policy_configuration=configuration,
        run_directory=run_dir,
        risk="low",
    )
    selection = Selection(
        selected_attempt_id="attempt_001",
        strategy="fixture",
        reason="Selected using acceptance-grade evidence.",
    )
    return (
        DeliveryMaterializerAdapter(git_provider=provider),
        selection,
        context,
        repo,
    )


def test_branch_creation_keeps_original_branch_untouched(tmp_path: Path) -> None:
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, materialization_type="local_branch"
    )
    original_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = adapter.materialize(selection, context)

    assert result.status == "succeeded"
    receipt = result.metadata["delivery_receipt"]
    metadata = receipt["metadata"]
    delivery_worktree = Path(metadata["delivery_worktree"])
    assert _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == original_head
    assert (repo / "example.txt").read_text(encoding="utf-8") == "old\n"
    assert (delivery_worktree / "example.txt").read_text(encoding="utf-8") == (
        "first\n"
    )
    assert metadata["commit"] is None


def test_dirty_worktree_fails_before_branch_delivery_and_preserves_patch(
    tmp_path: Path,
) -> None:
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, materialization_type="local_branch"
    )
    (repo / "unrelated.txt").write_text("user work\n", encoding="utf-8")

    result = adapter.materialize(selection, context)

    assert result.status == "failed"
    assert result.failure is not None and result.failure.code == "dirty_repository"
    assert result.failure.details["patch_preserved"] is True
    assert (
        context.run_directory / "attempts" / "attempt_001" / "patch.diff"
    ).is_file()
    assert (repo / "example.txt").read_text(encoding="utf-8") == "old\n"


def test_detached_head_fails_before_branch_delivery_and_preserves_patch(
    tmp_path: Path,
) -> None:
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, materialization_type="local_branch"
    )
    _git(repo, "checkout", "--detach", "HEAD")

    result = adapter.materialize(selection, context)

    assert result.status == "failed"
    assert result.failure is not None and result.failure.code == "detached_head"
    assert result.failure.details["patch_preserved"] is True
    assert (
        context.run_directory / "attempts" / "attempt_001" / "patch.diff"
    ).is_file()
    assert (repo / "example.txt").read_text(encoding="utf-8") == "old\n"


def test_unrelated_existing_delivery_branch_fails_and_preserves_patch(
    tmp_path: Path,
) -> None:
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, materialization_type="local_branch"
    )
    _git(repo, "branch", "villani/run-fixture")

    result = adapter.materialize(selection, context)

    assert result.status == "failed"
    assert result.failure is not None and result.failure.code == "branch_already_exists"
    assert result.failure.details["patch_preserved"] is True
    assert (
        context.run_directory / "attempts" / "attempt_001" / "patch.diff"
    ).is_file()
    assert (repo / "example.txt").read_text(encoding="utf-8") == "old\n"


def test_local_pull_request_fixture_branches_commits_pushes_and_records_body(
    tmp_path: Path,
) -> None:
    provider = FakeGitProvider()
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, materialization_type="pull_request", provider=provider
    )

    result = adapter.materialize(selection, context)

    assert result.status == "succeeded"
    receipt = result.metadata["delivery_receipt"]
    metadata = receipt["metadata"]
    assert metadata["commit"]
    assert metadata["pull_request"]["url"] == "fixture://pull/1"
    assert len(provider.pushes) == len(provider.requests) == 1
    body = (context.run_directory / "delivery" / "pull-request-body.md").read_text(
        encoding="utf-8"
    )
    assert "## Task" in body
    assert "## Validation" in body
    assert "Verifier authority" in body
    assert "generated by an agent" in body
    assert _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def test_push_failure_preserves_committed_local_branch_and_patch(
    tmp_path: Path,
) -> None:
    provider = FakeGitProvider(
        push_error=DeliveryError("push_rejected", "remote rejected the branch")
    )
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, materialization_type="pull_request", provider=provider
    )

    result = adapter.materialize(selection, context)

    assert result.status == "failed"
    assert result.failure is not None and result.failure.code == "push_rejected"
    assert result.failure.details["patch_preserved"] is True
    assert (context.run_directory / "delivery" / "branch-state.json").is_file()
    assert _git(repo, "show-ref", "--verify", "refs/heads/villani/run-fixture")


@pytest.mark.parametrize(
    ("failure_code", "message"),
    [
        ("remote_unavailable", "fixture remote is unavailable"),
        ("authentication_failure", "fixture authentication failed"),
    ],
)
def test_pull_request_transport_failures_preserve_committed_branch_and_patch(
    tmp_path: Path, failure_code: str, message: str
) -> None:
    provider = FakeGitProvider(push_error=DeliveryError(failure_code, message))
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, materialization_type="pull_request", provider=provider
    )

    result = adapter.materialize(selection, context)

    assert result.status == "failed"
    assert result.failure is not None and result.failure.code == failure_code
    assert result.failure.details["patch_preserved"] is True
    assert (
        context.run_directory / "attempts" / "attempt_001" / "patch.diff"
    ).is_file()
    assert _git(repo, "show-ref", "--verify", "refs/heads/villani/run-fixture")


def test_patch_conflict_fails_without_changing_original_repository(
    tmp_path: Path,
) -> None:
    conflict = PATCH_ONE.replace("-old", "-missing")
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, patch=conflict, materialization_type="local_branch"
    )

    result = adapter.materialize(selection, context)

    assert result.status == "failed"
    assert result.failure is not None and result.failure.code == "patch_conflict"
    assert (repo / "example.txt").read_text(encoding="utf-8") == "old\n"
    assert _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def test_target_branch_change_is_detected_before_delivery(tmp_path: Path) -> None:
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, materialization_type="local_branch"
    )
    (repo / "after.txt").write_text("changed target\n", encoding="utf-8")
    _git(repo, "add", "after.txt")
    _git(repo, "commit", "-m", "target moved")

    result = adapter.materialize(selection, context)

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code == "target_branch_changed"
    assert not (context.run_directory / "delivery" / "branch-state.json").exists()


def test_repository_moved_after_execution_preserves_selected_patch(
    tmp_path: Path,
) -> None:
    adapter, selection, context, repo = _materialization_fixture(
        tmp_path, materialization_type="local_branch"
    )
    moved = repo.with_name("repo-moved")
    repo.rename(moved)

    result = adapter.materialize(selection, context)

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code == "repository_moved"
    assert result.failure.details["patch_preserved"] is True
    assert (context.run_directory / "attempts" / "attempt_001" / "patch.diff").is_file()


def test_pull_request_content_redacts_secrets(tmp_path: Path) -> None:
    provider = FakeGitProvider()
    secret = "super-secret-provider-token"
    adapter, selection, context, _repo = _materialization_fixture(
        tmp_path,
        materialization_type="pull_request",
        provider=provider,
        task=f"Change example with api_key={secret}",
    )
    delivery_path = context.run_directory / "delivery.json"
    delivery = _document(delivery_path)
    delivery["review"]["validation_evidence"][0]["summary"] = (
        f"Authorization: Bearer {secret}"
    )
    delivery_path.write_text(json.dumps(delivery), encoding="utf-8")

    result = adapter.materialize(selection, context)

    assert result.status == "succeeded"
    body = (context.run_directory / "delivery" / "pull-request-body.md").read_text(
        encoding="utf-8"
    )
    assert secret not in body
    assert "REDACTED" in body
