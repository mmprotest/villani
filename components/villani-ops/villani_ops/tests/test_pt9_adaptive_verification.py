from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.adaptive_verification import (
    AdaptiveVerificationPlan,
    BinaryVerificationDecision,
    CompactReviewPackage,
    GateDArm,
    HumanOutcome,
    build_adaptive_verification_plan,
    build_supervision_metrics,
    evaluate_gate_d,
    make_human_outcome,
    policy_from_configuration,
)
from villani_ops.closed_loop.adapters.villani_verifier import (
    VillaniVerifierAdapter,
    _RepositoryValidationAuthority,
)
from villani_ops.closed_loop.focused_probes import execute_focused_probes
from villani_ops.closed_loop.interfaces import AttemptContext, AttemptResult
from villani_ops.closed_loop.qualification import QualificationStore
from villani_ops.closed_loop.verification_evidence import (
    FocusedProbeRequest,
    FocusedProbeTemporaryFile,
    RequirementDefinition,
)
from villani_ops.execution_environment.models import (
    CandidateCommandResult,
    ExecutionEnvironmentConfig,
    PreparedEnvironment,
)


ROOT = Path(__file__).resolve().parents[4]
VALID = ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"
DOCS = ROOT / "docs"
NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _plan(
    *,
    risk: str = "low",
    task: str = "Apply a bounded change.",
    changed_files: list[str] | None = None,
    configuration: dict[str, object] | None = None,
    qualification: str = "qualified",
    history: list[str] | None = None,
) -> AdaptiveVerificationPlan:
    return build_adaptive_verification_plan(
        run_id="run_001",
        attempt_id="attempt_001",
        task=task,
        success_criteria="The required behavior must be proved.",
        classification={"risk": risk, "difficulty": "easy"},
        changed_files=changed_files or ["src/change.txt"],
        candidate_patch="+one line\n",
        requirement_ids=["req_001"],
        policy_configuration=configuration or {},
        qualification_state=qualification,
        historical_failure_modes=history or [],
        created_at=NOW,
    )


def test_risk_tiers_use_generic_signals_scope_and_qualification() -> None:
    standard = _plan()
    elevated = _plan(risk="medium")
    critical = _plan(task="Perform an irreversible data migration safely.")

    assert standard.risk_tier == "standard"
    assert elevated.risk_tier == "elevated"
    assert critical.risk_tier == "critical"
    assert critical.independent_verifier_required is True
    assert (
        next(
            node
            for node in critical.nodes
            if node.kind == "independent_second_verifier"
        ).disposition
        == "required"
    )
    assert _plan(qualification="provisional").risk_tier == "elevated"
    assert _plan(history=["false_acceptance"]).risk_tier == "critical"


def test_sensitive_paths_are_configured_and_not_filename_heuristics() -> None:
    ordinary = _plan(changed_files=["security.py"])
    configured = _plan(
        changed_files=["controls/policy.txt"],
        configuration={"adaptive_verification": {"sensitive_paths": ["controls/*"]}},
    )

    assert ordinary.risk_tier == "standard"
    assert configured.risk_tier == "critical"
    assert "configured_sensitive_path_changed" in configured.risk_reasons


def test_plan_is_deterministic_and_semantic_context_is_blind() -> None:
    assert _plan().plan_id == _plan().plan_id
    plan = _plan()
    assert {"harness_identity", "route", "cost", "competing_candidates"} <= set(
        plan.semantic_context_excluded
    )
    assert not {
        "harness_identity",
        "route",
        "cost",
        "competing_candidates",
    }.intersection(plan.semantic_context_allowlist)

    context = AttemptContext(
        run_id="run_001",
        trace_id="trace_001",
        task_id="task_001",
        attempt_id="attempt_001",
        ordinal=1,
        task="Apply a bounded change.",
        repository_path="repo",
        success_criteria="Prove it.",
        requires_file_changes=True,
        backend_name="secret-harness",
        model="secret-model",
        policy_configuration={"route": "secret-route", "cost": 99},
        run_directory=Path("run"),
        attempt_directory=Path("run/attempts/attempt_001"),
    )
    result = AttemptResult(
        runner_name="secret-runner",
        status="completed",
        worktree_path="worktree",
        patch="+change\n",
        exit_code=0,
        metadata={
            "changed_files": ["src/change.txt"],
            "harness_identity": "secret-harness",
            "provider_identity": "secret-provider",
            "cost": 99,
        },
    )
    payload = VillaniVerifierAdapter()._verification_context(
        context,
        result,
        [
            RequirementDefinition(
                requirement_id="req_001",
                description="Prove it.",
                critical=True,
                observable=True,
                source="success_criteria",
            )
        ],
        _RepositoryValidationAuthority(passed=True, status="passed"),
    )
    serialized = json.dumps(payload, sort_keys=True)
    assert set(payload) == set(plan.semantic_context_allowlist)
    for hidden in ("secret-harness", "secret-model", "secret-route", "secret-provider"):
        assert hidden not in serialized


def _prepared(worktree: Path) -> PreparedEnvironment:
    return PreparedEnvironment(
        provider="inherit",
        provider_version="fixture",
        repository_path=str(worktree),
        worktree_path=str(worktree),
        environment={},
        removals=[],
        fingerprint="fixture-fingerprint",
        cache_key=None,
        cache_hit=False,
        setup_result=None,
        inspection={},
    )


def _command(
    worktree: Path,
    *,
    stdout: str = "expected",
    status: str = "passed",
    failure_code: str | None = None,
) -> CandidateCommandResult:
    stamp = NOW.isoformat().replace("+00:00", "Z")
    return CandidateCommandResult(
        validation_id="probe_001",
        argv=["fixture-probe"],
        command_role="verifier_probe",
        status=status,  # type: ignore[arg-type]
        exit_code=0 if status in {"passed", "failed"} else None,
        duration_ms=1,
        stdout=stdout,
        stderr="",
        stdout_bytes=len(stdout.encode()),
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        execution_environment_fingerprint="fixture-fingerprint",
        execution_provider="inherit",
        worktree_path=str(worktree.resolve()),
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
        started_at=stamp,
        completed_at=stamp,
        failure_code=failure_code,  # type: ignore[arg-type]
    )


def _probe() -> FocusedProbeRequest:
    return FocusedProbeRequest(
        probe_id="probe_001",
        requirement_ids=["req_001"],
        argv=["fixture-probe"],
        timeout_seconds=10,
        expected_exit_code=0,
        expected_stdout="expected",
        temporary_files=[
            FocusedProbeTemporaryFile(
                path=".villani-probe/input.txt",
                purpose="Provide the minimal behavior input.",
                content="héllo\n",
            )
        ],
        reason="Prove the exact missing behavior without embedding a solution.",
    )


def test_focused_probe_temporary_files_are_isolated_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    provider = SimpleNamespace(config=ExecutionEnvironmentConfig())

    def execute(**_kwargs: object) -> CandidateCommandResult:
        assert (worktree / ".villani-probe/input.txt").read_text(
            encoding="utf-8"
        ) == "héllo\n"
        return _command(worktree)

    monkeypatch.setattr(
        "villani_ops.closed_loop.focused_probes.execute_candidate_command", execute
    )
    report = execute_focused_probes(
        provider=provider,
        prepared_environment=_prepared(worktree),
        requests=[_probe()],
        run_id="run_001",
        attempt_id="attempt_001",
        candidate_id="attempt_001",
        baseline_sha256="a" * 64,
    )

    assert report.status == "passed"
    assert report.results[0].temporary_files[0].removed is True
    assert not (worktree / ".villani-probe/input.txt").exists()
    assert not (worktree / ".villani-probe").exists()
    dumped = report.model_dump(mode="json")
    assert "content" not in dumped["requests"][0]["temporary_files"][0]
    assert "héllo" not in json.dumps(dumped, ensure_ascii=False)


def test_focused_probe_separates_behavior_from_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    provider = SimpleNamespace(config=ExecutionEnvironmentConfig())
    monkeypatch.setattr(
        "villani_ops.closed_loop.focused_probes.execute_candidate_command",
        lambda **_kwargs: _command(worktree, stdout="wrong"),
    )
    behavior = execute_focused_probes(
        provider=provider,
        prepared_environment=_prepared(worktree),
        requests=[_probe()],
        run_id="run_001",
        attempt_id="attempt_001",
        candidate_id="attempt_001",
        baseline_sha256="a" * 64,
    )
    monkeypatch.setattr(
        "villani_ops.closed_loop.focused_probes.execute_candidate_command",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("provider unavailable")),
    )
    infrastructure = execute_focused_probes(
        provider=provider,
        prepared_environment=_prepared(worktree),
        requests=[_probe()],
        run_id="run_001",
        attempt_id="attempt_001",
        candidate_id="attempt_001",
        baseline_sha256="a" * 64,
    )

    assert behavior.status == "failed"
    assert behavior.failure_code == "focused_probe_behavior_failure"
    assert infrastructure.status == "infrastructure_error"
    assert infrastructure.failure_code == "focused_probe_provider_failure"


def test_binary_contract_normalizes_unclear_error_and_missing_semantic_to_zero() -> (
    None
):
    valid = _json(VALID / "binary-verification-decision.json")
    for status in ("unclear", "error", "not_invoked"):
        invalid = {**valid, "semantic_status": status, "decision": 1}
        with pytest.raises(ValidationError):
            BinaryVerificationDecision.model_validate(invalid)
        normalized = {
            **invalid,
            "decision": 0,
            "normalized_from": "error" if status != "unclear" else "unclear",
            "requirements_proved": [],
            "requirements_not_proved": ["req_001"],
            "blockers": ["Semantic proof did not complete."],
        }
        assert BinaryVerificationDecision.model_validate(normalized).decision == 0


def test_critical_binary_decision_requires_independent_verifier() -> None:
    valid = _json(VALID / "binary-verification-decision.json")
    valid.update(
        independent_verifier_required=True,
        independent_verifier_completed=False,
    )
    with pytest.raises(ValidationError, match="independent"):
        BinaryVerificationDecision.model_validate(valid)


def test_compact_review_package_has_complete_ready_proof_and_exact_review_gap() -> None:
    ready = CompactReviewPackage.model_validate(_json(VALID / "review-package.json"))
    assert ready.status == "ready_to_apply"
    assert ready.unresolved_decision is None
    assert ready.why_villani_trusts_it

    needs_review = ready.model_dump(mode="json")
    needs_review.update(status="needs_review", requirements_not_proved=["req_001"])
    with pytest.raises(ValidationError, match="unresolved"):
        CompactReviewPackage.model_validate(needs_review)
    needs_review["unresolved_decision"] = "Decide whether req_001 is acceptable."
    assert CompactReviewPackage.model_validate(needs_review).status == "needs_review"


def test_feedback_is_explicit_local_and_unknown_review_time_stays_unknown() -> None:
    outcome = make_human_outcome(
        run_id="run_001",
        attempt_id="attempt_001",
        outcome="accepted_as_is",
        review_minutes=None,
        full_trace_opened=None,
    )
    metrics = build_supervision_metrics(run_id="run_001", outcomes=[outcome])

    assert outcome.imported_from == "explicit_cli"
    assert outcome.review_time_accounting_status == "unknown"
    assert outcome.review_minutes is None
    assert metrics.review_time_accounting_status == "unknown"
    assert metrics.explicit_review_minutes is None
    assert metrics.full_trace_accounting_status == "unknown"
    assert metrics.application_without_full_trace_count == 0
    with pytest.raises(ValidationError):
        HumanOutcome.model_validate(
            {
                **outcome.model_dump(mode="json"),
                "review_minutes": 0,
                "review_time_accounting_status": "unknown",
            }
        )


def test_gate_d_pass_fail_and_insufficient_evidence() -> None:
    passed = evaluate_gate_d(
        arms=[
            GateDArm.model_validate(item)
            for item in _json(VALID / "gate-d.json")["arms"]
        ],
        generated_at=NOW,
    )
    failed_arms = list(passed.arms)
    failed_arms[-1] = failed_arms[-1].model_copy(update={"false_acceptances": 1})
    failed = evaluate_gate_d(arms=failed_arms, generated_at=NOW)
    insufficient_source = _json(DOCS / "PT9_FROZEN_GATE_D_INPUT.json")
    insufficient = evaluate_gate_d(
        arms=[GateDArm.model_validate(item) for item in insufficient_source["arms"]],
        generated_at=NOW,
    )

    assert passed.status == "PASS"
    assert passed.next_milestone_permitted is True
    assert failed.status == "FAIL"
    assert failed.next_milestone_permitted is False
    assert insufficient.status == "INSUFFICIENT_EVIDENCE"
    assert insufficient.next_milestone_permitted is False


def test_legacy_configuration_gets_conservative_defaults() -> None:
    policy = policy_from_configuration({})
    assert policy.require_semantic_verification is True
    assert policy.require_independent_verifier_for_critical is True
    assert policy.configured_sensitive_paths == []
    assert unified.DEFAULT_CONFIG["adaptive_verification"]["policy_version"] == (
        "adaptive_verification_v1"
    )


def test_cli_inspects_plan_imports_feedback_and_quarantines_false_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    result = CliRunner().invoke(unified.app, ["init"])
    assert result.exit_code == 0, result.output
    run_directory = home / "runs" / "run_protocol_fixture"
    shutil.copytree(VALID, run_directory)

    plan = CliRunner().invoke(
        unified.app,
        ["verification", "plan", "run_protocol_fixture", "--json"],
    )
    assert plan.exit_code == 0, plan.output
    assert json.loads(plan.output)["schema_version"] == (
        "villani.adaptive_verification_plan.v1"
    )

    imported = CliRunner().invoke(
        unified.app,
        [
            "verification",
            "feedback-import",
            "run_protocol_fixture",
            "--outcome",
            "false_acceptance",
            "--review-minutes",
            "3",
            "--did-not-open-full-trace",
            "--json",
        ],
    )
    assert imported.exit_code == 0, imported.output
    payload = json.loads(imported.output)
    assert payload["passive_monitoring"] is False
    assert payload["outcome"]["review_minutes"] == 3
    assert payload["outcome"]["full_trace_opened"] is False
    assert payload["qualification_invalidation"]["reason"] == "false_acceptance"
    assert (run_directory / "human-outcomes.jsonl").is_file()
    assert (run_directory / "supervision-metrics.json").is_file()
    invalidations = QualificationStore(home / "qualification").load_invalidations()
    assert len(invalidations) == 1
    assert invalidations[0].system_id == (
        "asys_80147fac99d0bfffb4605d4a447ad9a0b6d6e947426c95efcf7168cc6ec94dfa"
    )

    ambiguous = CliRunner().invoke(
        unified.app,
        [
            "verification",
            "feedback-import",
            "run_protocol_fixture",
            "--outcome",
            "accepted_as_is",
            "--file",
            str(VALID / "human-outcome.json"),
        ],
    )
    assert ambiguous.exit_code == 2
    assert "exactly one explicit --outcome or --file" in ambiguous.output


def test_cli_gate_d_never_promotes_empty_founder_evidence() -> None:
    runner = CliRunner()
    passed = runner.invoke(
        unified.app,
        ["verification", "gate-d", "--input", str(VALID / "gate-d.json"), "--json"],
    )
    insufficient = runner.invoke(
        unified.app,
        [
            "verification",
            "gate-d",
            "--input",
            str(DOCS / "PT9_FROZEN_GATE_D_INPUT.json"),
            "--json",
        ],
    )

    assert passed.exit_code == 0, passed.output
    assert json.loads(passed.output)["status"] == "PASS"
    assert insufficient.exit_code == 2, insufficient.output
    report = json.loads(insufficient.output)
    assert report["status"] == "INSUFFICIENT_EVIDENCE"
    assert report["next_milestone_permitted"] is False
