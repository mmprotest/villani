"""Conservative online evidence projection from a finalized controller run."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..agent_systems.models import AgentSystemIdentity
from ..event_writer import redact_data, redact_message
from ..qualification.models import (
    QualificationArtifactReference,
    QualificationObservation,
)
from ..qualification.repository import qualification_system_identity
from ..qualification.store import QualificationStore
from .models import (
    DurationEstimate,
    MoneyEstimate,
    OnlineEvidenceUpdateReport,
    RoutePlan,
)
from .online import record_finalized_outcome
from .store import EconomicsStore


def _money(
    amount: float | None,
    status: str,
    *,
    source: str,
    currency: str = "USD",
    sample_count: int = 1,
) -> MoneyEstimate:
    if status in {"complete", "partial"} and amount is not None:
        return MoneyEstimate(
            amount=amount,
            currency=currency,
            accounting_status=status,  # type: ignore[arg-type]
            source=source,
            sample_count=sample_count,
        )
    normalized = status if status in {"unknown", "not_applicable"} else "unknown"
    return MoneyEstimate(
        amount=None,
        currency=None,
        accounting_status=normalized,  # type: ignore[arg-type]
        source=source,
        sample_count=0,
    )


def _verification_cost(verification: Any | None) -> MoneyEstimate:
    if verification is None:
        return _money(None, "unknown", source="missing_authoritative_verification")
    usages = list(verification.llm_usage)
    if not usages:
        return _money(
            None,
            "not_applicable",
            source="deterministic_verifier_no_model_charge",
        )
    known = [item for item in usages if item.cost is not None]
    currencies = {item.currency for item in known}
    if len(currencies) != 1 or not known:
        return _money(None, "unknown", source="authoritative_verifier_cost_unknown")
    amount = sum(float(item.cost) for item in known)
    complete = len(known) == len(usages) and all(
        item.cost_accounting_status == "complete" for item in usages
    )
    return _money(
        amount,
        "complete" if complete else "partial",
        source=(
            "authoritative_verifier_usage"
            if complete
            else "partial_authoritative_verifier_usage"
        ),
        currency=next(iter(currencies)),
        sample_count=len(known),
    )


def _retry_cost(attempts: Sequence[Any], ordinal: int) -> MoneyEstimate:
    prior = [item for item in attempts if item.ordinal < ordinal]
    if not prior:
        return _money(None, "not_applicable", source="no_prior_retry_or_escalation")
    known = [item.cost_usd for item in prior if item.cost_usd is not None]
    if not known:
        return _money(None, "unknown", source="prior_attempt_costs_unknown")
    complete = len(known) == len(prior) and all(
        item.cost_accounting_status == "complete" for item in prior
    )
    return _money(
        sum(float(value) for value in known),
        "complete" if complete else "partial",
        source="prior_attempt_execution_costs",
        sample_count=len(known),
    )


def _total_observed_cost(
    attempts: Sequence[Any], verification_cost: MoneyEstimate
) -> tuple[float | None, str, str | None]:
    components = [
        _money(
            item.cost_usd,
            item.cost_accounting_status,
            source="authoritative_harness_attempt_cost",
        )
        for item in attempts
    ]
    components.append(verification_cost)
    known = [item for item in components if item.amount is not None]
    currencies = {item.currency for item in known}
    unknown = [
        item
        for item in components
        if item.accounting_status not in {"complete", "not_applicable"}
    ]
    if not known:
        return None, "unknown", None
    if len(currencies) != 1:
        return None, "unknown", None
    status = "complete" if not unknown else "partial"
    return (
        sum(float(item.amount) for item in known if item.amount is not None),
        status,
        next(iter(currencies)),
    )


def _report(run_id: str, recorded_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": "villani.online_evidence_update.v1",
        "run_id": run_id,
        "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
        "status": "skipped",
        "qualification_observation_id": None,
        "economics_observation_id": None,
        "profile_updated": False,
        "automatic_policy_metrics_eligible": False,
        "reasons": [],
    }


def _validated_report(report: Mapping[str, Any]) -> dict[str, Any]:
    return OnlineEvidenceUpdateReport.model_validate(report).model_dump(mode="json")


def record_runtime_economics(
    *,
    runtime: Any,
    identity_documents: Sequence[Mapping[str, Any]],
    qualification_store: QualificationStore | None,
    economics_store: EconomicsStore | None,
    recorded_at: datetime,
) -> dict[str, Any] | None:
    """Project one final route outcome, or persist why it could not train policy."""

    economics_configuration = runtime.request.policy_configuration.get("economics")
    economics_values = (
        economics_configuration if isinstance(economics_configuration, Mapping) else {}
    )
    online = economics_values.get("online_update")
    online_values = online if isinstance(online, Mapping) else {}
    # This is an explicit configuration migration.  Legacy/custom controller
    # configurations remain read-compatible and do not gain new side effects.
    if online_values.get("enabled") is not True:
        return None
    report = _report(runtime.run_id, recorded_at)
    try:
        if qualification_store is None or economics_store is None:
            report["reasons"] = ["online evidence stores were not configured"]
            return _validated_report(report)
        attempt = next(
            (
                item
                for item in runtime.attempts
                if item.attempt_id == runtime.selected_attempt_id
            ),
            runtime.attempts[-1] if runtime.attempts else None,
        )
        if attempt is None or runtime.classification is None:
            report["reasons"] = ["no finalized coding outcome is available"]
            return _validated_report(report)
        decision = next(
            (
                item
                for item in reversed(runtime.policy_decisions)
                if item.attempt_id == attempt.attempt_id
                and isinstance(item.metadata.get("route_plan"), Mapping)
            ),
            None,
        )
        if decision is None:
            report["reasons"] = [
                "legacy outcome has no PT8 route plan; no qualification was created"
            ]
            return _validated_report(report)
        route_plan = RoutePlan.model_validate(decision.metadata["route_plan"])
        identity_document = next(
            (
                item
                for item in identity_documents
                if item.get("system_id") == attempt.agent_system_id
            ),
            None,
        )
        if identity_document is None:
            report["reasons"] = ["exact agent-system identity is unavailable"]
            return _validated_report(report)
        identity = AgentSystemIdentity.model_validate(identity_document)
        repository_commit = route_plan.repository_head or ""
        if not re.fullmatch(r"[0-9a-f]{40,64}", repository_commit):
            report["reasons"] = ["repository baseline commit is unavailable"]
            return _validated_report(report)
        baseline_digest = str(
            attempt.metadata.get("baseline_sha256")
            or runtime.reliability_baseline_sha256
            or ""
        )
        baseline_valid = bool(re.fullmatch(r"[0-9a-f]{64}", baseline_digest))
        verification = next(
            (
                item
                for item in reversed(runtime.verifications)
                if item.attempt_id == attempt.attempt_id
            ),
            None,
        )
        authoritative = verification is not None and verification.outcome in {
            "accepted",
            "rejected",
        }
        proved_acceptable = (
            verification.outcome == "accepted"
            if verification is not None and authoritative
            else None
        )

        route_path = f"route-plans/{decision.decision_id}.json"
        artifact_paths = [f"attempts/{attempt.attempt_id}/attempt.json", route_path]
        verification_path = f"verification/{attempt.attempt_id}.json"
        if verification is not None:
            artifact_paths.append(verification_path)
        artifacts: list[QualificationArtifactReference] = []
        for relative_path in artifact_paths:
            path = runtime.store.run_directory / relative_path
            if path.is_file():
                artifacts.append(
                    QualificationArtifactReference(
                        kind=(
                            "route_plan"
                            if relative_path == route_path
                            else "verification"
                            if relative_path == verification_path
                            else "attempt"
                        ),
                        path=relative_path,
                        digest="sha256:"
                        + hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                )
        harness_path = (
            runtime.store.run_directory / attempt.harness_result_path
            if attempt.harness_result_path
            else None
        )
        candidate_evidence_complete = bool(
            (runtime.store.run_directory / artifact_paths[0]).is_file()
            and harness_path is not None
            and harness_path.is_file()
        )

        infrastructure_status = "resolved"
        infrastructure_reason: str | None = None
        error_code = attempt.error.code.lower() if attempt.error is not None else ""
        exclusions = {
            "cancellation": "cancellation",
            "cancelled": "cancellation",
            "environment": "environment_mismatch",
            "missing_executable": "missing_executable",
            "provider_outage": "provider_outage",
            "rate_limit": "provider_outage",
            "permission": "policy_denial",
            "policy_denied": "policy_denial",
        }
        if runtime.machine.state == "CANCELLED" or attempt.status == "cancelled":
            infrastructure_status = "excluded"
            infrastructure_reason = "cancellation"
        else:
            for marker, reason in exclusions.items():
                if marker in error_code:
                    infrastructure_status = "excluded"
                    infrastructure_reason = reason
                    break
        if verification is not None and verification.outcome in {"unclear", "error"}:
            infrastructure_status = "excluded"
            infrastructure_reason = "verifier_infrastructure_failure"
        if (
            runtime.delivery is not None
            and runtime.delivery.selected_attempt_id == attempt.attempt_id
            and runtime.delivery.state == "failed"
        ):
            infrastructure_status = "excluded"
            infrastructure_reason = "delivery_infrastructure_failure"
        if not candidate_evidence_complete or not baseline_valid:
            infrastructure_status = "unresolved"
            infrastructure_reason = (
                "incomplete_candidate_evidence"
                if not candidate_evidence_complete
                else "invalid_baseline_evidence"
            )
        if not authoritative and infrastructure_reason is None:
            infrastructure_status = "unresolved"
            infrastructure_reason = "missing_authoritative_verification"

        human_review_required = bool(
            runtime.delivery is not None
            and runtime.delivery.selected_attempt_id == attempt.attempt_id
            and runtime.delivery.approval.required
        )
        approval_status = (
            runtime.delivery.approval.status if runtime.delivery is not None else None
        )
        human_review_complete = approval_status in {
            "approved",
            "rejected",
            "rerun_requested",
        }
        human_review_status = (
            "complete"
            if human_review_required and human_review_complete
            else "missing"
            if human_review_required
            else "not_applicable"
        )
        accepted_as_is = (
            approval_status == "approved"
            if human_review_required and human_review_complete
            else None
        )
        secret_issue = redact_data(dict(identity_document)) != dict(identity_document)
        isolation_violation = (
            Path(attempt.worktree_path).resolve()
            == Path(runtime.request.repository_path).resolve()
        )
        corruption = isolation_violation
        exclusion_reasons = [
            reason
            for condition, reason in (
                (not baseline_valid, "invalid_baseline_evidence"),
                (not candidate_evidence_complete, "incomplete_candidate_evidence"),
                (not authoritative, "missing_authoritative_verification"),
                (infrastructure_status != "resolved", infrastructure_reason),
                (
                    human_review_required and not human_review_complete,
                    "required_human_review_missing",
                ),
                (corruption, "isolation_or_artifact_corruption"),
                (secret_issue, "secret_issue"),
            )
            if condition and reason
        ]
        eligible = bool(
            baseline_valid
            and candidate_evidence_complete
            and authoritative
            and infrastructure_status == "resolved"
            and (not human_review_required or human_review_complete)
            and not corruption
            and not secret_issue
        )
        verifier_cost = _verification_cost(verification)
        total_cost, total_cost_status, total_currency = _total_observed_cost(
            runtime.attempts, verifier_cost
        )
        duration_ms = max(
            int((recorded_at - runtime.created_at).total_seconds() * 1000), 0
        )
        qualification = QualificationObservation(
            observation_id="qobs_"
            + hashlib.sha256(
                f"{runtime.run_id}:{attempt.attempt_id}:{route_plan.plan_id}".encode()
            ).hexdigest(),
            recorded_at=recorded_at,
            observed_at=recorded_at,
            source_kind="canonical_run",
            source_suite_id=None,
            source_suite_digest=None,
            source_task_id=runtime.task_id,
            source_task_digest=hashlib.sha256(
                runtime.request.task.encode("utf-8")
            ).hexdigest(),
            source_trial_id=runtime.run_id,
            source_review_id=(
                runtime.delivery.approval.request_id
                if human_review_required and runtime.delivery is not None
                else None
            ),
            repository_id=route_plan.repository_id,
            repository_commit=repository_commit,
            repository_baseline_digest=(
                baseline_digest if baseline_valid else "0" * 64
            ),
            task_profile=route_plan.task_profile,
            profile_source="authoritative_run_classification",
            system=qualification_system_identity(
                identity,
                environment_fingerprint=(
                    identity.execution.environment_fingerprint or "unavailable"
                ),
            ),
            baseline_valid=baseline_valid,
            candidate_evidence_complete=candidate_evidence_complete,
            authoritative_verification_complete=authoritative,
            infrastructure_status=infrastructure_status,  # type: ignore[arg-type]
            human_review_required=human_review_required,
            human_review_status=human_review_status,  # type: ignore[arg-type]
            corruption_detected=corruption,
            secret_issue_detected=secret_issue,
            target_repository_modified=False,
            proved_acceptable=proved_acceptable,
            accepted_as_is=accepted_as_is,
            successful=(
                bool(
                    proved_acceptable is True
                    and (not human_review_required or accepted_as_is is True)
                )
                if eligible
                else None
            ),
            false_acceptance=False,
            false_rejection=False,
            later_rollback=False,
            reopened_defect=False,
            cost_amount=total_cost,
            cost_currency=total_currency,
            cost_accounting_status=total_cost_status,  # type: ignore[arg-type]
            duration_ms=duration_ms,
            duration_accounting_status="complete",
            review_minutes=None,
            eligible=eligible,
            exclusion_reason=(
                None
                if eligible
                else "; ".join(sorted(set(exclusion_reasons)))
                or "outcome excluded by qualification evidence policy"
            ),
            artifacts=artifacts,
        )
        economics = record_finalized_outcome(
            qualification_observation=qualification,
            route_plan=route_plan,
            execution_cost=_money(
                attempt.cost_usd,
                attempt.cost_accounting_status,
                source="authoritative_harness_attempt_cost",
            ),
            verification_cost=verifier_cost,
            human_review_cost=_money(
                None,
                "unknown" if human_review_required else "not_applicable",
                source=(
                    "review_minutes_or_cost_rate_unknown"
                    if human_review_required
                    else "human_review_not_required"
                ),
            ),
            retry_escalation_cost=_retry_cost(runtime.attempts, attempt.ordinal),
            duration=DurationEstimate(
                duration_ms=duration_ms,
                accounting_status="complete",
                source="controller_run_wall_clock",
                sample_count=1,
            ),
            attempt_count=len(runtime.attempts),
            escalation_count=sum(
                event.event_type == "escalation_selected"
                for event in runtime.committed_events
            ),
            review_minutes=None,
            qualification_store=qualification_store,
            economics_store=economics_store,
            route_plan_artifact_path=route_path,
            recorded_at=recorded_at,
        )
        report.update(
            {
                "status": "recorded" if qualification.eligible else "excluded",
                "qualification_observation_id": qualification.observation_id,
                "economics_observation_id": economics.observation_id,
                "profile_updated": economics.eligible_for_profile,
                "automatic_policy_metrics_eligible": (
                    economics.eligible_for_automatic_policy_metrics
                ),
                "reasons": (
                    [] if qualification.eligible else [qualification.exclusion_reason]
                ),
            }
        )
    except Exception as error:
        report.update(
            {
                "status": "failed",
                "profile_updated": False,
                "automatic_policy_metrics_eligible": False,
                "reasons": [redact_message(str(error))],
            }
        )
    return _validated_report(report)


__all__ = ["record_runtime_economics"]
