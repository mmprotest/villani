from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from villani_ops.cli.unified import DEFAULT_CONFIG, app
from villani_ops.closed_loop.economics import (
    DurationEstimate,
    HistoricalRouteCase,
    MoneyEstimate,
    RouteCandidateInput,
    RouteConstraints,
    RoutePolicy,
    RoutePolicyStore,
    EconomicsStore,
    calculate_objective,
    evaluate_route_policy,
    plan_route,
    record_finalized_outcome,
)
from villani_ops.closed_loop.economics.models import HistoricalSystemOutcome
from villani_ops.closed_loop.economics.runtime_update import record_runtime_economics
from villani_ops.closed_loop.qualification import QualificationStore, task_profile
from villani_ops.tests.test_pt7_repository_qualification import _identity, _observation


NOW = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)
SYSTEM_A = "asys_" + "a" * 64
SYSTEM_B = "asys_" + "b" * 64


def _money(
    amount: float | None,
    *,
    status: str = "complete",
    source: str = "fixture",
) -> MoneyEstimate:
    return MoneyEstimate(
        amount=amount,
        currency="USD" if amount is not None else None,
        accounting_status=status,  # type: ignore[arg-type]
        source=source,
        sample_count=1 if amount is not None else 0,
    )


def _duration(
    value: float | None,
    *,
    status: str = "complete",
) -> DurationEstimate:
    return DurationEstimate(
        duration_ms=value,
        accounting_status=status,  # type: ignore[arg-type]
        source="fixture",
        sample_count=1 if value is not None else 0,
    )


def _candidate(
    route: str,
    *,
    probability: float | None,
    execution: float | None,
    qualification: str = "qualified",
    capability: float = 80,
    local: bool = True,
    provider: str = "local",
    review: float | None = 1.0,
    verification: float | None = 1.0,
    retry: float | None = 1.0,
    duration: float | None = 1_000,
    reserve: bool = True,
    false_acceptance_count: int = 0,
) -> RouteCandidateInput:
    system_id = SYSTEM_A if route == "a" else SYSTEM_B
    return RouteCandidateInput(
        backend_name=route,
        route_name=route,
        system_id=system_id,
        harness=f"{route}-harness",
        model=f"{route}-model",
        provider=provider,
        local=local,
        permission_profile="workspace-write",
        availability="available",
        qualification_state=qualification,  # type: ignore[arg-type]
        qualification_level="exact_repository_task",
        qualification_policy_version="repository_qualification_v1",
        qualification_sample_count=20 if qualification == "qualified" else 1,
        conservative_acceptance_probability=probability,
        task_probability_threshold=0.8,
        false_acceptance_count=false_acceptance_count,
        drift_flags=[],
        capability_score=capability,
        execution_cost=(
            _money(execution)
            if execution is not None
            else _money(None, status="unknown")
        ),
        verification_cost=(
            _money(verification)
            if verification is not None
            else _money(None, status="unknown")
        ),
        human_review_cost=(
            _money(review) if review is not None else _money(None, status="unknown")
        ),
        retry_escalation_cost=(
            _money(retry) if retry is not None else _money(None, status="unknown")
        ),
        duration=(
            _duration(duration)
            if duration is not None
            else _duration(None, status="unknown")
        ),
        latency_penalty=_money(None, status="not_applicable"),
        reserve_satisfied=reserve,
        reserve_evidence={"verification": 1, "final_validation": 1},
        input_rejection_reasons=[],
    )


def _plan(
    candidates: list[RouteCandidateInput],
    *,
    policy: RoutePolicy | None = None,
    constraints: RouteConstraints | None = None,
):
    return plan_route(
        run_id="run-pt8",
        repository_id="repo-pt8",
        repository_head="1" * 40,
        task_profile=task_profile("maintenance", "easy", "low"),
        candidates=candidates,
        policy=policy or RoutePolicy(),
        constraints=constraints,
        evidence_cutoff=NOW,
        reserves={"verification": 1, "final_validation": 1},
    )


def test_full_and_partial_total_accepted_change_objective() -> None:
    policy = RoutePolicy()
    full = calculate_objective(
        _candidate("a", probability=0.5, execution=2.0), policy=policy
    )
    assert full.accounting_status == "complete"
    assert full.known_numerator_cost == 5.0
    assert full.expected_accepted_change_cost == 10.0
    assert full.partial_expected_known_cost is None

    partial = calculate_objective(
        _candidate(
            "a",
            probability=0.5,
            execution=2.0,
            review=None,
        ),
        policy=policy,
    )
    assert partial.accounting_status == "partial"
    assert partial.known_numerator_cost == 4.0
    assert partial.expected_accepted_change_cost is None
    assert partial.partial_expected_known_cost == 8.0
    assert "human_review_cost" in partial.unknown_components


def test_optimizer_uses_conservative_probability_and_is_deterministic() -> None:
    cheap_unreliable = _candidate("a", probability=0.5, execution=1.0)
    dear_reliable = _candidate("b", probability=0.9, execution=3.0)
    first = _plan([dear_reliable, cheap_unreliable])
    second = _plan([cheap_unreliable, dear_reliable])

    assert first.selection_mode == "accepted_change_optimizer"
    assert first.selected_first_system == "b"
    assert first.plan_id == second.plan_id
    assert first.input_digest == second.input_digest
    assert first.explanation.startswith(
        "Villani chose the route most likely to produce a proven change"
    )


def test_sparse_inputs_fall_back_to_strongest_evidence() -> None:
    plan = _plan(
        [
            _candidate("a", probability=0.85, execution=1.0, capability=70),
            _candidate("b", probability=0.90, execution=None, capability=90),
        ]
    )
    assert plan.selection_mode == "sparse_strongest_evidence"
    assert plan.selected_first_system == "b"
    assert any("execution_cost" in item for item in plan.unknowns)


def test_experimental_never_auto_selects_and_provisional_is_explicit() -> None:
    experimental = _plan(
        [
            _candidate(
                "a",
                probability=None,
                execution=0.1,
                qualification="experimental",
            )
        ]
    )
    assert experimental.selected_first_system is None
    assert experimental.selection_mode == "no_safe_route"

    provisional = _plan(
        [
            _candidate(
                "a",
                probability=0.5,
                execution=0.1,
                qualification="provisional",
            ),
            _candidate(
                "b",
                probability=0.6,
                execution=0.2,
                qualification="provisional",
            ),
        ]
    )
    assert provisional.selection_mode == "provisional_fallback"
    assert provisional.selected_first_system == "b"


def test_privacy_provider_cost_permission_and_reserve_constraints_fail_closed() -> None:
    remote = _candidate(
        "a",
        probability=0.9,
        execution=1.0,
        local=False,
        provider="remote",
        reserve=False,
    )
    plan = _plan(
        [remote],
        constraints=RouteConstraints(
            local_only=True,
            allowed_providers=["local"],
            allowed_permission_profiles=["read-only"],
            maximum_known_cost_usd=0.5,
        ),
    )
    reasons = plan.systems_considered[0].rejection_reasons
    assert "local-only privacy constraint" in reasons
    assert "provider is outside the allowed provider set" in reasons
    assert "permission profile is not allowed" in reasons
    assert "known route cost exceeds the configured maximum" in reasons
    assert "required downstream reserves are not satisfied" in reasons
    assert plan.selected_first_system is None


def test_local_first_prefers_local_economics_without_hiding_remote_fallbacks() -> None:
    local = _candidate("a", probability=0.85, execution=4.0, local=True)
    remote = _candidate(
        "b",
        probability=0.90,
        execution=1.0,
        local=False,
        provider="remote",
        capability=95,
    )
    plan = _plan(
        [local, remote],
        constraints=RouteConstraints(prefer_local=True),
    )
    assert plan.selected_first_system == "a"
    assert plan.ordered_fallbacks == ["b"]
    assert "Local first" in plan.explanation


def test_forced_choice_is_labeled_and_excluded_from_automatic_metrics() -> None:
    constraints = RouteConstraints(
        forced_system="a",
        allow_experimental_forced=True,
    )
    plan = _plan(
        [
            _candidate(
                "a",
                probability=None,
                execution=None,
                qualification="experimental",
            )
        ],
        constraints=constraints,
    )
    assert plan.selection_mode == "forced"
    assert plan.forced_choice is True
    assert plan.automatic_policy_metrics_eligible is False
    assert "forced system" in plan.explanation


def test_false_acceptance_quarantines_route_immediately() -> None:
    plan = _plan(
        [
            _candidate(
                "a",
                probability=0.99,
                execution=0.01,
                false_acceptance_count=1,
            ),
            _candidate("b", probability=0.85, execution=2.0),
        ]
    )
    assert plan.selected_first_system == "b"
    rejected = next(item for item in plan.systems_considered if item.route_name == "a")
    assert (
        "known false acceptance quarantines this profile" in rejected.rejection_reasons
    )


def test_online_update_trains_future_profiles_only_and_false_acceptance_invalidates(
    tmp_path: Path,
) -> None:
    identity = _identity("a")
    candidate = _candidate("a", probability=0.9, execution=1.0).model_copy(
        update={"system_id": identity.system_id}
    )
    route_plan = plan_route(
        run_id="online-run",
        repository_id="repo-pt8",
        repository_head="1" * 40,
        task_profile=task_profile("maintenance", "easy", "low"),
        candidates=[candidate],
        policy=RoutePolicy(),
        evidence_cutoff=NOW,
    )
    qualification = _observation(
        identity,
        "repo-pt8",
        "1" * 40,
        1,
        cost=4.0,
        trial_id="online-run",
    ).model_copy(update={"source_kind": "canonical_run"})
    qualification_store = QualificationStore(tmp_path / "qualification")
    economics_store = EconomicsStore(tmp_path / "economics")
    result = record_finalized_outcome(
        qualification_observation=qualification,
        route_plan=route_plan,
        execution_cost=_money(1.0),
        verification_cost=_money(1.0),
        human_review_cost=_money(1.0),
        retry_escalation_cost=_money(1.0),
        duration=_duration(1_000),
        attempt_count=1,
        escalation_count=0,
        review_minutes=2.5,
        qualification_store=qualification_store,
        economics_store=economics_store,
        recorded_at=NOW,
    )
    assert result.eligible_for_profile is True
    assert economics_store.load_snapshot().profiles[0].sample_count == 1  # type: ignore[union-attr]

    false_case = _observation(
        identity,
        "repo-pt8",
        "1" * 40,
        2,
        false_acceptance=True,
        trial_id="online-run",
        recorded_at=NOW + timedelta(seconds=1),
    ).model_copy(update={"source_kind": "canonical_run"})
    false_result = record_finalized_outcome(
        qualification_observation=false_case,
        route_plan=route_plan,
        execution_cost=_money(1.0),
        verification_cost=_money(1.0),
        human_review_cost=_money(1.0),
        retry_escalation_cost=_money(1.0),
        duration=_duration(1_000),
        attempt_count=1,
        escalation_count=0,
        review_minutes=2.5,
        qualification_store=qualification_store,
        economics_store=economics_store,
        recorded_at=NOW + timedelta(seconds=1),
    )
    assert false_result.eligible_for_profile is False
    assert qualification_store.load_invalidations()[0].reason == "false_acceptance"
    snapshot = economics_store.load_snapshot()
    assert snapshot is not None
    assert snapshot.profiles[0].sample_count == 1
    assert snapshot.profiles[0].false_acceptance_count == 1


def _historical_case(*, cutoff_after_decision: bool = False) -> HistoricalRouteCase:
    a = _candidate("a", probability=0.90, execution=3.0, capability=95)
    b = _candidate("b", probability=0.90, execution=1.0, capability=80)
    cutoff = NOW + timedelta(seconds=1) if cutoff_after_decision else NOW
    return HistoricalRouteCase(
        case_id="case-pt8",
        decision_at=NOW,
        repository_id="repo-pt8",
        repository_head="1" * 40,
        task_profile=task_profile("maintenance", "easy", "low"),
        candidates=[a, b],
        candidate_evidence_cutoffs={"a": NOW, "b": cutoff},
        outcomes=[
            HistoricalSystemOutcome(
                route_name=route,
                accepted_as_is=True,
                proved_acceptable=True,
                false_acceptance=False,
                eligible=True,
                total_cost=_money(cost),
                duration=_duration(1_000),
                review_minutes=1,
                escalation_count=0,
            )
            for route, cost in (("a", 6.0), ("b", 4.0))
        ],
    )


def test_point_in_time_replay_blocks_future_evidence_and_empty_replay_fails_closed() -> (
    None
):
    active = RoutePolicy(policy_version="active-v1", strategy="strongest_only")
    proposed = RoutePolicy(policy_version="proposed-v1")
    evaluation = evaluate_route_policy(
        [_historical_case(cutoff_after_decision=True)],
        active_policy=active,
        proposed_policy=proposed,
        generated_at=NOW,
    )
    assert evaluation.point_in_time_replay is True
    assert evaluation.safe_to_publish is True
    assert evaluation.comparisons[0].active_choice == "a"
    assert evaluation.comparisons[0].proposed_choice == "a"

    empty = evaluate_route_policy(
        [], active_policy=active, proposed_policy=proposed, generated_at=NOW
    )
    assert empty.safe_to_publish is False
    assert "no frozen historical cases" in empty.rejection_reasons


def test_safe_policy_publication_and_instant_rollback(tmp_path: Path) -> None:
    case = _historical_case()
    built_in = RoutePolicy(policy_version="built-in", strategy="strongest_only")
    first = RoutePolicy(policy_version="policy-one", strategy="strongest_only")
    first_eval = evaluate_route_policy(
        [case], active_policy=built_in, proposed_policy=first, generated_at=NOW
    )
    assert first_eval.safe_to_publish is True
    store = RoutePolicyStore(tmp_path / "policies")
    first_publication = store.publish(first, first_eval, published_at=NOW)

    second = RoutePolicy(policy_version="policy-two", strategy="strongest_only")
    second_eval = evaluate_route_policy(
        [case], active_policy=first, proposed_policy=second, generated_at=NOW
    )
    store.publish(second, second_eval, published_at=NOW + timedelta(seconds=1))
    assert store.active_policy(built_in).policy_version == "policy-two"
    rolled_back = store.rollback(rolled_back_at=NOW + timedelta(seconds=2))
    assert rolled_back.publication_id == first_publication.publication_id
    assert store.active_policy(built_in).policy_version == "policy-one"


def test_policy_evaluation_reports_all_required_strategy_scorecards() -> None:
    active = RoutePolicy(policy_version="active", strategy="strongest_only")
    proposed = RoutePolicy(policy_version="proposed")
    evaluation = evaluate_route_policy(
        [_historical_case()],
        active_policy=active,
        proposed_policy=proposed,
        generated_at=NOW,
    )
    assert {item.strategy for item in evaluation.strategy_metrics} == {
        "strongest_only",
        "cheapest_qualified",
        "accepted_change_optimizer",
        "forced",
    }
    optimized = next(
        item
        for item in evaluation.strategy_metrics
        if item.strategy == "accepted_change_optimizer"
    )
    assert optimized.accepted_as_is == 1
    assert optimized.total_cost.amount == 4.0
    assert optimized.review_minutes == 1
    assert optimized.unknown_input_rate == 0


def test_route_contract_has_no_task_name_or_future_result_input() -> None:
    fields = set(RouteCandidateInput.model_fields)
    assert "task_name" not in fields
    assert "benchmark_id" not in fields
    assert "future_result" not in fields
    document = _plan([_candidate("a", probability=0.9, execution=1.0)]).model_dump(
        mode="json"
    )
    assert "task_name" not in json.dumps(document)


def test_legacy_configuration_does_not_gain_online_update_side_effects() -> None:
    runtime = SimpleNamespace(request=SimpleNamespace(policy_configuration={}))
    assert (
        record_runtime_economics(
            runtime=runtime,
            identity_documents=[],
            qualification_store=None,
            economics_store=None,
            recorded_at=NOW,
        )
        is None
    )


def test_policy_cli_exposes_read_only_explain_and_safe_lifecycle() -> None:
    result = CliRunner().invoke(app, ["policy", "--help"])
    assert result.exit_code == 0, result.output
    for command in (
        "explain",
        "economics-evaluate",
        "economics-status",
        "economics-publish",
        "economics-rollback",
    ):
        assert command in result.output


def test_policy_cli_evaluates_publishes_and_rolls_back_without_starting_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "villani-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    monkeypatch.setenv("VILLANI_HOME", str(home))
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(
        json.dumps(
            {"cases": [_historical_case().model_dump(mode="json")]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    first_policy = RoutePolicy(
        policy_version="cli-policy-one", strategy="strongest_only"
    )
    first_policy_path = tmp_path / "policy-one.json"
    first_policy_path.write_text(
        first_policy.model_dump_json(indent=2), encoding="utf-8"
    )
    first_evaluation_path = tmp_path / "evaluation-one.json"
    first_evaluation = runner.invoke(
        app,
        [
            "policy",
            "economics-evaluate",
            "--cases",
            str(cases_path),
            "--proposed-policy",
            str(first_policy_path),
            "--output",
            str(first_evaluation_path),
            "--json",
        ],
    )
    assert first_evaluation.exit_code == 0, first_evaluation.output
    assert json.loads(first_evaluation.output)["safe_to_publish"] is True
    first_publish = runner.invoke(
        app,
        [
            "policy",
            "economics-publish",
            "--policy",
            str(first_policy_path),
            "--evaluation",
            str(first_evaluation_path),
            "--json",
        ],
    )
    assert first_publish.exit_code == 0, first_publish.output

    second_policy = RoutePolicy(policy_version="cli-policy-two")
    second_policy_path = tmp_path / "policy-two.json"
    second_policy_path.write_text(
        second_policy.model_dump_json(indent=2), encoding="utf-8"
    )
    second_evaluation_path = tmp_path / "evaluation-two.json"
    second_evaluation = runner.invoke(
        app,
        [
            "policy",
            "economics-evaluate",
            "--cases",
            str(cases_path),
            "--proposed-policy",
            str(second_policy_path),
            "--output",
            str(second_evaluation_path),
            "--json",
        ],
    )
    assert second_evaluation.exit_code == 0, second_evaluation.output
    second_publish = runner.invoke(
        app,
        [
            "policy",
            "economics-publish",
            "--policy",
            str(second_policy_path),
            "--evaluation",
            str(second_evaluation_path),
            "--json",
        ],
    )
    assert second_publish.exit_code == 0, second_publish.output

    status = runner.invoke(app, ["policy", "economics-status", "--json"])
    assert status.exit_code == 0, status.output
    assert (
        json.loads(status.output)["active_policy"]["policy_version"] == "cli-policy-two"
    )
    rollback = runner.invoke(app, ["policy", "economics-rollback", "--json"])
    assert rollback.exit_code == 0, rollback.output
    assert json.loads(rollback.output)["policy"]["policy_version"] == "cli-policy-one"
    assert not (home / "runs").exists()
