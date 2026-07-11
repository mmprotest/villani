from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from villani_ops.closed_loop.protocol_v2 import (
    ArtifactDescriptorV2,
    OutcomeV2,
    TelemetryEnvelopeV2,
)
from villani_ops.closed_loop.schema_validation import (
    ProtocolValidationError,
    parse_protocol_document,
)

from .. import models
from ..config import get_settings
from ..errors import AuthorizationError, ConflictError, NotFoundError, RateLimitError, ServiceError
from ..repositories import IngestionRepository, TenantRepository
from ..security import Principal


def normalized_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_document(value: Any) -> str:
    return hashlib.sha256(normalized_json(value).encode("utf-8")).hexdigest()


def comparable_timestamp(value):
    return value.timestamp()


@dataclass(frozen=True, slots=True)
class BatchIngestionResult:
    batch_id: str
    inserted: int
    duplicates: int
    replayed: bool


class IngestionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ingestion = IngestionRepository(session)
        self.tenants = TenantRepository(session)

    def _parse_events(self, documents: list[dict[str, Any]]) -> list[TelemetryEnvelopeV2]:
        parsed: list[TelemetryEnvelopeV2] = []
        try:
            for document in documents:
                value = parse_protocol_document(document)
                if not isinstance(value, TelemetryEnvelopeV2):
                    raise ServiceError("ingest batches accept only villani.telemetry_envelope.v2")
                parsed.append(value)
        except ProtocolValidationError as error:
            raise ServiceError(f"v2 schema validation failed: {error}") from error
        return parsed

    @staticmethod
    def _authorize_event(event: TelemetryEnvelopeV2, principal: Principal) -> None:
        if event.organization_id not in (None, principal.organization_id):
            raise AuthorizationError("event organization_id is outside the token scope")
        if event.workspace_id not in (None, principal.workspace_id):
            raise AuthorizationError("event workspace_id is outside the token scope")

    def _resolve_project_repository(
        self, event: TelemetryEnvelopeV2, principal: Principal
    ) -> tuple[models.Project, models.Repository]:
        project = (
            self.tenants.project(principal.organization_id, event.project_id)
            if event.project_id
            else None
        )
        repository = (
            self.tenants.repository(principal.organization_id, event.repository_id)
            if event.repository_id
            else None
        )
        if event.project_id and project is None:
            raise AuthorizationError("project_id does not belong to the token organization")
        if event.repository_id and repository is None:
            raise AuthorizationError("repository_id does not belong to the token organization")
        if project and project.workspace_id != principal.workspace_id:
            raise AuthorizationError("project_id does not belong to the token workspace")
        if repository and repository.workspace_id != principal.workspace_id:
            raise AuthorizationError("repository_id does not belong to the token workspace")
        if project and repository and repository.project_id != project.id:
            raise AuthorizationError("repository_id is not part of project_id")
        if repository and project is None:
            project = self.tenants.project(principal.organization_id, repository.project_id)
        if project and repository is None:
            repository = self.tenants.add_local_repository(
                principal.organization_id, principal.workspace_id, project
            )
        if project is None or repository is None:
            project, repository = self.tenants.add_default_project_repository(
                principal.organization_id, principal.workspace_id
            )
        return project, repository

    def ingest_batch(
        self, batch_id: str, documents: list[dict[str, Any]], principal: Principal
    ) -> BatchIngestionResult:
        try:
            return self._ingest_batch(batch_id, documents, principal)
        except BaseException:
            self.session.rollback()
            raise

    def _ingest_batch(
        self, batch_id: str, documents: list[dict[str, Any]], principal: Principal
    ) -> BatchIngestionResult:
        parsed = self._parse_events(documents)
        settings = get_settings()
        if principal.installation_id:
            if len(parsed) > settings.max_installation_batch_events:
                raise RateLimitError("installation batch event limit exceeded")
            installation = self.session.scalar(
                select(models.AgentInstallation)
                .where(
                    models.AgentInstallation.organization_id == principal.organization_id,
                    models.AgentInstallation.id == principal.installation_id,
                )
                .with_for_update()
            )
            if installation is None:
                raise AuthorizationError("installation no longer exists")
            since = models.utc_now() - timedelta(minutes=1)
            recent = int(
                self.session.scalar(
                    select(func.coalesce(func.sum(models.IngestBatch.event_count), 0)).where(
                        models.IngestBatch.organization_id == principal.organization_id,
                        models.IngestBatch.installation_id == principal.installation_id,
                        models.IngestBatch.received_at >= since,
                    )
                )
                or 0
            )
            if recent + len(parsed) > settings.max_installation_events_per_minute:
                raise RateLimitError("installation per-minute ingest limit exceeded")
        for event in parsed:
            self._authorize_event(event, principal)
        normalized = [event.model_dump(mode="json") for event in parsed]
        batch_digest = digest_document(normalized)

        existing = self.ingestion.batch(principal.organization_id, batch_id)
        if existing is not None:
            if existing.payload_sha256 != batch_digest:
                raise ConflictError(f"batch_id {batch_id!r} already has different content")
            return BatchIngestionResult(batch_id, 0, len(parsed), True)

        batch = models.IngestBatch(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            batch_id=batch_id,
            payload_sha256=batch_digest,
            event_count=len(parsed),
            inserted_count=0,
            duplicate_count=0,
            installation_id=principal.installation_id,
        )
        self.ingestion.add(batch)
        try:
            self.ingestion.flush()
        except IntegrityError:
            self.session.rollback()
            existing = self.ingestion.batch(principal.organization_id, batch_id)
            if existing is None or existing.payload_sha256 != batch_digest:
                raise ConflictError(f"batch_id {batch_id!r} collided")
            return BatchIngestionResult(batch_id, 0, len(parsed), True)

        inserted = 0
        duplicates = 0
        seen: dict[tuple[str, str], str] = {}
        for event, document in zip(parsed, normalized, strict=True):
            payload_digest = digest_document(document)
            in_batch_key = (event.event_id, event.idempotency_key)
            prior_digest = seen.get(in_batch_key)
            if prior_digest is not None:
                if prior_digest != payload_digest:
                    raise ConflictError("duplicate batch event identity has different content")
                duplicates += 1
                continue
            seen[in_batch_key] = payload_digest

            prior_event = self.ingestion.event_by_event_id(
                principal.organization_id, event.event_id
            )
            prior_idempotency = self.ingestion.event_by_idempotency(
                principal.organization_id, event.idempotency_key
            )
            if prior_event is not None or prior_idempotency is not None:
                prior = prior_event or prior_idempotency
                if prior is None or prior.payload_sha256 != payload_digest:
                    raise ConflictError("event identity already has different content")
                duplicates += 1
                continue

            project, repository = self._resolve_project_repository(event, principal)
            run = self.ingestion.run(principal.organization_id, event.run_id)
            if run is None:
                run = models.Run(
                    organization_id=principal.organization_id,
                    workspace_id=principal.workspace_id,
                    project_id=project.id,
                    repository_id=repository.id,
                    id=event.run_id,
                    trace_id=event.trace_id,
                    status="created" if event.name == "run_created" else "unknown",
                    first_occurred_at=event.occurred_at,
                    first_observed_at=event.observed_at,
                    last_observed_at=event.observed_at,
                )
                self.ingestion.add(run)
                self.ingestion.flush()
            elif (
                run.workspace_id != principal.workspace_id
                or run.project_id != project.id
                or run.repository_id != repository.id
                or run.trace_id != event.trace_id
            ):
                raise AuthorizationError(
                    "run identity references conflict with its recorded tenant"
                )
            else:
                if comparable_timestamp(event.occurred_at) < comparable_timestamp(
                    run.first_occurred_at
                ):
                    run.first_occurred_at = event.occurred_at
                if comparable_timestamp(event.observed_at) < comparable_timestamp(
                    run.first_observed_at
                ):
                    run.first_observed_at = event.observed_at
                if comparable_timestamp(event.observed_at) > comparable_timestamp(
                    run.last_observed_at
                ):
                    run.last_observed_at = event.observed_at

            terminal_names = {
                "run_completed": "completed",
                "run_failed": "failed",
                "run_exhausted": "exhausted",
            }
            if event.name in terminal_names:
                run.status = terminal_names[event.name]

            if event.attempt_id:
                attempt = self.session.get(
                    models.Attempt, (principal.organization_id, event.attempt_id)
                )
                if attempt is None:
                    self.ingestion.add(
                        models.Attempt(
                            organization_id=principal.organization_id,
                            id=event.attempt_id,
                            run_id=event.run_id,
                            status=event.status,
                        )
                    )
                elif attempt.run_id != event.run_id:
                    raise AuthorizationError("attempt_id belongs to another run")
                else:
                    attempt.status = event.status

            span = self.session.get(
                models.Span, (principal.organization_id, event.trace_id, event.span_id)
            )
            if span is None:
                self.ingestion.add(
                    models.Span(
                        organization_id=principal.organization_id,
                        trace_id=event.trace_id,
                        span_id=event.span_id,
                        parent_span_id=event.parent_span_id,
                        run_id=event.run_id,
                        attempt_id=event.attempt_id,
                        kind=event.kind,
                        name=event.name,
                        status=event.status,
                        started_at=event.occurred_at,
                        ended_at=event.occurred_at
                        if event.name.endswith(("completed", "failed"))
                        else None,
                        attributes=event.attributes,
                    )
                )
            elif span.run_id != event.run_id:
                raise AuthorizationError("span identity belongs to another run")
            else:
                span.status = event.status
                if span.ended_at is None or comparable_timestamp(
                    event.occurred_at
                ) > comparable_timestamp(span.ended_at):
                    span.ended_at = event.occurred_at

            self.ingestion.add(
                models.Event(
                    organization_id=principal.organization_id,
                    workspace_id=principal.workspace_id,
                    project_id=project.id,
                    repository_id=repository.id,
                    event_id=event.event_id,
                    idempotency_key=event.idempotency_key,
                    run_id=event.run_id,
                    attempt_id=event.attempt_id,
                    trace_id=event.trace_id,
                    span_id=event.span_id,
                    sequence_scope=event.sequence_scope,
                    sequence=event.sequence,
                    occurred_at=event.occurred_at,
                    observed_at=event.observed_at,
                    source=event.source,
                    kind=event.kind,
                    name=event.name,
                    status=event.status,
                    payload_sha256=payload_digest,
                    document=document,
                )
            )
            self.ingestion.add(
                models.Outbox(
                    organization_id=principal.organization_id,
                    workspace_id=principal.workspace_id,
                    topic="telemetry.ingested",
                    aggregate_type="event",
                    aggregate_id=event.event_id,
                    payload={
                        "event_id": event.event_id,
                        "run_id": event.run_id,
                        "event": document,
                    },
                )
            )
            inserted += 1

        batch.inserted_count = inserted
        batch.duplicate_count = duplicates
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            replay = self.ingestion.batch(principal.organization_id, batch_id)
            if replay is not None and replay.payload_sha256 == batch_digest:
                return BatchIngestionResult(batch_id, 0, len(parsed), True)
            raise ConflictError("event or sequence identity collision") from error
        return BatchIngestionResult(batch_id, inserted, duplicates, False)

    def register_artifact(
        self, run_id: str, document: dict[str, Any], principal: Principal
    ) -> dict[str, Any]:
        try:
            parsed = parse_protocol_document(document)
        except ProtocolValidationError as error:
            raise ServiceError(f"v2 schema validation failed: {error}") from error
        if not isinstance(parsed, ArtifactDescriptorV2):
            raise ServiceError("descriptor must use villani.artifact_descriptor.v2")
        run = self.ingestion.run(principal.organization_id, run_id)
        if run is None or run.workspace_id != principal.workspace_id:
            raise NotFoundError("run not found")
        normalized = parsed.model_dump(mode="json")
        artifact = self.session.get(
            models.Artifact, (principal.organization_id, parsed.artifact_id)
        )
        if artifact is not None:
            if artifact.document != normalized or artifact.run_id != run_id:
                raise ConflictError("artifact_id already has different content")
            return normalized
        self.ingestion.add(
            models.Artifact(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                id=parsed.artifact_id,
                run_id=run_id,
                digest_sha256=parsed.digest.value,
                size_bytes=parsed.size_bytes,
                status="pending",
                object_key=(
                    f"organizations/{principal.organization_id}/sha256/"
                    f"{parsed.digest.value[:2]}/{parsed.digest.value}"
                ),
                document=normalized,
            )
        )
        self.ingestion.add(
            models.Outbox(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                topic="artifact.descriptor.recorded",
                aggregate_type="artifact",
                aggregate_id=parsed.artifact_id,
                payload={"artifact_id": parsed.artifact_id, "run_id": run_id},
            )
        )
        self.session.commit()
        return normalized

    def record_outcome(self, document: dict[str, Any], principal: Principal) -> dict[str, Any]:
        try:
            parsed = parse_protocol_document(document)
        except ProtocolValidationError as error:
            raise ServiceError(f"v2 schema validation failed: {error}") from error
        if not isinstance(parsed, OutcomeV2):
            raise ServiceError("outcome must use villani.outcome.v2")
        run = self.ingestion.run(principal.organization_id, parsed.run_id)
        if run is None or run.workspace_id != principal.workspace_id:
            raise NotFoundError("run not found")
        if parsed.attempt_id:
            attempt = self.session.get(
                models.Attempt, (principal.organization_id, parsed.attempt_id)
            )
            if attempt is None or attempt.run_id != parsed.run_id:
                raise AuthorizationError("outcome attempt_id is not part of run_id")
        normalized = parsed.model_dump(mode="json")
        existing = (
            self.session.query(models.Outcome)
            .filter_by(
                organization_id=principal.organization_id,
                run_id=parsed.run_id,
                attempt_id=parsed.attempt_id,
            )
            .one_or_none()
        )
        if existing:
            if existing.document != normalized:
                raise ConflictError("outcome already has different content")
            return normalized
        self.ingestion.add(
            models.Outcome(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                run_id=parsed.run_id,
                attempt_id=parsed.attempt_id,
                attempt_key=parsed.attempt_id or "",
                document=normalized,
            )
        )
        if parsed.accepted is True:
            run.status = "accepted"
        self.ingestion.add(
            models.Outbox(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                topic="outcome.recorded",
                aggregate_type="run",
                aggregate_id=parsed.run_id,
                payload={"run_id": parsed.run_id, "attempt_id": parsed.attempt_id},
            )
        )
        self.session.commit()
        return normalized
