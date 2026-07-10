"""Deterministic failure categories for closed-loop policy decisions."""

from __future__ import annotations

from typing import Literal, TypeAlias

from .interfaces import AttemptResult, Verification


FailureCategory: TypeAlias = Literal[
    "infrastructure_failure",
    "implementation_failure",
    "capability_failure",
    "verification_failure",
    "no_change_failure",
    "materialization_failure",
]
RunnerFailureCategory: TypeAlias = Literal[
    "executable_not_found",
    "provider_config_error",
    "backend_connection_error",
    "backend_auth_error",
    "backend_rate_limited",
    "runner_nonzero_exit",
]


def classify_runner_failure(
    exit_code: int | None, stdout: str = "", stderr: str = ""
) -> RunnerFailureCategory:
    """Classify a Villani Code process failure from its exit and safe output."""

    text = f"{stderr}\n{stdout}".lower()
    if exit_code == 127 or any(
        marker in text
        for marker in (
            "was not found",
            "not recognized as",
            "command not found",
            "no such file or directory",
            "cannot find the path",
            "could not be found",
        )
    ):
        return "executable_not_found"
    if any(
        marker in text
        for marker in (
            "invalid provider",
            "unknown provider",
            "base_url",
            "base url",
            "base-url",
            "missing option '--base-url'",
            "missing option '--model'",
            "invalid value for '--provider'",
            "must be one of",
            "no resolved api key",
            "requires an api key",
        )
    ):
        return "provider_config_error"
    if any(
        marker in text
        for marker in ("429", "rate limit", "rate_limit", "too many requests")
    ):
        return "backend_rate_limited"
    if any(
        marker in text
        for marker in (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "authentication",
            "invalid api key",
            "invalid_api_key",
        )
    ):
        return "backend_auth_error"
    if any(
        marker in text
        for marker in (
            "connection refused",
            "connecterror",
            "connection error",
            "timed out",
            "timeout",
            "name or service not known",
            "temporary failure in name resolution",
            "name resolution",
            "getaddrinfo failed",
            "nameresolutionerror",
            "nodename nor servname",
            "dns",
            "network is unreachable",
        )
    ):
        return "backend_connection_error"
    return "runner_nonzero_exit"


_INFRASTRUCTURE_CODES = (
    "command_not_found",
    "executable",
    "endpoint",
    "connection",
    "authentication",
    "configuration",
    "isolation",
    "runner_exception",
    "executable_not_found",
    "provider_config_error",
    "backend_connection_error",
    "backend_auth_error",
    "backend_rate_limited",
    "runner_command_not_found",
)
_VERIFICATION_STATUSES = {
    "timed_out",
    "timeout",
    "malformed_output",
    "missing_compatible_trace",
    "missing_trace",
    "error",
    "failed",
}
_CAPABILITY_MARKERS = (
    "capability_failure",
    "insufficient capability",
    "lacks the required capability",
    "unable to satisfy the required behavior",
    "model capability is insufficient",
)


def material_progress(attempt: AttemptResult) -> bool:
    if attempt.patch and attempt.patch.strip():
        return True
    metrics = attempt.metadata.get("runner_metrics")
    telemetry = metrics if isinstance(metrics, dict) else attempt.runner_telemetry
    return any(
        int(telemetry.get(name) or 0) > 0
        for name in (
            "model_requests",
            "total_file_reads",
            "total_file_writes",
            "commands_executed",
        )
    )


def _infrastructure_failure(attempt: AttemptResult) -> bool:
    declared = str(attempt.metadata.get("failure_classification") or "").lower()
    if declared == "infrastructure_failure" or attempt.exit_code == 127:
        return True
    code = str(attempt.error.code if attempt.error else "").lower()
    message = str(attempt.error.message if attempt.error else "").lower()
    if any(marker in code or marker in message for marker in _INFRASTRUCTURE_CODES):
        return True
    timeout = "timeout" in code or "timed out" in message
    return timeout and not material_progress(attempt)


def _verification_failure(verification: Verification) -> bool:
    invocation = str(verification.metadata.get("invocation_status") or "").lower()
    blockers = " ".join(verification.risk_flags).lower()
    return bool(
        verification.outcome == "error"
        or verification.recommended_action == "retry_verifier"
        or invocation in _VERIFICATION_STATUSES
        or "malformed_verifier_output" in blockers
        or "missing_compatible_trace" in blockers
        or "verifier_error" in blockers
    )


def _capability_failure(verification: Verification) -> bool:
    evidence = " ".join(
        [
            verification.reason,
            *verification.risk_flags,
            *(item.summary for item in verification.failure_evidence),
        ]
    ).lower()
    explicit_metadata = verification.metadata.get("failure_category")
    return explicit_metadata == "capability_failure" or any(
        marker in evidence for marker in _CAPABILITY_MARKERS
    )


def classify_failure(
    attempt: AttemptResult,
    verification: Verification | None = None,
    *,
    requires_file_changes: bool = True,
) -> FailureCategory | None:
    """Classify evidence without treating a generic nonzero exit as incapability."""

    if _infrastructure_failure(attempt):
        return "infrastructure_failure"
    if requires_file_changes and not (attempt.patch and attempt.patch.strip()):
        return "no_change_failure"
    if verification is not None and _verification_failure(verification):
        return "verification_failure"
    if verification is not None and _capability_failure(verification):
        return "capability_failure"
    if verification is not None and not verification.acceptance_eligible:
        return "implementation_failure"
    if attempt.status != "completed" or attempt.exit_code not in {0, None}:
        return "implementation_failure" if material_progress(attempt) else None
    return None
