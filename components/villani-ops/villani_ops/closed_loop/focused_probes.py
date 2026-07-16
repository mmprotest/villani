"""Controller-owned focused probes using the shared candidate execution boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from villani_ops.execution_environment.candidate_execution import (
    execute_candidate_command,
)
from villani_ops.execution_environment.models import (
    CandidateCommandResult,
    ExecutionEnvironmentConfig,
    PreparedEnvironment,
)
from villani_ops.execution_environment.security import (
    ExecutionPolicyDenied,
    check_path,
)

from .interfaces import RuntimeEvent
from .verification_evidence import (
    FocusedProbeReport,
    FocusedProbeRequest,
    FocusedProbeResult,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path_policy_check(
    request: FocusedProbeRequest,
    *,
    prepared_environment: PreparedEnvironment,
    configuration: ExecutionEnvironmentConfig,
) -> None:
    """Reject explicit path traversal without interpreting command-language text."""

    worktree = Path(prepared_environment.worktree_path).resolve()
    for value in request.argv[1:]:
        candidate = Path(value)
        if not candidate.is_absolute() and ".." not in candidate.parts:
            continue
        check_path(
            candidate if candidate.is_absolute() else worktree / candidate,
            worktree,
            configuration.policy,
        )


def _effective_timeout(provider: object, requested: int) -> tuple[int, object | None]:
    configuration = getattr(provider, "config", None)
    if not isinstance(configuration, ExecutionEnvironmentConfig):
        return requested, None
    effective = min(requested, configuration.limits.timeout_seconds)
    limits = configuration.limits.model_copy(update={"timeout_seconds": effective})
    setattr(provider, "config", configuration.model_copy(update={"limits": limits}))
    return effective, configuration


def _compare(
    request: FocusedProbeRequest,
    command: CandidateCommandResult,
) -> tuple[bool, str]:
    failures: list[str] = []
    if command.exit_code != request.expected_exit_code:
        failures.append(
            f"expected exit {request.expected_exit_code}, got {command.exit_code}"
        )
    if (
        request.expected_stdout is not None
        and command.stdout != request.expected_stdout
    ):
        failures.append("stdout did not exactly match the expected value")
    for expected in request.expected_stdout_contains:
        if expected not in command.stdout:
            failures.append(f"stdout did not contain {expected!r}")
    for expected in request.expected_stderr_contains:
        if expected not in command.stderr:
            failures.append(f"stderr did not contain {expected!r}")
    if failures:
        return False, "; ".join(failures)
    return True, "The focused probe matched every declared expectation."


def execute_focused_probes(
    *,
    provider: object,
    prepared_environment: PreparedEnvironment,
    requests: list[FocusedProbeRequest],
    run_id: str,
    attempt_id: str,
    candidate_id: str,
    baseline_sha256: str,
    retry_count: int = 0,
) -> FocusedProbeReport:
    results: list[FocusedProbeResult] = []
    for request in requests:
        effective_timeout, original_config = _effective_timeout(
            provider, request.timeout_seconds
        )
        try:
            configuration = cast(
                ExecutionEnvironmentConfig, getattr(provider, "config")
            )
            _path_policy_check(
                request,
                prepared_environment=prepared_environment,
                configuration=configuration,
            )
            command = execute_candidate_command(
                provider=provider,
                prepared_environment=prepared_environment,
                argv=request.argv,
                command_role="verifier_probe",
                run_id=run_id,
                attempt_id=attempt_id,
                validation_id=request.probe_id,
                baseline_sha256=baseline_sha256,
                candidate_state="post_mutation",
            )
        except ExecutionPolicyDenied as error:
            timestamp = _now()
            command = CandidateCommandResult(
                validation_id=request.probe_id,
                argv=list(request.argv),
                command_role="verifier_probe",
                status="policy_denied",
                exit_code=None,
                duration_ms=0,
                stdout="",
                stderr=str(error),
                stdout_bytes=0,
                stderr_bytes=len(str(error).encode()),
                stdout_truncated=False,
                stderr_truncated=False,
                execution_environment_fingerprint=prepared_environment.fingerprint,
                execution_provider=prepared_environment.provider,
                worktree_path=str(Path(prepared_environment.worktree_path).resolve()),
                baseline_sha256=baseline_sha256,
                candidate_state="post_mutation",
                started_at=timestamp,
                completed_at=timestamp,
                failure_code="focused_probe_policy_denied",
            )
        except Exception as error:
            timestamp = _now()
            command = CandidateCommandResult(
                validation_id=request.probe_id,
                argv=list(request.argv),
                command_role="verifier_probe",
                status="infrastructure_error",
                exit_code=None,
                duration_ms=0,
                stdout="",
                stderr=str(error),
                stdout_bytes=0,
                stderr_bytes=len(str(error).encode()),
                stdout_truncated=False,
                stderr_truncated=False,
                execution_environment_fingerprint=prepared_environment.fingerprint,
                execution_provider=prepared_environment.provider,
                worktree_path=str(Path(prepared_environment.worktree_path).resolve()),
                baseline_sha256=baseline_sha256,
                candidate_state="post_mutation",
                started_at=timestamp,
                completed_at=timestamp,
                failure_code="focused_probe_provider_failure",
            )
        finally:
            if original_config is not None:
                setattr(provider, "config", original_config)

        if command.status in {
            "timed_out",
            "infrastructure_error",
            "policy_denied",
        }:
            status = "infrastructure_error"
            reason = (
                "The focused probe could not execute reliably: "
                f"{command.failure_code or command.status}."
            )
        else:
            matched, reason = _compare(request, command)
            status = "passed" if matched else "failed"
        results.append(
            FocusedProbeResult(
                probe_id=request.probe_id,
                requirement_ids=list(request.requirement_ids),
                request=request,
                command_result=command,
                status=status,  # type: ignore[arg-type]
                evidence_id=f"focused_probe:{request.probe_id}",
                effective_timeout_seconds=effective_timeout,
                reason=reason,
            )
        )

    failure_code: str | None
    if not requests:
        status = "unavailable"
        failure_code = "focused_probe_missing"
    elif any(item.status == "infrastructure_error" for item in results):
        status = "infrastructure_error"
        failure_code = next(
            (
                item.command_result.failure_code
                for item in results
                if item.status == "infrastructure_error"
            ),
            "focused_probe_provider_failure",
        )
    elif any(item.status == "failed" for item in results):
        status = "failed"
        failure_code = "focused_probe_behavior_failure"
    else:
        status = "passed"
        failure_code = "focused_probe_passed"
    return FocusedProbeReport(
        schema_version="villani.focused_probe.v1",
        run_id=run_id,
        attempt_id=attempt_id,
        candidate_id=candidate_id,
        execution_environment_fingerprint=prepared_environment.fingerprint,
        execution_provider=prepared_environment.provider,
        worktree_path=str(Path(prepared_environment.worktree_path).resolve()),
        baseline_sha256=baseline_sha256,
        requests=requests,
        results=results,
        status=status,  # type: ignore[arg-type]
        completed_at=_now(),
        retry_count=retry_count,
        failure_code=str(failure_code),
    )


def focused_probe_runtime_events(
    report: FocusedProbeReport,
) -> tuple[RuntimeEvent, ...]:
    events: list[RuntimeEvent] = []
    for result in report.results:
        command = result.command_result
        started = datetime.fromisoformat(command.started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(command.completed_at.replace("Z", "+00:00"))
        common: dict[str, Any] = {
            "run_id": report.run_id,
            "attempt_id": report.attempt_id,
            "probe_id": result.probe_id,
            "requirement_ids": list(result.requirement_ids),
            "argv": list(command.argv),
            "command_role": "verifier_probe",
            "worktree_path": command.worktree_path,
            "baseline_sha256": command.baseline_sha256,
            "candidate_state": command.candidate_state,
            "execution_environment_fingerprint": (
                command.execution_environment_fingerprint
            ),
            "execution_provider": command.execution_provider,
        }
        source = (
            f"focused-probe:{report.attempt_id}:{report.retry_count}:{result.probe_id}"
        )
        events.append(
            RuntimeEvent(
                event_type="focused_probe_started",
                timestamp=started,
                payload={
                    **common,
                    "exit_code": None,
                    "duration_ms": 0,
                    "failure_code": None,
                },
                source_event_id=f"{source}:started",
            )
        )
        event_type = (
            "focused_probe_completed"
            if result.status == "passed"
            else "focused_probe_failed"
            if result.status == "failed"
            else "focused_probe_infrastructure_error"
        )
        events.append(
            RuntimeEvent(
                event_type=event_type,
                timestamp=completed,
                payload={
                    **common,
                    "exit_code": command.exit_code,
                    "duration_ms": command.duration_ms,
                    "failure_code": (
                        None
                        if result.status == "passed"
                        else command.failure_code
                        if result.status == "infrastructure_error"
                        else "focused_probe_behavior_failure"
                    ),
                },
                source_event_id=f"{source}:completed",
            )
        )
    return tuple(events)


def load_focused_probe_report(path: Path) -> FocusedProbeReport | None:
    if not path.is_file():
        return None
    return FocusedProbeReport.model_validate_json(path.read_text(encoding="utf-8"))


def invalidate_focused_probe_report(
    report: FocusedProbeReport,
    *,
    failure_code: str,
    reason: str,
) -> FocusedProbeReport:
    results = [
        item.model_copy(
            update={
                "status": "infrastructure_error",
                "reason": reason,
                "command_result": item.command_result.model_copy(
                    update={
                        "status": "infrastructure_error",
                        "failure_code": failure_code,
                    }
                ),
            }
        )
        for item in report.results
    ]
    return report.model_copy(
        update={
            "results": results,
            "status": "infrastructure_error",
            "completed_at": _now(),
            "failure_code": failure_code,
        }
    )


def focused_probe_identity_valid(
    report: FocusedProbeReport,
    *,
    run_id: str,
    attempt_id: str,
    baseline_sha256: str,
    execution_environment_fingerprint: str,
    execution_provider: str,
    allowed_worktree_paths: list[str],
) -> bool:
    allowed = {str(Path(item).resolve()) for item in allowed_worktree_paths}
    report_worktree = str(Path(report.worktree_path).resolve())
    return bool(
        report.run_id == run_id
        and report.attempt_id == attempt_id
        and report.candidate_id == attempt_id
        and report.baseline_sha256 == baseline_sha256
        and report.execution_environment_fingerprint
        == execution_environment_fingerprint
        and report.execution_provider == execution_provider
        and report_worktree in allowed
        and all(
            item.command_result.execution_environment_fingerprint
            == execution_environment_fingerprint
            and item.command_result.execution_provider == execution_provider
            and str(Path(item.command_result.worktree_path).resolve())
            == report_worktree
            and item.command_result.baseline_sha256 == baseline_sha256
            and item.command_result.candidate_state == "post_mutation"
            for item in report.results
        )
    )
