from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from villani_ops.closed_loop.protocol_v2 import ResourceV2, TelemetryEnvelopeV2

from .contract import AdapterContext, SensitiveFieldPolicy

_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/-]{8,}|(?:sk|api[_-]?key)[-_=: ]+[a-z0-9._-]{8,}|(?:password|secret|token)\s*[=:]\s*\S+)"
)


def stable_hex(*parts: object, length: int) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    value = digest[:length]
    return ("1" + value[1:]) if set(value) == {"0"} else value


def redact(value: Any, policy: SensitiveFieldPolicy) -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            token_metric = (
                "token" in lowered and isinstance(item, (int, float)) and not isinstance(item, bool)
            )
            output[str(key)] = (
                "[REDACTED]"
                if not token_metric and any(p in lowered for p in policy.blocked_field_fragments)
                else redact(item, policy)
            )
        return output
    if isinstance(value, list):
        return [redact(item, policy) for item in value]
    if isinstance(value, str) and policy.redact_secret_shaped_text:
        return _SECRET_TEXT.sub("[REDACTED]", value)
    return value


def parse_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return fallback
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (
                parsed.astimezone(timezone.utc)
                if parsed.tzinfo
                else parsed.replace(tzinfo=timezone.utc)
            )
        except ValueError:
            pass
    return fallback


def category(event_type: str, record: Mapping[str, Any]) -> tuple[str, str, str]:
    lowered = event_type.lower()
    raw_item = record.get("item")
    item: Mapping[str, Any] = raw_item if isinstance(raw_item, Mapping) else {}
    if (
        "model" in lowered
        or lowered in {"item.started", "item.completed"}
        and item.get("type") in {"reasoning", "agent_message"}
    ):
        return (
            "model_call",
            event_type or "model_call",
            "error" if "fail" in lowered or "error" in lowered else "ok",
        )
    if "tool" in lowered or lowered in {"tool_use", "tool_result"}:
        return (
            "tool_call",
            event_type or "tool_call",
            "error" if "fail" in lowered or "error" in lowered else "ok",
        )
    if "command" in lowered or "exec" in lowered or item.get("type") == "command_execution":
        return (
            "command",
            event_type or "command",
            "error" if "fail" in lowered or "error" in lowered else "ok",
        )
    if (
        "file" in lowered
        or "patch" in lowered
        or item.get("type") in {"file_change", "mcp_tool_call"}
    ):
        return (
            "file_operation",
            event_type or "file_operation",
            "error" if "fail" in lowered or "error" in lowered else "ok",
        )
    if any(word in lowered for word in ("error", "failed")):
        return "agent_run", event_type or "error", "error"
    if any(word in lowered for word in ("complete", "result", "end", "exit")):
        return "agent_run", event_type or "terminal", "ok"
    return "agent_run", event_type or "event", "running"


def normalize_record(
    adapter: str,
    adapter_version: str,
    context: AdapterContext,
    record: Mapping[str, Any],
    *,
    sequence: int,
    native_id: str,
    revision: int,
    policy: SensitiveFieldPolicy,
    provider: str | None = None,
    parent_span_id: str | None = None,
) -> TelemetryEnvelopeV2:
    event_type = str(
        record.get("event_type") or record.get("type") or record.get("name") or "event"
    )
    kind, name, status = category(event_type, record)
    occurred = parse_time(
        record.get("ts") or record.get("timestamp") or record.get("created_at"), context.observed_at
    )
    content_digest = stable_hex(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False), length=24
    )
    event_id = f"evt2_{stable_hex(adapter, native_id, revision, content_digest, length=32)}"
    span_id = stable_hex(adapter, native_id, revision, length=16)
    attributes: dict[str, Any] = {
        "villani.adapter.name": adapter,
        "villani.adapter.version": adapter_version,
        "villani.native.event_id": native_id,
        "villani.native.provider": provider or adapter,
        "villani.native.event_type": event_type,
        "villani.native.revision": revision,
    }
    return TelemetryEnvelopeV2(
        schema_version="villani.telemetry_envelope.v2",
        event_id=event_id,
        idempotency_key=f"villani:adapter:{event_id}",
        occurred_at=occurred,
        observed_at=context.observed_at,
        sequence=sequence,
        sequence_scope=f"adapter:{adapter}:{context.run_id}",
        organization_id=None,
        workspace_id=None,
        project_id=None,
        repository_id=None,
        run_id=context.run_id,
        trace_id=context.trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id or context.root_span_id,
        attempt_id=context.attempt_id,
        source="agent_adapter",
        kind=kind,
        name=name,
        status=status,
        resource=ResourceV2(
            schema_version="villani.resource.v2",
            service_name=adapter,
            service_version=adapter_version,
            deployment_environment="local",
            host_id=None,
            process_id=None,
            attributes={"observation_source": "machine_readable"},
        ),
        attributes=attributes,
        body=redact(dict(record), policy),
    )
