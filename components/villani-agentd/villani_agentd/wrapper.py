"""Generic command wrapper that emits normalized v2 telemetry."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from villani_ops.closed_loop.protocol_v2 import ResourceV2, TelemetryEnvelopeV2

from .client import LocalClient
from .config import Limits
from .process import CapturedStream, ProcessResult, run_process
from .trace_context import propagated_environment
from .adapters import AdapterContext, get_adapter
from . import __version__


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _identity(prefix: str, bytes_count: int = 16) -> str:
    return f"{prefix}_{secrets.token_hex(bytes_count)}"


def _stream_body(stream: CapturedStream, content_limit: int) -> dict[str, Any]:
    encoded = stream.content.encode("utf-8")
    content = encoded[:content_limit].decode("utf-8", errors="ignore")
    body_truncated = len(encoded) > content_limit
    return {
        "content": content,
        "captured_bytes": stream.captured_bytes,
        "total_bytes": stream.total_bytes,
        "capture_truncated": stream.truncated,
        "body_truncated": body_truncated,
    }


def _event(
    *,
    event_id: str,
    sequence: int,
    occurred_at: datetime,
    run_id: str,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    name: str,
    status: str,
    body: dict[str, Any],
) -> TelemetryEnvelopeV2:
    return TelemetryEnvelopeV2(
        schema_version="villani.telemetry_envelope.v2",
        event_id=event_id,
        idempotency_key=f"villani:agentd:{event_id}",
        occurred_at=occurred_at,
        observed_at=occurred_at,
        sequence=sequence,
        sequence_scope=f"run:{run_id}",
        organization_id=None,
        workspace_id=None,
        project_id=None,
        repository_id=None,
        run_id=run_id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        attempt_id=None,
        source="agentd",
        kind="command",
        name=name,
        status=status,
        resource=ResourceV2(
            schema_version="villani.resource.v2",
            service_name="villani-agentd",
            service_version=__version__,
            deployment_environment="local",
            host_id=None,
            process_id=str(os.getpid()),
            attributes={"adapter": "generic"},
        ),
        attributes={"villani.adapter": "generic"},
        body=body,
    )


def wrap_generic(command: Sequence[str], client: LocalClient, limits: Limits) -> int:
    if not command:
        raise ValueError("a command is required after --")
    run_id = _identity("run")
    trace_id = secrets.token_hex(16)
    start_span = secrets.token_hex(8)
    exit_span = secrets.token_hex(8)
    environment, trace_id, inherited_parent = propagated_environment(trace_id, start_span, run_id)
    started_at = _now()
    executable = Path(command[0]).name
    client.request(
        "POST",
        "/v1/runs",
        {
            "run_id": run_id,
            "trace_id": trace_id,
            "created_at": started_at.isoformat().replace("+00:00", "Z"),
        },
    )
    started = _event(
        event_id=_identity("evt2"),
        sequence=1,
        occurred_at=started_at,
        run_id=run_id,
        trace_id=trace_id,
        span_id=start_span,
        parent_span_id=inherited_parent,
        name="command_started",
        status="running",
        body={
            "executable": executable,
            "argument_count": max(0, len(command) - 1),
            "shell": False,
        },
    )
    try:
        result = run_process(command, limits.stdout_bytes, limits.stderr_bytes, environment)
    except OSError as error:
        error_bytes = str(error).encode("utf-8")
        captured_error = error_bytes[: limits.stderr_bytes]
        result = ProcessResult(
            exit_code=127,
            pid=0,
            duration_ms=0,
            stdout=CapturedStream("", 0, 0, False),
            stderr=CapturedStream(
                captured_error.decode("utf-8", errors="ignore"),
                len(captured_error),
                len(error_bytes),
                len(error_bytes) > limits.stderr_bytes,
            ),
            cancelled=False,
        )
    ended_at = _now()
    per_stream_body_limit = max(1, limits.event_body_bytes // 4)
    exited = _event(
        event_id=_identity("evt2"),
        sequence=2,
        occurred_at=ended_at,
        run_id=run_id,
        trace_id=trace_id,
        span_id=exit_span,
        parent_span_id=start_span,
        name="command_cancelled" if result.cancelled else "command_completed",
        status="cancelled" if result.cancelled else ("ok" if result.exit_code == 0 else "error"),
        body={
            "process": {
                "pid": result.pid,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "cancelled": result.cancelled,
            },
            "stdout": _stream_body(result.stdout, per_stream_body_limit),
            "stderr": _stream_body(result.stderr, per_stream_body_limit),
            "truncation_explicit": True,
        },
    )
    client.request(
        "POST",
        "/v1/events:batch",
        {"events": [started.model_dump(mode="json"), exited.model_dump(mode="json")]},
    )
    client.request(
        "POST",
        f"/v1/runs/{run_id}/finalize",
        {
            "status": "cancelled" if result.cancelled else "completed",
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
        },
    )
    return result.exit_code


def wrap_adapter(
    adapter_name: str, command: Sequence[str], client: LocalClient, limits: Limits
) -> int:
    adapter = get_adapter(adapter_name)
    if adapter.name == "generic-process":
        return wrap_generic(command, client, limits)
    detection = adapter.detect()
    if not detection.available:
        missing = ", ".join(detection.missing_capabilities)
        raise RuntimeError(
            f"adapter {adapter.name} unsupported (detected version: {detection.detected_version or 'absent'}; missing capability: {missing})"
        )
    if not command:
        raise ValueError("a command is required after --")
    run_id = _identity("run")
    trace_id = secrets.token_hex(16)
    root_span = secrets.token_hex(8)
    started_at = _now()
    environment, trace_id, inherited_parent = propagated_environment(trace_id, root_span, run_id)
    parser = adapter.create_parser(AdapterContext(run_id, trace_id, root_span, started_at))
    observed: list[TelemetryEnvelopeV2] = []
    client.request(
        "POST",
        "/v1/runs",
        {
            "run_id": run_id,
            "trace_id": trace_id,
            "created_at": started_at.isoformat().replace("+00:00", "Z"),
        },
    )
    executable = Path(command[0]).name
    start = _event(
        event_id=_identity("evt2"),
        sequence=1,
        occurred_at=started_at,
        run_id=run_id,
        trace_id=trace_id,
        span_id=root_span,
        parent_span_id=inherited_parent,
        name="agent_observation_started",
        status="running",
        body={"adapter": adapter.name, "executable": executable, "shell": False},
    )
    try:
        result = run_process(
            adapter.construct_command(command),
            limits.stdout_bytes,
            limits.stderr_bytes,
            env=environment,
            stdout_callback=lambda chunk: observed.extend(parser.feed(chunk)),
        )
    except OSError as error:
        result = ProcessResult(
            127,
            0,
            0,
            CapturedStream("", 0, 0, False),
            CapturedStream(str(error), len(str(error).encode()), len(str(error).encode()), False),
            False,
        )
    observed.extend(parser.finish())
    ended = _now()
    terminal = _event(
        event_id=_identity("evt2"),
        sequence=2,
        occurred_at=ended,
        run_id=run_id,
        trace_id=trace_id,
        span_id=secrets.token_hex(8),
        parent_span_id=root_span,
        name="agent_observation_cancelled" if result.cancelled else "agent_observation_completed",
        status="cancelled" if result.cancelled else ("ok" if result.exit_code == 0 else "error"),
        body={
            "adapter": adapter.name,
            "process": {
                "pid": result.pid,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
            },
        },
    )
    client.request(
        "POST",
        "/v1/events:batch",
        {
            "events": [
                start.model_dump(mode="json"),
                *[event.model_dump(mode="json") for event in observed],
                terminal.model_dump(mode="json"),
            ]
        },
    )
    outcome = adapter.parse_final_outcome(run_id, result.exit_code, result.cancelled)
    client.request(
        "POST",
        f"/v1/runs/{run_id}/finalize",
        {
            "status": "cancelled" if result.cancelled else "completed",
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "outcome": outcome.model_dump(mode="json"),
        },
    )
    return result.exit_code
