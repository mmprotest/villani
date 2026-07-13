from __future__ import annotations

import pytest
from conftest import TEST_TOKEN, load_v2_fixture, seed_tenant
from sqlalchemy import func, select

from villani_control_plane.errors import AuthenticationError, AuthorizationError, ConflictError
from villani_control_plane.models import Event, IngestBatch, Outbox
from villani_control_plane.security import hash_token, mask_sensitive_fields, verify_token
from villani_control_plane.services import AuthenticationService, IngestionService, RunQueryService


def event(sequence: int = 1, **updates):
    value = load_v2_fixture("telemetry-envelope.json")
    value.update(
        {
            "event_id": f"evt_{sequence}",
            "idempotency_key": f"test:{sequence}",
            "sequence": sequence,
            "span_id": f"{sequence:016x}",
        }
    )
    value.update(updates)
    return value


def test_tokens_are_salted_and_verified_without_plaintext() -> None:
    token = "a-development-token-with-enough-entropy"
    first = hash_token(token)
    second = hash_token(token)
    assert first != second
    assert token not in first
    assert verify_token(token, first)
    assert not verify_token(token + "x", first)


def test_response_masking_preserves_harmless_token_metrics_and_statuses() -> None:
    value = mask_sensitive_fields(
        {
            "token": "test-token",
            "numeric_token_metric": 42,
            "input_tokens": 20,
            "token_accounting_status": "complete",
            "access_token": "unsafe",
            "message": "Bearer abcdefghijklmnop",
        }
    )
    assert value["token"] == "test-token"
    assert value["numeric_token_metric"] == 42
    assert value["input_tokens"] == 20
    assert value["token_accounting_status"] == "complete"
    assert value["access_token"] == "********"
    assert value["message"] == "********"


def test_authentication_is_scoped_and_rejects_unknown_token(session, principal) -> None:
    authenticated = AuthenticationService(session).authenticate(TEST_TOKEN)
    assert authenticated.organization_id == principal.organization_id
    assert authenticated.workspace_id == principal.workspace_id
    with pytest.raises(AuthenticationError):
        AuthenticationService(session).authenticate("not-the-token")


def test_duplicate_batches_and_events_are_idempotent(session, principal) -> None:
    service = IngestionService(session)
    first = service.ingest_batch("batch_1", [event()], principal)
    second = service.ingest_batch("batch_1", [event()], principal)
    third = service.ingest_batch("batch_2", [event()], principal)
    assert (first.inserted, first.duplicates, first.replayed) == (1, 0, False)
    assert (second.inserted, second.duplicates, second.replayed) == (0, 1, True)
    assert (third.inserted, third.duplicates) == (0, 1)
    assert session.scalar(select(func.count()).select_from(Event)) == 1
    assert session.scalar(select(func.count()).select_from(Outbox)) == 1


def test_redaction_projection_aggregates_inserted_events_once(session, principal) -> None:
    service = IngestionService(session)
    first = event(
        1,
        name="run_created",
        body={
            "task_instruction": "safe task",
            "villani_redaction": {
                "status": "redacted",
                "count": 2,
                "categories": ["registered_secret", "bearer_token"],
            },
        },
    )
    second = event(
        2,
        event_id="evt_2",
        idempotency_key="test:2",
        body={
            "villani_redaction": {
                "status": "redacted",
                "count": 1,
                "categories": ["registered_secret"],
            }
        },
    )
    service.ingest_batch("redaction-1", [first, second], principal)
    service.ingest_batch("redaction-2", [first, second], principal)

    run = RunQueryService(session).get_run(first["run_id"], principal)
    assert run.redaction_applied is True
    assert run.redacted_field_count == 3
    assert run.redaction_categories == ["bearer_token", "registered_secret"]
    assert run.redaction_status == {
        "status": "redacted",
        "applied": True,
        "count": 3,
        "redacted_field_count": 3,
        "categories": ["bearer_token", "registered_secret"],
    }


def test_batch_id_and_event_identity_collisions_fail(session, principal) -> None:
    service = IngestionService(session)
    service.ingest_batch("batch_1", [event()], principal)
    changed = event(status="error")
    with pytest.raises(ConflictError):
        service.ingest_batch("batch_1", [changed], principal)
    with pytest.raises(ConflictError):
        service.ingest_batch("batch_2", [changed], principal)


def test_cross_tenant_event_and_identity_references_fail(session, principal) -> None:
    other = seed_tenant(
        session,
        organization_id="org_2",
        workspace_id="workspace_2",
        project_id="project_2",
        repository_id="repo_2",
        token="another-development-token-long-enough",
    )
    with pytest.raises(AuthorizationError):
        IngestionService(session).ingest_batch(
            "bad_org", [event(organization_id=other.organization_id)], principal
        )
    with pytest.raises(AuthorizationError):
        IngestionService(session).ingest_batch(
            "bad_repo", [event(repository_id="repo_2")], principal
        )


def test_failed_batch_rolls_back_events_and_outbox(session, principal) -> None:
    first = event(1)
    second = event(2, event_id="other", idempotency_key="other")
    second["sequence"] = 1
    with pytest.raises(ConflictError):
        IngestionService(session).ingest_batch("rollback", [first, second], principal)
    assert session.scalar(select(func.count()).select_from(Event)) == 0
    assert session.scalar(select(func.count()).select_from(Outbox)) == 0
    assert session.scalar(select(func.count()).select_from(IngestBatch)) == 0


def test_observed_clock_cursor_pagination_and_run_filters(session, principal) -> None:
    events = []
    for sequence in range(1, 4):
        value = event(sequence)
        value["occurred_at"] = f"2026-07-11T00:00:0{4 - sequence}Z"
        value["observed_at"] = f"2026-07-11T00:00:0{sequence}Z"
        events.append(value)
    IngestionService(session).ingest_batch("page", events, principal)
    query = RunQueryService(session)
    first = query.events("run_001", principal, cursor=None, limit=2)
    second = query.events("run_001", principal, cursor=first.next_cursor, limit=2)
    assert [row["event_id"] for row in first.events] == ["evt_1", "evt_2"]
    assert [row["event_id"] for row in second.events] == ["evt_3"]
    assert (
        query.list_runs(
            principal,
            project_id="project_1",
            repository_id="repo_001",
            status=None,
            started_after=None,
            started_before=None,
            limit=10,
        )
        .runs[0]
        .id
        == "run_001"
    )
