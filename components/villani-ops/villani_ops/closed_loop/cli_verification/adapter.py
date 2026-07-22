"""CLI-backed semantic verifier adapter behind the existing Verifier port."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from villani_ops.closed_loop.adapters.villani_verifier import VillaniVerifierAdapter
from villani_ops.closed_loop.agent_systems.role_models import AgentRole
from villani_ops.closed_loop.claude_code_cli.driver import (
    ClaudeCodeCliDriver,
    ClaudeCodeDriverUnavailable,
)
from villani_ops.closed_loop.claude_code_cli.events import (
    ClaudeEventParseError,
    parse_claude_events,
    sanitize_claude_event_artifacts,
)
from villani_ops.closed_loop.claude_code_cli.models import ClaudeProbeResult
from villani_ops.closed_loop.cli_runtime import (
    CliCancellationHandle,
    CliCancellationOrigin,
    CliFailure as RuntimeFailure,
    CliProcessResult,
)
from villani_ops.closed_loop.codex_cli.driver import (
    CodexCliDriver,
    CodexDriverUnavailable,
    run_coroutine_sync,
)
from villani_ops.closed_loop.codex_cli.events import (
    CodexEventParseError,
    parse_codex_events,
)
from villani_ops.closed_loop.codex_cli.models import CodexProbeResult
from villani_ops.isolation.copy_git import remove_tree

from ..durable_io import write_json_atomic
from ..event_writer import redact_message
from ..interfaces import (
    AttemptContext,
    AttemptResult,
    EvidenceItem,
    Requirement,
    Verification,
)
from .models import (
    CLI_VERIFIER_NORMALIZED_RESULT_SCHEMA_VERSION,
    CliVerifierFailure,
    CliVerifierResult,
    normalize_cli_verifier_result,
)
from .workspace import (
    PreparedVerifierWorkspace,
    WorkspacePreparationError,
    prepare_verifier_workspace,
    repository_state_digest,
    verify_input_manifest,
)


CliDriver = CodexCliDriver | ClaudeCodeCliDriver
CliProbe = CodexProbeResult | ClaudeProbeResult


@dataclass(frozen=True, slots=True)
class _ExecutionEvidence:
    failure: CliVerifierFailure | None
    process_spawned: bool
    workspace_relative: str
    input_manifest_relative: str
    raw_result_relative: str
    normalized_result_relative: str
    independence_relative: str | None
    input_integrity_proved: bool
    target_unchanged: bool
    candidate_unchanged: bool
    process_independent: bool | None
    session_independent: bool | None


def _relative(path: Path, context: AttemptContext) -> str:
    return path.resolve().relative_to(Path(context.run_directory).resolve()).as_posix()


def _empty_usage(failure: CliVerifierFailure) -> tuple[dict[str, Any], ...]:
    return (
        {
            "stage": "verification",
            "backend": None,
            "model": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "token_accounting_status": "unknown",
            "model_calls": 0,
            "model_call_accounting_status": "complete",
            "cost": None,
            "cost_accounting_status": "unknown",
            "currency": None,
            "duration_ms": None,
            "duration_accounting_status": "unknown",
            "failure_state": failure.value,
        },
    )


def _failure_verification(
    context: AttemptContext,
    failure: CliVerifierFailure,
    reason: str,
    *,
    workspace: Path | None,
) -> Verification:
    from ..verification_evidence import extract_requirements

    definitions = extract_requirements(
        task_instruction=context.task,
        success_criteria=context.success_criteria,
        policy_configuration=context.policy_configuration,
    )
    artifact = None
    workspace_reference = None
    if workspace is not None:
        try:
            workspace_reference = _relative(workspace, context)
        except ValueError:
            workspace_reference = None
        candidate = workspace / "output" / "normalized-result.json"
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(
                candidate,
                {
                    "schema_version": CLI_VERIFIER_NORMALIZED_RESULT_SCHEMA_VERSION,
                    "status": "infrastructure_failure",
                    "failure_code": failure.value,
                    "decision": 0,
                    "reason": redact_message(reason),
                },
            )
            artifact = _relative(candidate, context)
        except Exception:
            artifact = None
    missing = tuple(
        EvidenceItem(
            evidence_id=f"cli_verifier_missing:{item.requirement_id}",
            kind="semantic_verification",
            summary="CLI verifier infrastructure did not produce acceptance-grade proof.",
            artifact_path=artifact,
        )
        for item in definitions
    )
    return Verification(
        verifier="villani_ops_verifier_pipeline",
        outcome="error",
        acceptance_eligible=False,
        confidence=None,
        reason=redact_message(reason),
        recommended_action="retry_verifier",
        requirement_results=tuple(
            Requirement(
                requirement_id=item.requirement_id,
                description=item.description,
                outcome="missing",
                evidence_ids=(f"cli_verifier_missing:{item.requirement_id}",),
            )
            for item in definitions
        ),
        missing_evidence=missing,
        risk_flags=(f"acceptance_blocker:{failure.value}",),
        raw_verifier_artifact=artifact,
        metadata={
            "verifier_version": "villani_cli_verifier_adapter_v1",
            "invocation_status": "error",
            "semantic_verifier_invoked": False,
            "semantic_verifier_status": "error",
            "cli_verifier_failure": failure.value,
            "cli_verifier_workspace": workspace_reference,
            "cli_verifier_normalized_result": artifact,
            "cli_verifier_process_spawned": False,
            "binary_user_projection": {"decision": 0, "reason": redact_message(reason)},
        },
        llm_usage=_empty_usage(failure),
    )


def _probe_failure(probe: CliProbe) -> CliVerifierFailure:
    values = {str(item.value) for item in probe.failures}
    if values.intersection({"codex_not_installed", "claude_not_installed"}):
        return CliVerifierFailure.EXECUTABLE_MISSING
    if values.intersection({"codex_not_authenticated", "claude_not_authenticated"}):
        return CliVerifierFailure.AUTH_MISSING
    if values.intersection({"unsupported_codex_version", "unsupported_claude_version"}):
        return CliVerifierFailure.UNSUPPORTED_VERSION
    if values.intersection({"permission_sandbox_failure", "permission_denied"}):
        return CliVerifierFailure.PERMISSION_FAILURE
    return CliVerifierFailure.UNSUPPORTED_CAPABILITY


async def _supervise(
    driver: CliDriver,
    invocation: Any,
    cancellation_event: Any | None,
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
) -> CliVerifierFailure | None:
    codes = {item.code for item in process.failures}
    if RuntimeFailure.TIMEOUT in codes:
        return CliVerifierFailure.TIMEOUT
    if RuntimeFailure.CANCELLED in codes:
        return CliVerifierFailure.CANCELLATION
    if RuntimeFailure.PROCESS_TREE_CLEANUP_FAILED in codes:
        return CliVerifierFailure.CLEANUP_FAILURE
    if codes.intersection(
        {RuntimeFailure.EXECUTABLE_NOT_FOUND, RuntimeFailure.EXECUTABLE_NOT_RUNNABLE}
    ):
        return CliVerifierFailure.EXECUTABLE_MISSING
    if RuntimeFailure.ARTIFACT_WRITE_FAILED in codes:
        return CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE
    lowered = diagnostic.casefold()
    if any(
        value in lowered
        for value in (
            "not logged in",
            "not authenticated",
            "login required",
            "authentication required",
            "authentication failed",
            "unauthorized",
        )
    ):
        return CliVerifierFailure.AUTH_MISSING
    if any(
        value in lowered
        for value in ("permission denied", "sandbox denied", "tool denied")
    ):
        return CliVerifierFailure.PERMISSION_FAILURE
    if any(
        value in lowered
        for value in (
            "unsupported version",
            "model unavailable",
            "model is unavailable",
        )
    ):
        return CliVerifierFailure.UNSUPPORTED_VERSION
    if RuntimeFailure.FINAL_OUTPUT_MISSING in codes:
        return CliVerifierFailure.MISSING_FINAL_RESULT
    if codes.intersection(
        {
            RuntimeFailure.MALFORMED_STREAM,
            RuntimeFailure.EVENT_LINE_LIMIT_EXCEEDED,
            RuntimeFailure.OUTPUT_DECODE_FAILED,
            RuntimeFailure.STDOUT_LIMIT_EXCEEDED,
            RuntimeFailure.STDERR_LIMIT_EXCEEDED,
        }
    ):
        return CliVerifierFailure.MALFORMED_OUTPUT
    if process.infrastructure_state != "succeeded" or process.exit_code not in {
        0,
        None,
    }:
        return CliVerifierFailure.PROCESS_CRASH
    return None


def _write_jsonl(path: Path, rows: tuple[dict[str, Any], ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def _copy_process_artifacts(
    process: CliProcessResult,
    staging: Path,
    workspace: PreparedVerifierWorkspace,
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


def _coder_process_id(context: AttemptContext, result: AttemptResult) -> int | None:
    value = result.metadata.get("process_result_path")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = Path(context.run_directory) / path
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    pid = document.get("pid") if isinstance(document, Mapping) else None
    return int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None


def _coder_session(result: AttemptResult) -> str | None:
    for key in ("codex_thread_id", "claude_code_session_id"):
        value = result.metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _pipeline_projection(
    result: CliVerifierResult | None,
    *,
    definitions: tuple[Any, ...],
    failure: CliVerifierFailure | None,
    reason: str,
) -> dict[str, Any]:
    if result is not None and failure is None:
        proved = set(result.requirements_proved)
        requirement_results = [
            {
                "id": item.requirement_id,
                "requirement": item.description,
                "status": (
                    "satisfied" if item.requirement_id in proved else "unsatisfied"
                ),
                "evidence": [f"cli_semantic:{item.requirement_id}"],
            }
            for item in definitions
        ]
        success = [
            {
                "id": f"cli_semantic:{item.requirement_id}",
                "kind": "semantic_reasoning",
                "summary": "Independent CLI semantic review reported this requirement proved.",
            }
            for item in definitions
            if item.requirement_id in proved
        ]
        failure_evidence = [
            {
                "id": f"cli_blocker:{item.code}",
                "kind": "semantic_reasoning",
                "summary": item.summary,
                "evidence_reference": item.evidence_reference,
            }
            for item in result.blocking_issues
        ]
        missing = [
            {
                "id": f"cli_semantic:{item.requirement_id}",
                "kind": "semantic_reasoning",
                "summary": "Independent CLI semantic review did not prove this requirement.",
            }
            for item in definitions
            if item.requirement_id not in proved
        ]
        semantic_failure = (
            CliVerifierFailure.SEMANTIC_REJECTION
            if result.blocking_issues
            else CliVerifierFailure.INSUFFICIENT_EVIDENCE
        )
        return {
            "result": result.decision,
            "verdict": "success" if result.decision == 1 else "failure",
            "recommendedAction": "accept" if result.decision == 1 else "reject",
            "reason": result.reason,
            "requirementResults": requirement_results,
            "successEvidence": success,
            "failureEvidence": failure_evidence,
            "missingEvidence": missing,
            "riskFlags": (
                []
                if result.decision == 1
                else [f"acceptance_blocker:{semantic_failure.value}"]
            ),
            "criticalRequirementCoverageProven": result.decision == 1,
            "focusedProbeRequests": [],
            "invocationStatus": "completed",
        }

    invocation_status = (
        "timeout"
        if failure == CliVerifierFailure.TIMEOUT
        else "malformed_output"
        if failure
        in {
            CliVerifierFailure.MALFORMED_OUTPUT,
            CliVerifierFailure.SCHEMA_FAILURE,
            CliVerifierFailure.MISSING_FINAL_RESULT,
        }
        else "subprocess_failure"
    )
    return {
        "result": 0,
        "verdict": "error",
        "recommendedAction": "retry_verifier",
        "reason": reason,
        "requirementResults": [
            {
                "id": item.requirement_id,
                "requirement": item.description,
                "status": "unclear",
                "evidence": [f"cli_verifier_missing:{item.requirement_id}"],
            }
            for item in definitions
        ],
        "successEvidence": [],
        "failureEvidence": [],
        "missingEvidence": [
            {
                "id": f"cli_verifier_missing:{item.requirement_id}",
                "kind": "semantic_verification",
                "summary": reason,
            }
            for item in definitions
        ],
        "riskFlags": [
            f"acceptance_blocker:{(failure or CliVerifierFailure.PROCESS_CRASH).value}"
        ],
        "criticalRequirementCoverageProven": False,
        "focusedProbeRequests": [],
        "invocationStatus": invocation_status,
    }


class CliVerifierAdapter:
    """Provider-specific process mechanics with one provider-neutral output contract."""

    def __init__(self, driver: CliDriver, *, probe: CliProbe) -> None:
        if driver.system.roles != {AgentRole.VERIFICATION}:
            raise ValueError(
                "CliVerifierAdapter requires a verification-only CLI agent system"
            )
        self.driver = driver
        self.probe = probe

    def _build_invocation(
        self,
        workspace: PreparedVerifierWorkspace,
        staging: Path,
    ) -> Any:
        common = {
            "probe": self.probe,
            "workspace": workspace.root,
            "artifact_directory": staging,
            "prompt_bytes": workspace.prompt.bytes,
            "prompt_reference": "input/verifier-prompt.txt",
            "prompt_sha256": workspace.prompt.sha256,
            "output_schema_path": workspace.input_directory
            / "verifier-result.schema.json",
            "verification_id": workspace.root.name,
        }
        if isinstance(self.driver, CodexCliDriver):
            return self.driver.build_verifier_invocation(
                **common,
                final_output_path=workspace.output_directory / "verifier-result.json",
            )
        settings = staging / "controlled-settings.json"
        mcp = staging / "controlled-mcp.json"
        write_json_atomic(
            settings,
            {
                "permissions": {"defaultMode": "plan"},
                "hooks": {},
                "enabledPlugins": {},
                "autoMemoryEnabled": False,
            },
        )
        write_json_atomic(mcp, {"mcpServers": {}})
        return self.driver.build_verifier_invocation(
            **common,
            controlled_settings_path=settings,
            controlled_mcp_path=mcp,
        )

    def _run_cli(
        self,
        context: AttemptContext,
        attempt_result: AttemptResult,
        workspace: PreparedVerifierWorkspace,
    ) -> tuple[dict[str, Any], _ExecutionEvidence]:
        raw_result_path = workspace.output_directory / "verifier-result.json"
        normalized_path = workspace.output_directory / "normalized-result.json"
        independence_path = workspace.agent_directory / "independence.json"
        if not self.probe.ready:
            failure = _probe_failure(self.probe)
            reason = "; ".join(self.probe.messages) or failure.value
            pipeline = _pipeline_projection(
                None,
                definitions=workspace.definitions,
                failure=failure,
                reason=reason,
            )
            write_json_atomic(
                normalized_path,
                {
                    "schema_version": CLI_VERIFIER_NORMALIZED_RESULT_SCHEMA_VERSION,
                    **pipeline,
                    "status": "infrastructure_failure",
                    "failure_code": failure.value,
                    "binary_user_projection": {"decision": 0, "reason": reason},
                },
            )
            write_json_atomic(
                independence_path,
                {
                    "schema_version": "villani.cli_verifier_independence.v1",
                    "process_spawned": False,
                    "session_created": False,
                    "input_manifest_blind": True,
                },
            )
            evidence = _ExecutionEvidence(
                failure=failure,
                process_spawned=False,
                workspace_relative=_relative(workspace.root, context),
                input_manifest_relative=_relative(workspace.manifest_path, context),
                raw_result_relative=_relative(raw_result_path, context),
                normalized_result_relative=_relative(normalized_path, context),
                independence_relative=_relative(independence_path, context),
                input_integrity_proved=True,
                target_unchanged=True,
                candidate_unchanged=True,
                process_independent=None,
                session_independent=None,
            )
            return pipeline, evidence

        staging = Path(tempfile.mkdtemp(prefix="villani-cli-verifier-"))
        process: CliProcessResult | None = None
        parsed_result: CliVerifierResult | None = None
        verifier_session: str | None = None
        normalized_rows: tuple[dict[str, Any], ...] = ()
        failure: CliVerifierFailure | None = None
        reason = "CLI verifier did not produce a result."
        try:
            invocation = self._build_invocation(workspace, staging)
            process = run_coroutine_sync(
                _supervise(self.driver, invocation, context.cancellation_event)
            )
            raw_events = staging / "raw-events.jsonl"
            stderr = (
                (staging / "stderr.log").read_text(encoding="utf-8", errors="replace")
                if (staging / "stderr.log").is_file()
                else ""
            )
            if isinstance(self.driver, CodexCliDriver):
                try:
                    parsed = parse_codex_events(
                        raw_events,
                        started_at=process.started_at,
                        run_id=context.run_id,
                        attempt_id=context.attempt_id,
                        worktree_path=str(workspace.root),
                        baseline_sha256=context.baseline_sha256,
                    )
                    verifier_session = parsed.thread_id
                    normalized_rows = parsed.normalized_rows
                except (OSError, UnicodeDecodeError, CodexEventParseError) as error:
                    failure = CliVerifierFailure.MALFORMED_OUTPUT
                    reason = f"Codex verifier event stream was malformed: {error}"
                raw_text = (
                    raw_result_path.read_text(encoding="utf-8", errors="strict")
                    if raw_result_path.is_file()
                    else ""
                )
            else:
                sanitize_claude_event_artifacts(
                    tuple(
                        path
                        for path in (raw_events, staging / "stdout.log")
                        if path.is_file()
                    )
                )
                try:
                    parsed = parse_claude_events(
                        raw_events,
                        started_at=process.started_at,
                        run_id=context.run_id,
                        attempt_id=context.attempt_id,
                        worktree_path=str(workspace.root),
                        baseline_sha256=context.baseline_sha256,
                    )
                    verifier_session = parsed.session_id
                    normalized_rows = parsed.normalized_rows
                    if isinstance(parsed.structured_output, Mapping):
                        raw_text = json.dumps(
                            dict(parsed.structured_output), ensure_ascii=False
                        )
                        raw_result_path.write_text(
                            raw_text + "\n", encoding="utf-8", newline="\n"
                        )
                    else:
                        raw_text = ""
                        if parsed.structured_output_parse_error:
                            failure = CliVerifierFailure.MALFORMED_OUTPUT
                            reason = parsed.structured_output_parse_error
                except (OSError, UnicodeDecodeError, ClaudeEventParseError) as error:
                    raw_text = ""
                    failure = CliVerifierFailure.MALFORMED_OUTPUT
                    reason = f"Claude verifier event stream was malformed: {error}"

            runtime_failure = _runtime_failure(process, stderr)
            if runtime_failure is not None:
                failure = runtime_failure
                reason = (
                    process.failures[0].message
                    if process.failures
                    else f"CLI verifier process failed with exit {process.exit_code}."
                )
            elif failure is None:
                if not raw_text:
                    failure = CliVerifierFailure.MISSING_FINAL_RESULT
                    reason = "CLI verifier produced no final structured result."
                else:
                    try:
                        parsed_result = normalize_cli_verifier_result(
                            raw_text,
                            requirement_ids={
                                item.requirement_id for item in workspace.definitions
                            },
                        )
                        reason = parsed_result.reason
                    except ValueError as error:
                        message = str(error)
                        failure = (
                            CliVerifierFailure.MALFORMED_OUTPUT
                            if message.startswith("malformed verifier JSON")
                            else CliVerifierFailure.SCHEMA_FAILURE
                        )
                        reason = message

            input_ok, input_reason = verify_input_manifest(workspace)
            try:
                target_unchanged = (
                    repository_state_digest(workspace.target_repository)
                    == workspace.target_state_before
                )
                candidate_unchanged = (
                    repository_state_digest(workspace.candidate_worktree)
                    == workspace.candidate_state_before
                )
            except Exception as error:
                target_unchanged = False
                candidate_unchanged = False
                input_reason = f"repository post-state proof failed: {error}"
            if not input_ok:
                failure = CliVerifierFailure.INPUT_MANIFEST_VIOLATION
                reason = input_reason
                parsed_result = None
            elif not target_unchanged or not candidate_unchanged:
                failure = CliVerifierFailure.PERMISSION_FAILURE
                reason = "Verifier access changed the target or candidate repository."
                parsed_result = None

            coder_pid = _coder_process_id(context, attempt_result)
            coder_session = _coder_session(attempt_result)
            coder_is_cli = attempt_result.runner_name.startswith(
                ("codex_cli:", "claude_code_cli:")
            )
            verifier_pid = process.pid
            process_independent = (
                verifier_pid != coder_pid
                if verifier_pid is not None and coder_pid is not None
                else None
            )
            session_independent = (
                verifier_session != coder_session
                if verifier_session is not None and coder_session is not None
                else None
            )
            cwd_independent = workspace.root != workspace.candidate_worktree
            roots = (workspace.output_directory, workspace.agent_directory)
            writable_roots_exclude_repositories = all(
                not root.is_relative_to(workspace.target_repository)
                and not root.is_relative_to(workspace.candidate_worktree)
                for root in roots
            )
            if failure is None and (
                verifier_pid is None
                or verifier_session is None
                or not cwd_independent
                or not writable_roots_exclude_repositories
                or (
                    coder_is_cli
                    and (
                        process_independent is not True
                        or session_independent is not True
                    )
                )
            ):
                failure = CliVerifierFailure.INDEPENDENCE_VIOLATION
                reason = "Independent verifier process, session, cwd, or writable-root proof failed."
                parsed_result = None

            _copy_process_artifacts(process, staging, workspace, normalized_rows)
            manifest = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
            blindness = manifest.get("blindness", {})
            input_manifest_blind = isinstance(blindness, Mapping) and all(
                value is False for value in blindness.values()
            )
            write_json_atomic(
                independence_path,
                {
                    "schema_version": "villani.cli_verifier_independence.v1",
                    "process_spawned": True,
                    "verifier_process_id": verifier_pid,
                    "coder_process_id": coder_pid,
                    "process_ids_distinct": process_independent,
                    "verifier_session_id": verifier_session,
                    "coder_session_id": coder_session,
                    "session_ids_distinct": session_independent,
                    "verifier_cwd": str(workspace.root),
                    "coder_worktree": str(workspace.candidate_worktree),
                    "target_repository": str(workspace.target_repository),
                    "cwd_differs_from_coder_worktree": cwd_independent,
                    "writable_roots": [str(item) for item in roots],
                    "writable_roots_exclude_candidate_and_target": (
                        writable_roots_exclude_repositories
                    ),
                    "input_manifest_blind": input_manifest_blind,
                    "input_manifest_excludes_provider_model_cost_rank_and_competitors": (
                        input_manifest_blind
                    ),
                    "coder_transcript_referenced": False,
                    "same_provider": (
                        attempt_result.runner_name.startswith("codex_cli:")
                        and isinstance(self.driver, CodexCliDriver)
                    )
                    or (
                        attempt_result.runner_name.startswith("claude_code_cli:")
                        and isinstance(self.driver, ClaudeCodeCliDriver)
                    ),
                    "input_integrity_proved": input_ok,
                    "target_unchanged": target_unchanged,
                    "candidate_unchanged": candidate_unchanged,
                    "process_ids_public": False,
                },
            )
        except (
            OSError,
            ValueError,
            TypeError,
            ValidationError,
            CodexDriverUnavailable,
            ClaudeCodeDriverUnavailable,
        ) as error:
            if failure is None:
                failure = CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE
                reason = (
                    f"CLI verifier invocation failed: {type(error).__name__}: {error}"
                )
            input_ok, _ = verify_input_manifest(workspace)
            target_unchanged = False
            candidate_unchanged = False
            process_independent = None
            session_independent = None
        finally:
            if (
                process is not None
                and not (workspace.agent_directory / "process-result.json").is_file()
            ):
                try:
                    _copy_process_artifacts(
                        process, staging, workspace, normalized_rows
                    )
                except Exception as error:
                    failure = CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE
                    reason = (
                        "CLI verifier process evidence could not be preserved: "
                        f"{type(error).__name__}: {error}"
                    )
                    parsed_result = None
            try:
                remove_tree(staging)
                staging_removed = not staging.exists()
            except Exception:
                staging_removed = False
            if not staging_removed:
                failure = CliVerifierFailure.CLEANUP_FAILURE
                reason = "CLI verifier staging cleanup failed after governed evidence was preserved."
                parsed_result = None
            try:
                write_json_atomic(
                    workspace.agent_directory / "cleanup.json",
                    {
                        "schema_version": "villani.cli_verifier_cleanup.v1",
                        "staging_removed": staging_removed,
                        "governed_process_evidence_preserved": (
                            process is None
                            or (
                                workspace.agent_directory / "process-result.json"
                            ).is_file()
                        ),
                    },
                )
            except Exception:
                failure = CliVerifierFailure.CLEANUP_FAILURE
                reason = "CLI verifier cleanup evidence could not be persisted."
                parsed_result = None

        pipeline = _pipeline_projection(
            parsed_result,
            definitions=workspace.definitions,
            failure=failure,
            reason=reason,
        )
        status = (
            "accepted"
            if parsed_result is not None
            and parsed_result.decision == 1
            and failure is None
            else "semantic_rejection"
            if parsed_result is not None
            and parsed_result.blocking_issues
            and failure is None
            else "insufficient_evidence"
            if parsed_result is not None and failure is None
            else "infrastructure_failure"
        )
        effective_failure = failure
        if (
            effective_failure is None
            and parsed_result is not None
            and parsed_result.decision == 0
        ):
            effective_failure = (
                CliVerifierFailure.SEMANTIC_REJECTION
                if parsed_result.blocking_issues
                else CliVerifierFailure.INSUFFICIENT_EVIDENCE
            )
        write_json_atomic(
            normalized_path,
            {
                "schema_version": CLI_VERIFIER_NORMALIZED_RESULT_SCHEMA_VERSION,
                **pipeline,
                "status": status,
                "failure_code": effective_failure.value if effective_failure else None,
                "requirements_proved": (
                    parsed_result.requirements_proved if parsed_result else []
                ),
                "requirements_not_proved": (
                    parsed_result.requirements_not_proved
                    if parsed_result
                    else [item.requirement_id for item in workspace.definitions]
                ),
                "blocking_issues": (
                    [
                        item.model_dump(mode="json")
                        for item in parsed_result.blocking_issues
                    ]
                    if parsed_result
                    else []
                ),
                "binary_user_projection": {
                    "decision": pipeline["result"],
                    "reason": pipeline["reason"],
                },
            },
        )
        evidence = _ExecutionEvidence(
            failure=effective_failure,
            process_spawned=process is not None and process.pid is not None,
            workspace_relative=_relative(workspace.root, context),
            input_manifest_relative=_relative(workspace.manifest_path, context),
            raw_result_relative=_relative(raw_result_path, context),
            normalized_result_relative=_relative(normalized_path, context),
            independence_relative=(
                _relative(independence_path, context)
                if independence_path.is_file()
                else None
            ),
            input_integrity_proved=bool(locals().get("input_ok", False)),
            target_unchanged=bool(locals().get("target_unchanged", False)),
            candidate_unchanged=bool(locals().get("candidate_unchanged", False)),
            process_independent=locals().get("process_independent"),
            session_independent=locals().get("session_independent"),
        )
        return pipeline, evidence

    def verify(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
    ) -> Verification:
        try:
            workspace = prepare_verifier_workspace(attempt_context, attempt_result)
        except WorkspacePreparationError as error:
            return _failure_verification(
                attempt_context,
                error.failure,
                str(error),
                workspace=error.workspace,
            )
        evidence_holder: list[_ExecutionEvidence] = []

        def invoke(**_kwargs: Any) -> dict[str, Any]:
            raw, evidence = self._run_cli(attempt_context, attempt_result, workspace)
            evidence_holder.append(evidence)
            return raw

        metadata = dict(attempt_result.metadata)
        metadata["debug_trace_path"] = _relative(
            workspace.input_directory / "debug-artifacts", attempt_context
        )
        isolated_result = replace(attempt_result, metadata=metadata)
        pipeline = VillaniVerifierAdapter(
            raw_verifier=invoke,
            invocation="in_process",
            no_llm=False,
            model=self.driver.system.model,
            timeout_seconds=self.driver.system.timeout_seconds,
        )
        verification = pipeline.verify(attempt_context, isolated_result)
        if evidence_holder:
            evidence = evidence_holder[-1]
        else:
            normalized = workspace.output_directory / "normalized-result.json"
            write_json_atomic(
                normalized,
                {
                    "schema_version": CLI_VERIFIER_NORMALIZED_RESULT_SCHEMA_VERSION,
                    "status": "deterministic_rejection",
                    "failure_code": None,
                    "decision": 0,
                    "reason": verification.reason,
                    "binary_user_projection": {
                        "decision": 0,
                        "reason": verification.reason,
                    },
                },
            )
            evidence = _ExecutionEvidence(
                failure=None,
                process_spawned=False,
                workspace_relative=_relative(workspace.root, attempt_context),
                input_manifest_relative=_relative(
                    workspace.manifest_path, attempt_context
                ),
                raw_result_relative=_relative(
                    workspace.output_directory / "verifier-result.json",
                    attempt_context,
                ),
                normalized_result_relative=_relative(normalized, attempt_context),
                independence_relative=None,
                input_integrity_proved=True,
                target_unchanged=True,
                candidate_unchanged=True,
                process_independent=None,
                session_independent=None,
            )
        binary_decision = int(
            verification.metadata.get("binary_verification_decision") or 0
        )
        public_reason = verification.reason
        normalized_path = (
            Path(attempt_context.run_directory) / evidence.normalized_result_relative
        )
        try:
            normalized_value = json.loads(normalized_path.read_text(encoding="utf-8"))
            normalized_document = (
                dict(normalized_value) if isinstance(normalized_value, Mapping) else {}
            )
            semantic_decision = normalized_document.get(
                "result", normalized_document.get("decision")
            )
            semantic_status = normalized_document.get("status")
            normalized_document.update(
                {
                    "schema_version": CLI_VERIFIER_NORMALIZED_RESULT_SCHEMA_VERSION,
                    "semantic_decision": semantic_decision,
                    "semantic_status": semantic_status,
                    "decision": binary_decision,
                    "reason": public_reason,
                    "acceptance_eligible": verification.acceptance_eligible,
                    "final_outcome": verification.outcome,
                    "final_reason_code": verification.metadata.get(
                        "computed_final_reason_code"
                    ),
                    "status": (
                        "accepted"
                        if binary_decision == 1
                        else "semantic_rejection"
                        if evidence.failure == CliVerifierFailure.SEMANTIC_REJECTION
                        else "insufficient_evidence"
                        if evidence.failure == CliVerifierFailure.INSUFFICIENT_EVIDENCE
                        else "infrastructure_failure"
                        if verification.outcome == "error"
                        else "deterministic_rejection"
                    ),
                    "binary_user_projection": {
                        "decision": binary_decision,
                        "reason": public_reason,
                    },
                }
            )
            write_json_atomic(normalized_path, normalized_document)
        except Exception:
            failure = CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE
            public_reason = "CLI verifier normalized result could not be finalized."
            binary_decision = 0
            evidence = replace(evidence, failure=failure)
            verification = replace(
                verification,
                outcome="error",
                acceptance_eligible=False,
                reason=public_reason,
                recommended_action="retry_verifier",
                risk_flags=(
                    *verification.risk_flags,
                    f"acceptance_blocker:{failure.value}",
                ),
            )
        return replace(
            verification,
            metadata={
                **dict(verification.metadata),
                "cli_verifier_workspace": evidence.workspace_relative,
                "cli_verifier_input_manifest": evidence.input_manifest_relative,
                "cli_verifier_raw_result": evidence.raw_result_relative,
                "cli_verifier_normalized_result": evidence.normalized_result_relative,
                "cli_verifier_independence_evidence": evidence.independence_relative,
                "cli_verifier_failure": (
                    evidence.failure.value if evidence.failure is not None else None
                ),
                "cli_verifier_process_spawned": evidence.process_spawned,
                "cli_verifier_input_integrity_proved": (
                    evidence.input_integrity_proved
                ),
                "cli_verifier_target_unchanged": evidence.target_unchanged,
                "cli_verifier_candidate_unchanged": evidence.candidate_unchanged,
                "cli_verifier_process_independent": evidence.process_independent,
                "cli_verifier_session_independent": evidence.session_independent,
                "binary_user_projection": {
                    "decision": binary_decision,
                    "reason": public_reason,
                },
            },
        )

    def finalize_with_focused_probes(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
        initial_verification: Verification,
    ) -> Verification:
        return VillaniVerifierAdapter(no_llm=False).finalize_with_focused_probes(
            attempt_context, attempt_result, initial_verification
        )

    def finalize_independent_verification(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
        verification: Verification,
    ) -> Verification:
        return VillaniVerifierAdapter(no_llm=False).finalize_independent_verification(
            attempt_context, attempt_result, verification
        )


__all__ = ["CliVerifierAdapter"]
