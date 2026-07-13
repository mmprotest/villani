"""Optional delivery of canonical run evidence to a durable local event sink."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from villani_ops.execution_environment.secrets import registered_secret_values

from .durable_io import append_jsonl_durable, read_jsonl_tolerant
from .protocol import EventEnvelope
from .protocol_v2 import ArtifactDescriptorV2, DigestV2, OutcomeV2
from .run_store import RunStore
from .schema_validation import validate_event_stream


EventSinkStatus = Literal[
    "connected",
    "not_installed",
    "not_running",
    "temporarily_unavailable",
    "rejected_protocol",
]


@dataclass(frozen=True, slots=True)
class EventSinkDiagnostic:
    status: EventSinkStatus
    sink: str = "agentd"
    detail: str | None = None


class RunEventSink(Protocol):
    """Transport boundary used by the closed loop without importing agentd."""

    def availability(self) -> EventSinkDiagnostic: ...

    def open_run(self, run_id: str, trace_id: str, created_at: datetime) -> None: ...

    def submit_event(self, event: EventEnvelope) -> None: ...

    def register_artifact(
        self, run_id: str, descriptor: ArtifactDescriptorV2, content: bytes
    ) -> None: ...

    def finalize_run(self, run_id: str, outcome: OutcomeV2) -> None: ...


class UnavailableEventSink:
    """Explicit local-only sink carrying the reason agentd is unavailable."""

    def __init__(self, diagnostic: EventSinkDiagnostic) -> None:
        self._diagnostic = diagnostic

    def availability(self) -> EventSinkDiagnostic:
        return self._diagnostic

    def open_run(self, run_id: str, trace_id: str, created_at: datetime) -> None:
        del run_id, trace_id, created_at

    def submit_event(self, event: EventEnvelope) -> None:
        del event

    def register_artifact(
        self, run_id: str, descriptor: ArtifactDescriptorV2, content: bytes
    ) -> None:
        del run_id, descriptor, content

    def finalize_run(self, run_id: str, outcome: OutcomeV2) -> None:
        del run_id, outcome


_SECRET_BYTES = re.compile(
    rb"(?i)(?:api[_-]?key|password|secret|private[_-]?key)\s*[:=]\s*"
    rb"(?!test(?:-|_|\b))[^\s,;]+|bearer\s+[A-Za-z0-9._~+/-]{12,}|"
    rb"\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{16,}\b"
)
SAFE_CANONICAL_ARTIFACTS = (
    "manifest.json",
    "state.json",
    "task.json",
    "classification.json",
    "selection.json",
    "materialization.json",
)


class RunEventDelivery:
    """Replay-safe sink lifecycle attached to one canonical run store."""

    def __init__(
        self,
        store: RunStore,
        sink: RunEventSink,
        now: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._sink = sink
        self._now = now
        self._diagnostic = sink.availability()
        self._opened = False
        self._last_delivered_sequence = 0
        self._record(
            "availability",
            status=self._diagnostic.status,
            detail=self._diagnostic.detail,
        )

    def _record(self, operation: str, **fields: Any) -> None:
        append_jsonl_durable(
            self._store.run_directory / "telemetry_diagnostics.jsonl",
            {
                "schema_version": "villani.telemetry_diagnostic.v1",
                "timestamp": self._now().isoformat().replace("+00:00", "Z"),
                "sink": self._diagnostic.sink,
                "operation": operation,
                **fields,
            },
        )

    def event_persisted(self, event: EventEnvelope) -> None:
        """Deliver only after the event already exists in canonical events.jsonl."""

        if self._diagnostic.status != "connected":
            return
        try:
            if not self._opened:
                self._sink.open_run(event.run_id, event.trace_id, event.timestamp)
                self._opened = True
            documents = read_jsonl_tolerant(self._store.run_directory / "events.jsonl")
            for persisted in validate_event_stream(documents):
                if persisted.sequence <= self._last_delivered_sequence:
                    continue
                self._sink.submit_event(persisted)
                self._last_delivered_sequence = persisted.sequence
        except Exception as error:
            self._record(
                "event_delivery",
                status="temporarily_unavailable",
                event_id=event.event_id,
                sequence=event.sequence,
                exception_class=type(error).__name__,
            )

    def finalize(self) -> None:
        """Register safe metadata and finalize only after local terminal snapshots exist."""

        if self._diagnostic.status != "connected":
            return
        existing = validate_event_stream(
            read_jsonl_tolerant(self._store.run_directory / "events.jsonl")
        )
        if existing:
            self.event_persisted(existing[-1])
        state = _read_object(self._store.run_directory / "state.json")
        manifest = _read_object(self._store.run_directory / "manifest.json")
        if not bool(state.get("terminal")) or manifest.get("completed_at") is None:
            self._record(
                "finalization",
                status="rejected_protocol",
                detail="local_run_not_finalized",
            )
            return
        try:
            withheld_count = 0
            withheld_categories: set[str] = set()
            for relative in SAFE_CANONICAL_ARTIFACTS:
                path = self._store.run_directory / relative
                if not path.is_file():
                    continue
                content = path.read_bytes()
                unsafe_categories = artifact_withholding_categories(content)
                if unsafe_categories:
                    withheld_count += 1
                    withheld_categories.update(unsafe_categories)
                    self._record(
                        "artifact_registration",
                        status="rejected_protocol",
                        artifact=relative,
                        detail="sensitive_content_rejected",
                        categories=list(unsafe_categories),
                    )
                    continue
                digest = hashlib.sha256(content).hexdigest()
                descriptor = ArtifactDescriptorV2(
                    schema_version="villani.artifact_descriptor.v2",
                    artifact_id=f"artifact_{hashlib.sha256((self._store.run_id + ':' + relative).encode()).hexdigest()[:24]}",
                    digest=DigestV2(algorithm="sha256", value=digest),
                    size_bytes=len(content),
                    media_type="application/json",
                    logical_role="canonical_metadata",
                    sensitivity="internal",
                    retention_class="run",
                    encryption_status="unknown",
                    storage_reference=None,
                    provenance_status="recorded",
                    attributes={"villani.local.relative_path": relative},
                )
                self._sink.register_artifact(self._store.run_id, descriptor, content)
            self._sink.finalize_run(
                self._store.run_id,
                build_canonical_outcome(
                    self._store.run_directory,
                    withheld_artifact_count=withheld_count,
                    withheld_artifact_categories=tuple(sorted(withheld_categories)),
                ),
            )
        except Exception as error:
            self._record(
                "finalization",
                status="temporarily_unavailable",
                exception_class=type(error).__name__,
            )


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path.name}")
    return value


def contains_registered_secret(content: bytes) -> bool:
    return bool(artifact_withholding_categories(content))


def artifact_withholding_categories(content: bytes) -> tuple[str, ...]:
    """Classify unsafe artifact content without exposing the matched value."""

    categories: set[str] = set()
    if any(
        secret.encode() in content
        for secret in registered_secret_values()
        if secret
    ):
        categories.add("registered_secret")
    if re.search(rb"(?i)bearer\s+[A-Za-z0-9._~+/-]{12,}", content):
        categories.add("bearer_token")
    if re.search(rb"(?i)\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{16,}\b", content):
        categories.add("api_key")
    if re.search(
        rb"(?i)(?:api[_-]?key|password|secret|private[_-]?key)\s*[:=]\s*"
        rb"(?!test(?:-|_|\b))[^\s,;]+",
        content,
    ):
        categories.add("credential_assignment")
    if _SECRET_BYTES.search(content) and not categories:
        categories.add("sensitive_content")
    return tuple(sorted(categories))


def build_canonical_outcome(
    run_directory: Path,
    *,
    withheld_artifact_count: int = 0,
    withheld_artifact_categories: tuple[str, ...] = (),
) -> OutcomeV2:
    manifest = _read_object(run_directory / "manifest.json")
    selected = manifest.get("selected_attempt_id")
    verification: Mapping[str, Any] = {}
    if isinstance(selected, str):
        path = run_directory / "verification" / f"{selected}.json"
        if path.is_file():
            verification = _read_object(path)
    materialization: Mapping[str, Any] = {}
    materialization_path = run_directory / "materialization.json"
    if materialization_path.is_file():
        materialization = _read_object(materialization_path)
    verification_status = verification.get("outcome")
    if verification_status not in {"accepted", "rejected", "unclear", "error"}:
        verification_status = "not_run"
    cost = manifest.get("total_cost_usd")
    currency = (manifest.get("currency") or "USD") if cost is not None else None
    latency = manifest.get("run_wall_clock_duration_ms")
    return OutcomeV2(
        schema_version="villani.outcome.v2",
        run_id=str(manifest["run_id"]),
        attempt_id=selected if isinstance(selected, str) else None,
        verification_status=verification_status,
        accepted=(
            bool(verification.get("acceptance_eligible")) if verification else None
        ),
        materialized=(
            materialization.get("status") == "succeeded" if materialization else None
        ),
        merged=None,
        reverted=None,
        ci_state=None,
        developer_disposition=None,
        defect_association=None,
        cost=float(cost) if isinstance(cost, (int, float)) else None,
        currency=str(currency) if currency is not None else None,
        cost_accounting_status=str(manifest.get("cost_accounting_status", "unknown")),
        latency_ms=int(latency) if isinstance(latency, int) else None,
        latency_accounting_status=str(
            manifest.get("run_wall_clock_duration_accounting_status", "unknown")
        ),
        provenance_status="recorded",
        provenance={
            "source": "canonical_local_run_bundle",
            "manifest": "manifest.json",
            "withheld_artifact_count": withheld_artifact_count,
            "withheld_artifact_categories": list(withheld_artifact_categories),
        },
    )
