from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from villani_ops.evaluation_lab.hardening import (
    FAILURE_TAXONOMY,
    PRIORITIZATION_FORMULA,
    FailureObservation,
    FrozenTaskOutcome,
    VerifierEvidenceObservation,
    _paired_real_population,
    build_founder_proof_certificate,
    build_hardening_analysis,
    build_verifier_diagnostics,
    cluster_failures,
    compare_frozen_outcomes,
    prioritize_failure_clusters,
    scan_production_for_evidence_identifiers,
)
from villani_ops.evaluation_lab.models import (
    AccountingAmount,
    AgentSystemIdentity,
    DurationAmount,
    EvaluationTask,
    EvaluationTrial,
    FileChangeRequirement,
    GateCheck,
    HumanReview,
    SourceSnapshot,
    TaskProvenance,
    ValidationCommand,
)
from villani_ops.evaluation_lab.reporting import _metrics, build_report
from villani_ops.evaluation_lab.workspace import (
    add_task,
    freeze_suite,
    import_baseline,
    init_suite,
)


def _known_cost(value: float = 2.0) -> AccountingAmount:
    return AccountingAmount(
        value=value,
        currency="USD",
        accounting_status="complete",
        source="measured_test_cost",
    )


def _unknown_cost() -> AccountingAmount:
    return AccountingAmount(
        value=None,
        currency=None,
        accounting_status="unknown",
        source="cost_not_reported",
    )


def _failure(
    suffix: str,
    *,
    evidence_kind: str = "real_founder_work",
    review_minutes: float | None = 10,
) -> FailureObservation:
    return FailureObservation(
        observation_id=f"observation_{suffix}",
        taxonomy="validation_failure",
        mechanism="validation_discovery_unavailable",
        task_reference=f"private_task_{suffix}",
        repository_reference=f"private_repository_{suffix}",
        task_classes=["validation_sensitive"],
        agent_system="configured_agent_system",
        cost_impact=_known_cost(),
        review_minutes_impact=review_minutes,
        recoverable_accepted_change_loss=0.5,
        diagnostic_confidence=0.8,
        artifact_references=[f"trials/{suffix}/trial.json"],
        generic_fix_exists=True,
        evidence_kind=evidence_kind,
    )


def _trial_with_duration(duration: DurationAmount) -> EvaluationTrial:
    return EvaluationTrial(
        trial_id="trial_unknown_duration",
        suite_id="suite_duration_regression",
        suite_digest="a" * 64,
        task_id="task_duration_regression",
        task_digest="b" * 64,
        arm="direct",
        repetition=1,
        randomized_order=1,
        order_digest="c" * 64,
        status="completed",
        started_at="2026-07-17T00:00:00Z",
        completed_at="2026-07-17T00:01:00Z",
        agent_system=AgentSystemIdentity(
            product="Generic product",
            product_version="1",
            harness="Generic harness",
            harness_version="1",
            agent="Generic agent",
            agent_version="1",
            execution_provider="inherit",
            environment_fingerprint="generic-fixture",
        ),
        run_id="run_duration_regression",
        baseline_digest="d" * 64,
        baseline_restore_digest="d" * 64,
        execution_cost=_known_cost(),
        verification_cost=_known_cost(0),
        local_compute_cost=_unknown_cost(),
        total_cost=_known_cost(),
        duration=duration,
        proved_acceptable=True,
        verification_status="complete",
        target_repository_modified=False,
        attempts=1,
        escalations=0,
        configuration_mode="automatic",
        artifact_references=["trials/trial_unknown_duration/trial.json"],
        evidence_eligible=True,
    )


def _accepted_review(trial: EvaluationTrial) -> HumanReview:
    return HumanReview(
        review_id="review_unknown_duration",
        trial_id=trial.trial_id,
        created_at="2026-07-17T00:02:00Z",
        reviewer_id="reviewer",
        blinded=True,
        arm_revealed_during_review=False,
        outcome="accepted_as_is",
        correction_required=False,
        review_minutes=3,
        severity="none",
        false_acceptance=False,
        false_rejection=False,
    )


def _frozen_outcome(
    *,
    cost: float,
    review_minutes: float,
    tool_version: str,
) -> FrozenTaskOutcome:
    return FrozenTaskOutcome(
        task_digest="1" * 64,
        baseline_digest="2" * 64,
        arm="villani",
        repetition=1,
        proved_acceptable=True,
        human_outcome="accepted_as_is",
        total_cost=_known_cost(cost),
        review_minutes=review_minutes,
        tool_versions={"product": tool_version},
    )


def _empty_frozen_suite(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "evaluation@example.invalid"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Evaluation fixture"],
        cwd=source,
        check=True,
    )
    (source / "value.txt").write_text("baseline\n", encoding="utf-8")
    (source / "check.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').exists()\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=source, check=True)

    root = tmp_path / "suite"
    init_suite(
        root,
        title="Synthetic PT4 insufficiency fixture",
        randomization_seed="pt4-insufficiency-test-seed",
        evidence_kind="synthetic_fixture",
    )
    snapshot = import_baseline(root, repository=source)
    add_task(
        root,
        baseline_digest=snapshot.baseline_digest,
        verbatim_task="Preserve the generic fixture",
        success_criteria=("The authoritative check passes",),
        validation=(
            ValidationCommand(
                validation_id="check",
                argv=[sys.executable, "check.py"],
                visibility="runner_visible",
            ),
        ),
        task_id="task_pt4_synthetic_fixture",
        evidence_kind="synthetic_fixture",
    )
    freeze_suite(root, disclosure_complete=True)
    return root


def _real_task(repository_identity: str) -> EvaluationTask:
    return EvaluationTask(
        task_id="task_certificate_redaction",
        suite_id="suite_certificate",
        task_version=1,
        immutable_baseline_digest="3" * 64,
        source_snapshot=SourceSnapshot(
            repository_identity=repository_identity,
            resolved_commit="4" * 40,
            baseline_digest="3" * 64,
            archive_digest="5" * 64,
            archive_path="baselines/fixture/code.zip",
            included_paths=["source.txt"],
            excluded_paths=[],
            file_count=1,
            restore_verified=True,
        ),
        verbatim_task="A generic frozen task",
        success_criteria=["The authoritative check passes"],
        authoritative_validation=[
            ValidationCommand(validation_id="check", argv=["tool", "check"])
        ],
        file_change_requirement=FileChangeRequirement(),
        provenance=TaskProvenance(
            captured_at="2026-07-17T00:00:00Z",
            captured_by="test",
            source_reference="generic_certificate_fixture",
        ),
        confidentiality="confidential",
        evidence_kind="real_founder_work",
        evidence_eligible=True,
        frozen=True,
        content_digest="6" * 64,
    )


def test_failure_taxonomy_clustering_and_synthetic_exclusion() -> None:
    expected = (
        "task_misunderstanding",
        "irrelevant_navigation",
        "context_waste",
        "patch_quality",
        "validation_failure",
        "missing_validation",
        "verifier_false_reject",
        "verifier_false_accept",
        "runner_infrastructure",
        "model_capability",
        "environment_mismatch",
        "retry_without_progress",
        "premature_escalation",
        "late_escalation",
        "selection_error",
        "delivery_conflict",
        "unknown_accounting",
        "user_flow_friction",
        "evidence_backed_other",
    )
    assert FAILURE_TAXONOMY == expected

    first = _failure("one", review_minutes=10)
    second = _failure("two", review_minutes=20)
    synthetic = _failure("synthetic", evidence_kind="synthetic_fixture")
    clusters = cluster_failures([first, second, synthetic])

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["count"] == 2
    assert cluster["distinct_task_count"] == 2
    assert cluster["repeated"] is True
    assert cluster["review_impact"]["average_minutes"] == 15
    assert cluster["cost_impact"]["accounting_status"] == "complete"
    assert (
        cluster["acceptance_impact"]["recoverable_accepted_change_loss_average"] == 0.5
    )
    assert cluster["diagnostic_confidence"] == 0.8
    serialized = json.dumps(clusters, sort_keys=True)
    assert first.task_reference not in serialized
    assert first.repository_reference not in serialized
    assert synthetic.observation_id not in serialized


def test_prioritization_uses_required_math_and_refuses_unknown_burden() -> None:
    ranked_cluster = cluster_failures(
        [_failure("one", review_minutes=10), _failure("two", review_minutes=20)]
    )[0]
    unknown_cluster = {
        **ranked_cluster,
        "mechanism": "validation_runtime_not_reported",
        "review_impact": {
            **ranked_cluster["review_impact"],
            "average_minutes": None,
            "accounting_status": "unknown",
        },
    }

    result = prioritize_failure_clusters([unknown_cluster, ranked_cluster])

    assert result["formula"] == PRIORITIZATION_FORMULA
    assert result["status"] == "available"
    assert result["ranked"] == [
        {
            "rank": 1,
            "taxonomy": "validation_failure",
            "mechanism": "validation_discovery_unavailable",
            "score": 12.0,
            "frequency": 2,
            "recoverable_accepted_change_loss": 0.5,
            "average_burden": 15.0,
            "burden_unit": "review_minutes",
            "diagnostic_confidence": 0.8,
            "formula": PRIORITIZATION_FORMULA,
        }
    ]
    assert result["unranked"] == [
        {
            "cluster": "validation_failure:validation_runtime_not_reported",
            "reason": "unknown_comparable_burden",
        }
    ]


def test_verifier_diagnostics_are_human_labelled_and_exclude_infrastructure() -> None:
    observations = [
        VerifierEvidenceObservation(
            case_id="tp",
            trial_reference="private_trial_tp",
            verifier_proved_acceptable=True,
            human_accepted_as_is=True,
            evidence_types=["deterministic_check"],
            semantic_result=True,
            deterministic_result=True,
            artifact_references=["trials/tp/trial.json"],
            evidence_kind="real_founder_work",
        ),
        VerifierEvidenceObservation(
            case_id="fp",
            trial_reference="private_trial_fp",
            verifier_proved_acceptable=True,
            human_accepted_as_is=False,
            requirement_errors=["requirement_not_met"],
            evidence_types=["semantic_review"],
            semantic_result=True,
            deterministic_result=False,
            artifact_references=["trials/fp/trial.json"],
            evidence_kind="real_founder_work",
        ),
        VerifierEvidenceObservation(
            case_id="tn",
            trial_reference="private_trial_tn",
            verifier_proved_acceptable=False,
            human_accepted_as_is=False,
            evidence_types=["deterministic_check"],
            artifact_references=["trials/tn/trial.json"],
            evidence_kind="real_founder_work",
        ),
        VerifierEvidenceObservation(
            case_id="fn",
            trial_reference="private_trial_fn",
            verifier_proved_acceptable=False,
            human_accepted_as_is=True,
            evidence_types=["semantic_review"],
            semantic_result=False,
            deterministic_result=True,
            artifact_references=["trials/fn/trial.json"],
            evidence_kind="real_founder_work",
        ),
        VerifierEvidenceObservation(
            case_id="infra",
            trial_reference="private_trial_infrastructure",
            verifier_proved_acceptable=None,
            human_accepted_as_is=None,
            infrastructure_exclusion=True,
            artifact_references=["trials/infra/trial.json"],
            evidence_kind="real_founder_work",
        ),
        VerifierEvidenceObservation(
            case_id="synthetic",
            trial_reference="synthetic_trial",
            verifier_proved_acceptable=True,
            human_accepted_as_is=False,
            evidence_kind="synthetic_fixture",
        ),
    ]

    diagnostics = build_verifier_diagnostics(observations)

    assert diagnostics["human_labelled_cases"] == 4
    assert diagnostics["confusion_matrix"] == {
        "true_positive": 1,
        "false_positive": 1,
        "true_negative": 1,
        "false_negative": 1,
    }
    assert diagnostics["precision"] == 0.5
    assert diagnostics["recall"] == 0.5
    assert diagnostics["specificity"] == 0.5
    assert diagnostics["f1"] == 0.5
    assert diagnostics["infrastructure_exclusions"]["count"] == 1
    assert diagnostics["requirement_level_errors"] == [
        {"requirement_error": "requirement_not_met", "count": 1}
    ]
    assert diagnostics["semantic_deterministic_disagreement"]["count"] == 2
    assert diagnostics["calibration"]["probability_fabricated"] is False
    serialized = json.dumps(diagnostics, sort_keys=True)
    assert "private_trial_fp" not in serialized
    assert "synthetic_trial" not in serialized


def test_exact_before_after_identity_and_tool_version_disclosure() -> None:
    before = _frozen_outcome(cost=3, review_minutes=8, tool_version="1")
    after = _frozen_outcome(cost=2, review_minutes=5, tool_version="2")

    comparisons = compare_frozen_outcomes([before], [after])

    assert len(comparisons) == 1
    comparison = comparisons[0]
    assert comparison["baseline_digest"] == "2" * 64
    assert comparison["cost_delta"] == -1
    assert comparison["review_minutes_delta"] == -3
    assert comparison["tool_version_changes"] == {
        "product": {"before": "1", "after": "2"}
    }
    assert before.task_digest not in comparison["task_reference"]

    drifted = after.model_copy(update={"baseline_digest": "9" * 64})
    with pytest.raises(ValueError, match="identities differ"):
        compare_frozen_outcomes([before], [drifted])
    with pytest.raises(ValueError, match="must be unique"):
        compare_frozen_outcomes([before, before], [after])


def test_paired_population_requires_same_repetition_task_and_baseline() -> None:
    task = _real_task("private_repository")
    duration = DurationAmount(
        value_ms=1000,
        accounting_status="complete",
        source="measured_test_duration",
    )
    direct = _trial_with_duration(duration).model_copy(
        update={
            "trial_id": "trial_direct",
            "task_id": task.task_id,
            "task_digest": task.content_digest,
            "baseline_digest": task.immutable_baseline_digest,
            "baseline_restore_digest": task.immutable_baseline_digest,
            "arm": "direct",
            "repetition": 1,
        }
    )
    wrong_repetition = direct.model_copy(
        update={
            "trial_id": "trial_villani_wrong_repetition",
            "arm": "villani",
            "repetition": 2,
        }
    )

    paired, paired_trials = _paired_real_population(
        "real_founder_work", [task], [direct, wrong_repetition]
    )
    assert paired == set()
    assert paired_trials == []

    matching = wrong_repetition.model_copy(update={"repetition": 1})
    paired, paired_trials = _paired_real_population(
        "real_founder_work", [task], [direct, matching]
    )
    assert paired == {task.task_id}
    assert {trial.arm for trial in paired_trials} == {"direct", "villani"}

    drifted = matching.model_copy(update={"baseline_digest": "9" * 64})
    paired, paired_trials = _paired_real_population(
        "real_founder_work", [task], [direct, drifted]
    )
    assert paired == set()
    assert paired_trials == []


def test_unknown_duration_regression_preserves_unknown_accounting() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[4]
        / "integration"
        / "fixtures"
        / "evaluation"
        / "pt4"
        / "unknown-duration-accounting.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    duration = DurationAmount.model_validate(fixture["input"]["duration"])
    trial = _trial_with_duration(duration)

    _reliability, review, _cost, _supervision, _false = _metrics(
        [trial], [_accepted_review(trial)]
    )
    metric = review["direct.elapsed_time_per_accepted_as_is_change_ms"]

    assert fixture["task_or_repository_identifiers_present"] is False
    assert fixture["before"]["elapsed_time_per_accepted_as_is_change_ms"] == {
        "value": 0.0,
        "accounting_status": "complete",
    }
    assert metric.value is None
    assert metric.accounting_status == "unknown"
    assert (
        metric.model_dump()["value"]
        is fixture["after"]["elapsed_time_per_accepted_as_is_change_ms"]["value"]
    )


def test_insufficient_evidence_fails_closed_and_recalculates_gate(
    tmp_path: Path,
) -> None:
    suite_root = _empty_frozen_suite(tmp_path)

    analysis = build_hardening_analysis(
        suite_root,
        failure_observations=[_failure("synthetic", evidence_kind="synthetic_fixture")],
        correctness_regressions=[
            {
                "mechanism": "unknown_duration_was_coerced_to_numeric_zero",
                "result": "passed",
            }
        ],
    )

    assert analysis["status"] == "INSUFFICIENT_EVIDENCE"
    assert analysis["gate_b"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert analysis["sufficiency"]["paired_real_tasks"] == 0
    assert analysis["sufficiency"]["baseline_integrity"] is None
    assert analysis["sufficiency"]["human_labels_complete"] is False
    assert analysis["failure_clusters"] == []
    assert analysis["prioritization"]["ranked"] == []
    assert analysis["production_changes_authorized"] is False
    assert analysis["speculative_performance_changes"] == []
    assert analysis["verifier_behavior_changed"] is False
    assert analysis["no_false_acceptance_introduced"] == (
        "not_applicable_no_verifier_change"
    )
    assert analysis["founder_proof_certificate"] is None
    assert analysis["founder_proof_certificate_issued"] is False
    assert analysis["pt5_authorized"] is False
    assert analysis["pt5_started"] is False


def test_certificate_requires_pass_is_content_addressed_and_redacts_repositories(
    tmp_path: Path,
) -> None:
    suite_root = _empty_frozen_suite(tmp_path)
    base_report = build_report(suite_root)
    passed_report = base_report.model_copy(
        update={
            "evidence_kind": "real_founder_work",
            "founder_gate_status": "PASS",
            "confusion_matrix": {
                "true_positive": 30,
                "false_positive": 0,
                "true_negative": 0,
                "false_negative": 0,
            },
            "founder_gate_checks": [
                GateCheck(
                    check_id="minimum_paired_tasks",
                    status="pass",
                    actual=30,
                    required=30,
                    reason="enough paired tasks",
                ),
                GateCheck(
                    check_id="review_or_cost_improvement",
                    status="pass",
                    actual={
                        "median_review_time_reduction": 0.31,
                        "total_cost_per_accepted_change_reduction": 0.10,
                    },
                    required={"review_time": 0.30, "cost": 0.25},
                    reason="review threshold passed",
                ),
                GateCheck(
                    check_id="automatic_configuration",
                    status="pass",
                    actual=0.82,
                    required=0.80,
                    reason="configuration threshold passed",
                ),
            ],
        }
    )
    private_repository = "founder/private/repository/path"

    first = build_founder_proof_certificate(
        report=passed_report,
        tasks=[_real_task(private_repository)],
        trials=[],
    )
    second = build_founder_proof_certificate(
        report=passed_report,
        tasks=[_real_task(private_repository)],
        trials=[],
    )

    assert first == second
    assert first["certificate_digest"] == second["certificate_digest"]
    assert first["repositories_redacted"] is True
    assert private_repository not in json.dumps(first, sort_keys=True)
    assert first["repositories"][0].startswith("repository_")
    with pytest.raises(ValueError, match="requires Gate B PASS"):
        build_founder_proof_certificate(
            report=base_report,
            tasks=[_real_task(private_repository)],
            trials=[],
        )
    unsafe_report = passed_report.model_copy(
        update={
            "confusion_matrix": {
                "true_positive": 29,
                "false_positive": 1,
                "true_negative": 0,
                "false_negative": 0,
            }
        }
    )
    with pytest.raises(ValueError, match="forbids known false acceptance"):
        build_founder_proof_certificate(
            report=unsafe_report,
            tasks=[_real_task(private_repository)],
            trials=[],
        )


def test_static_scan_rejects_exact_task_identifiers_in_production_rules(
    tmp_path: Path,
) -> None:
    identifier = "founder_task_identifier_canary_7f36a9d1"
    production_root = Path(__file__).resolve().parents[1] / "evaluation_lab"

    assert (
        scan_production_for_evidence_identifiers([production_root], [identifier]) == []
    )

    canary_root = tmp_path / "production"
    canary_root.mkdir()
    canary_file = canary_root / "rule.py"
    canary_file.write_text(f'TASK = "{identifier}"\n', encoding="utf-8")
    violations = scan_production_for_evidence_identifiers([canary_root], [identifier])

    assert len(violations) == 1
    assert identifier not in violations[0]
    assert violations[0].endswith(
        ":identifier_"
        + __import__("hashlib").sha256(identifier.encode("utf-8")).hexdigest()[:16]
    )
