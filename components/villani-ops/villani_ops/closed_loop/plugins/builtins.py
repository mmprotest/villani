"""Trusted in-process wrappers for the canonical implementations.

The wrappers deliberately construct the same RPC request/response envelopes used by
subprocess plugins. They retain the existing Python objects only as an implementation
detail, so canonical behavior and controller interfaces stay unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..interfaces import (
    AttemptContext,
    AttemptResult,
    AttemptRunner,
    EligibleCandidate,
    Materialization,
    MaterializationContext,
    Materializer,
    Selection,
    SelectionContext,
    Selector,
    Verification,
    Verifier,
)
from .models import (
    AgentRunnerPlugin,
    ExecutionProviderPlugin,
    MaterializerPlugin,
    PROTOCOL_VERSIONS,
    PluginCallRequest,
    PluginCallResponse,
    PluginKind,
    ResourceRequirements,
    SelectorPlugin,
    VerifierPlugin,
)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in fields(value)
            if not item.metadata.get("plugin_exclude")
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _digest(source: Path) -> str:
    return f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"


def _fields(
    kind: PluginKind,
    name: str,
    version: str,
    capabilities: list[str],
    source: Path,
) -> dict[str, Any]:
    return {
        "name": name,
        "version": version,
        "protocol_versions": [PROTOCOL_VERSIONS[kind]],
        "capabilities": capabilities,
        "configuration_schema": {"type": "object", "additionalProperties": True},
        "required_secrets": [],
        "supported_platforms": ["any"],
        "resource_requirements": ResourceRequirements(),
        "trust_level": "built_in_trusted",
        "enabled": True,
        "transport": "in-process",
        "digest": _digest(source),
        "builtin": True,
    }


_CLOSED_LOOP = Path(__file__).resolve().parents[1]
_PACKAGE = _CLOSED_LOOP.parent

AGENT_RUNNER_MANIFEST = AgentRunnerPlugin(
    **_fields(
        PluginKind.AGENT_RUNNER,
        "villani-code-runner",
        "1.0.0",
        ["coding", "isolated_worktree", "telemetry"],
        _CLOSED_LOOP / "adapters" / "villani_code_attempt.py",
    )
)
VERIFIER_MANIFEST = VerifierPlugin(
    **_fields(
        PluginKind.VERIFIER,
        "villani-verifier",
        "1.0.0",
        ["evidence_verification", "fail_closed_acceptance"],
        _CLOSED_LOOP / "adapters" / "villani_verifier.py",
    )
)
SELECTOR_MANIFEST = SelectorPlugin(
    **_fields(
        PluginKind.SELECTOR,
        "villani-deterministic-selector",
        "1.0.0",
        ["deterministic_evidence_ranking"],
        _CLOSED_LOOP / "adapters" / "evidence_selector.py",
    )
)
MATERIALIZER_MANIFEST = MaterializerPlugin(
    **_fields(
        PluginKind.MATERIALIZER,
        "villani-patch-materializer",
        "1.0.0",
        ["recorded_patch_only", "safe_apply"],
        _CLOSED_LOOP / "adapters" / "patch_materializer.py",
    )
)
EXECUTION_PROVIDER_MANIFEST = ExecutionProviderPlugin(
    **_fields(
        PluginKind.EXECUTION_PROVIDER,
        "villani-execution-providers",
        "1.0.0",
        ["inherit", "setup-command", "container", "devcontainer"],
        _PACKAGE / "execution_environment" / "providers.py",
    )
)


def builtin_plugin_manifests() -> tuple[Any, ...]:
    return (
        AGENT_RUNNER_MANIFEST,
        VERIFIER_MANIFEST,
        SELECTOR_MANIFEST,
        MATERIALIZER_MANIFEST,
        EXECUTION_PROVIDER_MANIFEST,
    )


class _InProcessBoundary:
    plugin_manifest: Any

    def _round_trip(self, operation: str, payload: dict[str, Any], result: Any) -> None:
        protocol = PROTOCOL_VERSIONS[self.plugin_manifest.kind]
        request = PluginCallRequest(
            request_id="in_process",
            protocol_version=protocol,
            operation=operation,
            payload=_json_value(payload),
        )
        request = PluginCallRequest.model_validate_json(request.model_dump_json())
        response = PluginCallResponse(
            request_id=request.request_id,
            protocol_version=request.protocol_version,
            status="ok",
            result={"value": _json_value(result)},
        )
        PluginCallResponse.model_validate_json(response.model_dump_json())


class BuiltinAgentRunnerPlugin(_InProcessBoundary):
    plugin_manifest = AGENT_RUNNER_MANIFEST
    additional_plugin_manifests = (EXECUTION_PROVIDER_MANIFEST,)

    def __init__(self, implementation: AttemptRunner) -> None:
        self.implementation = implementation

    def run(self, attempt_context: AttemptContext) -> AttemptResult:
        result = self.implementation.run(attempt_context)
        self._round_trip("run", {"attempt_context": attempt_context}, result)
        return result


class BuiltinVerifierPlugin(_InProcessBoundary):
    plugin_manifest = VERIFIER_MANIFEST

    def __init__(self, implementation: Verifier) -> None:
        self.implementation = implementation

    def verify(
        self, attempt_context: AttemptContext, attempt_result: AttemptResult
    ) -> Verification:
        result = self.implementation.verify(attempt_context, attempt_result)
        self._round_trip(
            "verify",
            {"attempt_context": attempt_context, "attempt_result": attempt_result},
            result,
        )
        return result


class BuiltinSelectorPlugin(_InProcessBoundary):
    plugin_manifest = SELECTOR_MANIFEST

    def __init__(self, implementation: Selector) -> None:
        self.implementation = implementation

    def select(
        self,
        eligible_candidates: tuple[EligibleCandidate, ...],
        context: SelectionContext,
    ) -> Selection:
        result = self.implementation.select(eligible_candidates, context)
        self._round_trip(
            "select",
            {"eligible_candidates": eligible_candidates, "context": context},
            result,
        )
        return result


class BuiltinMaterializerPlugin(_InProcessBoundary):
    plugin_manifest = MATERIALIZER_MANIFEST

    def __init__(self, implementation: Materializer) -> None:
        self.implementation = implementation

    def materialize(
        self, selection: Selection, context: MaterializationContext
    ) -> Materialization:
        result = self.implementation.materialize(selection, context)
        self._round_trip(
            "materialize", {"selection": selection, "context": context}, result
        )
        return result


class BuiltinExecutionProviderPlugin(_InProcessBoundary):
    """Proxy an existing execution provider through the common in-process envelope."""

    plugin_manifest = EXECUTION_PROVIDER_MANIFEST

    def __init__(self, implementation: Any) -> None:
        self.implementation = implementation
        self.config = implementation.config
        self.name = implementation.name

    def prepare(self, *, repository: Path, worktree: Path) -> Any:
        result = self.implementation.prepare(repository=repository, worktree=worktree)
        self._round_trip(
            "prepare", {"repository": repository, "worktree": worktree}, result
        )
        return result

    def command_environment(self, prepared: Any) -> dict[str, str]:
        result = self.implementation.command_environment(prepared)
        self._round_trip("command_environment", {"prepared": prepared}, result)
        return result

    def execute(self, prepared: Any, command: Any) -> Any:
        result = self.implementation.execute(prepared, command)
        self._round_trip(
            "execute", {"prepared": prepared, "command": list(command)}, result
        )
        return result

    def collect(self, prepared: Any) -> dict[str, Any]:
        result = self.implementation.collect(prepared)
        self._round_trip("collect", {"prepared": prepared}, result)
        return result

    def cleanup(self, prepared: Any) -> None:
        self.implementation.cleanup(prepared)
        self._round_trip("cleanup", {"prepared": prepared}, {})

    def capability_report(self) -> dict[str, Any]:
        result = self.implementation.capability_report()
        self._round_trip("capability_report", {}, result)
        return result

    def fingerprint(self, repository: Path) -> str:
        result = self.implementation.fingerprint(repository)
        self._round_trip(
            "fingerprint", {"repository": repository}, {"fingerprint": result}
        )
        return result

    def wrap_command(self, prepared: Any, command: Any) -> list[str]:
        result = self.implementation.wrap_command(prepared, command)
        self._round_trip(
            "wrap_command",
            {"prepared": prepared, "command": list(command)},
            {"command": result},
        )
        return result

    def validate_command(self, prepared: Any, command: Any) -> None:
        self.implementation.validate_command(prepared, command)

    def runner_controls(self, prepared: Any) -> Any:
        method = getattr(self.implementation, "runner_controls", None)
        return method(prepared) if callable(method) else None
