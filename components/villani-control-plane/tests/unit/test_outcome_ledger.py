from __future__ import annotations

from conftest import TEST_TOKEN, load_v2_fixture
from fastapi.testclient import TestClient

from villani_control_plane import models
from villani_control_plane.database import get_session
from villani_control_plane.main import create_app


def _client(session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _seed_run(client: TestClient, headers: dict[str, str]) -> None:
    event = load_v2_fixture("telemetry-envelope.json")
    response = client.post(
        "/v1/ingest/batches", headers=headers, json={"batch_id": "ledger-run", "events": [event]}
    )
    assert response.status_code == 200


def test_outcome_corrections_append_and_unverified_success_is_not_labelled(
    session, principal
) -> None:
    client = _client(session)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    _seed_run(client, headers)
    outcome = load_v2_fixture("outcome.json")
    outcome["attempt_id"] = None
    outcome["verification_status"] = "unclear"
    outcome["accepted"] = True
    request = {
        "outcome": outcome,
        "provenance": {
            "source": "fake-ci",
            "source_event_id": "evt-1",
            "observed_at": "2026-07-11T00:00:00Z",
        },
        "confidence": 0.7,
    }
    first = client.post("/v1/outcome-ledger/outcomes", headers=headers, json=request)
    assert first.status_code == 201
    assert first.json()["version"] == 1
    assert first.json()["capability_success_label"] is None

    changed = dict(outcome)
    changed["ci_state"] = "passed"
    conflict = client.post(
        "/v1/outcome-ledger/outcomes", headers=headers, json={**request, "outcome": changed}
    )
    assert conflict.status_code == 409
    correction = client.post(
        "/v1/outcome-ledger/outcomes",
        headers=headers,
        json={
            "outcome": changed,
            "provenance": {
                "source": "fake-ci",
                "source_event_id": "evt-2",
                "observed_at": "2026-07-11T00:01:00Z",
            },
            "confidence": 0.9,
            "corrects_version": 1,
        },
    )
    assert correction.status_code == 201
    assert correction.json()["version"] == 2
    assert correction.json()["supersedes_outcome_id"] is not None
    ledger = client.get("/v1/outcome-ledger/runs/run_001", headers=headers).json()
    assert [item["version"] for item in ledger["outcome_versions"]] == [1, 2]


def test_fake_git_provider_signals_and_shadow_metrics_use_verified_labels(
    session, principal
) -> None:
    client = _client(session)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    _seed_run(client, headers)
    webhook = {
        "provider": "fake",
        "delivery_id": "delivery-1",
        "repository_id": "repo_001",
        "run_id": "run_001",
        "attempt_id": None,
        "observed_at": "2026-07-11T00:00:00Z",
        "events": [
            {"event_type": "ci", "state": "passed", "external_id": "ci-1", "confidence": 1.0},
            {"event_type": "merge", "state": "merged", "external_id": "merge-1", "confidence": 1.0},
        ],
    }
    response = client.post("/v1/outcome-ledger/git-webhooks", headers=headers, json=webhook)
    assert response.status_code == 201
    assert len(response.json()["signals"]) == 2
    replay = client.post("/v1/outcome-ledger/git-webhooks", headers=headers, json=webhook)
    assert all(item["duplicate"] for item in replay.json()["signals"])

    outcome = load_v2_fixture("outcome.json")
    outcome["attempt_id"] = None
    recorded = client.post(
        "/v1/outcome-ledger/outcomes",
        headers=headers,
        json={
            "outcome": outcome,
            "provenance": {
                "source": "verifier",
                "source_event_id": "verify-1",
                "observed_at": "2026-07-11T00:02:00Z",
            },
            "confidence": 0.99,
        },
    )
    assert recorded.json()["capability_success_label"] is True
    observation = {
        "run_id": "run_001",
        "recommendation_id": "shadow_001",
        "shadow_strategy": "backend-a:model",
        "actual_strategy": "backend-a:model",
        "shadow_policy_version": "shadow_router_v1",
        "actual_policy_version": "bootstrap_v1",
        "recorded_at": "2026-07-11T00:01:00Z",
    }
    assert (
        client.post(
            "/v1/shadow-routing/observations", headers=headers, json=observation
        ).status_code
        == 201
    )
    metrics = client.get("/v1/shadow-routing/metrics", headers=headers).json()
    assert metrics["choice_match_rate"] == 1.0
    assert metrics["verified_success_rate_when_matched"] == 1.0
    assert metrics["operational_or_unverifiable_outcomes_excluded"] is True


def test_outcome_reconciles_terminal_duration_without_erasing_withholding_notice(
    session, principal
) -> None:
    client = _client(session)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    _seed_run(client, headers)
    run = session.get(models.Run, (principal.organization_id, "run_001"))
    assert run is not None
    run.canonical_projection = {
        **dict(run.canonical_projection or {}),
        "duration_ms": 100,
        "withheld_artifact_count": 1,
        "withheld_artifact_categories": ["registered_secret"],
    }
    session.commit()
    outcome = load_v2_fixture("outcome.json")
    outcome["attempt_id"] = None
    outcome["latency_ms"] = 4321
    outcome["provenance"] = {
        **dict(outcome.get("provenance") or {}),
        "withheld_artifact_count": 0,
        "withheld_artifact_categories": [],
    }

    response = client.post(
        "/v1/outcome-ledger/outcomes",
        headers=headers,
        json={
            "outcome": outcome,
            "provenance": {
                "source": "agentd",
                "source_event_id": "duration-reconciliation",
                "observed_at": "2026-07-11T00:02:00Z",
            },
            "confidence": 1.0,
        },
    )

    assert response.status_code == 201, response.text
    session.refresh(run)
    assert run.duration_ms == 4321
    assert run.canonical_projection["duration_ms"] == 4321
    assert run.canonical_projection["withheld_artifact_count"] == 1
    assert run.canonical_projection["withheld_artifact_categories"] == ["registered_secret"]
