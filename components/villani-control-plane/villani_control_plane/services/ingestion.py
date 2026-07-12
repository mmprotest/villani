from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from villani_ops.closed_loop.protocol_v2 import (
    ArtifactDescriptorV2,
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


def _first(mapping: dict[str, Any], *names: str):
    return next((mapping[name] for name in names if mapping.get(name) is not None), None)


def project_run_observability(run: models.Run, event: TelemetryEnvelopeV2) -> None:
    values = {**event.attributes, **event.body}
    text_fields = {
        "agent_name": ("agent_name", "agent"),
        "model_name": ("model_name", "model"),
        "provider_name": ("provider_name", "provider"),
        "policy_version": ("policy_version",),
        "task_category": ("task_category", "category"),
        "verification_status": ("verification_status", "verification_outcome"),
        "failure_category": ("failure_category", "root_cause_category"),
    }
    for target, names in text_fields.items():
        value = _first(values, *names)
        if isinstance(value, str) and value:
            setattr(run, target, value)
    numeric_fields = {
        "cost_usd": ("total_cost_usd", "cost_usd"),
        "total_tokens": ("total_tokens",),
        "duration_ms": ("duration_ms", "run_duration_ms"),
        "queue_time_ms": ("queue_time_ms",),
        "verifier_cost_usd": ("verifier_cost_usd",),
        "rejected_cost_usd": ("rejected_cost_usd", "wasted_cost_usd"),
    }
    for target, names in numeric_fields.items():
        value = _first(values, *names)
        if isinstance(value, (int, float)) and value >= 0:
            setattr(run, target, value)
    if isinstance(values.get("cost_accounting_status"), str):
        run.cost_accounting_status = values["cost_accounting_status"]
    if isinstance(values.get("token_accounting_status"), str):
        run.token_accounting_status = values["token_accounting_status"]
    if isinstance(values.get("verifier_disagreement"), bool):
        run.verifier_disagreement = values["verifier_disagreement"]
    tags = values.get("tags")
    if isinstance(tags, list):
        normalized = sorted({str(tag).strip() for tag in tags if str(tag).strip()})
        run.tags = normalized
        run.tags_text = "|" + "|".join(normalized) + "|" if normalized else ""
    if event.name == "escalation_selected":
        run.escalation_count += 1


def record_failure_cluster(session: Session, run: models.Run, event: TelemetryEnvelopeV2) -> None:
    if event.name != "run_failed":
        return
    values = {**event.attributes, **event.body}
    category = run.failure_category or str(values.get("failure_category") or "unclassified")
    detail = str(values.get("root_cause") or values.get("message") or event.name)
    normalized = " ".join(detail.lower().split())[:512]
    signature = hashlib.sha256(f"{category}|{normalized}".encode()).hexdigest()
    cluster = session.get(models.FailureCluster, (run.organization_id, signature))
    if cluster is None:
        session.add(
            models.FailureCluster(
                organization_id=run.organization_id,
                workspace_id=run.workspace_id,
                signature=signature,
                failure_category=category,
                deterministic_label=f"{category}: {normalized[:160]}",
                occurrence_count=1,
                first_seen_at=event.occurred_at,
                last_seen_at=event.occurred_at,
            )
        )
    else:
        cluster.occurrence_count += 1
        cluster.last_seen_at = max(cluster.last_seen_at, event.occurred_at)


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
        from ..metrics import metrics
        from .governance import GovernanceService, QuotaService

        quotas = QuotaService(self.session)
        governance = GovernanceService(self.session)
        project_hint = next((event.project_id for event in parsed if event.project_id), None)
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
        normalized = []
        retention_expiries: list[datetime | None] = []
        for event in parsed:
            document = event.model_dump(mode="json")
            name = event.name.lower()
            data_class = (
                "prompt"
                if "prompt" in name
                else "response"
                if "response" in name or "completion" in name
                else "source"
                if "file" in name or "source" in name
                else "metadata"
            )
            governance.enforce_residency(
                principal.organization_id,
                principal.workspace_id,
                event.project_id,
                settings.deployment_region,
                list(event.attributes.get("data_residency_labels", [])),
            )
            decision = governance.govern(
                data_class,
                document,
                principal.organization_id,
                principal.workspace_id,
                event.project_id,
            )
            retention_expiries.append(
                datetime.fromisoformat(decision.expires_at) if decision.expires_at else None
            )
            governed = decision.document
            if governed is None:
                governed = {
                    key: value
                    for key, value in document.items()
                    if key in GovernanceService.METADATA_FIELDS
                }
                governed["governance_excluded"] = data_class
            if settings.metadata_only:
                governed = {
                    key: value
                    for key, value in governed.items()
                    if key in GovernanceService.METADATA_FIELDS
                }
            normalized.append(governed)
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

        # Charge only the transaction that durably owns the batch identity.
        # Concurrent replays return above after the unique batch row serializes them.
        quotas.consume(principal, "events", len(parsed), f"batch:{batch_id}", project_hint)

        inserted = 0
        duplicates = 0
        finalized_run_ids: set[str] = set()
        seen: dict[tuple[str, str], str] = {}
        for event, document, retention_expires_at in zip(
            parsed, normalized, retention_expiries, strict=True
        ):
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
                project_tags = (
                    project.chargeback_tags if hasattr(project, "chargeback_tags") else {}
                )
                quotas.consume(
                    principal, "runs", 1, f"run:{event.run_id}", project.id, project_tags
                )
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
                finalized_run_ids.add(event.run_id)

            project_run_observability(run, event)
            record_failure_cluster(self.session, run, event)

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
                    run.attempt_count += 1
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
                    retention_expires_at=retention_expires_at,
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
        metrics.add("villani_ingest_events_total", inserted, organization=principal.organization_id)
        metrics.add("villani_ingest_batches_total", 1, organization=principal.organization_id)
        if finalized_run_ids:
            from ..tamper import commit_run

            for run_id in sorted(finalized_run_ids):
                commit_run(self.session, principal.organization_id, run_id)
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
        from .outcome_ledger import OutcomeLedgerService

        return OutcomeLedgerService(self.session).record_v2(document, principal)["outcome"]
