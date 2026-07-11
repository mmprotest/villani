from __future__ import annotations

from conftest import load_v2_fixture

from villani_control_plane.models import Attempt
from villani_control_plane.services import IngestionService


def test_daemon_exact_telemetry_artifact_and_outcome_fixtures(session, principal) -> None:
    telemetry = load_v2_fixture("telemetry-envelope.json")
    result = IngestionService(session).ingest_batch("fixture_batch", [telemetry], principal)
    assert result.inserted == 1

    descriptor = load_v2_fixture("artifact-descriptor.json")
    stored_descriptor = IngestionService(session).register_artifact(
        telemetry["run_id"], descriptor, principal
    )
    assert stored_descriptor == descriptor

    outcome = load_v2_fixture("outcome.json")
    session.add(
        Attempt(
            organization_id=principal.organization_id,
            id=outcome["attempt_id"],
            run_id=outcome["run_id"],
            status="completed",
        )
    )
    session.commit()
    stored_outcome = IngestionService(session).record_outcome(outcome, principal)
    assert stored_outcome == outcome
