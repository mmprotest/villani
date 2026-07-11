"""Controller and execution-provider adapters for subprocess plugin RPC."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from pydantic import BaseModel, TypeAdapter

from villani_ops.execution_environment.models import (
    CommandResult,
    ExecutionEnvironmentConfig,
    PreparedEnvironment,
)

from ..interfaces import (
    AttemptContext,
    AttemptResult,
    EligibleCandidate,
    Materialization,
    MaterializationContext,
    Selection,
    SelectionContext,
    Verification,
)
from .models import PluginKind, PluginManifest
from .transport import SubprocessPluginClient


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
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class _RpcAdapter:
    expected_kind: PluginKind

    def __init__(
        self,
        client: SubprocessPluginClient,
        *,
        configuration: Mapping[str, object] | None = None,
        available_secrets: Mapping[str, str] | None = None,
        cancellation: Event | None = None,
    ) -> None:
        if client.manifest.kind != self.expected_kind:
            raise ValueError(
                f"expected {self.expected_kind.value} manifest, got {client.manifest.kind.value}"
            )
        self.client = client
        self.plugin_manifest: PluginManifest = client.manifest
        self.configuration = dict(configuration or {})
        self.available_secrets = dict(available_secrets or {})
        self.cancellation = cancellation

    def _call(
        self,
        operation: str,
        payload: Mapping[str, object],
        *,
        cancellation: Event | None = None,
    ) -> dict[str, object]:
        return self.client.call(
            operation,
            _json_value(payload),
            configuration=self.configuration,
            available_secrets=self.available_secrets,
            cancellation=cancellation
            if cancellation is not None
            else self.cancellation,
        )


class OutOfProcessAgentRunnerPlugin(_RpcAdapter):
    expected_kind = PluginKind.AGENT_RUNNER

    def run(self, attempt_context: AttemptContext) -> AttemptResult:
        return TypeAdapter(AttemptResult).validate_python(
            self._call(
                "run",
                {"attempt_context": attempt_context},
                cancellation=attempt_context.cancellation_event,
            )
        )


class OutOfProcessVerifierPlugin(_RpcAdapter):
    expected_kind = PluginKind.VERIFIER

    def verify(
        self, attempt_context: AttemptContext, attempt_result: AttemptResult
    ) -> Verification:
        return TypeAdapter(Verification).validate_python(
            self._call(
                "verify",
                {"attempt_context": attempt_context, "attempt_result": attempt_result},
            )
        )


class OutOfProcessSelectorPlugin(_RpcAdapter):
    expected_kind = PluginKind.SELECTOR

    def select(
        self,
        eligible_candidates: tuple[EligibleCandidate, ...],
        context: SelectionContext,
    ) -> Selection:
        return TypeAdapter(Selection).validate_python(
            self._call(
                "select",
                {"eligible_candidates": eligible_candidates, "context": context},
            )
        )


class OutOfProcessMaterializerPlugin(_RpcAdapter):
    expected_kind = PluginKind.MATERIALIZER

    def materialize(
        self, selection: Selection, context: MaterializationContext
    ) -> Materialization:
        return TypeAdapter(Materialization).validate_python(
            self._call("materialize", {"selection": selection, "context": context})
        )


class OutOfProcessExecutionProviderPlugin(_RpcAdapter):
    expected_kind = PluginKind.EXECUTION_PROVIDER

    def __init__(
        self,
        client: SubprocessPluginClient,
        *,
        provider_configuration: ExecutionEnvironmentConfig,
        configuration: Mapping[str, object] | None = None,
        available_secrets: Mapping[str, str] | None = None,
        cancellation: Event | None = None,
    ) -> None:
        super().__init__(
            client,
            configuration=configuration,
            available_secrets=available_secrets,
            cancellation=cancellation,
        )
        self.config = provider_configuration
        self.name = client.manifest.name

    def prepare(self, *, repository: Path, worktree: Path) -> PreparedEnvironment:
        return PreparedEnvironment.model_validate(
            self._call("prepare", {"repository": repository, "worktree": worktree})
        )

    def command_environment(self, prepared: PreparedEnvironment) -> dict[str, str]:
        result = self._call("command_environment", {"prepared": prepared})
        return {str(key): str(value) for key, value in result.items()}

    def execute(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> CommandResult:
        return CommandResult.model_validate(
            self._call("execute", {"prepared": prepared, "command": list(command)})
        )

    def collect(self, prepared: PreparedEnvironment) -> dict[str, Any]:
        return dict(self._call("collect", {"prepared": prepared}))

    def cleanup(self, prepared: PreparedEnvironment) -> None:
        self._call("cleanup", {"prepared": prepared})

    def capability_report(self) -> dict[str, Any]:
        return dict(self._call("capability_report", {}))

    def fingerprint(self, repository: Path) -> str:
        result = self._call("fingerprint", {"repository": repository})
        value = result.get("fingerprint")
        if not isinstance(value, str) or not value:
            raise ValueError("execution provider returned an invalid fingerprint")
        return value

    def wrap_command(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> list[str]:
        result = self._call(
            "wrap_command", {"prepared": prepared, "command": list(command)}
        )
        wrapped = result.get("command")
        if not isinstance(wrapped, list) or not all(
            isinstance(item, str) for item in wrapped
        ):
            raise ValueError("execution provider returned an invalid command")
        return wrapped
