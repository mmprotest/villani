from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..config import Settings, get_settings
from ..errors import AuthorizationError, ConflictError, NotFoundError, ServiceError
from ..schemas import RemoteTaskRequest, TaskCompletionRequest
from ..security import Principal
from .ingestion import digest_document

TERMINAL_TASK_STATES = {"completed", "cancelled", "dead_letter"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _now_text(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _span_id() -> str:
    value = secrets.token_hex(8)
    return value if value != "0" * 16 else "1" + value[1:]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class ClaimResult:
    task: dict[str, Any] | None


class RemoteDispatchService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    @staticmethod
    def _require_installation(principal: Principal) -> str:
        if principal.installation_id is None:
            raise AuthorizationError("installation credential required")
        return principal.installation_id

    @staticmethod
    def _require_control_principal(principal: Principal) -> None:
        if principal.installation_id is not None:
            raise AuthorizationError("control-plane API token required")

    def _run(self, task: models.RemoteTask) -> models.Run:
        run = self.session.get(models.Run, (task.organization_id, task.run_id))
        if run is None:
            raise NotFoundError("task run not found")
        return run

    def _record_event(
        self,
        task: models.RemoteTask,
        name: str,
        status: str,
        attributes: dict[str, Any],
        *,
        span_id: str | None = None,
    ) -> None:
        run = self._run(task)
        now = models.utc_now()
        task.event_sequence += 1
        event_id = f"remote_{uuid4()}"
        selected_span = span_id or task.lifecycle_span_id
        document = {
            "schema_version": "villani.telemetry_envelope.v2",
            "event_id": event_id,
            "idempotency_key": f"remote:{task.id}:{task.event_sequence}:{name}",
            "occurred_at": _now_text(now),
            "observed_at": _now_text(now),
            "sequence": task.event_sequence,
            "sequence_scope": f"remote-task:{task.id}",
            "organization_id": task.organization_id,
            "workspace_id": task.workspace_id,
            "project_id": run.project_id,
            "repository_id": task.repository_id,
            "run_id": task.run_id,
            "trace_id": run.trace_id,
            "span_id": selected_span,
            "parent_span_id": (
                task.lifecycle_span_id if selected_span != task.lifecycle_span_id else None
            ),
            "attempt_id": None,
            "source": "villani-control-plane",
            "kind": "remote_dispatch",
            "name": name,
            "status": status,
            "resource": {
                "schema_version": "villani.resource.v2",
                "service_name": "villani-control-plane",
                "service_version": self.settings.build_version,
                "deployment_environment": "single-region",
                "host_id": None,
                "process_id": None,
                "attributes": {},
            },
            "attributes": attributes,
            "body": {"message": name},
        }
        self.session.add(
            models.Event(
                organization_id=task.organization_id,
                event_id=event_id,
                idempotency_key=document["idempotency_key"],
                workspace_id=task.workspace_id,
                project_id=run.project_id,
                repository_id=task.repository_id,
                run_id=task.run_id,
                attempt_id=None,
                trace_id=run.trace_id,
                span_id=selected_span,
                sequence_scope=document["sequence_scope"],
                sequence=task.event_sequence,
                occurred_at=now,
                observed_at=now,
                source=document["source"],
                kind=document["kind"],
                name=name,
                status=status,
                payload_sha256=digest_document(document),
                document=document,
            )
        )
        self.session.add(
            models.Outbox(
                organization_id=task.organization_id,
                workspace_id=task.workspace_id,
                topic=f"runs.{task.run_id}.events",
                aggregate_type="remote_task",
                aggregate_id=task.id,
                payload=document,
            )
        )

    def _close_span(self, task: models.RemoteTask, span_id: str, status: str) -> None:
        run = self._run(task)
        span = self.session.get(models.Span, (task.organization_id, run.trace_id, span_id))
        if span is not None and span.ended_at is None:
            span.status = status
            span.ended_at = models.utc_now()

    def _serialize_task(self, task: models.RemoteTask) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "run_id": task.run_id,
            "repository": task.repository_reference,
            "task_input": task.task_input,
            "policy_version": task.policy_version,
            "required_capabilities": task.required_capabilities,
            "priority": task.priority,
            "deadline": _now_text(task.deadline) if task.deadline else None,
            "attempt_count": task.attempt_count,
            "max_attempts": task.max_attempts,
            "state": task.state,
            "finalization_idempotency_key": task.finalization_idempotency_key,
        }

    def submit(self, request: RemoteTaskRequest, principal: Principal) -> dict[str, Any]:
        self._require_control_principal(principal)
        now = models.utc_now()
        if request.deadline is not None and _utc(request.deadline) <= now:
            raise ServiceError("task deadline must be in the future")
        run = self.session.get(models.Run, (principal.organization_id, request.run_id))
        repository = self.session.get(
            models.Repository,
            (principal.organization_id, request.repository.repository_id),
        )
        if (
            run is None
            or repository is None
            or run.workspace_id != principal.workspace_id
            or repository.workspace_id != principal.workspace_id
            or run.repository_id != repository.id
        ):
            raise AuthorizationError("run and repository must belong to the token workspace")
        supplied_url = request.repository.checkout_url
        if supplied_url and supplied_url != repository.canonical_url:
            raise AuthorizationError("checkout URL must match the registered repository")
        checkout_url = repository.canonical_url or supplied_url
        if checkout_url and (urlsplit(checkout_url).username or urlsplit(checkout_url).password):
            raise ServiceError("repository URLs must not contain credentials")
        secret_reference = request.repository.checkout_secret
        if secret_reference and secret_reference.scope_repository_id != repository.id:
            raise AuthorizationError("checkout credential scope does not match repository")
        repository_reference = request.repository.model_dump(mode="json")
        repository_reference["checkout_url"] = checkout_url
        normalized = {
            "task_id": request.task_id,
            "run_id": request.run_id,
            "task_input": request.task_input,
            "policy_version": request.policy_version,
            "repository": repository_reference,
            "required_capabilities": request.required_capabilities.model_dump(mode="json"),
            "priority": request.priority,
            "deadline": _now_text(request.deadline) if request.deadline else None,
            "max_attempts": request.max_attempts,
        }
        payload_digest = digest_document(normalized)
        existing = self.session.scalar(
            select(models.RemoteTask).where(
                models.RemoteTask.organization_id == principal.organization_id,
                models.RemoteTask.submission_idempotency_key == request.submission_idempotency_key,
            )
        )
        if existing is not None:
            if existing.task_input_sha256 != payload_digest:
                raise ConflictError("task submission idempotency key has different content")
            return {**self._serialize_task(existing), "replayed": True}
        existing_id = self.session.get(
            models.RemoteTask, (principal.organization_id, request.task_id)
        )
        if existing_id is not None:
            raise ConflictError("task_id already exists")
        lifecycle_span = _span_id()
        task = models.RemoteTask(
            organization_id=principal.organization_id,
            id=request.task_id,
            workspace_id=principal.workspace_id,
            run_id=request.run_id,
            repository_id=repository.id,
            repository_reference=repository_reference,
            submission_idempotency_key=request.submission_idempotency_key,
            task_input=request.task_input,
            task_input_sha256=payload_digest,
            policy_version=request.policy_version,
            required_capabilities=request.required_capabilities.model_dump(mode="json"),
            priority=request.priority,
            deadline=request.deadline,
            max_attempts=request.max_attempts,
            attempt_count=0,
            state="queued",
            next_eligible_at=now,
            finalization_idempotency_key=f"remote-finalize:{uuid4()}",
            lifecycle_span_id=lifecycle_span,
            event_sequence=0,
            materialized=False,
            finalized=False,
        )
        self.session.add(task)
        self.session.add(
            models.Span(
                organization_id=task.organization_id,
                trace_id=run.trace_id,
                span_id=lifecycle_span,
                run_id=run.id,
                attempt_id=None,
                parent_span_id=None,
                kind="remote_dispatch",
                name="remote.task.lifecycle",
                status="running",
                started_at=now,
                ended_at=None,
                attributes={"task_id": task.id, "policy_version": task.policy_version},
            )
        )
        self._record_event(task, "remote.task.queued", "ok", {"priority": task.priority})
        self.session.commit()
        return {**self._serialize_task(task), "replayed": False}

    def heartbeat(
        self,
        worker_id: str,
        capabilities: dict[str, Any],
        status: str,
        principal: Principal,
    ) -> dict[str, Any]:
        installation_id = self._require_installation(principal)
        now = models.utc_now()
        digest = hashlib.sha256(_canonical(capabilities).encode()).hexdigest()
        worker = self.session.get(models.Worker, (principal.organization_id, worker_id))
        if worker is None:
            worker = models.Worker(
                organization_id=principal.organization_id,
                id=worker_id,
                workspace_id=principal.workspace_id,
                installation_id=installation_id,
                status=status,
                version=str(capabilities["version"]),
                capabilities=capabilities,
                capabilities_sha256=digest,
                concurrency=int(capabilities["concurrency"]),
                active_leases=0,
                last_heartbeat_at=now,
            )
            self.session.add(worker)
        elif (
            worker.installation_id != installation_id
            or worker.workspace_id != principal.workspace_id
        ):
            raise AuthorizationError("worker belongs to another installation or workspace")
        else:
            worker.status = status
            worker.version = str(capabilities["version"])
            worker.capabilities = capabilities
            worker.capabilities_sha256 = digest
            worker.concurrency = int(capabilities["concurrency"])
            worker.last_heartbeat_at = now
            worker.deleted_at = None
        active = int(
            self.session.scalar(
                select(func.count())
                .select_from(models.TaskLease)
                .where(
                    models.TaskLease.organization_id == principal.organization_id,
                    models.TaskLease.worker_id == worker_id,
                    models.TaskLease.state == "active",
                    models.TaskLease.expires_at > now,
                )
            )
            or 0
        )
        worker.active_leases = active
        self.session.flush()
        self.session.add(
            models.WorkerHeartbeat(
                organization_id=principal.organization_id,
                workspace_id=principal.workspace_id,
                worker_id=worker_id,
                observed_at=now,
                status=status,
                active_leases=active,
                capabilities_sha256=digest,
            )
        )
        self.session.commit()
        return {"worker_id": worker_id, "active_leases": active, "accepted": True}

    @staticmethod
    def _matches(capabilities: dict[str, Any], required: dict[str, Any]) -> bool:
        def contains_all(capability_name: str, requirement_name: str | None = None) -> bool:
            wanted = set(required.get(requirement_name or capability_name) or [])
            return wanted.issubset(set(capabilities.get(capability_name) or []))

        if required.get("platforms") and capabilities.get("platform") not in required["platforms"]:
            return False
        if (
            required.get("architectures")
            and capabilities.get("architecture") not in required["architectures"]
        ):
            return False
        if not contains_all("execution_providers"):
            return False
        if not contains_all("agent_adapters"):
            return False
        if not contains_all("reachable_models") or not contains_all("reachable_runtimes"):
            return False
        if float(capabilities.get("cpu_count", 0)) < float(required.get("min_cpu_count", 0)):
            return False
        if int(capabilities.get("memory_bytes", 0)) < int(required.get("min_memory_bytes", 0)):
            return False
        if (
            required.get("network_classes")
            and capabilities.get("network_class") not in required["network_classes"]
        ):
            return False
        if not contains_all("data_residency_labels"):
            return False
        if required.get("gpu_required"):
            vendors = set(required.get("gpu_vendors") or [])
            minimum = int(required.get("min_gpu_memory_bytes", 0))
            if not any(
                (not vendors or gpu.get("vendor") in vendors)
                and int(gpu.get("memory_bytes") or 0) >= minimum
                for gpu in capabilities.get("gpus") or []
            ):
                return False
        return True

    def _expire_leases(self, organization_id: str, workspace_id: str, now: datetime) -> None:
        leases = list(
            self.session.scalars(
                select(models.TaskLease)
                .where(
                    models.TaskLease.organization_id == organization_id,
                    models.TaskLease.workspace_id == workspace_id,
                    models.TaskLease.state == "active",
                    models.TaskLease.expires_at <= now,
                )
                .order_by(models.TaskLease.expires_at)
                .limit(self.settings.remote_task_claim_candidates)
                .with_for_update(skip_locked=True)
            )
        )
        for lease in leases:
            task = self.session.scalar(
                select(models.RemoteTask)
                .where(
                    models.RemoteTask.organization_id == organization_id,
                    models.RemoteTask.id == lease.task_id,
                )
                .with_for_update()
            )
            if task is None or lease.state != "active":
                continue
            lease.state = "expired"
            lease.ended_at = now
            worker = self.session.get(models.Worker, (organization_id, lease.worker_id))
            if worker is not None:
                worker.active_leases = max(0, worker.active_leases - 1)
            self._close_span(task, lease.span_id, "error")
            self._record_event(
                task,
                "remote.task.lease_expired",
                "error",
                {"lease_id": lease.id, "worker_id": lease.worker_id},
                span_id=lease.span_id,
            )
            if task.cancellation_requested_at is not None:
                task.state = "cancelled"
                task.completed_at = now
                task.terminal_reason = "cancelled after worker lease expired"
                self._close_span(task, task.lifecycle_span_id, "cancelled")
            elif task.attempt_count >= task.max_attempts or (
                task.deadline is not None and _utc(task.deadline) <= now
            ):
                task.state = "dead_letter"
                task.completed_at = now
                task.terminal_reason = "lease expired and retry budget exhausted"
                self._record_event(task, "remote.task.dead_lettered", "error", {})
                self._close_span(task, task.lifecycle_span_id, "error")
            else:
                task.state = "queued"
                task.next_eligible_at = now + timedelta(
                    seconds=self.settings.remote_task_retry_delay_seconds
                )
                self._record_event(
                    task,
                    "remote.task.retry_scheduled",
                    "ok",
                    {"attempt_count": task.attempt_count},
                )

    def _expire_deadlines(self, organization_id: str, workspace_id: str, now: datetime) -> None:
        tasks = list(
            self.session.scalars(
                select(models.RemoteTask)
                .where(
                    models.RemoteTask.organization_id == organization_id,
                    models.RemoteTask.workspace_id == workspace_id,
                    models.RemoteTask.state == "queued",
                    models.RemoteTask.deadline.is_not(None),
                    models.RemoteTask.deadline <= now,
                )
                .limit(self.settings.remote_task_claim_candidates)
                .with_for_update(skip_locked=True)
            )
        )
        for task in tasks:
            task.state = "dead_letter"
            task.completed_at = now
            task.terminal_reason = "deadline elapsed before assignment"
            self._record_event(task, "remote.task.dead_lettered", "error", {"deadline": True})
            self._close_span(task, task.lifecycle_span_id, "error")

    def claim(self, worker_id: str, principal: Principal) -> ClaimResult:
        installation_id = self._require_installation(principal)
        now = models.utc_now()
        self._expire_leases(principal.organization_id, principal.workspace_id, now)
        self._expire_deadlines(principal.organization_id, principal.workspace_id, now)
        worker = self.session.scalar(
            select(models.Worker)
            .where(
                models.Worker.organization_id == principal.organization_id,
                models.Worker.id == worker_id,
            )
            .with_for_update()
        )
        stale_before = now - timedelta(seconds=self.settings.worker_heartbeat_stale_seconds)
        if (
            worker is None
            or worker.installation_id != installation_id
            or worker.workspace_id != principal.workspace_id
        ):
            raise AuthorizationError("worker is not registered to this installation")
        if worker.status != "online" or _utc(worker.last_heartbeat_at) < stale_before:
            raise ConflictError("worker heartbeat is stale or worker is not accepting tasks")
        active = int(
            self.session.scalar(
                select(func.count())
                .select_from(models.TaskLease)
                .where(
                    models.TaskLease.organization_id == principal.organization_id,
                    models.TaskLease.worker_id == worker.id,
                    models.TaskLease.state == "active",
                    models.TaskLease.expires_at > now,
                )
            )
            or 0
        )
        worker.active_leases = active
        if active >= worker.concurrency:
            self.session.commit()
            return ClaimResult(None)
        candidates = list(
            self.session.scalars(
                select(models.RemoteTask)
                .where(
                    models.RemoteTask.organization_id == principal.organization_id,
                    models.RemoteTask.workspace_id == principal.workspace_id,
                    models.RemoteTask.state == "queued",
                    models.RemoteTask.next_eligible_at <= now,
                    (models.RemoteTask.deadline.is_(None) | (models.RemoteTask.deadline > now)),
                )
                .order_by(models.RemoteTask.priority.desc(), models.RemoteTask.created_at)
                .limit(self.settings.remote_task_claim_candidates)
                .with_for_update(skip_locked=True)
            )
        )
        task = next(
            (
                candidate
                for candidate in candidates
                if self._matches(worker.capabilities, candidate.required_capabilities)
            ),
            None,
        )
        if task is None:
            self.session.commit()
            return ClaimResult(None)
        task.attempt_count += 1
        task.state = "leased"
        span_id = _span_id()
        expires = now + timedelta(seconds=self.settings.remote_task_lease_seconds)
        lease = models.TaskLease(
            organization_id=task.organization_id,
            workspace_id=task.workspace_id,
            task_id=task.id,
            worker_id=worker.id,
            installation_id=installation_id,
            attempt_number=task.attempt_count,
            state="active",
            acquired_at=now,
            renewed_at=now,
            expires_at=expires,
            span_id=span_id,
        )
        self.session.add(lease)
        self.session.flush()
        run = self._run(task)
        self.session.add(
            models.Span(
                organization_id=task.organization_id,
                trace_id=run.trace_id,
                span_id=span_id,
                run_id=run.id,
                attempt_id=None,
                parent_span_id=task.lifecycle_span_id,
                kind="remote_dispatch",
                name="remote.task.lease",
                status="running",
                started_at=now,
                ended_at=None,
                attributes={
                    "task_id": task.id,
                    "worker_id": worker.id,
                    "attempt_number": task.attempt_count,
                },
            )
        )
        worker.active_leases = active + 1
        assignment_attributes = {
            "lease_id": lease.id,
            "worker_id": worker.id,
            "attempt_number": task.attempt_count,
            "policy_version": task.policy_version,
            "capability_constraints_satisfied": True,
            "worker_data_residency_labels": worker.capabilities.get("data_residency_labels", []),
        }
        self._record_event(
            task,
            "remote.task.dispatched",
            "ok",
            assignment_attributes,
            span_id=span_id,
        )
        self._record_event(
            task,
            "remote.task.leased",
            "ok",
            assignment_attributes,
            span_id=span_id,
        )
        self.session.commit()
        payload = self._serialize_task(task)
        payload.update(
            {
                "lease_id": lease.id,
                "lease_expires_at": _now_text(expires),
                "worker_id": worker.id,
            }
        )
        return ClaimResult(payload)

    def _owned_lease(
        self, task_id: str, lease_id: str, principal: Principal
    ) -> tuple[models.TaskLease, models.RemoteTask]:
        installation_id = self._require_installation(principal)
        lease = self.session.scalar(
            select(models.TaskLease)
            .where(
                models.TaskLease.organization_id == principal.organization_id,
                models.TaskLease.id == lease_id,
                models.TaskLease.task_id == task_id,
            )
            .with_for_update()
        )
        if lease is None or lease.installation_id != installation_id:
            raise NotFoundError("task lease not found")
        task = self.session.scalar(
            select(models.RemoteTask)
            .where(
                models.RemoteTask.organization_id == principal.organization_id,
                models.RemoteTask.id == task_id,
            )
            .with_for_update()
        )
        if task is None or task.workspace_id != principal.workspace_id:
            raise NotFoundError("task not found")
        return lease, task

    def renew(self, task_id: str, lease_id: str, principal: Principal) -> dict[str, Any]:
        lease, task = self._owned_lease(task_id, lease_id, principal)
        now = models.utc_now()
        if lease.state != "active" or _utc(lease.expires_at) <= now:
            raise ConflictError("task lease is no longer active")
        seconds = (
            self.settings.remote_task_cancellation_grace_seconds
            if task.cancellation_requested_at is not None
            else self.settings.remote_task_lease_seconds
        )
        lease.renewed_at = now
        lease.expires_at = now + timedelta(seconds=seconds)
        self._record_event(
            task,
            "remote.task.lease_renewed",
            "ok",
            {
                "lease_id": lease.id,
                "cancellation_requested": task.cancellation_requested_at is not None,
            },
            span_id=lease.span_id,
        )
        self.session.commit()
        return {
            "lease_id": lease.id,
            "expires_at": _now_text(lease.expires_at),
            "cancellation_requested": task.cancellation_requested_at is not None,
            "cancellation_reason": task.cancellation_reason,
        }

    def cancel(self, task_id: str, reason: str, principal: Principal) -> dict[str, Any]:
        self._require_control_principal(principal)
        task = self.session.scalar(
            select(models.RemoteTask)
            .where(
                models.RemoteTask.organization_id == principal.organization_id,
                models.RemoteTask.id == task_id,
                models.RemoteTask.workspace_id == principal.workspace_id,
            )
            .with_for_update()
        )
        if task is None:
            raise NotFoundError("task not found")
        if task.state in TERMINAL_TASK_STATES:
            return {"task_id": task.id, "state": task.state, "replayed": True}
        if task.cancellation_requested_at is None:
            task.cancellation_requested_at = models.utc_now()
            task.cancellation_reason = reason
            if task.state == "queued":
                task.state = "cancelled"
                task.completed_at = models.utc_now()
                task.terminal_reason = reason
                self._close_span(task, task.lifecycle_span_id, "cancelled")
            self._record_event(
                task,
                "remote.task.cancellation_requested",
                "cancelled",
                {"reason": reason},
            )
        self.session.commit()
        return {"task_id": task.id, "state": task.state, "replayed": False}

    def complete(
        self,
        task_id: str,
        lease_id: str,
        request: TaskCompletionRequest,
        principal: Principal,
    ) -> dict[str, Any]:
        lease, task = self._owned_lease(task_id, lease_id, principal)
        document = request.model_dump(mode="json")
        completion_digest = digest_document(document)
        if lease.completion_idempotency_key is not None:
            if (
                lease.completion_idempotency_key == request.idempotency_key
                and lease.completion_sha256 == completion_digest
            ):
                return {"task_id": task.id, "state": task.state, "replayed": True}
            raise ConflictError("lease completion identity already has different content")
        now = models.utc_now()
        if lease.state != "active" or _utc(lease.expires_at) <= now:
            raise ConflictError("task lease is no longer active")
        if request.finalization_idempotency_key != task.finalization_idempotency_key:
            raise ConflictError("task finalization idempotency key does not match")
        if request.status == "succeeded" and not (request.materialized and request.finalized):
            raise ServiceError("successful completion requires materialized and finalized evidence")
        lease.completion_idempotency_key = request.idempotency_key
        lease.completion_sha256 = completion_digest
        lease.ended_at = now
        terminal = True
        if task.cancellation_requested_at is not None:
            lease.state = "cancelled"
            task.state = "cancelled"
            task.completion_idempotency_key = request.idempotency_key
            task.completion_sha256 = completion_digest
            task.completion = document
            task.completed_at = now
            task.terminal_reason = task.cancellation_reason or "worker reported cancellation"
        elif request.status == "succeeded":
            if task.completion_idempotency_key is not None:
                if (
                    task.completion_idempotency_key == request.idempotency_key
                    and task.completion_sha256 == completion_digest
                ):
                    return {"task_id": task.id, "state": task.state, "replayed": True}
                raise ConflictError("task was already finalized with different completion")
            lease.state = "completed"
            task.state = "completed"
            task.materialized = True
            task.finalized = True
            task.completion_idempotency_key = request.idempotency_key
            task.completion_sha256 = completion_digest
            task.completion = document
            task.completed_at = now
        elif request.status == "cancelled":
            lease.state = "cancelled"
            task.state = "cancelled"
            task.completion_idempotency_key = request.idempotency_key
            task.completion_sha256 = completion_digest
            task.completion = document
            task.completed_at = now
            task.terminal_reason = task.cancellation_reason or "worker reported cancellation"
        else:
            lease.state = "failed"
            if task.attempt_count >= task.max_attempts or (
                task.deadline is not None and _utc(task.deadline) <= now
            ):
                task.state = "dead_letter"
                task.completed_at = now
                task.terminal_reason = "worker failure exhausted retry budget"
            else:
                terminal = False
                task.state = "queued"
                task.next_eligible_at = now + timedelta(
                    seconds=self.settings.remote_task_retry_delay_seconds
                )
        transition_name = {
            "completed": "remote.task.completed",
            "cancelled": "remote.task.cancelled",
            "dead_letter": "remote.task.dead_lettered",
        }.get(task.state, "remote.task.retry_scheduled")
        transition_status = (
            "ok"
            if task.state == "completed"
            else "cancelled"
            if task.state == "cancelled"
            else "error"
        )
        self._record_event(
            task,
            transition_name,
            transition_status,
            {
                "lease_id": lease.id,
                "worker_id": lease.worker_id,
                "reported_status": request.status,
                "terminal_state": task.state,
                "materialized": request.materialized,
                "finalized": request.finalized,
            },
            span_id=lease.span_id,
        )
        self._close_span(task, lease.span_id, "ok" if task.state == "completed" else "error")
        if terminal:
            self._close_span(
                task,
                task.lifecycle_span_id,
                "ok" if task.state == "completed" else task.state,
            )
        worker = self.session.get(models.Worker, (task.organization_id, lease.worker_id))
        if worker is not None:
            worker.active_leases = max(0, worker.active_leases - 1)
        self.session.commit()
        return {"task_id": task.id, "state": task.state, "replayed": False}
