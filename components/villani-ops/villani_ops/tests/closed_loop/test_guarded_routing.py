from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import villani_ops.cli.unified as unified

from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.guarded_routing import (
    GuardedTaskRouter,
    resolve_routing_configuration,
)
from villani_ops.closed_loop.interfaces import (
    AttemptSummary,
    BackendOption,
    BudgetContext,
    Classification,
    ClosedLoopRunRequest,
    PolicyDecision,
    VerificationSummary,
)
from villani_ops.tests.closed_loop.fakes import (
    PATCH_ONE,
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
)


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _option(name: str, *, eligible: bool = True, cost: float = 1.0) -> BackendOption:
    return BackendOption(
        backend_name=name,
        model=f"{name}-model",
        eligible=eligible,
        capability_score=90,
        estimated_cost_usd=cost,
        cost_accounting_status="complete",
        rejection_reasons=() if eligible else ("not eligible",),
    )


def _bootstrap(action: str = "attempt") -> PolicyDecision:
    options = (_option("bootstrap"), _option("unsafe"), _option("safe"))
    return PolicyDecision(
        action=action,
        reason="bootstrap",
        considered_backends=options,
        chosen_backend="bootstrap"
        if action in {"attempt", "retry", "escalate"}
        else None,
        chosen_model="bootstrap-model"
        if action in {"attempt", "retry", "escalate"}
        else None,
        policy_version="bootstrap_v1",
    )


def _budget(
    *, attempts: int = 0, actual_cost: float = 0.0, stage_attempts: int = 0
) -> BudgetContext:
    return BudgetContext(
        remaining_attempts=3 - attempts,
        remaining_cost_usd=10 - actual_cost,
        cost_accounting_status="complete",
        remaining_wall_time_ms=10_000,
        duration_accounting_status="complete",
        actual_attempts_used=attempts,
        actual_cost_consumed_usd=actual_cost,
        actual_cost_accounting_status="complete",
        actual_wall_time_ms=100,
        actual_stage_attempts_used=stage_attempts,
    )


def _routing(mode: str) -> dict:
    return {
        "routing": {
            "mode": mode,
            "permissions": {"user_enforce": True, "workspace_enforce": True},
            "emergency_fallback": {
                "backend_name": "bootstrap",
                "model": "bootstrap-model",
            },
            "constraints": {
                "security_sensitive": True,
                "residency": "au",
                "maximum_cost_usd": 2,
            },
            "active_policy": {
                "state": "active",
                "version": "guarded_v1",
                "rules": {
                    "alternatives": [
                        {
                            "backend_name": "unsafe",
                            "model": "unsafe-model",
                            "agent_adapter": "codex",
                            "execution_provider": "container",
                            "maximum_attempts": 2,
                            "candidate_strategy": "deterministic_evidence_v1",
                            "verifier_graph_version": "villani_ops_verifier_pipeline_v1",
                            "escalation_sequence": ["unsafe", "safe"],
                            "estimated_cost_usd": 0.1,
                            "expected_success": 0.99,
                            "expected_latency_ms": 100,
                            "uncertainty": 0.1,
                            "security_approved": False,
                            "residencies": ["au"],
                        },
                        {
                            "backend_name": "safe",
                            "model": "safe-model",
                            "agent_adapter": "codex",
                            "execution_provider": "container",
                            "maximum_attempts": 2,
                            "candidate_strategy": "deterministic_evidence_v1",
                            "verifier_graph_version": "villani_ops_verifier_pipeline_v1",
                            "escalation_sequence": ["safe"],
                            "estimated_cost_usd": 1.0,
                            "expected_success": 0.8,
                            "expected_latency_ms": 500,
                            "uncertainty": 0.2,
                            "security_approved": True,
                            "residencies": ["au"],
                        },
                    ]
                },
            },
            "experiment_assignment": {
                "experiment_id": "exp",
                "arm": "enforce",
                "propensity": 0.1,
            },
        }
    }


@pytest.mark.parametrize("mode", ["observe", "recommend"])
def test_observe_and_recommend_cannot_change_execution(mode: str) -> None:
    bootstrap = _bootstrap()
    final, record = GuardedTaskRouter(_routing(mode)).evaluate(
        run_id="run",
        sequence=1,
        bootstrap=bootstrap,
        attempts=(),
        verifications=(),
        budget=_budget(),
        timestamp=NOW,
        experiment_assignment={"experiment_id": "exp", "propensity": 0.1},
    )
    assert final == bootstrap
    assert record.recommended_route.backend_name == "safe"
    assert record.execution_route.backend_name == "bootstrap"
    assert record.controls_execution is False


def test_existing_installations_default_to_observe() -> None:
    final, record = GuardedTaskRouter({}).evaluate(
        run_id="run",
        sequence=1,
        bootstrap=_bootstrap(),
        attempts=(),
        verifications=(),
        budget=_budget(),
        timestamp=NOW,
    )
    assert record.mode == "observe"
    assert final == _bootstrap()


def test_enforce_selects_only_eligible_complete_task_route_and_is_reproducible() -> (
    None
):
    router = GuardedTaskRouter(_routing("enforce"))
    arguments = dict(
        run_id="run",
        sequence=1,
        bootstrap=_bootstrap(),
        attempts=(),
        verifications=(),
        budget=_budget(),
        timestamp=NOW,
        experiment_assignment={
            "experiment_id": "exp",
            "arm": "enforce",
            "propensity": 0.1,
        },
    )
    final, first = router.evaluate(**arguments)
    _, second = router.evaluate(**arguments)
    assert final.chosen_backend == "safe"
    assert first == second
    assert first.execution_route == first.recommended_route
    assert first.execution_route.model_dump() == {
        "agent_adapter": "codex",
        "backend_name": "safe",
        "model": "safe-model",
        "execution_provider": "container",
        "maximum_attempts": 2,
        "candidate_strategy": "deterministic_evidence_v1",
        "verifier_graph_version": "villani_ops_verifier_pipeline_v1",
        "escalation_sequence": ("safe",),
    }
    assert (
        next(
            item for item in first.alternatives if item.route.backend_name == "unsafe"
        ).eligible
        is False
    )
    assert first.experiment_assignment["propensity"] == 0.1


def test_circuit_breaker_emergency_disable_cap_and_marginal_value_stop_paid_attempt() -> (
    None
):
    config = _routing("enforce")
    config["routing"]["circuit_breakers"] = {
        "minimum_samples": 1,
        "provider_failure_rate": 0.5,
    }
    failed = AttemptSummary(
        attempt_id="attempt_001",
        backend_name="safe",
        exit_code=1,
        status="failed",
        cost_usd=1,
        cost_accounting_status="complete",
        failure_category="infrastructure_failure",
        duration_ms=100,
    )
    final, record = GuardedTaskRouter(config).evaluate(
        run_id="run",
        sequence=2,
        bootstrap=_bootstrap("retry"),
        attempts=(failed,),
        verifications=(),
        budget=_budget(attempts=1, actual_cost=1),
        timestamp=NOW,
    )
    assert final.action == "exhaust"
    assert "provider_failure_rate" in record.circuit_breakers.reasons
    config["routing"]["emergency_disabled"] = True
    emergency, emergency_record = GuardedTaskRouter(config).evaluate(
        run_id="run",
        sequence=2,
        bootstrap=_bootstrap("retry"),
        attempts=(),
        verifications=(),
        budget=_budget(),
        timestamp=NOW,
    )
    assert emergency.action == "exhaust"
    assert "emergency_global_disable" in emergency_record.circuit_breakers.reasons
    marginal_config = _routing("enforce")
    marginal_config["routing"]["value_of_success_usd"] = 1
    marginal_config["routing"]["minimum_marginal_value_usd"] = 0
    marginal, marginal_record = GuardedTaskRouter(marginal_config).evaluate(
        run_id="run",
        sequence=2,
        bootstrap=_bootstrap("escalate"),
        attempts=(failed,),
        verifications=(),
        budget=_budget(attempts=1, actual_cost=1),
        timestamp=NOW,
    )
    assert marginal.action == "exhaust"
    assert marginal_record.expected_marginal_value_usd == pytest.approx(-0.36)
    capped, capped_record = GuardedTaskRouter(_routing("enforce")).evaluate(
        run_id="run",
        sequence=2,
        bootstrap=_bootstrap("retry"),
        attempts=(),
        verifications=(),
        budget=_budget(stage_attempts=2),
        timestamp=NOW,
    )
    assert capped.action == "exhaust"
    assert "maximum-attempt cap" in capped_record.final_reason


@pytest.mark.parametrize(
    (
        "breaker",
        "attempt_value",
        "verification_value",
        "actual_cost",
        "expected_reason",
    ),
    [
        ({"latency_ms": 50}, {"duration_ms": 100}, None, 1, "latency"),
        ({"rate_limit_count": 1}, {"rate_limited": True}, None, 1, "rate_limits"),
        (
            {"verifier_disagreement_count": 1},
            {},
            {"disagreement": True},
            1,
            "verifier_disagreement",
        ),
        ({"budget_anomaly_factor": 2}, {}, None, 5, "budget_anomaly"),
    ],
)
def test_each_circuit_breaker_opens_before_next_attempt(
    breaker: dict,
    attempt_value: dict,
    verification_value: dict | None,
    actual_cost: float,
    expected_reason: str,
) -> None:
    config = _routing("enforce")
    config["routing"]["circuit_breakers"] = breaker
    attempt_summary = AttemptSummary(
        attempt_id="attempt_001",
        backend_name="safe",
        exit_code=0,
        status="completed",
        cost_usd=actual_cost,
        cost_accounting_status="complete",
        **attempt_value,
    )
    verifications = (
        (
            VerificationSummary(
                attempt_id="attempt_001",
                outcome="rejected",
                acceptance_eligible=False,
                recommended_action="escalate",
                **verification_value,
            ),
        )
        if verification_value
        else ()
    )
    final, record = GuardedTaskRouter(config).evaluate(
        run_id="run",
        sequence=2,
        bootstrap=_bootstrap("retry"),
        attempts=(attempt_summary,),
        verifications=verifications,
        budget=_budget(attempts=1, actual_cost=actual_cost),
        timestamp=NOW,
    )
    assert final.action == "exhaust"
    assert expected_reason in record.circuit_breakers.reasons


def test_fallback_order_fail_closed_and_scope_precedence() -> None:
    config = _routing("enforce")
    config["routing"]["active_policy"]["state"] = "paused"
    config["routing"]["last_known_good_policy"] = {
        **config["routing"]["active_policy"],
        "state": "active",
        "version": "lkg_v1",
    }
    _, lkg = GuardedTaskRouter(config).evaluate(
        run_id="run",
        sequence=1,
        bootstrap=_bootstrap(),
        attempts=(),
        verifications=(),
        budget=_budget(),
        timestamp=NOW,
    )
    assert lkg.policy_source == "last_known_good_policy"
    config["routing"]["last_known_good_policy"]["state"] = "paused"
    _, bootstrap = GuardedTaskRouter(config).evaluate(
        run_id="run",
        sequence=1,
        bootstrap=_bootstrap(),
        attempts=(),
        verifications=(),
        budget=_budget(),
        timestamp=NOW,
    )
    assert bootstrap.policy_source == "fail_closed"
    resolved, precedence = resolve_routing_configuration(
        {
            "routing": {
                "mode": "observe",
                "identity": {"workspace_id": "w", "repository_id": "r"},
                "scopes": {
                    "organization": {"constraints": {"maximum_cost_usd": 5}},
                    "workspace": {"scope_id": "w", "mode": "recommend"},
                    "project": {"scope_id": "wrong", "mode": "enforce"},
                    "repository": {
                        "scope_id": "r",
                        "constraints": {"maximum_cost_usd": 2},
                    },
                },
            }
        }
    )
    assert resolved["mode"] == "recommend"
    assert resolved["constraints"]["maximum_cost_usd"] == 2
    assert precedence == (
        "installation_default",
        "organization",
        "workspace",
        "repository",
    )


def test_policy_explain_reports_resolved_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = {
        "routing": {
            "mode": "observe",
            "identity": {"workspace_id": "w", "repository_id": "r"},
            "scopes": {
                "workspace": {"scope_id": "w", "mode": "recommend"},
                "repository": {
                    "scope_id": "r",
                    "constraints": {"maximum_cost_usd": 1},
                },
            },
        }
    }
    monkeypatch.setattr(unified, "_load_config", lambda: configuration)
    result = CliRunner().invoke(unified.app, ["policy", "explain", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["mode"] == "recommend"
    assert document["applied_precedence"] == [
        "installation_default",
        "workspace",
        "repository",
    ]
    assert document["step_level_routing"] is False


def test_controller_persists_guarded_input_and_enforces_backend(tmp_path: Path) -> None:
    target = tmp_path / "repository"
    target.mkdir()
    (target / "example.txt").write_text("old\n", encoding="utf-8")
    bootstrap = _bootstrap()
    policy = FakePolicyEngine(
        [
            bootstrap,
            PolicyDecision(
                action="select",
                reason="select",
                considered_backends=bootstrap.considered_backends,
            ),
        ]
    )
    runner = FakeAttemptRunner([attempt(patch=PATCH_ONE)])
    controller = ClosedLoopController(
        classifier=FakeClassifier(
            Classification(difficulty="easy", risk="low", category="test", confidence=1)
        ),
        policy_engine=policy,
        attempt_runner=runner,
        verifier=FakeVerifier([accepted_verification()]),
        selector=FakeSelector(),
        materializer=FakeMaterializer(),
        now=FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
    )
    result = controller.run(
        ClosedLoopRunRequest(
            task="change",
            repository_path=target,
            success_criteria="passes",
            runs_root=tmp_path / "runs",
            max_attempts=3,
            policy_configuration={"version": "fake_v1", **_routing("enforce")},
        )
    )
    assert result.terminal_state == "COMPLETED"
    assert runner.calls[0].backend_name == "safe"
    assert runner.calls[0].execution_provider == "container"
    records = [
        json.loads(line)
        for line in (result.run_directory / "guarded_routing_decisions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert records[0]["controls_execution"] is True
    assert records[0]["input_digest_sha256"]
    assert records[0]["policy_version"] == "guarded_v1"
