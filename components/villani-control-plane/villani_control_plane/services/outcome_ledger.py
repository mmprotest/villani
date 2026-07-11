from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from villani_ops.closed_loop.protocol_v2 import OutcomeV2
from villani_ops.closed_loop.schema_validation import (
    ProtocolValidationError,
    parse_protocol_document,
)

from .. import models
from ..errors import AuthorizationError, ConflictError, NotFoundError, ServiceError
from ..schemas import GitOutcomeWebhook, OutcomeProvenance, ShadowRoutingObservationRequest
from ..security import Principal


def capability_success_label(outcome: OutcomeV2) -> bool | None:
    """Return only acceptance-grade model labels; operational states remain null."""

    failure_category = str(outcome.provenance.get("failure_category") or "")
    if failure_category in {"infrastructure_failure", "verification_failure"}:
        return None
    if outcome.provenance_status != "recorded":
        return None
    if outcome.verification_status == "accepted" and outcome.accepted is True:
        return True
    if outcome.verification_status == "rejected" and outcome.accepted is False:
        return False
    return None


@dataclass(frozen=True, slots=True)
class FakeGitProvider:
    """Deterministic provider used by tests; performs no network operations."""

    name: str = "fake"

    def signals(self, webhook: GitOutcomeWebhook) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "signal_type": event.event_type,
                "state": event.state,
                "source_event_id": f"{webhook.delivery_id}:{event.external_id}",
                "confidence": event.confidence,
                "correction_of_signal_id": event.correction_of_signal_id,
                "provenance": {
                    "contract_version": webhook.contract_version,
                    "repository_id": webhook.repository_id,
                    "attributes": event.attributes,
                },
            }
            for event in webhook.events
        )


class OutcomeLedgerService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _run(self, principal: Principal, run_id: str) -> models.Run:
        run = self.session.get(models.Run, (principal.organization_id, run_id))
        if run is None or run.workspace_id != principal.workspace_id:
            raise NotFoundError("run not found")
        return run

    def record_v2(
        self,
        document: dict[str, Any],
        principal: Principal,
        *,
        provenance: OutcomeProvenance | None = None,
        confidence: float = 1.0,
        corrects_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            parsed = parse_protocol_document(document)
        except ProtocolValidationError as error:
            raise ServiceError(f"v2 schema validation failed: {error}") from error
        if not isinstance(parsed, OutcomeV2):
            raise ServiceError("outcome must use villani.outcome.v2")
        run = self._run(principal, parsed.run_id)
        if parsed.attempt_id:
            attempt = self.session.get(
                models.Attempt, (principal.organization_id, parsed.attempt_id)
            )
            if attempt is None or attempt.run_id != parsed.run_id:
                raise AuthorizationError("outcome attempt_id is not part of run_id")
        normalized = parsed.model_dump(mode="json")
        attempt_key = parsed.attempt_id or ""
        latest = self.session.scalar(
            select(models.Outcome)
            .where(
                models.Outcome.organization_id == principal.organization_id,
                models.Outcome.run_id == parsed.run_id,
                models.Outcome.attempt_key == attempt_key,
            )
            .order_by(models.Outcome.version.desc())
            .limit(1)
        )
        if latest is not None and corrects_version is None:
            if latest.document == normalized:
                return self._result(latest)
            raise ConflictError("outcome differs; submit an explicit correction with provenance")
        if corrects_version is not None:
            if latest is None or latest.version != corrects_version:
                raise ConflictError("correction must identify the current outcome version")
            if provenance is None:
                raise ServiceError("correction provenance is required")
        version = 1 if latest is None else latest.version + 1
        provenance_document = (
            provenance.model_dump(mode="json")
            if provenance is not None
            else {
                "source": parsed.provenance.get("source", "v2_outcome"),
                "source_event_id": f"{parsed.run_id}:{attempt_key}:v{version}",
                "attributes": parsed.provenance,
            }
        )
        row = models.Outcome(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            run_id=parsed.run_id,
            attempt_id=parsed.attempt_id,
            attempt_key=attempt_key,
            version=version,
            supersedes_outcome_id=latest.id if latest else None,
            provenance=provenance_document,
            confidence=confidence,
            capability_success_label=capability_success_label(parsed),
            document=normalized,
        )
        self.session.add(row)
        if parsed.verification_status is not None:
            run.verification_status = parsed.verification_status
        if parsed.cost is not None:
            run.cost_usd = parsed.cost
        run.cost_accounting_status = parsed.cost_accounting_status
        if parsed.latency_ms is not None:
            run.duration_ms = int(parsed.latency_ms)
        if parsed.accepted is True:
            run.status = "accepted"
        self.session.add(
            models.Outbox(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                topic="outcome.version.recorded",
                aggregate_type="run",
                aggregate_id=parsed.run_id,
                payload={
                    "run_id": parsed.run_id,
                    "attempt_id": parsed.attempt_id,
                    "version": version,
                },
            )
        )
        self.session.commit()
        return self._result(row)

    @staticmethod
    def _result(row: models.Outcome) -> dict[str, Any]:
        return {
            "outcome": row.document,
            "version": row.version,
            "supersedes_outcome_id": row.supersedes_outcome_id,
            "provenance": row.provenance,
            "confidence": row.confidence,
            "capability_success_label": row.capability_success_label,
        }

    def ingest_webhook(
        self, webhook: GitOutcomeWebhook, principal: Principal
    ) -> list[dict[str, Any]]:
        self._run(principal, webhook.run_id)
        if webhook.provider != "fake":
            raise ServiceError(
                "no live Git provider is configured; use a registered provider adapter"
            )
        output: list[dict[str, Any]] = []
        for value in FakeGitProvider().signals(webhook):
            existing = self.session.scalar(
                select(models.OutcomeSignal).where(
                    models.OutcomeSignal.organization_id == principal.organization_id,
                    models.OutcomeSignal.source_provider == webhook.provider,
                    models.OutcomeSignal.source_event_id == value["source_event_id"],
                    models.OutcomeSignal.signal_type == value["signal_type"],
                )
            )
            if existing is not None:
                output.append({"id": existing.id, "duplicate": True})
                continue
            correction_id = value["correction_of_signal_id"]
            if correction_id:
                corrected = self.session.get(
                    models.OutcomeSignal, (principal.organization_id, correction_id)
                )
                if corrected is None or corrected.run_id != webhook.run_id:
                    raise ConflictError("signal correction target is not part of this run")
            row = models.OutcomeSignal(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                run_id=webhook.run_id,
                attempt_id=webhook.attempt_id,
                signal_type=value["signal_type"],
                state=value["state"],
                source_provider=webhook.provider,
                source_event_id=value["source_event_id"],
                correction_of_signal_id=correction_id,
                confidence=value["confidence"],
                provenance=value["provenance"],
                observed_at=webhook.observed_at,
            )
            self.session.add(row)
            self.session.flush()
            output.append({"id": row.id, "duplicate": False})
        self.session.commit()
        return output

    def record_shadow(
        self, request: ShadowRoutingObservationRequest, principal: Principal
    ) -> dict[str, Any]:
        self._run(principal, request.run_id)
        existing = self.session.scalar(
            select(models.ShadowRoutingObservation).where(
                models.ShadowRoutingObservation.organization_id == principal.organization_id,
                models.ShadowRoutingObservation.run_id == request.run_id,
                models.ShadowRoutingObservation.recommendation_id == request.recommendation_id,
            )
        )
        normalized = request.model_dump(mode="python")
        if existing:
            current = {key: getattr(existing, key) for key in normalized}
            if current != normalized:
                raise ConflictError("shadow observation is immutable")
            return request.model_dump(mode="json")
        self.session.add(
            models.ShadowRoutingObservation(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                **normalized,
            )
        )
        self.session.commit()
        return request.model_dump(mode="json")

    def metrics(self, principal: Principal) -> dict[str, Any]:
        observations = list(
            self.session.scalars(
                select(models.ShadowRoutingObservation).where(
                    models.ShadowRoutingObservation.organization_id == principal.organization_id,
                    models.ShadowRoutingObservation.workspace_id == principal.workspace_id,
                )
            )
        )
        matched = [item for item in observations if item.shadow_strategy == item.actual_strategy]
        labelled: list[tuple[models.ShadowRoutingObservation, bool]] = []
        for item in observations:
            outcome = self.session.scalar(
                select(models.Outcome)
                .where(
                    models.Outcome.organization_id == principal.organization_id,
                    models.Outcome.run_id == item.run_id,
                    models.Outcome.capability_success_label.is_not(None),
                )
                .order_by(models.Outcome.version.desc())
                .limit(1)
            )
            if outcome is not None:
                labelled.append((item, bool(outcome.capability_success_label)))

        def rate(rows: list[tuple[models.ShadowRoutingObservation, bool]]) -> float | None:
            return sum(success for _, success in rows) / len(rows) if rows else None

        labelled_match = [
            row for row in labelled if row[0].shadow_strategy == row[0].actual_strategy
        ]
        labelled_mismatch = [
            row for row in labelled if row[0].shadow_strategy != row[0].actual_strategy
        ]
        return {
            "observation_count": len(observations),
            "choice_match_count": len(matched),
            "choice_match_rate": len(matched) / len(observations) if observations else None,
            "verified_label_count": len(labelled),
            "verified_success_rate_when_matched": rate(labelled_match),
            "verified_success_rate_when_mismatched": rate(labelled_mismatch),
            "operational_or_unverifiable_outcomes_excluded": True,
        }

    def ledger(self, run_id: str, principal: Principal) -> dict[str, Any]:
        self._run(principal, run_id)
        outcomes = list(
            self.session.scalars(
                select(models.Outcome)
                .where(
                    models.Outcome.organization_id == principal.organization_id,
                    models.Outcome.run_id == run_id,
                )
                .order_by(models.Outcome.attempt_key, models.Outcome.version)
            )
        )
        signals = list(
            self.session.scalars(
                select(models.OutcomeSignal)
                .where(
                    models.OutcomeSignal.organization_id == principal.organization_id,
                    models.OutcomeSignal.run_id == run_id,
                )
                .order_by(models.OutcomeSignal.observed_at, models.OutcomeSignal.id)
            )
        )
        return {
            "run_id": run_id,
            "outcome_versions": [self._result(row) for row in outcomes],
            "signals": [
                {
                    "id": row.id,
                    "attempt_id": row.attempt_id,
                    "signal_type": row.signal_type,
                    "state": row.state,
                    "source_provider": row.source_provider,
                    "source_event_id": row.source_event_id,
                    "correction_of_signal_id": row.correction_of_signal_id,
                    "confidence": row.confidence,
                    "provenance": row.provenance,
                    "observed_at": row.observed_at,
                }
                for row in signals
            ],
        }
