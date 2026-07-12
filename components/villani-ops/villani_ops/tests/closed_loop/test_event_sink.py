from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.event_sink import (
    EventSinkDiagnostic,
    UnavailableEventSink,
)
from villani_ops.closed_loop.interfaces import ClosedLoopRunRequest
from villani_ops.closed_loop.protocol import EventEnvelope
from villani_ops.closed_loop.protocol_v2 import ArtifactDescriptorV2, OutcomeV2
from villani_ops.tests.closed_loop.fakes import (
    PATCH_ONE,
    FakeAttemptRunner,
    FakeClassifier,
    FakeMaterializer,
    FakeMonotonic,
    FakePolicyEngine,
    FakeSelector,
    FakeVerifier,
    FixedNow,
    StableIds,
    accepted_verification,
    attempt,
    backend,
    policy,
)


class RecordingSink:
    def __init__(self, *, fail_after_sequence: int | None = None) -> None:
        self.fail_after_sequence = fail_after_sequence
        self.stopped = False
        self.opened: list[tuple[str, str, datetime]] = []
        self.events: dict[str, EventEnvelope] = {}
        self.artifacts: dict[str, bytes] = {}
        self.outcomes: dict[str, OutcomeV2] = {}

    def availability(self) -> EventSinkDiagnostic:
        return EventSinkDiagnostic("connected")

    def open_run(self, run_id: str, trace_id: str, created_at: datetime) -> None:
        self.opened.append((run_id, trace_id, created_at))

    def submit_event(self, event: EventEnvelope) -> None:
        if self.fail_after_sequence is not None and event.sequence > self.fail_after_sequence:
            self.stopped = True
            raise ConnectionError("agentd stopped")
        existing = self.events.get(event.event_id)
        if existing is not None:
            assert existing == event
        self.events[event.event_id] = event

    def register_artifact(
        self, run_id: str, descriptor: ArtifactDescriptorV2, content: bytes
    ) -> None:
        if self.stopped:
            raise ConnectionError("agentd stopped")
        assert descriptor.attributes["villani.local.relative_path"]
        self.artifacts[descriptor.artifact_id] = content

    def finalize_run(self, run_id: str, outcome: OutcomeV2) -> None:
        if self.stopped:
            raise ConnectionError("agentd stopped")
        existing = self.outcomes.get(run_id)
        if existing is not None:
            assert existing == outcome
        self.outcomes[run_id] = outcome


def _request(tmp_path: Path) -> ClosedLoopRunRequest:
    return ClosedLoopRunRequest(
        task="Apply the deterministic change.",
        repository_path=tmp_path / "repository",
        success_criteria="The deterministic check passes.",
        runs_root=tmp_path / "runs",
        max_attempts=1,
        policy_configuration={"version": "event_sink_test_v1"},
    )


def _controller(sink: Any) -> ClosedLoopController:
    option = backend("local")
    return ClosedLoopController(
        classifier=FakeClassifier(),
        policy_engine=FakePolicyEngine(
            [policy("attempt", backend_option=option), policy("select")]
        ),
        attempt_runner=FakeAttemptRunner([attempt(patch=PATCH_ONE, cost=None)]),
        verifier=FakeVerifier([accepted_verification()]),
        selector=FakeSelector(),
        materializer=FakeMaterializer(),
        now=FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
        event_sink=sink,
    )


def test_connected_sink_uses_canonical_identity_and_finalizes_after_local_bundle(
    tmp_path: Path,
) -> None:
    sink = RecordingSink()

    result = _controller(sink).run(_request(tmp_path))

    assert result.terminal_state == "COMPLETED"
    assert sink.opened[0][0] == result.run_id
    events = list(sink.events.values())
    assert {event.run_id for event in events} == {result.run_id}
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert sink.outcomes[result.run_id].attempt_id == "attempt_001"
    assert sink.outcomes[result.run_id].cost is None
    assert sink.outcomes[result.run_id].cost_accounting_status == "unknown"
    assert json.loads((result.run_directory / "state.json").read_text())["terminal"] is True
    assert json.loads((result.run_directory / "manifest.json").read_text())[
        "completed_at"
    ]


class InjectedCrash(BaseException):
    pass


class CrashAfterCreation:
    def __call__(self, boundary: str) -> None:
        if boundary == "after_run_creation":
            raise InjectedCrash(boundary)


def test_interrupted_run_resumes_same_identity_and_monotonic_sequence(tmp_path: Path) -> None:
    sink = RecordingSink()
    controller = _controller(sink)
    controller._failure_injector = CrashAfterCreation()
    with pytest.raises(InjectedCrash):
        controller.run(_request(tmp_path))

    resumed = _controller(sink).resume("run_test_001", tmp_path / "runs")

    assert resumed.terminal_state == "COMPLETED"
    assert {opened[0] for opened in sink.opened} == {resumed.run_id}
    sequences = sorted(event.sequence for event in sink.events.values())
    assert sequences == list(range(1, len(sequences) + 1))
    assert sink.outcomes[resumed.run_id].attempt_id == "attempt_001"


def test_sink_outage_does_not_change_success_and_is_durable_diagnostic(
    tmp_path: Path,
) -> None:
    sink = RecordingSink(fail_after_sequence=3)

    result = _controller(sink).run(_request(tmp_path))

    assert result.terminal_state == "COMPLETED"
    diagnostics = [
        json.loads(line)
        for line in (result.run_directory / "telemetry_diagnostics.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        item["operation"] == "event_delivery"
        and item["status"] == "temporarily_unavailable"
        for item in diagnostics
    )
    assert any(
        item["operation"] == "finalization"
        and item["status"] == "temporarily_unavailable"
        for item in diagnostics
    )


def test_event_ids_and_finalization_are_idempotent(tmp_path: Path) -> None:
    sink = RecordingSink()
    result = _controller(sink).run(_request(tmp_path))
    first_events = dict(sink.events)
    first_outcome = sink.outcomes[result.run_id]

    for event in tuple(first_events.values()):
        sink.submit_event(event)
    sink.finalize_run(result.run_id, first_outcome)

    assert sink.events == first_events
    assert sink.outcomes == {result.run_id: first_outcome}


def test_agentd_absent_keeps_run_local_and_successful(tmp_path: Path) -> None:
    sink = UnavailableEventSink(EventSinkDiagnostic("not_running"))

    result = _controller(sink).run(_request(tmp_path))

    assert result.terminal_state == "COMPLETED"
    diagnostic = json.loads(
        (result.run_directory / "telemetry_diagnostics.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert diagnostic["status"] == "not_running"


def test_event_sink_records_no_raw_registered_secret(tmp_path: Path) -> None:
    from villani_ops.execution_environment.secrets import register_secret_values

    secret = "sink-secret-canary-7f98"
    register_secret_values([secret])
    sink = RecordingSink()
    result = _controller(sink).run(_request(tmp_path))

    serialized = json.dumps(
        [event.model_dump(mode="json") for event in sink.events.values()],
        default=str,
    ) + "".join(content.decode("utf-8") for content in sink.artifacts.values())
    assert secret not in serialized
    assert result.terminal_state == "COMPLETED"
