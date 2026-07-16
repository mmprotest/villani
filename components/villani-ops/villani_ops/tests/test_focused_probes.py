from __future__ import annotations

from pathlib import Path

import pytest

from villani_ops.closed_loop.focused_probes import execute_focused_probes
from villani_ops.closed_loop.verification_evidence import FocusedProbeRequest
from villani_ops.execution_environment.models import (
    CommandResult,
    ExecutionEnvironmentConfig,
    PreparedEnvironment,
)
from villani_ops.execution_environment.security import ExecutionPolicyDenied


def _command(
    *,
    exit_code: int = 0,
    stdout: str = "ok",
    stderr: str = "",
    timed_out: bool = False,
    failure_classification: str | None = None,
) -> CommandResult:
    return CommandResult(
        exit_code=exit_code,
        duration_ms=2,
        stdout=stdout,
        stderr=stderr,
        stdout_bytes=len(stdout.encode()),
        stderr_bytes=len(stderr.encode()),
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=timed_out,
        disk_limit_exceeded=False,
        process_limit_exceeded=False,
        failure_classification=failure_classification,
    )


class Provider:
    name = "inherit"

    def __init__(
        self,
        result: CommandResult | Exception,
        *,
        fingerprint: str = "fingerprint",
    ) -> None:
        self.config = ExecutionEnvironmentConfig(provider="inherit")
        self.result = result
        self.observed_fingerprint = fingerprint
        self.calls: list[tuple[PreparedEnvironment, list[str]]] = []

    def fingerprint(self, _repository: Path) -> str:
        return self.observed_fingerprint

    def execute(
        self,
        prepared: PreparedEnvironment,
        argv: list[str],
    ) -> CommandResult:
        self.calls.append((prepared, argv))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _prepared(tmp_path: Path) -> PreparedEnvironment:
    repository = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repository.mkdir()
    worktree.mkdir()
    return PreparedEnvironment(
        provider="inherit",
        provider_version="test",
        repository_path=str(repository),
        worktree_path=str(worktree),
        environment={"PATH": "candidate-path"},
        removals=[],
        fingerprint="fingerprint",
        cache_key=None,
        cache_hit=False,
        setup_result=None,
        inspection={},
    )


def _request(
    *,
    expected_exit_code: int = 0,
    expected_stdout: str | None = "ok",
) -> FocusedProbeRequest:
    return FocusedProbeRequest(
        probe_id="probe-1",
        requirement_ids=["req-1"],
        argv=["candidate-probe", "--check"],
        timeout_seconds=30,
        expected_exit_code=expected_exit_code,
        expected_stdout=expected_stdout,
        expected_stdout_contains=[],
        expected_stderr_contains=[],
        reason="exact observable behavior",
    )


def _run(
    tmp_path: Path,
    provider: Provider,
    request: FocusedProbeRequest | None = None,
):
    return execute_focused_probes(
        provider=provider,
        prepared_environment=_prepared(tmp_path),
        requests=[request or _request()],
        run_id="run-1",
        attempt_id="attempt-1",
        candidate_id="attempt-1",
        baseline_sha256="a" * 64,
    )


def test_focused_probe_uses_provider_execute_and_exact_environment(
    tmp_path: Path,
) -> None:
    provider = Provider(_command())
    report = _run(tmp_path, provider)

    assert report.status == "passed"
    assert len(provider.calls) == 1
    prepared, argv = provider.calls[0]
    assert prepared.fingerprint == report.execution_environment_fingerprint
    assert prepared.environment["PATH"] == "candidate-path"
    assert argv == ["candidate-probe", "--check"]


def test_expected_nonzero_exit_can_be_a_passing_probe(tmp_path: Path) -> None:
    provider = Provider(_command(exit_code=2, stdout="", stderr="expected"))
    report = _run(
        tmp_path,
        provider,
        _request(expected_exit_code=2, expected_stdout=""),
    )
    assert report.status == "passed"


def test_probe_timeout_is_infrastructure_error(tmp_path: Path) -> None:
    provider = Provider(
        _command(
            exit_code=124,
            timed_out=True,
            failure_classification="timeout",
        )
    )
    report = _run(tmp_path, provider)

    assert report.status == "infrastructure_error"
    assert report.failure_code == "focused_probe_timeout"
    assert report.results[0].status == "infrastructure_error"


def test_probe_policy_denial_is_infrastructure_error(tmp_path: Path) -> None:
    provider = Provider(
        ExecutionPolicyDenied(
            policy="command",
            action="candidate-probe",
            reason="denied by test policy",
        )
    )
    report = _run(tmp_path, provider)

    assert report.status == "infrastructure_error"
    assert report.failure_code == "focused_probe_policy_denied"


def test_probe_environment_mismatch_fails_closed(tmp_path: Path) -> None:
    provider = Provider(_command(), fingerprint="different")
    report = _run(tmp_path, provider)

    assert report.status == "infrastructure_error"
    assert report.failure_code == "focused_probe_environment_mismatch"
    assert provider.calls == []


def test_probe_request_rejects_empty_argv() -> None:
    with pytest.raises(ValueError):
        FocusedProbeRequest(
            probe_id="probe-1",
            requirement_ids=["req-1"],
            argv=[],
            timeout_seconds=30,
            expected_exit_code=0,
            expected_stdout=None,
            expected_stdout_contains=[],
            expected_stderr_contains=[],
            reason="invalid",
        )
