"""Provider-specific Codex probe, command construction, and failure mapping."""

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
from villani_ops.subprocess_utils import resolve_command_prefix

from .models import (
    CodexFailure,
    CodexProbeResult,
    CodexProviderIdentity,
)


_T = TypeVar("_T")

_SCOPED_PERMISSION_PROFILE_MINIMUM_VERSION = (0, 138, 0)
_VERIFIER_PERMISSION_PROFILE = "villani_verifier_read_only"
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_PROBE_EVIDENCE_TEXT_LIMIT = 2048


def _semantic_version(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _normalized_help(*values: str) -> str:
    """Normalize platform line endings and terminal colour before parsing."""

    combined = "\n".join(values)
    return _ANSI_ESCAPE.sub("", combined).replace("\r\n", "\n").replace("\r", "\n")


def _probe_succeeded(result: CliProcessResult) -> bool:
    return result.infrastructure_state == "succeeded" and result.exit_code == 0


def _bounded_probe_text(value: str) -> str:
    return value[:_PROBE_EVIDENCE_TEXT_LIMIT]


def _approval_probe_evidence(
    *,
    arguments: tuple[str, ...],
    result: CliProcessResult,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    """Return bounded, already-supervisor-redacted no-model probe evidence."""

    return {
        "argv": list(arguments),
        "exit_code": result.exit_code,
        "infrastructure_state": result.infrastructure_state,
        "timed_out": any(
            failure.code == RuntimeFailure.TIMEOUT for failure in result.failures
        ),
        "stdout_captured": bool(stdout),
        "stderr_captured": bool(stderr),
        "stdout_sha256": f"sha256:{hashlib.sha256(stdout.encode('utf-8')).hexdigest()}",
        "stderr_sha256": f"sha256:{hashlib.sha256(stderr.encode('utf-8')).hexdigest()}",
        "stdout_excerpt": _bounded_probe_text(stdout),
        "stderr_excerpt": _bounded_probe_text(stderr),
        "used_model": False,
    }


def _verifier_permission_overrides() -> tuple[str, ...]:
    """Return a closed read boundary for model-generated verifier commands.

    Legacy ``--sandbox read-only`` blocks writes but permits broad filesystem
    inspection. Codex permission profiles (0.138+) let Villani grant only the
    runtime minimum plus the verification workspace. Passing ``--sandbox``
    would make Codex ignore this profile, so the custom profile is the
    read-only sandbox policy for verifier invocations.
    """

    return (
        f'default_permissions="{_VERIFIER_PERMISSION_PROFILE}"',
        (
            f"permissions.{_VERIFIER_PERMISSION_PROFILE}.filesystem="
            '{":minimal"="read",":workspace_roots"={"."="read"}}'
        ),
        f"permissions.{_VERIFIER_PERMISSION_PROFILE}.network.enabled=false",
        "allow_login_shell=false",
    )


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
            additions.update(minimal_cli_environment_values())
        return CliEnvironmentPolicy(
            mode=mode,
            additions=additions,
            redaction_keys=_environment_redaction_keys(),
        ).resolve()

    def _probe_timeout_seconds(self) -> float:
        value = self.system.provider_options.get("probe_timeout_seconds", 8.0)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value <= 0
            or value > 30
        ):
            raise CodexDriverUnavailable(
                "provider_options.probe_timeout_seconds must be greater than "
                "zero and no more than 30 seconds"
            )
        return float(value)

    async def _probe_command(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        directory: Path,
        name: str,
    ) -> tuple[CliProcessResult, str, str]:
        artifact = directory / name
        environment = self._environment()
        launch_executable, launch_arguments = self._normalized_launcher(
            executable,
            (*self.launcher_arguments, *arguments),
        )
        invocation = CliInvocation(
            executable=launch_executable,
            arguments=launch_arguments,
            cwd=directory,
            stdin_bytes=None,
            environment=environment.values,
            environment_redaction_keys=environment.redaction_keys,
            environment_metadata=environment.metadata,
            timeout_seconds=self._probe_timeout_seconds(),
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

    @staticmethod
    def _normalized_launcher(
        executable: Path, arguments: Sequence[str]
    ) -> tuple[Path, tuple[str, ...]]:
        """Use Villani's argv-only launcher support for Windows batch shims."""

        prefix = resolve_command_prefix(str(executable))
        if not prefix:
            raise CodexDriverUnavailable(
                f"Codex executable {str(executable)!r} is not runnable"
            )
        return Path(prefix[0]), (*prefix[1:], *arguments)

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
            config_approval_arguments = (
                "-c",
                'approval_policy="never"',
                "--strict-config",
                "exec",
                "--help",
            )
            global_approval_arguments = (
                "--ask-for-approval",
                "never",
                "exec",
                "--help",
            )
            (
                (version_result, version_stdout, version_stderr),
                (global_help_result, global_help_stdout, global_help_stderr),
                (exec_help_result, exec_help_stdout, exec_help_stderr),
                (login_result, login_stdout, login_stderr),
                (
                    config_approval_result,
                    config_approval_stdout,
                    config_approval_stderr,
                ),
                (
                    global_approval_result,
                    global_approval_stdout,
                    global_approval_stderr,
                ),
            ) = await asyncio.gather(
                self._probe_command(resolved, ("--version",), directory, "version"),
                self._probe_command(resolved, ("--help",), directory, "global-help"),
                self._probe_command(
                    resolved, ("exec", "--help"), directory, "exec-help"
                ),
                self._probe_command(
                    resolved, ("login", "status"), directory, "login-status"
                ),
                self._probe_command(
                    resolved,
                    config_approval_arguments,
                    directory,
                    "approval-config-override",
                ),
                self._probe_command(
                    resolved,
                    global_approval_arguments,
                    directory,
                    "approval-global-flag",
                ),
            )

        exact_version = (version_stdout or version_stderr).strip() or None
        parsed_version = _semantic_version(exact_version)
        if not _probe_succeeded(version_result) or not exact_version:
            fail(
                CodexFailure.UNSUPPORTED_VERSION,
                "`codex --version` did not complete successfully with exact version output.",
            )
        global_help_text = _normalized_help(
            global_help_stdout, global_help_stderr
        ).casefold()
        exec_help_text = _normalized_help(exec_help_stdout, exec_help_stderr).casefold()
        config_approval_validated = _probe_succeeded(config_approval_result)
        global_approval_advertised = (
            _probe_succeeded(global_help_result)
            and "--ask-for-approval" in global_help_text
            and "never" in global_help_text
        )
        global_approval_validated = global_approval_advertised and _probe_succeeded(
            global_approval_result
        )
        approval_strategy = (
            "config_override"
            if config_approval_validated
            else "global_flag"
            if global_approval_validated
            else None
        )
        unattended_safe_execution = approval_strategy is not None
        config_override_advertised = (
            "--config" in global_help_text or "--config" in exec_help_text
        )
        strict_config_advertised = (
            "--strict-config" in global_help_text or "--strict-config" in exec_help_text
        )
        capabilities = {
            "exec": _probe_succeeded(exec_help_result),
            "jsonl_output": "--json" in exec_help_text,
            "model_selection": "--model" in exec_help_text,
            "workspace_selection": "--cd" in exec_help_text,
            "sandbox_selection": "--sandbox" in exec_help_text
            and "workspace-write" in exec_help_text,
            "read_only_sandbox": "--sandbox" in exec_help_text
            and "read-only" in exec_help_text,
            "schema_output": "--output-schema" in exec_help_text,
            "last_message_output": "--output-last-message" in exec_help_text,
            "ephemeral": "--ephemeral" in exec_help_text,
            "unattended_safe_execution": unattended_safe_execution,
            # Kept for old run-bundle and UI readers. Fresh eligibility uses
            # unattended_safe_execution plus the selected validated strategy.
            "noninteractive_approval": unattended_safe_execution,
            "approval_policy_never_config_override": config_approval_validated,
            "global_approval_flag_advertised": global_approval_advertised,
            "global_approval_flag_validated": global_approval_validated,
            "ignore_user_config": "--ignore-user-config" in exec_help_text,
            "ignore_project_rules": "--ignore-rules" in exec_help_text,
            "skip_git_repo_check": "--skip-git-repo-check" in exec_help_text,
            "strict_config": (strict_config_advertised or config_approval_validated),
            "config_override": (
                config_override_advertised or config_approval_validated
            ),
            "scoped_permission_profiles": (
                parsed_version is not None
                and parsed_version >= _SCOPED_PERMISSION_PROFILE_MINIMUM_VERSION
                and (config_override_advertised or config_approval_validated)
                and (strict_config_advertised or config_approval_validated)
            ),
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
            "unattended_safe_execution",
        }
        if any(role != AgentRole.CODING for role in self.system.roles):
            required.update({"read_only_sandbox", "skip_git_repo_check"})
        if any(
            self.system.policy_for_role(role).instruction_policy == "villani_controlled"
            for role in self.system.roles
        ):
            required.update({"ignore_user_config", "ignore_project_rules"})
        configured_effort = self.system.provider_options.get("reasoning_effort")
        if configured_effort is not None and (
            not isinstance(configured_effort, str) or not configured_effort.strip()
        ):
            fail(
                CodexFailure.UNSUPPORTED_REQUIRED_FLAG,
                "provider_options.reasoning_effort must be a non-empty string.",
            )
        if configured_effort is not None and not (
            capabilities["config_override"] and capabilities["strict_config"]
        ):
            fail(
                CodexFailure.UNSUPPORTED_REQUIRED_FLAG,
                "Configured Codex reasoning effort requires validated --config and --strict-config support.",
            )
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
        authentication_ready = _probe_succeeded(login_result) and not negative_auth
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

        for role in sorted(self.system.roles, key=lambda item: item.value):
            policy = self.system.policy_for_role(role)
            if role == AgentRole.CODING:
                if policy.permission_profile not in {
                    "workspace_write",
                    "workspace-write",
                }:
                    fail(
                        CodexFailure.PERMISSION_SANDBOX_FAILURE,
                        "Codex coding requires permission_profile='workspace_write'; broader or read-only profiles are unsupported.",
                    )
                continue
            if not capabilities.get("scoped_permission_profiles", False):
                fail(
                    CodexFailure.UNSUPPORTED_VERSION,
                    f"Codex {role.value} requires scoped permission profiles (Codex 0.138.0 or later with --config and --strict-config) so reads are confined to the role workspace.",
                )
            if policy.permission_profile not in {"read_only", "read-only"}:
                fail(
                    CodexFailure.PERMISSION_SANDBOX_FAILURE,
                    f"Codex {role.value} requires permission_profile='read_only'.",
                )
            if policy.instruction_policy != "villani_controlled":
                fail(
                    CodexFailure.UNSUPPORTED_REQUIRED_FLAG,
                    f"Codex {role.value} requires instruction_policy='villani_controlled'.",
                )
            if policy.environment_policy != "minimal":
                fail(
                    CodexFailure.UNSUPPORTED_REQUIRED_FLAG,
                    f"Codex {role.value} requires environment_policy='minimal' so sessions and ambient identity cannot cross role boundaries.",
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
            approval_strategy=approval_strategy,  # type: ignore[arg-type]
            approval_policy="never" if approval_strategy is not None else None,
            approval_probe_used_model=False,
            approval_probe_evidence={
                "selected_strategy": approval_strategy,
                "approval_policy": ("never" if approval_strategy is not None else None),
                "probe_used_model": False,
                "config_override": _approval_probe_evidence(
                    arguments=config_approval_arguments,
                    result=config_approval_result,
                    stdout=config_approval_stdout,
                    stderr=config_approval_stderr,
                ),
                "global_flag": _approval_probe_evidence(
                    arguments=global_approval_arguments,
                    result=global_approval_result,
                    stdout=global_approval_stdout,
                    stderr=global_approval_stderr,
                ),
            },
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

    def _driver_for_role(self, role: AgentRole) -> "CodexCliDriver":
        return CodexCliDriver(
            self.system.for_role(role),
            supervisor=self.supervisor,
            launcher_arguments=self.launcher_arguments,
        )

    def _reasoning_effort(self) -> str | None:
        value = self.system.provider_options.get("reasoning_effort")
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise CodexDriverUnavailable(
                "provider_options.reasoning_effort must be a non-empty string"
            )
        return value.strip()

    @staticmethod
    def _approval_arguments(probe: CodexProbeResult) -> list[str]:
        if not probe.capabilities.get("unattended_safe_execution", False):
            raise CodexDriverUnavailable(
                "Codex doctor did not validate unattended safe execution"
            )
        if probe.approval_policy != "never":
            raise CodexDriverUnavailable(
                "Codex doctor did not validate approval policy 'never'"
            )
        if probe.approval_strategy == "config_override":
            return ["-c", 'approval_policy="never"']
        if probe.approval_strategy == "global_flag":
            return ["--ask-for-approval", "never"]
        raise CodexDriverUnavailable(
            "Codex doctor did not select a supported approval strategy"
        )

    def _global_arguments(
        self,
        *,
        probe: CodexProbeResult,
        include_scoped_read_only_profile: bool,
    ) -> list[str]:
        """Build options that must precede the ``exec`` subcommand."""

        arguments = [*self.launcher_arguments, *self._approval_arguments(probe)]
        config_supported = bool(
            probe.capabilities.get("config_override", False)
            and probe.capabilities.get("strict_config", False)
        )
        effort = self._reasoning_effort()
        config_overrides: list[str] = []
        if config_supported:
            if effort is not None:
                config_overrides.append(
                    f"model_reasoning_effort={json.dumps(effort, ensure_ascii=False)}"
                )
            config_overrides.append('web_search="disabled"')
            if include_scoped_read_only_profile:
                config_overrides.extend(_verifier_permission_overrides())
        elif effort is not None:
            raise CodexDriverUnavailable(
                "configured reasoning effort requires validated Codex config overrides"
            )
        elif include_scoped_read_only_profile:
            raise CodexDriverUnavailable(
                "Codex read-only roles require validated strict config overrides"
            )
        for override in config_overrides:
            arguments.extend(("-c", override))
        if config_overrides or probe.approval_strategy == "config_override":
            if not probe.capabilities.get("strict_config", False):
                raise CodexDriverUnavailable(
                    "Codex config overrides require validated --strict-config support"
                )
            arguments.append("--strict-config")
        return arguments

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
        if AgentRole.CODING not in self.system.roles:
            raise CodexDriverUnavailable(
                "Codex coding invocation requires a system advertising coding"
            )
        if len(self.system.roles) > 1:
            return self._driver_for_role(AgentRole.CODING).build_invocation(
                probe=probe,
                worktree=worktree,
                agent_directory=agent_directory,
                prompt_bytes=prompt_bytes,
                prompt_reference=prompt_reference,
                prompt_sha256=prompt_sha256,
                output_schema_path=output_schema_path,
                final_output_path=final_output_path,
                run_id=run_id,
                attempt_id=attempt_id,
                baseline_sha256=baseline_sha256,
            )
        if not probe.ready or probe.resolved_executable is None:
            detail = "; ".join(probe.messages) or "Codex doctor did not pass"
            raise CodexDriverUnavailable(detail)
        arguments: list[str] = [
            *self._global_arguments(
                probe=probe, include_scoped_read_only_profile=False
            ),
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
        launch_executable, launch_arguments = self._normalized_launcher(
            Path(probe.resolved_executable), arguments
        )
        return CliInvocation(
            executable=launch_executable,
            arguments=launch_arguments,
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
                "driver": "codex",
                "configured_model": self.system.model,
                "cli_version": probe.exact_version_output,
                "worktree": str(Path(worktree).resolve()),
                "baseline_sha256": baseline_sha256,
                "instruction_policy": self.system.instruction_policy,
                "permission_policy": self.system.permission_profile,
                "approval_strategy": probe.approval_strategy,
                "approval_policy": probe.approval_policy,
                "sandbox": "workspace-write",
                "network_access": False,
                "session_resume": False,
            },
            target_repository_writable=False,
            prompt_artifact_reference=prompt_reference,
            prompt_sha256=prompt_sha256,
            event_stream_format="jsonl",
            utf8_policy="strict",
            final_output_path=final_output_path,
            require_final_output=True,
        )

    def _build_read_only_role_invocation(
        self,
        *,
        role: AgentRole,
        probe: CodexProbeResult,
        workspace: Path,
        artifact_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        final_output_path: Path,
        role_invocation_id: str,
    ) -> CliInvocation:
        """Build one ephemeral process with reads confined to its role workspace."""

        if role == AgentRole.CODING or role not in self.system.roles:
            raise CodexDriverUnavailable(
                f"Codex {role.value} invocation requires a system advertising "
                f"{role.value}"
            )
        if len(self.system.roles) > 1:
            return self._driver_for_role(role)._build_read_only_role_invocation(
                role=role,
                probe=probe,
                workspace=workspace,
                artifact_directory=artifact_directory,
                prompt_bytes=prompt_bytes,
                prompt_reference=prompt_reference,
                prompt_sha256=prompt_sha256,
                output_schema_path=output_schema_path,
                final_output_path=final_output_path,
                role_invocation_id=role_invocation_id,
            )
        if not probe.ready or probe.resolved_executable is None:
            detail = "; ".join(probe.messages) or "Codex doctor did not pass"
            raise CodexDriverUnavailable(detail)
        if self.system.environment_policy != "minimal":
            raise CodexDriverUnavailable(
                f"Codex {role.value} requires environment_policy='minimal'"
            )
        arguments: list[str] = [
            *self._global_arguments(probe=probe, include_scoped_read_only_profile=True),
            "exec",
            "--ephemeral",
            "--json",
            "--model",
            self.system.model,
            "--skip-git-repo-check",
            "--cd",
            str(Path(workspace).resolve()),
            "--output-schema",
            str(Path(output_schema_path).resolve()),
            "--output-last-message",
            str(Path(final_output_path).resolve()),
            "--ignore-user-config",
            "--ignore-rules",
        ]
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
        launch_executable, launch_arguments = self._normalized_launcher(
            Path(probe.resolved_executable), arguments
        )
        return CliInvocation(
            executable=launch_executable,
            arguments=launch_arguments,
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
                "driver": "codex",
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
                "controller_owned_output_roots": [
                    str((Path(workspace) / "output").resolve()),
                    str((Path(workspace) / "agent").resolve()),
                ],
                "target_repository_writable": False,
                "candidate_worktree_writable": False,
                "instruction_policy": "villani_controlled",
                "filesystem_read_roots": [
                    ":minimal",
                    str(Path(workspace).resolve()),
                ],
                "permission_policy": _VERIFIER_PERMISSION_PROFILE,
                "approval_strategy": probe.approval_strategy,
                "approval_policy": probe.approval_policy,
                "sandbox": "read-only",
                "sandbox_enforcement": "scoped_permission_profile",
                "network_access": False,
                "session_resume": False,
            },
            target_repository_writable=False,
            prompt_artifact_reference=prompt_reference,
            prompt_sha256=prompt_sha256,
            event_stream_format="jsonl",
            utf8_policy="strict",
            final_output_path=final_output_path,
            require_final_output=True,
        )

    def build_verifier_invocation(
        self,
        *,
        probe: CodexProbeResult,
        workspace: Path,
        artifact_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        final_output_path: Path,
        verification_id: str,
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
            final_output_path=final_output_path,
            role_invocation_id=verification_id,
        )

    def build_classifier_invocation(
        self,
        *,
        probe: CodexProbeResult,
        workspace: Path,
        artifact_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        final_output_path: Path,
        classification_id: str,
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
            final_output_path=final_output_path,
            role_invocation_id=classification_id,
        )

    def build_selector_invocation(
        self,
        *,
        probe: CodexProbeResult,
        workspace: Path,
        artifact_directory: Path,
        prompt_bytes: bytes,
        prompt_reference: str,
        prompt_sha256: str,
        output_schema_path: Path,
        final_output_path: Path,
        selection_id: str,
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
            final_output_path=final_output_path,
            role_invocation_id=selection_id,
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
            approval_strategy=probe.approval_strategy,
            approval_policy=probe.approval_policy or "never",
            approval_probe_used_model=probe.approval_probe_used_model,
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
