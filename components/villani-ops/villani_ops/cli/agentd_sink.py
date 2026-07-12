"""CLI composition for the optional local agentd event sink."""

from __future__ import annotations

from datetime import datetime

from villani_ops.closed_loop.event_sink import (
    EventSinkDiagnostic,
    RunEventSink,
    UnavailableEventSink,
)
from villani_ops.closed_loop.event_writer import redact_data
from villani_ops.closed_loop.protocol import EventEnvelope
from villani_ops.closed_loop.protocol_v2 import ArtifactDescriptorV2, OutcomeV2
from villani_ops.closed_loop.translate_v2 import translate_v1_event


class AgentdEventSink:
    def __init__(self, client: object) -> None:
        self._client = client

    def availability(self) -> EventSinkDiagnostic:
        return EventSinkDiagnostic("connected")

    def open_run(self, run_id: str, trace_id: str, created_at: datetime) -> None:
        self._client.open_run(run_id, trace_id, created_at)

    def submit_event(self, event: EventEnvelope) -> None:
        safe = EventEnvelope.model_validate(redact_data(event.model_dump(mode="json")))
        self._client.submit_events([translate_v1_event(safe).model_dump(mode="json")])

    def register_artifact(
        self, run_id: str, descriptor: ArtifactDescriptorV2, content: bytes
    ) -> None:
        self._client.register_artifact(
            run_id, descriptor.model_dump(mode="json"), content
        )

    def finalize_run(self, run_id: str, outcome: OutcomeV2) -> None:
        self._client.finalize_run(run_id, outcome.model_dump(mode="json"))


def build_agentd_event_sink() -> RunEventSink:
    try:
        from villani_agentd.client import ClientError, LocalClient
        from villani_agentd.config import AgentdPaths
    except ImportError:
        return UnavailableEventSink(EventSinkDiagnostic("not_installed"))

    paths = AgentdPaths.default()
    if not paths.endpoint.is_file() or not paths.token.is_file():
        return UnavailableEventSink(EventSinkDiagnostic("not_running"))
    try:
        client = LocalClient.from_files(paths)
        health = client.health()
    except ClientError as error:
        return UnavailableEventSink(
            EventSinkDiagnostic("temporarily_unavailable", detail=type(error).__name__)
        )
    if health.get("status") != "ok" or health.get("version") != "v1":
        return UnavailableEventSink(EventSinkDiagnostic("rejected_protocol"))
    return AgentdEventSink(client)
