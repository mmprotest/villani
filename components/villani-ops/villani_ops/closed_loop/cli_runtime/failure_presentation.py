"""Safe, provider-neutral presentation for CLI infrastructure failures."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import ConfigDict, Field

from ..agent_systems.role_models import AgentRole
from ..durable_io import write_json_atomic
from ..protocol import StrictProtocolModel
from .models import CliFailure, CliProcessResult


CLI_INFRASTRUCTURE_FAILURE_SCHEMA_VERSION = (
    "villani.cli_infrastructure_failure_presentation.v1"
)


class CliInfrastructureFailurePresentation(StrictProtocolModel):
    """Facts required to distinguish tool failure from task rejection."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "villani.cli_infrastructure_failure_presentation.v1"
    ] = "villani.cli_infrastructure_failure_presentation.v1"
    stage: Literal["classification", "coding", "verification", "selection"]
    role: AgentRole
    agent_system_id: str = Field(min_length=1)
    safe_error_summary: str = Field(min_length=1, max_length=1000)
    target_repository_modified: bool
    partial_patch_preserved: bool
    automatic_fallback_performed: bool
    exact_repair_action: str = Field(min_length=1, max_length=1000)
    evidence_path: str = Field(min_length=1)


def _safe_reference(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return "agent/process-result.json"
    return normalized or "agent/process-result.json"


def _failure_code(process: CliProcessResult | Mapping[str, Any]) -> str:
    failures = (
        process.failures
        if isinstance(process, CliProcessResult)
        else process.get("failures", [])
    )
    if failures:
        first = failures[0]
        code = first.code if hasattr(first, "code") else first.get("code")
        return str(getattr(code, "value", code) or "unknown_infrastructure_failure")
    return "unknown_infrastructure_failure"


def _summary(process: CliProcessResult | Mapping[str, Any]) -> str:
    failures = (
        process.failures
        if isinstance(process, CliProcessResult)
        else process.get("failures", [])
    )
    if failures:
        first = failures[0]
        value = first.message if hasattr(first, "message") else first.get("message")
        if value:
            return str(value)[:1000]
    state = (
        process.infrastructure_state
        if isinstance(process, CliProcessResult)
        else process.get("infrastructure_state")
    )
    return f"CLI role process ended in infrastructure state {state or 'failed'}."


def _repair_action(agent_system_id: str, code: str) -> str:
    command = f"villani agents doctor {agent_system_id}"
    if code == CliFailure.TIMEOUT.value:
        return (
            f"Run `{command}`; then increase this role's configured timeout or fix "
            "the reported CLI hang before retrying."
        )
    if code in {
        CliFailure.MALFORMED_STREAM.value,
        CliFailure.EVENT_LINE_LIMIT_EXCEEDED.value,
        CliFailure.OUTPUT_DECODE_FAILED.value,
        CliFailure.FINAL_OUTPUT_MISSING.value,
    }:
        return f"Run `{command}` and install a version that passes structured-output conformance."
    if code in {
        CliFailure.EXECUTABLE_NOT_FOUND.value,
        CliFailure.EXECUTABLE_NOT_RUNNABLE.value,
        CliFailure.SPAWN_FAILED.value,
    }:
        return f"Install or repair the configured executable, then run `{command}`."
    if code == CliFailure.CANCELLED.value:
        return f"Run `{command}` and start a new run when the cancellation cause is resolved."
    return f"Run `{command}` and apply its exact reported repair action before retrying."


def build_cli_failure_presentation(
    *,
    role: AgentRole,
    agent_system_id: str,
    process: CliProcessResult | Mapping[str, Any],
    target_repository_modified: bool,
    partial_patch_preserved: bool,
    automatic_fallback_performed: bool,
    evidence_path: str,
) -> CliInfrastructureFailurePresentation:
    code = _failure_code(process)
    return CliInfrastructureFailurePresentation(
        stage=role.value,  # type: ignore[arg-type]
        role=role,
        agent_system_id=agent_system_id,
        safe_error_summary=_summary(process),
        target_repository_modified=target_repository_modified,
        partial_patch_preserved=partial_patch_preserved,
        automatic_fallback_performed=automatic_fallback_performed,
        exact_repair_action=_repair_action(agent_system_id, code),
        evidence_path=_safe_reference(evidence_path),
    )


def write_cli_failure_presentation(
    path: Path,
    presentation: CliInfrastructureFailurePresentation,
) -> None:
    write_json_atomic(path, presentation.model_dump(mode="json"))


__all__ = [
    "CLI_INFRASTRUCTURE_FAILURE_SCHEMA_VERSION",
    "CliInfrastructureFailurePresentation",
    "build_cli_failure_presentation",
    "write_cli_failure_presentation",
]
