"""One shell-free execution boundary for commands run against a candidate."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Sequence, cast

from .models import (
    CandidateCommandFailureCode,
    CandidateCommandResult,
    CandidateCommandRole,
    PreparedEnvironment,
)
from .security import ExecutionPolicyDenied
from .secrets import registered_secret_values

if TYPE_CHECKING:
    from .providers import ExecutionEnvironmentProvider


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_output(value: str, prepared: PreparedEnvironment) -> str:
    secrets = list(registered_secret_values())
    for name, item in prepared.environment.items():
        normalized = name.lower()
        if (
            any(
                marker in normalized
                for marker in ("key", "token", "secret", "password", "authorization")
            )
            and item
        ):
            secrets.append(item)
    # Import lazily so the lower-level execution package does not acquire an
    # import-time dependency on the closed-loop package.
    from villani_ops.closed_loop.event_writer import redact_data

    return str(redact_data(value, secrets=tuple(dict.fromkeys(secrets))))


def _infrastructure_result(
    *,
    prepared_environment: PreparedEnvironment,
    argv: list[str],
    command_role: CandidateCommandRole,
    validation_id: str,
    baseline_sha256: str,
    candidate_state: str,
    started_at: str,
    status: str,
    failure_code: CandidateCommandFailureCode,
    stderr: str,
    duration_ms: int = 0,
) -> CandidateCommandResult:
    return CandidateCommandResult(
        validation_id=validation_id,
        argv=argv,
        command_role=command_role,
        status=status,  # type: ignore[arg-type]
        exit_code=None,
        duration_ms=duration_ms,
        stdout="",
        stderr=_redact_output(stderr, prepared_environment),
        stdout_bytes=0,
        stderr_bytes=len(stderr.encode()),
        stdout_truncated=False,
        stderr_truncated=False,
        execution_environment_fingerprint=prepared_environment.fingerprint,
        execution_provider=prepared_environment.provider,
        worktree_path=str(Path(prepared_environment.worktree_path).resolve()),
        baseline_sha256=baseline_sha256,
        candidate_state=candidate_state,  # type: ignore[arg-type]
        started_at=started_at,
        completed_at=_timestamp(),
        failure_code=failure_code,
    )


def _failure_code(
    command_role: CandidateCommandRole,
    category: Literal[
        "passed",
        "behavior_failure",
        "timeout",
        "executable_missing",
        "environment_mismatch",
        "provider_failure",
        "policy_denied",
        "malformed_result",
    ],
) -> CandidateCommandFailureCode:
    if command_role == "verifier_probe":
        return {
            "passed": "focused_probe_passed",
            "behavior_failure": "focused_probe_behavior_failure",
            "timeout": "focused_probe_timeout",
            "executable_missing": "focused_probe_executable_missing",
            "environment_mismatch": "focused_probe_environment_mismatch",
            "provider_failure": "focused_probe_provider_failure",
            "policy_denied": "focused_probe_policy_denied",
            "malformed_result": "focused_probe_malformed_result",
        }[category]  # type: ignore[return-value]
    return {
        "passed": "repository_validation_passed",
        "behavior_failure": "repository_validation_test_failure",
        "timeout": "repository_validation_timeout",
        "executable_missing": "repository_validation_executable_missing",
        "environment_mismatch": "repository_validation_environment_mismatch",
        "provider_failure": "repository_validation_provider_failure",
        "policy_denied": "repository_validation_policy_denied",
        "malformed_result": "repository_validation_malformed_result",
    }[category]  # type: ignore[return-value]


def execute_candidate_command(
    *,
    provider: object,
    prepared_environment: PreparedEnvironment,
    argv: Sequence[str],
    command_role: CandidateCommandRole,
    run_id: str,
    attempt_id: str,
    validation_id: str,
    baseline_sha256: str,
    candidate_state: str,
) -> CandidateCommandResult:
    """Execute one argv through the exact provider and prepared environment.

    ``run_id`` and ``attempt_id`` are required at this boundary even though the
    command result stores them in its enclosing report and runtime events.
    """

    if not run_id or not attempt_id:
        raise ValueError("candidate command requires run_id and attempt_id")
    command = list(argv)
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("candidate command argv must contain non-empty strings")
    if candidate_state != "post_mutation":
        raise ValueError("candidate commands require candidate_state='post_mutation'")
    if not baseline_sha256:
        raise ValueError("candidate command requires baseline_sha256")

    started_at = _timestamp()
    worktree = Path(prepared_environment.worktree_path).resolve()
    environment_provider = cast("ExecutionEnvironmentProvider", provider)
    if not worktree.is_dir():
        return _infrastructure_result(
            prepared_environment=prepared_environment,
            argv=command,
            command_role=command_role,
            validation_id=validation_id,
            baseline_sha256=baseline_sha256,
            candidate_state=candidate_state,
            started_at=started_at,
            status="infrastructure_error",
            failure_code=_failure_code(command_role, "provider_failure"),
            stderr="candidate worktree is unavailable",
        )

    try:
        observed_fingerprint = str(
            environment_provider.fingerprint(Path(prepared_environment.repository_path))
        )
    except Exception as error:
        return _infrastructure_result(
            prepared_environment=prepared_environment,
            argv=command,
            command_role=command_role,
            validation_id=validation_id,
            baseline_sha256=baseline_sha256,
            candidate_state=candidate_state,
            started_at=started_at,
            status="infrastructure_error",
            failure_code=_failure_code(command_role, "provider_failure"),
            stderr=str(error),
        )
    if observed_fingerprint != prepared_environment.fingerprint:
        return _infrastructure_result(
            prepared_environment=prepared_environment,
            argv=command,
            command_role=command_role,
            validation_id=validation_id,
            baseline_sha256=baseline_sha256,
            candidate_state=candidate_state,
            started_at=started_at,
            status="infrastructure_error",
            failure_code=_failure_code(command_role, "environment_mismatch"),
            stderr="execution-environment fingerprint changed before command execution",
        )

    try:
        raw = environment_provider.execute(prepared_environment, command)
    except ExecutionPolicyDenied as error:
        return _infrastructure_result(
            prepared_environment=prepared_environment,
            argv=command,
            command_role=command_role,
            validation_id=validation_id,
            baseline_sha256=baseline_sha256,
            candidate_state=candidate_state,
            started_at=started_at,
            status="policy_denied",
            failure_code=_failure_code(command_role, "policy_denied"),
            stderr=str(error),
        )
    except FileNotFoundError as error:
        return _infrastructure_result(
            prepared_environment=prepared_environment,
            argv=command,
            command_role=command_role,
            validation_id=validation_id,
            baseline_sha256=baseline_sha256,
            candidate_state=candidate_state,
            started_at=started_at,
            status="infrastructure_error",
            failure_code=_failure_code(command_role, "executable_missing"),
            stderr=str(error),
        )
    except Exception as error:
        return _infrastructure_result(
            prepared_environment=prepared_environment,
            argv=command,
            command_role=command_role,
            validation_id=validation_id,
            baseline_sha256=baseline_sha256,
            candidate_state=candidate_state,
            started_at=started_at,
            status="infrastructure_error",
            failure_code=_failure_code(command_role, "provider_failure"),
            stderr=str(error),
        )

    required_fields = (
        "exit_code",
        "duration_ms",
        "stdout",
        "stderr",
        "stdout_bytes",
        "stderr_bytes",
        "stdout_truncated",
        "stderr_truncated",
        "timed_out",
        "failure_classification",
    )
    if any(not hasattr(raw, field) for field in required_fields):
        return _infrastructure_result(
            prepared_environment=prepared_environment,
            argv=command,
            command_role=command_role,
            validation_id=validation_id,
            baseline_sha256=baseline_sha256,
            candidate_state=candidate_state,
            started_at=started_at,
            status="infrastructure_error",
            failure_code=_failure_code(command_role, "malformed_result"),
            stderr="execution provider returned a malformed command result",
        )

    failure_classification = getattr(raw, "failure_classification", None)
    exit_code = int(raw.exit_code)
    stderr = str(raw.stderr or "")
    # Providers raise FileNotFoundError when their outer executable is missing.
    # Containerized command runners conventionally return 127 for an executable
    # that cannot be located. Do not inspect arbitrary stderr here: a candidate
    # test can legitimately fail with "no such file" when the implementation
    # omitted a required artifact.
    missing = exit_code == 127
    if bool(raw.timed_out) or failure_classification == "timeout":
        status = "timed_out"
        failure_code: CandidateCommandFailureCode | None = _failure_code(
            command_role, "timeout"
        )
    elif failure_classification == "policy_denied":
        status = "policy_denied"
        failure_code = _failure_code(command_role, "policy_denied")
    elif failure_classification in {
        "disk_limit",
        "process_limit",
        "memory_limit",
    }:
        status = "infrastructure_error"
        failure_code = _failure_code(command_role, "provider_failure")
    elif missing:
        status = "infrastructure_error"
        failure_code = _failure_code(command_role, "executable_missing")
    elif exit_code != 0:
        status = "failed"
        failure_code = _failure_code(command_role, "behavior_failure")
    else:
        status = "passed"
        failure_code = _failure_code(command_role, "passed")

    return CandidateCommandResult(
        validation_id=validation_id,
        argv=command,
        command_role=command_role,
        status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        duration_ms=max(int(raw.duration_ms), 0),
        stdout=_redact_output(str(raw.stdout or ""), prepared_environment),
        stderr=_redact_output(stderr, prepared_environment),
        stdout_bytes=max(int(raw.stdout_bytes), 0),
        stderr_bytes=max(int(raw.stderr_bytes), 0),
        stdout_truncated=bool(raw.stdout_truncated),
        stderr_truncated=bool(raw.stderr_truncated),
        execution_environment_fingerprint=prepared_environment.fingerprint,
        execution_provider=prepared_environment.provider,
        worktree_path=str(worktree),
        baseline_sha256=baseline_sha256,
        candidate_state="post_mutation",
        started_at=started_at,
        completed_at=_timestamp(),
        failure_code=failure_code,
    )
