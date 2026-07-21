"""Provider-specific Codex probe, command construction, and failure mapping."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import threading
from collections.abc import Coroutine, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from villani_ops.closed_loop.agent_systems.role_models import (
    AgentRole,
    CliAgentSystemConfig,
)
from villani_ops.closed_loop.cli_runtime import (
    CliEnvironmentPolicy,
    CliFailure as RuntimeFailure,
    CliInvocation,
    CliOutputLimits,
    CliProcessResult,
    CliProcessSupervisor,
)

from .models import (
    CodexFailure,
    CodexProbeResult,
    CodexProviderIdentity,
)


_T = TypeVar("_T")


class CodexDriverUnavailable(RuntimeError):
    pass


def run_coroutine_sync(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """Run async CLI work from either a normal or already-async construction path."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    result: list[_T] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as error:  # pragma: no cover - defensive thread bridge
            failure.append(error)

    thread = threading.Thread(target=target, name="villani-codex-async", daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _environment_redaction_keys() -> frozenset[str]:
    markers = (
        "token",
        "secret",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "credential",
    )
    return frozenset(
        name
        for name in os.environ
        if any(marker in name.casefold() for marker in markers)
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


class CodexCliDriver:
    """Codex-specific behavior below the provider-neutral process supervisor."""

    def __init__(
        self,
        system: CliAgentSystemConfig,
        *,
        supervisor: CliProcessSupervisor | None = None,
        launcher_arguments: Sequence[str] | None = None,
    ) -> None:
        if system.driver != "codex":
            raise ValueError("CodexCliDriver requires driver='codex'")
        self.system = system
        self.supervisor = supervisor or CliProcessSupervisor()
        configured_launcher = system.provider_options.get("launcher_arguments", [])
        if launcher_arguments is None:
            if not isinstance(configured_launcher, list) or not all(
                isinstance(item, str) and item for item in configured_launcher
            ):
                raise ValueError(
                    "provider_options.launcher_arguments must be a list of strings"
                )
            launcher_arguments = configured_launcher
        self.launcher_arguments = tuple(str(item) for item in launcher_arguments)

    def resolve_executable(self) -> Path | None:
        configured = Path(self.system.executable).expanduser()
        has_separator = any(
            separator in self.system.executable for separator in ("/", "\\")
        )
        if configured.is_absolute() or has_separator:
            resolved = configured.resolve()
            return resolved if resolved.is_file() else None
        found = shutil.which(self.system.executable)
        return Path(found).resolve() if found else None

    def _environment(self):
        mode = self.system.environment_policy
        if mode not in {"inherit", "minimal"}:
            raise CodexDriverUnavailable(
                f"unsupported environment_policy {mode!r}; expected 'inherit' or 'minimal'"
            )
        additions: dict[str, str] = {}
        if mode == "minimal":
            path_name = "Path" if os.name == "nt" and "Path" in os.environ else "PATH"
            additions[path_name] = os.environ.get(path_name, os.environ.get("PATH", ""))
            if os.name == "nt" and os.environ.get("SystemRoot"):
                additions["SystemRoot"] = os.environ["SystemRoot"]
        return CliEnvironmentPolicy(
            mode=mode,
            additions=additions,
            redaction_keys=_environment_redaction_keys(),
        ).resolve()

    async def _probe_command(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        directory: Path,
        name: str,
    ) -> tuple[CliProcessResult, str, str]:
        artifact = directory / name
        environment = self._environment()
        invocation = CliInvocation(
            executable=executable,
            arguments=(*self.launcher_arguments, *arguments),
            cwd=directory,
            stdin_bytes=None,
            environment=environment.values,
            environment_redaction_keys=environment.redaction_keys,
            environment_metadata=environment.metadata,
            timeout_seconds=8.0,
            graceful_shutdown_seconds=1.0,
            stdout_path=artifact / "stdout.log",
            stderr_path=artifact / "stderr.log",
            output_limits=CliOutputLimits(
                maximum_stdout_bytes=1024 * 1024,
                maximum_stderr_bytes=1024 * 1024,
                maximum_stdout_chunk_bytes=256 * 1024,
                maximum_stderr_chunk_bytes=256 * 1024,
                maximum_event_line_bytes=256 * 1024,
                maximum_tail_bytes=64 * 1024,
            ),
            role_workspace_identity={"role": "probe", "system_id": self.system.id},
            target_repository_writable=False,
        )
        result = await self.supervisor.run(invocation)
        return (
            result,
            _read_text(invocation.stdout_path),
            _read_text(invocation.stderr_path),
        )

    async def probe_async(self) -> CodexProbeResult:
        checked_at = _utc_now()
        resolved = self.resolve_executable()
        if resolved is None:
            return CodexProbeResult(
                system_id=self.system.id,
                checked_at=checked_at,
                configured_executable=self.system.executable,
                resolved_executable=None,
                exact_version_output=None,
                authentication_ready=False,
                authentication_method="unknown",
                capabilities={},
                ready=False,
                failures=[CodexFailure.NOT_INSTALLED],
                messages=[
                    f"Codex executable {self.system.executable!r} was not found; install Codex CLI or correct agents.{self.system.id}.executable."
                ],
            )

        failures: list[CodexFailure] = []
        messages: list[str] = []

        def fail(code: CodexFailure, message: str) -> None:
            if code not in failures:
                failures.append(code)
            messages.append(message)

        with tempfile.TemporaryDirectory(
            prefix="villani-codex-probe-"
        ) as raw_directory:
            directory = Path(raw_directory).resolve()
            version_result, version_stdout, version_stderr = await self._probe_command(
                resolved, ("--version",), directory, "version"
            )
            help_result, help_stdout, help_stderr = await self._probe_command(
                resolved, ("exec", "--help"), directory, "exec-help"
            )
            login_result, login_stdout, login_stderr = await self._probe_command(
                resolved, ("login", "status"), directory, "login-status"
            )

        exact_version = (version_stdout or version_stderr).strip() or None
        if version_result.infrastructure_state != "succeeded" or not exact_version:
            fail(
                CodexFailure.UNSUPPORTED_VERSION,
                "`codex --version` did not complete successfully with exact version output.",
            )
        help_text = f"{help_stdout}\n{help_stderr}".casefold()
        capabilities = {
            "exec": help_result.infrastructure_state == "succeeded",
            "jsonl_output": "--json" in help_text,
            "model_selection": "--model" in help_text or "-m," in help_text,
            "workspace_selection": "--cd" in help_text or "-c," in help_text,
            "sandbox_selection": "--sandbox" in help_text
            and "workspace-write" in help_text,
            "schema_output": "--output-schema" in help_text,
            "last_message_output": "--output-last-message" in help_text,
            "ephemeral": "--ephemeral" in help_text,
            "noninteractive_approval": "--ask-for-approval" in help_text
            and "never" in help_text,
            "ignore_user_config": "--ignore-user-config" in help_text,
            "ignore_project_rules": "--ignore-rules" in help_text,
        }
        required = {
            "exec",
            "jsonl_output",
            "model_selection",
            "workspace_selection",
            "sandbox_selection",
            "schema_output",
            "last_message_output",
            "ephemeral",
            "noninteractive_approval",
        }
        if self.system.instruction_policy == "villani_controlled":
            required.update({"ignore_user_config", "ignore_project_rules"})
        missing = sorted(name for name in required if not capabilities.get(name, False))
        if missing:
            fail(
                CodexFailure.UNSUPPORTED_REQUIRED_FLAG,
                "Installed Codex CLI lacks required safe non-interactive capability/capabilities: "
                + ", ".join(missing),
            )

        login_text = f"{login_stdout}\n{login_stderr}".casefold()
        negative_auth = any(
            marker in login_text
            for marker in (
                "not logged in",
                "not authenticated",
                "login required",
                "no active login",
            )
        )
        authentication_ready = (
            login_result.infrastructure_state == "succeeded" and not negative_auth
        )
        if "chatgpt" in login_text:
            authentication_method = "chatgpt"
        elif "api key" in login_text or "api_key" in login_text:
            authentication_method = "api_key"
        elif authentication_ready:
            authentication_method = "authenticated_unspecified"
        elif negative_auth or login_result.exit_code not in {None, 0}:
            authentication_method = "not_authenticated"
        else:
            authentication_method = "unknown"
        if not authentication_ready:
            fail(
                CodexFailure.NOT_AUTHENTICATED,
                "`codex login status` did not report an active login; authenticate with Codex CLI before running Villani.",
            )

        if self.system.roles != {AgentRole.CODING}:
            fail(
                CodexFailure.UNSUPPORTED_REQUIRED_FLAG,
                "Milestone 3 supports Codex CLI only when the system declares the coding role and no non-coding roles.",
            )
        if self.system.permission_profile not in {"workspace_write", "workspace-write"}:
            fail(
                CodexFailure.PERMISSION_SANDBOX_FAILURE,
                "Codex coding requires permission_profile='workspace_write'; broader or read-only profiles are unsupported.",
            )

        return CodexProbeResult(
            system_id=self.system.id,
            checked_at=checked_at,
            configured_executable=self.system.executable,
            resolved_executable=str(resolved),
            exact_version_output=exact_version,
            authentication_ready=authentication_ready,
            authentication_method=authentication_method,  # type: ignore[arg-type]
            capabilities=capabilities,
            ready=not failures,
            failures=failures,
            messages=messages,
        )

    def probe(self) -> CodexProbeResult:
        return run_coroutine_sync(self.probe_async())

    @staticmethod
    def integer_option(options: Mapping[str, Any], name: str, default: int) -> int:
        value = options.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise CodexDriverUnavailable(
                f"provider_options.{name} must be a positive integer"
            )
        return value

    def build_invocation(
        self,
        *,
        probe: CodexProbeResult,
        worktree: Path,
        agent_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        final_output_path: Path,
        run_id: str,
        attempt_id: str,
        baseline_sha256: str | None,
    ) -> CliInvocation:
        if not probe.ready or probe.resolved_executable is None:
            detail = "; ".join(probe.messages) or "Codex doctor did not pass"
            raise CodexDriverUnavailable(detail)
        arguments: list[str] = [
            *self.launcher_arguments,
            "exec",
            "--ephemeral",
            "--json",
            "--model",
            self.system.model,
            "--sandbox",
            "workspace-write",
            "--cd",
            str(Path(worktree).resolve()),
            "--output-schema",
            str(Path(output_schema_path).resolve()),
            "--output-last-message",
            str(Path(final_output_path).resolve()),
            "--ask-for-approval",
            "never",
        ]
        if self.system.instruction_policy == "villani_controlled":
            arguments.extend(("--ignore-user-config", "--ignore-rules"))
        arguments.append("-")
        environment = self._environment()
        options = self.system.provider_options
        limits = CliOutputLimits(
            maximum_stdout_bytes=self.integer_option(
                options, "maximum_stdout_bytes", 16 * 1024 * 1024
            ),
            maximum_stderr_bytes=self.integer_option(
                options, "maximum_stderr_bytes", 16 * 1024 * 1024
            ),
            maximum_stdout_chunk_bytes=self.integer_option(
                options, "maximum_stdout_chunk_bytes", 1024 * 1024
            ),
            maximum_stderr_chunk_bytes=self.integer_option(
                options, "maximum_stderr_chunk_bytes", 1024 * 1024
            ),
            maximum_event_line_bytes=self.integer_option(
                options, "maximum_event_line_bytes", 1024 * 1024
            ),
            maximum_tail_bytes=self.integer_option(
                options, "maximum_tail_bytes", 16 * 1024
            ),
        )
        return CliInvocation(
            executable=Path(probe.resolved_executable),
            arguments=tuple(arguments),
            cwd=Path(worktree).resolve(),
            stdin_bytes=prompt_bytes,
            environment=environment.values,
            environment_redaction_keys=environment.redaction_keys,
            environment_metadata=environment.metadata,
            timeout_seconds=float(self.system.timeout_seconds),
            graceful_shutdown_seconds=float(
                options.get("graceful_shutdown_seconds", 3.0)
            ),
            stdout_path=agent_directory / "stdout.log",
            stderr_path=agent_directory / "stderr.log",
            raw_event_path=agent_directory / "codex-events.jsonl",
            invocation_path=agent_directory / "invocation.json",
            process_result_path=agent_directory / "process-result.json",
            output_tail_path=agent_directory / "output-tail.json",
            output_limits=limits,
            role_workspace_identity={
                "role": "coding",
                "run_id": run_id,
                "attempt_id": attempt_id,
                "agent_system_id": self.system.id,
                "worktree": str(Path(worktree).resolve()),
                "baseline_sha256": baseline_sha256,
                "instruction_policy": self.system.instruction_policy,
            },
            target_repository_writable=False,
            prompt_artifact_reference=prompt_reference,
            prompt_sha256=prompt_sha256,
            event_stream_format="jsonl",
            utf8_policy="strict",
            final_output_path=final_output_path,
            require_final_output=True,
        )

    @staticmethod
    def safe_command(invocation: CliInvocation) -> tuple[str, ...]:
        values = [str(invocation.executable), *invocation.arguments]
        for index in invocation.argument_redaction_indices:
            values[index + 1] = "[REDACTED]"
        return tuple(values)

    def provider_identity(self, probe: CodexProbeResult) -> CodexProviderIdentity:
        if (
            not probe.ready
            or not probe.resolved_executable
            or not probe.exact_version_output
        ):
            raise CodexDriverUnavailable(
                "provider identity requires a passing Codex probe"
            )
        digest = hashlib.sha256()
        with Path(probe.resolved_executable).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return CodexProviderIdentity(
            system_id=self.system.id,
            resolved_executable=probe.resolved_executable,
            executable_sha256=f"sha256:{digest.hexdigest()}",
            exact_version_output=probe.exact_version_output,
            model=self.system.model,
            authentication_ready=probe.authentication_ready,
            authentication_method=probe.authentication_method,
            capabilities=probe.capabilities,
            instruction_policy=self.system.instruction_policy,
            permission_profile=self.system.permission_profile,
            environment_policy=self.system.environment_policy,
            probed_at=probe.checked_at,
        )

    @staticmethod
    def classify_failure(
        process: CliProcessResult,
        *,
        stderr_tail: str,
        final_output_error: str | None,
        path_violation: bool,
        has_patch: bool,
    ) -> CodexFailure | None:
        if path_violation:
            return CodexFailure.PATH_VIOLATION
        runtime_codes = {item.code for item in process.failures}
        if (
            RuntimeFailure.EXECUTABLE_NOT_FOUND in runtime_codes
            or RuntimeFailure.EXECUTABLE_NOT_RUNNABLE in runtime_codes
        ):
            return CodexFailure.NOT_INSTALLED
        if RuntimeFailure.TIMEOUT in runtime_codes:
            return CodexFailure.PROCESS_TIMEOUT
        if RuntimeFailure.CANCELLED in runtime_codes:
            return CodexFailure.PROCESS_CANCELLATION
        if RuntimeFailure.PROCESS_TREE_CLEANUP_FAILED in runtime_codes:
            return CodexFailure.CLEANUP_FAILURE
        if (
            RuntimeFailure.MALFORMED_STREAM in runtime_codes
            or RuntimeFailure.EVENT_LINE_LIMIT_EXCEEDED in runtime_codes
            or RuntimeFailure.OUTPUT_DECODE_FAILED in runtime_codes
        ):
            return CodexFailure.MALFORMED_JSONL
        lowered = stderr_tail.casefold()
        if any(
            marker in lowered
            for marker in (
                "not authenticated",
                "authentication failed",
                "login required",
                "unauthorized",
            )
        ):
            return CodexFailure.PROVIDER_AUTHENTICATION_FAILURE
        if any(
            marker in lowered
            for marker in (
                "rate limit",
                "too many requests",
                "overloaded",
                "temporarily unavailable",
            )
        ):
            return CodexFailure.PROVIDER_RATE_LIMIT_OR_OVERLOAD
        if "model" in lowered and any(
            marker in lowered
            for marker in (
                "unavailable",
                "not found",
                "does not exist",
                "not supported",
            )
        ):
            return CodexFailure.MODEL_UNAVAILABLE
        if any(
            marker in lowered
            for marker in (
                "sandbox",
                "permission denied",
                "read-only",
                "workspace-write",
            )
        ):
            return CodexFailure.PERMISSION_SANDBOX_FAILURE
        if process.exit_code not in {None, 0}:
            return CodexFailure.PROCESS_CRASH
        if RuntimeFailure.FINAL_OUTPUT_MISSING in runtime_codes:
            return CodexFailure.MISSING_FINAL_STRUCTURED_OUTPUT
        if final_output_error is not None:
            return CodexFailure.STRUCTURED_OUTPUT_SCHEMA_FAILURE
        if process.infrastructure_state != "succeeded":
            return CodexFailure.PROCESS_CRASH
        if not has_patch:
            return CodexFailure.COMPLETED_NO_PATCH
        return None


__all__ = [
    "CodexCliDriver",
    "CodexDriverUnavailable",
    "run_coroutine_sync",
]
