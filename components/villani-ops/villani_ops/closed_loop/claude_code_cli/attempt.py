"""Claude Code implementation of the existing coding AttemptRunner port."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from villani_ops.closed_loop.adapters.git_isolation import GitIsolationAdapter
from villani_ops.closed_loop.cli_coding.evidence import (
    collect_candidate_evidence,
    prepare_candidate,
    relative_to_run,
    sanitize_and_parse_final,
    write_normalized_events,
)
from villani_ops.closed_loop.cli_runtime import (
    CliCancellationHandle,
    CliCancellationOrigin,
    CliOutputTail,
    CliProcessResult,
)
from villani_ops.closed_loop.durable_io import write_json_atomic
from villani_ops.closed_loop.event_writer import redact_data, redact_message
from villani_ops.closed_loop.interfaces import (
    AttemptContext,
    AttemptResult,
    DependencyFailure,
)
from villani_ops.execution_environment.secrets import registered_secret_values

from .driver import ClaudeCodeCliDriver, run_coroutine_sync
from .events import (
    ClaudeEventParseError,
    ParsedClaudeEvents,
    parse_claude_events,
    sanitize_claude_event_artifacts,
)
from .models import ClaudeCoderResult, ClaudeFailure, ClaudeProbeResult
from .prompt import build_claude_coding_prompt


def _empty_parsed_events() -> ParsedClaudeEvents:
    return ParsedClaudeEvents(
        runtime_events=(),
        normalized_rows=(),
        session_id=None,
        input_tokens=None,
        output_tokens=None,
        total_cost_usd=None,
        reported_model=None,
        system_metadata={},
        final_result=None,
        structured_output=None,
        structured_output_parse_error=None,
    )


class ClaudeCodeCliAttemptAdapter:
    """Run one complete Claude Code coding loop in an isolated Git worktree."""

    def __init__(
        self,
        driver: ClaudeCodeCliDriver,
        *,
        probe: ClaudeProbeResult | None = None,
        isolation: GitIsolationAdapter | None = None,
    ) -> None:
        self.driver = driver
        self.probe = probe or driver.probe()
        self.isolation = isolation or GitIsolationAdapter()

    async def _supervise(
        self,
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
            else ClaudeFailure.UNSUPPORTED_VERSION
        )
        message = "; ".join(self.probe.messages) or "Claude Code doctor did not pass"
        return AttemptResult(
            runner_name=f"claude_code_cli:{self.driver.system.id}",
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

    def run(self, attempt_context: AttemptContext) -> AttemptResult:  # noqa: C901
        if not self.probe.ready:
            return self._probe_failure(attempt_context)
        context = attempt_context
        attempt_directory = Path(context.attempt_directory).resolve()
        agent_directory = attempt_directory / "agent"
        repository_directory = attempt_directory / "repository"
        agent_directory.mkdir(parents=True, exist_ok=True)
        repository_directory.mkdir(parents=True, exist_ok=True)
        secrets = tuple(registered_secret_values())
        prepared = prepare_candidate(
            context=context,
            isolation=self.isolation,
            repository_directory=repository_directory,
            secrets=secrets,
            schema_prefix="claude_code",
        )
        isolated = prepared.isolated
        worktree = prepared.worktree
        if prepared.external_symlinks:
            status_document = {
                "schema_version": "villani.claude_code_repository_status.v1",
                "status": "rejected_before_execution",
                "entries": [],
                "forbidden_paths_touched": [],
                "unsafe_paths": [],
                "untracked_files": [],
                "external_symlinks": list(prepared.external_symlinks),
                "path_violation": True,
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
                runner_name=f"claude_code_cli:{self.driver.system.id}",
                status="failed",
                worktree_path=str(worktree),
                patch=None,
                exit_code=None,
                model=self.driver.system.model,
                error=DependencyFailure(
                    code=ClaudeFailure.PATH_VIOLATION.value,
                    message=(
                        "isolated worktree contains a symlink resolving outside its scope"
                    ),
                    details={
                        "paths": list(prepared.external_symlinks),
                        "infrastructure_failure": True,
                    },
                ),
                metadata={
                    "failure_category": ClaudeFailure.PATH_VIOLATION.value,
                    "infrastructure_failure": True,
                    "changed_files": [],
                    "path_violation": True,
                    "agent_system_id": self.driver.system.id,
                    "agent_role": "coding",
                    "candidate_execution_worktree_paths": [str(worktree)],
                },
            )

        prompt = build_claude_coding_prompt(
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
        output_schema_path.write_bytes(
            (SCHEMA_ROOT / "claude-coder-result.schema.json").read_bytes()
        )
        controlled_settings_path: Path | None = None
        controlled_mcp_path: Path | None = None
        if self.driver.system.instruction_policy == "villani_controlled":
            controlled_settings_path = agent_directory / "controlled-settings.json"
            controlled_mcp_path = agent_directory / "controlled-mcp.json"
            write_json_atomic(
                controlled_settings_path,
                {
                    "permissions": {"defaultMode": "acceptEdits"},
                    "hooks": {},
                    "enabledPlugins": {},
                    "autoMemoryEnabled": False,
                },
            )
            write_json_atomic(controlled_mcp_path, {"mcpServers": {}})

        initial_identity = self.driver.provider_identity(self.probe)
        write_json_atomic(agent_directory / "provider.json", initial_identity)
        invocation = self.driver.build_invocation(
            probe=self.probe,
            worktree=worktree,
            agent_directory=agent_directory,
            prompt_bytes=prompt.bytes,
            prompt_reference=relative_to_run(prompt_path, context),
            prompt_sha256=prompt.sha256,
            output_schema_path=output_schema_path,
            run_id=context.run_id,
            attempt_id=context.attempt_id,
            baseline_sha256=context.baseline_sha256,
            controlled_settings_path=controlled_settings_path,
            controlled_mcp_path=controlled_mcp_path,
        )
        process = run_coroutine_sync(
            self._supervise(invocation, context.cancellation_event)
        )

        raw_events_path = agent_directory / "claude-events.jsonl"
        sanitize_claude_event_artifacts(
            (
                raw_events_path,
                agent_directory / "stdout.log",
                agent_directory / "stderr.log",
            ),
            secrets=secrets,
        )
        event_error: str | None = None
        try:
            parsed_events = parse_claude_events(
                raw_events_path,
                started_at=process.started_at,
                run_id=context.run_id,
                attempt_id=context.attempt_id,
                worktree_path=str(worktree),
                baseline_sha256=context.baseline_sha256,
                secrets=secrets,
            )
        except (OSError, UnicodeDecodeError, ClaudeEventParseError) as error:
            parsed_events = _empty_parsed_events()
            event_error = str(error)
        normalized_events_path = agent_directory / "normalized-events.jsonl"
        write_normalized_events(normalized_events_path, parsed_events)

        final_output_path = agent_directory / "final-output.json"
        write_json_atomic(
            final_output_path,
            parsed_events.structured_output,
        )
        final_result, final_error = sanitize_and_parse_final(
            final_output_path,
            model=ClaudeCoderResult,
            maximum_bytes=self.driver.integer_option(
                self.driver.system.provider_options,
                "maximum_final_output_bytes",
                1024 * 1024,
            ),
            secrets=secrets,
        )
        if parsed_events.structured_output_parse_error is not None:
            final_error = parsed_events.structured_output_parse_error

        provider_identity = self.driver.provider_identity(self.probe, parsed_events)
        write_json_atomic(agent_directory / "provider.json", provider_identity)
        collected = collect_candidate_evidence(
            context=context,
            prepared=prepared,
            repository_directory=repository_directory,
            process=process,
            parsed=parsed_events,
            provider_identity=provider_identity.model_dump(mode="json"),
            execution_provider="claude_code_cli",
            environment_policy=self.driver.system.environment_policy,
            secrets=secrets,
            schema_prefix="claude_code",
        )

        try:
            tail = CliOutputTail.model_validate_json(
                (agent_directory / "output-tail.json").read_text(encoding="utf-8")
            )
            stderr_tail = str(redact_data(tail.stderr, secrets=secrets))
            stdout_replacements = tail.stdout_decode_replacements
            stderr_replacements = tail.stderr_decode_replacements
        except (OSError, ValidationError):
            stderr_tail = ""
            stdout_replacements = False
            stderr_replacements = False
        stdout_tail = "[structured Claude stream stored in claude-events.jsonl]"
        write_json_atomic(
            agent_directory / "output-tail.json",
            {
                "schema_version": "villani.cli_output_tail.v1",
                "stdout": stdout_tail,
                "stderr": stderr_tail,
                "maximum_tail_bytes": invocation.output_limits.maximum_tail_bytes,
                "utf8_policy": "strict",
                "stdout_decode_replacements": stdout_replacements,
                "stderr_decode_replacements": stderr_replacements,
            },
        )
        diagnostic_text = "\n".join(
            item
            for item in (
                stderr_tail,
                json.dumps(parsed_events.final_result, ensure_ascii=False)
                if parsed_events.final_result is not None
                else "",
                event_error or "",
                final_error or "",
            )
            if item
        )
        failure = self.driver.classify_failure(
            process,
            diagnostic_text=diagnostic_text,
            stream_error=event_error,
            final_output_error=final_error,
            final_result_present=parsed_events.final_result is not None,
            path_violation=collected.path_violation,
            has_patch=bool(collected.patch_bytes.strip()),
        )
        if collected.changed_document.get("capture_failure") and failure is None:
            failure = ClaudeFailure.PROCESS_CRASH
        status = (
            "cancelled"
            if failure == ClaudeFailure.PROCESS_CANCELLATION
            else "failed"
            if failure is not None
            else "completed"
        )
        normalized_result = {
            "schema_version": "villani.claude_code_normalized_result.v1",
            "run_id": context.run_id,
            "attempt_id": context.attempt_id,
            "status": status,
            "failure": failure.value if failure else None,
            "agent_report": (
                final_result.model_dump(mode="json") if final_result else None
            ),
            "agent_report_error": final_error,
            "patch": {
                "path": relative_to_run(collected.patch_path, context),
                "sha256": collected.changed_document["candidate_digest"],
                "bytes": len(collected.patch_bytes),
                "changed_files": list(collected.changed_files),
                "source": "git",
            },
            "provider_identity": relative_to_run(
                agent_directory / "provider.json", context
            ),
            "session_id": parsed_events.session_id,
        }
        write_json_atomic(
            agent_directory / "normalized-result.json",
            redact_data(normalized_result, secrets=secrets),
        )

        infrastructure_failure = failure not in {
            None,
            ClaudeFailure.COMPLETED_NO_PATCH,
        }
        failure_classification = (
            "infrastructure_failure"
            if infrastructure_failure
            else "coding_failure"
            if failure == ClaudeFailure.COMPLETED_NO_PATCH
            else None
        )
        quality = collected.quality
        repository_validation = collected.repository_validation
        quality_document = quality.model_dump(mode="json")
        metadata = {
            "failure_category": failure.value if failure else None,
            "failure_classification": failure_classification,
            "infrastructure_failure": infrastructure_failure,
            "agent_system_id": self.driver.system.id,
            "agent_role": "coding",
            "claude_code_driver": "print_stream_json",
            "claude_code_session_id": parsed_events.session_id,
            "instruction_policy": self.driver.system.instruction_policy,
            "permission_profile": self.driver.system.permission_profile,
            "no_session_persistence": True,
            "changed_files": list(collected.changed_files),
            "has_non_empty_patch": bool(collected.patch_bytes.strip()),
            "forbidden_paths_touched": list(collected.forbidden_paths),
            "path_violation": collected.path_violation,
            "baseline_digest": prepared.baseline_document["baseline_digest"],
            "candidate_digest": collected.changed_document["candidate_digest"],
            "candidate_bundle_path": relative_to_run(
                attempt_directory / "candidate" / "candidate.json", context
            ),
            "candidate_bundle_schema_version": (
                collected.candidate_manifest.schema_version
            ),
            "candidate_patch_quality": quality_document,
            "candidate_patch_quality_status": quality.status,
            "candidate_patch_quality_path": relative_to_run(
                attempt_directory / "candidate-patch-quality.json", context
            ),
            "candidate_quality_report": quality_document,
            "relevant_diff_ratio": quality.relevant_diff_ratio,
            "line_ending_only_lines": quality.line_ending_only_lines,
            "candidate_execution_worktree_paths": [str(worktree)],
            "worktree": isolated.metadata,
            "repository_validation_path": relative_to_run(
                attempt_directory / "repository-validation.json", context
            ),
            "repository_validation": repository_validation.model_dump(mode="json"),
            "repository_validation_status": repository_validation.status,
            "repository_validation_failure_code": (repository_validation.failure_code),
            "repository_validation_authoritative": False,
            "repository_validation_retry_count": repository_validation.retry_count,
            "execution_provider": "claude_code_cli",
            "execution_environment_fingerprint": collected.fingerprint,
            "provider_identity_path": relative_to_run(
                agent_directory / "provider.json", context
            ),
            "normalized_result_path": relative_to_run(
                agent_directory / "normalized-result.json", context
            ),
            "normalized_events_path": relative_to_run(normalized_events_path, context),
            "debug_trace_path": relative_to_run(
                collected.verifier_trace_directory, context
            ),
            "process_result_path": relative_to_run(
                agent_directory / "process-result.json", context
            ),
        }
        token_complete = (
            parsed_events.input_tokens is not None
            and parsed_events.output_tokens is not None
        )
        cost_complete = parsed_events.total_cost_usd is not None
        telemetry = {
            "provider": "anthropic",
            "model": parsed_events.reported_model or self.driver.system.model,
            "configured_model": self.driver.system.model,
            "agent_system_id": self.driver.system.id,
            "exact_version_output": self.probe.exact_version_output,
            "session_id": parsed_events.session_id,
            "input_tokens": parsed_events.input_tokens,
            "output_tokens": parsed_events.output_tokens,
            "token_accounting_status": "complete" if token_complete else "unknown",
            "cost_usd": parsed_events.total_cost_usd,
            "cost_accounting_status": "complete" if cost_complete else "unknown",
            "cost_source": (
                "claude_code_authoritative_total_cost_usd" if cost_complete else None
            ),
            "process": {
                "state": process.infrastructure_state,
                "exit_code": process.exit_code,
                "timed_out": process.timed_out,
                "cancelled": process.cancelled,
                "cleanup_status": process.cleanup_status,
            },
            "resolved_trace_dir": str(collected.verifier_trace_directory),
            "agent_report": (
                final_result.model_dump(mode="json") if final_result else None
            ),
        }
        dependency_error = (
            DependencyFailure(
                code=failure.value,
                message=redact_message(
                    event_error
                    or final_error
                    or (
                        process.failures[0].message
                        if process.failures
                        else failure.value
                    )
                ),
                details={
                    "infrastructure_failure": infrastructure_failure,
                    "process_state": process.infrastructure_state,
                    "partial_patch_preserved": bool(collected.patch_bytes.strip()),
                },
            )
            if failure is not None
            else None
        )
        return AttemptResult(
            runner_name=f"claude_code_cli:{self.driver.system.id}",
            status=status,
            worktree_path=str(worktree),
            patch=collected.patch if collected.patch.strip() else None,
            exit_code=process.exit_code,
            model=parsed_events.reported_model or self.driver.system.model,
            stdout=stdout_tail,
            stderr=stderr_tail,
            runner_telemetry=redact_data(telemetry, secrets=secrets),
            trace={
                "provider": "claude_code",
                "raw_events": relative_to_run(raw_events_path, context),
                "normalized_events": relative_to_run(normalized_events_path, context),
            },
            trace_path=relative_to_run(normalized_events_path, context),
            telemetry_path=relative_to_run(
                agent_directory / "process-result.json", context
            ),
            runtime_events=parsed_events.runtime_events,
            duration_ms=process.duration_ms,
            duration_accounting_status="complete",
            input_tokens=parsed_events.input_tokens,
            output_tokens=parsed_events.output_tokens,
            token_accounting_status="complete" if token_complete else "unknown",
            cost_usd=parsed_events.total_cost_usd,
            cost_accounting_status="complete" if cost_complete else "unknown",
            error=dependency_error,
            metadata=redact_data(metadata, secrets=secrets),
        )


__all__ = ["ClaudeCodeCliAttemptAdapter"]
