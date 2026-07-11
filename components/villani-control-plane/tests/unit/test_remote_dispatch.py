from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from villani_ops.closed_loop.schema_validation import parse_protocol_document

from villani_control_plane import models
from villani_control_plane.config import Settings
from villani_control_plane.errors import ConflictError
from villani_control_plane.schemas import (
    RemoteTaskRequest,
    TaskCompletionRequest,
    WorkerCapabilities,
)
from villani_control_plane.security import Principal
from villani_control_plane.services import RemoteDispatchService


def capabilities(
    *,
    residency: list[str] | None = None,
    platform: str = "linux",
    concurrency: int = 1,
) -> WorkerCapabilities:
    return WorkerCapabilities(
        platform=platform,
        architecture="x86_64",
        execution_providers=["container"],
        agent_adapters=["codex"],
        reachable_models=["gpt-5"],
        reachable_runtimes=["python-3.11"],
        cpu_count=8,
        memory_bytes=16 * 1024**3,
        gpus=[],
        concurrency=concurrency,
        network_class="restricted-egress",
        data_residency_labels=residency or ["au-sydney"],
        version="0.1.0",
    )


def setup_dispatch(session, principal):
    now = models.utc_now()
    session.add(
        models.Run(
            organization_id=principal.organization_id,
            id="run_remote",
            workspace_id=principal.workspace_id,
            project_id="project_1",
            repository_id="repo_001",
            trace_id="1234567890abcdef1234567890abcdef",
            status="created",
            first_occurred_at=now,
            first_observed_at=now,
            last_observed_at=now,
        )
    )
    session.add(
        models.AgentInstallation(
            organization_id=principal.organization_id,
            id="installation_1",
            workspace_id=principal.workspace_id,
            agent_name="worker",
        )
    )
    session.add(
        models.AgentInstallation(
            organization_id=principal.organization_id,
            id="installation_2",
            workspace_id=principal.workspace_id,
            agent_name="worker-two",
        )
    )
    session.commit()
    settings = Settings(
        database_url="sqlite://",
        remote_task_lease_seconds=1,
        remote_task_retry_delay_seconds=0,
    )
    service = RemoteDispatchService(session, settings)
    worker_one = Principal(
        "worker-one", principal.organization_id, principal.workspace_id, "installation_1"
    )
    worker_two = Principal(
        "worker-two", principal.organization_id, principal.workspace_id, "installation_2"
    )
    return service, worker_one, worker_two


def task_request(task_id: str = "task_1", *, residency: str = "au-sydney") -> RemoteTaskRequest:
    return RemoteTaskRequest.model_validate(
        {
            "task_id": task_id,
            "submission_idempotency_key": f"submit:{task_id}",
            "run_id": "run_remote",
            "task_input": {"goal": "change one file", "success_criteria": "tests pass"},
            "policy_version": "policy-v1",
            "repository": {"repository_id": "repo_001", "revision": "abc123"},
            "required_capabilities": {
                "platforms": ["linux"],
                "architectures": ["x86_64"],
                "execution_providers": ["container"],
                "agent_adapters": ["codex"],
                "reachable_models": ["gpt-5"],
                "reachable_runtimes": ["python-3.11"],
                "min_cpu_count": 4,
                "min_memory_bytes": 8 * 1024**3,
                "network_classes": ["restricted-egress"],
                "data_residency_labels": [residency],
            },
            "priority": 10,
            "max_attempts": 2,
        }
    )


def register(service, worker_id, principal, value):
    return service.heartbeat(worker_id, value.model_dump(mode="json"), "online", principal)


def test_capability_and_residency_constraints_precede_assignment(session, principal) -> None:
    service, worker_one, worker_two = setup_dispatch(session, principal)
    service.submit(task_request(), principal)
    register(service, "wrong-region", worker_one, capabilities(residency=["us-east"]))
    assert service.claim("wrong-region", worker_one).task is None
    register(service, "wrong-platform", worker_two, capabilities(platform="windows"))
    assert service.claim("wrong-platform", worker_two).task is None
    register(service, "compatible", worker_two, capabilities())
    claimed = service.claim("compatible", worker_two).task
    assert claimed is not None
    assert claimed["task_id"] == "task_1"
    assert claimed["attempt_count"] == 1


def test_expired_lease_reassigns_and_stale_owner_cannot_finalize(session, principal) -> None:
    service, worker_one, worker_two = setup_dispatch(session, principal)
    service.submit(task_request(), principal)
    register(service, "worker-1", worker_one, capabilities())
    register(service, "worker-2", worker_two, capabilities())
    first = service.claim("worker-1", worker_one).task
    assert first is not None
    lease = session.get(models.TaskLease, (principal.organization_id, first["lease_id"]))
    assert lease is not None
    lease.expires_at = models.utc_now() - timedelta(seconds=1)
    session.commit()
    second = service.claim("worker-2", worker_two).task
    assert second is not None and second["lease_id"] != first["lease_id"]
    assert second["attempt_count"] == 2
    with pytest.raises(ConflictError):
        service.complete(
            "task_1",
            first["lease_id"],
            TaskCompletionRequest(
                idempotency_key="complete:old",
                finalization_idempotency_key=first["finalization_idempotency_key"],
                status="succeeded",
                materialized=True,
                finalized=True,
            ),
            worker_one,
        )


def test_completion_is_idempotent_and_finalizes_once(session, principal) -> None:
    service, worker_one, _ = setup_dispatch(session, principal)
    service.submit(task_request(), principal)
    register(service, "worker-1", worker_one, capabilities())
    claimed = service.claim("worker-1", worker_one).task
    assert claimed is not None
    completion = TaskCompletionRequest(
        idempotency_key="completion:task-1",
        finalization_idempotency_key=claimed["finalization_idempotency_key"],
        status="succeeded",
        materialized=True,
        finalized=True,
        result={"patch_digest": "abc"},
    )
    first = service.complete("task_1", claimed["lease_id"], completion, worker_one)
    replay = service.complete("task_1", claimed["lease_id"], completion, worker_one)
    assert first == {"task_id": "task_1", "state": "completed", "replayed": False}
    assert replay == {"task_id": "task_1", "state": "completed", "replayed": True}
    task = session.get(models.RemoteTask, (principal.organization_id, "task_1"))
    assert task is not None and task.materialized and task.finalized
    assert (
        session.scalar(
            select(func.count())
            .select_from(models.TaskLease)
            .where(
                models.TaskLease.organization_id == principal.organization_id,
                models.TaskLease.task_id == "task_1",
            )
        )
        == 1
    )


def test_cancellation_is_returned_on_renewal_and_has_terminal_evidence(session, principal) -> None:
    service, worker_one, _ = setup_dispatch(session, principal)
    service.submit(task_request(), principal)
    register(service, "worker-1", worker_one, capabilities())
    claimed = service.claim("worker-1", worker_one).task
    assert claimed is not None
    service.cancel("task_1", "operator request", principal)
    renewal = service.renew("task_1", claimed["lease_id"], worker_one)
    assert renewal["cancellation_requested"] is True
    assert renewal["cancellation_reason"] == "operator request"
    result = service.complete(
        "task_1",
        claimed["lease_id"],
        TaskCompletionRequest(
            idempotency_key="completion:cancel",
            finalization_idempotency_key=claimed["finalization_idempotency_key"],
            status="cancelled",
            result={"child_terminated": True, "terminal_evidence": "signal"},
        ),
        worker_one,
    )
    assert result["state"] == "cancelled"
    names = list(
        session.scalars(
            select(models.Event.name).where(
                models.Event.organization_id == principal.organization_id,
                models.Event.run_id == "run_remote",
            )
        )
    )
    assert "remote.task.cancellation_requested" in names
    assert "remote.task.cancelled" in names
    for document in session.scalars(
        select(models.Event.document).where(models.Event.run_id == "run_remote")
    ):
        parse_protocol_document(document)


def test_worker_failures_retry_then_dead_letter(session, principal) -> None:
    service, worker_one, _ = setup_dispatch(session, principal)
    service.submit(task_request(), principal)
    register(service, "worker-1", worker_one, capabilities())
    first = service.claim("worker-1", worker_one).task
    assert first is not None
    failed = TaskCompletionRequest(
        idempotency_key="failure:1",
        finalization_idempotency_key=first["finalization_idempotency_key"],
        status="failed",
        result={"failure_class": "child_exit"},
    )
    assert service.complete("task_1", first["lease_id"], failed, worker_one)["state"] == "queued"
    second = service.claim("worker-1", worker_one).task
    assert second is not None and second["attempt_count"] == 2
    failed_again = failed.model_copy(update={"idempotency_key": "failure:2"})
    result = service.complete("task_1", second["lease_id"], failed_again, worker_one)
    assert result["state"] == "dead_letter"
    names = list(
        session.scalars(select(models.Event.name).where(models.Event.run_id == "run_remote"))
    )
    assert "remote.task.retry_scheduled" in names
    assert "remote.task.dead_lettered" in names
