from datetime import datetime, timedelta, timezone

import pytest

from villani_ops.closed_loop.adapters.villani_verifier import _repository_validation_authority
from villani_ops.closed_loop.interfaces import AttemptContext, AttemptResult, RuntimeEvent


BASELINE = "a" * 64
NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def context(tmp_path):
    worktree = tmp_path / "run/worktree"
    worktree.mkdir(parents=True)
    return AttemptContext(
        run_id="run_1", trace_id="trace_1", task_id="task_1", attempt_id="attempt_001",
        ordinal=1, task="fix", repository_path=str(tmp_path / "repo"),
        success_criteria="passes", requires_file_changes=True, backend_name="fixture",
        model="fixture", policy_configuration={}, run_directory=tmp_path / "run",
        attempt_directory=tmp_path / "run/attempts/attempt_001",
        baseline_sha256=BASELINE,
    )


def result(ctx, *events):
    return AttemptResult(
        runner_name="fixture", status="completed", worktree_path=str(ctx.run_directory / "worktree"),
        patch="diff --git a/a b/a", exit_code=0, runtime_events=tuple(events),
    )


def command(ctx, *, role="repository_validation", exit_code=0, when=NOW, attempt_id=None, worktree=None, state="post_mutation"):
    return RuntimeEvent(
        "command_completed" if exit_code == 0 else "command_failed", when,
        {"command_role": role, "exit_code": exit_code, "run_id": ctx.run_id,
         "attempt_id": attempt_id or ctx.attempt_id, "worktree_path": worktree or str(ctx.run_directory / "worktree"),
         "baseline_sha256": BASELINE, "candidate_state": state},
    )


@pytest.mark.parametrize("role", ["inspection", "discovery", "unknown", "candidate_authored_validation", "materialization_check"])
def test_successful_non_authoritative_roles_never_authorize(tmp_path, role):
    ctx = context(tmp_path)
    assert _repository_validation_authority(ctx, result(ctx, command(ctx, role=role))) == (False, False)


def test_explicit_repository_validation_passes_and_later_failure_blocks(tmp_path):
    ctx = context(tmp_path)
    assert _repository_validation_authority(ctx, result(ctx, command(ctx))) == (True, False)
    assert _repository_validation_authority(
        ctx, result(ctx, command(ctx), command(ctx, exit_code=1, when=NOW + timedelta(seconds=1)))
    ) == (False, True)


def test_validation_must_match_attempt_worktree_baseline_and_final_mutation(tmp_path):
    ctx = context(tmp_path)
    mutation = RuntimeEvent("file_write", NOW + timedelta(seconds=1), {"path": "a"})
    assert _repository_validation_authority(ctx, result(ctx, command(ctx), mutation)) == (False, False)
    assert _repository_validation_authority(ctx, result(ctx, command(ctx, attempt_id="attempt_002"))) == (False, False)
    assert _repository_validation_authority(ctx, result(ctx, command(ctx, worktree=str(tmp_path / "other")))) == (False, False)
    bad_baseline = command(ctx)
    bad_baseline = RuntimeEvent(bad_baseline.event_type, bad_baseline.timestamp, {**bad_baseline.payload, "baseline_sha256": "b" * 64})
    assert _repository_validation_authority(ctx, result(ctx, bad_baseline)) == (False, False)
