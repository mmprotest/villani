"""Codex CLI implementation of the existing coding AttemptRunner port."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from villani_ops.agentic.git_artifacts import is_patch_excluded
from villani_ops.closed_loop.adapters.git_isolation import GitIsolationAdapter
from villani_ops.closed_loop.cli_runtime import (
    CliCancellationHandle,
    CliCancellationOrigin,
    CliOutputTail,
    CliProcessResult,
)
from villani_ops.closed_loop.cli_coding.evidence import (
    collect_candidate_evidence as collect_cli_candidate_evidence,
    prepare_candidate as prepare_cli_candidate,
    relative_to_run as cli_relative_to_run,
    sanitize_and_parse_final as sanitize_and_parse_cli_final,
    write_normalized_events as write_cli_normalized_events,
)
from villani_ops.closed_loop.durable_io import write_json_atomic
from villani_ops.closed_loop.event_writer import redact_data, redact_message
from villani_ops.closed_loop.interfaces import (
    AttemptContext,
    AttemptResult,
    DependencyFailure,
)
from villani_ops.execution_environment.models import (
    CandidatePatchQuality,
)
from villani_ops.execution_environment.secrets import registered_secret_values

from .driver import CodexCliDriver, run_coroutine_sync
from .events import CodexEventParseError, ParsedCodexEvents, parse_codex_events
from .models import CodexCoderResult, CodexFailure, CodexProbeResult
from .prompt import build_codex_coding_prompt


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _git(repo: Path, *arguments: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        text=text,
        capture_output=True,
        check=False,
    )


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _relative_to_run(path: Path, context: AttemptContext) -> str:
    return (
        Path(path)
        .resolve()
        .relative_to(Path(context.run_directory).resolve())
        .as_posix()
    )


def _safe_path(worktree: Path, relative: str) -> bool:
    candidate = Path(relative.replace("\\", "/"))
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        return False
    try:
        resolved = (worktree / candidate).resolve(strict=False)
        resolved.relative_to(worktree.resolve())
    except (OSError, ValueError):
        return False
    return True


def _status_document(worktree: Path) -> dict[str, Any]:
    completed = _git(
        worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        text=False,
    )
    if completed.returncode != 0:
        return {
            "schema_version": "villani.codex_repository_status.v1",
            "status": "failed",
            "error": bytes(completed.stderr or b"").decode("utf-8", errors="replace"),
            "entries": [],
            "forbidden_paths_touched": [],
            "unsafe_paths": [],
            "untracked_files": [],
        }
    fields = (
        bytes(completed.stdout or b"").decode("utf-8", errors="replace").split("\0")
    )
    entries: list[dict[str, Any]] = []
    paths: list[str] = []
    untracked: list[str] = []
    index = 0
    while index < len(fields):
        raw = fields[index]
        index += 1
        if not raw:
            continue
        status = raw[:2]
        path = raw[3:] if len(raw) > 3 else ""
        row_paths = [path] if path else []
        if ("R" in status or "C" in status) and index < len(fields) and fields[index]:
            row_paths.append(fields[index])
            index += 1
        entries.append({"status": status, "paths": row_paths})
        paths.extend(row_paths)
        if status == "??":
            untracked.extend(row_paths)
    unique_paths = sorted(dict.fromkeys(paths))
    return {
        "schema_version": "villani.codex_repository_status.v1",
        "status": "captured",
        "entries": entries,
        "forbidden_paths_touched": sorted(
            path for path in unique_paths if is_patch_excluded(path)
        ),
        "unsafe_paths": sorted(
            path for path in unique_paths if not _safe_path(worktree, path)
        ),
        "untracked_files": sorted(dict.fromkeys(untracked)),
    }


def _external_symlinks(worktree: Path) -> list[str]:
    unsafe: list[str] = []
    for path in worktree.rglob("*"):
        if not path.is_symlink():
            continue
        try:
            path.resolve(strict=False).relative_to(worktree.resolve())
        except (OSError, ValueError):
            unsafe.append(path.relative_to(worktree).as_posix())
    return sorted(unsafe)


def _patch_quality(
    context: AttemptContext,
    patch: str,
    changed_files: list[str],
    status: Mapping[str, Any],
    *,
    path_violation: bool,
) -> CandidatePatchQuality:
    untracked = [str(item) for item in status.get("untracked_files", [])]
    added = sum(
        1
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1
        for line in patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    if path_violation:
        quality_status = "ineligible"
        reasons = ["path_violation"]
    elif not patch.strip():
        quality_status = "ineligible"
        reasons = ["empty_patch"]
    else:
        quality_status = "eligible"
        reasons = ["git_derived_patch_present"]
    return CandidatePatchQuality(
        schema_version="villani.candidate_patch_quality.v1",
        candidate_id=context.attempt_id,
        status=quality_status,
        tracked_files_changed=sorted(
            path for path in changed_files if path not in set(untracked)
        ),
        relevant_files_changed=sorted(changed_files),
        untracked_files=sorted(untracked),
        ignored_files=[],
        villani_owned_files=[
            str(item) for item in status.get("forbidden_paths_touched", [])
        ],
        generated_files=[],
        semantic_lines_added=added,
        semantic_lines_removed=removed,
        line_ending_only_lines=0,
        whitespace_only_lines=0,
        file_mode_only_changes=[],
        bulk_rewrite_files=[],
        relevant_diff_ratio=1.0 if patch.strip() else 0.0,
        reason_codes=reasons,
    )


def _sanitize_and_parse_final(
    path: Path, *, maximum_bytes: int, secrets: tuple[str, ...]
) -> tuple[CodexCoderResult | None, str | None]:
    if not path.is_file():
        return None, "final structured output is missing"
    try:
        data = path.read_bytes()
    except OSError as error:
        return None, f"final structured output could not be read: {error}"
    if len(data) > maximum_bytes:
        bounded = data[:maximum_bytes].decode("utf-8", errors="replace")
        path.write_text(str(redact_data(bounded, secrets=secrets)), encoding="utf-8")
        return None, f"final structured output exceeded {maximum_bytes} bytes"
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        path.write_text(
            str(redact_data(data.decode("utf-8", errors="replace"), secrets=secrets)),
            encoding="utf-8",
        )
        return None, f"final structured output is not UTF-8 at byte {error.start}"
    try:
        result = CodexCoderResult.model_validate_json(text)
    except ValidationError as error:
        path.write_text(str(redact_data(text, secrets=secrets)), encoding="utf-8")
        issue = error.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(item) for item in issue.get("loc", ())) or "value"
        return None, f"{location}: {issue.get('msg', 'invalid structured output')}"
    safe = redact_data(result.model_dump(mode="json"), secrets=secrets)
    write_json_atomic(path, safe)
    return CodexCoderResult.model_validate(safe), None


def _write_normalized_events(path: Path, parsed: ParsedCodexEvents) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in parsed.normalized_rows
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def _write_verifier_trace_projection(
    *,
    directory: Path,
    context: AttemptContext,
    worktree: Path,
    changed_files: list[str],
    parsed: ParsedCodexEvents,
    provider_identity: Mapping[str, Any],
    process: CliProcessResult,
    secrets: tuple[str, ...],
) -> None:
    """Project exposed Codex events into the existing verifier trace boundary."""

    directory.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        directory / "session_meta.json",
        redact_data(
            {
                "schema_version": "villani.codex_verifier_trace.v1",
                "run_id": context.run_id,
                "attempt_id": context.attempt_id,
                "objective": context.task,
                "repo": str(worktree),
                "provider": "codex_cli",
                "model": provider_identity.get("model"),
                "source_events": "../normalized-events.jsonl",
                "source_provider_identity": "../provider.json",
                "created_at": process.started_at.isoformat().replace("+00:00", "Z"),
            },
            secrets=secrets,
        ),
    )
    commands: list[Mapping[str, Any]] = []
    tool_calls: list[Mapping[str, Any]] = []
    model_responses: list[Mapping[str, Any]] = []
    for row in parsed.normalized_rows:
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload")
        values = payload if isinstance(payload, Mapping) else {}
        if event_type in {"command_completed", "command_failed"}:
            commands.append(
                {
                    "event_id": row.get("source_event_id"),
                    "ts": row.get("timestamp"),
                    "command": values.get("command"),
                    "command_role": values.get("command_role") or "unknown",
                    "candidate_state": "post_mutation",
                    "cwd": str(worktree),
                    "exit_code": values.get("exit_code"),
                    "stdout": values.get("output") or "",
                    "stderr": "",
                }
            )
        elif event_type in {"tool_call_started", "tool_call_completed"}:
            tool_calls.append(
                {
                    "tool_call_id": row.get("source_event_id"),
                    "tool_name": values.get("tool") or "codex_tool",
                    "tool_category": "provider_tool",
                    "started_at": row.get("timestamp"),
                    "status": (
                        "completed"
                        if event_type == "tool_call_completed"
                        else "started"
                    ),
                    "args": {},
                    "result_summary": "Exposed by Codex JSONL.",
                }
            )
        elif event_type == "agent_message":
            model_responses.append(
                {
                    "event_id": row.get("source_event_id"),
                    "ts": row.get("timestamp"),
                    "text": values.get("text") or "",
                }
            )
    _write_jsonl(directory / "commands.jsonl", commands)
    _write_jsonl(directory / "tool_calls.jsonl", tool_calls)
    _write_jsonl(directory / "model_responses.jsonl", model_responses)
    _write_jsonl(directory / "patches.jsonl", [])
    _write_jsonl(directory / "validations.jsonl", [])
    summary = {
        "schema_version": "villani.codex_verifier_trace_summary.v1",
        "status": "completed"
        if process.infrastructure_state == "succeeded"
        else "failed",
        "duration_ms": process.duration_ms,
        "changed_files": changed_files,
        "tokens_input": parsed.input_tokens,
        "tokens_output": parsed.output_tokens,
        "source": "codex_jsonl_projection",
        "hidden_reasoning_included": False,
    }
    write_json_atomic(directory / "summary.json", summary)
    write_json_atomic(directory / "final_summary.json", summary)


class CodexCliAttemptAdapter:
    """Run one Codex coding loop in a Villani-owned isolated Git worktree."""

    def __init__(
        self,
        driver: CodexCliDriver,
        *,
        probe: CodexProbeResult | None = None,
        isolation: GitIsolationAdapter | None = None,
    ) -> None:
        self.driver = driver
        self.probe = probe or driver.probe()
        self.isolation = isolation or GitIsolationAdapter()

    async def _supervise(
        self,
        invocation,
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
            return await self.driver.supervisor.run(invocation, cancellation)
        finally:
            bridge_task.cancel()
            await asyncio.gather(bridge_task, return_exceptions=True)

    def _probe_failure(self, context: AttemptContext) -> AttemptResult:
        agent_directory = Path(context.attempt_directory) / "agent"
        agent_directory.mkdir(parents=True, exist_ok=True)
        write_json_atomic(agent_directory / "provider.json", self.probe)
        failure = (
            self.probe.failures[0]
            if self.probe.failures
            else CodexFailure.UNSUPPORTED_VERSION
        )
        message = "; ".join(self.probe.messages) or "Codex doctor did not pass"
        return AttemptResult(
            runner_name=f"codex_cli:{self.driver.system.id}",
            status="failed",
            worktree_path="unavailable",
            patch=None,
            exit_code=None,
            model=self.driver.system.model,
            error=DependencyFailure(
                code=failure.value,
                message=redact_message(message),
                details={
                    "infrastructure_failure": True,
                    "agent_system_id": self.driver.system.id,
                },
            ),
            metadata={
                "failure_category": failure.value,
                "infrastructure_failure": True,
                "agent_system_id": self.driver.system.id,
                "agent_role": "coding",
            },
        )

    def run(self, attempt_context: AttemptContext) -> AttemptResult:
        if not self.probe.ready:
            return self._probe_failure(attempt_context)
        context = attempt_context
        attempt_directory = Path(context.attempt_directory).resolve()
        agent_directory = attempt_directory / "agent"
        repository_directory = attempt_directory / "repository"
        agent_directory.mkdir(parents=True, exist_ok=True)
        repository_directory.mkdir(parents=True, exist_ok=True)
        secrets = tuple(registered_secret_values())
        prepared = prepare_cli_candidate(
            context=context,
            isolation=self.isolation,
            repository_directory=repository_directory,
            secrets=secrets,
            schema_prefix="codex",
        )
        isolated = prepared.isolated
        worktree = prepared.worktree
        external_symlinks = list(prepared.external_symlinks)
        baseline_document = prepared.baseline_document

        if external_symlinks:
            status_document = {
                **_status_document(worktree),
                "external_symlinks": external_symlinks,
            }
            write_json_atomic(repository_directory / "status.json", status_document)
            write_json_atomic(
                repository_directory / "cleanup.json",
                {
                    "status": "retained_for_controller_cleanup",
                    "worktree": str(worktree),
                },
            )
            return AttemptResult(
                runner_name=f"codex_cli:{self.driver.system.id}",
                status="failed",
                worktree_path=str(worktree),
                patch=None,
                exit_code=None,
                model=self.driver.system.model,
                error=DependencyFailure(
                    code=CodexFailure.PATH_VIOLATION.value,
                    message="isolated worktree contains a symlink resolving outside its scope",
                    details={
                        "paths": external_symlinks,
                        "infrastructure_failure": True,
                    },
                ),
                metadata={
                    "failure_category": CodexFailure.PATH_VIOLATION.value,
                    "infrastructure_failure": True,
                    "changed_files": [],
                    "path_violation": True,
                    "agent_system_id": self.driver.system.id,
                    "agent_role": "coding",
                    "candidate_execution_worktree_paths": [str(worktree)],
                },
            )

        prompt = build_codex_coding_prompt(
            task=context.task,
            success_criteria=context.success_criteria,
            attempt_id=context.attempt_id,
            worktree=worktree,
            instruction_policy=self.driver.system.instruction_policy,
        )
        prompt_path = agent_directory / "prompt.txt"
        prompt_path.write_text(prompt.text, encoding="utf-8", newline="\n")
        (agent_directory / "prompt.digest").write_text(
            prompt.sha256 + "\n", encoding="utf-8"
        )

        from villani_ops.closed_loop.schema_validation import SCHEMA_ROOT

        output_schema_path = agent_directory / "coder-result.schema.json"
        shutil.copyfile(
            SCHEMA_ROOT / "codex-coder-result.schema.json", output_schema_path
        )
        final_output_path = agent_directory / "final-output.json"
        provider_identity = self.driver.provider_identity(self.probe)
        write_json_atomic(agent_directory / "provider.json", provider_identity)
        invocation = self.driver.build_invocation(
            probe=self.probe,
            worktree=worktree,
            agent_directory=agent_directory,
            prompt_bytes=prompt.bytes,
            prompt_reference=cli_relative_to_run(prompt_path, context),
            prompt_sha256=prompt.sha256,
            output_schema_path=output_schema_path,
            final_output_path=final_output_path,
            run_id=context.run_id,
            attempt_id=context.attempt_id,
            baseline_sha256=context.baseline_sha256,
        )
        process = run_coroutine_sync(
            self._supervise(invocation, context.cancellation_event)
        )

        parsed_events: ParsedCodexEvents
        event_error: str | None = None
        try:
            parsed_events = parse_codex_events(
                agent_directory / "codex-events.jsonl",
                started_at=process.started_at,
                run_id=context.run_id,
                attempt_id=context.attempt_id,
                worktree_path=str(worktree),
                baseline_sha256=context.baseline_sha256,
                secrets=secrets,
            )
        except (OSError, UnicodeDecodeError, CodexEventParseError) as error:
            parsed_events = ParsedCodexEvents((), (), None, None, None)
            event_error = str(error)
        normalized_events_path = agent_directory / "normalized-events.jsonl"
        write_cli_normalized_events(normalized_events_path, parsed_events)
        verifier_trace_directory = agent_directory / "verifier-trace"

        final_result, final_error = sanitize_and_parse_cli_final(
            final_output_path,
            model=CodexCoderResult,
            maximum_bytes=self.driver.integer_option(
                self.driver.system.provider_options,
                "maximum_final_output_bytes",
                1024 * 1024,
            ),
            secrets=secrets,
        )
        if event_error is not None and final_error is None:
            final_error = event_error

        collected = collect_cli_candidate_evidence(
            context=context,
            prepared=prepared,
            repository_directory=repository_directory,
            process=process,
            parsed=parsed_events,
            provider_identity=provider_identity.model_dump(mode="json"),
            execution_provider="codex_cli",
            environment_policy=self.driver.system.environment_policy,
            secrets=secrets,
            schema_prefix="codex",
        )
        forbidden = list(collected.forbidden_paths)
        path_violation = collected.path_violation
        patch_path = collected.patch_path
        patch_bytes = collected.patch_bytes
        patch = collected.patch
        changed_files = list(collected.changed_files)
        changed_document = collected.changed_document
        quality = collected.quality
        fingerprint = collected.fingerprint
        repository_validation = collected.repository_validation
        candidate_manifest = collected.candidate_manifest
        verifier_trace_directory = collected.verifier_trace_directory

        try:
            tail = CliOutputTail.model_validate_json(
                (agent_directory / "output-tail.json").read_text(encoding="utf-8")
            )
            stdout_tail, stderr_tail = tail.stdout, tail.stderr
        except (OSError, ValidationError):
            stdout_tail = ""
            stderr_tail = ""
        failure = self.driver.classify_failure(
            process,
            stderr_tail=stderr_tail,
            final_output_error=final_error,
            path_violation=path_violation,
            has_patch=bool(patch_bytes.strip()),
        )
        if changed_document.get("capture_failure") and failure is None:
            failure = CodexFailure.PROCESS_CRASH
        status = (
            "cancelled"
            if failure == CodexFailure.PROCESS_CANCELLATION
            else "failed"
            if failure is not None
            else "completed"
        )
        normalized_result = {
            "schema_version": "villani.codex_normalized_result.v1",
            "run_id": context.run_id,
            "attempt_id": context.attempt_id,
            "status": status,
            "failure": failure.value if failure else None,
            "agent_report": final_result.model_dump(mode="json")
            if final_result
            else None,
            "agent_report_error": final_error,
            "patch": {
                "path": cli_relative_to_run(patch_path, context),
                "sha256": changed_document["candidate_digest"],
                "bytes": len(patch_bytes),
                "changed_files": changed_files,
                "source": "git",
            },
            "provider_identity": cli_relative_to_run(
                agent_directory / "provider.json", context
            ),
            "thread_id": parsed_events.thread_id,
        }
        write_json_atomic(
            agent_directory / "normalized-result.json",
            redact_data(normalized_result, secrets=secrets),
        )

        infrastructure_failure = failure not in {None, CodexFailure.COMPLETED_NO_PATCH}
        failure_classification = (
            "infrastructure_failure"
            if infrastructure_failure
            else "coding_failure"
            if failure == CodexFailure.COMPLETED_NO_PATCH
            else None
        )
        quality_document = quality.model_dump(mode="json")
        metadata = {
            "failure_category": failure.value if failure else None,
            "failure_classification": failure_classification,
            "infrastructure_failure": infrastructure_failure,
            "agent_system_id": self.driver.system.id,
            "agent_role": "coding",
            "codex_driver": "exec_jsonl",
            "codex_thread_id": parsed_events.thread_id,
            "instruction_policy": self.driver.system.instruction_policy,
            "permission_profile": self.driver.system.permission_profile,
            "changed_files": changed_files,
            "has_non_empty_patch": bool(patch_bytes.strip()),
            "forbidden_paths_touched": forbidden,
            "path_violation": path_violation,
            "baseline_digest": baseline_document["baseline_digest"],
            "candidate_digest": changed_document["candidate_digest"],
            "candidate_bundle_path": cli_relative_to_run(
                attempt_directory / "candidate" / "candidate.json", context
            ),
            "candidate_bundle_schema_version": candidate_manifest.schema_version,
            "candidate_patch_quality": quality_document,
            "candidate_patch_quality_status": quality.status,
            "candidate_patch_quality_path": cli_relative_to_run(
                attempt_directory / "candidate-patch-quality.json", context
            ),
            "candidate_quality_report": quality_document,
            "relevant_diff_ratio": quality.relevant_diff_ratio,
            "line_ending_only_lines": quality.line_ending_only_lines,
            "candidate_execution_worktree_paths": [str(worktree)],
            "worktree": isolated.metadata,
            "repository_validation_path": cli_relative_to_run(
                attempt_directory / "repository-validation.json", context
            ),
            "repository_validation": repository_validation.model_dump(mode="json"),
            "repository_validation_status": repository_validation.status,
            "repository_validation_failure_code": repository_validation.failure_code,
            "repository_validation_authoritative": False,
            "repository_validation_retry_count": repository_validation.retry_count,
            "execution_provider": "codex_cli",
            "execution_environment_fingerprint": fingerprint,
            "provider_identity_path": cli_relative_to_run(
                agent_directory / "provider.json", context
            ),
            "normalized_result_path": cli_relative_to_run(
                agent_directory / "normalized-result.json", context
            ),
            "normalized_events_path": cli_relative_to_run(
                normalized_events_path, context
            ),
            "debug_trace_path": cli_relative_to_run(verifier_trace_directory, context),
            "process_result_path": cli_relative_to_run(
                agent_directory / "process-result.json", context
            ),
        }
        telemetry = {
            "provider": "codex",
            "model": self.driver.system.model,
            "agent_system_id": self.driver.system.id,
            "exact_version_output": self.probe.exact_version_output,
            "thread_id": parsed_events.thread_id,
            "input_tokens": parsed_events.input_tokens,
            "output_tokens": parsed_events.output_tokens,
            "token_accounting_status": (
                "complete"
                if parsed_events.input_tokens is not None
                and parsed_events.output_tokens is not None
                else "unknown"
            ),
            "cost_usd": None,
            "cost_accounting_status": "unknown",
            "process": {
                "state": process.infrastructure_state,
                "exit_code": process.exit_code,
                "timed_out": process.timed_out,
                "cancelled": process.cancelled,
                "cleanup_status": process.cleanup_status,
            },
            "resolved_trace_dir": str(verifier_trace_directory),
            "agent_report": final_result.model_dump(mode="json")
            if final_result
            else None,
        }
        dependency_error = (
            DependencyFailure(
                code=failure.value,
                message=redact_message(
                    final_error
                    or (
                        process.failures[0].message
                        if process.failures
                        else failure.value
                    )
                ),
                details={
                    "infrastructure_failure": infrastructure_failure,
                    "process_state": process.infrastructure_state,
                    "partial_patch_preserved": bool(patch_bytes.strip()),
                },
            )
            if failure is not None
            else None
        )
        return AttemptResult(
            runner_name=f"codex_cli:{self.driver.system.id}",
            status=status,
            worktree_path=str(worktree),
            patch=patch if patch.strip() else None,
            exit_code=process.exit_code,
            model=self.driver.system.model,
            stdout=stdout_tail,
            stderr=stderr_tail,
            runner_telemetry=redact_data(telemetry, secrets=secrets),
            trace={
                "provider": "codex",
                "raw_events": cli_relative_to_run(
                    agent_directory / "codex-events.jsonl", context
                ),
                "normalized_events": cli_relative_to_run(
                    normalized_events_path, context
                ),
            },
            trace_path=cli_relative_to_run(normalized_events_path, context),
            telemetry_path=cli_relative_to_run(
                agent_directory / "process-result.json", context
            ),
            runtime_events=parsed_events.runtime_events,
            duration_ms=process.duration_ms,
            duration_accounting_status="complete",
            input_tokens=parsed_events.input_tokens,
            output_tokens=parsed_events.output_tokens,
            token_accounting_status=(
                "complete"
                if parsed_events.input_tokens is not None
                and parsed_events.output_tokens is not None
                else "unknown"
            ),
            cost_usd=None,
            cost_accounting_status="unknown",
            error=dependency_error,
            metadata=redact_data(metadata, secrets=secrets),
        )


__all__ = ["CodexCliAttemptAdapter"]
