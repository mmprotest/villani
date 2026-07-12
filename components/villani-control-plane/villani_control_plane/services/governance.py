from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import AuthorizationError, ConflictError, NotFoundError, RateLimitError
from ..models import (
    Artifact,
    DeletionWorkflow,
    Event,
    GovernanceExport,
    GovernancePolicy,
    LegalHold,
    QuotaPolicy,
    Run,
    UsageRecord,
    utc_now,
)
from ..object_store import ObjectStore
from ..security import Principal, mask_sensitive_fields

DATA_CLASSES = frozenset({"metadata", "prompt", "response", "source", "artifact", "audit"})
QUOTA_METRICS = frozenset(
    {
        "runs",
        "events",
        "artifact_bytes",
        "model_cost",
        "concurrency",
        "workers",
        "exports",
        "queries",
    }
)


class DLPHook(Protocol):
    name: str

    def inspect(self, data_class: str, value: dict[str, Any]) -> dict[str, Any]: ...


class BuiltinDLPHook:
    name = "builtin"
    patterns = (
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    )

    def inspect(self, data_class: str, value: dict[str, Any]) -> dict[str, Any]:
        del data_class
        encoded = json.dumps(value, sort_keys=True)
        for pattern in self.patterns:
            encoded = pattern.sub("***REDACTED***", encoded)
        return json.loads(encoded)


class FakeDLPHook:
    name = "fake"

    def inspect(self, data_class: str, value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        result["dlp_checked"] = data_class
        return result


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    policy_id: str | None
    data_class: str
    retained: bool
    expires_at: str | None
    document: dict[str, Any] | None


class GovernanceService:
    METADATA_FIELDS = frozenset(
        {
            "schema_version",
            "event_id",
            "idempotency_key",
            "organization_id",
            "workspace_id",
            "project_id",
            "repository_id",
            "run_id",
            "attempt_id",
            "trace_id",
            "span_id",
            "parent_span_id",
            "sequence_scope",
            "sequence",
            "occurred_at",
            "observed_at",
            "source",
            "kind",
            "name",
            "status",
            "artifact_id",
            "digest",
            "size_bytes",
            "media_type",
            "sensitivity",
            "retention_class",
            "storage_reference",
        }
    )

    def __init__(self, session: Session, hooks: dict[str, DLPHook] | None = None) -> None:
        self.session = session
        self.hooks = {"builtin": BuiltinDLPHook(), "fake": FakeDLPHook(), **(hooks or {})}

    def resolve(
        self, organization_id: str, workspace_id: str | None, project_id: str | None
    ) -> GovernancePolicy | None:
        policies = list(
            self.session.scalars(
                select(GovernancePolicy).where(
                    GovernancePolicy.organization_id == organization_id,
                    GovernancePolicy.active.is_(True),
                )
            )
        )
        eligible = [
            policy
            for policy in policies
            if (policy.workspace_id is None or policy.workspace_id == workspace_id)
            and (policy.project_id is None or policy.project_id == project_id)
        ]
        return max(
            eligible,
            key=lambda policy: (
                policy.project_id is not None,
                policy.workspace_id is not None,
                policy.version,
            ),
            default=None,
        )

    def create_policy(
        self,
        principal: Principal,
        *,
        workspace_id: str | None,
        project_id: str | None,
        retention_days: dict[str, int],
        metadata_only: bool,
        exclusions: list[str],
        redaction_rules: dict[str, Any],
        dlp_hook: str,
        allowed_regions: list[str],
        required_residency_labels: list[str],
    ) -> GovernancePolicy:
        unknown = (set(retention_days) | set(exclusions)) - DATA_CLASSES
        if unknown:
            raise ConflictError("unknown data classes: " + ", ".join(sorted(unknown)))
        if dlp_hook not in self.hooks:
            raise ConflictError("DLP hook is not registered")
        policy = GovernancePolicy(
            organization_id=principal.organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
            retention_days=retention_days,
            metadata_only=metadata_only,
            exclusions=sorted(set(exclusions)),
            redaction_rules=redaction_rules,
            dlp_hook=dlp_hook,
            allowed_regions=sorted(set(allowed_regions)),
            required_residency_labels=sorted(set(required_residency_labels)),
        )
        self.session.add(policy)
        self.session.commit()
        return policy

    def govern(
        self,
        data_class: str,
        document: dict[str, Any],
        organization_id: str,
        workspace_id: str | None,
        project_id: str | None,
    ) -> GovernanceDecision:
        if data_class not in DATA_CLASSES:
            raise ValueError("unknown governance data class")
        policy = self.resolve(organization_id, workspace_id, project_id)
        if policy is None:
            return GovernanceDecision(None, data_class, True, None, document)
        if data_class in policy.exclusions:
            return GovernanceDecision(policy.id, data_class, False, None, None)
        value = mask_sensitive_fields(document)
        if policy.metadata_only and data_class != "metadata":
            value = {key: item for key, item in value.items() if key in self.METADATA_FIELDS}
        for field, replacement in policy.redaction_rules.items():
            if field in value:
                value[field] = replacement
        hook = self.hooks.get(policy.dlp_hook)
        if hook is None:
            raise AuthorizationError("configured DLP hook is unavailable")
        value = hook.inspect(data_class, value)
        days = policy.retention_days.get(data_class)
        expires = utc_now() + timedelta(days=int(days)) if days is not None else None
        return GovernanceDecision(
            policy.id, data_class, True, expires.isoformat() if expires else None, value
        )

    def enforce_residency(
        self,
        organization_id: str,
        workspace_id: str | None,
        project_id: str | None,
        region: str,
        labels: list[str],
    ) -> None:
        policy = self.resolve(organization_id, workspace_id, project_id)
        if policy is None:
            return
        if policy.allowed_regions and region not in policy.allowed_regions:
            raise AuthorizationError("region is prohibited by data-residency policy")
        if not set(policy.required_residency_labels).issubset(labels):
            raise AuthorizationError("required data-residency labels are missing")

    def place_hold(
        self, principal: Principal, target_type: str, target_id: str, reason: str
    ) -> LegalHold:
        hold = LegalHold(
            organization_id=principal.organization_id,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
        )
        self.session.add(hold)
        self.session.commit()
        return hold

    def request_deletion(
        self, principal: Principal, target_type: str, target_id: str
    ) -> DeletionWorkflow:
        if self.session.scalar(
            select(LegalHold).where(
                LegalHold.organization_id == principal.organization_id,
                LegalHold.target_type == target_type,
                LegalHold.target_id == target_id,
                LegalHold.active.is_(True),
            )
        ):
            raise ConflictError("target is under legal hold")
        workflow = DeletionWorkflow(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            target_type=target_type,
            target_id=target_id,
            requested_by=principal.actor_id,
            tombstone={
                "target_type": target_type,
                "target_id": target_id,
                "requested_at": utc_now().isoformat(),
            },
        )
        self.session.add(workflow)
        self.session.commit()
        return workflow

    def complete_deletion(
        self, principal: Principal, workflow_id: str, store: ObjectStore | None = None
    ) -> DeletionWorkflow:
        workflow = self.session.get(DeletionWorkflow, workflow_id)
        if workflow is None or workflow.organization_id != principal.organization_id:
            raise NotFoundError("deletion workflow not found")
        if workflow.state == "completed":
            return workflow
        deleted_artifacts = 0
        if workflow.target_type == "run":
            run = self.session.get(Run, (principal.organization_id, workflow.target_id))
            if run is None or run.workspace_id != principal.workspace_id:
                raise NotFoundError("run not found")
            for artifact in self.session.scalars(
                select(Artifact).where(
                    Artifact.organization_id == principal.organization_id,
                    Artifact.run_id == run.id,
                )
            ):
                if store:
                    store.delete(artifact.object_key)
                artifact.status = "deleted"
                artifact.document = {"artifact_id": artifact.id, "deleted": True}
                deleted_artifacts += 1
            run.deleted_at = utc_now()
        else:
            raise ConflictError("unsupported deletion target type")
        evidence = {
            "completed_at": utc_now().isoformat(),
            "deleted_artifacts": deleted_artifacts,
            "tombstone_sha256": hashlib.sha256(
                json.dumps(workflow.tombstone, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        workflow.state = "completed"
        workflow.completion_evidence = evidence
        workflow.completed_at = utc_now()
        self.session.commit()
        return workflow

    def sweep_retention(
        self, principal: Principal, store: ObjectStore | None = None
    ) -> dict[str, int]:
        now = utc_now()
        holds = {
            (hold.target_type, hold.target_id)
            for hold in self.session.scalars(
                select(LegalHold).where(
                    LegalHold.organization_id == principal.organization_id,
                    LegalHold.active.is_(True),
                )
            )
        }
        artifacts = 0
        for artifact in self.session.scalars(
            select(Artifact).where(
                Artifact.organization_id == principal.organization_id,
                Artifact.workspace_id == principal.workspace_id,
                Artifact.retention_expires_at.is_not(None),
                Artifact.retention_expires_at <= now,
                Artifact.status != "expired",
            )
        ):
            if ("artifact", artifact.id) in holds or ("run", artifact.run_id) in holds:
                continue
            if store:
                store.delete(artifact.object_key)
            artifact.status = "expired"
            artifact.document = {"artifact_id": artifact.id, "retention_expired": True}
            artifacts += 1
        events = 0
        for event in self.session.scalars(
            select(Event).where(
                Event.organization_id == principal.organization_id,
                Event.workspace_id == principal.workspace_id,
                Event.retention_expires_at.is_not(None),
                Event.retention_expires_at <= now,
            )
        ):
            if ("run", event.run_id) in holds:
                continue
            event.document = {
                "event_id": event.event_id,
                "run_id": event.run_id,
                "retention_expired": True,
                "payload_sha256": event.payload_sha256,
            }
            event.retention_expires_at = None
            events += 1
        self.session.commit()
        return {"events_tombstoned": events, "artifacts_expired": artifacts}

    def create_export(
        self, principal: Principal, project_id: str | None, rows: list[dict[str, Any]]
    ) -> GovernanceExport:
        safe_rows = [mask_sensitive_fields(row) for row in rows]
        manifest = {
            "schema_version": "villani.governance_export.v1",
            "organization_id": principal.organization_id,
            "workspace_id": principal.workspace_id,
            "project_id": project_id,
            "row_count": len(safe_rows),
            "rows": safe_rows,
        }
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        record = GovernanceExport(
            organization_id=principal.organization_id,
            workspace_id=principal.workspace_id,
            project_id=project_id,
            manifest=manifest,
            digest_sha256=digest,
            requested_by=principal.actor_id,
        )
        self.session.add(record)
        self.session.commit()
        return record


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    metric: str
    used: float
    limit: float | None
    warning: bool


class QuotaService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve(
        self, organization_id: str, workspace_id: str | None, project_id: str | None
    ) -> QuotaPolicy | None:
        policies = list(
            self.session.scalars(
                select(QuotaPolicy).where(
                    QuotaPolicy.organization_id == organization_id, QuotaPolicy.active.is_(True)
                )
            )
        )
        eligible = [
            policy
            for policy in policies
            if (policy.workspace_id is None or policy.workspace_id == workspace_id)
            and (policy.project_id is None or policy.project_id == project_id)
        ]
        return max(
            eligible,
            key=lambda policy: (policy.project_id is not None, policy.workspace_id is not None),
            default=None,
        )

    def create_policy(
        self,
        principal: Principal,
        limits: dict[str, float],
        soft_percent: int,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> QuotaPolicy:
        unknown = set(limits) - QUOTA_METRICS
        if unknown:
            raise ConflictError("unknown quota metrics: " + ", ".join(sorted(unknown)))
        if not 1 <= soft_percent <= 100 or any(value < 0 for value in limits.values()):
            raise ConflictError("invalid quota threshold")
        policy = QuotaPolicy(
            organization_id=principal.organization_id,
            workspace_id=workspace_id,
            project_id=project_id,
            limits=limits,
            soft_percent=soft_percent,
        )
        self.session.add(policy)
        self.session.commit()
        return policy

    def consume(
        self,
        principal: Principal,
        metric: str,
        amount: float,
        source_id: str,
        project_id: str | None = None,
        chargeback_tags: dict[str, str] | None = None,
    ) -> QuotaDecision:
        if metric not in QUOTA_METRICS or amount < 0:
            raise ValueError("invalid quota usage")
        existing = self.session.scalar(
            select(UsageRecord).where(
                UsageRecord.organization_id == principal.organization_id,
                UsageRecord.metric == metric,
                UsageRecord.source_id == source_id,
            )
        )
        used = float(
            self.session.scalar(
                select(func.coalesce(func.sum(UsageRecord.amount), 0)).where(
                    UsageRecord.organization_id == principal.organization_id,
                    UsageRecord.workspace_id == principal.workspace_id,
                    UsageRecord.project_id == project_id
                    if project_id
                    else UsageRecord.project_id.is_(None),
                    UsageRecord.metric == metric,
                )
            )
            or 0
        )
        policy = self.resolve(principal.organization_id, principal.workspace_id, project_id)
        limit = float(policy.limits[metric]) if policy and metric in policy.limits else None
        if existing is None and limit is not None and used + amount > limit:
            raise RateLimitError(f"hard quota exceeded for {metric}")
        if existing is None:
            self.session.add(
                UsageRecord(
                    organization_id=principal.organization_id,
                    workspace_id=principal.workspace_id,
                    project_id=project_id,
                    metric=metric,
                    amount=amount,
                    source_id=source_id,
                    chargeback_tags=chargeback_tags or {},
                )
            )
            self.session.flush()
            used += amount
        warning = bool(limit is not None and policy and used >= limit * policy.soft_percent / 100)
        return QuotaDecision(metric, used, limit, warning)

    def enforce_current(
        self,
        principal: Principal,
        metric: str,
        current: float,
        project_id: str | None = None,
    ) -> QuotaDecision:
        if metric not in QUOTA_METRICS:
            raise ValueError("invalid quota metric")
        policy = self.resolve(principal.organization_id, principal.workspace_id, project_id)
        limit = float(policy.limits[metric]) if policy and metric in policy.limits else None
        if limit is not None and current >= limit:
            raise RateLimitError(f"hard quota exceeded for {metric}")
        warning = bool(
            limit is not None and policy and current >= limit * policy.soft_percent / 100
        )
        return QuotaDecision(metric, current, limit, warning)

    def export_usage(self, principal: Principal) -> dict[str, Any]:
        rows = list(
            self.session.scalars(
                select(UsageRecord)
                .where(
                    UsageRecord.organization_id == principal.organization_id,
                    UsageRecord.workspace_id == principal.workspace_id,
                )
                .order_by(UsageRecord.recorded_at, UsageRecord.id)
            )
        )
        return {
            "schema_version": "villani.usage_export.v1",
            "organization_id": principal.organization_id,
            "workspace_id": principal.workspace_id,
            "records": [
                {
                    "metric": row.metric,
                    "amount": row.amount,
                    "project_id": row.project_id,
                    "chargeback_tags": row.chargeback_tags,
                    "source_id": row.source_id,
                    "recorded_at": row.recorded_at.isoformat(),
                }
                for row in rows
            ],
        }
