"""Provider-specific process mechanics behind provider-neutral read-only roles."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from ..agent_systems.role_models import AgentRole
from ..claude_code_cli.driver import ClaudeCodeCliDriver
from ..claude_code_cli.events import (
    ClaudeEventParseError,
    parse_claude_events,
    sanitize_claude_event_artifacts,
)
from ..claude_code_cli.models import ClaudeProbeResult
from ..cli_runtime import (
    CliCancellationHandle,
    CliCancellationOrigin,
    CliFailure as RuntimeFailure,
    CliProcessResult,
)
from ..codex_cli.driver import CodexCliDriver, run_coroutine_sync
from ..codex_cli.events import CodexEventParseError, parse_codex_events
from ..codex_cli.models import CodexProbeResult
from ..durable_io import write_json_atomic
from .models import CliRoleFailure
from .workspace import (
    CliRoleWorkspaceError,
    PreparedCliRoleWorkspace,
    repository_state_digest,
    verify_cli_role_manifest,
)


CliRoleDriver: TypeAlias = CodexCliDriver | ClaudeCodeCliDriver
CliRoleProbe: TypeAlias = CodexProbeResult | ClaudeProbeResult


@dataclass(frozen=True, slots=True)
class CliRoleExecution:
    raw_text: str
    failure: CliRoleFailure | None
    reason: str
    process_spawned: bool
    process_id: int | None
    session_id: str | None
    input_integrity_proved: bool
    target_unchanged: bool
    candidates_unchanged: bool


def _probe_failure(probe: CliRoleProbe) -> CliRoleFailure:
    values = {str(item.value) for item in probe.failures}
    if values.intersection({"codex_not_installed", "claude_not_installed"}):
        return CliRoleFailure.EXECUTABLE_MISSING
    if values.intersection({"codex_not_authenticated", "claude_not_authenticated"}):
        return CliRoleFailure.AUTH_MISSING
    if values.intersection({"unsupported_codex_version", "unsupported_claude_version"}):
        return CliRoleFailure.UNSUPPORTED_VERSION
    if values.intersection({"permission_sandbox_failure", "permission_denied"}):
        return CliRoleFailure.PERMISSION_FAILURE
    return CliRoleFailure.UNSUPPORTED_CAPABILITY


async def _supervise(
    driver: CliRoleDriver, invocation: Any, cancellation_event: Any | None
) -> CliProcessResult:
    cancellation = CliCancellationHandle()

    async def bridge() -> None:
        if cancellation_event is None or not hasattr(cancellation_event, "is_set"):
            return
        while not cancellation.is_cancelled:
            if cancellation_event.is_set():
                cancellation.cancel(CliCancellationOrigin.CONTROLLER)
                return
            await asyncio.sleep(0.01)

    bridge_task = asyncio.create_task(bridge())
    try:
        return await driver.supervisor.run(invocation, cancellation)
    finally:
        bridge_task.cancel()
        await asyncio.gather(bridge_task, return_exceptions=True)


def _runtime_failure(
    process: CliProcessResult, diagnostic: str
) -> CliRoleFailure | None:
    codes = {item.code for item in process.failures}
    if RuntimeFailure.TIMEOUT in codes:
        return CliRoleFailure.TIMEOUT
    if RuntimeFailure.CANCELLED in codes:
        return CliRoleFailure.CANCELLATION
    if RuntimeFailure.PROCESS_TREE_CLEANUP_FAILED in codes:
        return CliRoleFailure.CLEANUP_FAILURE
    if codes.intersection(
        {RuntimeFailure.EXECUTABLE_NOT_FOUND, RuntimeFailure.EXECUTABLE_NOT_RUNNABLE}
    ):
        return CliRoleFailure.EXECUTABLE_MISSING
    if RuntimeFailure.ARTIFACT_WRITE_FAILED in codes:
        return CliRoleFailure.ARTIFACT_PREPARATION_FAILURE
    lowered = diagnostic.casefold()
    if any(
        marker in lowered
        for marker in (
            "not logged in",
            "not authenticated",
            "login required",
            "authentication required",
            "authentication failed",
            "unauthorized",
        )
    ):
        return CliRoleFailure.AUTH_MISSING
    if any(
        marker in lowered
        for marker in ("permission denied", "sandbox denied", "tool denied")
    ):
        return CliRoleFailure.PERMISSION_FAILURE
    if any(
        marker in lowered
        for marker in (
            "unsupported version",
            "model unavailable",
            "model is unavailable",
        )
    ):
        return CliRoleFailure.UNSUPPORTED_VERSION
    if process.exit_code not in {0, None} or (
        process.infrastructure_state != "succeeded"
        and RuntimeFailure.FINAL_OUTPUT_MISSING not in codes
    ):
        return CliRoleFailure.PROCESS_CRASH
    if RuntimeFailure.FINAL_OUTPUT_MISSING in codes:
        return CliRoleFailure.MISSING_FINAL_RESULT
    if codes.intersection(
        {
            RuntimeFailure.MALFORMED_STREAM,
            RuntimeFailure.EVENT_LINE_LIMIT_EXCEEDED,
            RuntimeFailure.OUTPUT_DECODE_FAILED,
            RuntimeFailure.STDOUT_LIMIT_EXCEEDED,
            RuntimeFailure.STDERR_LIMIT_EXCEEDED,
        }
    ):
        return CliRoleFailure.MALFORMED_OUTPUT
    return None


def _write_jsonl(path: Path, rows: tuple[dict[str, Any], ...]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def _preserve_process_artifacts(
    process: CliProcessResult,
    staging: Path,
    workspace: PreparedCliRoleWorkspace,
    normalized_rows: tuple[dict[str, Any], ...],
) -> None:
    destination = workspace.agent_directory
    for name in (
        "invocation.json",
        "stdout.log",
        "stderr.log",
        "raw-events.jsonl",
        "output-tail.json",
        "controlled-settings.json",
        "controlled-mcp.json",
    ):
        source = staging / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    _write_jsonl(destination / "normalized-events.jsonl", normalized_rows)
    final_process = process.model_copy(
        update={
            "stdout": process.stdout.model_copy(
                update={"artifact_path": str(destination / "stdout.log")}
            ),
            "stderr": process.stderr.model_copy(
                update={"artifact_path": str(destination / "stderr.log")}
            ),
            "raw_events": process.raw_events.model_copy(
                update={"artifact_path": str(destination / "raw-events.jsonl")}
            ),
            "invocation_artifact": str(destination / "invocation.json"),
            "output_tail_artifact": str(destination / "output-tail.json"),
            "process_result_artifact": str(destination / "process-result.json"),
        }
    )
    write_json_atomic(destination / "process-result.json", final_process)


def _controlled_claude_files(staging: Path) -> tuple[Path, Path]:
    settings = staging / "controlled-settings.json"
    mcp = staging / "controlled-mcp.json"
    write_json_atomic(
        settings,
        {
            "autoMemoryEnabled": False,
            "hooks": {},
            "enabledPlugins": {},
            "permissions": {"allow": [], "deny": ["Bash", "Edit", "Write"]},
        },
    )
    write_json_atomic(mcp, {"mcpServers": {}})
    return settings, mcp


def _build_invocation(
    driver: CliRoleDriver,
    probe: CliRoleProbe,
    role: AgentRole,
    workspace: PreparedCliRoleWorkspace,
    staging: Path,
) -> Any:
    common = {
        "probe": probe,
        "workspace": workspace.root,
        "artifact_directory": staging,
        "prompt_bytes": workspace.prompt_bytes,
        "prompt_reference": workspace.prompt_reference,
        "prompt_sha256": workspace.prompt_sha256,
        "output_schema_path": workspace.output_schema_path,
    }
    if isinstance(driver, CodexCliDriver):
        if role == AgentRole.CLASSIFICATION:
            return driver.build_classifier_invocation(
                **common,
                final_output_path=workspace.raw_result_path,
                classification_id=workspace.invocation_id,
            )
        return driver.build_selector_invocation(
            **common,
            final_output_path=workspace.raw_result_path,
            selection_id=workspace.invocation_id,
        )
    settings, mcp = _controlled_claude_files(staging)
    if role == AgentRole.CLASSIFICATION:
        return driver.build_classifier_invocation(
            **common,
            classification_id=workspace.invocation_id,
            controlled_settings_path=settings,
            controlled_mcp_path=mcp,
        )
    return driver.build_selector_invocation(
        **common,
        selection_id=workspace.invocation_id,
        controlled_settings_path=settings,
        controlled_mcp_path=mcp,
    )


def _parse_provider_output(
    *,
    driver: CliRoleDriver,
    process: CliProcessResult,
    staging: Path,
    workspace: PreparedCliRoleWorkspace,
    run_id: str,
) -> tuple[str, str | None, tuple[dict[str, Any], ...], CliRoleFailure | None, str]:
    raw_events = staging / "raw-events.jsonl"
    if isinstance(driver, CodexCliDriver):
        try:
            parsed = parse_codex_events(
                raw_events,
                started_at=process.started_at,
                run_id=run_id,
                attempt_id=workspace.invocation_id,
                worktree_path=str(workspace.root),
                baseline_sha256=None,
            )
            raw = (
                workspace.raw_result_path.read_text(encoding="utf-8", errors="strict")
                if workspace.raw_result_path.is_file()
                else ""
            )
            return raw, parsed.thread_id, parsed.normalized_rows, None, ""
        except (OSError, UnicodeDecodeError, CodexEventParseError) as error:
            return (
                "",
                None,
                (),
                CliRoleFailure.MALFORMED_OUTPUT,
                f"Codex {workspace.role} event stream was malformed: {error}",
            )
    sanitize_claude_event_artifacts(
        tuple(path for path in (raw_events, staging / "stdout.log") if path.is_file())
    )
    try:
        parsed = parse_claude_events(
            raw_events,
            started_at=process.started_at,
            run_id=run_id,
            attempt_id=workspace.invocation_id,
            worktree_path=str(workspace.root),
            baseline_sha256=None,
        )
        if isinstance(parsed.structured_output, Mapping):
            raw = json.dumps(dict(parsed.structured_output), ensure_ascii=False)
            workspace.raw_result_path.write_text(
                raw + "\n", encoding="utf-8", newline="\n"
            )
        else:
            raw = ""
        if parsed.structured_output_parse_error:
            return (
                raw,
                parsed.session_id,
                parsed.normalized_rows,
                CliRoleFailure.MALFORMED_OUTPUT,
                parsed.structured_output_parse_error,
            )
        return raw, parsed.session_id, parsed.normalized_rows, None, ""
    except (OSError, UnicodeDecodeError, ClaudeEventParseError) as error:
        return (
            "",
            None,
            (),
            CliRoleFailure.MALFORMED_OUTPUT,
            f"Claude Code {workspace.role} event stream was malformed: {error}",
        )


def execute_cli_role(
    *,
    driver: CliRoleDriver,
    probe: CliRoleProbe,
    role: AgentRole,
    workspace: PreparedCliRoleWorkspace,
    run_id: str,
    cancellation_event: Any | None = None,
) -> CliRoleExecution:
    manifest_ok, manifest_reason = verify_cli_role_manifest(workspace)
    if not manifest_ok:
        return CliRoleExecution(
            raw_text="",
            failure=CliRoleFailure.INPUT_MANIFEST_VIOLATION,
            reason=manifest_reason,
            process_spawned=False,
            process_id=None,
            session_id=None,
            input_integrity_proved=False,
            target_unchanged=True,
            candidates_unchanged=True,
        )
    if not probe.ready:
        probe_failure = _probe_failure(probe)
        return CliRoleExecution(
            raw_text="",
            failure=probe_failure,
            reason="; ".join(probe.messages) or probe_failure.value,
            process_spawned=False,
            process_id=None,
            session_id=None,
            input_integrity_proved=True,
            target_unchanged=True,
            candidates_unchanged=True,
        )

    raw = ""
    session_id: str | None = None
    normalized_rows: tuple[dict[str, Any], ...] = ()
    failure: CliRoleFailure | None = None
    reason = ""
    process: CliProcessResult | None = None
    with tempfile.TemporaryDirectory(
        prefix=f"villani-cli-{role.value}-"
    ) as raw_staging:
        staging = Path(raw_staging).resolve()
        try:
            invocation = _build_invocation(driver, probe, role, workspace, staging)
            process = run_coroutine_sync(
                _supervise(driver, invocation, cancellation_event)
            )
            raw, session_id, normalized_rows, failure, reason = _parse_provider_output(
                driver=driver,
                process=process,
                staging=staging,
                workspace=workspace,
                run_id=run_id,
            )
            stderr = (
                (staging / "stderr.log").read_text(encoding="utf-8", errors="replace")
                if (staging / "stderr.log").is_file()
                else ""
            )
            runtime_failure = _runtime_failure(process, stderr)
            if runtime_failure is not None:
                failure = runtime_failure
                reason = (
                    process.failures[0].message
                    if process.failures
                    else f"CLI {role.value} process failed with exit {process.exit_code}."
                )
            elif failure is None and not raw.strip():
                failure = CliRoleFailure.MISSING_FINAL_RESULT
                reason = (
                    f"CLI {role.value} process produced no final structured result."
                )
            _preserve_process_artifacts(process, staging, workspace, normalized_rows)
        except Exception as error:
            failure = CliRoleFailure.ARTIFACT_PREPARATION_FAILURE
            reason = (
                f"CLI {role.value} invocation failed: {type(error).__name__}: {error}"
            )
            if process is not None:
                try:
                    _preserve_process_artifacts(
                        process, staging, workspace, normalized_rows
                    )
                except Exception:
                    pass

    manifest_after, manifest_after_reason = verify_cli_role_manifest(workspace)
    integrity_error: str | None = None
    try:
        target_unchanged = (
            repository_state_digest(workspace.target_repository)
            == workspace.target_state_before
        )
        candidates_unchanged = all(
            repository_state_digest(path) == before
            for path, before in workspace.candidate_states_before
        )
    except (OSError, CliRoleWorkspaceError) as error:
        target_unchanged = False
        candidates_unchanged = False
        integrity_error = f"CLI role repository integrity could not be proved: {error}"
    if not manifest_after:
        failure = CliRoleFailure.INPUT_MANIFEST_VIOLATION
        reason = manifest_after_reason
    elif integrity_error is not None:
        failure = CliRoleFailure.ARTIFACT_PREPARATION_FAILURE
        reason = integrity_error
    elif not target_unchanged:
        failure = CliRoleFailure.TARGET_MUTATION
        reason = f"CLI {role.value} changed the target repository."
    elif not candidates_unchanged:
        failure = CliRoleFailure.CANDIDATE_MUTATION
        reason = f"CLI {role.value} changed a candidate worktree."

    independence = {
        "schema_version": "villani.cli_role_independence.v1",
        "role": role.value,
        "role_invocation_id": workspace.invocation_id,
        "process_id": process.pid if process is not None else None,
        "session_id": session_id,
        "cwd": str(workspace.root),
        "resume_requested": False,
        "session_persistence": False,
        "process_spawned": process is not None,
        "target_repository_access": False,
        "candidate_worktree_access": False,
        "agent_writable_roots": [],
        "input_manifest_verified": manifest_after,
        "target_repository_unchanged": target_unchanged,
        "candidate_worktrees_unchanged": candidates_unchanged,
    }
    try:
        write_json_atomic(workspace.agent_directory / "independence.json", independence)
    except Exception as error:
        failure = CliRoleFailure.ARTIFACT_PREPARATION_FAILURE
        reason = f"CLI role independence evidence could not be preserved: {error}"

    return CliRoleExecution(
        raw_text=raw,
        failure=failure,
        reason=reason,
        process_spawned=process is not None,
        process_id=process.pid if process is not None else None,
        session_id=session_id,
        input_integrity_proved=manifest_after,
        target_unchanged=target_unchanged,
        candidates_unchanged=candidates_unchanged,
    )


__all__ = ["CliRoleExecution", "execute_cli_role"]
