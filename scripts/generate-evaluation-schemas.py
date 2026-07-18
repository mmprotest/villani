#!/usr/bin/env python3
"""Regenerate normative and packaged Founder Thesis Lab v1 schemas."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "components" / "villani-ops"
sys.path.insert(0, str(OPS))

from villani_ops.evaluation_lab.models import (  # noqa: E402
    AccountingAmount,
    AgentSystemIdentity,
    DurationAmount,
    EvaluationReport,
    EvaluationSuite,
    EvaluationTask,
    EvaluationTrial,
    FileChangeRequirement,
    GateCheck,
    HumanReview,
    MetricValue,
    SourceSnapshot,
    TaskProvenance,
    ValidationCommand,
)


MODELS = {
    "evaluation-suite.schema.json": EvaluationSuite,
    "evaluation-task.schema.json": EvaluationTask,
    "evaluation-trial.schema.json": EvaluationTrial,
    "human-review.schema.json": HumanReview,
    "evaluation-report.schema.json": EvaluationReport,
}


def main() -> None:
    destinations = (
        ROOT / "schemas" / "v1",
        OPS / "villani_ops" / "schemas" / "v1",
    )
    for filename, model in MODELS.items():
        schema = model.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://villani.dev/schemas/v1/{filename}"
        text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for destination in destinations:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / filename).write_text(text, encoding="utf-8")

    fixture_root = ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"
    now = "2026-07-17T00:00:00Z"
    suite = EvaluationSuite.model_validate(
        {
            "suite_id": "suite_fixture",
            "title": "Synthetic contract fixture",
            "suite_version": 1,
            "status": "frozen",
            "created_at": now,
            "frozen_at": now,
            "randomization_seed": "fixture-randomization-seed-v1",
            "task_versions": [{"task_id": "task_fixture", "task_digest": "b" * 64}],
            "evidence_kind": "synthetic_fixture",
            "confidentiality": "internal",
            "disclosure_complete": True,
            "content_digest": "a" * 64,
        }
    )
    snapshot = SourceSnapshot(
        repository_identity="repo_fixture",
        resolved_commit="1" * 40,
        baseline_digest="c" * 64,
        archive_digest="d" * 64,
        archive_path="baselines/fixture/code.zip",
        included_paths=["src/value.txt"],
        excluded_paths=[],
        file_count=1,
        restore_verified=True,
    )
    task = EvaluationTask(
        task_id="task_fixture",
        suite_id="suite_fixture",
        task_version=1,
        immutable_baseline_digest="c" * 64,
        source_snapshot=snapshot,
        verbatim_task="Update the value while preserving behavior.",
        success_criteria=["The authoritative validation passes."],
        authoritative_validation=[
            ValidationCommand(validation_id="check_001", argv=["python", "-m", "pytest"])
        ],
        file_change_requirement=FileChangeRequirement(),
        provenance=TaskProvenance(
            captured_at=now,
            captured_by="fixture",
            source_reference="synthetic_contract_fixture",
        ),
        confidentiality="internal",
        evidence_kind="synthetic_fixture",
        evidence_eligible=False,
        frozen=True,
        content_digest="b" * 64,
    )
    unknown = AccountingAmount(
        value=None,
        currency=None,
        accounting_status="unknown",
        source="fixture_unknown",
    )
    not_applicable = AccountingAmount(
        value=None,
        currency=None,
        accounting_status="not_applicable",
        source="fixture_local_verification",
    )
    trial = EvaluationTrial(
        trial_id="trial_fixture",
        suite_id="suite_fixture",
        suite_digest="a" * 64,
        task_id="task_fixture",
        task_digest="b" * 64,
        arm="direct",
        repetition=1,
        randomized_order=1,
        order_digest="e" * 64,
        status="completed",
        started_at=now,
        completed_at=now,
        agent_system=AgentSystemIdentity(
            product="Direct coding system",
            product_version="fixture",
            harness="direct_single_call",
            harness_version="fixture",
            agent="fixture-agent",
            agent_version="fixture",
            execution_provider="inherit",
            environment_fingerprint="fixture-environment",
        ),
        run_id="direct_fixture",
        baseline_digest="c" * 64,
        baseline_restore_digest="c" * 64,
        execution_cost=unknown,
        verification_cost=not_applicable,
        local_compute_cost=unknown,
        total_cost=unknown,
        duration=DurationAmount(
            value_ms=100,
            accounting_status="complete",
            source="measured_fixture",
        ),
        proved_acceptable=False,
        verification_status="complete",
        target_repository_modified=False,
        attempts=1,
        escalations=0,
        configuration_mode="automatic",
        artifact_references=["trial.json"],
        evidence_eligible=False,
    )
    review = HumanReview(
        review_id="review_fixture",
        trial_id="trial_fixture",
        created_at=now,
        reviewer_id="fixture-reviewer",
        blinded=True,
        arm_revealed_during_review=False,
        outcome="accepted_as_is",
        correction_required=False,
        review_minutes=1,
        severity="none",
        false_acceptance=False,
        false_rejection=True,
    )
    report = EvaluationReport(
        report_id="report_fixture",
        suite_id="suite_fixture",
        suite_digest="a" * 64,
        generated_at=now,
        evidence_kind="synthetic_fixture",
        confidentiality="internal",
        raw_counts={"tasks": 1},
        reliability={"direct.proved_acceptable_rate": MetricValue(value=0, numerator=0, denominator=1, accounting_status="complete")},
        review_time={},
        cost={},
        supervision={},
        false_acceptance={},
        paired_task_deltas=[],
        task_classes=[],
        failure_modes=[],
        missing_evidence=[],
        confusion_matrix={"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 1},
        classification_metrics={"precision": None, "recall": 0, "specificity": None, "f1": None},
        calibration={"status": "not_defined"},
        verifier_wrong_cases=[],
        cost_decomposition=[],
        route_decomposition=[],
        trial_bundle_links=["trials/trial_fixture/trial.json"],
        unknowns=[{"field": "total_cost"}],
        exclusions=[],
        disclosures_complete=True,
        founder_gate_status="INSUFFICIENT_EVIDENCE",
        founder_gate_checks=[
            GateCheck(
                check_id="minimum_paired_tasks",
                status="insufficient_evidence",
                actual=0,
                required=30,
                reason="Synthetic fixtures do not count.",
            )
        ],
    )
    fixtures = {
        "evaluation-suite.json": suite,
        "evaluation-task.json": task,
        "evaluation-trial.json": trial,
        "human-review.json": review,
        "evaluation-report.json": report,
    }
    for filename, value in fixtures.items():
        (fixture_root / filename).write_text(
            json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
