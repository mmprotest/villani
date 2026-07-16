"""Typed repository validation built on the shared candidate execution boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from villani_ops.execution_environment.candidate_execution import (
    execute_candidate_command,
)
from villani_ops.execution_environment.models import (
    PreparedEnvironment,
    RepositoryValidationCommandResult,
    RepositoryValidationFailureCode,
    RepositoryValidationReport,
)

from .interfaces import RuntimeEvent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _configured_commands(
    configuration: Mapping[str, Any],
) -> tuple[list[tuple[str, list[str]]], bool]:
    configured = configuration.get("repository_validation_commands")
    if configured is None:
        return [], False
    if not isinstance(configured, list):
        return [], True
    commands: list[tuple[str, list[str]]] = []
    malformed = False
    for index, item in enumerate(configured, 1):
        if not isinstance(item, Mapping):
            malformed = True
            continue
        argv = item.get("argv")
        if not (
            isinstance(argv, list)
            and argv
            and all(isinstance(value, str) and value for value in argv)
        ):
            malformed = True
            continue
        validation_id = str(
            item.get("validation_id") or f"repository_validation_{index:03d}"
        )
        if not validation_id:
            malformed = True
            continue
        commands.append((validation_id, list(argv)))
    return commands, malformed


def execute_repository_validation(
    *,
    provider: object,
    prepared_environment: PreparedEnvironment,
    configuration: Mapping[str, Any],
    run_id: str,
    attempt_id: str,
    candidate_id: str,
    baseline_sha256: str,
    retry_count: int = 0,
) -> RepositoryValidationReport:
    commands, malformed = _configured_commands(configuration)
    results: list[RepositoryValidationCommandResult] = []
    for validation_id, argv in commands:
        result = execute_candidate_command(
            provider=provider,
            prepared_environment=prepared_environment,
            argv=argv,
            command_role="repository_validation",
            run_id=run_id,
            attempt_id=attempt_id,
            validation_id=validation_id,
            baseline_sha256=baseline_sha256,
            candidate_state="post_mutation",
        )
        results.append(
            RepositoryValidationCommandResult.model_validate(
                result.model_dump(mode="json")
            )
        )

    failure_code: RepositoryValidationFailureCode | None
    if malformed:
        status = "infrastructure_error"
        authoritative = False
        failure_code = "repository_validation_malformed_result"
    elif not results:
        status = "unavailable"
        authoritative = False
        failure_code = "repository_validation_unavailable"
    elif any(
        item.status in {"timed_out", "infrastructure_error", "policy_denied"}
        for item in results
    ):
        status = "infrastructure_error"
        authoritative = False
        failure_code = next(
            (
                item.failure_code
                for item in results
                if item.status in {"timed_out", "infrastructure_error", "policy_denied"}
                and item.failure_code is not None
            ),
            "repository_validation_provider_failure",
        )
    elif any(item.status == "failed" for item in results):
        status = "failed"
        authoritative = True
        failure_code = "repository_validation_test_failure"
    else:
        status = "passed"
        authoritative = True
        failure_code = "repository_validation_passed"

    return RepositoryValidationReport(
        schema_version="villani.repository_validation.v2",
        run_id=run_id,
        attempt_id=attempt_id,
        candidate_id=candidate_id,
        execution_environment_fingerprint=prepared_environment.fingerprint,
        execution_provider=prepared_environment.provider,
        commands=results,
        status=status,  # type: ignore[arg-type]
        authoritative=authoritative,
        completed_at=_now(),
        retry_count=retry_count,
        failure_code=failure_code,
    )


def invalidate_repository_validation(
    report: RepositoryValidationReport,
    *,
    failure_code: RepositoryValidationFailureCode,
) -> RepositoryValidationReport:
    commands = [
        item.model_copy(
            update={
                "status": "infrastructure_error",
                "failure_code": failure_code,
            }
        )
        for item in report.commands
    ]
    return report.model_copy(
        update={
            "commands": commands,
            "status": "infrastructure_error",
            "authoritative": False,
            "completed_at": _now(),
            "failure_code": failure_code,
        }
    )


def load_repository_validation_report(
    attempt_directory: Path,
) -> RepositoryValidationReport | None:
    path = Path(attempt_directory) / "repository-validation.json"
    if not path.is_file():
        return None
    return RepositoryValidationReport.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def repository_validation_runtime_events(
    report: RepositoryValidationReport,
) -> tuple[RuntimeEvent, ...]:
    events: list[RuntimeEvent] = []
    for command in report.commands:
        started = datetime.fromisoformat(command.started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(command.completed_at.replace("Z", "+00:00"))
        common = {
            "run_id": report.run_id,
            "attempt_id": report.attempt_id,
            "validation_id": command.validation_id,
            "argv": list(command.argv),
            "command_role": command.command_role,
            "worktree_path": command.worktree_path,
            "baseline_sha256": command.baseline_sha256,
            "candidate_state": command.candidate_state,
            "execution_environment_fingerprint": (
                command.execution_environment_fingerprint
            ),
            "execution_provider": command.execution_provider,
        }
        source_prefix = (
            f"repository-validation:{report.attempt_id}:"
            f"{report.retry_count}:{command.validation_id}"
        )
        events.append(
            RuntimeEvent(
                event_type="repository_validation_started",
                timestamp=started,
                payload={
                    **common,
                    "exit_code": None,
                    "duration_ms": 0,
                    "failure_code": None,
                },
                source_event_id=f"{source_prefix}:started",
            )
        )
        event_type = (
            "repository_validation_completed"
            if command.status == "passed"
            else "repository_validation_failed"
            if command.status == "failed"
            else "repository_validation_infrastructure_error"
        )
        events.append(
            RuntimeEvent(
                event_type=event_type,
                timestamp=completed,
                payload={
                    **common,
                    "exit_code": command.exit_code,
                    "duration_ms": command.duration_ms,
                    "failure_code": command.failure_code,
                },
                source_event_id=f"{source_prefix}:completed",
            )
        )
    return tuple(events)
