from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def evaluator():
    name = "live_evaluation_decision_tests"
    spec = importlib.util.spec_from_file_location(name, ROOT / "evaluation/live_evaluation.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def rows(
    *,
    count: int = 30,
    adaptive_cost: float | None = 1.0,
    adaptive_successes: int | None = None,
    adaptive_false_acceptances: int = 0,
):
    module = evaluator()
    result = []
    revision = "a" * 40
    for policy in module.POLICIES:
        for index in range(count):
            success = True
            if policy == "adaptive" and adaptive_successes is not None:
                success = index < adaptive_successes
            model_cost = 2.0 if policy == "strong-only" else 1.2
            if policy == "adaptive":
                model_cost = adaptive_cost
            result.append(
                module.LiveObservation(
                    policy=policy,
                    task_id=f"task-{index:03d}",
                    run_id=f"run-{policy}-{index}",
                    verified_success=success,
                    false_acceptance=(
                        policy == "adaptive" and index < adaptive_false_acceptances
                    ),
                    false_rejection=not success,
                    total_model_cost=model_cost,
                    verifier_cost=0.2 if model_cost is not None else None,
                    wall_time_ms=100,
                    attempts=2,
                    escalations=int(policy in {"adaptive", "cheap-first-escalation"}),
                    revisions={"repository_revision": revision},
                    resolved_repository_revision=revision,
                    policy_configuration_digest=f"digest-{policy}",
                    locks={"evaluation": "v2", "verifier": "locked"},
                )
            )
    return module, result


def refusal_codes(report):
    return {reason["code"] for reason in report["savings_claim_refusal_reasons"]}


def test_sufficient_sample_but_adaptive_more_expensive_refuses_claim():
    module, observations = rows(adaptive_cost=3.0)
    report = module.aggregate(observations, 30)
    assert not report["savings_claim_supported"]
    assert "cost_improvement_point_estimate" in refusal_codes(report)


def test_success_non_inferiority_and_false_acceptance_guardrails_refuse_claim():
    module, observations = rows(adaptive_successes=20, adaptive_false_acceptances=2)
    report = module.aggregate(observations, 30, maximum_false_acceptance_rate=0.01)
    assert not report["savings_claim_supported"]
    assert {
        "success_non_inferiority_point_estimate",
        "false_acceptance_guardrail",
    } <= refusal_codes(report)


def test_unknown_cost_and_different_paired_task_sets_refuse_claim():
    module, observations = rows(adaptive_cost=None)
    observations = [
        row
        for row in observations
        if not (row.policy == "adaptive" and row.task_id == "task-000")
    ]
    report = module.aggregate(observations, 29)
    assert not report["savings_claim_supported"]
    assert {"missing_accounting", "different_paired_task_sets"} <= refusal_codes(report)


def test_all_statistical_economic_and_quality_gates_support_claim():
    module, observations = rows(adaptive_cost=1.0)
    report = module.aggregate(observations, 30)
    assert report["savings_claim_supported"]
    assert report["savings_claim_refusal_reasons"] == []
    assert report["decision_rule"] == {
        "minimum_sample_size": 30,
        "success_non_inferiority_margin": 0.05,
        "minimum_required_cost_improvement": 0.1,
        "maximum_false_acceptance_rate": 0.01,
        "confidence_level": 0.95,
        "method": "paired_task_nonparametric_bootstrap_percentile",
        "bootstrap_samples": 10_000,
        "bootstrap_seed": 20_260_712,
    }


def test_sample_size_alone_never_supports_claim_and_below_threshold_refuses():
    module, observations = rows(count=29)
    report = module.aggregate(observations, 30)
    assert not report["savings_claim_supported"]
    assert "minimum_sample_size" in refusal_codes(report)


def test_repository_revision_mismatch_refuses_claim():
    module, observations = rows()
    changed = observations[0]
    observations[0] = replace(changed, resolved_repository_revision="b" * 40)
    report = module.aggregate(observations, 30)
    assert "repository_revision_mismatch" in refusal_codes(report)


def _repository(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Villani tests"], cwd=path, check=True)
    (path / "value.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "value.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True, capture_output=True, check=True
    ).stdout.strip()


def test_policy_materialization_cannot_contaminate_another_checkout(tmp_path: Path):
    module = evaluator()
    repository = tmp_path / "repository"
    revision = _repository(repository)
    with module.isolated_checkout(repository, revision) as (first, _):
        (first / "value.txt").write_text("policy-one\n", encoding="utf-8")
    with module.isolated_checkout(repository, revision) as (second, _):
        assert (second / "value.txt").read_text(encoding="utf-8") == "base\n"
        assert subprocess.run(
            ["git", "status", "--porcelain"], cwd=second, text=True, capture_output=True, check=True
        ).stdout == ""


def test_dirty_checkout_before_execution_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = evaluator()
    repository = tmp_path / "repository"
    revision = _repository(repository)
    original = module._git

    def dirty(repo, *arguments, **kwargs):
        result = original(repo, *arguments, **kwargs)
        if arguments == ("status", "--porcelain"):
            return subprocess.CompletedProcess(result.args, 0, " M value.txt\n", "")
        return result

    monkeypatch.setattr(module, "_git", dirty)
    with pytest.raises(RuntimeError, match="dirty before execution"):
        with module.isolated_checkout(repository, revision):
            pass


def test_evaluation_does_not_mutate_production_configuration(tmp_path: Path):
    module, observations = rows()
    production = tmp_path / "config.yaml"
    production.write_bytes(b"mode: observe\n")
    before = production.read_bytes()
    module.aggregate(observations, 30)
    assert production.read_bytes() == before
