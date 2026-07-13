from __future__ import annotations

from conftest import load_v2_fixture

from villani_control_plane.models import Attempt
from villani_control_plane.services import IngestionService
from villani_control_plane.services.query import RunQueryService


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


def test_attempt_identity_is_scoped_by_run_and_replay_is_idempotent(
    session, principal
) -> None:
    service = IngestionService(session)
    query = RunQueryService(session)
    outcome_fixture = load_v2_fixture("outcome.json")
    for index, run_id in enumerate(("run_a", "run_b"), start=1):
        event = load_v2_fixture("telemetry-envelope.json")
        event.update(
            event_id=f"evt_attempt_scope_{index}",
            idempotency_key=f"attempt-scope:{index}",
            run_id=run_id,
            attempt_id="attempt_001",
            sequence_scope=f"run:{run_id}",
            trace_id=f"{index}" * 32,
            span_id=f"{index}" * 16,
        )
        first = service.ingest_batch(f"batch_{run_id}", [event], principal)
        replay = service.ingest_batch(f"batch_{run_id}", [event], principal)
        assert (first.inserted, first.duplicates, first.replayed) == (1, 0, False)
        assert (replay.inserted, replay.duplicates, replay.replayed) == (0, 1, True)

        outcome = dict(outcome_fixture)
        outcome["run_id"] = run_id
        assert service.record_outcome(outcome, principal)["attempt_id"] == "attempt_001"
        detail = query.get_run(run_id, principal)
        assert detail.attempts == [{"id": "attempt_001", "status": "ok"}]
        assert detail.outcomes[0]["attempt_id"] == "attempt_001"
        timeline = query.events(run_id, principal, cursor=None, limit=10)
        assert len(timeline.events) == 1
        assert timeline.events[0]["attempt_id"] == "attempt_001"
        assert f"{run_id}:attempt_001" not in str(detail.model_dump(mode="json"))


def test_one_batch_accepts_multiple_events_for_the_same_scoped_attempt(
    session, principal
) -> None:
    events = []
    for sequence in (1, 2):
        event = load_v2_fixture("telemetry-envelope.json")
        event.update(
            event_id=f"evt_same_attempt_{sequence}",
            idempotency_key=f"same-attempt:{sequence}",
            run_id="run_same_attempt_batch",
            attempt_id="attempt_001",
            sequence_scope="run:run_same_attempt_batch",
            sequence=sequence,
            trace_id="a" * 32,
            span_id=f"{sequence}" * 16,
        )
        events.append(event)

    result = IngestionService(session).ingest_batch(
        "batch_same_attempt", events, principal
    )

    assert result.inserted == 2
    assert RunQueryService(session).get_run(
        "run_same_attempt_batch", principal
    ).attempts == [{"id": "attempt_001", "status": "ok"}]


def test_one_batch_accepts_repeated_canonical_attempt_ids_across_runs(
    session, principal
) -> None:
    events = []
    ordinal = 0
    for run_index, run_id in enumerate(("run_repeated_a", "run_repeated_b"), start=1):
        for sequence in (1, 2):
            ordinal += 1
            event = load_v2_fixture("telemetry-envelope.json")
            event.update(
                event_id=f"evt_repeated_batch_{ordinal}",
                idempotency_key=f"repeated-batch:{ordinal}",
                run_id=run_id,
                attempt_id="attempt_001",
                sequence_scope=f"run:{run_id}",
                sequence=sequence,
                trace_id=f"{run_index}" * 32,
                span_id=f"{ordinal:x}".zfill(16),
            )
            events.append(event)

    result = IngestionService(session).ingest_batch(
        "batch_repeated_attempt_ids", events, principal
    )

    assert result.inserted == 4
    for run_id in ("run_repeated_a", "run_repeated_b"):
        assert RunQueryService(session).get_run(run_id, principal).attempts == [
            {"id": "attempt_001", "status": "ok"}
        ]
