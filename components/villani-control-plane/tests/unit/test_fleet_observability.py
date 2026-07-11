from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import load_v2_fixture
from sqlalchemy import text

from villani_control_plane import models
from villani_control_plane.errors import AuthorizationError, ConflictError, NotFoundError
from villani_control_plane.live import LiveMessage
from villani_control_plane.schemas import (
    AlertRuleRequest,
    FeedbackRequest,
    FleetExportRequest,
    FleetFilters,
    FleetSearchRequest,
    MetricRequest,
    ReviewQueueRequest,
    SavedViewRequest,
)
from villani_control_plane.services import IngestionService
from villani_control_plane.services.fleet import AlertService, FleetObservabilityService

NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def add_run(session, principal, run_id: str, **values):
    run = models.Run(
        organization_id=principal.organization_id,
        workspace_id=principal.workspace_id,
        project_id="project_1",
        repository_id="repo_001",
        id=run_id,
        trace_id=(run_id.encode().hex() + "0" * 32)[:32],
        status=values.pop("status", "completed"),
        first_occurred_at=NOW,
        first_observed_at=NOW,
        last_observed_at=NOW,
        **values,
    )
    session.add(run)
    session.flush()
    return run


def test_ingestion_projects_search_dimensions_and_failure_signature(session, principal) -> None:
    event = load_v2_fixture("telemetry-envelope.json")
    event.update(
        event_id="fleet-failure", idempotency_key="fleet-failure", name="run_failed", status="error"
    )
    event["attributes"] = {
        "agent": "codex",
        "model": "m",
        "provider": "openai",
        "policy_version": "p1",
        "task_category": "bug",
        "tags": ["prod"],
    }
    event["body"] = {
        "failure_category": "tool_loop",
        "root_cause": "Repeated command cycle",
        "cost_usd": 3.5,
        "cost_accounting_status": "complete",
        "total_tokens": 42,
        "token_accounting_status": "complete",
        "duration_ms": 900,
    }
    IngestionService(session).ingest_batch("fleet-projection", [event], principal)
    run = session.get(models.Run, (principal.organization_id, event["run_id"]))
    assert (run.agent_name, run.model_name, run.provider_name, run.policy_version) == (
        "codex",
        "m",
        "openai",
        "p1",
    )
    assert (run.cost_usd, run.total_tokens, run.duration_ms, run.tags_text) == (
        3.5,
        42,
        900,
        "|prod|",
    )
    clusters = FleetObservabilityService(session).clusters(principal)
    assert clusters[0]["failure_category"] == "tool_loop"
    assert clusters[0]["advisory_label"] is None


def test_metric_denominators_unknowns_and_comparisons_are_exact(session, principal) -> None:
    add_run(
        session,
        principal,
        "accepted_known",
        verification_status="accepted",
        cost_usd=10,
        duration_ms=100,
        queue_time_ms=20,
        attempt_count=1,
        verifier_cost_usd=1,
        rejected_cost_usd=0,
        model_name="m1",
    )
    add_run(
        session,
        principal,
        "accepted_unknown",
        verification_status="accepted",
        cost_usd=None,
        duration_ms=None,
        queue_time_ms=None,
        attempt_count=2,
        escalation_count=1,
        model_name="m1",
    )
    add_run(
        session,
        principal,
        "rejected",
        verification_status="rejected",
        cost_usd=5,
        duration_ms=300,
        queue_time_ms=40,
        attempt_count=1,
        verifier_disagreement=True,
        rejected_cost_usd=5,
        model_name="m2",
    )
    add_run(
        session, principal, "unclear", verification_status="unclear", cost_usd=None, model_name=None
    )
    add_run(
        session,
        principal,
        "accepted_false",
        verification_status="accepted",
        cost_usd=20,
        model_name="m2",
    )
    session.add_all(
        [
            models.Outcome(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                run_id="accepted_known",
                attempt_id=None,
                attempt_key="",
                version=1,
                provenance={},
                confidence=1,
                document={"verification_status": "accepted", "materialized": True, "merged": False},
            ),
            models.Outcome(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                run_id="accepted_false",
                attempt_id=None,
                attempt_key="",
                version=1,
                provenance={},
                confidence=1,
                document={"verification_status": "accepted", "materialized": True, "merged": True},
            ),
            models.RunFeedback(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                run_id="rejected",
                actor_id=principal.token_id,
                kind="developer_disposition",
                document={"disposition": "approved"},
                version=1,
            ),
            models.RunFeedback(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                run_id="accepted_false",
                actor_id=principal.token_id,
                kind="developer_disposition",
                document={"disposition": "rejected"},
                version=1,
            ),
        ]
    )
    session.commit()
    result = FleetObservabilityService(session).metrics(MetricRequest(group_by="model"), principal)
    metrics = result["metrics"]
    assert metrics["verified_success_rate"] == {
        "value": 0.75,
        "numerator": 3,
        "denominator": 4,
        "unknown_outcome_count": 1,
    }
    assert metrics["cost_per_accepted_change"] == {
        "value": 15,
        "known_denominator": 2,
        "unknown_cost_count": 1,
        "total_changes": 3,
    }
    assert metrics["cost_per_materialized_change"] == {
        "value": 15,
        "known_denominator": 2,
        "unknown_cost_count": 0,
        "total_changes": 2,
    }
    assert metrics["cost_per_merged_change"] == {
        "value": 20,
        "known_denominator": 1,
        "unknown_cost_count": 0,
        "total_changes": 1,
    }
    assert metrics["duration_ms"] == {"average": 200, "known_count": 2, "unknown_count": 3}
    assert metrics["false_acceptance_rate"] == {
        "value": 1,
        "numerator": 1,
        "denominator": 1,
        "unlabeled_count": 2,
    }
    assert metrics["false_rejection_rate"] == {
        "value": 1,
        "numerator": 1,
        "denominator": 1,
        "unlabeled_count": 0,
    }
    assert metrics["verifier_spend_usd"]["unknown_count"] == 4
    assert set(result["comparisons"]) == {"m1", "m2", "unknown"}
    assert (
        FleetObservabilityService(session).metric_definitions()["version"]
        == "villani.fleet_metrics.v1"
    )


def test_search_views_exports_feedback_and_clusters_are_tenant_scoped(session, principal) -> None:
    add_run(
        session,
        principal,
        "visible",
        agent_name="codex",
        model_name="m",
        provider_name="openai",
        policy_version="p1",
        task_category="bug",
        verification_status="accepted",
        cost_usd=2,
        total_tokens=100,
        duration_ms=500,
        tags=["prod", "python"],
        tags_text="|prod|python|",
    )
    session.commit()
    service = FleetObservabilityService(session)
    filters = FleetFilters(agent="codex", tags=["prod"], min_cost_usd=1, max_tokens=200)
    page = service.search(FleetSearchRequest(filters=filters, limit=1), principal)
    assert [run["id"] for run in page.runs] == ["visible"]
    with pytest.raises(AuthorizationError):
        service.search(FleetSearchRequest(filters=FleetFilters(organization_id="other")), principal)
    view = service.create_view(
        SavedViewRequest(
            name="Production",
            filter_ast=filters.model_dump(mode="json"),
            columns=["state", "cost"],
            sort=[{"field": "last_observed_at", "direction": "desc"}],
        ),
        principal,
    )
    assert view["owner"] == principal.token_id and view["version"] == 1
    updated = service.update_view(
        view["id"],
        SavedViewRequest(
            name="Production v2",
            filter_ast=filters.model_dump(mode="json"),
            columns=["state"],
            sort=[],
            version=1,
        ),
        principal,
    )
    assert updated["version"] == 2
    with pytest.raises(ConflictError):
        service.update_view(
            view["id"],
            SavedViewRequest(name="stale", version=1),
            principal,
        )
    feedback = service.create_feedback(
        "visible",
        FeedbackRequest(kind="annotation", document={"note": "reviewed", "api_key": "hidden"}),
        principal,
    )
    assert feedback["document"]["api_key"] == "********"
    correction = service.create_feedback(
        "visible",
        FeedbackRequest(
            kind="correction", document={"note": "corrected"}, corrects_feedback_id=feedback["id"]
        ),
        principal,
    )
    assert correction["version"] == 2
    queue = service.enqueue_review(
        ReviewQueueRequest(run_id="visible", queue="verification", priority=5, reason="spot check"),
        principal,
    )
    assert queue["state"] == "open" and service.review_queue(principal)[0]["run_id"] == "visible"
    media, exported = service.export(FleetExportRequest(filters=filters, format="csv"), principal)
    assert media == "text/csv" and "visible" in exported and "hidden" not in exported
    other = principal.__class__("token", "other", principal.workspace_id, None)
    with pytest.raises(NotFoundError):
        service.feedback("visible", other)
    assert service.metrics(MetricRequest(), other)["metrics"]["run_count"] == 0
    assert "visible" not in service.export(FleetExportRequest(format="json"), other)[1]


def test_alert_replay_cooldown_and_resolve_are_idempotent(session, principal) -> None:
    rule = AlertService(session).create(
        AlertRuleRequest(
            name="Spend",
            rule_type="spend",
            threshold={"operator": "gte", "value": 5},
            cooldown_seconds=3600,
            destination={"type": "test_webhook"},
        ),
        principal,
    )
    service = AlertService(session)
    high = LiveMessage(
        "source-1",
        principal.organization_id,
        principal.workspace_id,
        "telemetry.ingested",
        {"run_id": "r", "event": {"attributes": {}, "body": {"cost_usd": 10}}},
    )
    assert service.evaluate(high) == 1
    assert service.evaluate(high) == 0
    assert (
        service.evaluate(
            LiveMessage(
                "source-2",
                principal.organization_id,
                principal.workspace_id,
                "telemetry.ingested",
                {"run_id": "r", "event": {"attributes": {}, "body": {"cost_usd": 12}}},
            )
        )
        == 0
    )
    assert (
        service.evaluate(
            LiveMessage(
                "source-3",
                principal.organization_id,
                principal.workspace_id,
                "telemetry.ingested",
                {"run_id": "r", "event": {"attributes": {}, "body": {"cost_usd": 1}}},
            )
        )
        == 1
    )
    events = service.events(principal)
    assert [event["event_type"] for event in events] == ["resolved", "fired"]
    assert all(event["document"]["delivery"]["status"] == "recorded_not_sent" for event in events)
    assert rule["destination"] == {"type": "test_webhook"}
    other = principal.__class__("token", "other", principal.workspace_id, None)
    assert service.list(other) == [] and service.events(other) == []


def test_cursor_search_uses_indexes_and_never_loads_100000_runs(session, principal) -> None:
    mappings = []
    for index in range(100_000):
        observed = NOW + timedelta(seconds=index)
        mappings.append(
            {
                "organization_id": principal.organization_id,
                "workspace_id": principal.workspace_id,
                "project_id": "project_1",
                "repository_id": "repo_001",
                "id": f"run_{index:06d}",
                "trace_id": f"{index + 1:032x}",
                "status": "completed",
                "first_occurred_at": observed,
                "first_observed_at": observed,
                "last_observed_at": observed,
                "provider_name": "openai",
                "model_name": "fleet-model",
                "cost_accounting_status": "unknown",
                "token_accounting_status": "unknown",
                "attempt_count": 0,
                "escalation_count": 0,
                "tags": [],
                "tags_text": "",
            }
        )
    session.bulk_insert_mappings(models.Run, mappings)
    session.commit()
    service = FleetObservabilityService(session)
    first = service.search(
        FleetSearchRequest(filters=FleetFilters(provider="openai"), limit=100), principal
    )
    second = service.search(
        FleetSearchRequest(
            filters=FleetFilters(provider="openai"), cursor=first.next_cursor, limit=100
        ),
        principal,
    )
    assert len(first.runs) == len(second.runs) == 100
    assert first.runs[0]["id"] == "run_099999" and second.runs[0]["id"] == "run_099899"
    assert set(run["id"] for run in first.runs).isdisjoint(run["id"] for run in second.runs)
    plan = session.execute(
        text(
            "EXPLAIN QUERY PLAN SELECT * FROM runs WHERE organization_id='org_1' AND workspace_id='workspace_1' AND provider_name='openai' ORDER BY last_observed_at DESC LIMIT 101"
        )
    ).all()
    assert "INDEX" in " ".join(str(row) for row in plan).upper()
