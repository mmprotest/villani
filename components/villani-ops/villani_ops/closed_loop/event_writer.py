"""Canonical controller event construction and safe failure payloads."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .protocol import EventEnvelope
from .run_store import RunStore
from .event_sink import RunEventDelivery, RunEventSink
from villani_ops.execution_environment.secrets import registered_secret_values


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret)(\s*[:=]\s*)\S+"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def redact_message(message: str, *, limit: int = 500) -> str:
    redacted = message.replace("\r", " ").replace("\n", " ")
    for secret in registered_secret_values():
        redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.lower().startswith("(?i)(api"):
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted[:limit] or "dependency failed without a message"


def failure_payload(error: BaseException, *, operation: str) -> dict[str, Any]:
    return {
        "operation": operation,
        "exception_class": error.__class__.__name__,
        "message": redact_message(str(error)),
    }


def redact_data(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Recursively redact credential-shaped values and explicitly supplied secrets."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return redact_data(model_dump(mode="json"), secrets=secrets)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        redacted = value
        for secret in (*registered_secret_values(), *secrets):
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        if not redacted:
            return ""
        return redact_message(redacted, limit=max(500, len(redacted)))
    if isinstance(value, list):
        return [redact_data(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, secrets=secrets) for item in value)
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {
                "api_key",
                "apikey",
                "authorization",
                "password",
                "secret",
                "token",
                "headers",
            }:
                output[str(key)] = "[REDACTED]"
            else:
                output[str(key)] = redact_data(item, secrets=secrets)
        return output
    return value


class EventWriter:
    def __init__(
        self,
        store: RunStore,
        trace_id: str,
        now: Callable[[], datetime],
        on_event: Callable[[EventEnvelope], None] | None = None,
        event_sink: RunEventSink | None = None,
    ) -> None:
        self._store = store
        self._trace_id = trace_id
        self._now = now
        self._on_event = on_event
        self._delivery = (
            RunEventDelivery(store, event_sink, now) if event_sink is not None else None
        )

    def emit(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        attempt_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        event = self._store.append_event(
            timestamp=self._now(),
            trace_id=self._trace_id,
            attempt_id=attempt_id,
            parent_event_id=parent_event_id,
            source="controller",
            event_type=event_type,
            payload=payload or {},
        )
        if self._delivery is not None:
            self._delivery.event_persisted(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                # Console observers are advisory and run after durable persistence.
                pass
        return event

    def finalize_delivery(self) -> None:
        if self._delivery is not None:
            self._delivery.finalize()
