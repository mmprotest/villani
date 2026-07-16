from datetime import datetime, timedelta, timezone

import pytest

from villani_ops.closed_loop.adapters.villani_verifier import (
    _repository_validation_authority,
    _repository_validation_details,
)
from villani_ops.closed_loop.focused_probes import focused_probe_identity_valid
from villani_ops.closed_loop.failure_classification import classify_failure
from villani_ops.closed_loop.interfaces import (
    AttemptContext,
    AttemptResult,
    RuntimeEvent,
    Verification,
)
from villani_ops.closed_loop.verification_evidence import (
    FocusedProbeReport,
    FocusedProbeRequest,
    FocusedProbeResult,
)
from villani_ops.execution_environment.models import (
    CandidateCommandResult,
    RepositoryValidationCommandResult,
    RepositoryValidationReport,
)


BASELINE = "a" * 64
NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def context(tmp_path):
    worktree = tmp_path / "run/worktree"
    worktree.mkdir(parents=True)
    return AttemptContext(
        run_id="run_1",
        trace_id="trace_1",
        task_id="task_1",
        attempt_id="attempt_001",
        ordinal=1,
        task="fix",
        repository_path=str(tmp_path / "repo"),
        success_criteria="passes",
        requires_file_changes=True,
        backend_name="fixture",
        model="fixture",
        policy_configuration={},
        run_directory=tmp_path / "run",
        attempt_directory=tmp_path / "run/attempts/attempt_001",
        baseline_sha256=BASELINE,
    )


def result(ctx, *events):
    return AttemptResult(
        runner_name="fixture",
        status="completed",
        worktree_path=str(ctx.run_directory / "worktree"),
        patch="diff --git a/a b/a",
        exit_code=0,
        runtime_events=tuple(events),
    )


def v2_result(ctx, *, report_fingerprint="fingerprint", command_worktree=None):
    command_result = RepositoryValidationCommandResult(
        validation_id="validation_001",
        argv=["fixture-test"],
        command_role="repository_validation",
        status="passed",
        exit_code=0,
        duration_ms=1,
        stdout="passed",
        stderr="",
        stdout_bytes=6,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        execution_environment_fingerprint=report_fingerprint,
        execution_provider="inherit",
        worktree_path=command_worktree or str(ctx.run_directory / "worktree"),
        baseline_sha256=BASELINE,
        candidate_state="post_mutation",
        started_at=NOW.isoformat(),
        completed_at=NOW.isoformat(),
        failure_code="repository_validation_passed",
    )
    report = RepositoryValidationReport(
        schema_version="villani.repository_validation.v2",
        run_id=ctx.run_id,
        attempt_id=ctx.attempt_id,
        candidate_id=ctx.attempt_id,
        execution_environment_fingerprint=report_fingerprint,
        execution_provider="inherit",
        commands=[command_result],
        status="passed",
        authoritative=True,
        completed_at=NOW.isoformat(),
        failure_code="repository_validation_passed",
    )
    ctx.attempt_directory.mkdir(parents=True, exist_ok=True)
    (ctx.attempt_directory / "repository-validation.json").write_text(
        report.model_dump_json(),
        encoding="utf-8",
    )
    return AttemptResult(
        runner_name="fixture",
        status="completed",
        worktree_path=str(ctx.run_directory / "worktree"),
        patch="diff --git a/a b/a",
        exit_code=0,
        metadata={
            "execution_environment_fingerprint": "fingerprint",
            "execution_provider": "inherit",
        },
    )


def command(
    ctx,
    *,
    role="repository_validation",
    exit_code=0,
    when=NOW,
    attempt_id=None,
    worktree=None,
    state="post_mutation",
):
    return RuntimeEvent(
        "command_completed" if exit_code == 0 else "command_failed",
        when,
        {
            "command_role": role,
            "exit_code": exit_code,
            "run_id": ctx.run_id,
            "attempt_id": attempt_id or ctx.attempt_id,
            "worktree_path": worktree or str(ctx.run_directory / "worktree"),
            "baseline_sha256": BASELINE,
            "candidate_state": state,
        },
    )


@pytest.mark.parametrize(
    "role",
    [
        "inspection",
        "discovery",
        "unknown",
        "candidate_authored_validation",
        "materialization_check",
    ],
)
def test_successful_non_authoritative_roles_never_authorize(tmp_path, role):
    ctx = context(tmp_path)
    assert _repository_validation_authority(
        ctx, result(ctx, command(ctx, role=role))
    ) == (False, False)


def test_explicit_repository_validation_passes_and_later_failure_blocks(tmp_path):
    ctx = context(tmp_path)
    assert _repository_validation_authority(ctx, result(ctx, command(ctx))) == (
        True,
        False,
    )
    assert _repository_validation_authority(
        ctx,
        result(
            ctx,
            command(ctx),
            command(ctx, exit_code=1, when=NOW + timedelta(seconds=1)),
        ),
    ) == (False, True)


def test_validation_must_match_attempt_worktree_baseline_and_final_mutation(tmp_path):
    ctx = context(tmp_path)
    mutation = RuntimeEvent("file_write", NOW + timedelta(seconds=1), {"path": "a"})
    assert _repository_validation_authority(
        ctx, result(ctx, command(ctx), mutation)
    ) == (False, False)
    assert _repository_validation_authority(
        ctx, result(ctx, command(ctx, attempt_id="attempt_002"))
    ) == (False, False)
    assert _repository_validation_authority(
        ctx, result(ctx, command(ctx, worktree=str(tmp_path / "other")))
    ) == (False, False)
    bad_baseline = command(ctx)
    bad_baseline = RuntimeEvent(
        bad_baseline.event_type,
        bad_baseline.timestamp,
        {**bad_baseline.payload, "baseline_sha256": "b" * 64},
    )
    assert _repository_validation_authority(ctx, result(ctx, bad_baseline)) == (
        False,
        False,
    )


def test_v2_report_is_primary_authority_source(tmp_path):
    ctx = context(tmp_path)
    details = _repository_validation_details(ctx, v2_result(ctx))

    assert details.passed is True
    assert details.source == "repository_validation_v2"
    assert details.status == "passed"


@pytest.mark.parametrize(
    ("report_fingerprint", "command_worktree"),
    [
        ("different-fingerprint", None),
        ("fingerprint", "different-worktree"),
    ],
)
def test_v2_report_identity_mismatch_fails_closed(
    tmp_path, report_fingerprint, command_worktree
):
    ctx = context(tmp_path)
    details = _repository_validation_details(
        ctx,
        v2_result(
            ctx,
            report_fingerprint=report_fingerprint,
            command_worktree=command_worktree,
        ),
    )

    assert details.infrastructure_error is True
    assert details.failure_code == "repository_validation_environment_mismatch"


def test_mismatched_external_validation_is_not_candidate_failure(tmp_path):
    ctx = context(tmp_path)
    candidate = v2_result(
        ctx,
        report_fingerprint="different-fingerprint",
    )
    details = _repository_validation_details(ctx, candidate)
    verification = Verification(
        verifier="fixture",
        outcome="error",
        acceptance_eligible=False,
        confidence=None,
        reason="Repository environment mismatch.",
        recommended_action="retry_verifier",
        metadata={
            "repository_validation_status": details.status,
            "repository_validation_failure_code": details.failure_code,
            "retry_scope": "repository_validation",
        },
    )

    assert classify_failure(candidate, verification) == "verification_failure"


def test_legacy_exact_structured_event_reports_legacy_source(tmp_path):
    ctx = context(tmp_path)
    details = _repository_validation_details(ctx, result(ctx, command(ctx)))

    assert details.passed is True
    assert details.source == "legacy_runtime_events"


def focused_report(ctx) -> FocusedProbeReport:
    request = FocusedProbeRequest(
        probe_id="probe-1",
        requirement_ids=["req-1"],
        argv=["fixture-probe"],
        timeout_seconds=30,
        expected_exit_code=0,
        expected_stdout="ok",
        expected_stdout_contains=[],
        expected_stderr_contains=[],
        reason="exact behavior",
    )
    command_result = CandidateCommandResult(
        validation_id="probe-1",
        argv=["fixture-probe"],
        command_role="verifier_probe",
        status="passed",
        exit_code=0,
        duration_ms=1,
        stdout="ok",
        stderr="",
        stdout_bytes=2,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        execution_environment_fingerprint="fingerprint",
        execution_provider="inherit",
        worktree_path=str(ctx.run_directory / "worktree"),
        baseline_sha256=BASELINE,
        candidate_state="post_mutation",
        started_at=NOW.isoformat(),
        completed_at=NOW.isoformat(),
        failure_code="focused_probe_passed",
    )
    return FocusedProbeReport(
        schema_version="villani.focused_probe.v1",
        run_id=ctx.run_id,
        attempt_id=ctx.attempt_id,
        candidate_id=ctx.attempt_id,
        execution_environment_fingerprint="fingerprint",
        execution_provider="inherit",
        worktree_path=str(ctx.run_directory / "worktree"),
        baseline_sha256=BASELINE,
        requests=[request],
        results=[
            FocusedProbeResult(
                probe_id="probe-1",
                requirement_ids=["req-1"],
                request=request,
                command_result=command_result,
                status="passed",
                evidence_id="focused_probe:probe-1",
                effective_timeout_seconds=30,
                reason="passed",
            )
        ],
        status="passed",
        completed_at=NOW.isoformat(),
    )


def test_focused_probe_identity_requires_same_attempt_worktree_and_environment(
    tmp_path,
):
    ctx = context(tmp_path)
    report = focused_report(ctx)
    common = {
        "run_id": ctx.run_id,
        "attempt_id": ctx.attempt_id,
        "baseline_sha256": BASELINE,
        "execution_environment_fingerprint": "fingerprint",
        "execution_provider": "inherit",
        "allowed_worktree_paths": [str(ctx.run_directory / "worktree")],
    }
    assert focused_probe_identity_valid(report, **common) is True
    assert (
        focused_probe_identity_valid(
            report,
            **{**common, "attempt_id": "attempt_002"},
        )
        is False
    )
    assert (
        focused_probe_identity_valid(
            report,
            **{
                **common,
                "execution_environment_fingerprint": "other",
            },
        )
        is False
    )
    assert (
        focused_probe_identity_valid(
            report,
            **{
                **common,
                "allowed_worktree_paths": [str(tmp_path / "other")],
            },
        )
        is False
    )
