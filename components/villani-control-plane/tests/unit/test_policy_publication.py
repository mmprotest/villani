from __future__ import annotations

import inspect

from conftest import TEST_TOKEN
from fastapi.testclient import TestClient
from villani_ops.closed_loop.controller import ClosedLoopController

from villani_control_plane.database import get_session
from villani_control_plane.main import create_app
from villani_control_plane.services.remote_dispatch import RemoteDispatchService


def _client(session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _create(client: TestClient, headers: dict[str, str], version: str, **updates):
    value = {
        "policy_id": "router",
        "policy_version": version,
        "policy_snapshot": {"segments": {"small": "economy"}, "version": version},
        "canary_percentage": 10,
        "rollback_thresholds": {"success_rate_min": 0.8},
        "manual_approval_required": True,
        "evaluation_provenance": {
            "evaluation_report_digest": "a" * 64,
            "source_dataset_id": "fixture_dataset_v1",
            "assignment_provenance_complete": True,
            "propensity_known": True,
        },
    }
    value.update(updates)
    response = client.post("/v1/policy-publications", headers=headers, json=value)
    assert response.status_code == 201, response.text
    return response.json()


def test_immutable_publication_manual_approval_canary_and_rollback_restore_prior(
    session, principal
) -> None:
    client = _client(session)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    prior = _create(client, headers, "v1", manual_approval_required=False)
    prior_id = prior["publication_id"]
    assert (
        client.post(
            f"/v1/policy-publications/{prior_id}/transition",
            headers=headers,
            json={"state": "shadow", "reason": "offline evidence"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/policy-publications/{prior_id}/transition",
            headers=headers,
            json={"state": "active", "reason": "baseline"},
        ).status_code
        == 200
    )
    candidate = _create(client, headers, "v2", prior_publication_id=prior_id)
    candidate_id = candidate["publication_id"]
    changed = client.post(
        "/v1/policy-publications",
        headers=headers,
        json={
            "policy_id": "router",
            "policy_version": "v2",
            "policy_snapshot": {"changed": True},
            "canary_percentage": 10,
            "rollback_thresholds": {},
            "manual_approval_required": True,
            "evaluation_provenance": {
                "evaluation_report_digest": "b" * 64,
                "source_dataset_id": "fixture_dataset_v1",
                "assignment_provenance_complete": True,
                "propensity_known": True,
            },
        },
    )
    assert changed.status_code == 409
    assert (
        client.post(
            f"/v1/policy-publications/{candidate_id}/approve",
            headers=headers,
            json={"evidence": {"report_digest": "fixture"}},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/policy-publications/{candidate_id}/transition",
            headers=headers,
            json={"state": "shadow", "reason": "offline passed"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/policy-publications/{candidate_id}/transition",
            headers=headers,
            json={"state": "canary", "reason": "approved canary"},
        ).status_code
        == 200
    )
    rollback = client.post(
        f"/v1/policy-publications/{candidate_id}/evaluate-canary",
        headers=headers,
        json={"success_rate": 0.5, "cost_usd": 1, "latency_ms": 100, "calibration_error": 0.1},
    )
    assert rollback.status_code == 200
    assert rollback.json() == {
        "rolled_back": True,
        "breaches": ["success_rate_min"],
        "restored_publication_id": prior_id,
    }
    candidate_after = client.get(f"/v1/policy-publications/{candidate_id}", headers=headers).json()
    prior_after = client.get(f"/v1/policy-publications/{prior_id}", headers=headers).json()
    assert candidate_after["state"] == "rolled_back"
    assert prior_after["state"] == "active"
    assert prior_after["policy_snapshot"] == {"segments": {"small": "economy"}, "version": "v1"}
    assert prior_after["immutable"] is True
    assert prior_after["controls_live_execution"] is False


def test_emergency_disable_pauses_and_blocks_activation(session, principal) -> None:
    client = _client(session)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    publication = _create(client, headers, "disable-v1", manual_approval_required=False)
    publication_id = publication["publication_id"]
    client.post(
        f"/v1/policy-publications/{publication_id}/transition",
        headers=headers,
        json={"state": "shadow", "reason": "test"},
    )
    disabled = client.post(
        "/v1/policy-publications/emergency-disable",
        headers=headers,
        json={"disabled": True, "reason": "operator stop"},
    )
    assert disabled.status_code == 200
    blocked = client.post(
        f"/v1/policy-publications/{publication_id}/transition",
        headers=headers,
        json={"state": "canary", "reason": "must block"},
    )
    assert blocked.status_code == 409
    assert (
        client.get(f"/v1/policy-publications/{publication_id}", headers=headers).json()[
            "globally_disabled"
        ]
        is True
    )


def test_publication_refuses_unknown_assignment_provenance(session, principal) -> None:
    client = _client(session)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    response = client.post(
        "/v1/policy-publications",
        headers=headers,
        json={
            "policy_id": "router",
            "policy_version": "unsafe-v1",
            "policy_snapshot": {"segments": {}},
            "evaluation_provenance": {
                "evaluation_report_digest": "c" * 64,
                "source_dataset_id": "censored_fixture",
                "assignment_provenance_complete": False,
                "propensity_known": False,
            },
        },
    )
    assert response.status_code == 400
    assert "publication refused" in response.json()["message"]


def test_publication_and_offline_evaluation_are_not_live_execution_dependencies() -> None:
    controller_source = inspect.getsource(ClosedLoopController)
    dispatch_source = inspect.getsource(RemoteDispatchService)
    for forbidden in ("PolicyPublicationService", "ExperimentAssignment", "OptimizedPolicy"):
        assert forbidden not in controller_source
        assert forbidden not in dispatch_source
