"""Villani Code attempt adapter using canonical isolation and artifact paths."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from villani_ops.core.backend import Backend
from villani_ops.runners.base import RunnerAdapter, RunnerContext, RunnerResult
from villani_ops.runners.villani_code import VillaniCodeAdapter
from villani_ops.verifier.service import resolve_verifier_debug_dir

from ..durable_io import write_json_atomic
from ..costs import actual_attempt_cost
from ..event_writer import redact_data
from ..interfaces import AttemptContext, AttemptResult, DependencyFailure
from ..protocol import AttemptSnapshot, FailureDetail
from .git_isolation import GitIsolationAdapter
from .runtime_event_translation import (
    preserve_raw_trace,
    sanitize_artifact_tree,
    translate_runtime_events,
)


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _secret_values(backend: Backend, env: Mapping[str, str]) -> tuple[str, ...]:
    values: list[str] = []
    key = backend.resolved_api_key()
    if key:
        values.append(key)
    for name, value in env.items():
        normalized = name.lower()
        if any(word in normalized for word in ("key", "token", "secret", "password", "authorization")):
            if value:
                values.append(str(value))
    return tuple(dict.fromkeys(values))


def _token_accounting(result: RunnerResult) -> tuple[int | None, int | None, str]:
    status = str(result.token_accounting_status or "missing").lower()
    if status == "missing":
        return None, None, "unknown"
    mapped = "complete" if status == "verified" else "partial"
    return result.input_tokens, result.output_tokens, mapped


class VillaniCodeAttemptAdapter:
    """Run one Villani Code protocol adapter inside a Git-baselined copy."""

    def __init__(
        self,
        *,
        backends: Mapping[str, Backend] | None = None,
        runner: RunnerAdapter | None = None,
        isolation: GitIsolationAdapter | None = None,
    ) -> None:
        self._backends = dict(backends or {})
        self._runner = runner or VillaniCodeAdapter()
        self._isolation = isolation or GitIsolationAdapter()

    def _backend(self, context: AttemptContext) -> Backend:
        if context.backend_name in self._backends:
            return self._backends[context.backend_name]
        configured = context.policy_configuration.get("backends")
        if isinstance(configured, Mapping):
            raw = configured.get(context.backend_name)
            if isinstance(raw, Mapping):
                return Backend.model_validate({"name": context.backend_name, **dict(raw)})
        raise ValueError(f"no Villani Code backend config for {context.backend_name}")

    def run(self, attempt_context: AttemptContext) -> AttemptResult:
        attempt_dir = Path(attempt_context.attempt_directory).resolve()
        run_dir = Path(attempt_context.run_directory).resolve()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        backend = self._backend(attempt_context)
        isolated = self._isolation.create(attempt_context)

        configured_env = attempt_context.policy_configuration.get("runner_env")
        runner_env = dict(backend.env)
        if isinstance(configured_env, Mapping):
            runner_env.update({str(key): str(value) for key, value in configured_env.items()})
        runner_env.update(
            {
                "VILLANI_RUN_ID": attempt_context.run_id,
                "VILLANI_TRACE_ID": attempt_context.trace_id,
                "VILLANI_ATTEMPT_ID": attempt_context.attempt_id,
            }
        )
        secrets = _secret_values(backend, runner_env)
        timeout = int(
            attempt_context.policy_configuration.get("attempt_timeout_seconds")
            or backend.timeout_seconds
            or 1200
        )
        runner_context = RunnerContext(
            attempt_id=attempt_context.attempt_id,
            repo_path=str(isolated.copied.worktree_path),
            task_instruction=attempt_context.task,
            success_criteria=attempt_context.success_criteria,
            backend=backend,
            timeout_seconds=timeout,
            run_dir=str(attempt_dir),
            env=runner_env,
        )

        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        runner_exception: Exception | None = None
        try:
            runner_result = self._runner.run(runner_context)
        except Exception as error:
            runner_exception = error
            runner_result = RunnerResult(exit_code=1, stderr=str(error))
        measured_duration = max(int((time.monotonic() - started_monotonic) * 1000), 0)
        completed_at = datetime.now(timezone.utc)

        debug_root = (
            Path(runner_result.debug_artifact_dir).resolve()
            if runner_result.debug_artifact_dir
            else None
        )
        runner_trace = (
            Path(runner_result.resolved_trace_dir).resolve()
            if runner_result.resolved_trace_dir
            else None
        )
        resolved = resolve_verifier_debug_dir(debug_root, runner_trace)
        trace_source = resolved or debug_root or runner_trace
        raw_trace_path: Path | None = None
        runtime_events = ()
        if trace_source is not None and trace_source.exists():
            raw_trace_path = preserve_raw_trace(
                trace_source,
                attempt_dir / "trace" / "raw",
                secrets=secrets,
            )
            runtime_events = translate_runtime_events(
                raw_trace_path, secrets=secrets
            )
        if (
            debug_root is not None
            and debug_root.exists()
            and debug_root != attempt_dir
            and raw_trace_path is not None
            and debug_root != raw_trace_path
        ):
            shutil.rmtree(debug_root, ignore_errors=True)

        sanitize_artifact_tree(attempt_dir, secrets=secrets)
        capture = self._isolation.capture(isolated)
        patch_path = isolated.patch_path
        patch = patch_path.read_text(encoding="utf-8", errors="replace")
        stdout = str(redact_data(runner_result.stdout or "", secrets=secrets))
        stderr = str(redact_data(runner_result.stderr or "", secrets=secrets))
        (attempt_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(stderr, encoding="utf-8")

        input_tokens, output_tokens, token_status = _token_accounting(runner_result)
        duration_ms = runner_result.duration_ms
        if duration_ms is None:
            duration_ms = measured_duration
        cost_breakdown = actual_attempt_cost(
            backend,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=duration_ms / 1000,
            started=True,
        )
        cost = cost_breakdown.total
        cost_status = cost_breakdown.accounting_status
        failure: DependencyFailure | None = None
        failure_classification: str | None = None
        if runner_exception is not None:
            failure_classification = "infrastructure_failure"
            failure = DependencyFailure(
                code="runner_exception",
                message=str(redact_data(str(runner_exception), secrets=secrets)),
                details={"exception_class": runner_exception.__class__.__name__},
            )
        elif runner_result.exit_code == 127:
            failure_classification = "infrastructure_failure"
            failure = DependencyFailure(
                code="runner_command_not_found",
                message="Villani Code runner command was not found.",
                details={"exit_code": 127},
            )
        elif runner_result.exit_code != 0:
            failure_classification = "runner_failure"
            failure = DependencyFailure(
                code="runner_nonzero_exit",
                message=f"Villani Code runner exited with {runner_result.exit_code}.",
                details={"exit_code": runner_result.exit_code},
            )
        if capture.failure_reason:
            failure_classification = failure_classification or "patch_capture_failure"
            failure = failure or DependencyFailure(
                code="patch_capture_failure",
                message=str(redact_data(capture.failure_reason, secrets=secrets)),
            )

        status = (
            "completed"
            if runner_result.exit_code == 0 and not capture.failure_reason
            else "failed"
        )
        telemetry = redact_data(
            {
                **dict(runner_result.telemetry or {}),
                "backend": {
                    "name": backend.name,
                    "provider": backend.provider,
                    "model": backend.model,
                },
                "model_requests": runner_result.model_requests,
                "model_failures": runner_result.model_failures,
                "usage_records": runner_result.usage_records,
                "runner_events": runner_result.events,
                "total_tokens": runner_result.total_tokens,
                "total_tool_calls": runner_result.total_tool_calls,
                "tool_calls_by_name": runner_result.tool_calls_by_name,
                "total_file_reads": runner_result.total_file_reads,
                "total_file_writes": runner_result.total_file_writes,
                "commands_executed": runner_result.commands_executed,
                "commands_failed": runner_result.commands_failed,
                "first_substantive_file_read_tool_index": runner_result.first_substantive_file_read_tool_index,
                "first_substantive_file_read_seconds": runner_result.first_substantive_file_read_seconds,
                "first_file_mutation_tool_index": runner_result.first_file_mutation_tool_index,
                "first_file_mutation_seconds": runner_result.first_file_mutation_seconds,
                "first_command_tool_index": runner_result.first_command_tool_index,
                "first_command_seconds": runner_result.first_command_seconds,
                "token_accounting_status": runner_result.token_accounting_status,
                "token_accounting_warnings": runner_result.token_accounting_warnings,
                "provider_reported_total_cost": runner_result.total_cost,
                "cost_breakdown": cost_breakdown.as_dict(),
                "translated_runtime_event_count": len(runtime_events),
            },
            secrets=secrets,
        )
        telemetry_path = attempt_dir / "runner_telemetry.json"
        write_json_atomic(telemetry_path, telemetry)
        trace_relative = (
            _relative(raw_trace_path, run_dir) if raw_trace_path is not None else None
        )
        telemetry_relative = _relative(telemetry_path, run_dir)
        worktree_metadata = {
            **isolated.metadata,
            "source_repository": isolated.metadata["source_repository"],
        }
        metadata = {
            "worktree": worktree_metadata,
            "changed_files": capture.changed_files,
            "patch_capture": capture.model_dump(mode="json"),
            "failure_classification": failure_classification,
            "cost_breakdown": cost_breakdown.as_dict(),
            "debug_trace_path": trace_relative,
            "runner_metrics": {
                "model_requests": runner_result.model_requests,
                "model_failures": runner_result.model_failures,
                "total_tool_calls": runner_result.total_tool_calls,
                "tool_calls_by_name": runner_result.tool_calls_by_name,
                "total_file_reads": runner_result.total_file_reads,
                "total_file_writes": runner_result.total_file_writes,
                "commands_executed": runner_result.commands_executed,
                "commands_failed": runner_result.commands_failed,
            },
        }

        encoded_patch = patch.encode("utf-8")
        protocol_error = (
            FailureDetail(
                code=failure.code,
                message=failure.message,
                details=dict(failure.details),
            )
            if failure is not None
            else None
        )
        adapter_snapshot = AttemptSnapshot(
            schema_version="villani.attempt.v1",
            attempt_id=attempt_context.attempt_id,
            run_id=attempt_context.run_id,
            trace_id=attempt_context.trace_id,
            ordinal=attempt_context.ordinal,
            backend_name=attempt_context.backend_name,
            runner_name=getattr(self._runner, "name", "villani-code"),
            model=attempt_context.model or backend.model,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            worktree_path=str(isolated.copied.worktree_path),
            patch_path=f"attempts/{attempt_context.attempt_id}/patch.diff",
            patch_sha256=hashlib.sha256(encoded_patch).hexdigest(),
            patch_bytes=len(encoded_patch),
            stdout_path=f"attempts/{attempt_context.attempt_id}/stdout.log",
            stderr_path=f"attempts/{attempt_context.attempt_id}/stderr.log",
            runner_telemetry_path=telemetry_relative,
            trace_path=trace_relative,
            exit_code=runner_result.exit_code,
            duration_ms=duration_ms,
            duration_accounting_status="complete",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_accounting_status=token_status,
            cost_usd=cost,
            cost_accounting_status=cost_status,
            error=protocol_error,
            metadata=metadata,
        )
        write_json_atomic(
            attempt_dir / "attempt.json", adapter_snapshot.model_dump(mode="json")
        )
        write_json_atomic(attempt_dir / "worktree.json", worktree_metadata)

        return AttemptResult(
            runner_name=getattr(self._runner, "name", "villani-code"),
            status=status,
            worktree_path=str(isolated.copied.worktree_path),
            patch=patch,
            exit_code=runner_result.exit_code,
            model=attempt_context.model or backend.model,
            stdout=stdout,
            stderr=stderr,
            runner_telemetry=telemetry,
            trace={
                "raw_trace_path": trace_relative,
                "translated_runtime_event_count": len(runtime_events),
            },
            trace_path=trace_relative,
            telemetry_path=telemetry_relative,
            runtime_events=runtime_events,
            duration_ms=duration_ms,
            duration_accounting_status="complete",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_accounting_status=token_status,
            cost_usd=cost,
            cost_accounting_status=cost_status,
            error=failure,
            metadata=metadata,
        )
