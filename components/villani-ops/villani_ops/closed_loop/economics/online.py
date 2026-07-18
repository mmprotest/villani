"""Append finalized, explicitly evidenced outcomes for future routing only."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath

from ..qualification.models import (
    QualificationInvalidation,
    QualificationObservation,
)
from ..qualification.store import QualificationStore
from .models import (
    DurationEstimate,
    EconomicsObservation,
    MoneyEstimate,
    RoutePlan,
    canonical_digest,
)
from .store import EconomicsStore


def record_finalized_outcome(
    *,
    qualification_observation: QualificationObservation,
    route_plan: RoutePlan,
    execution_cost: MoneyEstimate,
    verification_cost: MoneyEstimate,
    human_review_cost: MoneyEstimate,
    retry_escalation_cost: MoneyEstimate,
    duration: DurationEstimate,
    attempt_count: int,
    escalation_count: int,
    review_minutes: float | None,
    qualification_store: QualificationStore,
    economics_store: EconomicsStore,
    route_plan_artifact_path: str | None = None,
    recorded_at: datetime | None = None,
) -> EconomicsObservation:
    """Persist one outcome without allowing it to affect its own run.

    Qualification remains the source of eligibility truth.  Ineligible and
    infrastructure-excluded observations are retained in both ledgers but are
    never included in derived economics profiles.  A known false acceptance is
    also appended as an immediate severe qualification invalidation.
    """

    if route_plan.run_id != qualification_observation.source_trial_id:
        raise ValueError(
            "route plan and qualification outcome belong to different runs"
        )
    system = qualification_observation.system
    selected = next(
        (
            item
            for item in route_plan.systems_considered
            if item.system_id == system.system_id
            and item.route_name == system.route_name
        ),
        None,
    )
    if selected is None:
        raise ValueError("qualification system is absent from the recorded route plan")
    if qualification_observation.repository_id != route_plan.repository_id:
        raise ValueError("route plan and qualification repository identities differ")
    if qualification_observation.task_profile != route_plan.task_profile:
        raise ValueError("route plan and qualification task profiles differ")
    if attempt_count < 1 or escalation_count < 0:
        raise ValueError("attempt and escalation counts must be non-negative evidence")
    if route_plan_artifact_path is not None:
        normalized_path = PurePosixPath(route_plan_artifact_path.replace("\\", "/"))
        if normalized_path.is_absolute() or ".." in normalized_path.parts:
            raise ValueError("route plan evidence path must stay inside the run bundle")

    now = recorded_at or datetime.now(timezone.utc)
    profile_eligible = bool(
        qualification_observation.eligible
        and qualification_observation.authoritative_verification_complete
        and qualification_observation.infrastructure_status == "resolved"
        and not qualification_observation.false_acceptance
    )
    exclusion_reason = (
        None
        if profile_eligible
        else "false_acceptance"
        if qualification_observation.false_acceptance
        else qualification_observation.exclusion_reason
        or "qualification evidence excluded"
    )
    source = {
        "qualification_observation_id": qualification_observation.observation_id,
        "route_plan_id": route_plan.plan_id,
        "execution_cost": execution_cost.model_dump(mode="json"),
        "verification_cost": verification_cost.model_dump(mode="json"),
        "human_review_cost": human_review_cost.model_dump(mode="json"),
        "retry_escalation_cost": retry_escalation_cost.model_dump(mode="json"),
        "duration": duration.model_dump(mode="json"),
        "attempt_count": attempt_count,
        "escalation_count": escalation_count,
        "review_minutes": review_minutes,
    }
    economics_observation = EconomicsObservation(
        observation_id="eobs_" + canonical_digest(source).removeprefix("sha256:"),
        recorded_at=now,
        observed_at=qualification_observation.observed_at,
        source_run_id=route_plan.run_id,
        source_route_plan_id=route_plan.plan_id,
        qualification_observation_id=qualification_observation.observation_id,
        repository_id=qualification_observation.repository_id,
        task_profile=qualification_observation.task_profile,
        system_id=system.system_id,
        system_identity_digest=system.identity_digest,
        route_name=system.route_name,
        policy_version=route_plan.policy_version,
        forced_choice=route_plan.forced_choice,
        qualification_eligible=qualification_observation.eligible,
        authoritative_verification_complete=(
            qualification_observation.authoritative_verification_complete
        ),
        infrastructure_status=qualification_observation.infrastructure_status,
        proved_acceptable=qualification_observation.proved_acceptable,
        accepted_as_is=qualification_observation.accepted_as_is,
        false_acceptance=qualification_observation.false_acceptance,
        eligible_for_profile=profile_eligible,
        eligible_for_automatic_policy_metrics=bool(
            qualification_observation.eligible
            and qualification_observation.authoritative_verification_complete
            and qualification_observation.infrastructure_status == "resolved"
            and not qualification_observation.false_acceptance
            and not route_plan.forced_choice
        ),
        exclusion_reason=exclusion_reason,
        execution_cost=execution_cost,
        verification_cost=verification_cost,
        human_review_cost=human_review_cost,
        retry_escalation_cost=retry_escalation_cost,
        duration=duration,
        review_minutes=review_minutes,
        attempt_count=attempt_count,
        escalation_count=escalation_count,
    )

    # Idempotent content-addressed records make a recovery retry safe.  Append
    # qualification first: a temporary economics failure can only make routing
    # more conservative because its objective inputs remain sparse.
    qualification_store.append_observation(qualification_observation)
    if qualification_observation.false_acceptance:
        detail = (
            "A finalized accepted change was later identified as a false acceptance."
        )
        invalidation_source = {
            "system_id": system.system_id,
            "repository_id": qualification_observation.repository_id,
            "observation_id": qualification_observation.observation_id,
            "detail": detail,
        }
        qualification_store.append_invalidation(
            QualificationInvalidation(
                invalidation_id="qinv_"
                + canonical_digest(invalidation_source).removeprefix("sha256:"),
                recorded_at=now,
                system_id=system.system_id,
                route_name=system.route_name,
                repository_id=qualification_observation.repository_id,
                reason="false_acceptance",
                severity="severe",
                evidence_reference=(
                    str(normalized_path)
                    if route_plan_artifact_path is not None
                    else f"route-plans/{route_plan.plan_id}.json"
                ),
                evidence_digest=canonical_digest(route_plan.model_dump(mode="json")),
                detail=detail,
            )
        )
    qualification_store.rebuild(generated_at=now)
    economics_store.append_observation(economics_observation)
    economics_store.rebuild(generated_at=now)
    return economics_observation


__all__ = ["record_finalized_outcome"]
