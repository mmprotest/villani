from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from villani_ops.closed_loop.capabilities.effective import (
    resolve_effective_capability,
)
from villani_ops.closed_loop.capabilities.ingest import rebuild_snapshot
from villani_ops.closed_loop.capabilities.models import (
    CapabilityProfile,
    CapabilitySnapshot,
    EmpiricalBackendInput,
    IncludedAttempt,
    ProfileKey,
)
from villani_ops.closed_loop.capabilities.optimizer import optimize_sequence
from villani_ops.closed_loop.capabilities.scoring import (
    expected_cost_to_success,
    resolve_empirical_score,
    wilson_lower_bound,
)
from villani_ops.closed_loop.capabilities.store import CapabilityStore
from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.interfaces import (
    AttemptSummary,
    BudgetContext,
    ClosedLoopRunRequest,
    PolicyContext,
    VerificationSummary,
)
from villani_ops.closed_loop.policy import BootstrapPolicyEngine
from villani_ops.closed_loop.protocol import ClassificationSnapshot
from villani_ops.core.backend import Backend
from villani_ops.tests.closed_loop.fakes import (
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
    backend as fake_backend,
    policy as fake_policy,
)


SCORER = "empirical_wilson_v1"
CLASSIFIER = "fixture_classifier_v1"
VERIFIER = "fixture_verifier_v1"
STAMP = "2026-07-10T00:00:10Z"


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _fixture_run(
    runs_root: Path,
    run_id: str,
    *,
    attempt_id: str = "attempt_001",
    backend: str = "fixture",
    provider: str = "local",
    model: str = "fixture-model",
    category: str = "bug_fix",
    difficulty: str = "medium",
    risk: str = "low",
    outcome: str = "accepted",
    acceptance_eligible: bool = True,
    failure_category: str | None = None,
    attempt_status: str = "completed",
    materialization_status: str | None = "succeeded",
    cost: float | None = 2.0,
    cost_status: str = "complete",
    duration_ms: int | None = 1_000,
    human_modified: bool = False,
    outcome_label: str | None = None,
    accepted_candidates_required: int = 1,
    repository_validation_status: str | None = None,
    repository_validation_failure_code: str | None = None,
) -> Path:
    """Materialize a deterministic minimal canonical run-bundle fixture."""

    run = runs_root / run_id
    attempt_metadata: dict[str, Any] = {"provider": provider}
    if failure_category:
        attempt_metadata["failure_category"] = failure_category
    if human_modified:
        attempt_metadata["human_modified"] = True
        attempt_metadata["human_modification_label"] = "reviewer_adjusted"
    if outcome_label:
        attempt_metadata["capability_outcome_label"] = outcome_label
    if repository_validation_status is None:
        if acceptance_eligible:
            repository_validation_status = "passed"
        elif failure_category in {
            "implementation_failure",
            "capability_failure",
            "no_change_failure",
        }:
            repository_validation_status = "failed"
        elif failure_category == "infrastructure_failure":
            repository_validation_status = "infrastructure_error"
        else:
            repository_validation_status = "passed"
    if repository_validation_failure_code is None:
        repository_validation_failure_code = {
            "passed": "repository_validation_passed",
            "failed": "repository_validation_test_failure",
            "infrastructure_error": "repository_validation_provider_failure",
        }.get(repository_validation_status, "repository_validation_unavailable")
    verification_metadata: dict[str, Any] = {
        "verifier_version": VERIFIER,
        "repository_validation_status": repository_validation_status,
        "repository_validation_failure_code": (
            repository_validation_failure_code
        ),
        "repository_validation_authoritative": (
            repository_validation_status in {"passed", "failed"}
        ),
        "infrastructure_failure_present": failure_category
        in {"infrastructure_failure", "verification_failure"},
        "computed_final_result": 1 if acceptance_eligible else 0,
    }
    if failure_category:
        verification_metadata["failure_category"] = failure_category
    if outcome_label:
        verification_metadata["capability_outcome_label"] = outcome_label

    _write(
        run / "manifest.json",
        {
            "schema_version": "villani.run_manifest.v1",
            "run_id": run_id,
            "trace_id": f"trace_{run_id}",
            "task_id": f"task_{run_id}",
            "created_at": "2026-07-10T00:00:00Z",
            "updated_at": STAMP,
            "completed_at": STAMP,
            "final_state": "COMPLETED"
            if materialization_status == "succeeded"
            else "FAILED",
            "attempt_ids": [attempt_id],
            "selected_attempt_id": attempt_id if acceptance_eligible else None,
            "total_cost_usd": cost,
            "cost_accounting_status": cost_status,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "token_accounting_status": "complete",
            "total_duration_ms": duration_ms,
            "duration_accounting_status": "complete"
            if duration_ms is not None
            else "unknown",
            "artifact_paths": {
                "task": "task.json",
                "classification": "classification.json",
                "state": "state.json",
                "events": "events.jsonl",
                "policy_decisions": "policy_decisions.jsonl",
                "selection": "selection.json",
                "materialization": "materialization.json",
            },
            "metadata": {
                "policy_configuration": {
                    "policy": {
                        "accepted_candidates_required": accepted_candidates_required
                    },
                    "backends": {backend: {"provider": provider, "model": model}},
                }
            },
        },
    )
    _write(
        run / "classification.json",
        {
            "schema_version": "villani.classification.v1",
            "classification_id": "classification_001",
            "run_id": run_id,
            "task_id": f"task_{run_id}",
            "classified_at": "2026-07-10T00:00:01Z",
            "difficulty": difficulty,
            "risk": risk,
            "category": category,
            "required_capabilities": ["code_editing"],
            "estimated_attempts_needed": 1,
            "needs_tests": True,
            "confidence": 0.9,
            "reasoning_summary": "deterministic M8 fixture",
            "signals": {},
            "metadata": {"classifier_version": CLASSIFIER},
        },
    )
    _write(
        run / "attempts" / attempt_id / "attempt.json",
        {
            "schema_version": "villani.attempt.v1",
            "attempt_id": attempt_id,
            "run_id": run_id,
            "trace_id": f"trace_{run_id}",
            "ordinal": 1,
            "backend_name": backend,
            "runner_name": "villani_code",
            "model": model,
            "status": attempt_status,
            "started_at": "2026-07-10T00:00:02Z",
            "completed_at": "2026-07-10T00:00:04Z",
            "worktree_path": ".worktrees/attempt_001",
            "patch_path": f"attempts/{attempt_id}/patch.diff",
            "patch_sha256": "a" * 64,
            "patch_bytes": 10,
            "stdout_path": f"attempts/{attempt_id}/stdout.log",
            "stderr_path": f"attempts/{attempt_id}/stderr.log",
            "runner_telemetry_path": f"attempts/{attempt_id}/runner_telemetry.json",
            "trace_path": f"attempts/{attempt_id}/trace/events.jsonl",
            "exit_code": 0 if attempt_status == "completed" else 1,
            "duration_ms": duration_ms,
            "duration_accounting_status": "complete"
            if duration_ms is not None
            else "unknown",
            "input_tokens": 100,
            "output_tokens": 50,
            "token_accounting_status": "complete",
            "cost_usd": cost,
            "cost_accounting_status": cost_status,
            "error": None,
            "metadata": attempt_metadata,
        },
    )
    _write(
        run / "verification" / f"{attempt_id}.json",
        {
            "schema_version": "villani.verification.v1",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "verified_at": "2026-07-10T00:00:06Z",
            "verifier": "fixture_verifier",
            "outcome": outcome,
            "acceptance_eligible": acceptance_eligible,
            "confidence": 0.99 if outcome != "error" else None,
            "reason": "deterministic verifier result",
            "requirement_results": [],
            "success_evidence": [],
            "failure_evidence": [],
            "missing_evidence": [],
            "risk_flags": [],
            "recommended_action": "accept"
            if acceptance_eligible
            else ("retry_verifier" if outcome == "error" else "reject"),
            "raw_verifier_artifact": None,
            "metadata": verification_metadata,
        },
    )
    if acceptance_eligible:
        selected = outcome_label != "accepted_not_selected"
        _write(
            run / "selection.json",
            {
                "schema_version": "villani.selection.v1",
                "selection_id": "selection_001",
                "run_id": run_id,
                "selected_at": "2026-07-10T00:00:07Z",
                "strategy": "deterministic_evidence_v1",
                "eligible_candidate_ids": [attempt_id],
                "selected_candidate_ids": [attempt_id] if selected else [],
                "rankings": [],
                "reason": "fixture selection",
                "advisory_comparison": None,
                "metadata": {},
            },
        )
        if selected and materialization_status is not None:
            _write(
                run / "materialization.json",
                {
                    "schema_version": "villani.materialization.v1",
                    "materialization_id": "materialization_001",
                    "run_id": run_id,
                    "trace_id": f"trace_{run_id}",
                    "selection_id": "selection_001",
                    "selected_attempt_id": attempt_id,
                    "started_at": "2026-07-10T00:00:08Z",
                    "completed_at": STAMP
                    if materialization_status == "succeeded"
                    else None,
                    "status": materialization_status,
                    "source_patch_path": f"attempts/{attempt_id}/patch.diff",
                    "target_repository_path": "/fixture/repo",
                    "materialized_patch_path": "final.patch"
                    if materialization_status == "succeeded"
                    else None,
                    "patch_sha256": "a" * 64
                    if materialization_status == "succeeded"
                    else None,
                    "changed_files": ["example.py"]
                    if materialization_status == "succeeded"
                    else [],
                    "failure": None
                    if materialization_status == "succeeded"
                    else {
                        "code": "apply_failed",
                        "message": "fixture failure",
                        "details": {},
                    },
                    "metadata": {},
                },
            )
    return run


def _fine(snapshot: CapabilitySnapshot, backend: str = "fixture") -> CapabilityProfile:
    return next(
        profile
        for profile in snapshot.profiles
        if profile.key.backend_name == backend
        and profile.key.task_category == "bug_fix"
        and profile.key.difficulty == "medium"
        and profile.key.risk == "low"
    )


def test_accepted_materialized_attempt_counts_as_success(tmp_path: Path) -> None:
    _fixture_run(tmp_path, "run_success")
    profile = _fine(rebuild_snapshot(tmp_path))
    assert (
        profile.successes,
        profile.verified_model_failures,
        profile.sample_count,
    ) == (1, 0, 1)
    assert profile.included_attempts[0].attempt_id == "attempt_001"


@pytest.mark.parametrize(
    "failure_category",
    ["implementation_failure", "capability_failure", "no_change_failure"],
)
def test_verified_rejection_counts_as_failure(
    tmp_path: Path, failure_category: str
) -> None:
    _fixture_run(
        tmp_path,
        "run_failure",
        outcome="rejected",
        acceptance_eligible=False,
        failure_category=failure_category,
        materialization_status=None,
    )
    profile = _fine(rebuild_snapshot(tmp_path))
    assert (
        profile.successes,
        profile.verified_model_failures,
        profile.sample_count,
    ) == (0, 1, 1)


@pytest.mark.parametrize(
    ("category", "outcome", "reason"),
    [
        ("infrastructure_failure", "rejected", "provider_failure"),
        (
            "verification_failure",
            "error",
            "verifier_infrastructure_failure",
        ),
    ],
)
def test_non_model_failures_are_excluded(
    tmp_path: Path, category: str, outcome: str, reason: str
) -> None:
    _fixture_run(
        tmp_path,
        f"run_{category}",
        outcome=outcome,
        acceptance_eligible=False,
        failure_category=category,
        materialization_status=None,
    )
    profile = _fine(rebuild_snapshot(tmp_path))
    assert profile.sample_count == 0
    assert profile.excluded_outcome_counts[reason] == 1


def test_human_modified_result_is_excluded_with_separate_count(tmp_path: Path) -> None:
    _fixture_run(tmp_path, "run_human", human_modified=True)
    profile = _fine(rebuild_snapshot(tmp_path))
    assert profile.sample_count == 0
    assert profile.excluded_outcome_counts["human_modified"] == 1


def test_duplicate_attempt_is_ignored(tmp_path: Path) -> None:
    source = _fixture_run(tmp_path, "run_duplicate")
    duplicate = tmp_path / "copied_directory"
    shutil.copytree(source, duplicate)
    snapshot = rebuild_snapshot(tmp_path)
    profile = _fine(snapshot)
    assert profile.sample_count == 1
    assert snapshot.excluded_outcome_counts["duplicate_attempt"] == 1


def test_materialization_failure_is_excluded(tmp_path: Path) -> None:
    _fixture_run(tmp_path, "run_materialize", materialization_status="failed")
    profile = _fine(rebuild_snapshot(tmp_path))
    assert profile.sample_count == 0
    assert profile.excluded_outcome_counts["materialization_failure"] == 1


def test_fixture_exclusion_counts_are_exact(tmp_path: Path) -> None:
    _fixture_run(tmp_path, "run_success")
    _fixture_run(
        tmp_path,
        "run_infrastructure",
        outcome="rejected",
        acceptance_eligible=False,
        failure_category="infrastructure_failure",
        materialization_status=None,
    )
    _fixture_run(
        tmp_path,
        "run_verifier",
        outcome="error",
        acceptance_eligible=False,
        failure_category="verification_failure",
        materialization_status=None,
    )
    _fixture_run(tmp_path, "run_human", human_modified=True)
    _fixture_run(tmp_path, "run_materialization", materialization_status="failed")
    source = _fixture_run(tmp_path, "run_duplicate")
    shutil.copytree(source, tmp_path / "duplicate_copy")
    snapshot = rebuild_snapshot(tmp_path)
    assert snapshot.excluded_outcome_counts == {
        "duplicate_attempt": 1,
        "human_modified": 1,
        "materialization_failure": 1,
        "provider_failure": 1,
        "verifier_infrastructure_failure": 1,
    }
    assert _fine(snapshot).sample_count == 2


def test_profile_aggregates_known_cost_duration_and_tokens(tmp_path: Path) -> None:
    _fixture_run(tmp_path, "run_one", cost=2.0, duration_ms=1_000)
    _fixture_run(
        tmp_path,
        "run_two",
        outcome="rejected",
        acceptance_eligible=False,
        failure_category="capability_failure",
        materialization_status=None,
        cost=4.0,
        duration_ms=3_000,
    )
    profile = _fine(rebuild_snapshot(tmp_path))
    assert profile.raw_success_rate == 0.5
    assert profile.mean_actual_attempt_cost == 3.0
    assert profile.median_actual_attempt_cost == 3.0
    assert profile.mean_duration_ms == 2_000.0
    assert profile.median_duration_ms == 2_000.0
    assert profile.mean_input_tokens == 100.0
    assert profile.mean_output_tokens == 50.0
    assert profile.first_observed_at == "2026-07-10T00:00:06Z"
    assert profile.last_observed_at == "2026-07-10T00:00:06Z"


def test_explicit_unselected_acceptance_in_multi_candidate_policy_counts(
    tmp_path: Path,
) -> None:
    _fixture_run(
        tmp_path,
        "run_unselected",
        outcome_label="accepted_not_selected",
        accepted_candidates_required=2,
        materialization_status=None,
    )
    profile = _fine(rebuild_snapshot(tmp_path))
    assert profile.successes == 1


def test_controller_explicitly_labels_clean_unselected_acceptance(
    tmp_path: Path,
) -> None:
    option = fake_backend("fixture")
    controller = ClosedLoopController(
        classifier=FakeClassifier(),
        policy_engine=FakePolicyEngine(
            [
                fake_policy("attempt", backend_option=option),
                fake_policy("attempt", backend_option=option),
                fake_policy("select"),
            ]
        ),
        attempt_runner=FakeAttemptRunner([attempt(), attempt()]),
        verifier=FakeVerifier([accepted_verification(), accepted_verification()]),
        selector=FakeSelector(selected_attempt_id="attempt_001"),
        materializer=FakeMaterializer(),
        now=FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
    )
    result = controller.run(
        ClosedLoopRunRequest(
            task="collect two accepted candidates",
            repository_path=tmp_path / "repo",
            success_criteria="both candidates verify",
            runs_root=tmp_path / "runs",
            max_attempts=2,
            policy_configuration={
                "policy": {
                    "version": "bootstrap_v1",
                    "accepted_candidates_required": 2,
                }
            },
        )
    )
    assert result.terminal_state == "COMPLETED"
    unselected_attempt = json.loads(
        (result.run_directory / "attempts" / "attempt_002" / "attempt.json").read_text(
            encoding="utf-8"
        )
    )
    unselected_verification = json.loads(
        (result.run_directory / "verification" / "attempt_002.json").read_text(
            encoding="utf-8"
        )
    )
    assert unselected_attempt["metadata"]["capability_outcome_label"] == (
        "accepted_not_selected"
    )
    assert unselected_verification["metadata"]["capability_outcome_label"] == (
        "accepted_not_selected"
    )


def test_rebuild_is_atomic_idempotent_and_provenance_is_append_only(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    registry = tmp_path / "capabilities"
    _fixture_run(runs, "run_stable")
    store = CapabilityStore(registry)
    first = store.rebuild(runs)
    snapshot_bytes = store.snapshot_path.read_bytes()
    provenance_bytes = store.provenance_path.read_bytes()
    second = store.rebuild(runs)
    assert first.snapshot.profile_digest == second.snapshot.profile_digest
    assert store.snapshot_path.read_bytes() == snapshot_bytes
    assert store.provenance_path.read_bytes() == provenance_bytes
    assert len(store.provenance_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not list(registry.glob("*.tmp"))


@pytest.mark.parametrize(
    ("successes", "samples", "expected"),
    [
        (0, 10, 0.0),
        (5, 10, 0.236593090512564),
        (20, 20, 0.8388748419471806),
    ],
)
def test_wilson_bound_matches_known_values(
    successes: int, samples: int, expected: float
) -> None:
    assert wilson_lower_bound(successes, samples) == pytest.approx(expected, abs=1e-15)


def _key(
    *,
    backend: str = "fixture",
    model: str = "fixture-model",
    category: str = "bug_fix",
    difficulty: str = "medium",
    risk: str = "low",
) -> ProfileKey:
    return ProfileKey(
        backend_name=backend,
        provider="local",
        model=model,
        task_category=category,
        difficulty=difficulty,
        risk=risk,
        classifier_version=CLASSIFIER,
        verifier_version=VERIFIER,
        scorer_version=SCORER,
    )


def _profile(
    key: ProfileKey, samples: int, successes: int, *, mean_cost: float = 2.0
) -> CapabilityProfile:
    return CapabilityProfile(
        key=key,
        included_attempts=[
            IncludedAttempt(
                run_id=f"run_{index:03d}",
                attempt_id="attempt_001",
                outcome=("success" if index < successes else "verified_model_failure"),
            )
            for index in range(samples)
        ],
        successes=successes,
        verified_model_failures=samples - successes,
        sample_count=samples,
        raw_success_rate=successes / samples if samples else 0.0,
        wilson_lower_bound=wilson_lower_bound(successes, samples),
        mean_actual_attempt_cost=mean_cost,
        median_actual_attempt_cost=mean_cost,
        mean_duration_ms=1000.0,
        median_duration_ms=1000.0,
        mean_input_tokens=100.0,
        mean_output_tokens=50.0,
        excluded_outcome_counts={},
        first_observed_at=STAMP if samples else None,
        last_observed_at=STAMP if samples else None,
        source_data_digest="1" * 64,
    )


def _snapshot(*profiles: CapabilityProfile) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        schema_version="villani.capability_snapshot.v1",
        scorer_version=SCORER,
        source_data_digest="2" * 64,
        profile_digest="3" * 64,
        generated_at=STAMP,
        profiles=list(profiles),
        excluded_outcome_counts={},
        source_run_count=1,
        source_attempt_count=sum(profile.sample_count for profile in profiles),
    )


def test_sparse_group_falls_back_to_static_score() -> None:
    selected = resolve_empirical_score(
        _snapshot(_profile(_key(), 19, 15)),
        _key(),
        static_capability_score=77,
        minimum_empirical_samples=20,
    )
    assert selected.empirical_status == "insufficient_data"
    assert selected.capability_score_used == 77
    assert selected.score_source == "static"
    assert selected.static_capability_score == 77


def test_sparse_fine_group_backs_off_to_category_difficulty() -> None:
    selected = resolve_empirical_score(
        _snapshot(
            _profile(_key(), 5, 5),
            _profile(_key(risk="*"), 30, 24),
        ),
        _key(),
        static_capability_score=77,
        minimum_empirical_samples=20,
    )
    assert selected.empirical_status == "sufficient_data"
    assert selected.selected_level == "category_difficulty"
    assert selected.empirical_capability_score == int(100 * wilson_lower_bound(24, 30))


def test_empirical_score_never_overwrites_static_score() -> None:
    selected = resolve_empirical_score(
        _snapshot(_profile(_key(), 20, 20)),
        _key(),
        static_capability_score=12,
        minimum_empirical_samples=20,
    )
    assert selected.static_capability_score == 12
    assert selected.empirical_capability_score != 12
    assert selected.score_source == "empirical"


def test_expected_cost_to_success_formula() -> None:
    assert expected_cost_to_success(2.0, 0.5, sufficient=True) == pytest.approx(4.0)
    assert expected_cost_to_success(None, 0.5, sufficient=True) is None
    assert expected_cost_to_success(2.0, 0.0, sufficient=True) is None


def _input(
    name: str, probability: float | None, cost: float | None, *, sufficient: bool = True
) -> EmpiricalBackendInput:
    return EmpiricalBackendInput(
        backend_name=name,
        conservative_success_probability=probability,
        mean_actual_attempt_cost=cost,
        sufficient_probability_data=sufficient,
        profile_version=SCORER,
        profile_digest="4" * 64,
        sample_count=20 if sufficient else 19,
        effective_capability_score=(probability or 0.0) * 100.0,
        median_duration_ms=1_000.0,
        profile_level="category_difficulty_risk",
        probability_source="wilson_lower_bound",
        cost_source="actual_profile_mean",
    )


def test_two_backend_sequence_formulas_and_cheap_first_choice() -> None:
    result = optimize_sequence(
        [_input("cheap", 0.5, 1.0), _input("strong", 0.7, 4.0)],
        max_attempts=2,
        target_success_probability=0.80,
    )
    cheap_first = next(
        x for x in result.considered_sequences if x.backends == ("cheap", "strong")
    )
    assert cheap_first.expected_cost == pytest.approx(3.0)
    assert cheap_first.success_probability == pytest.approx(0.85)
    assert result.chosen_sequence == ("cheap", "strong")


def test_optimizer_chooses_strong_first_when_expected_cost_is_lower() -> None:
    result = optimize_sequence(
        [_input("cheap", 0.1, 2.0), _input("strong", 0.8, 3.0)],
        max_attempts=2,
        target_success_probability=0.80,
    )
    assert result.chosen_sequence == ("strong",)


@pytest.mark.parametrize(
    ("inputs", "missing"),
    [
        ([_input("unknown", 0.5, None)], "unknown:mean_actual_attempt_cost"),
        (
            [_input("sparse", None, 1.0, sufficient=False)],
            "sparse:insufficient_probability_data",
        ),
    ],
)
def test_missing_optimizer_inputs_force_bootstrap_fallback(
    inputs: list[EmpiricalBackendInput], missing: str
) -> None:
    result = optimize_sequence(inputs, max_attempts=3)
    assert result.optimizer_status == "bootstrap_fallback"
    assert result.fallback_policy_version == "bootstrap_v1"
    assert missing in result.missing_inputs


def test_max_attempts_and_cost_budget_constrain_sequences() -> None:
    inputs = [_input("a", 0.5, 2.0), _input("b", 0.5, 2.0)]
    one = optimize_sequence(inputs, max_attempts=1, target_success_probability=0.8)
    assert all(len(sequence.backends) <= 1 for sequence in one.considered_sequences)
    budgeted = optimize_sequence(
        inputs,
        max_attempts=2,
        known_cost_budget=3.0,
        target_success_probability=0.8,
    )
    assert all(
        sequence.worst_case_cost <= 3.0 for sequence in budgeted.considered_sequences
    )
    assert budgeted.rejected_by_cost_budget == 2


def test_more_than_eight_backends_are_pruned_deterministically() -> None:
    result = optimize_sequence(
        [_input(f"backend_{index}", 0.5, float(index + 1)) for index in range(10)],
        max_attempts=1,
        target_success_probability=0.8,
    )
    assert result.pruning_rule == (
        "pruned_to_8_lowest_conservative_cost_to_success_then_backend_name"
    )
    assert result.pruned_backends == ("backend_8", "backend_9")
    assert result.total_enumerated_sequences == 8


def test_same_source_data_produces_same_digest_and_optimizer_decision(
    tmp_path: Path,
) -> None:
    _fixture_run(tmp_path, "run_deterministic")
    first = rebuild_snapshot(tmp_path)
    second = rebuild_snapshot(tmp_path)
    assert first.source_data_digest == second.source_data_digest
    assert first.profile_digest == second.profile_digest
    inputs = [_input("a", 0.5, 1.0), _input("b", 0.7, 2.0)]
    assert optimize_sequence(inputs, max_attempts=2) == optimize_sequence(
        inputs, max_attempts=2
    )


def test_sufficient_empirical_evidence_can_qualify_below_static_threshold() -> None:
    classification = ClassificationSnapshot(
        schema_version="villani.classification.v1",
        classification_id="classification_001",
        run_id="run_policy",
        task_id="task_policy",
        classified_at="2026-07-10T00:00:00Z",
        difficulty="easy",
        risk="low",
        category="bug_fix",
        required_capabilities=[],
        estimated_attempts_needed=1,
        needs_tests=True,
        confidence=0.9,
        reasoning_summary="fixture",
        signals={},
        metadata={"classifier_version": CLASSIFIER},
    )
    cheap = Backend(
        name="cheap",
        provider="local",
        model="cheap-model",
        roles=["coding"],
        capability_score=10,
        billing_mode="fixed",
        fixed_cost_per_attempt=0.1,
    )
    strong = Backend(
        name="strong",
        provider="local",
        model="strong-model",
        roles=["coding"],
        capability_score=90,
        billing_mode="fixed",
        fixed_cost_per_attempt=0.2,
    )
    snapshot = _snapshot(
        _profile(
            _key(backend="cheap", model="cheap-model", difficulty="easy"),
            20,
            20,
            mean_cost=0.1,
        ),
        _profile(
            _key(backend="strong", model="strong-model", difficulty="easy"),
            20,
            20,
            mean_cost=3.0,
        ),
    )
    configuration = {
        "policy": {"version": "bootstrap_v1"},
        "capabilities": {
            "minimum_empirical_samples": 20,
            "target_success_probability": 0.80,
            "classifier_version": CLASSIFIER,
            "verifier_version": VERIFIER,
            "scorer_version": SCORER,
        },
    }
    context = PolicyContext(
        run_id="run_policy",
        trace_id="trace_policy",
        state="CLASSIFIED",
        classification=classification,
        attempts=(),
        verifications=(),
        eligible_candidate_ids=(),
        budget=BudgetContext(
            remaining_attempts=2,
            remaining_cost_usd=None,
            cost_accounting_status="not_applicable",
            remaining_wall_time_ms=None,
            duration_accounting_status="not_applicable",
        ),
        policy_configuration=configuration,
    )
    decision = BootstrapPolicyEngine(
        {"cheap": cheap, "strong": strong},
        configuration,
        capability_snapshot=snapshot,
    ).decide(context)
    assert decision.chosen_backend == "cheap"
    assert decision.policy_version == "empirical_sequence_v2"
    expected_effective = float(int(100 * snapshot.profiles[0].wilson_lower_bound))
    assert {
        item.backend_name: item.capability_score
        for item in decision.considered_backends
    } == {"cheap": expected_effective, "strong": expected_effective}
    assert (
        decision.metadata["capability_scores"]["cheap"][
            "configured_capability_score"
        ]
        == 10
    )
    eligibility = decision.metadata["eligibility_by_backend"]["cheap"]
    assert eligibility["static_eligible"] is False
    assert eligibility["empirical_eligible"] is True
    assert decision.metadata["policy_path_used"] == "empirical_sequence_v2"


def _routing_classification() -> ClassificationSnapshot:
    return ClassificationSnapshot(
        schema_version="villani.classification.v1",
        classification_id="classification_effective",
        run_id="run_effective",
        task_id="task_effective",
        classified_at="2026-07-10T00:00:00Z",
        difficulty="medium",
        risk="low",
        category="bug_fix",
        required_capabilities=[],
        estimated_attempts_needed=1,
        needs_tests=True,
        confidence=0.9,
        reasoning_summary="fixture",
        signals={},
        metadata={"classifier_version": CLASSIFIER},
    )


def test_qualified_empirical_capability_uses_floor_of_wilson_bound() -> None:
    backend = Backend(
        name="fixture",
        provider="local",
        model="fixture-model",
        roles=["coding"],
        capability_score=99,
    )
    profile = _profile(_key(), 20, 20)
    resolution = resolve_effective_capability(
        backend,
        _routing_classification(),
        _snapshot(profile),
        {
            "capabilities": {
                "minimum_empirical_samples": 20,
                "classifier_version": CLASSIFIER,
                "verifier_version": VERIFIER,
                "scorer_version": SCORER,
            }
        },
    )

    assert resolution.capability_provenance == "qualified_empirical"
    assert resolution.effective_capability_score == int(
        100 * profile.wilson_lower_bound
    )
    assert resolution.empirical_sample_count == 20
    assert resolution.selected_level == "category_difficulty_risk"


def test_sparse_observation_receives_sample_size_uncertainty_penalty() -> None:
    backend = Backend(
        name="fixture",
        provider="local",
        model="fixture-model",
        roles=["coding"],
        capability_score=90,
    )
    profile = _profile(_key(), 5, 5)
    resolution = resolve_effective_capability(
        backend,
        _routing_classification(),
        _snapshot(profile),
        {
            "capabilities": {
                "minimum_empirical_samples": 20,
                "observed_uncertainty_penalty_max": 15,
                "classifier_version": CLASSIFIER,
                "verifier_version": VERIFIER,
                "scorer_version": SCORER,
            }
        },
    )

    assert resolution.capability_provenance == "observed"
    assert resolution.qualification_status == "provisional"
    assert resolution.uncertainty_penalty == 12
    assert resolution.effective_capability_score < 90
    assert resolution.empirical_sample_count == 5


@pytest.mark.parametrize(
    ("failure_code", "excluded_reason"),
    [
        ("repository_validation_environment_mismatch", "environment_mismatch"),
        ("repository_validation_executable_missing", "missing_executable"),
        ("repository_validation_policy_denied", "policy_denial"),
    ],
)
def test_validation_infrastructure_does_not_enter_capability_denominator(
    tmp_path: Path, failure_code: str, excluded_reason: str
) -> None:
    _fixture_run(
        tmp_path,
        f"run_{excluded_reason}",
        outcome="error",
        acceptance_eligible=False,
        failure_category="verification_failure",
        materialization_status=None,
        repository_validation_status="infrastructure_error",
        repository_validation_failure_code=failure_code,
    )
    snapshot = rebuild_snapshot(tmp_path)
    profile = _fine(snapshot)

    assert profile.sample_count == 0
    assert profile.excluded_outcome_counts[excluded_reason] == 1
    assert snapshot.excluded_outcome_counts[excluded_reason] == 1


def test_optimizer_persists_conservative_probability_and_profile_inputs() -> None:
    value = _input("qualified", 0.73, 2.5)
    result = optimize_sequence(
        [value], max_attempts=1, target_success_probability=0.70
    )

    assert result.optimizer_status == "empirical"
    assert result.input_backends[0].probability_source == "wilson_lower_bound"
    assert result.input_backends[0].conservative_success_probability == 0.73
    assert result.input_backends[0].effective_capability_score == 73
    assert result.input_backends[0].median_duration_ms == 1_000


def test_qualified_weak_can_route_and_retry_once_then_no_progress_escalates() -> None:
    classification = _routing_classification().model_copy(
        update={"difficulty": "hard"}
    )
    weak = Backend(
        name="weak",
        provider="local",
        model="weak-model",
        roles=["coding"],
        capability_score=80,
        billing_mode="fixed",
        fixed_cost_per_attempt=0.1,
    )
    strong = Backend(
        name="strong",
        provider="local",
        model="strong-model",
        roles=["coding"],
        capability_score=100,
        capability_score_source="explicit_override",
        billing_mode="fixed",
        fixed_cost_per_attempt=1.0,
    )
    snapshot = _snapshot(
        _profile(
            _key(backend="weak", model="weak-model", difficulty="hard"),
            30,
            30,
            mean_cost=0.1,
        )
    )
    configuration = {
        "public_policy": {
            "preset": "cheapest-acceptable",
            "selection_preference": "cheapest_acceptable",
        },
        "policy": {"version": "bootstrap_v1"},
        "capabilities": {
            "minimum_empirical_samples": 20,
            "classifier_version": CLASSIFIER,
            "verifier_version": VERIFIER,
            "scorer_version": SCORER,
        },
    }
    engine = BootstrapPolicyEngine(
        {weak.name: weak, strong.name: strong},
        configuration,
        capability_snapshot=snapshot,
    )
    budget = BudgetContext(
        remaining_attempts=3,
        remaining_cost_usd=None,
        cost_accounting_status="not_applicable",
        remaining_wall_time_ms=None,
        duration_accounting_status="not_applicable",
    )
    base = PolicyContext(
        run_id="run_empirical_retry",
        trace_id="trace_empirical_retry",
        state="CLASSIFIED",
        classification=classification,
        attempts=(),
        verifications=(),
        eligible_candidate_ids=(),
        budget=budget,
        policy_configuration=configuration,
    )
    progress = {
        "credible_progress": True,
        "progress_score": 0.8,
        "relevant_patch_present": True,
        "relevant_diff_ratio": 0.9,
        "validation_improvement_count": 1,
        "relevant_files_changed": 1,
        "irrelevant_files_changed": 0,
        "duplicate_read_ratio": 0.0,
        "repeated_failure_ratio": 0.0,
        "turns_after_last_progress": 0,
        "tokens_after_last_progress": 0,
        "reason_codes": ["relevant_tracked_patch", "validation_improved"],
        "actionable_feedback": True,
        "candidate_quality_status": "eligible",
    }
    credible_attempt = AttemptSummary(
        attempt_id="attempt_001",
        backend_name="weak",
        exit_code=1,
        status="completed",
        cost_usd=0.1,
        cost_accounting_status="complete",
        failure_category="implementation_failure",
        material_progress=True,
        progress_assessment=progress,
    )
    actionable = VerificationSummary(
        attempt_id="attempt_001",
        outcome="rejected",
        acceptance_eligible=False,
        recommended_action="reject",
        failure_category="implementation_failure",
        actionable_correction=True,
    )
    no_progress = AttemptSummary(
        attempt_id="attempt_001",
        backend_name="weak",
        exit_code=1,
        status="completed",
        cost_usd=0.1,
        cost_accounting_status="complete",
        failure_category="implementation_failure",
        progress_assessment={
            **progress,
            "credible_progress": False,
            "progress_score": 0.0,
            "relevant_patch_present": False,
            "relevant_diff_ratio": 0.0,
            "validation_improvement_count": 0,
            "relevant_files_changed": 0,
            "reason_codes": ["no_credible_progress_signal"],
            "actionable_feedback": False,
            "candidate_quality_status": "ineligible",
            "candidate_empty": True,
        },
    )

    assert engine.decide(base).chosen_backend == "weak"
    retry = engine.decide(
        replace(
            base,
            state="REJECTED",
            attempts=(credible_attempt,),
            verifications=(actionable,),
        )
    )
    escalated = engine.decide(
        replace(
            base,
            state="REJECTED",
            attempts=(no_progress,),
            verifications=(),
        )
    )

    assert retry.action == "retry"
    assert retry.chosen_backend == "weak"
    assert escalated.action == "escalate"
    assert escalated.chosen_backend == "strong"
