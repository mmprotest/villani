from __future__ import annotations

import sys

from villani_agentd.config import AgentdPaths, Limits, SyncConfig
from villani_agentd.remote_worker import (
    CheckoutCredentialBroker,
    RemoteExecutionResult,
    RemoteExecutionWorker,
)


class FakeClient:
    def __init__(self, *, cancel: bool = False) -> None:
        self.cancel = cancel
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, path, body, auth=True):
        self.requests.append((method, path, body))
        if path.endswith("/heartbeat"):
            return {"accepted": True}
        if path.endswith("/tasks/claim"):
            return {
                "task": {
                    "task_id": "task-1",
                    "lease_id": "lease-1",
                    "finalization_idempotency_key": "final-1",
                }
            }
        if path.endswith("/renew"):
            return {"cancellation_requested": self.cancel}
        if path.endswith("/complete"):
            return {"state": body["status"]}
        raise AssertionError(path)


def config(**updates) -> SyncConfig:
    values = {
        "endpoint": "http://localhost:8000",
        "installation_id": "installation-1",
        "remote_execution_enabled": True,
        "worker_id": "worker-1",
        "data_residency_labels": ("au-sydney",),
        "network_class": "restricted-egress",
        "lease_renewal_seconds": 0.01,
    }
    values.update(updates)
    return SyncConfig(**values)


def test_worker_pull_completion_uses_server_finalization_key(tmp_path) -> None:
    client = FakeClient()
    worker = RemoteExecutionWorker(
        AgentdPaths(tmp_path / "agentd"),
        config(),
        Limits(),
        client=client,
        capabilities={
            "platform": "linux",
            "architecture": "x86_64",
            "execution_providers": ["container"],
            "agent_adapters": ["codex"],
            "reachable_models": [],
            "reachable_runtimes": [],
            "cpu_count": 2,
            "memory_bytes": 1024,
            "gpus": [],
            "concurrency": 1,
            "network_class": "restricted-egress",
            "data_residency_labels": ["au-sydney"],
            "version": "test",
        },
        executor=lambda task: RemoteExecutionResult(
            "succeeded", True, True, {"patch_digest": "abc"}
        ),
    )
    assert worker.run_once() is True
    completion = next(body for _, path, body in client.requests if path.endswith("/complete"))
    assert completion["idempotency_key"] == "remote-complete:final-1"
    assert completion["finalization_idempotency_key"] == "final-1"
    assert completion["materialized"] is True


def test_server_cancellation_terminates_the_child_and_records_evidence(tmp_path) -> None:
    worker = RemoteExecutionWorker.__new__(RemoteExecutionWorker)
    worker.config = config()
    worker.client = FakeClient(cancel=True)
    result = worker._run_child(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        {"task_id": "task-1", "lease_id": "lease-1"},
    )
    assert result.status == "cancelled"
    assert result.evidence["child_terminated"] is True
    assert any(path.endswith("/renew") for _, path, _ in worker.client.requests)


def test_remote_execution_is_explicitly_disabled_by_default() -> None:
    value = SyncConfig("http://localhost:8000", "installation-1")
    assert value.remote_execution_enabled is False
    assert value.worker_id is None


def test_checkout_secret_is_ephemeral_scoped_and_not_in_durable_report() -> None:
    broker = CheckoutCredentialBroker(
        {"repo-token": [sys.executable, "-c", "print('short-lived-value')"]}
    )
    lease = broker.acquire(
        {
            "broker_reference": "repo-token",
            "scope_repository_id": "repo-1",
            "expires_in_seconds": 300,
        },
        "repo-1",
    )
    assert lease.environment["VILLANI_CHECKOUT_TOKEN"] == "short-lived-value"
    assert "short-lived-value" not in str(lease.durable_report())
    lease.cleanup()
    assert lease.environment == {}
