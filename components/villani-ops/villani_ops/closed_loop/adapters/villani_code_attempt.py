"""Villani Code attempt adapter using canonical isolation and artifact paths."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from villani_ops.core.backend import Backend
from villani_ops.runners.base import (
    CandidateExecutionAcknowledgement,
    RunnerAdapter,
    RunnerContext,
    RunnerResult,
)
from villani_ops.runners.villani_code import (
    VillaniCodeAdapter,
    provider_for_villani_code_cli,
)
from villani_ops.verifier.service import resolve_verifier_debug_dir
from villani_ops.execution_environment import (
    CandidateCommandResult,
    RepositoryValidationCommandResult,
    RepositoryValidationFailureCode,
    RepositoryValidationReport,
    provider_from_configuration,
)
from villani_ops.execution_environment.models import (
    CandidatePatchQuality,
    FocusedProbeFailureCode,
)

from ..durable_io import write_json_atomic
from ..costs import actual_attempt_cost, provider_reported_attempt_cost
from ..event_writer import redact_data
from ..interfaces import AttemptContext, AttemptResult, DependencyFailure, RuntimeEvent
from ..protocol import AccountingStatus, AttemptSnapshot, FailureDetail
from ..protocol_v2 import ResourceV2
from ..candidate_bundle import (
    apply_candidate_bundle,
    candidate_state_sha256,
    read_patch_text,
    write_candidate_bundle,
)
from ..candidate_quality import (
    assess_candidate_patch_quality,
    prepare_candidate_worktree,
)
from .git_isolation import GitIsolationAdapter
from .runtime_event_translation import (
    preserve_raw_trace,
    sanitize_artifact_tree,
    translate_runtime_events,
)
from ..failure_classification import classify_runner_failure
from ..focused_probes import (
    execute_focused_probes as execute_focused_probe_requests,
    focused_probe_runtime_events,
    invalidate_focused_probe_report,
    load_focused_probe_report,
)
from ..plugins.builtins import AGENT_RUNNER_MANIFEST, EXECUTION_PROVIDER_MANIFEST
from ..repository_validation import (
    execute_repository_validation,
    invalidate_repository_validation,
    load_repository_validation_report,
    repository_validation_runtime_events,
)
from ..verification_evidence import (
    FocusedProbeReport,
    FocusedProbeRequest,
    FocusedProbeResult,
)
from ..validation_coverage import (
    build_validation_coverage,
    legacy_validation_coverage,
)
from villani_ops.providers import validate_runtime_credentials


_EFFICIENCY_TELEMETRY_KEYS = (
    "time_to_first_relevant_file",
    "tool_calls_to_first_relevant_file",
    "time_to_first_relevant_patch",
    "tool_calls_to_first_relevant_patch",
    "tokens_to_first_relevant_patch",
    "unique_files_read",
    "unique_relevant_files_read",
    "duplicate_file_reads",
    "duplicate_searches",
    "repeated_commands",
    "repeated_command_failures",
    "tokens_after_last_relevant_progress",
    "turns_after_last_relevant_progress",
    "relevant_patch_revisions",
    "validation_improvement_count",
    "duplicate_tool_results",
    "context_items_added",
    "context_items_reused",
    "context_items_compacted",
    "tokens_removed_by_compaction",
    "estimated_tokens_before_projection",
    "estimated_tokens_after_projection",
    "unique_command_failures",
    "failed_command_ratio",
    "commands_retried_without_state_change",
)


def _runner_efficiency_telemetry(
    telemetry: Mapping[str, Any],
) -> dict[str, Any]:
    raw_summary = telemetry.get("raw_summary")
    summary = raw_summary if isinstance(raw_summary, Mapping) else {}
    output: dict[str, Any] = {}
    for key in _EFFICIENCY_TELEMETRY_KEYS:
        if key in telemetry:
            output[key] = telemetry[key]
        elif key in summary:
            output[key] = summary[key]
    return output


def _runner_relevant_paths(telemetry: Mapping[str, Any]) -> list[str]:
    raw_summary = telemetry.get("raw_summary")
    summary = raw_summary if isinstance(raw_summary, Mapping) else {}
    values = (
        telemetry.get("relevant_files_read") or summary.get("relevant_files_read") or []
    )
    return [str(value) for value in values] if isinstance(values, list) else []


def _failure_from_runner_output(
    exit_code: int | None, stdout: str, stderr: str
) -> tuple[str, str]:
    """Classify invocation failures without confusing unavailable backends with code failures."""

    code = classify_runner_failure(exit_code, stdout, stderr)
    return (
        code,
        "infrastructure_failure" if code != "runner_nonzero_exit" else "runner_failure",
    )


def _failure_snippets(stdout: str, stderr: str) -> dict[str, str]:
    limit = 2_000
    return {
        "stdout_snippet": stdout[-limit:],
        "stderr_snippet": stderr[-limit:],
    }


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _secret_values(backend: Backend, env: Mapping[str, str]) -> tuple[str, ...]:
    values: list[str] = []
    key = backend.resolved_api_key()
    if key:
        values.append(key)
    for name, value in env.items():
        normalized = name.lower()
        if any(
            word in normalized
            for word in ("key", "token", "secret", "password", "authorization")
        ):
            if value:
                values.append(str(value))
    return tuple(dict.fromkeys(values))


def _credential_free_environment(
    values: Mapping[str, str],
    *,
    denied_names: set[str] | None = None,
    denied_values: set[str] | None = None,
) -> dict[str, str]:
    """Preserve the candidate runtime while removing model credentials."""

    explicit_names = {
        str(name).casefold().replace("-", "_")
        for name in (denied_names or set())
        if name
    }
    explicit_values = {str(value) for value in (denied_values or set()) if value}

    def credential_name(name: str) -> bool:
        normalized = name.casefold().replace("-", "_")
        return bool(
            normalized in explicit_names
            or normalized
            in {
                "authorization",
                "password",
                "passwd",
                "secret",
                "token",
            }
            or normalized.endswith(
                (
                    "_api_key",
                    "_access_key",
                    "_secret_key",
                    "_token",
                    "_password",
                    "_credential",
                    "_credentials",
                )
            )
        )

    return {
        str(name): str(value)
        for name, value in values.items()
        if not credential_name(str(name)) and str(value) not in explicit_values
    }


def _token_accounting(
    result: RunnerResult,
) -> tuple[int | None, int | None, AccountingStatus]:
    status = str(result.token_accounting_status or "missing").lower()
    if status == "missing":
        return None, None, "unknown"
    mapped: AccountingStatus = "complete" if status == "verified" else "partial"
    return result.input_tokens, result.output_tokens, mapped


class VillaniCodeAttemptAdapter:
    """Run one Villani Code protocol adapter inside a Git-baselined copy."""

    plugin_manifest = AGENT_RUNNER_MANIFEST
    additional_plugin_manifests = (EXECUTION_PROVIDER_MANIFEST,)

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
        self._environment_lock = threading.RLock()
        self._active_environments: dict[str, tuple[Any, Any]] = {}

    def _backend(self, context: AttemptContext) -> Backend:
        if context.backend_name in self._backends:
            return self._backends[context.backend_name]
        configured = context.policy_configuration.get("backends")
        if isinstance(configured, Mapping):
            raw = configured.get(context.backend_name)
            if isinstance(raw, Mapping):
                return Backend.model_validate(
                    {"name": context.backend_name, **dict(raw)}
                )
        raise ValueError(f"no Villani Code backend config for {context.backend_name}")

    def _remember_environment(
        self,
        attempt_id: str,
        provider: Any,
        prepared_environment: Any,
    ) -> None:
        with self._environment_lock:
            if attempt_id in self._active_environments:
                raise RuntimeError(
                    f"attempt {attempt_id!r} already owns a prepared environment"
                )
            self._active_environments[attempt_id] = (
                provider,
                prepared_environment,
            )

    def _release_environment(
        self,
        attempt_id: str,
        *,
        suppress_errors: bool = False,
    ) -> None:
        with self._environment_lock:
            active = self._active_environments.pop(attempt_id, None)
        if active is None:
            return
        provider, prepared_environment = active
        try:
            provider.cleanup(prepared_environment)
        except Exception:
            if not suppress_errors:
                raise

    def run(self, attempt_context: AttemptContext) -> AttemptResult:
        try:
            result = self._run(attempt_context)
        except BaseException:
            self._release_environment(
                attempt_context.attempt_id,
                suppress_errors=True,
            )
            raise
        self._release_environment(attempt_context.attempt_id)
        return result

    def _run(self, attempt_context: AttemptContext) -> AttemptResult:
        attempt_dir = Path(attempt_context.attempt_directory).resolve()
        run_dir = Path(attempt_context.run_directory).resolve()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        backend = self._backend(attempt_context)
        if not bool(getattr(self._runner, "uses_vendor_auth", False)):
            validate_runtime_credentials(backend)
        isolated = self._isolation.create(attempt_context)

        configured_env = attempt_context.policy_configuration.get("runner_env")
        source_environment = {**dict(os.environ), **dict(backend.env)}
        if isinstance(configured_env, Mapping):
            source_environment.update(
                {str(key): str(value) for key, value in configured_env.items()}
            )
        environment_provider = provider_from_configuration(
            attempt_context.policy_configuration,
            source_environment=source_environment,
            cache_root=Path(attempt_context.run_directory).parent.parent
            / "cache"
            / "execution-environments",
            selection=attempt_context.execution_provider
            or backend.execution_environment,
            pluginized=True,
        )
        prepared_environment = environment_provider.prepare(
            repository=Path(attempt_context.repository_path),
            worktree=isolated.copied.worktree_path,
        )
        try:
            self._remember_environment(
                attempt_context.attempt_id,
                environment_provider,
                prepared_environment,
            )
        except BaseException:
            environment_provider.cleanup(prepared_environment)
            raise
        runner_env = environment_provider.command_environment(prepared_environment)
        runner_env.update(
            {
                "VILLANI_RUN_ID": attempt_context.run_id,
                "VILLANI_TRACE_ID": attempt_context.trace_id,
                "VILLANI_ATTEMPT_ID": attempt_context.attempt_id,
                "VILLANI_CANDIDATE_BASELINE_SHA256": attempt_context.baseline_sha256
                or "",
                "VILLANI_CANDIDATE_DIMENSIONS": json.dumps(
                    dict(attempt_context.candidate_dimensions),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "VILLANI_EXECUTION_ENVIRONMENT_FINGERPRINT": (
                    prepared_environment.fingerprint
                ),
                "VILLANI_EXECUTION_PROVIDER": prepared_environment.provider,
            }
        )
        api_key = backend.resolved_api_key()
        injected_environment_names: list[str] = []
        if api_key:
            secret_name = (
                "ANTHROPIC_API_KEY"
                if provider_for_villani_code_cli(backend.provider) == "anthropic"
                else "OPENAI_API_KEY"
            )
            if prepared_environment.provider == "devcontainer":
                self._release_environment(attempt_context.attempt_id)
                raise RuntimeError(
                    "devcontainer cannot inject backend credentials through a selected-process-only boundary; use container or a credential-free local backend"
                )
            runner_env[secret_name] = api_key
            injected_environment_names.append(secret_name)
        prepared_environment.runtime_state["injected_environment_names"] = (
            injected_environment_names
        )
        controls: Mapping[str, Any] = {}
        try:
            validate_command = getattr(environment_provider, "validate_command", None)
            if callable(validate_command):
                validate_command(
                    prepared_environment, [backend.command_name or "villani-code"]
                )
            runner_controls = getattr(environment_provider, "runner_controls", None)
            if callable(runner_controls):
                returned_controls = runner_controls(prepared_environment)
                if isinstance(returned_controls, Mapping):
                    controls = returned_controls
        except BaseException:
            self._release_environment(
                attempt_context.attempt_id,
                suppress_errors=True,
            )
            raise
        prepared_environment.execution_environment_selection = (
            attempt_context.execution_provider
            or backend.execution_environment
            or prepared_environment.provider
        )
        prepared_environment.configuration_digest = hashlib.sha256(
            json.dumps(
                environment_provider.config.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        prepared_environment.command_prefix_digest = hashlib.sha256(
            json.dumps(
                list(controls.get("execution_prefix") or []),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
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
            inherit_parent_environment=False,
            execution_prefix=list(controls.get("execution_prefix") or []),
            workspace_limit_bytes=controls.get("workspace_limit_bytes"),
            cleanup_command=list(controls.get("cleanup_command") or []),
            secure_secret_injection=True,
            cancellation_event=attempt_context.cancellation_event,
            candidate_dimensions=dict(attempt_context.candidate_dimensions),
        )

        environment_report = prepared_environment.durable_report()
        write_json_atomic(
            attempt_dir / "execution_environment.json", environment_report
        )
        write_json_atomic(run_dir / "execution_environment.json", environment_report)
        write_json_atomic(
            run_dir / "preflight.json",
            {
                "schema_version": "villani.execution_preflight.v1",
                "repository": prepared_environment.inspection,
                "provider": environment_provider.capability_report(),
                "execution_environment_fingerprint": prepared_environment.fingerprint,
                "inferred_setup_executed": False,
            },
        )
        write_json_atomic(
            run_dir / "resource.json",
            ResourceV2(
                schema_version="villani.resource.v2",
                service_name="villani",
                service_version=None,
                deployment_environment="local",
                host_id=None,
                process_id=None,
                attributes={
                    "villani.execution_environment.provider": prepared_environment.provider,
                    "villani.execution_environment.fingerprint": prepared_environment.fingerprint,
                    "villani.execution_environment.preflight": "preflight.json",
                },
            ).model_dump(mode="json"),
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
        runtime_events: tuple[RuntimeEvent, ...] = ()
        if trace_source is not None and trace_source.exists():
            raw_trace_path = preserve_raw_trace(
                trace_source,
                attempt_dir / "trace" / "raw",
                secrets=secrets,
            )
            runtime_events = translate_runtime_events(
                raw_trace_path,
                secrets=secrets,
                run_id=attempt_context.run_id,
                attempt_id=attempt_context.attempt_id,
                worktree_path=str(isolated.copied.worktree_path),
                baseline_sha256=attempt_context.baseline_sha256,
            )
        structured_runtime_events: list[RuntimeEvent] = []
        for index, raw_event in enumerate(runner_result.runtime_events, 1):
            if not isinstance(raw_event, Mapping):
                continue
            raw_timestamp = raw_event.get("timestamp")
            try:
                timestamp = datetime.fromisoformat(
                    str(raw_timestamp).replace("Z", "+00:00")
                )
            except ValueError:
                timestamp = completed_at
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            structured_runtime_events.append(
                RuntimeEvent(
                    event_type=str(raw_event.get("event_type") or "warning"),
                    timestamp=timestamp.astimezone(timezone.utc),
                    payload=dict(
                        redact_data(
                            raw_event.get("payload")
                            if isinstance(raw_event.get("payload"), Mapping)
                            else {},
                            secrets=secrets,
                        )
                    ),
                    source_event_id=str(
                        raw_event.get("source_event_id") or f"structured-runner:{index}"
                    ),
                )
            )
        runtime_events = (*runtime_events, *structured_runtime_events)
        if (
            debug_root is not None
            and debug_root.exists()
            and debug_root != attempt_dir
            and raw_trace_path is not None
            and debug_root != raw_trace_path
        ):
            shutil.rmtree(debug_root, ignore_errors=True)

        sanitize_artifact_tree(attempt_dir, secrets=secrets)
        task_for_quality = "\n".join(
            value
            for value in (
                attempt_context.task,
                attempt_context.success_criteria,
            )
            if value
        )
        candidate_preparation = prepare_candidate_worktree(
            worktree=isolated.copied.worktree_path,
            task=task_for_quality,
        )
        candidate_quality = assess_candidate_patch_quality(
            worktree=isolated.copied.worktree_path,
            candidate_id=attempt_context.attempt_id,
            task=task_for_quality,
            preparation=candidate_preparation,
            relevant_paths=_runner_relevant_paths(dict(runner_result.telemetry or {})),
            policy_configuration=attempt_context.policy_configuration,
        )
        candidate_quality_path = attempt_dir / "candidate-patch-quality.json"
        write_json_atomic(
            candidate_quality_path,
            redact_data(candidate_quality, secrets=secrets),
        )
        baseline_sha256 = (
            attempt_context.baseline_sha256
            or hashlib.sha256(
                json.dumps(
                    isolated.metadata["source_repository"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        candidate_state_before_path = attempt_dir / ".candidate-state-before.diff"
        candidate_state_after_path = attempt_dir / ".candidate-state-after.diff"
        candidate_state_before = candidate_state_sha256(
            isolated.copied.worktree_path, candidate_state_before_path
        )
        repository_validation = execute_repository_validation(
            provider=environment_provider,
            prepared_environment=prepared_environment,
            configuration=attempt_context.policy_configuration,
            run_id=attempt_context.run_id,
            attempt_id=attempt_context.attempt_id,
            candidate_id=attempt_context.attempt_id,
            baseline_sha256=baseline_sha256,
        )
        candidate_state_after = candidate_state_sha256(
            isolated.copied.worktree_path, candidate_state_after_path
        )
        candidate_state_before_path.unlink(missing_ok=True)
        candidate_state_after_path.unlink(missing_ok=True)
        if candidate_state_after != candidate_state_before:
            repository_validation = invalidate_repository_validation(
                repository_validation,
                failure_code="repository_validation_malformed_result",
            )
        write_json_atomic(
            attempt_dir / "repository-validation.json",
            redact_data(repository_validation, secrets=secrets),
        )
        try:
            validation_coverage = build_validation_coverage(
                worktree=isolated.copied.worktree_path,
                task_instruction=attempt_context.task,
                success_criteria=attempt_context.success_criteria,
                policy_configuration=attempt_context.policy_configuration,
                repository_validation=repository_validation,
                candidate_quality=candidate_quality,
            )
        except Exception:
            # Coverage-generation failure cannot convert a passing command into
            # requirement proof.  Persist a conservative readable projection.
            validation_coverage = legacy_validation_coverage(
                repository_validation=repository_validation,
                task_instruction=attempt_context.task,
                success_criteria=attempt_context.success_criteria,
                policy_configuration=attempt_context.policy_configuration,
            ).model_copy(
                update={
                    "migration": {
                        "source_schema_version": repository_validation.schema_version,
                        "mode": "coverage_generation_failed_closed",
                        "behavior_coverage_inferred": False,
                    }
                }
            )
        write_json_atomic(
            attempt_dir / "validation-coverage.json",
            redact_data(validation_coverage, secrets=secrets),
        )
        runtime_events = (
            *runtime_events,
            *repository_validation_runtime_events(repository_validation),
        )
        capture = self._isolation.capture(isolated)
        patch_path = isolated.patch_path
        patch = read_patch_text(patch_path, errors="replace")
        candidate_manifest = write_candidate_bundle(
            context=attempt_context,
            worktree=isolated.copied.worktree_path,
            patch=patch,
            changed_files=capture.changed_files,
            source_repository=isolated.metadata["source_repository"],
            execution_environment_report=environment_report,
            repository_validation=repository_validation,
            candidate_patch_quality=candidate_quality,
            execution_provider=prepared_environment.provider,
            execution_environment_fingerprint=prepared_environment.fingerprint,
            secrets=secrets,
        )
        write_json_atomic(
            attempt_dir / "candidate" / "validation-coverage.json",
            redact_data(validation_coverage, secrets=secrets),
        )
        collection: dict[str, Any] = {}
        try:
            collection = environment_provider.collect(prepared_environment)
        finally:
            self._release_environment(attempt_context.attempt_id)
        environment_report = prepared_environment.durable_report()
        environment_report["collection"] = collection
        write_json_atomic(
            attempt_dir / "execution_environment.json", environment_report
        )
        write_json_atomic(run_dir / "execution_environment.json", environment_report)
        stdout = str(redact_data(runner_result.stdout or "", secrets=secrets))
        stderr = str(redact_data(runner_result.stderr or "", secrets=secrets))
        (attempt_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(stderr, encoding="utf-8")

        input_tokens, output_tokens, token_status = _token_accounting(runner_result)
        duration_ms = runner_result.duration_ms
        if duration_ms is None:
            duration_ms = measured_duration
        if (
            runner_result.total_cost is not None
            and runner_result.cost_accounting_status == "complete"
            and runner_result.cost_currency
            and runner_result.cost_source
        ):
            cost_breakdown = provider_reported_attempt_cost(
                backend,
                amount=runner_result.total_cost,
                currency=runner_result.cost_currency,
                source=runner_result.cost_source,
            )
        else:
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
        elif runner_result.exit_code != 0:
            if runner_result.failure_code:
                code = runner_result.failure_code
                failure_classification = (
                    "coding_failure"
                    if code in {"codex_coding_failure", "claude_coding_failure"}
                    else "infrastructure_failure"
                )
            else:
                code, failure_classification = _failure_from_runner_output(
                    runner_result.exit_code, stdout, stderr
                )
            failure = DependencyFailure(
                code=code,
                message=(
                    f"{getattr(self._runner, 'name', 'coding')} runner command was not found."
                    if code == "executable_not_found"
                    else f"{getattr(self._runner, 'name', 'coding')} runner exited with {runner_result.exit_code}."
                ),
                details={
                    "exit_code": runner_result.exit_code,
                    "retryable": runner_result.failure_retryable,
                    **_failure_snippets(stdout, stderr),
                },
            )
        if capture.failure_reason:
            failure_classification = failure_classification or "patch_capture_failure"
            failure = failure or DependencyFailure(
                code="patch_capture_failure",
                message=str(redact_data(capture.failure_reason, secrets=secrets)),
            )

        status: Literal["completed", "failed", "cancelled"] = (
            "cancelled"
            if runner_result.cancelled
            else "completed"
            if runner_result.exit_code == 0 and not capture.failure_reason
            else "failed"
        )
        structured_file_writes = sum(
            event.event_type in {"file_write", "file_patch_applied"}
            for event in runtime_events
        )
        total_file_writes = max(
            int(runner_result.total_file_writes or 0), structured_file_writes
        )
        telemetry = redact_data(
            {
                **dict(runner_result.telemetry or {}),
                **_runner_efficiency_telemetry(dict(runner_result.telemetry or {})),
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
                "total_file_writes": total_file_writes,
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
                "provider_reported_cost_currency": runner_result.cost_currency,
                "provider_reported_cost_status": runner_result.cost_accounting_status,
                "provider_reported_cost_source": runner_result.cost_source,
                "per_model_usage": runner_result.per_model_usage,
                "cost_breakdown": cost_breakdown.as_dict(),
                "translated_runtime_event_count": len(runtime_events),
                "execution_environment": environment_report,
                "repository_validation_path": (
                    f"attempts/{attempt_context.attempt_id}/repository-validation.json"
                ),
                "repository_validation": repository_validation.model_dump(mode="json"),
                "validation_coverage_path": (
                    f"attempts/{attempt_context.attempt_id}/validation-coverage.json"
                ),
                "validation_coverage": validation_coverage.model_dump(mode="json"),
                "candidate_bundle_path": (
                    f"attempts/{attempt_context.attempt_id}/candidate/candidate.json"
                ),
                "candidate_patch_quality_path": (
                    f"attempts/{attempt_context.attempt_id}/"
                    "candidate-patch-quality.json"
                ),
                "candidate_quality_report": candidate_quality.model_dump(mode="json"),
                "relevant_diff_ratio": candidate_quality.relevant_diff_ratio,
                "line_ending_only_lines": (candidate_quality.line_ending_only_lines),
                "generated_files_excluded": (
                    candidate_preparation.generated_files_excluded
                ),
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
            "total_file_writes": total_file_writes,
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
                "total_file_writes": total_file_writes,
                "commands_executed": runner_result.commands_executed,
                "commands_failed": runner_result.commands_failed,
                **_runner_efficiency_telemetry(dict(runner_result.telemetry or {})),
                "relevant_diff_ratio": candidate_quality.relevant_diff_ratio,
                "line_ending_only_lines": (candidate_quality.line_ending_only_lines),
                "generated_files_excluded": (
                    candidate_preparation.generated_files_excluded
                ),
            },
            "execution_environment_fingerprint": prepared_environment.fingerprint,
            "execution_environment_preflight": "preflight.json",
            "execution_provider": prepared_environment.provider,
            "candidate_execution_worktree_paths": [
                str(isolated.copied.worktree_path.resolve())
            ],
            "repository_validation_path": (
                f"attempts/{attempt_context.attempt_id}/repository-validation.json"
            ),
            "repository_validation_status": repository_validation.status,
            "repository_validation_failure_code": (repository_validation.failure_code),
            "repository_validation_authoritative": (
                repository_validation.authoritative
            ),
            "repository_validation_retry_count": repository_validation.retry_count,
            "validation_coverage_path": (
                f"attempts/{attempt_context.attempt_id}/validation-coverage.json"
            ),
            "validation_coverage_schema_version": validation_coverage.schema_version,
            "candidate_bundle_path": (
                f"attempts/{attempt_context.attempt_id}/candidate/candidate.json"
            ),
            "candidate_patch_path": (
                f"attempts/{attempt_context.attempt_id}/candidate/"
                f"{candidate_manifest.patch_path}"
            ),
            "candidate_patch_quality_path": (
                f"attempts/{attempt_context.attempt_id}/candidate-patch-quality.json"
            ),
            "candidate_quality_report": candidate_quality.model_dump(mode="json"),
            "relevant_diff_ratio": candidate_quality.relevant_diff_ratio,
            "line_ending_only_lines": candidate_quality.line_ending_only_lines,
            "generated_files_excluded": (
                candidate_preparation.generated_files_excluded
            ),
        }
        effective_config_path = attempt_dir / "effective_candidate_configuration.json"
        if effective_config_path.is_file():
            try:
                effective_config = (
                    CandidateExecutionAcknowledgement.model_validate_json(
                        effective_config_path.read_text(encoding="utf-8")
                    )
                )
            except (ValueError, json.JSONDecodeError) as error:
                metadata["runner_acknowledged_candidate_configuration"] = False
                metadata["candidate_configuration_acknowledgement_error"] = type(
                    error
                ).__name__
            else:
                effective_document = effective_config.model_dump(mode="json")
                metadata["effective_candidate_configuration"] = effective_document
                metadata["runner_acknowledged_candidate_configuration"] = bool(
                    effective_config.runner_acknowledged
                )
                metadata["effective_configuration_sha256"] = (
                    effective_config.effective_configuration_digest
                    if effective_config.runner_acknowledged
                    else None
                )

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

    def execute_focused_probes(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
        requests: list[Mapping[str, Any]],
    ) -> AttemptResult:
        """Rehydrate a preserved candidate and run controller-owned probes only."""

        parsed = [FocusedProbeRequest.model_validate(item) for item in requests]
        attempt_dir = Path(attempt_context.attempt_directory).resolve()
        run_dir = Path(attempt_context.run_directory).resolve()
        verification_dir = run_dir / "verification"
        verification_dir.mkdir(parents=True, exist_ok=True)
        request_path = (
            verification_dir
            / f"{attempt_context.attempt_id}-focused-probe-requests.json"
        )
        report_path = (
            verification_dir / f"{attempt_context.attempt_id}-focused-probes.json"
        )
        prior = load_focused_probe_report(report_path)
        retry_count = prior.retry_count + 1 if prior is not None else 0
        write_json_atomic(
            request_path,
            redact_data(
                {
                    "schema_version": "villani.focused_probe_requests.v1",
                    "run_id": attempt_context.run_id,
                    "attempt_id": attempt_context.attempt_id,
                    "retry_count": retry_count,
                    "requests": [item.model_dump(mode="json") for item in parsed],
                }
            ),
        )

        backend = self._backend(attempt_context)
        probe_root = (
            attempt_dir / f".verification-probe-{retry_count + 1:03d}"
        ).resolve()
        probe_root.relative_to(attempt_dir)
        probe_context = replace(
            attempt_context,
            attempt_directory=probe_root,
        )
        candidate_dir = attempt_dir / "candidate"
        isolated = None
        probe_worktree = str((probe_root / "worktree").resolve())
        configured_env = attempt_context.policy_configuration.get("runner_env")
        raw_source_environment = {**dict(os.environ), **dict(backend.env)}
        if isinstance(configured_env, Mapping):
            raw_source_environment.update(
                {str(key): str(value) for key, value in configured_env.items()}
            )
        model_credential_names = {
            backend.api_key_env.strip()
            if backend.api_key_env and backend.api_key_env.strip()
            else ""
        }
        resolved_model_credential = backend.resolved_api_key(raw_source_environment)
        model_credential_values = {
            resolved_model_credential or "",
            backend.api_key or "",
        }
        source_environment = _credential_free_environment(
            raw_source_environment,
            denied_names=model_credential_names,
            denied_values=model_credential_values,
        )
        provider = None
        original_fingerprint = str(
            attempt_result.metadata.get("execution_environment_fingerprint") or ""
        )
        baseline_sha256 = (
            attempt_context.baseline_sha256
            or str(attempt_result.metadata.get("baseline_sha256") or "")
            or "unknown"
        )

        def failed_report(
            *,
            fingerprint: str,
            provider_name: str,
            failure_code: FocusedProbeFailureCode,
            stderr: str,
        ) -> FocusedProbeReport:
            safe_stderr = str(redact_data(stderr))
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            results = []
            for request in parsed:
                command = CandidateCommandResult(
                    validation_id=request.probe_id,
                    argv=list(request.argv),
                    command_role="verifier_probe",
                    status="infrastructure_error",
                    exit_code=None,
                    duration_ms=0,
                    stdout="",
                    stderr=safe_stderr,
                    stdout_bytes=0,
                    stderr_bytes=len(stderr.encode()),
                    stdout_truncated=False,
                    stderr_truncated=False,
                    execution_environment_fingerprint=fingerprint,
                    execution_provider=provider_name,
                    worktree_path=probe_worktree,
                    baseline_sha256=baseline_sha256,
                    candidate_state="post_mutation",
                    started_at=timestamp,
                    completed_at=timestamp,
                    failure_code=failure_code,
                )
                results.append(
                    FocusedProbeResult(
                        probe_id=request.probe_id,
                        requirement_ids=list(request.requirement_ids),
                        request=request,
                        command_result=command,
                        status="infrastructure_error",
                        evidence_id=f"focused_probe:{request.probe_id}",
                        effective_timeout_seconds=request.timeout_seconds,
                        reason=safe_stderr,
                    )
                )
            return FocusedProbeReport(
                schema_version="villani.focused_probe.v1",
                run_id=attempt_context.run_id,
                attempt_id=attempt_context.attempt_id,
                candidate_id=attempt_context.attempt_id,
                execution_environment_fingerprint=fingerprint,
                execution_provider=provider_name,
                worktree_path=probe_worktree,
                baseline_sha256=baseline_sha256,
                requests=parsed,
                results=results,
                status="infrastructure_error",
                completed_at=timestamp,
                retry_count=retry_count,
                failure_code=failure_code,
            )

        prepared_environment = None
        environment_report: dict[str, Any] = {}
        report: FocusedProbeReport
        try:
            isolated = self._isolation.create(probe_context)
            apply_candidate_bundle(isolated.copied.worktree_path, candidate_dir)
            probe_worktree = str(isolated.copied.worktree_path.resolve())
            provider = provider_from_configuration(
                attempt_context.policy_configuration,
                source_environment=source_environment,
                cache_root=Path(attempt_context.run_directory).parent.parent
                / "cache"
                / "execution-environments",
                selection=attempt_context.execution_provider
                or backend.execution_environment,
                pluginized=True,
            )
            prepared_environment = provider.prepare(
                repository=Path(attempt_context.repository_path),
                worktree=isolated.copied.worktree_path,
            )
            prepared_environment.execution_environment_selection = (
                attempt_context.execution_provider
                or backend.execution_environment
                or prepared_environment.provider
            )
            prepared_environment.configuration_digest = hashlib.sha256(
                json.dumps(
                    provider.config.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            runner_controls = getattr(provider, "runner_controls", None)
            controls = (
                runner_controls(prepared_environment)
                if callable(runner_controls)
                else {}
            )
            prepared_environment.command_prefix_digest = hashlib.sha256(
                json.dumps(
                    list(
                        controls.get("execution_prefix") or []
                        if isinstance(controls, Mapping)
                        else []
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            probe_temp = probe_root / "tmp"
            probe_temp.mkdir(parents=True, exist_ok=True)
            prepared_environment.environment = _credential_free_environment(
                prepared_environment.environment,
                denied_names=model_credential_names,
                denied_values=model_credential_values,
            )
            prepared_environment.environment.update(
                {
                    "TMPDIR": str(probe_temp),
                    "TMP": str(probe_temp),
                    "TEMP": str(probe_temp),
                }
            )
            environment_report = prepared_environment.durable_report()
            if prepared_environment.fingerprint != original_fingerprint:
                report = failed_report(
                    fingerprint=prepared_environment.fingerprint,
                    provider_name=prepared_environment.provider,
                    failure_code="focused_probe_environment_mismatch",
                    stderr=(
                        "rehydrated execution-environment fingerprint does not "
                        "match the original candidate"
                    ),
                )
            else:
                before_path = probe_root / ".candidate-probe-before.diff"
                after_path = probe_root / ".candidate-probe-after.diff"
                before = candidate_state_sha256(
                    isolated.copied.worktree_path, before_path
                )
                report = execute_focused_probe_requests(
                    provider=provider,
                    prepared_environment=prepared_environment,
                    requests=parsed,
                    run_id=attempt_context.run_id,
                    attempt_id=attempt_context.attempt_id,
                    candidate_id=attempt_context.attempt_id,
                    baseline_sha256=baseline_sha256,
                    retry_count=retry_count,
                )
                after = candidate_state_sha256(
                    isolated.copied.worktree_path, after_path
                )
                before_path.unlink(missing_ok=True)
                after_path.unlink(missing_ok=True)
                if before != after:
                    report = invalidate_focused_probe_report(
                        report,
                        failure_code="focused_probe_malformed_result",
                        reason=(
                            "Focused verification changed the preserved candidate; "
                            "the probe is not authoritative."
                        ),
                    )
        except Exception as error:
            report = failed_report(
                fingerprint=original_fingerprint or "unknown",
                provider_name=str(
                    getattr(
                        provider,
                        "name",
                        attempt_context.execution_provider or "unknown",
                    )
                ),
                failure_code="focused_probe_provider_failure",
                stderr=str(error),
            )
        finally:
            if prepared_environment is not None and provider is not None:
                collection: dict[str, Any] = {}
                try:
                    collection = provider.collect(prepared_environment)
                except Exception as error:
                    report = failed_report(
                        fingerprint=prepared_environment.fingerprint,
                        provider_name=prepared_environment.provider,
                        failure_code="focused_probe_provider_failure",
                        stderr=f"focused probe collection failed: {error}",
                    )
                try:
                    provider.cleanup(prepared_environment)
                except Exception as error:
                    report = failed_report(
                        fingerprint=prepared_environment.fingerprint,
                        provider_name=prepared_environment.provider,
                        failure_code="focused_probe_provider_failure",
                        stderr=f"focused probe cleanup failed: {error}",
                    )
                environment_report = prepared_environment.durable_report()
                environment_report["collection"] = collection
            if isolated is not None:
                try:
                    self._isolation.cleanup(isolated.copied.worktree_path)
                except Exception as error:
                    report = failed_report(
                        fingerprint=(
                            prepared_environment.fingerprint
                            if prepared_environment is not None
                            else original_fingerprint or "unknown"
                        ),
                        provider_name=str(
                            getattr(
                                provider,
                                "name",
                                attempt_context.execution_provider or "unknown",
                            )
                        ),
                        failure_code="focused_probe_provider_failure",
                        stderr=f"focused probe isolation cleanup failed: {error}",
                    )
            probe_root.relative_to(attempt_dir)
            shutil.rmtree(probe_root, ignore_errors=True)

        write_json_atomic(report_path, redact_data(report))
        environment_path = (
            verification_dir
            / f"{attempt_context.attempt_id}-focused-probe-environment-"
            f"{retry_count + 1:03d}.json"
        )
        if environment_report:
            write_json_atomic(environment_path, redact_data(environment_report))
        relative_report = report_path.relative_to(run_dir).as_posix()
        relative_requests = request_path.relative_to(run_dir).as_posix()
        telemetry = dict(attempt_result.runner_telemetry)
        telemetry.update(
            {
                "focused_probe_report_path": relative_report,
                "focused_probe_requests_path": relative_requests,
                "focused_probe_status": report.status,
                "focused_probe_retry_count": retry_count,
            }
        )
        write_json_atomic(attempt_dir / "runner_telemetry.json", redact_data(telemetry))
        raw_allowed_paths = attempt_result.metadata.get(
            "candidate_execution_worktree_paths",
            [attempt_result.worktree_path],
        )
        if not isinstance(raw_allowed_paths, list):
            raw_allowed_paths = [raw_allowed_paths]
        allowed_paths = [str(item) for item in raw_allowed_paths if item]
        if probe_worktree not in allowed_paths:
            allowed_paths.append(probe_worktree)
        metadata = dict(attempt_result.metadata)
        metadata.update(
            {
                "focused_probe_report_path": relative_report,
                "focused_probe_requests_path": relative_requests,
                "focused_probe_status": report.status,
                "focused_probe_failure_code": report.failure_code,
                "focused_probe_retry_count": retry_count,
                "focused_probe_worktree_path": probe_worktree,
                "candidate_execution_worktree_paths": allowed_paths,
                "focused_probe_environment_path": (
                    environment_path.relative_to(run_dir).as_posix()
                    if environment_report
                    else None
                ),
            }
        )
        return replace(
            attempt_result,
            runner_telemetry=telemetry,
            runtime_events=focused_probe_runtime_events(report),
            metadata=metadata,
        )

    def retry_repository_validation(
        self,
        attempt_context: AttemptContext,
        attempt_result: AttemptResult,
    ) -> AttemptResult:
        """Rehydrate one preserved candidate and rerun only repository validation."""

        attempt_dir = Path(attempt_context.attempt_directory).resolve()
        candidate_dir = attempt_dir / "candidate"
        prior = load_repository_validation_report(attempt_dir)
        if prior is None:
            raise RuntimeError(
                "repository validation retry requires a persisted v2 report"
            )
        backend = self._backend(attempt_context)
        isolated = self._isolation.create(attempt_context)
        apply_candidate_bundle(isolated.copied.worktree_path, candidate_dir)
        configured_env = attempt_context.policy_configuration.get("runner_env")
        source_environment = {**dict(os.environ), **dict(backend.env)}
        if isinstance(configured_env, Mapping):
            source_environment.update(
                {str(key): str(value) for key, value in configured_env.items()}
            )
        provider = provider_from_configuration(
            attempt_context.policy_configuration,
            source_environment=source_environment,
            cache_root=Path(attempt_context.run_directory).parent.parent
            / "cache"
            / "execution-environments",
            selection=attempt_context.execution_provider
            or backend.execution_environment,
            pluginized=True,
        )
        original_fingerprint = str(
            attempt_result.metadata.get("execution_environment_fingerprint")
            or prior.execution_environment_fingerprint
        )
        baseline_sha256 = (
            attempt_context.baseline_sha256
            or str(attempt_result.metadata.get("baseline_sha256") or "")
            or (prior.commands[0].baseline_sha256 if prior.commands else "unknown")
        )
        retry_count = prior.retry_count + 1

        def failed_report(
            *,
            fingerprint: str,
            provider_name: str,
            failure_code: RepositoryValidationFailureCode,
            stderr: str,
        ) -> RepositoryValidationReport:
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            commands = [
                RepositoryValidationCommandResult(
                    validation_id=item.validation_id,
                    argv=list(item.argv),
                    command_role="repository_validation",
                    status="infrastructure_error",
                    exit_code=None,
                    duration_ms=0,
                    stdout="",
                    stderr=str(redact_data(stderr)),
                    stdout_bytes=0,
                    stderr_bytes=len(stderr.encode()),
                    stdout_truncated=False,
                    stderr_truncated=False,
                    execution_environment_fingerprint=fingerprint,
                    execution_provider=provider_name,
                    worktree_path=str(isolated.copied.worktree_path.resolve()),
                    baseline_sha256=baseline_sha256,
                    candidate_state="post_mutation",
                    started_at=timestamp,
                    completed_at=timestamp,
                    failure_code=failure_code,
                )
                for item in prior.commands
            ]
            return RepositoryValidationReport(
                schema_version="villani.repository_validation.v2",
                run_id=attempt_context.run_id,
                attempt_id=attempt_context.attempt_id,
                candidate_id=attempt_context.attempt_id,
                execution_environment_fingerprint=fingerprint,
                execution_provider=provider_name,
                commands=commands,
                status="infrastructure_error",
                authoritative=False,
                completed_at=timestamp,
                retry_count=retry_count,
                failure_code=failure_code,
            )

        prepared_environment = None
        environment_report: dict[str, Any] = {}
        try:
            prepared_environment = provider.prepare(
                repository=Path(attempt_context.repository_path),
                worktree=isolated.copied.worktree_path,
            )
            prepared_environment.execution_environment_selection = (
                attempt_context.execution_provider
                or backend.execution_environment
                or prepared_environment.provider
            )
            prepared_environment.configuration_digest = hashlib.sha256(
                json.dumps(
                    provider.config.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            runner_controls = getattr(provider, "runner_controls", None)
            controls = (
                runner_controls(prepared_environment)
                if callable(runner_controls)
                else {}
            )
            prepared_environment.command_prefix_digest = hashlib.sha256(
                json.dumps(
                    list(
                        controls.get("execution_prefix") or []
                        if isinstance(controls, Mapping)
                        else []
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            environment_report = prepared_environment.durable_report()
            if prepared_environment.fingerprint != original_fingerprint:
                report = failed_report(
                    fingerprint=prepared_environment.fingerprint,
                    provider_name=prepared_environment.provider,
                    failure_code="repository_validation_environment_mismatch",
                    stderr=(
                        "rehydrated execution-environment fingerprint does not "
                        "match the original candidate"
                    ),
                )
            else:
                before_path = attempt_dir / ".candidate-retry-before.diff"
                after_path = attempt_dir / ".candidate-retry-after.diff"
                before = candidate_state_sha256(
                    isolated.copied.worktree_path, before_path
                )
                report = execute_repository_validation(
                    provider=provider,
                    prepared_environment=prepared_environment,
                    configuration=attempt_context.policy_configuration,
                    run_id=attempt_context.run_id,
                    attempt_id=attempt_context.attempt_id,
                    candidate_id=attempt_context.attempt_id,
                    baseline_sha256=baseline_sha256,
                    retry_count=retry_count,
                )
                after = candidate_state_sha256(
                    isolated.copied.worktree_path, after_path
                )
                before_path.unlink(missing_ok=True)
                after_path.unlink(missing_ok=True)
                if before != after:
                    report = invalidate_repository_validation(
                        report,
                        failure_code="repository_validation_malformed_result",
                    )
        except Exception as error:
            report = failed_report(
                fingerprint=original_fingerprint,
                provider_name=str(getattr(provider, "name", prior.execution_provider)),
                failure_code="repository_validation_provider_failure",
                stderr=str(error),
            )
        finally:
            if prepared_environment is not None:
                collection: dict[str, Any] = {}
                try:
                    collection = provider.collect(prepared_environment)
                finally:
                    provider.cleanup(prepared_environment)
                environment_report = prepared_environment.durable_report()
                environment_report["collection"] = collection

        write_json_atomic(
            attempt_dir / "repository-validation.json", redact_data(report)
        )
        write_json_atomic(
            candidate_dir / "repository-validation.json", redact_data(report)
        )
        quality_path = attempt_dir / "candidate-patch-quality.json"
        try:
            candidate_quality = CandidatePatchQuality.model_validate_json(
                quality_path.read_text(encoding="utf-8")
            )
            validation_coverage = build_validation_coverage(
                worktree=isolated.copied.worktree_path,
                task_instruction=attempt_context.task,
                success_criteria=attempt_context.success_criteria,
                policy_configuration=attempt_context.policy_configuration,
                repository_validation=report,
                candidate_quality=candidate_quality,
            )
        except Exception:
            validation_coverage = legacy_validation_coverage(
                repository_validation=report,
                task_instruction=attempt_context.task,
                success_criteria=attempt_context.success_criteria,
                policy_configuration=attempt_context.policy_configuration,
            ).model_copy(
                update={
                    "migration": {
                        "source_schema_version": report.schema_version,
                        "mode": "coverage_generation_failed_closed",
                        "behavior_coverage_inferred": False,
                    }
                }
            )
        write_json_atomic(
            attempt_dir / "validation-coverage.json", redact_data(validation_coverage)
        )
        write_json_atomic(
            candidate_dir / "validation-coverage.json", redact_data(validation_coverage)
        )
        retry_environment_path = (
            attempt_dir
            / f"execution-environment-validation-retry-{retry_count:03d}.json"
        )
        if environment_report:
            write_json_atomic(retry_environment_path, redact_data(environment_report))
        telemetry = dict(attempt_result.runner_telemetry)
        telemetry.update(
            {
                "repository_validation_path": (
                    f"attempts/{attempt_context.attempt_id}/repository-validation.json"
                ),
                "repository_validation": report.model_dump(mode="json"),
                "repository_validation_retry_count": retry_count,
                "validation_coverage_path": (
                    f"attempts/{attempt_context.attempt_id}/validation-coverage.json"
                ),
                "validation_coverage": validation_coverage.model_dump(mode="json"),
            }
        )
        write_json_atomic(attempt_dir / "runner_telemetry.json", redact_data(telemetry))
        metadata = dict(attempt_result.metadata)
        metadata.update(
            {
                "repository_validation_status": report.status,
                "repository_validation_failure_code": report.failure_code,
                "repository_validation_authoritative": report.authoritative,
                "repository_validation_retry_count": retry_count,
                "validation_coverage_path": (
                    f"attempts/{attempt_context.attempt_id}/validation-coverage.json"
                ),
                "validation_coverage_schema_version": validation_coverage.schema_version,
                "repository_validation_retry_environment_path": (
                    retry_environment_path.relative_to(
                        Path(attempt_context.run_directory)
                    ).as_posix()
                    if environment_report
                    else None
                ),
            }
        )
        return replace(
            attempt_result,
            worktree_path=str(isolated.copied.worktree_path),
            runner_telemetry=telemetry,
            runtime_events=repository_validation_runtime_events(report),
            metadata=metadata,
        )
