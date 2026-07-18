"""Versioned contracts for complete coding-agent systems and harness sessions."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


AGENT_SYSTEM_SCHEMA_VERSION = "villani.agent_system.v1"
HARNESS_PROTOCOL_VERSION = "villani.harness_adapter.v1"
HARNESS_RESULT_SCHEMA_VERSION = "villani.harness_result.v1"
HARNESS_CONFORMANCE_SCHEMA_VERSION = "villani.harness_conformance_report.v1"
HARNESS_DISCOVERY_SCHEMA_VERSION = "villani.harness_discovery.v1"
MAXIMUM_HARNESS_MESSAGE_BYTES = 8 * 1024 * 1024
MAXIMUM_BUFFERED_EVENT_BYTES = 32 * 1024 * 1024

HARNESS_LIFECYCLE_OPERATIONS = (
    "probe",
    "describe_capabilities",
    "prepare_session",
    "execute_task",
    "stream_events",
    "request_cancellation",
    "collect_result",
    "collect_artifacts",
    "cleanup",
    "doctor",
)

HARNESS_RUNTIME_CONTRACT = {
    "stdout_stderr_policy": "separate_channels",
    "timeout_policy": "controller_attempt_deadline",
    "cancellation_policy": "cooperative_then_process_tree",
    "permission_policy": "normalized_request_and_resolution_events",
    "backpressure_policy": "bounded_buffer_fail_closed",
    "max_stdout_bytes": MAXIMUM_HARNESS_MESSAGE_BYTES,
    "max_stderr_bytes": MAXIMUM_HARNESS_MESSAGE_BYTES,
    "max_buffered_event_bytes": MAXIMUM_BUFFERED_EVENT_BYTES,
}


CAPABILITY_NAMES = (
    "file_editing",
    "command_execution",
    "streaming",
    "cancellation",
    "usage_reporting",
    "cost_reporting",
    "model_identity",
    "session_identity",
    "resume",
    "fork",
    "permission_requests",
    "custom_model",
    "custom_provider",
    "local_model",
    "mcp",
    "acp",
    "structured_result",
    "complete_trace",
    "isolated_worktree",
    "non_interactive_execution",
)

NORMALIZED_EVENT_NAMES = (
    "session_started",
    "agent_message",
    "reasoning_summary",
    "plan_update",
    "command_start",
    "command_output",
    "command_complete",
    "file_change_start",
    "file_change_complete",
    "tool_call_start",
    "tool_call_complete",
    "permission_request",
    "permission_resolution",
    "usage_update",
    "retry",
    "warning",
    "harness_error",
    "session_complete",
    "cancellation",
)

REQUIRED_HARNESS_CONFORMANCE_CHECKS = (
    "manifest",
    "protocol_negotiation",
    "version_capture",
    "worktree_enforcement",
    "path_safety",
    "event_ordering",
    "cancellation",
    "timeout",
    "malformed_output",
    "oversized_output",
    "process_crash",
    "missing_executable",
    "permissions",
    "artifacts",
    "patch_correctness",
    "cleanup",
    "secret_redaction",
    "unknown_cost",
    "cross_platform_paths",
    "successful_patch",
    "no_patch",
    "command_recovery",
    "permission_request",
    "rate_limit_retry",
    "unsupported_version",
    "schema_change",
    "missing_final_result",
    "partial_patch_on_crash",
    "known_cost",
    "non_ascii_spaced_paths",
    "large_output",
    "outside_isolation_mutation",
)


class StrictAgentSystemModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilityState(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class CapabilitySource(str, Enum):
    DECLARED = "declared"
    DETECTED = "detected"
    CONFORMANCE = "conformance_tested"
    UNSUPPORTED = "unsupported"


class CapabilityEvidence(StrictAgentSystemModel):
    source: CapabilitySource
    reference: str = Field(min_length=1)
    observed_at: datetime | None = None
    digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class CapabilityAssessment(StrictAgentSystemModel):
    state: CapabilityState
    evidence: list[CapabilityEvidence] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> "CapabilityAssessment":
        if self.state == CapabilityState.UNSUPPORTED and not any(
            item.source == CapabilitySource.UNSUPPORTED for item in self.evidence
        ):
            raise ValueError("unsupported capabilities require unsupported evidence")
        return self


class HarnessIdentity(StrictAgentSystemModel):
    harness_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    executable_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    transport: Literal[
        "local_subprocess",
        "acp_stdio",
        "direct_protocol",
        "structured_headless_cli",
    ]


class ModelProviderIdentity(StrictAgentSystemModel):
    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_revision: str | None = None
    endpoint_identity: str | None = None
    serving_engine: str | None = None
    serving_engine_version: str | None = None
    context_metadata: dict[str, Any] = Field(default_factory=dict)
    tool_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_endpoint_credentials(self) -> "ModelProviderIdentity":
        if self.endpoint_identity:
            parsed = urllib.parse.urlsplit(self.endpoint_identity)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "endpoint identity cannot contain credentials, query, or fragment"
                )
        return self


class ExecutionIdentity(StrictAgentSystemModel):
    execution_provider: str = Field(min_length=1)
    environment_fingerprint: str | None = None
    permission_profile: str = Field(min_length=1)
    network_policy: Literal["none", "restricted", "allowed", "unknown"]
    sandbox_identity: str | None = None


class RouteProfile(StrictAgentSystemModel):
    repository_profile: str = Field(min_length=1)
    task_profile: str = Field(min_length=1)
    verification_policy: str = Field(min_length=1)
    tool_protocol: str = Field(min_length=1)
    prompt_protocol: str = Field(min_length=1)


class QualificationReference(StrictAgentSystemModel):
    kind: Literal["declared", "detected", "conformance", "operator"]
    reference: str = Field(min_length=1)
    digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class BillingIdentity(StrictAgentSystemModel):
    mode: Literal["token", "compute_time", "fixed", "hybrid", "unknown"]
    cost_source: str | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    unknown_fields: list[str] = Field(default_factory=list)


class HarnessReadiness(StrictAgentSystemModel):
    """Non-secret discovery and enablement facts for one configured harness."""

    installed: bool
    command_identity: str = Field(min_length=1)
    exact_version: str | None = None
    supported_version_range: str | None = None
    version_supported: bool | None = None
    authentication_status: Literal["ready", "not_ready", "unknown", "not_applicable"]
    protocol: str = Field(min_length=1)
    conformance_status: Literal["passed", "failed", "not_run", "insufficient_evidence"]
    qualification_state: Literal[
        "qualified",
        "bootstrap",
        "experimental",
        "provisional",
        "unqualified",
        "unsupported",
        "disabled",
    ]
    custom_model_capability: Literal["supported", "unsupported", "unknown"]
    custom_provider_capability: Literal["supported", "unsupported", "unknown"]
    local_model_capability: Literal["supported", "unsupported", "unknown"]
    repair_action: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class HarnessDiscovery(StrictAgentSystemModel):
    schema_version: Literal["villani.harness_discovery.v1"] = (
        HARNESS_DISCOVERY_SCHEMA_VERSION
    )
    harness_id: Literal["villani-code", "codex", "claude-code"]
    display_name: str = Field(min_length=1)
    readiness: HarnessReadiness
    detected_at: datetime


class AgentSystemIdentity(StrictAgentSystemModel):
    schema_version: Literal["villani.agent_system.v1"] = AGENT_SYSTEM_SCHEMA_VERSION
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    route_name: str = Field(min_length=1)
    production_enabled: bool
    qualification_status: Literal[
        "qualified",
        "bootstrap",
        "experimental",
        "provisional",
        "unqualified",
        "unsupported",
        "disabled",
    ]
    harness: HarnessIdentity
    model_provider: ModelProviderIdentity
    execution: ExecutionIdentity
    route_profile: RouteProfile
    capabilities: dict[str, CapabilityAssessment]
    qualification_references: list[QualificationReference]
    billing: BillingIdentity
    readiness: HarnessReadiness | None = None
    detection_time: datetime
    detection_source: str = Field(min_length=1)
    configuration_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    configuration: dict[str, Any]
    redaction_status: Literal["redacted", "no_sensitive_values_detected"]
    unknown_fields: list[str]

    @model_validator(mode="after")
    def validate_identity(self) -> "AgentSystemIdentity":
        actual_digest, projection, removed_sensitive_values = configuration_digest(
            self.configuration
        )
        if removed_sensitive_values or projection != self.configuration:
            raise ValueError("identity configuration must already be secret-free")
        if self.configuration_digest != actual_digest:
            raise ValueError(
                "configuration_digest must address the canonical configuration"
            )
        digest = self.configuration_digest.removeprefix("sha256:")
        if self.system_id != f"asys_{digest}":
            raise ValueError("system_id must be derived from configuration_digest")
        missing = sorted(set(CAPABILITY_NAMES) - set(self.capabilities))
        extra = sorted(set(self.capabilities) - set(CAPABILITY_NAMES))
        if missing or extra:
            raise ValueError(
                f"capability map mismatch; missing={missing!r}, extra={extra!r}"
            )
        if self.production_enabled and self.qualification_status in {
            "disabled",
            "unsupported",
            "unqualified",
        }:
            raise ValueError(
                "production-enabled systems must be bootstrap or qualified"
            )
        if self.qualification_status == "qualified" and not any(
            item.kind == "conformance" for item in self.qualification_references
        ):
            raise ValueError(
                "qualified systems require a conformance qualification reference"
            )
        return self


class HarnessSession(StrictAgentSystemModel):
    session_id: str = Field(min_length=1)
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    state: Literal[
        "prepared", "executing", "completed", "failed", "cancelled", "cleaned"
    ]
    prepared_at: datetime


class NormalizedHarnessEvent(StrictAgentSystemModel):
    sequence: int = Field(ge=1)
    timestamp: datetime
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    payload: dict[str, Any]
    raw_namespace: str | None = None
    raw_name: str | None = None

    @model_validator(mode="after")
    def validate_name(self) -> "NormalizedHarnessEvent":
        if self.name not in NORMALIZED_EVENT_NAMES:
            if not self.raw_namespace or not self.raw_name:
                raise ValueError(
                    "unknown events require raw_namespace and raw_name preservation"
                )
        if self.name == "permission_request" and not {
            "request_id",
            "permission",
        }.issubset(self.payload):
            raise ValueError("permission requests require request_id and permission")
        if self.name == "permission_resolution" and not {
            "request_id",
            "resolution",
        }.issubset(self.payload):
            raise ValueError("permission resolutions require request_id and resolution")
        return self


class HarnessArtifact(StrictAgentSystemModel):
    kind: str = Field(min_length=1)
    path: str = Field(min_length=1)
    digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_path(self) -> "HarnessArtifact":
        normalized = self.path.replace("\\", "/")
        if (
            PurePosixPath(normalized).is_absolute()
            or PureWindowsPath(self.path).is_absolute()
            or ".." in PurePosixPath(normalized).parts
        ):
            raise ValueError("artifact paths must be run-relative and safe")
        return self


class HarnessUsage(StrictAgentSystemModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    accounting_status: Literal["complete", "partial", "unknown", "not_applicable"]
    per_model: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_accounting(self) -> "HarnessUsage":
        values = (self.input_tokens, self.output_tokens)
        if self.accounting_status == "complete" and any(
            value is None for value in values
        ):
            raise ValueError("complete usage requires token counts")
        if self.accounting_status in {"unknown", "not_applicable"} and any(
            value is not None for value in values
        ):
            raise ValueError("unknown usage values must be null")
        return self


class HarnessCost(StrictAgentSystemModel):
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    accounting_status: Literal["complete", "partial", "unknown", "not_applicable"]
    source: str | None = None
    per_model: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unknown_cost(self) -> "HarnessCost":
        if self.accounting_status in {"unknown", "not_applicable"} and (
            self.amount is not None or self.currency is not None
        ):
            raise ValueError("unknown cost must be null, never numeric zero")
        if self.accounting_status == "complete" and (
            self.amount is None or self.currency is None
        ):
            raise ValueError("complete cost requires amount and currency")
        if any(value < 0 for value in self.per_model.values()):
            raise ValueError("per-model cost cannot be negative")
        return self


class HarnessExecutionIdentity(StrictAgentSystemModel):
    """Identity actually acknowledged by the harness during this attempt."""

    harness_id: str = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    protocol: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    protocol_schema_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    model_id: str | None = None
    provider: str | None = None
    reasoning_effort: str | None = None
    system_metadata: dict[str, Any] = Field(default_factory=dict)


class CleanupResult(StrictAgentSystemModel):
    status: Literal["succeeded", "failed", "not_required", "unknown"]
    completed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class HarnessInfrastructureFailure(StrictAgentSystemModel):
    code: str = Field(min_length=1)
    category: Literal[
        "cancellation",
        "timeout",
        "protocol",
        "process",
        "missing_executable",
        "permission",
        "environment",
        "malformed_output",
        "oversized_output",
        "cleanup",
        "transport_overload",
        "rate_limit",
        "unknown",
    ]
    message: str = Field(min_length=1)
    retryable: bool | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class HarnessResult(StrictAgentSystemModel):
    schema_version: Literal["villani.harness_result.v1"] = HARNESS_RESULT_SCHEMA_VERSION
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    isolated_worktree: str = Field(min_length=1)
    baseline_digest: str = Field(pattern=r"^(sha256:)?[0-9a-f]{64}$")
    patch: str | None
    changed_files: list[str]
    stdout: str
    stderr: str
    normalized_events: list[NormalizedHarnessEvent]
    raw_trace: dict[str, Any]
    execution_identity: HarnessExecutionIdentity | None = None
    usage: HarnessUsage
    cost: HarnessCost
    duration_ms: int | None = Field(default=None, ge=0)
    duration_accounting_status: Literal[
        "complete", "partial", "unknown", "not_applicable"
    ]
    harness_status: Literal["completed", "failed", "cancelled"]
    infrastructure_failure: HarnessInfrastructureFailure | None
    artifacts: list[HarnessArtifact]
    cleanup: CleanupResult

    @model_validator(mode="after")
    def validate_equivalent_evidence(self) -> "HarnessResult":
        expected_sequences = list(range(1, len(self.normalized_events) + 1))
        if [item.sequence for item in self.normalized_events] != expected_sequences:
            raise ValueError(
                "normalized event sequences must be contiguous and ordered"
            )
        timestamps = [item.timestamp for item in self.normalized_events]
        if timestamps != sorted(timestamps):
            raise ValueError("normalized event timestamps must be ordered")
        worktree = self.isolated_worktree.replace("\\", "/")
        if ".." in PurePosixPath(worktree).parts:
            raise ValueError("isolated worktree path cannot contain parent traversal")
        for changed in self.changed_files:
            normalized = changed.replace("\\", "/")
            parts = normalized.split("/")
            if (
                not normalized
                or normalized.startswith("/")
                or re.match(r"^[A-Za-z]:", normalized)
                or ".." in parts
            ):
                raise ValueError(
                    "changed file paths must be worktree-relative and safe"
                )
        if len(self.stdout.encode("utf-8")) > MAXIMUM_HARNESS_MESSAGE_BYTES:
            raise ValueError("stdout exceeds the harness message bound")
        if len(self.stderr.encode("utf-8")) > MAXIMUM_HARNESS_MESSAGE_BYTES:
            raise ValueError("stderr exceeds the harness message bound")
        event_bytes = sum(
            len(
                json.dumps(
                    item.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for item in self.normalized_events
        )
        if event_bytes > MAXIMUM_BUFFERED_EVENT_BYTES:
            raise ValueError("normalized events exceed the backpressure buffer bound")
        if self.duration_accounting_status == "complete" and self.duration_ms is None:
            raise ValueError("complete duration requires duration_ms")
        if self.duration_accounting_status in {"unknown", "not_applicable"} and (
            self.duration_ms is not None
        ):
            raise ValueError("unknown duration must be null")
        return self


class DoctorCheck(StrictAgentSystemModel):
    name: str = Field(min_length=1)
    status: Literal["pass", "fail", "unknown", "skipped"]
    message: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class AgentSystemDoctorReport(StrictAgentSystemModel):
    schema_version: Literal["villani.agent_system_doctor.v1"] = (
        "villani.agent_system_doctor.v1"
    )
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    checked_at: datetime
    selectable: bool
    checks: list[DoctorCheck]


class HarnessConformanceCheck(StrictAgentSystemModel):
    check_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    status: Literal["pass", "fail", "not_run"]
    evidence: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_pass_evidence(self) -> "HarnessConformanceCheck":
        if self.status == "pass" and not self.evidence:
            raise ValueError("passing conformance checks require evidence")
        return self


class HarnessConformanceReport(StrictAgentSystemModel):
    schema_version: Literal["villani.harness_conformance_report.v1"] = (
        HARNESS_CONFORMANCE_SCHEMA_VERSION
    )
    report_id: str = Field(min_length=1)
    system_id: str = Field(pattern=r"^asys_[0-9a-f]{64}$")
    harness_id: str = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    generated_at: datetime
    status: Literal["passed", "failed", "insufficient_evidence"]
    checks: list[HarnessConformanceCheck]
    production_qualification_authorized: bool

    @model_validator(mode="after")
    def validate_status(self) -> "HarnessConformanceReport":
        check_ids = [item.check_id for item in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("conformance check IDs must be unique")
        if set(check_ids) != set(REQUIRED_HARNESS_CONFORMANCE_CHECKS):
            raise ValueError("conformance report must contain every required check")
        statuses = {item.status for item in self.checks}
        expected = (
            "failed"
            if "fail" in statuses
            else "insufficient_evidence"
            if "not_run" in statuses
            else "passed"
        )
        if self.status != expected:
            raise ValueError(f"conformance status must be {expected}")
        if self.production_qualification_authorized and expected != "passed":
            raise ValueError("qualification authorization must fail closed")
        return self


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:secret|password|passwd|token|authorization|auth|credential|"
    r"api[_-]?key|private[_-]?key|client[_-]?secret|bearer|cookie)(?:$|[_-])|"
    r"^(?:apikey|privatekey|accesskey|secretkey)$",
    re.IGNORECASE,
)


def non_secret_configuration(value: Any) -> tuple[Any, bool]:
    """Return a deterministic configuration projection with secret values removed."""

    redacted = False

    def visit(item: Any) -> Any:
        nonlocal redacted
        if isinstance(item, BaseModel):
            return visit(item.model_dump(mode="json"))
        if isinstance(item, Mapping):
            output: dict[str, Any] = {}
            for raw_key in sorted(item, key=lambda candidate: str(candidate)):
                key = str(raw_key)
                lowered = key.lower()
                is_reference = lowered.endswith(("_env", "_ref", "_name"))
                if _SENSITIVE_KEY.search(lowered) and not is_reference:
                    redacted = True
                    continue
                output[key] = visit(item[raw_key])
            return output
        if isinstance(item, (list, tuple)):
            return [visit(child) for child in item]
        if isinstance(item, Path):
            return item.as_posix()
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        return str(item)

    return visit(value), redacted


def configuration_digest(value: Mapping[str, Any]) -> tuple[str, dict[str, Any], bool]:
    projection, redacted = non_secret_configuration(value)
    if not isinstance(projection, dict):  # pragma: no cover - mapping guarantees this
        raise TypeError("agent-system configuration must project to an object")
    encoded = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}", projection, redacted


def file_digest(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


__all__ = [
    "AGENT_SYSTEM_SCHEMA_VERSION",
    "AgentSystemDoctorReport",
    "AgentSystemIdentity",
    "BillingIdentity",
    "CAPABILITY_NAMES",
    "CapabilityAssessment",
    "CapabilityEvidence",
    "CapabilitySource",
    "CapabilityState",
    "CleanupResult",
    "DoctorCheck",
    "ExecutionIdentity",
    "HARNESS_CONFORMANCE_SCHEMA_VERSION",
    "HARNESS_DISCOVERY_SCHEMA_VERSION",
    "HARNESS_LIFECYCLE_OPERATIONS",
    "HARNESS_PROTOCOL_VERSION",
    "HARNESS_RUNTIME_CONTRACT",
    "HarnessArtifact",
    "HarnessConformanceCheck",
    "HarnessConformanceReport",
    "HarnessCost",
    "HarnessDiscovery",
    "HarnessExecutionIdentity",
    "HarnessIdentity",
    "HarnessInfrastructureFailure",
    "HarnessResult",
    "HarnessReadiness",
    "HarnessSession",
    "HarnessUsage",
    "MAXIMUM_BUFFERED_EVENT_BYTES",
    "MAXIMUM_HARNESS_MESSAGE_BYTES",
    "ModelProviderIdentity",
    "NORMALIZED_EVENT_NAMES",
    "NormalizedHarnessEvent",
    "QualificationReference",
    "REQUIRED_HARNESS_CONFORMANCE_CHECKS",
    "RouteProfile",
    "configuration_digest",
    "file_digest",
    "non_secret_configuration",
    "utc_now",
]
