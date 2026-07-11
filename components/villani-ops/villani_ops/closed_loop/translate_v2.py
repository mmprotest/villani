"""Deterministic projection of canonical v1 events into v2 transport records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .durable_io import read_jsonl_tolerant
from .protocol import EventEnvelope
from .protocol_v2 import ResourceV2, TelemetryEnvelopeV2
from .schema_validation import validate_event_stream


def _digest(namespace: str, value: str, length: int) -> str:
    return hashlib.sha256(f"villani:v2:{namespace}:{value}".encode()).hexdigest()[
        :length
    ]


def legacy_trace_id_to_w3c(legacy_trace_id: str) -> str:
    """Return a stable, non-zero W3C trace-id while retaining legacy identity elsewhere."""
    return _digest("trace", legacy_trace_id, 32)


def legacy_event_id_to_span_id(legacy_event_id: str) -> str:
    return _digest("span", legacy_event_id, 16)


def _kind(event_type: str) -> str:
    if event_type.startswith("model_"):
        return "model_call"
    if event_type.startswith("tool_"):
        return "tool_call"
    if event_type.startswith("command_"):
        return "command"
    if event_type.startswith("file_") or event_type.startswith("patch_"):
        return "file_operation"
    if event_type.startswith("verification_"):
        return "verifier"
    if event_type in {"policy_selected", "retry_selected", "escalation_selected"}:
        return "policy_decision"
    if event_type == "candidate_selected":
        return "selection"
    if event_type.startswith("materialization_"):
        return "materialization"
    if event_type.startswith("attempt_"):
        return "agent_run"
    return "controller_stage"


def _status(event_type: str) -> str:
    if event_type.endswith(("_failed", "_error")) or event_type == "run_failed":
        return "error"
    if (
        event_type.endswith(("_completed", "_selected"))
        or event_type == "run_completed"
    ):
        return "ok"
    if event_type.endswith("_started"):
        return "running"
    return "unset"


def translate_v1_event(
    event: EventEnvelope, *, resource_attributes: Mapping[str, Any] | None = None
) -> TelemetryEnvelopeV2:
    legacy_trace_id = event.trace_id
    event_key = _digest("event", f"{event.run_id}:{event.event_id}", 32)
    return TelemetryEnvelopeV2(
        schema_version="villani.telemetry_envelope.v2",
        event_id=f"evt2_{event_key}",
        idempotency_key=f"villani:v2:{event_key}",
        occurred_at=event.timestamp,
        observed_at=event.timestamp,
        sequence=event.sequence,
        sequence_scope=f"run:{event.run_id}",
        organization_id=None,
        workspace_id=None,
        project_id=None,
        repository_id=None,
        run_id=event.run_id,
        trace_id=legacy_trace_id_to_w3c(legacy_trace_id),
        span_id=legacy_event_id_to_span_id(event.event_id),
        parent_span_id=(
            legacy_event_id_to_span_id(event.parent_event_id)
            if event.parent_event_id is not None
            else None
        ),
        attempt_id=event.attempt_id,
        source=event.source,
        kind=_kind(event.event_type),
        name=event.event_type,
        status=_status(event.event_type),
        resource=ResourceV2(
            schema_version="villani.resource.v2",
            service_name="villani",
            service_version=None,
            deployment_environment="local",
            host_id=None,
            process_id=None,
            attributes=dict(resource_attributes or {}),
        ),
        attributes={
            "villani.legacy.schema_version": event.schema_version,
            "villani.legacy.event_id": event.event_id,
            "villani.legacy.trace_id": legacy_trace_id,
            "villani.clock.status": "legacy_single_timestamp",
        },
        body=dict(event.payload),
    )


def translate_v1_events(
    events: Sequence[Mapping[str, Any]],
    *,
    resource_attributes: Mapping[str, Any] | None = None,
) -> list[TelemetryEnvelopeV2]:
    return [
        translate_v1_event(event, resource_attributes=resource_attributes)
        for event in validate_event_stream(events)
    ]


def translate_v1_run(run_directory: str | Path) -> list[TelemetryEnvelopeV2]:
    root = Path(run_directory)
    attributes: Mapping[str, Any] | None = None
    resource_path = root / "resource.json"
    if resource_path.is_file():
        document = json.loads(resource_path.read_text(encoding="utf-8"))
        resource = ResourceV2.model_validate(document)
        attributes = resource.attributes
    return translate_v1_events(
        read_jsonl_tolerant(root / "events.jsonl"), resource_attributes=attributes
    )


def normalized_v2_jsonl(records: list[TelemetryEnvelopeV2]) -> bytes:
    lines = [
        json.dumps(
            record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        for record in records
    ]
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
