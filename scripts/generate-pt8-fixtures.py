#!/usr/bin/env python3
"""Generate canonical PT8 protocol fixtures from the production models."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "components" / "villani-ops"
sys.path.insert(0, str(OPS))

from villani_ops.closed_loop.economics.evaluation import evaluate_route_policy  # noqa: E402
from villani_ops.closed_loop.economics.models import (  # noqa: E402
    DurationEstimate,
    EconomicsObservation,
    EconomicsProfile,
    EconomicsProfileKey,
    EconomicsSnapshot,
    HistoricalRouteCase,
    HistoricalSystemOutcome,
    MoneyEstimate,
    OnlineEvidenceUpdateReport,
    RouteCandidateInput,
    RoutePolicy,
    RoutePolicyPublication,
    canonical_digest,
)
from villani_ops.closed_loop.economics.planner import plan_route  # noqa: E402
from villani_ops.closed_loop.qualification.models import (  # noqa: E402
    QualificationDistribution,
    QualificationTaskProfile,
)


NOW = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)
RUN_ID = "run_fixture_pt8"
SYSTEM_ID = "asys_" + "4" * 64
IDENTITY_DIGEST = "sha256:" + "5" * 64
QUALIFICATION_OBSERVATION_ID = "qobs_" + "6" * 64
TASK_PROFILE = QualificationTaskProfile(
    category="maintenance",
    difficulty="easy",
    risk="low",
    required_capabilities=[],
)


def money(amount: float | None, status: str = "complete") -> MoneyEstimate:
    return MoneyEstimate(
        amount=amount,
        currency="USD" if amount is not None else None,
        accounting_status=status,
        source="pt8_protocol_fixture",
        sample_count=1 if amount is not None else 0,
    )


def duration(value: float | None, status: str = "complete") -> DurationEstimate:
    return DurationEstimate(
        duration_ms=value,
        accounting_status=status,
        source="pt8_protocol_fixture",
        sample_count=1 if value is not None else 0,
    )


def distribution(value: float, unit: str) -> QualificationDistribution:
    return QualificationDistribution(
        known_count=1,
        unknown_count=0,
        minimum=value,
        median=value,
        p90=value,
        maximum=value,
        unit=unit,
    )


def candidate() -> RouteCandidateInput:
    return RouteCandidateInput(
        backend_name="fixture_economy",
        route_name="fixture_economy",
        system_id=SYSTEM_ID,
        harness="villani-code",
        model="fixture-small",
        provider="local",
        local=True,
        permission_profile="workspace-write",
        availability="available",
        qualification_state="qualified",
        qualification_level="exact_repository_task",
        qualification_policy_version="repository_qualification_v1",
        qualification_sample_count=20,
        conservative_acceptance_probability=0.84,
        task_probability_threshold=0.6,
        false_acceptance_count=0,
        drift_flags=[],
        capability_score=0.9,
        execution_cost=money(1.25),
        verification_cost=money(0.25),
        human_review_cost=money(None, "not_applicable"),
        retry_escalation_cost=money(None, "not_applicable"),
        duration=duration(1200),
        latency_penalty=money(None, "not_applicable"),
        reserve_satisfied=True,
        reserve_evidence={"verification": "preserved", "final_validation": "preserved"},
        input_rejection_reasons=[],
    )


def write(name: str, document: object) -> None:
    destination = (
        ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run" / name
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    active = RoutePolicy(policy_version="accepted-change-fixture-active", strategy="strongest_only")
    proposed = RoutePolicy(policy_version="accepted-change-fixture-v1")
    route_plan = plan_route(
        run_id=RUN_ID,
        repository_id="repo_fixture_pt8",
        repository_head="a" * 40,
        task_profile=TASK_PROFILE,
        candidates=[candidate()],
        policy=proposed,
        evidence_cutoff=NOW,
        reserves={"verification": "preserved", "final_validation": "preserved"},
    )
    observation = EconomicsObservation(
        observation_id="eobs_" + "7" * 64,
        recorded_at=NOW,
        observed_at=NOW,
        source_run_id=RUN_ID,
        source_route_plan_id=route_plan.plan_id,
        qualification_observation_id=QUALIFICATION_OBSERVATION_ID,
        repository_id="repo_fixture_pt8",
        task_profile=TASK_PROFILE,
        system_id=SYSTEM_ID,
        system_identity_digest=IDENTITY_DIGEST,
        route_name="fixture_economy",
        policy_version=proposed.policy_version,
        forced_choice=False,
        qualification_eligible=True,
        authoritative_verification_complete=True,
        infrastructure_status="resolved",
        proved_acceptable=True,
        accepted_as_is=True,
        false_acceptance=False,
        eligible_for_profile=True,
        eligible_for_automatic_policy_metrics=True,
        exclusion_reason=None,
        execution_cost=money(1.25),
        verification_cost=money(0.25),
        human_review_cost=money(None, "not_applicable"),
        retry_escalation_cost=money(None, "not_applicable"),
        duration=duration(1200),
        review_minutes=None,
        attempt_count=1,
        escalation_count=0,
    )
    component_distributions = {
        "execution_cost": {"USD": distribution(1.25, "USD")},
        "verification_cost": {"USD": distribution(0.25, "USD")},
        "human_review_cost": {},
        "retry_escalation_cost": {},
    }
    profile = EconomicsProfile(
        key=EconomicsProfileKey(
            repository_id="repo_fixture_pt8",
            task_profile=TASK_PROFILE,
            system_id=SYSTEM_ID,
            system_identity_digest=IDENTITY_DIGEST,
            route_name="fixture_economy",
        ),
        observation_ids=[observation.observation_id],
        sample_count=1,
        successes=1,
        failures=0,
        exclusions={},
        cost_distributions=component_distributions,
        cost_unknown_counts={
            "execution_cost": 0,
            "verification_cost": 0,
            "human_review_cost": 0,
            "retry_escalation_cost": 0,
        },
        duration_distribution=distribution(1200, "ms"),
        review_minutes_distribution=QualificationDistribution(
            known_count=0,
            unknown_count=1,
            minimum=None,
            median=None,
            p90=None,
            maximum=None,
            unit="minutes",
        ),
        attempt_count_distribution=distribution(1, "count"),
        escalation_count_distribution=distribution(0, "count"),
        false_acceptance_count=0,
        last_evidence_at=NOW,
        source_digest=canonical_digest(observation.model_dump(mode="json")),
    )
    snapshot_seed = {
        "generated_at": NOW.isoformat(),
        "observation_count": 1,
        "profiles": [profile.model_dump(mode="json")],
        "exclusions": {},
    }
    snapshot = EconomicsSnapshot(
        generated_at=NOW,
        source_digest=canonical_digest(observation.model_dump(mode="json")),
        snapshot_digest=canonical_digest(snapshot_seed),
        observation_count=1,
        profiles=[profile],
        exclusions={},
    )
    historical = HistoricalRouteCase(
        case_id="founder-fixture-pt8",
        decision_at=NOW,
        repository_id="repo_fixture_pt8",
        repository_head="a" * 40,
        task_profile=TASK_PROFILE,
        candidates=[candidate()],
        candidate_evidence_cutoffs={"fixture_economy": NOW},
        outcomes=[
            HistoricalSystemOutcome(
                route_name="fixture_economy",
                accepted_as_is=True,
                proved_acceptable=True,
                false_acceptance=False,
                eligible=True,
                total_cost=money(1.5),
                duration=duration(1200),
                review_minutes=0,
                escalation_count=0,
            )
        ],
    )
    evaluation = evaluate_route_policy(
        [historical],
        active_policy=active,
        proposed_policy=proposed,
        generated_at=NOW,
    )
    publication_digest_seed = canonical_digest(
        {
            "policy": proposed.model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
        }
    )
    publication = RoutePolicyPublication(
        publication_id="rpub_" + hashlib.sha256(publication_digest_seed.encode()).hexdigest(),
        published_at=NOW,
        policy=proposed,
        policy_digest=canonical_digest(proposed.model_dump(mode="json")),
        evaluation_id=evaluation.evaluation_id,
        evaluation_digest=canonical_digest(evaluation.model_dump(mode="json")),
        prior_policy_version=active.policy_version,
        state="active",
    )
    update = OnlineEvidenceUpdateReport(
        run_id=RUN_ID,
        recorded_at=NOW,
        status="recorded",
        qualification_observation_id=QUALIFICATION_OBSERVATION_ID,
        economics_observation_id=observation.observation_id,
        profile_updated=True,
        automatic_policy_metrics_eligible=True,
        reasons=[],
    )

    documents = {
        "route-policy.json": proposed,
        "route-plan.json": route_plan,
        "economics-observation.json": observation,
        "economics-snapshot.json": snapshot,
        "route-policy-evaluation.json": evaluation,
        "route-policy-publication.json": publication,
        "online-evidence-update.json": update,
    }
    for name, model in documents.items():
        write(name, model.model_dump(mode="json"))

    invalid = observation.model_dump(mode="json")
    invalid["execution_cost"] = {
        **invalid["execution_cost"],
        "amount": 0,
        "currency": "USD",
        "accounting_status": "unknown",
    }
    invalid_path = (
        ROOT
        / "integration"
        / "fixtures"
        / "protocol"
        / "v1"
        / "invalid"
        / "economics_unknown_cost_as_zero.json"
    )
    invalid_path.write_text(
        json.dumps(invalid, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
