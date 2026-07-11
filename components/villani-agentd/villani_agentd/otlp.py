"""Strict OTLP/HTTP JSON trace normalization."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from villani_ops.closed_loop.protocol_v2 import ResourceV2, TelemetryEnvelopeV2

from .adapters.contract import SensitiveFieldPolicy
from .adapters.normalize import redact
from .spool import SpoolError


def _value(value: Any) -> Any:
    if not isinstance(value, Mapping) or len(value) != 1:
        raise SpoolError("OTLP attribute value must contain exactly one typed value")
    key, item = next(iter(value.items()))
    if key in {"stringValue", "bytesValue"}:
        return item
    if key == "intValue":
        try:
            return int(item)
        except (TypeError, ValueError) as error:
            raise SpoolError("OTLP intValue must be an integer") from error
    if key == "doubleValue":
        try:
            return float(item)
        except (TypeError, ValueError) as error:
            raise SpoolError("OTLP doubleValue must be numeric") from error
    if key == "boolValue":
        if not isinstance(item, bool):
            raise SpoolError("OTLP boolValue must be boolean")
        return item
    if key == "arrayValue" and isinstance(item, Mapping) and isinstance(item.get("values"), list):
        return [_value(child) for child in item["values"]]
    if key == "kvlistValue" and isinstance(item, Mapping):
        return _attributes(item.get("values"))
    raise SpoolError(f"unsupported OTLP attribute value: {key}")


def _attributes(rows: Any) -> dict[str, Any]:
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise SpoolError("OTLP attributes must be an array")
    output: dict[str, Any] = {}
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("key"), str)
            or "value" not in row
        ):
            raise SpoolError("OTLP attribute must contain key and value")
        output[row["key"]] = _value(row["value"])
    return output


def _time(nanos: Any) -> datetime:
    try:
        integer = int(nanos)
    except (TypeError, ValueError) as error:
        raise SpoolError("OTLP span requires a valid startTimeUnixNano") from error
    if integer < 0:
        raise SpoolError("OTLP timestamp must be non-negative")
    try:
        return datetime.fromtimestamp(integer / 1_000_000_000, tz=timezone.utc)
    except (ValueError, OSError, OverflowError) as error:
        raise SpoolError("OTLP timestamp is outside the supported range") from error


def normalize_otlp_traces(document: Mapping[str, Any]) -> list[TelemetryEnvelopeV2]:
    resource_spans = document.get("resourceSpans")
    if not isinstance(resource_spans, list):
        raise SpoolError("OTLP payload requires resourceSpans array")
    events: list[TelemetryEnvelopeV2] = []
    for resource_group in resource_spans:
        if not isinstance(resource_group, Mapping):
            raise SpoolError("OTLP resourceSpans entry must be an object")
        resource_doc = resource_group.get("resource") or {}
        if not isinstance(resource_doc, Mapping):
            raise SpoolError("OTLP resource must be an object")
        resource_attrs = _attributes(resource_doc.get("attributes"))
        scope_spans = resource_group.get("scopeSpans")
        if not isinstance(scope_spans, list):
            raise SpoolError("OTLP resourceSpans requires scopeSpans array")
        for scope_group in scope_spans:
            if not isinstance(scope_group, Mapping) or not isinstance(
                scope_group.get("spans"), list
            ):
                raise SpoolError("OTLP scopeSpans requires spans array")
            raw_scope = scope_group.get("scope")
            scope: Mapping[str, Any] = raw_scope if isinstance(raw_scope, Mapping) else {}
            for span in scope_group["spans"]:
                if not isinstance(span, Mapping):
                    raise SpoolError("OTLP span must be an object")
                trace_id, span_id = str(span.get("traceId") or ""), str(span.get("spanId") or "")
                parent = str(span.get("parentSpanId") or "") or None
                if (
                    len(trace_id) != 32
                    or len(span_id) != 16
                    or any(c not in "0123456789abcdef" for c in trace_id + span_id)
                ):
                    raise SpoolError("OTLP traceId/spanId must be lower-case W3C hexadecimal IDs")
                if set(trace_id) == {"0"} or set(span_id) == {"0"}:
                    raise SpoolError("OTLP traceId/spanId must be non-zero")
                if parent and (
                    len(parent) != 16 or any(c not in "0123456789abcdef" for c in parent)
                ):
                    raise SpoolError("OTLP parentSpanId must be a lower-case W3C span ID")
                attrs = redact(
                    {**resource_attrs, **_attributes(span.get("attributes"))},
                    SensitiveFieldPolicy(),
                )
                occurred = _time(span.get("startTimeUnixNano"))
                run_id = str(attrs.get("villani.run_id") or f"otlp_{trace_id}")
                provider = attrs.get("gen_ai.provider.name") or attrs.get("gen_ai.system")
                kind = (
                    "model_call"
                    if provider or any(key.startswith("gen_ai.") for key in attrs)
                    else "external_service"
                )
                raw_status = span.get("status")
                status_doc: Mapping[str, Any] = (
                    raw_status if isinstance(raw_status, Mapping) else {}
                )
                code = status_doc.get("code")
                status = (
                    "error"
                    if code in (2, "STATUS_CODE_ERROR")
                    else ("ok" if code in (1, "STATUS_CODE_OK") else "unset")
                )
                event_id = (
                    "evt2_otlp_" + hashlib.sha256(f"{trace_id}:{span_id}".encode()).hexdigest()[:32]
                )
                sequence = (int(span_id, 16) % 9_223_372_036_854_775_806) + 1
                body = {
                    "otlp": {
                        "end_time_unix_nano": span.get("endTimeUnixNano"),
                        "events": span.get("events", []),
                        "links": span.get("links", []),
                        "status_message": status_doc.get("message"),
                    },
                    "gen_ai": {
                        "provider": provider,
                        "operation": attrs.get("gen_ai.operation.name"),
                        "request_model": attrs.get("gen_ai.request.model"),
                        "response_model": attrs.get("gen_ai.response.model"),
                        "input_tokens": attrs.get("gen_ai.usage.input_tokens"),
                        "output_tokens": attrs.get("gen_ai.usage.output_tokens"),
                    },
                }
                events.append(
                    TelemetryEnvelopeV2(
                        schema_version="villani.telemetry_envelope.v2",
                        event_id=event_id,
                        idempotency_key=f"otlp:{trace_id}:{span_id}",
                        occurred_at=occurred,
                        observed_at=occurred,
                        sequence=sequence,
                        sequence_scope=f"otlp:{trace_id}",
                        organization_id=None,
                        workspace_id=None,
                        project_id=None,
                        repository_id=None,
                        run_id=run_id,
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent,
                        attempt_id=None,
                        source="otlp",
                        kind=kind,
                        name=str(span.get("name") or "otlp_span"),
                        status=status,
                        resource=ResourceV2(
                            schema_version="villani.resource.v2",
                            service_name=str(resource_attrs.get("service.name") or "otlp-client"),
                            service_version=str(resource_attrs["service.version"])
                            if "service.version" in resource_attrs
                            else None,
                            deployment_environment=str(
                                resource_attrs["deployment.environment.name"]
                            )
                            if "deployment.environment.name" in resource_attrs
                            else None,
                            host_id=str(resource_attrs["host.id"])
                            if "host.id" in resource_attrs
                            else None,
                            process_id=str(resource_attrs["process.pid"])
                            if "process.pid" in resource_attrs
                            else None,
                            attributes={
                                "otel.scope.name": scope.get("name"),
                                "otel.scope.version": scope.get("version"),
                            },
                        ),
                        attributes=attrs,
                        body=body,
                    )
                )
    return events
