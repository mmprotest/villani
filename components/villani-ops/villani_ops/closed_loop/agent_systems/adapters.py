"""Harness lifecycle adapters and the controller-facing agent-system runner."""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol

from villani_ops.core.backend import Backend
from villani_ops.subprocess_utils import resolve_command_prefix

from ..adapters.villani_code_attempt import VillaniCodeAttemptAdapter
from ..durable_io import write_json_atomic
from ..event_writer import redact_data
from ..interfaces import AttemptContext, AttemptResult, RuntimeEvent
from ..qualification import (
    QualificationStore,
    assess_qualification,
    repository_qualification_context,
    task_profile,
)
from .models import (
    AgentSystemDoctorReport,
    AgentSystemIdentity,
    CleanupResult,
    DoctorCheck,
    HarnessArtifact,
    HarnessCost,
    HarnessExecutionIdentity,
    HarnessInfrastructureFailure,
    HARNESS_LIFECYCLE_OPERATIONS,
    HARNESS_RUNTIME_CONTRACT,
    HarnessResult,
    HarnessSession,
    HarnessUsage,
    NormalizedHarnessEvent,
    NORMALIZED_EVENT_NAMES,
    utc_now,
)


def _relative_artifact_path(context: AttemptContext, path: str | None) -> str | None:
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return (
                candidate.resolve()
                .relative_to(Path(context.run_directory).resolve())
                .as_posix()
            )
        except ValueError:
            return None
    normalized = PurePosixPath(str(path).replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        return None
    return normalized.as_posix()


def _artifact(
    context: AttemptContext, kind: str, path: str | None
) -> HarnessArtifact | None:
    relative = _relative_artifact_path(context, path)
    if relative is None:
        return None
    absolute = Path(context.run_directory) / relative
    digest = None
    size = None
    if absolute.is_file():
        payload = absolute.read_bytes()
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        size = len(payload)
    return HarnessArtifact(kind=kind, path=relative, digest=digest, size_bytes=size)


_EVENT_MAP = {
    "command_started": "command_start",
    "command_completed": "command_complete",
    "command_failed": "command_complete",
    "file_write": "file_change_complete",
    "tool_call_started": "tool_call_start",
    "tool_call_completed": "tool_call_complete",
    "tool_call_failed": "tool_call_complete",
}


def _raw_event_name(harness_id: str, event_name: str) -> str:
    namespace = re.sub(r"[^a-z0-9]+", "_", harness_id.lower()).strip("_") or "harness"
    normalized = re.sub(r"[^a-z0-9]+", "_", event_name.lower()).strip("_") or "event"
    return f"raw.{namespace}.{normalized}"


def _failure_category(code: str) -> str:
    lowered = code.lower()
    for token, category in (
        ("overload", "transport_overload"),
        ("rate_limit", "rate_limit"),
        ("rate_limited", "rate_limit"),
        ("cancel", "cancellation"),
        ("timeout", "timeout"),
        ("protocol", "protocol"),
        ("schema", "protocol"),
        ("version", "protocol"),
        ("executable", "missing_executable"),
        ("permission", "permission"),
        ("credential", "permission"),
        ("auth", "permission"),
        ("sandbox", "environment"),
        ("environment", "environment"),
        ("malformed", "malformed_output"),
        ("oversized", "oversized_output"),
        ("cleanup", "cleanup"),
        ("process", "process"),
        ("crash", "process"),
    ):
        if token in lowered:
            return category
    return "unknown"


def normalize_events(
    identity: AgentSystemIdentity,
    session: HarnessSession,
    result: AttemptResult,
) -> tuple[NormalizedHarnessEvent, ...]:
    started_at = (
        session.prepared_at.replace(tzinfo=timezone.utc)
        if session.prepared_at.tzinfo is None
        else session.prepared_at.astimezone(timezone.utc)
    )
    last_timestamp = started_at
    events: list[NormalizedHarnessEvent] = [
        NormalizedHarnessEvent(
            sequence=1,
            timestamp=started_at,
            name="session_started",
            payload={
                "session_id": session.session_id,
                "system_id": identity.system_id,
            },
        )
    ]
    for raw in result.runtime_events:
        mapped = _EVENT_MAP.get(raw.event_type)
        payload = dict(raw.payload)
        payload.setdefault("session_id", session.session_id)
        source_timestamp = (
            raw.timestamp.replace(tzinfo=timezone.utc)
            if raw.timestamp.tzinfo is None
            else raw.timestamp.astimezone(timezone.utc)
        )
        event_timestamp = max(source_timestamp, last_timestamp)
        if event_timestamp != source_timestamp:
            payload["event_timestamp_adjusted_from"] = source_timestamp.isoformat()
        if mapped is None and raw.event_type in NORMALIZED_EVENT_NAMES:
            mapped = raw.event_type
        if (
            raw.event_type == "reasoning_summary"
            and payload.get("safe_to_persist") is not True
        ):
            mapped = "warning"
            payload = {
                "session_id": session.session_id,
                "code": "reasoning_summary_withheld",
                "message": "Harness reasoning was not marked safe to persist.",
            }
        if mapped is None:
            events.append(
                NormalizedHarnessEvent(
                    sequence=len(events) + 1,
                    timestamp=event_timestamp,
                    name=_raw_event_name(identity.harness.harness_id, raw.event_type),
                    payload=payload,
                    raw_namespace=identity.harness.harness_id,
                    raw_name=raw.event_type,
                )
            )
        else:
            if raw.event_type.endswith("failed"):
                payload.setdefault("status", "failed")
            events.append(
                NormalizedHarnessEvent(
                    sequence=len(events) + 1,
                    timestamp=event_timestamp,
                    name=mapped,
                    payload=payload,
                )
            )
        last_timestamp = event_timestamp
    events.append(
        NormalizedHarnessEvent(
            sequence=len(events) + 1,
            timestamp=max(datetime.now(timezone.utc), last_timestamp),
            name="cancellation" if result.status == "cancelled" else "session_complete",
            payload={"session_id": session.session_id, "status": result.status},
        )
    )
    return tuple(events)


class HarnessAdapter(Protocol):
    """Versioned lifecycle implemented by every future harness integration."""

    identity: AgentSystemIdentity

    def probe(self) -> Mapping[str, Any]: ...

    def describe_capabilities(self) -> Mapping[str, Any]: ...

    def prepare_session(self, context: AttemptContext) -> HarnessSession: ...

    def execute_task(
        self, session: HarnessSession, context: AttemptContext
    ) -> AttemptResult: ...

    def stream_events(
        self, session: HarnessSession, result: AttemptResult
    ) -> tuple[NormalizedHarnessEvent, ...]: ...

    def request_cancellation(self, session: HarnessSession) -> bool: ...

    def collect_result(
        self,
        session: HarnessSession,
        context: AttemptContext,
        result: AttemptResult,
        events: tuple[NormalizedHarnessEvent, ...],
        cleanup: CleanupResult,
    ) -> HarnessResult: ...

    def collect_artifacts(
        self, context: AttemptContext, result: AttemptResult
    ) -> tuple[HarnessArtifact, ...]: ...

    def cleanup(self, session: HarnessSession) -> CleanupResult: ...

    def doctor(self) -> AgentSystemDoctorReport: ...


class VillaniCodeHarnessAdapter:
    """Expose one structured runner through Villani's shared isolation boundary."""

    def __init__(
        self,
        identity: AgentSystemIdentity,
        backends: Mapping[str, Backend],
        *,
        implementation: VillaniCodeAttemptAdapter | None = None,
        command: str | None = None,
    ) -> None:
        self.identity = identity
        self._command = command or str(
            self.identity.configuration.get("harness", {}).get("command_identity")
            or "villani-code"
        )
        self._implementation = implementation or VillaniCodeAttemptAdapter(
            backends=backends
        )
        self._sessions: dict[str, tuple[HarnessSession, AttemptContext]] = {}
        self._lock = threading.RLock()

    def probe(self) -> Mapping[str, Any]:
        command = self._command
        prefix = resolve_command_prefix(command)
        return {
            "protocol_version": self.identity.harness.protocol_version,
            "supported_protocol_versions": ["villani.harness_adapter.v1"],
            "harness_id": self.identity.harness.harness_id,
            "harness_version": self.identity.harness.version,
            "available": prefix is not None,
            "command": command,
            "lifecycle_operations": list(HARNESS_LIFECYCLE_OPERATIONS),
            "runtime_contract": dict(HARNESS_RUNTIME_CONTRACT),
        }

    def describe_capabilities(self) -> Mapping[str, Any]:
        return {
            name: assessment.model_dump(mode="json")
            for name, assessment in self.identity.capabilities.items()
        }

    def prepare_session(self, context: AttemptContext) -> HarnessSession:
        session = HarnessSession(
            session_id=f"session_{context.attempt_id}",
            system_id=self.identity.system_id,
            run_id=context.run_id,
            attempt_id=context.attempt_id,
            state="prepared",
            prepared_at=utc_now(),
        )
        with self._lock:
            if session.session_id in self._sessions:
                raise RuntimeError(f"duplicate harness session {session.session_id}")
            self._sessions[session.session_id] = (session, context)
        return session

    def execute_task(
        self, session: HarnessSession, context: AttemptContext
    ) -> AttemptResult:
        self._require(session, context)
        return self._implementation.run(context)

    def stream_events(
        self, session: HarnessSession, result: AttemptResult
    ) -> tuple[NormalizedHarnessEvent, ...]:
        return normalize_events(self.identity, session, result)

    def request_cancellation(self, session: HarnessSession) -> bool:
        with self._lock:
            active = self._sessions.get(session.session_id)
        if active is None:
            return False
        cancellation = active[1].cancellation_event
        setter = getattr(cancellation, "set", None)
        if callable(setter):
            setter()
            return True
        return False

    def collect_artifacts(
        self, context: AttemptContext, result: AttemptResult
    ) -> tuple[HarnessArtifact, ...]:
        candidates = (
            (
                "patch",
                f"attempts/{context.attempt_id}/patch.diff"
                if result.patch is not None
                else None,
            ),
            ("stdout", f"attempts/{context.attempt_id}/stdout.log"),
            ("stderr", f"attempts/{context.attempt_id}/stderr.log"),
            ("raw_trace", result.trace_path),
            ("telemetry", result.telemetry_path),
            (
                "candidate_bundle",
                str(result.metadata.get("candidate_bundle_path") or "") or None,
            ),
            (
                "repository_validation",
                str(result.metadata.get("repository_validation_path") or "") or None,
            ),
        )
        return tuple(
            item
            for kind, path in candidates
            if (item := _artifact(context, kind, path)) is not None
        )

    def collect_result(
        self,
        session: HarnessSession,
        context: AttemptContext,
        result: AttemptResult,
        events: tuple[NormalizedHarnessEvent, ...],
        cleanup: CleanupResult,
    ) -> HarnessResult:
        changed = result.metadata.get("changed_files")
        changed_files = (
            [str(item).replace("\\", "/") for item in changed]
            if isinstance(changed, (list, tuple))
            else []
        )
        infrastructure_failure = bool(
            result.error is not None
            and result.error.code
            not in {"codex_coding_failure", "claude_coding_failure"}
        )
        failure = (
            HarnessInfrastructureFailure(
                code=result.error.code,
                category=_failure_category(result.error.code),  # type: ignore[arg-type]
                message=result.error.message,
                retryable=(
                    bool(result.error.details["retryable"])
                    if isinstance(result.error.details.get("retryable"), bool)
                    else None
                ),
                details=dict(result.error.details),
            )
            if infrastructure_failure and result.error is not None
            else None
        )
        if context.baseline_sha256 is None:
            raise ValueError("harness result requires an immutable baseline digest")
        try:
            if (
                Path(result.worktree_path).resolve()
                == Path(context.repository_path).resolve()
            ):
                raise ValueError(
                    "harness execution must not use the target repository directly"
                )
        except OSError as error:
            raise ValueError("harness worktree identity is invalid") from error
        raw_execution_identity = result.runner_telemetry.get(
            "harness_execution_identity"
        )
        execution_identity = None
        if isinstance(raw_execution_identity, Mapping):
            execution_identity = HarnessExecutionIdentity.model_validate(
                {
                    "harness_id": raw_execution_identity.get("harness_id")
                    or self.identity.harness.harness_id,
                    "harness_version": raw_execution_identity.get("harness_version")
                    or self.identity.harness.version,
                    "protocol": raw_execution_identity.get("protocol")
                    or self.identity.harness.protocol,
                    "protocol_version": raw_execution_identity.get("protocol_version")
                    or self.identity.harness.protocol_version,
                    "protocol_schema_digest": raw_execution_identity.get(
                        "protocol_schema_digest"
                    ),
                    "session_id": raw_execution_identity.get("session_id"),
                    "thread_id": raw_execution_identity.get("thread_id"),
                    "turn_id": raw_execution_identity.get("turn_id"),
                    "model_id": raw_execution_identity.get("model_id"),
                    "provider": raw_execution_identity.get("provider"),
                    "reasoning_effort": raw_execution_identity.get("reasoning_effort"),
                    "system_metadata": raw_execution_identity.get("system_metadata")
                    if isinstance(
                        raw_execution_identity.get("system_metadata"), Mapping
                    )
                    else {},
                }
            )
        raw_per_model = result.runner_telemetry.get("per_model_usage")
        per_model_usage = (
            {
                str(model): dict(value)
                for model, value in raw_per_model.items()
                if isinstance(value, Mapping) and not str(model).startswith("_")
            }
            if isinstance(raw_per_model, Mapping)
            else {}
        )
        raw_per_model_cost = (
            raw_per_model.get("_cost_usd")
            if isinstance(raw_per_model, Mapping)
            else None
        )
        per_model_cost = (
            {
                str(model): float(value)
                for model, value in raw_per_model_cost.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            if isinstance(raw_per_model_cost, Mapping)
            else {}
        )
        reported_currency = result.runner_telemetry.get(
            "provider_reported_cost_currency"
        )
        reported_source = result.runner_telemetry.get("provider_reported_cost_source")
        raw_cost_breakdown = result.runner_telemetry.get("cost_breakdown")
        cost_breakdown = (
            raw_cost_breakdown if isinstance(raw_cost_breakdown, Mapping) else {}
        )
        return HarnessResult(
            system_id=self.identity.system_id,
            session_id=session.session_id,
            run_id=context.run_id,
            attempt_id=context.attempt_id,
            isolated_worktree=result.worktree_path,
            baseline_digest=context.baseline_sha256,
            patch=result.patch,
            changed_files=changed_files,
            stdout=result.stdout,
            stderr=result.stderr,
            normalized_events=list(events),
            raw_trace=dict(result.trace),
            execution_identity=execution_identity,
            usage=HarnessUsage(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                accounting_status=result.token_accounting_status,
                per_model=per_model_usage,
            ),
            cost=HarnessCost(
                amount=result.cost_usd,
                currency=(
                    str(
                        reported_currency
                        or cost_breakdown.get("currency")
                        or self.identity.billing.currency
                    )
                    if result.cost_usd is not None
                    else None
                ),
                accounting_status=result.cost_accounting_status,
                source=(
                    str(
                        reported_source
                        or cost_breakdown.get("source")
                        or self.identity.billing.cost_source
                    )
                    if result.cost_usd is not None
                    else None
                ),
                per_model=per_model_cost,
            ),
            duration_ms=result.duration_ms,
            duration_accounting_status=result.duration_accounting_status,
            harness_status=result.status,
            infrastructure_failure=failure,
            artifacts=list(self.collect_artifacts(context, result)),
            cleanup=cleanup,
        )

    def cleanup(self, session: HarnessSession) -> CleanupResult:
        with self._lock:
            active = self._sessions.pop(session.session_id, None)
        return CleanupResult(
            status="succeeded" if active is not None else "not_required",
            completed_at=utc_now(),
            details={
                "adapter_session_released": active is not None,
                "execution_environment_cleanup": "completed_by_villani_code_adapter",
            },
        )

    def doctor(self) -> AgentSystemDoctorReport:
        probe = self.probe()
        available = bool(probe["available"])
        readiness = self.identity.readiness
        return AgentSystemDoctorReport(
            system_id=self.identity.system_id,
            checked_at=utc_now(),
            selectable=bool(self.identity.production_enabled and available),
            checks=[
                DoctorCheck(
                    name="production_enablement",
                    status="pass" if self.identity.production_enabled else "fail",
                    message=(
                        "Agent system is production enabled."
                        if self.identity.production_enabled
                        else "Agent system is disabled and cannot be selected."
                    ),
                ),
                DoctorCheck(
                    name="protocol_negotiation",
                    status="pass"
                    if readiness is None
                    or readiness.details.get("protocol_probe") != "failed"
                    else "fail",
                    message=f"Protocol {self.identity.harness.protocol_version}.",
                ),
                DoctorCheck(
                    name="executable",
                    status="pass" if available else "fail",
                    message=(
                        "Harness executable is available."
                        if available
                        else "Harness executable is missing."
                    ),
                    evidence={"command": probe["command"]},
                ),
                DoctorCheck(
                    name="exact_version",
                    status=(
                        "pass"
                        if readiness is None or readiness.version_supported is not False
                        else "fail"
                    ),
                    message=(
                        f"Detected {self.identity.harness.version}; supported range "
                        f"{readiness.supported_version_range or 'adapter-managed'}."
                        if readiness is not None
                        else f"Detected {self.identity.harness.version}."
                    ),
                ),
                DoctorCheck(
                    name="authentication",
                    status=(
                        "pass"
                        if readiness is None
                        or readiness.authentication_status
                        in {"ready", "not_applicable"}
                        else "fail"
                        if readiness.authentication_status == "not_ready"
                        else "unknown"
                    ),
                    message=(
                        f"Authentication is {readiness.authentication_status}."
                        if readiness is not None
                        else "Authentication is adapter-managed."
                    ),
                ),
                DoctorCheck(
                    name="qualification",
                    status=(
                        "pass"
                        if self.identity.qualification_status
                        in {"qualified", "bootstrap"}
                        else "unknown"
                        if self.identity.qualification_status
                        in {"experimental", "provisional"}
                        else "fail"
                    ),
                    message=(
                        f"Qualification is {self.identity.qualification_status}; "
                        "Gate C is not implied."
                    ),
                    evidence={
                        "conformance_status": (
                            readiness.conformance_status if readiness else "not_run"
                        ),
                        "repair_action": (
                            readiness.repair_action
                            if readiness
                            else "Run the harness conformance suite."
                        ),
                    },
                ),
            ],
        )

    def execute_focused_probes(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
        requests: list[Mapping[str, Any]],
    ) -> AttemptResult:
        return self._implementation.execute_focused_probes(
            attempt_context, attempt_result, requests
        )

    def _require(self, session: HarnessSession, context: AttemptContext) -> None:
        with self._lock:
            active = self._sessions.get(session.session_id)
        if active is None or active[1].attempt_id != context.attempt_id:
            raise RuntimeError("harness session does not match attempt context")


class AgentSystemAttemptRunner:
    """Select one qualified configured system, then execute its lifecycle."""

    def __init__(
        self,
        identities: tuple[AgentSystemIdentity, ...],
        by_backend: Mapping[str, AgentSystemIdentity],
        adapters: Mapping[str, HarnessAdapter],
        *,
        migration_report: Mapping[str, Any],
        qualification_store: QualificationStore | None = None,
        backends: Mapping[str, Backend] | None = None,
    ) -> None:
        self.agent_system_identities = identities
        self.agent_system_identity_by_backend = dict(by_backend)
        self._adapters = dict(adapters)
        self.agent_system_migration_report = dict(migration_report)
        self.qualification_store = qualification_store
        self.backends = dict(backends or {})

    def _resolve(
        self, backend_name: str | AttemptContext
    ) -> tuple[AgentSystemIdentity, HarnessAdapter]:
        context = backend_name if isinstance(backend_name, AttemptContext) else None
        resolved_backend_name = (
            context.backend_name if context is not None else backend_name
        )
        identity = self.agent_system_identity_by_backend.get(resolved_backend_name)
        if identity is None:
            raise ValueError(
                f"backend {resolved_backend_name!r} has no configured agent system"
            )
        if not identity.production_enabled:
            raise ValueError(f"agent system {identity.system_id} is disabled")
        if context is not None and self.qualification_store is not None:
            classification = context.classification
            requested = task_profile(
                str(classification.get("category") or "unknown"),
                str(classification.get("difficulty") or "hard"),
                str(classification.get("risk") or "high"),
                classification.get("required_capabilities") or (),
            )
            backend = self.backends.get(resolved_backend_name)
            try:
                assessment = assess_qualification(
                    identity=identity,
                    repository=repository_qualification_context(
                        context.repository_path
                    ),
                    requested_task=requested,
                    configuration=context.policy_configuration,
                    store=self.qualification_store,
                    backend_execution_selection=(
                        backend.execution_environment if backend is not None else None
                    ),
                )
            except Exception as error:
                raise ValueError(
                    f"repository qualification could not be proved for {identity.system_id}: {error}"
                ) from error
            qualification = context.policy_configuration.get("qualification")
            values = qualification if isinstance(qualification, Mapping) else {}
            manual = values.get("manual_override")
            manual_values = manual if isinstance(manual, Mapping) else {}
            manual_route = str(manual_values.get("route_name") or "")
            manual_experimental = bool(
                manual_values.get("allow_experimental") is True
                and manual_route
                in {resolved_backend_name, identity.route_name}
            )
            setup = context.policy_configuration.get("setup")
            setup_values = setup if isinstance(setup, Mapping) else {}
            model_management = context.policy_configuration.get("model_management")
            model_values = (
                model_management if isinstance(model_management, Mapping) else {}
            )
            setup_bootstrap_experimental = bool(
                setup_values.get("bootstrap_policy") is True
                and str(model_values.get("bootstrap_default") or "")
                == resolved_backend_name
            )
            if assessment.state == "unsupported":
                raise ValueError(
                    f"agent system {identity.system_id} is unsupported for this repository: {assessment.caveat}"
                )
            if assessment.state == "experimental" and not (
                manual_experimental or setup_bootstrap_experimental
            ):
                raise ValueError(
                    f"agent system {identity.system_id} is Experimental and requires an explicit manual override"
                )
            if assessment.state not in {
                "qualified",
                "provisional",
                "experimental",
            }:
                raise ValueError(
                    f"agent system {identity.system_id} is not eligible for this repository"
                )
        elif identity.qualification_status not in {
            "qualified",
            "bootstrap",
            "provisional",
        }:
            raise ValueError(
                f"agent system {identity.system_id} is not qualified for production"
            )
        adapter = self._adapters.get(identity.system_id)
        if adapter is None:
            raise ValueError(
                f"agent system {identity.system_id} has no production adapter"
            )
        return identity, adapter

    def run(self, attempt_context: AttemptContext) -> AttemptResult:
        identity, adapter = self._resolve(attempt_context)
        session = adapter.prepare_session(attempt_context)
        try:
            result = adapter.execute_task(session, attempt_context)
            events = adapter.stream_events(session, result)
        finally:
            cleanup = adapter.cleanup(session)
        harness_result = adapter.collect_result(
            session, attempt_context, result, events, cleanup
        )
        relative = f"attempts/{attempt_context.attempt_id}/harness-result.json"
        write_json_atomic(
            Path(attempt_context.run_directory) / relative,
            redact_data(harness_result.model_dump(mode="json")),
        )
        runtime_events = tuple(
            RuntimeEvent(
                event_type=event.name.replace(".", "_").replace("-", "_"),
                timestamp=event.timestamp,
                payload={
                    **event.payload,
                    "session_id": session.session_id,
                    "agent_system_id": identity.system_id,
                    "raw_namespace": event.raw_namespace,
                    "raw_name": event.raw_name,
                },
                source_event_id=f"{session.session_id}:{event.sequence}",
            )
            for event in events
        )
        return replace(
            result,
            runtime_events=runtime_events,
            metadata={
                **dict(result.metadata),
                "agent_system_id": identity.system_id,
                "agent_system_identity_path": f"agent-systems/{identity.system_id}.json",
                "harness_result_path": relative,
                "harness_session_id": session.session_id,
                "harness_cleanup": cleanup.model_dump(mode="json"),
            },
        )

    def execute_focused_probes(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
        requests: list[Mapping[str, Any]],
    ) -> AttemptResult:
        _, adapter = self._resolve(attempt_context.backend_name)
        execute = getattr(adapter, "execute_focused_probes", None)
        if not callable(execute):
            raise TypeError("agent system does not support focused probes")
        updated = execute(attempt_context, attempt_result, requests)
        return replace(
            updated,
            metadata={
                **dict(updated.metadata),
                **{
                    key: value
                    for key, value in attempt_result.metadata.items()
                    if key.startswith("agent_system_") or key.startswith("harness_")
                },
            },
        )


__all__ = [
    "AgentSystemAttemptRunner",
    "HarnessAdapter",
    "VillaniCodeHarnessAdapter",
    "normalize_events",
]
