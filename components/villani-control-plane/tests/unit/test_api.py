from __future__ import annotations

from conftest import TEST_TOKEN, load_v2_fixture
from fastapi.testclient import TestClient

from villani_control_plane.database import get_session
from villani_control_plane.main import create_app
from villani_control_plane.models import AgentInstallation
from villani_control_plane.security import hash_token, token_lookup_digest


def client_for(session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_health_build_and_authentication_boundary(session, principal) -> None:
    client = client_for(session)
    assert client.get("/health").json() == {"status": "ok"}
    assert "version" in client.get("/build-version").json()
    assert client.get("/v1/runs").status_code == 401
    assert client.get("/v1/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_ingest_and_query_endpoints(session, principal) -> None:
    client = client_for(session)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    event = load_v2_fixture("telemetry-envelope.json")
    response = client.post(
        "/v1/ingest/batches",
        headers=headers,
        json={"batch_id": "api_batch", "events": [event]},
    )
    assert response.status_code == 200
    assert response.json()["inserted"] == 1
    assert client.get("/v1/runs/run_001", headers=headers).status_code == 200
    page = client.get("/v1/runs/run_001/events?limit=1", headers=headers).json()
    assert [item["event_id"] for item in page["events"]] == [event["event_id"]]
    assert page["events"][0]["occurred_at"] == event["occurred_at"]
    listing = client.get(
        "/v1/runs?project_id=project_1&repository_id=repo_001", headers=headers
    ).json()
    assert [run["id"] for run in listing["runs"]] == ["run_001"]


def test_attempt_lifecycle_and_acknowledged_configuration_projection(
    session, principal
) -> None:
    client = client_for(session)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    base = load_v2_fixture("telemetry-envelope.json")

    def event(
        sequence: int,
        name: str,
        status: str,
        body: dict,
    ) -> dict:
        value = dict(base)
        value.update(
            event_id=f"evt2_attempt_{sequence}",
            idempotency_key=f"attempt-lifecycle:{sequence}",
            sequence=sequence,
            span_id=f"{sequence:016x}",
            attempt_id="attempt_001" if sequence > 1 else None,
            name=name,
            status=status,
            body=body,
        )
        return value

    configuration = {
        "candidate_id": "attempt_001",
        "requested_dimensions": {"prompt_strategy_id": "test_first", "seed": 7},
        "applied_dimensions": {"prompt_strategy_id": "test_first"},
        "unsupported_dimensions": {"seed": 7},
        "rejected_dimensions": {},
        "rendered_prompt_digest": "a" * 64,
        "effective_configuration_digest": "b" * 64,
        "runner_acknowledged": True,
        "acknowledgement_timestamp": "2026-07-11T00:00:02Z",
        "provider_acknowledgement": None,
    }
    events = [
        event(1, "run_created", "ok", {"task_instruction": "fix it"}),
        event(2, "attempt_started", "running", {}),
        event(
            3,
            "attempt_completed",
            "ok",
            {
                "status": "completed",
                "backend_name": "standard",
                "model": "model-a",
                "file_write_count": 1,
                "changed_files": ["example.py"],
                "candidate_configuration": configuration,
                "candidate_configuration_acknowledged": True,
                "effective_configuration_sha256": "b" * 64,
            },
        ),
        event(
            4,
            "verification_completed",
            "ok",
            {
                "outcome": "rejected",
                "acceptance_eligible": False,
                "metadata": {"failure_category": "implementation_failure"},
            },
        ),
    ]

    response = client.post(
        "/v1/ingest/batches",
        headers=headers,
        json={"batch_id": "attempt-lifecycle", "events": events},
    )

    assert response.status_code == 200, response.text
    detail = client.get("/v1/runs/run_001", headers=headers).json()
    assert detail["attempts"] == [{"id": "attempt_001", "status": "completed"}]
    candidate = detail["candidate_outcomes"]["attempt_001"]
    assert candidate["file_write_count"] == 1
    assert candidate["changed_files"] == ["example.py"]
    assert candidate["candidate_configuration"] == configuration
    assert candidate["candidate_configuration_acknowledged"] is True
    assert candidate["failure_category"] == "implementation_failure"


def test_api_rejects_cross_tenant_routing_metadata(session, principal) -> None:
    client = client_for(session)
    event = load_v2_fixture("telemetry-envelope.json")
    event["organization_id"] = "org_other"
    response = client.post(
        "/v1/ingest/batches",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        json={"batch_id": "cross", "events": [event]},
    )
    assert response.status_code == 403


def test_remote_dispatch_endpoints_separate_control_and_worker_authority(
    session, principal
) -> None:
    client = client_for(session)
    control_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    installation_token = "installation-remote-worker-token-long-enough"
    session.add(
        AgentInstallation(
            organization_id=principal.organization_id,
            id="installation-api",
            workspace_id=principal.workspace_id,
            agent_name="remote-worker",
            credential_lookup_digest=token_lookup_digest(installation_token),
            credential_hash=hash_token(installation_token),
        )
    )
    session.commit()
    event = load_v2_fixture("telemetry-envelope.json")
    assert (
        client.post(
            "/v1/ingest/batches",
            headers=control_headers,
            json={"batch_id": "remote-api-run", "events": [event]},
        ).status_code
        == 200
    )
    task = {
        "task_id": "api-task",
        "submission_idempotency_key": "api-task-submit",
        "run_id": event["run_id"],
        "task_input": {"goal": "test pull dispatch"},
        "policy_version": "policy-v1",
        "repository": {"repository_id": "repo_001", "revision": "abc"},
        "required_capabilities": {"data_residency_labels": ["au-sydney"]},
    }
    assert client.post("/v1/tasks", headers=control_headers, json=task).status_code == 201
    worker_headers = {"Authorization": f"Bearer {installation_token}"}
    capability = {
        "platform": "linux",
        "architecture": "x86_64",
        "execution_providers": ["container"],
        "agent_adapters": ["codex"],
        "reachable_models": [],
        "reachable_runtimes": [],
        "cpu_count": 4,
        "memory_bytes": 8 * 1024**3,
        "gpus": [],
        "concurrency": 1,
        "network_class": "restricted-egress",
        "data_residency_labels": ["au-sydney"],
        "version": "test",
    }
    assert (
        client.put(
            "/v1/workers/api-worker/heartbeat",
            headers=worker_headers,
            json={"capabilities": capability, "status": "online"},
        ).status_code
        == 200
    )
    claim = client.post("/v1/workers/api-worker/tasks/claim", headers=worker_headers).json()["task"]
    assert claim["task_id"] == "api-task"
    assert (
        client.put(
            "/v1/workers/control-cannot-work/heartbeat",
            headers=control_headers,
            json={"capabilities": capability, "status": "online"},
        ).status_code
        == 403
    )
    assert client.post("/v1/tasks", headers=worker_headers, json=task).status_code == 403
