from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from villani_ops.cli.unified import app
from villani_ops.evaluation_lab.models import (
    AccountingAmount,
    AgentSystemIdentity,
    DurationAmount,
    EvaluationSuite,
    EvaluationTask,
    EvaluationTrial,
    FileChangeRequirement,
    HumanReview,
    SetupCommand,
    SourceSnapshot,
    TaskProvenance,
    ValidationCommand,
)
from villani_ops.evaluation_lab.reporting import (
    _metrics,
    founder_gate,
    load_trials,
    write_reports,
)
from villani_ops.evaluation_lab.reviews import (
    append_review,
    latest_reviews,
    load_reviews,
)
from villani_ops.evaluation_lab.runner import (
    ArmExecutionResult,
    _backend_from_configuration,
    capture_patch,
    run_setup,
    run_paired_suite,
)
from villani_ops.evaluation_lab.workspace import (
    add_task,
    compact_artifact_path,
    export_portable_suite,
    freeze_suite,
    import_baseline,
    init_suite,
    load_task,
    restore_snapshot,
)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _repository(path: Path, *, secret: bool = False) -> tuple[Path, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "evaluation@example.invalid")
    _git(path, "config", "user.name", "Evaluation fixture")
    (path / "value.txt").write_text("baseline\n", encoding="utf-8")
    (path / "check.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').read_text() == 'changed\\n'\n",
        encoding="utf-8",
    )
    if secret:
        (path / "config.txt").write_text(
            "api" + '_key = "' + "this-is-a-real-looking-secret-value" + '"\n',
            encoding="utf-8",
        )
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "baseline")
    return path, _git(path, "rev-parse", "HEAD")


def _suite(
    tmp_path: Path,
    *,
    task_text: str = "Change the stored value without changing the check.",
    future_context: bool = False,
) -> tuple[Path, Path, EvaluationTask]:
    source, _commit = _repository(tmp_path / "source")
    suite = tmp_path / "suite"
    init_suite(
        suite,
        title="Synthetic mechanics",
        randomization_seed="synthetic-randomization-seed",
        evidence_kind="synthetic_fixture",
        measured_power_watts=50,
        electricity_price_per_kwh=0.30,
        currency="USD",
    )
    snapshot = import_baseline(suite, repository=source)
    future = tmp_path / "future.patch"
    future.write_text("diff --git a/value.txt b/value.txt\n+changed\n", encoding="utf-8")
    task = add_task(
        suite,
        baseline_digest=snapshot.baseline_digest,
        verbatim_task=task_text,
        success_criteria=("value.txt contains changed",),
        validation=(
            ValidationCommand(
                validation_id="check_value",
                argv=[sys.executable, "check.py"],
                visibility="runner_visible",
            ),
        ),
        task_id="task_fixture",
        future_context_files=(future,) if future_context else (),
        category_labels=("maintenance",),
        risk_labels=("low",),
        evidence_kind="synthetic_fixture",
    )
    freeze_suite(suite, disclosure_complete=True)
    return suite, source, load_task(suite, task.task_id)


class FakeExecutor:
    def __init__(self, *, interrupt_once: bool = False) -> None:
        self.calls: list[tuple[str, Path, dict[str, object]]] = []
        self.interrupt_once = interrupt_once

    def execute(
        self,
        *,
        arm: str,
        trial_id: str,
        runner_payload,
        workspace: Path,
        artifact_directory: Path,
    ) -> ArmExecutionResult:
        self.calls.append((arm, workspace, dict(runner_payload)))
        if self.interrupt_once:
            self.interrupt_once = False
            raise KeyboardInterrupt
        assert "task_id" not in runner_payload
        assert "evaluator_only" not in runner_payload
        assert "arm" not in runner_payload
        (workspace / "value.txt").write_text("changed\n", encoding="utf-8")
        patch, _files = capture_patch(workspace)
        (artifact_directory / "candidate.patch").write_bytes(patch)
        cost = 2.0 if arm == "direct" else 1.0
        return ArmExecutionResult(
            run_id=f"{arm}_{trial_id}",
            patch=patch,
            agent_system=AgentSystemIdentity(
                product="Fixture",
                product_version="1",
                harness=f"{arm}_fixture",
                harness_version="1",
                agent="fixture-agent",
                agent_version="1",
                model="fixture-model",
                provider="fixture-provider",
                execution_provider="inherit",
                environment_fingerprint="fixture-environment",
            ),
            execution_cost=AccountingAmount(
                value=cost,
                currency="USD",
                accounting_status="complete",
                source="fixture_measured_cost",
            ),
            duration_ms=100,
            attempts=1 if arm == "direct" else 2,
            escalations=0 if arm == "direct" else 1,
            product_proved_acceptable=True if arm == "villani" else None,
            artifact_references=("execution/candidate.patch",),
        )


def test_snapshot_is_immutable_both_arms_share_it_and_resume_prevents_duplicates(
    tmp_path: Path,
) -> None:
    suite, source, task = _suite(tmp_path)
    (source / "value.txt").write_text("future source mutation\n", encoding="utf-8")
    restored = tmp_path / "restored"
    assert restore_snapshot(suite, task.source_snapshot, restored) == task.immutable_baseline_digest
    assert (restored / "value.txt").read_text(encoding="utf-8") == "baseline\n"

    executor = FakeExecutor()
    first = run_paired_suite(suite, repetitions=1, executor=executor)
    assert first == {"completed": 2, "skipped": 0, "excluded": 0}
    trials = load_trials(suite)
    assert len(trials) == 2
    assert {trial.arm for trial in trials} == {"direct", "villani"}
    assert {trial.baseline_restore_digest for trial in trials} == {
        task.immutable_baseline_digest
    }
    assert all(trial.target_repository_modified is False for trial in trials)
    assert all(trial.proved_acceptable is True for trial in trials)
    assert Counter(arm for arm, _workspace, _payload in executor.calls) == {
        "direct": 1,
        "villani": 1,
    }
    assert (source / "value.txt").read_text(encoding="utf-8") == "future source mutation\n"
    assert not list((suite / "trials").glob("*/isolation"))
    for path in (suite / "trials").glob("*/verification/verification-input.json"):
        verification_input = json.loads(path.read_text(encoding="utf-8"))
        assert not {
            "arm",
            "harness",
            "route",
            "cost",
            "competing_candidates",
            "task_id",
        }.intersection(verification_input)

    plan_before = (suite / "run-plan.json").read_bytes()
    second = run_paired_suite(suite, repetitions=1, executor=executor)
    assert second == {"completed": 0, "skipped": 2, "excluded": 0}
    assert len(executor.calls) == 2
    assert (suite / "run-plan.json").read_bytes() == plan_before
    assert {trial.order_digest for trial in trials} == {
        json.loads(plan_before)["entries"][0]["order_digest"]
    }


def test_interrupted_trial_resumes_under_the_same_identity(tmp_path: Path) -> None:
    suite, _source, _task = _suite(tmp_path)
    executor = FakeExecutor(interrupt_once=True)
    with pytest.raises(KeyboardInterrupt):
        run_paired_suite(suite, executor=executor)
    interrupted = load_trials(suite)
    assert len(interrupted) == 1
    assert interrupted[0].status == "interrupted"
    interrupted_id = interrupted[0].trial_id
    result = run_paired_suite(suite, executor=executor)
    assert result["completed"] == 2
    completed = {trial.trial_id: trial for trial in load_trials(suite)}
    assert completed[interrupted_id].status == "completed"
    assert len(completed) == 2


def test_atomic_suite_lock_prevents_concurrent_duplicate_trials(tmp_path: Path) -> None:
    suite, _source, _task = _suite(tmp_path)
    lock = suite / "evaluation-run.lock"
    lock.write_text("active evaluation fixture", encoding="utf-8")
    with pytest.raises(RuntimeError, match="another evaluation process owns this suite"):
        run_paired_suite(suite, executor=FakeExecutor())
    assert not list((suite / "trials").glob("*/trial.json"))
    lock.unlink()
    assert run_paired_suite(suite, executor=FakeExecutor())["completed"] == 2
    assert not lock.exists()


def test_future_solution_and_hidden_material_never_enter_runner_or_export(
    tmp_path: Path,
) -> None:
    suite, _source, task = _suite(tmp_path, future_context=True)
    payload = json.dumps(task.runner_payload(), sort_keys=True)
    assert "future.patch" not in payload
    assert "diff --git" not in payload
    assert "task_fixture" not in payload
    exported = export_portable_suite(suite, tmp_path / "portable.zip")
    with zipfile.ZipFile(exported) as bundle:
        names = bundle.namelist()
        joined = "\n".join(names)
        assert "evaluator-only" not in joined
        assert "future" not in joined
        assert "task_fixture" not in joined
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["contains_expected_patch"] is False
        nested_name = next(name for name in names if name.endswith("/code.zip"))
        nested = tmp_path / "nested.zip"
        nested.write_bytes(bundle.read(nested_name))
    with zipfile.ZipFile(nested) as code:
        assert "value.txt" in code.namelist()
        assert code.read("value.txt") == b"baseline\n"


def test_secret_exclusion_fails_closed_and_forbidden_secret_files_are_omitted(
    tmp_path: Path,
) -> None:
    source, _commit = _repository(tmp_path / "secret-source", secret=True)
    suite = tmp_path / "secret-suite"
    init_suite(
        suite,
        title="Secret screen",
        randomization_seed="secret-screening-seed",
        evidence_kind="synthetic_fixture",
    )
    with pytest.raises(ValueError, match="possible secret"):
        import_baseline(suite, repository=source)

    clean_source, _ = _repository(tmp_path / "excluded-source")
    (clean_source / ".env").write_text(
        "api" + "_key='" + "excluded-secret-value" + "'\n", encoding="utf-8"
    )
    _git(clean_source, "add", ".env")
    _git(clean_source, "commit", "-qm", "tracked forbidden secret")
    clean_suite = tmp_path / "excluded-suite"
    init_suite(
        clean_suite,
        title="Excluded secret",
        randomization_seed="excluded-secret-seed",
        evidence_kind="synthetic_fixture",
    )
    snapshot = import_baseline(clean_suite, repository=clean_source)
    assert ".env" in snapshot.excluded_paths
    assert ".env" not in snapshot.included_paths
    safe_task = add_task(
        clean_suite,
        baseline_digest=snapshot.baseline_digest,
        verbatim_task="Change the stored value.",
        success_criteria=("The check passes",),
        validation=(
            ValidationCommand(validation_id="check", argv=[sys.executable, "check.py"]),
        ),
    )
    assert ".env" in safe_task.secret_exclusions
    with pytest.raises(ValueError, match="possible secret in task metadata"):
        add_task(
            clean_suite,
            baseline_digest=snapshot.baseline_digest,
            verbatim_task="Use authorization=do-not-store-this-secret-value",
            success_criteria=("The check passes",),
            validation=(
                ValidationCommand(validation_id="check", argv=[sys.executable, "check.py"]),
            ),
        )


def test_secret_candidate_is_excluded_and_removed_from_trial_artifacts(
    tmp_path: Path,
) -> None:
    suite, _source, _task = _suite(tmp_path)

    class SecretExecutor(FakeExecutor):
        def execute(self, **kwargs) -> ArmExecutionResult:
            workspace = kwargs["workspace"]
            artifact_directory = kwargs["artifact_directory"]
            workspace.joinpath("value.txt").write_text(
                "authorization=do-not-store-this-secret-value\n", encoding="utf-8"
            )
            patch, _files = capture_patch(workspace)
            artifact_directory.joinpath("candidate.patch").write_bytes(patch)
            return ArmExecutionResult(
                run_id="secret_candidate",
                patch=patch,
                agent_system=AgentSystemIdentity(
                    product="Fixture",
                    product_version="1",
                    harness="fixture",
                    harness_version="1",
                    agent="fixture",
                    agent_version="1",
                    execution_provider="inherit",
                    environment_fingerprint="fixture",
                ),
                execution_cost=AccountingAmount(
                    value=None,
                    currency=None,
                    accounting_status="unknown",
                    source="fixture",
                ),
                duration_ms=1,
                attempts=1,
                escalations=0,
                product_proved_acceptable=None,
            )

    result = run_paired_suite(suite, executor=SecretExecutor())
    assert result == {"completed": 0, "skipped": 0, "excluded": 2}
    for path in (suite / "trials").glob("*/execution/candidate.patch"):
        assert path.read_bytes() == b""
    assert "do-not-store-this-secret-value" not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in (suite / "trials").glob("*/trial.json")
    )


def test_allowed_setup_cannot_commit_or_add_visible_files(tmp_path: Path) -> None:
    suite, _source, task = _suite(tmp_path)
    workspace = tmp_path / "setup-workspace"
    restore_snapshot(suite, task.source_snapshot, workspace)
    output = tmp_path / "setup.json"
    command = SetupCommand(
        setup_id="visible_file",
        argv=[sys.executable, "-c", "open('visible.txt','w').write('x')"],
    )
    assert run_setup((command,), workspace, output) is False


def test_review_ledger_is_blinded_append_only_and_amendments_preserve_history(
    tmp_path: Path,
) -> None:
    suite, _source, _task = _suite(tmp_path)
    run_paired_suite(suite, executor=FakeExecutor())
    trial = load_trials(suite)[0]
    first = append_review(
        suite,
        trial_id=trial.trial_id,
        reviewer_id="founder",
        outcome="accepted_as_is",
        review_minutes=4,
    )
    before = (suite / "human-reviews.jsonl").read_text(encoding="utf-8")
    second = append_review(
        suite,
        trial_id=trial.trial_id,
        reviewer_id="founder",
        outcome="accepted_after_correction",
        review_minutes=7,
        correction_summary="Adjusted one edge case.",
        severity="low",
        amends_review_id=first.review_id,
    )
    after = (suite / "human-reviews.jsonl").read_text(encoding="utf-8")
    assert after.startswith(before)
    assert len(load_reviews(suite)) == 2
    assert latest_reviews(load_reviews(suite))[trial.trial_id] == second
    assert first.blinded and second.blinded
    assert first.arm_revealed_during_review is False
    with pytest.raises(ValueError, match="possible secret"):
        append_review(
            suite,
            trial_id=trial.trial_id,
            reviewer_id="founder",
            outcome="rejected",
            review_minutes=1,
            correction_summary="authorization=do-not-store-this-secret-value",
            severity="high",
        )


def _task_model(index: int) -> EvaluationTask:
    digest = f"{index + 1:064x}"[-64:]
    repository = "repo_one" if index % 2 == 0 else "repo_two"
    return EvaluationTask(
        task_id=f"task_{index:03d}",
        suite_id="suite_gate",
        task_version=1,
        immutable_baseline_digest=digest,
        source_snapshot=SourceSnapshot(
            repository_identity=repository,
            resolved_commit="1" * 40,
            baseline_digest=digest,
            archive_digest="a" * 64,
            archive_path=f"baselines/{digest}/code.zip",
            included_paths=["value.txt"],
            excluded_paths=[],
            file_count=1,
            restore_verified=True,
        ),
        verbatim_task="Generic real-task fixture",
        success_criteria=["Authoritative check passes"],
        authoritative_validation=[
            ValidationCommand(validation_id="check", argv=["python", "check.py"])
        ],
        file_change_requirement=FileChangeRequirement(),
        provenance=TaskProvenance(
            captured_at="2026-07-17T00:00:00Z",
            captured_by="test",
            source_reference="in_memory_gate_unit_test",
        ),
        confidentiality="internal",
        evidence_kind="real_founder_work",
        evidence_eligible=True,
        frozen=True,
        content_digest="b" * 64,
    )


def _trial_model(
    task: EvaluationTask,
    arm: str,
    *,
    unknown_cost: bool = False,
) -> EvaluationTrial:
    known = AccountingAmount(
        value=2.0 if arm == "direct" else 1.5,
        currency="USD",
        accounting_status="complete",
        source="known_fixture_cost",
    )
    unknown = AccountingAmount(
        value=None,
        currency=None,
        accounting_status="unknown",
        source="unknown_fixture_cost",
    )
    return EvaluationTrial(
        trial_id=f"trial_{task.task_id}_{arm}",
        suite_id="suite_gate",
        suite_digest="c" * 64,
        task_id=task.task_id,
        task_digest="b" * 64,
        arm=arm,
        repetition=1,
        randomized_order=1 if arm == "direct" else 2,
        order_digest="d" * 64,
        status="completed",
        started_at="2026-07-17T00:00:00Z",
        completed_at="2026-07-17T00:01:00Z",
        agent_system=AgentSystemIdentity(
            product="Fixture",
            product_version="1",
            harness=arm,
            harness_version="1",
            agent="fixture",
            agent_version="1",
            model="fixture",
            provider="fixture",
            execution_provider="inherit",
            environment_fingerprint="fixture",
        ),
        run_id=f"run_{task.task_id}_{arm}",
        baseline_digest=task.immutable_baseline_digest,
        baseline_restore_digest=task.immutable_baseline_digest,
        execution_cost=unknown if unknown_cost else known,
        verification_cost=AccountingAmount(
            value=None,
            currency=None,
            accounting_status="not_applicable",
            source="local_commands",
        ),
        local_compute_cost=AccountingAmount(
            value=0.0,
            currency="USD",
            accounting_status="complete",
            source="measured_fixture",
        ),
        total_cost=unknown if unknown_cost else known,
        duration=DurationAmount(
            value_ms=60000,
            accounting_status="complete",
            source="measured_fixture",
        ),
        proved_acceptable=True,
        verification_status="complete",
        target_repository_modified=False,
        attempts=1 if arm == "direct" else 2,
        escalations=0,
        configuration_mode="automatic",
        artifact_references=["trial.json"],
        evidence_eligible=True,
    )


def _review_model(trial: EvaluationTrial, *, accepted: bool = True) -> HumanReview:
    return HumanReview(
        review_id=f"review_{trial.trial_id}",
        trial_id=trial.trial_id,
        created_at="2026-07-17T00:02:00Z",
        reviewer_id="founder",
        blinded=True,
        arm_revealed_during_review=False,
        outcome="accepted_as_is" if accepted else "rejected",
        correction_required=False,
        review_minutes=10 if trial.arm == "direct" else 5,
        severity="none" if accepted else "high",
        false_acceptance=not accepted,
        false_rejection=False,
    )


def test_metric_correctness_and_unknown_accounting() -> None:
    task = _task_model(0)
    direct, villani = _trial_model(task, "direct"), _trial_model(task, "villani")
    reviews = [_review_model(direct), _review_model(villani)]
    reliability, review_time, cost, supervision, false_acceptance = _metrics(
        [direct, villani], reviews
    )
    assert reliability["direct.proved_acceptable_rate"].value == 1
    assert review_time["direct.median_review_minutes"].value == 10
    assert cost["direct.cost_per_proved_acceptable_change"].value == 2
    assert supervision["villani.attempts_per_accepted_change"].value == 2
    assert false_acceptance["villani.false_acceptance_rate"].value == 0

    unknown = _trial_model(task, "direct", unknown_cost=True)
    _reliability, _review, unknown_costs, _supervision, _false = _metrics(
        [unknown], [_review_model(unknown)]
    )
    assert (
        unknown_costs["direct.cost_per_proved_acceptable_change"].accounting_status
        == "unknown"
    )
    assert unknown_costs["direct.cost_per_proved_acceptable_change"].value is None

    second_task = _task_model(1)
    eur_trial = _trial_model(second_task, "direct").model_copy(
        update={
            "total_cost": AccountingAmount(
                value=2,
                currency="EUR",
                accounting_status="complete",
                source="known_fixture_cost",
            )
        }
    )
    _reliability, _review, mixed_costs, _supervision, _false = _metrics(
        [direct, eur_trial], [_review_model(direct), _review_model(eur_trial)]
    )
    assert (
        mixed_costs["direct.total_cost_per_human_accepted_as_is_change"].value
        is None
    )
    assert (
        mixed_costs[
            "direct.total_cost_per_human_accepted_as_is_change"
        ].accounting_status
        == "unknown"
    )


def test_real_infrastructure_exclusions_are_counted() -> None:
    task = _task_model(0)
    completed = _trial_model(task, "direct")
    excluded = EvaluationTrial.model_validate(
        {
            **completed.model_dump(mode="json"),
            "trial_id": "trial_excluded_direct",
            "status": "excluded",
            "proved_acceptable": None,
            "verification_status": "infrastructure_failure",
            "exclusion_reason": "runner unavailable",
        }
    )
    _reliability, _review, _cost, supervision, _false = _metrics(
        [completed, excluded], [_review_model(completed)]
    )
    metric = supervision["direct.infrastructure_exclusion_rate"]
    assert metric.value == 0.5
    assert (metric.numerator, metric.denominator) == (1, 2)


def test_founder_gate_pass_fail_and_insufficient_states(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import villani_ops.evaluation_lab.reporting as reporting

    suite = EvaluationSuite(
        suite_id="suite_gate",
        title="In-memory gate unit test",
        suite_version=1,
        status="frozen",
        created_at="2026-07-17T00:00:00Z",
        frozen_at="2026-07-17T00:00:00Z",
        randomization_seed="gate-state-unit-test-seed",
        evidence_kind="real_founder_work",
        confidentiality="internal",
        disclosure_complete=True,
        content_digest="c" * 64,
    )
    suite_root = tmp_path / "gate"
    suite_root.mkdir()
    (suite_root / "suite.json").write_text(
        suite.model_dump_json(indent=2), encoding="utf-8"
    )
    monkeypatch.setattr(
        reporting,
        "validate_suite",
        lambda _root: {"valid": True, "issues": [], "task_count": 30},
    )
    tasks = [_task_model(index) for index in range(30)]
    trials = [
        _trial_model(task, arm) for task in tasks for arm in ("direct", "villani")
    ]
    reviews = [_review_model(trial) for trial in trials]
    reliability, review_time, cost, _supervision, _false = _metrics(trials, reviews)
    status, checks = founder_gate(
        suite_directory=suite_root,
        tasks=tasks,
        trials=trials,
        reviews=reviews,
        reliability=reliability,
        review_time=review_time,
        cost=cost,
        disclosures_complete=True,
    )
    assert status == "PASS"
    assert {check.status for check in checks} == {"pass"}

    failed_reviews = list(reviews)
    failed_reviews[1] = _review_model(trials[1], accepted=False)
    reliability, review_time, cost, _supervision, _false = _metrics(
        trials, failed_reviews
    )
    status, checks = founder_gate(
        suite_directory=suite_root,
        tasks=tasks,
        trials=trials,
        reviews=failed_reviews,
        reliability=reliability,
        review_time=review_time,
        cost=cost,
        disclosures_complete=True,
    )
    assert status == "FAIL"
    assert next(
        item for item in checks if item.check_id == "zero_known_false_acceptance"
    ).status == "fail"

    short_tasks = tasks[:10]
    short_ids = {task.task_id for task in short_tasks}
    short_trials = [trial for trial in trials if trial.task_id in short_ids]
    short_reviews = [review for review in reviews if any(trial.trial_id == review.trial_id for trial in short_trials)]
    reliability, review_time, cost, _supervision, _false = _metrics(
        short_trials, short_reviews
    )
    status, _checks = founder_gate(
        suite_directory=suite_root,
        tasks=short_tasks,
        trials=short_trials,
        reviews=short_reviews,
        reliability=reliability,
        review_time=review_time,
        cost=cost,
        disclosures_complete=True,
    )
    assert status == "INSUFFICIENT_EVIDENCE"


def test_report_is_redacted_answer_first_and_synthetic_trials_never_count(
    tmp_path: Path,
) -> None:
    task_marker = "Change the value. INTERNAL-TASK-MARKER-DO-NOT-REPORT"
    suite, _source, _task = _suite(tmp_path, task_text=task_marker)
    run_paired_suite(suite, executor=FakeExecutor())
    for trial in load_trials(suite):
        append_review(
            suite,
            trial_id=trial.trial_id,
            reviewer_id="founder",
            outcome="accepted_as_is",
            review_minutes=2,
        )
    report, json_path, markdown_path, html_path = write_reports(suite)
    assert report.founder_gate_status == "INSUFFICIENT_EVIDENCE"
    assert report.raw_counts["synthetic_trials_excluded_from_gate"] == 2
    assert report.small_sample_significance_claimed is False
    for path in (json_path, markdown_path, html_path):
        text = path.read_text(encoding="utf-8")
        assert "INTERNAL-TASK-MARKER-DO-NOT-REPORT" not in text
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# Villani Founder Thesis Lab")
    assert "## Answer first" in markdown
    assert "confusion matrix" in markdown
    assert "## Task classes and failure modes" in markdown
    assert report.calibration["probability_fabricated"] is False
    html_text = html_path.read_text(encoding="utf-8")
    assert "<h2>Answer first</h2>" in html_text
    assert "<a href=" in html_text


def test_external_path_compaction_and_cli_contracts(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    external = tmp_path / "external" / "runs" / "run_1"
    external.mkdir(parents=True)
    assert compact_artifact_path(external, anchor) == external.resolve().as_posix()

    suite, _source, _task = _suite(tmp_path / "cli")
    runner = CliRunner()
    help_result = runner.invoke(app, ["eval", "--help"])
    assert help_result.exit_code == 0
    assert "Founder Thesis Lab" in help_result.output
    validation = runner.invoke(app, ["eval", "validate", str(suite), "--json"])
    assert validation.exit_code == 0, validation.output
    assert json.loads(validation.output)["valid"] is True
    assert json.loads(validation.output)["passive_monitoring"] is False
    assert json.loads(validation.output)["external_harness"] is False
    gate = runner.invoke(app, ["eval", "gate", str(suite), "--json"])
    assert gate.exit_code == 2
    assert json.loads(gate.output)["status"] == "INSUFFICIENT_EVIDENCE"


def test_cli_capture_freeze_and_portable_export_workflow(tmp_path: Path) -> None:
    source, commit = _repository(tmp_path / "cli-source")
    suite = tmp_path / "cli-suite"
    runner = CliRunner()
    initialized = runner.invoke(
        app,
        [
            "eval",
            "init",
            str(suite),
            "--title",
            "CLI mechanics",
            "--randomization-seed",
            "cli-workflow-randomization-seed",
            "--synthetic-fixture",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    imported = runner.invoke(
        app,
        [
            "eval",
            "import-baseline",
            str(suite),
            "--repo",
            str(source),
            "--commit",
            commit,
            "--json",
        ],
    )
    assert imported.exit_code == 0, imported.output
    baseline = json.loads(imported.output)["baseline_digest"]
    captured = runner.invoke(
        app,
        [
            "eval",
            "add-task",
            str(suite),
            "Change the stored value without changing the check.",
            "--baseline",
            baseline,
            "--success-criteria",
            "value.txt contains changed",
            "--validation-command",
            "python check.py",
            "--category",
            "maintenance",
            "--risk",
            "low",
        ],
    )
    assert captured.exit_code == 0, captured.output
    validated = runner.invoke(app, ["eval", "validate", str(suite), "--json"])
    assert validated.exit_code == 0, validated.output
    frozen = runner.invoke(
        app, ["eval", "freeze", str(suite), "--disclosure-complete"]
    )
    assert frozen.exit_code == 0, frozen.output
    portable = tmp_path / "portable-evaluation.zip"
    exported = runner.invoke(
        app,
        ["eval", "export", str(suite), "--output", str(portable)],
    )
    assert exported.exit_code == 0, exported.output
    assert portable.is_file()
    with zipfile.ZipFile(portable) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["contains_actual_allowed_code"] is True
        assert manifest["contains_evaluator_only_material"] is False


def test_direct_arm_selects_the_strongest_configured_coding_system() -> None:
    selected = _backend_from_configuration(
        {
            "backends": {
                "weaker": {
                    "provider": "local",
                    "model": "model-small",
                    "capability_score": 10,
                    "roles": ["coding"],
                },
                "stronger": {
                    "provider": "local",
                    "model": "model-large",
                    "capability_score": 90,
                    "roles": ["coding"],
                },
            }
        }
    )
    assert selected.name == "stronger"
    assert selected.model == "model-large"
