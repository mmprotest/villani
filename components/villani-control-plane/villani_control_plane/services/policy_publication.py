from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..errors import ConflictError, NotFoundError, ServiceError
from ..schemas import PolicyPublicationCreateRequest
from ..security import Principal

STATES = {"draft", "shadow", "canary", "active", "paused", "rolled_back"}
ALLOWED = {
    "draft": {"shadow", "paused"},
    "shadow": {"canary", "active", "paused"},
    "canary": {"active", "paused", "rolled_back"},
    "active": {"paused", "rolled_back"},
    "paused": {"shadow", "canary", "active", "rolled_back"},
    "rolled_back": set(),
}
THRESHOLDS = {"success_rate_min", "cost_usd_max", "latency_ms_max", "calibration_error_max"}


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PolicyPublicationService:
    """Metadata-only publication lifecycle; no execution component reads it."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _publication(self, publication_id: str, principal: Principal) -> models.PolicyPublication:
        row = self.session.get(
            models.PolicyPublication, (principal.organization_id, publication_id)
        )
        if row is None or row.workspace_id != principal.workspace_id:
            raise NotFoundError("policy publication not found")
        return row

    def _state(self, publication: models.PolicyPublication) -> str:
        value = self.session.scalar(
            select(models.PolicyPublicationTransition.state)
            .where(
                models.PolicyPublicationTransition.organization_id == publication.organization_id,
                models.PolicyPublicationTransition.publication_id == publication.id,
            )
            .order_by(
                models.PolicyPublicationTransition.created_at.desc(),
                models.PolicyPublicationTransition.id.desc(),
            )
            .limit(1)
        )
        return str(value or "draft")

    def _disabled(self, principal: Principal) -> bool:
        row = self.session.get(
            models.PolicySafetyControl, (principal.organization_id, principal.workspace_id)
        )
        return bool(row and row.globally_disabled)

    def create(
        self, request: PolicyPublicationCreateRequest, principal: Principal
    ) -> dict[str, Any]:
        if not (
            request.evaluation_provenance.assignment_provenance_complete
            and request.evaluation_provenance.propensity_known
        ):
            raise ServiceError(
                "policy publication refused: assignment provenance and propensity must be known"
            )
        unknown = set(request.rollback_thresholds) - THRESHOLDS
        if unknown:
            raise ServiceError(f"unknown rollback thresholds: {','.join(sorted(unknown))}")
        prior = None
        if request.prior_publication_id:
            prior = self._publication(request.prior_publication_id, principal)
            if prior.policy_id != request.policy_id:
                raise ConflictError("prior publication belongs to another policy")
        snapshot_digest = _digest(request.policy_snapshot)
        existing = self.session.scalar(
            select(models.PolicyPublication).where(
                models.PolicyPublication.organization_id == principal.organization_id,
                models.PolicyPublication.workspace_id == principal.workspace_id,
                models.PolicyPublication.policy_id == request.policy_id,
                models.PolicyPublication.policy_version == request.policy_version,
            )
        )
        if existing:
            if existing.snapshot_sha256 != snapshot_digest:
                raise ConflictError("policy version is immutable and has different content")
            return self.get(existing.id, principal)
        row = models.PolicyPublication(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            policy_id=request.policy_id,
            policy_version=request.policy_version,
            policy_snapshot=request.policy_snapshot,
            snapshot_sha256=snapshot_digest,
            prior_publication_id=prior.id if prior else None,
            canary_percentage=request.canary_percentage,
            rollback_thresholds=request.rollback_thresholds,
            evaluation_provenance=request.evaluation_provenance.model_dump(mode="json"),
            manual_approval_required=request.manual_approval_required,
            created_by=principal.token_id,
        )
        self.session.add(row)
        self.session.flush()
        self._append(row, "draft", "publication_created", principal.token_id)
        self.session.commit()
        return self.get(row.id, principal)

    def _append(
        self,
        publication: models.PolicyPublication,
        state: str,
        reason: str,
        actor: str,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            models.PolicyPublicationTransition(
                organization_id=publication.organization_id,
                workspace_id=publication.workspace_id,
                publication_id=publication.id,
                state=state,
                reason=reason,
                actor=actor,
                metrics=metrics or {},
            )
        )

    def approve(
        self, publication_id: str, evidence: dict[str, Any], principal: Principal
    ) -> dict[str, Any]:
        self._publication(publication_id, principal)
        existing = self.session.scalar(
            select(models.PolicyPublicationApproval).where(
                models.PolicyPublicationApproval.organization_id == principal.organization_id,
                models.PolicyPublicationApproval.publication_id == publication_id,
            )
        )
        if existing:
            if existing.evidence != evidence:
                raise ConflictError("manual approval is immutable")
            return self.get(publication_id, principal)
        self.session.add(
            models.PolicyPublicationApproval(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                publication_id=publication_id,
                approved_by=principal.token_id,
                evidence=evidence,
            )
        )
        self.session.commit()
        return self.get(publication_id, principal)

    def transition(
        self, publication_id: str, state: str, reason: str, principal: Principal
    ) -> dict[str, Any]:
        if state not in STATES:
            raise ServiceError("unknown publication state")
        publication = self._publication(publication_id, principal)
        current = self._state(publication)
        if state == current:
            return self.get(publication_id, principal)
        if state not in ALLOWED[current]:
            raise ConflictError(f"illegal publication transition {current} -> {state}")
        if state in {"canary", "active"}:
            if self._disabled(principal):
                raise ConflictError("global policy publication is disabled")
            if publication.manual_approval_required:
                approval = self.session.scalar(
                    select(models.PolicyPublicationApproval.id).where(
                        models.PolicyPublicationApproval.organization_id
                        == principal.organization_id,
                        models.PolicyPublicationApproval.publication_id == publication.id,
                    )
                )
                if approval is None:
                    raise ConflictError("manual approval is required")
            if state == "canary" and not 0 < publication.canary_percentage < 100:
                raise ConflictError("canary state requires a percentage between zero and 100")
        self._append(publication, state, reason, principal.token_id)
        self.session.commit()
        return self.get(publication_id, principal)

    def evaluate_canary(
        self, publication_id: str, metrics: dict[str, float | None], principal: Principal
    ) -> dict[str, Any]:
        publication = self._publication(publication_id, principal)
        if self._state(publication) != "canary":
            raise ConflictError("publication is not in canary state")
        thresholds = publication.rollback_thresholds
        breaches: list[str] = []
        mapping = {
            "success_rate_min": (metrics.get("success_rate"), lambda actual, limit: actual < limit),
            "cost_usd_max": (metrics.get("cost_usd"), lambda actual, limit: actual > limit),
            "latency_ms_max": (metrics.get("latency_ms"), lambda actual, limit: actual > limit),
            "calibration_error_max": (
                metrics.get("calibration_error"),
                lambda actual, limit: actual > limit,
            ),
        }
        for name, (actual, comparator) in mapping.items():
            if name in thresholds and (actual is None or comparator(actual, thresholds[name])):
                breaches.append(name if actual is not None else f"{name}:missing")
        if breaches:
            self._append(
                publication,
                "rolled_back",
                "automatic_threshold_rollback",
                "system",
                {"metrics": metrics, "breaches": breaches},
            )
            restored = None
            if publication.prior_publication_id:
                prior = self._publication(publication.prior_publication_id, principal)
                self._append(prior, "active", f"restored_after_rollback:{publication.id}", "system")
                restored = prior.id
            self.session.commit()
            return {"rolled_back": True, "breaches": breaches, "restored_publication_id": restored}
        return {"rolled_back": False, "breaches": [], "restored_publication_id": None}

    def emergency_disable(
        self, disabled: bool, reason: str, principal: Principal
    ) -> dict[str, Any]:
        row = self.session.get(
            models.PolicySafetyControl, (principal.organization_id, principal.workspace_id)
        )
        if row is None:
            row = models.PolicySafetyControl(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                globally_disabled=disabled,
                reason=reason,
                actor=principal.token_id,
            )
            self.session.add(row)
        else:
            row.globally_disabled, row.reason, row.actor = disabled, reason, principal.token_id
        if disabled:
            publications = list(
                self.session.scalars(
                    select(models.PolicyPublication).where(
                        models.PolicyPublication.organization_id == principal.organization_id,
                        models.PolicyPublication.workspace_id == principal.workspace_id,
                    )
                )
            )
            for publication in publications:
                if self._state(publication) in {"active", "canary"}:
                    self._append(
                        publication, "paused", f"emergency_disable:{reason}", principal.token_id
                    )
        self.session.commit()
        return {"globally_disabled": disabled, "reason": reason, "controls_live_execution": False}

    def get(self, publication_id: str, principal: Principal) -> dict[str, Any]:
        publication = self._publication(publication_id, principal)
        transitions = list(
            self.session.scalars(
                select(models.PolicyPublicationTransition)
                .where(
                    models.PolicyPublicationTransition.organization_id == principal.organization_id,
                    models.PolicyPublicationTransition.publication_id == publication_id,
                )
                .order_by(
                    models.PolicyPublicationTransition.created_at,
                    models.PolicyPublicationTransition.id,
                )
            )
        )
        approval = self.session.scalar(
            select(models.PolicyPublicationApproval).where(
                models.PolicyPublicationApproval.organization_id == principal.organization_id,
                models.PolicyPublicationApproval.publication_id == publication_id,
            )
        )
        return {
            "publication_id": publication.id,
            "policy_id": publication.policy_id,
            "policy_version": publication.policy_version,
            "policy_snapshot": publication.policy_snapshot,
            "snapshot_sha256": publication.snapshot_sha256,
            "prior_publication_id": publication.prior_publication_id,
            "canary_percentage": publication.canary_percentage,
            "rollback_thresholds": publication.rollback_thresholds,
            "evaluation_provenance": publication.evaluation_provenance,
            "manual_approval_required": publication.manual_approval_required,
            "approved": approval is not None,
            "state": self._state(publication),
            "transitions": [
                {
                    "state": item.state,
                    "reason": item.reason,
                    "actor": item.actor,
                    "metrics": item.metrics,
                    "timestamp": item.created_at,
                }
                for item in transitions
            ],
            "globally_disabled": self._disabled(principal),
            "immutable": True,
            "controls_live_execution": False,
        }
