"""Claude Code probe, safe command construction, identity, and failure mapping."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
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
    minimal_cli_environment_values,
)

from .events import ParsedClaudeEvents
from .models import ClaudeFailure, ClaudeProbeResult, ClaudeProviderIdentity


_T = TypeVar("_T")
_VERSION = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
_MINIMUM_VERSION = (2, 1, 138)
_MAXIMUM_VERSION = (2, 2, 0)
_ALLOWED_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
_VERIFIER_ALLOWED_TOOLS = ("Read", "Glob", "Grep")
_CONTROLLED_DISABLED_FEATURES = (
    "hooks",
    "plugins",
    "mcp_servers",
    "auto_memory",
    "user_instructions",
    "project_instructions",
    "slash_commands",
    "browser_integration",
)


class ClaudeCodeDriverUnavailable(RuntimeError):
    pass


def run_coroutine_sync(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """Run async driver work from a synchronous controller construction path."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    result: list[_T] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as error:  # pragma: no cover - defensive bridge
            failure.append(error)

    thread = threading.Thread(target=target, name="villani-claude-async", daemon=True)
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
        "authorization",
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


def _parse_version(output: str) -> tuple[str | None, tuple[int, int, int] | None]:
    match = _VERSION.search(output)
    if match is None:
        return None, None
    parts = tuple(int(match.group(index)) for index in (1, 2, 3))
    return ".".join(str(item) for item in parts), parts  # type: ignore[return-value]


def _authentication_status(
    stdout: str, stderr: str, succeeded: bool
) -> tuple[bool, str]:
    combined = f"{stdout}\n{stderr}".strip()
    lowered = combined.casefold()
    try:
        document = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        document = None
    if isinstance(document, Mapping):
        raw_ready = document.get("loggedIn")
        if raw_ready is None:
            raw_ready = document.get("authenticated")
        if raw_ready is None:
            raw_ready = document.get("ready")
        ready = bool(raw_ready) and succeeded
        method_value = str(
            document.get("authMethod")
            or document.get("auth_method")
            or document.get("method")
            or ""
        ).casefold()
    else:
        negative = any(
            marker in lowered
            for marker in (
                "not logged in",
                "not authenticated",
                "login required",
                "no active login",
            )
        )
        ready = succeeded and not negative and bool(combined)
        method_value = lowered
    if "claude.ai" in method_value or "claude ai" in method_value:
        method = "claude_ai"
    elif "api key" in method_value or "api_key" in method_value:
        method = "api_key"
    elif ready:
        method = "authenticated_unspecified"
    elif combined:
        method = "not_authenticated"
    else:
        method = "unknown"
    return ready, method


class ClaudeCodeCliDriver:
    """Provider-specific behavior beneath the shared process supervisor."""

    def __init__(
        self,
        system: CliAgentSystemConfig,
        *,
        supervisor: CliProcessSupervisor | None = None,
        launcher_arguments: Sequence[str] | None = None,
    ) -> None:
        if system.driver != "claude_code":
            raise ValueError("ClaudeCodeCliDriver requires driver='claude_code'")
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
            raise ClaudeCodeDriverUnavailable(
                f"unsupported environment_policy {mode!r}; expected 'inherit' or 'minimal'"
            )
        additions: dict[str, str] = {}
        if mode == "minimal":
            additions.update(minimal_cli_environment_values())
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

    async def probe_async(self) -> ClaudeProbeResult:  # noqa: C901
        checked_at = _utc_now()
        resolved = self.resolve_executable()
        if resolved is None:
            return ClaudeProbeResult(
                system_id=self.system.id,
                checked_at=checked_at,
                configured_executable=self.system.executable,
                resolved_executable=None,
                exact_version_output=None,
                parsed_version=None,
                authentication_ready=False,
                authentication_method="unknown",
                doctor_ready=False,
                capabilities={},
                resolved_flags={},
                ready=False,
                failures=[ClaudeFailure.NOT_INSTALLED],
                messages=[
                    f"Claude Code executable {self.system.executable!r} was not found; install Claude Code or correct agents.{self.system.id}.executable."
                ],
            )

        failures: list[ClaudeFailure] = []
        messages: list[str] = []

        def fail(code: ClaudeFailure, message: str) -> None:
            if code not in failures:
                failures.append(code)
            messages.append(message)

        with tempfile.TemporaryDirectory(
            prefix="villani-claude-probe-"
        ) as raw_directory:
            directory = Path(raw_directory).resolve()
            version_result, version_stdout, version_stderr = await self._probe_command(
                resolved, ("--version",), directory, "version"
            )
            help_result, help_stdout, help_stderr = await self._probe_command(
                resolved, ("--help",), directory, "help"
            )
            auth_result, auth_stdout, auth_stderr = await self._probe_command(
                resolved, ("auth", "status"), directory, "auth-status"
            )
            doctor_result, _doctor_stdout, _doctor_stderr = await self._probe_command(
                resolved, ("doctor",), directory, "doctor"
            )

        exact_version = (version_stdout or version_stderr).strip() or None
        parsed_version, parsed_tuple = _parse_version(exact_version or "")
        if (
            version_result.infrastructure_state != "succeeded"
            or not exact_version
            or parsed_tuple is None
            or not (_MINIMUM_VERSION <= parsed_tuple < _MAXIMUM_VERSION)
        ):
            fail(
                ClaudeFailure.UNSUPPORTED_VERSION,
                "`claude --version` did not report a supported Claude Code version "
                f"(required >=2.1.138,<2.2.0; observed {exact_version!r}).",
            )

        help_text = f"{help_stdout}\n{help_stderr}"
        lowered_help = help_text.casefold()
        allowed_tools_flag = (
            "--allowedTools"
            if "--allowedTools" in help_text
            else "--allowed-tools"
            if "--allowed-tools" in help_text
            else ""
        )
        capabilities = {
            "print_mode": help_result.infrastructure_state == "succeeded"
            and (
                "--print" in help_text
                or re.search(r"(^|\s)-p([,\s]|$)", help_text) is not None
            ),
            "stream_json": "--output-format" in help_text
            and "stream-json" in lowered_help,
            "structured_output": "--json-schema" in help_text,
            "no_session_persistence": "--no-session-persistence" in help_text,
            "model_selection": "--model" in help_text,
            "permission_mode": "--permission-mode" in help_text
            and "acceptedits" in lowered_help,
            "read_only_permission_mode": "--permission-mode" in help_text
            and "plan" in lowered_help,
            "tools": "--tools" in help_text,
            "allowed_tools": bool(allowed_tools_flag),
            "verbose": "--verbose" in help_text,
            "no_chrome": "--no-chrome" in help_text,
            "bare": "--bare" in help_text,
            "settings": "--settings" in help_text,
            "setting_sources": "--setting-sources" in help_text,
            "strict_mcp_config": "--strict-mcp-config" in help_text,
            "mcp_config": "--mcp-config" in help_text,
            "disable_slash_commands": "--disable-slash-commands" in help_text,
            "max_turns": "--max-turns" in help_text,
            "stdin_prompt": "--print" in help_text
            or re.search(r"(^|\s)-p([,\s]|$)", help_text) is not None,
        }
        resolved_flags = {
            "print": "-p",
            "allowed_tools": allowed_tools_flag,
        }
        required = {
            "print_mode",
            "stream_json",
            "structured_output",
            "no_session_persistence",
            "model_selection",
            "permission_mode",
            "tools",
            "allowed_tools",
            "verbose",
            "no_chrome",
            "stdin_prompt",
        }
        if self.system.instruction_policy == "villani_controlled":
            required.update(
                {
                    "bare",
                    "settings",
                    "setting_sources",
                    "strict_mcp_config",
                    "mcp_config",
                    "disable_slash_commands",
                }
            )
        missing = sorted(name for name in required if not capabilities.get(name, False))
        if missing:
            failure = (
                ClaudeFailure.MISSING_STRUCTURED_OUTPUT_CAPABILITY
                if any(name in {"stream_json", "structured_output"} for name in missing)
                else ClaudeFailure.UNSUPPORTED_REQUIRED_CAPABILITY
            )
            fail(
                failure,
                "Installed Claude Code lacks required safe non-interactive capability/capabilities: "
                + ", ".join(missing),
            )

        authentication_ready, authentication_method = _authentication_status(
            auth_stdout,
            auth_stderr,
            auth_result.infrastructure_state == "succeeded",
        )
        if not authentication_ready:
            fail(
                ClaudeFailure.NOT_AUTHENTICATED,
                "`claude auth status` did not report active authentication; authenticate with Claude Code directly before running Villani.",
            )
        doctor_ready = doctor_result.infrastructure_state == "succeeded"
        if not doctor_ready:
            fail(
                ClaudeFailure.AMBIENT_STARTUP_FAILURE,
                "`claude doctor` reported an unhealthy installation or configuration; resolve its diagnostics before running Villani.",
            )
        if self.system.roles not in tuple({role} for role in AgentRole):
            fail(
                ClaudeFailure.UNSUPPORTED_REQUIRED_CAPABILITY,
                "Claude Code CLI systems must declare exactly one supported role.",
            )
        if self.system.roles == {
            AgentRole.CODING
        } and self.system.permission_profile not in {
            "workspace_write",
            "workspace-write",
        }:
            fail(
                ClaudeFailure.PERMISSION_DENIED,
                "Claude coding requires permission_profile='workspace_write'; broader or read-only profiles are unsupported.",
            )
        if self.system.roles and AgentRole.CODING not in self.system.roles:
            if not capabilities.get("read_only_permission_mode", False):
                fail(
                    ClaudeFailure.UNSUPPORTED_REQUIRED_CAPABILITY,
                    "Claude Code read-only roles require the plan permission mode.",
                )
            if self.system.permission_profile not in {"read_only", "read-only"}:
                fail(
                    ClaudeFailure.PERMISSION_DENIED,
                    "Claude Code read-only roles require permission_profile='read_only'.",
                )
            if self.system.instruction_policy != "villani_controlled":
                fail(
                    ClaudeFailure.UNSUPPORTED_REQUIRED_CAPABILITY,
                    "Claude Code read-only roles require instruction_policy='villani_controlled'.",
                )
            if self.system.environment_policy != "minimal":
                fail(
                    ClaudeFailure.UNSUPPORTED_REQUIRED_CAPABILITY,
                    "Claude Code read-only roles require environment_policy='minimal' so sessions and ambient identity cannot cross role boundaries.",
                )

        return ClaudeProbeResult(
            system_id=self.system.id,
            checked_at=checked_at,
            configured_executable=self.system.executable,
            resolved_executable=str(resolved),
            exact_version_output=exact_version,
            parsed_version=parsed_version,
            authentication_ready=authentication_ready,
            authentication_method=authentication_method,  # type: ignore[arg-type]
            doctor_ready=doctor_ready,
            capabilities=capabilities,
            resolved_flags=resolved_flags,
            ready=not failures,
            failures=failures,
            messages=messages,
        )

    def probe(self) -> ClaudeProbeResult:
        return run_coroutine_sync(self.probe_async())

    @staticmethod
    def integer_option(options: Mapping[str, Any], name: str, default: int) -> int:
        value = options.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ClaudeCodeDriverUnavailable(
                f"provider_options.{name} must be a positive integer"
            )
        return value

    def build_invocation(
        self,
        *,
        probe: ClaudeProbeResult,
        worktree: Path,
        agent_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        run_id: str,
        attempt_id: str,
        baseline_sha256: str | None,
        controlled_settings_path: Path | None = None,
        controlled_mcp_path: Path | None = None,
    ) -> CliInvocation:
        if not probe.ready or probe.resolved_executable is None:
            detail = "; ".join(probe.messages) or "Claude Code doctor did not pass"
            raise ClaudeCodeDriverUnavailable(detail)
        schema = json.loads(Path(output_schema_path).read_text(encoding="utf-8"))
        schema_argument = json.dumps(
            schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        allowed_tools = ",".join(_ALLOWED_TOOLS)
        allowed_flag = probe.resolved_flags.get("allowed_tools")
        if not allowed_flag:
            raise ClaudeCodeDriverUnavailable(
                "Claude Code doctor did not resolve the allowed-tools flag"
            )
        arguments: list[str] = [
            *self.launcher_arguments,
            "-p",
            "--model",
            self.system.model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            allowed_tools,
            allowed_flag,
            allowed_tools,
            "--no-chrome",
            "--json-schema",
            schema_argument,
        ]
        if probe.capabilities.get("max_turns", False):
            arguments.extend(
                [
                    "--max-turns",
                    str(
                        self.integer_option(
                            self.system.provider_options, "max_turns", 20
                        )
                    ),
                ]
            )
        disabled_features: tuple[str, ...] = ()
        if self.system.instruction_policy == "villani_controlled":
            if controlled_settings_path is None or controlled_mcp_path is None:
                raise ClaudeCodeDriverUnavailable(
                    "villani_controlled requires controlled settings and MCP files"
                )
            arguments.extend(
                [
                    "--bare",
                    "--settings",
                    str(Path(controlled_settings_path).resolve()),
                    "--setting-sources=",
                    "--strict-mcp-config",
                    "--mcp-config",
                    str(Path(controlled_mcp_path).resolve()),
                    "--disable-slash-commands",
                ]
            )
            disabled_features = _CONTROLLED_DISABLED_FEATURES

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
            raw_event_path=agent_directory / "claude-events.jsonl",
            invocation_path=agent_directory / "invocation.json",
            process_result_path=agent_directory / "process-result.json",
            output_tail_path=agent_directory / "output-tail.json",
            output_limits=limits,
            role_workspace_identity={
                "role": "coding",
                "run_id": run_id,
                "attempt_id": attempt_id,
                "agent_system_id": self.system.id,
                "driver": "claude_code",
                "configured_model": self.system.model,
                "cli_version": probe.exact_version_output,
                "worktree": str(Path(worktree).resolve()),
                "baseline_sha256": baseline_sha256,
                "instruction_policy": self.system.instruction_policy,
                "permission_policy": self.system.permission_profile,
                "project_user_discovery_permitted": (
                    self.system.instruction_policy == "native_project"
                ),
                "disabled_ambient_features": list(disabled_features),
            },
            target_repository_writable=False,
            prompt_artifact_reference=prompt_reference,
            prompt_sha256=prompt_sha256,
            event_stream_format="jsonl",
            utf8_policy="strict",
            require_final_output=False,
        )

    def _build_read_only_role_invocation(
        self,
        *,
        role: AgentRole,
        probe: ClaudeProbeResult,
        workspace: Path,
        artifact_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        role_invocation_id: str,
        controlled_settings_path: Path,
        controlled_mcp_path: Path,
    ) -> CliInvocation:
        """Build one no-session process for a read-only Villani role."""

        if role == AgentRole.CODING or self.system.roles != {role}:
            raise ClaudeCodeDriverUnavailable(
                f"Claude Code {role.value} invocation requires a {role.value}-only system"
            )
        if not probe.ready or probe.resolved_executable is None:
            detail = "; ".join(probe.messages) or "Claude Code doctor did not pass"
            raise ClaudeCodeDriverUnavailable(detail)
        if self.system.environment_policy != "minimal":
            raise ClaudeCodeDriverUnavailable(
                f"Claude Code {role.value} requires environment_policy='minimal'"
            )
        schema = json.loads(Path(output_schema_path).read_text(encoding="utf-8"))
        schema_argument = json.dumps(
            schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        allowed_tools = (
            ",".join(_VERIFIER_ALLOWED_TOOLS) if role == AgentRole.VERIFICATION else ""
        )
        allowed_flag = probe.resolved_flags.get("allowed_tools")
        if not allowed_flag:
            raise ClaudeCodeDriverUnavailable(
                "Claude Code doctor did not resolve the allowed-tools flag"
            )
        arguments: list[str] = [
            *self.launcher_arguments,
            "-p",
            "--model",
            self.system.model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--permission-mode",
            "plan",
            "--tools",
            allowed_tools,
            allowed_flag,
            allowed_tools,
            "--no-chrome",
            "--json-schema",
            schema_argument,
            "--bare",
            "--settings",
            str(Path(controlled_settings_path).resolve()),
            "--setting-sources=",
            "--strict-mcp-config",
            "--mcp-config",
            str(Path(controlled_mcp_path).resolve()),
            "--disable-slash-commands",
        ]
        if probe.capabilities.get("max_turns", False):
            arguments.extend(
                [
                    "--max-turns",
                    str(
                        self.integer_option(
                            self.system.provider_options, "max_turns", 20
                        )
                    ),
                ]
            )
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
            cwd=Path(workspace).resolve(),
            stdin_bytes=prompt_bytes,
            environment=environment.values,
            environment_redaction_keys=environment.redaction_keys,
            environment_metadata=environment.metadata,
            timeout_seconds=float(self.system.timeout_seconds),
            graceful_shutdown_seconds=float(
                options.get("graceful_shutdown_seconds", 3.0)
            ),
            stdout_path=artifact_directory / "stdout.log",
            stderr_path=artifact_directory / "stderr.log",
            raw_event_path=artifact_directory / "raw-events.jsonl",
            invocation_path=artifact_directory / "invocation.json",
            process_result_path=artifact_directory / "process-result.json",
            output_tail_path=artifact_directory / "output-tail.json",
            output_limits=limits,
            role_workspace_identity={
                "role": role.value,
                "role_invocation_id": role_invocation_id,
                "agent_system_id": self.system.id,
                "driver": "claude_code",
                "configured_model": self.system.model,
                "cli_version": probe.exact_version_output,
                "verification_id": (
                    role_invocation_id if role == AgentRole.VERIFICATION else None
                ),
                "cwd": str(Path(workspace).resolve()),
                "writable_roots": (
                    [
                        str((Path(workspace) / "output").resolve()),
                        str((Path(workspace) / "agent").resolve()),
                    ]
                    if role == AgentRole.VERIFICATION
                    else []
                ),
                "agent_writable_roots": [],
                "target_repository_writable": False,
                "candidate_worktree_writable": False,
                "instruction_policy": "villani_controlled",
                "permission_policy": self.system.permission_profile,
                "project_user_discovery_permitted": False,
                "disabled_ambient_features": list(_CONTROLLED_DISABLED_FEATURES),
            },
            target_repository_writable=False,
            prompt_artifact_reference=prompt_reference,
            prompt_sha256=prompt_sha256,
            event_stream_format="jsonl",
            utf8_policy="strict",
            require_final_output=False,
        )

    def build_verifier_invocation(
        self,
        *,
        probe: ClaudeProbeResult,
        workspace: Path,
        artifact_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        verification_id: str,
        controlled_settings_path: Path,
        controlled_mcp_path: Path,
    ) -> CliInvocation:
        return self._build_read_only_role_invocation(
            role=AgentRole.VERIFICATION,
            probe=probe,
            workspace=workspace,
            artifact_directory=artifact_directory,
            prompt_bytes=prompt_bytes,
            prompt_reference=prompt_reference,
            prompt_sha256=prompt_sha256,
            output_schema_path=output_schema_path,
            role_invocation_id=verification_id,
            controlled_settings_path=controlled_settings_path,
            controlled_mcp_path=controlled_mcp_path,
        )

    def build_classifier_invocation(
        self,
        *,
        probe: ClaudeProbeResult,
        workspace: Path,
        artifact_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        classification_id: str,
        controlled_settings_path: Path,
        controlled_mcp_path: Path,
    ) -> CliInvocation:
        return self._build_read_only_role_invocation(
            role=AgentRole.CLASSIFICATION,
            probe=probe,
            workspace=workspace,
            artifact_directory=artifact_directory,
            prompt_bytes=prompt_bytes,
            prompt_reference=prompt_reference,
            prompt_sha256=prompt_sha256,
            output_schema_path=output_schema_path,
            role_invocation_id=classification_id,
            controlled_settings_path=controlled_settings_path,
            controlled_mcp_path=controlled_mcp_path,
        )

    def build_selector_invocation(
        self,
        *,
        probe: ClaudeProbeResult,
        workspace: Path,
        artifact_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        selection_id: str,
        controlled_settings_path: Path,
        controlled_mcp_path: Path,
    ) -> CliInvocation:
        return self._build_read_only_role_invocation(
            role=AgentRole.SELECTION,
            probe=probe,
            workspace=workspace,
            artifact_directory=artifact_directory,
            prompt_bytes=prompt_bytes,
            prompt_reference=prompt_reference,
            prompt_sha256=prompt_sha256,
            output_schema_path=output_schema_path,
            role_invocation_id=selection_id,
            controlled_settings_path=controlled_settings_path,
            controlled_mcp_path=controlled_mcp_path,
        )

    @staticmethod
    def safe_command(invocation: CliInvocation) -> tuple[str, ...]:
        values = [str(invocation.executable), *invocation.arguments]
        try:
            schema_index = values.index("--json-schema") + 1
        except ValueError:
            schema_index = -1
        if schema_index > 0:
            values[schema_index] = "<inline-coder-result-schema>"
        for index in invocation.argument_redaction_indices:
            values[index + 1] = "[REDACTED]"
        return tuple(values)

    def provider_identity(
        self,
        probe: ClaudeProbeResult,
        parsed: ParsedClaudeEvents | None = None,
    ) -> ClaudeProviderIdentity:
        if (
            not probe.ready
            or not probe.resolved_executable
            or not probe.exact_version_output
            or not probe.parsed_version
        ):
            raise ClaudeCodeDriverUnavailable(
                "provider identity requires a passing Claude Code probe"
            )
        digest = hashlib.sha256()
        with Path(probe.resolved_executable).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        metadata = parsed.system_metadata if parsed is not None else {}

        def tuple_strings(name: str) -> tuple[str, ...]:
            raw = metadata.get(name)
            if not isinstance(raw, list):
                return ()
            return tuple(str(item) for item in raw)

        controlled = self.system.instruction_policy == "villani_controlled"
        return ClaudeProviderIdentity(
            system_id=self.system.id,
            resolved_executable=probe.resolved_executable,
            executable_sha256=f"sha256:{digest.hexdigest()}",
            exact_version_output=probe.exact_version_output,
            parsed_version=probe.parsed_version,
            configured_model=self.system.model,
            reported_model=parsed.reported_model if parsed is not None else None,
            session_id=parsed.session_id if parsed is not None else None,
            authentication_ready=probe.authentication_ready,
            authentication_method=probe.authentication_method,
            capabilities=probe.capabilities,
            resolved_flags=probe.resolved_flags,
            instruction_policy=self.system.instruction_policy,
            permission_profile=self.system.permission_profile,
            allowed_tools=_ALLOWED_TOOLS,
            environment_policy=self.system.environment_policy,
            project_user_discovery_permitted=not controlled,
            disabled_ambient_features=(
                _CONTROLLED_DISABLED_FEATURES if controlled else ()
            ),
            reported_tools=tuple_strings("tools"),
            reported_mcp_servers=tuple_strings("mcp_servers"),
            reported_plugins=tuple_strings("plugins"),
            probed_at=probe.checked_at,
        )

    @staticmethod
    def classify_failure(
        process: CliProcessResult,
        *,
        diagnostic_text: str,
        stream_error: str | None,
        final_output_error: str | None,
        final_result_present: bool,
        path_violation: bool,
        has_patch: bool,
    ) -> ClaudeFailure | None:
        if path_violation:
            return ClaudeFailure.PATH_VIOLATION
        runtime_codes = {item.code for item in process.failures}
        if (
            RuntimeFailure.EXECUTABLE_NOT_FOUND in runtime_codes
            or RuntimeFailure.EXECUTABLE_NOT_RUNNABLE in runtime_codes
        ):
            return ClaudeFailure.NOT_INSTALLED
        if RuntimeFailure.TIMEOUT in runtime_codes:
            return ClaudeFailure.PROCESS_TIMEOUT
        if RuntimeFailure.CANCELLED in runtime_codes:
            return ClaudeFailure.PROCESS_CANCELLATION
        if RuntimeFailure.PROCESS_TREE_CLEANUP_FAILED in runtime_codes:
            return ClaudeFailure.CLEANUP_FAILURE
        if (
            RuntimeFailure.MALFORMED_STREAM in runtime_codes
            or RuntimeFailure.EVENT_LINE_LIMIT_EXCEEDED in runtime_codes
            or RuntimeFailure.OUTPUT_DECODE_FAILED in runtime_codes
            or stream_error is not None
        ):
            return ClaudeFailure.INVALID_JSON
        lowered = diagnostic_text.casefold()
        if any(
            marker in lowered
            for marker in (
                "not authenticated",
                "authentication failed",
                "login required",
                "unauthorized",
                "invalid api key",
            )
        ):
            return ClaudeFailure.PROVIDER_AUTHENTICATION_FAILURE
        if any(
            marker in lowered
            for marker in (
                "rate limit",
                "rate_limit",
                "too many requests",
                "overloaded",
                "temporarily unavailable",
                '"status":429',
                '"status":529',
            )
        ):
            return ClaudeFailure.PROVIDER_RATE_LIMIT_OR_OVERLOAD
        if "model" in lowered and any(
            marker in lowered
            for marker in (
                "unavailable",
                "not found",
                "does not exist",
                "not supported",
            )
        ):
            return ClaudeFailure.MODEL_UNAVAILABLE
        if any(
            marker in lowered
            for marker in (
                "tool denied",
                "tool is not allowed",
                "tool not allowed",
                "disallowed tool",
            )
        ):
            return ClaudeFailure.TOOL_DENIED
        if any(
            marker in lowered
            for marker in (
                "permission denied",
                "permission mode",
                "acceptEdits denied".casefold(),
                "cannot edit",
                "read-only",
            )
        ):
            return ClaudeFailure.PERMISSION_DENIED
        if any(
            marker in lowered
            for marker in ("mcp startup", "plugin startup", "hook startup")
        ):
            return ClaudeFailure.AMBIENT_STARTUP_FAILURE
        if any(
            marker in lowered
            for marker in (
                '"is_error": true',
                '"subtype": "error"',
                '"subtype": "failed"',
            )
        ):
            return ClaudeFailure.PROCESS_CRASH
        if process.exit_code not in {None, 0}:
            return ClaudeFailure.PROCESS_CRASH
        if not final_result_present:
            return ClaudeFailure.MISSING_FINAL_RESULT
        if final_output_error is not None:
            return ClaudeFailure.JSON_SCHEMA_FAILURE
        if process.infrastructure_state != "succeeded":
            return ClaudeFailure.PROCESS_CRASH
        if not has_patch:
            return ClaudeFailure.COMPLETED_NO_PATCH
        return None


__all__ = [
    "ClaudeCodeCliDriver",
    "ClaudeCodeDriverUnavailable",
    "run_coroutine_sync",
]
