from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from villani_ops.closed_loop.agent_systems.acp import ACPClient, _inside
from villani_ops.closed_loop.agent_systems.configuration import (
    build_agent_system_identities,
)
from villani_ops.closed_loop.agent_systems.discovery import (
    CLAUDE_CODE_SUPPORTED_VERSION_RANGE,
    CODEX_SUPPORTED_VERSION_RANGE,
    claude_version_supported,
    codex_version_supported,
    discover_harness,
)
from villani_ops.core.backend import Backend
from villani_ops.runners.base import RunnerContext
from villani_ops.runners.claude_code import ClaudeCodeRunner
from villani_ops.runners.codex_app_server import CodexAppServerRunner


FIXTURES = Path(__file__).parent / "fixtures" / "pt6"
FAKE = FIXTURES / "fake_structured_harness.py"


@pytest.fixture(autouse=True)
def executable_fake() -> None:
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR)


def _backend(harness: str) -> Backend:
    return Backend(
        name=harness,
        provider="openai" if harness == "codex" else "anthropic",
        model="fixture-model",
        roles=["coding"],
        capability_score=50,
        command_name=str(FAKE),
        metadata={"reasoning_effort": "medium"},
    )


def _context(
    tmp_path: Path,
    harness: str,
    scenario: str,
    *,
    cancellation: threading.Event | None = None,
) -> RunnerContext:
    root = tmp_path / "repo space 雪"
    attempt = tmp_path / "attempt space 雪"
    root.mkdir(parents=True, exist_ok=True)
    attempt.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "PT6_FAKE_HARNESS": harness,
            "PT6_SCENARIO": scenario,
            "PT6_SECRET": "pt6-super-secret-value",
            "PT6_API_TOKEN": "pt6-super-secret-value",
        }
    )
    return RunnerContext(
        attempt_id="attempt-1",
        repo_path=str(root),
        task_instruction="Create the requested fixture. 雪",
        success_criteria="The requested file exists.",
        backend=_backend(harness),
        timeout_seconds=5,
        run_dir=str(attempt),
        env=environment,
        inherit_parent_environment=False,
        cancellation_event=cancellation or threading.Event(),
        candidate_dimensions={"agent": harness, "model": "fixture-model"},
    )


def _runner(harness: str):
    if harness == "codex":
        return CodexAppServerRunner(command=str(FAKE), expected_version="0.144.5")
    return ClaudeCodeRunner(
        command=str(FAKE),
        expected_version="2.1.138",
        strict_native_sandbox_available=True,
    )


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_successful_patch_identity_usage_and_non_ascii_spaced_path(
    tmp_path: Path, harness: str
) -> None:
    context = _context(tmp_path, harness, "success")
    result = _runner(harness).run(context)
    assert result.exit_code == 0, result.stderr
    assert (Path(context.repo_path) / "answer 雪.txt").read_text() == "patched\n"
    identity = result.telemetry["harness_execution_identity"]
    assert identity["harness_id"] == harness
    assert identity["harness_version"] in {"0.144.5", "2.1.138"}
    assert identity["model_id"]
    assert identity["provider"] in {"openai", "anthropic"}
    assert result.token_accounting_status == "verified"
    assert not (Path(context.run_dir) / "claude-input.txt").exists()


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_permissions_are_worktree_scoped_and_target_is_not_mutated(
    tmp_path: Path, harness: str
) -> None:
    target = tmp_path / "target repository" / "protected.txt"
    target.parent.mkdir()
    target.write_text("immutable\n", encoding="utf-8")
    context = _context(tmp_path / "attempt", harness, "success")
    context.task_instruction = (
        f"Do not modify the target at {target}; edit the candidate."
    )
    result = _runner(harness).run(context)
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8") == "immutable\n"
    if harness == "codex":
        messages = [
            json.loads(line)["message"]
            for line in (Path(result.debug_artifact_dir or "") / "raw-protocol.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        thread_start = next(
            message for message in messages if message.get("method") == "thread/start"
        )
        assert thread_start["params"]["sandbox"] == "workspace-write"
        assert thread_start["params"]["approvalPolicy"] == "never"
        assert (
            Path(thread_start["params"]["cwd"]).resolve()
            == Path(context.repo_path).resolve()
        )
    else:
        settings = json.loads(
            (Path(context.run_dir) / "claude-settings.json").read_text(encoding="utf-8")
        )
        assert settings["sandbox"] == {
            "allowUnsandboxedCommands": False,
            "enabled": True,
            "failIfUnavailable": True,
        }


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_no_patch_is_successful_and_cost_is_unknown(
    tmp_path: Path, harness: str
) -> None:
    context = _context(tmp_path, harness, "no_patch")
    result = _runner(harness).run(context)
    assert result.exit_code == 0
    assert list(Path(context.repo_path).iterdir()) == []
    assert result.total_cost is None
    assert result.cost_accounting_status == "unknown"


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
@pytest.mark.parametrize(
    ("scenario", "failure"),
    [
        ("malformed", "malformed"),
        ("large_output", "oversized"),
        ("missing_final", "missing_final"),
        ("partial_crash", "missing_final"),
        ("rate_limit", "rate_limited"),
    ],
)
def test_protocol_failures_are_structured_and_partial_patch_is_preserved(
    tmp_path: Path, harness: str, scenario: str, failure: str
) -> None:
    context = _context(tmp_path, harness, scenario)
    result = _runner(harness).run(context)
    assert result.exit_code != 0
    assert result.failure_code is not None
    assert failure in result.failure_code, result.failure_code
    if scenario == "partial_crash":
        assert (Path(context.repo_path) / "partial.txt").is_file()
    if scenario == "rate_limit":
        assert result.failure_retryable is True


def test_codex_schema_change_and_unsupported_version_fail_closed(
    tmp_path: Path,
) -> None:
    schema_context = _context(tmp_path / "schema", "codex", "schema_change")
    schema_result = _runner("codex").run(schema_context)
    assert schema_result.failure_code == "codex_schema_change"

    version_context = _context(tmp_path / "version", "codex", "success")
    version_context.env["PT6_FAKE_VERSION"] = "codex-cli 0.145.0"
    version_result = _runner("codex").run(version_context)
    assert version_result.failure_code == "codex_version_changed"


def test_claude_known_cost_is_authoritative_and_per_model(tmp_path: Path) -> None:
    context = _context(tmp_path, "claude-code", "known_cost")
    result = _runner("claude-code").run(context)
    assert result.total_cost == pytest.approx(0.0125)
    assert result.cost_currency == "USD"
    assert result.cost_accounting_status == "complete"
    assert result.cost_source == "claude_code_authoritative_total_cost_usd"
    assert result.per_model_usage["claude-fixture"]["costUSD"] == pytest.approx(0.0125)


def test_claude_resume_is_scoped_to_the_same_attempt(tmp_path: Path) -> None:
    first = _context(tmp_path / "first", "claude-code", "success")
    second = _context(tmp_path / "second", "claude-code", "success")
    second.attempt_id = "attempt-2"
    runner = ClaudeCodeRunner(
        command=str(FAKE),
        expected_version="2.1.138",
        strict_native_sandbox_available=True,
        resume_same_attempt=True,
    )
    settings = Path(first.run_dir) / "settings.json"
    initial = runner._arguments(first, settings)  # noqa: SLF001
    assert "--session-id" in initial
    assert "--resume" not in initial
    assert "--no-session-persistence" not in initial
    runner._resume_sessions[first.attempt_id] = "same-attempt-session"  # noqa: SLF001
    resumed = runner._arguments(first, settings)  # noqa: SLF001
    assert resumed[resumed.index("--resume") + 1] == "same-attempt-session"
    other = runner._arguments(second, settings)  # noqa: SLF001
    assert "same-attempt-session" not in other
    assert "--session-id" in other


def test_claude_large_context_uses_controlled_file_without_truncation(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, "claude-code", "success")
    context.task_instruction = "雪" * 700_000
    result = _runner("claude-code").run(context)
    assert result.exit_code == 0
    metadata = json.loads(
        (Path(context.run_dir) / "claude-input-metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["size_bytes"] > 2_000_000
    assert metadata["sha256"].startswith("sha256:")
    assert not (Path(context.run_dir) / "claude-input.txt").exists()


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_command_recovery_permission_and_secret_redaction(
    tmp_path: Path, harness: str
) -> None:
    recovery_context = _context(tmp_path / "recovery", harness, "command_recovery")
    recovery = _runner(harness).run(recovery_context)
    command_events = [
        event
        for event in recovery.runtime_events
        if event.get("event_type") == "command_completed"
    ]
    assert len(command_events) == 2

    permission_context = _context(tmp_path / "permission", harness, "permission")
    permission = _runner(harness).run(permission_context)
    assert permission.exit_code == 0
    assert {event["event_type"] for event in permission.runtime_events}.issuperset(
        {"permission_request", "permission_resolution"}
    )

    secret_context = _context(tmp_path / "secret", harness, "secret")
    secret = _runner(harness).run(secret_context)
    raw_path = Path(secret.debug_artifact_dir or "") / (
        "raw-protocol.jsonl" if harness == "codex" else "raw-stream.jsonl"
    )
    assert "pt6-super-secret-value" not in raw_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_cancellation_is_bounded_and_cleans_up_process(
    tmp_path: Path, harness: str
) -> None:
    cancellation = threading.Event()
    context = _context(tmp_path, harness, "cancel", cancellation=cancellation)
    timer = threading.Timer(0.25, cancellation.set)
    timer.start()
    started = time.monotonic()
    try:
        result = _runner(harness).run(context)
    finally:
        timer.cancel()
    assert result.cancelled is True
    assert result.exit_code == 130
    assert time.monotonic() - started < 4


def test_fake_discovery_reports_exact_versions_auth_protocol_and_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PT6_FAKE_HARNESS", "codex")
    codex = discover_harness("codex", str(FAKE))
    assert codex.readiness.exact_version == "0.144.5"
    assert codex.readiness.authentication_status == "ready"
    assert codex.readiness.version_supported is True
    assert codex.readiness.supported_version_range == CODEX_SUPPORTED_VERSION_RANGE
    monkeypatch.setenv("PT6_FAKE_HARNESS", "claude-code")
    claude = discover_harness("claude-code", str(FAKE))
    assert claude.readiness.exact_version == "2.1.138"
    assert claude.readiness.authentication_status == "ready"
    assert (
        claude.readiness.supported_version_range == CLAUDE_CODE_SUPPORTED_VERSION_RANGE
    )
    assert codex_version_supported("0.144.5")
    assert not codex_version_supported("0.145.0")
    assert claude_version_supported("2.1.138")
    assert not claude_version_supported("2.2.0")


@pytest.mark.parametrize(
    ("harness", "provider", "strict"),
    [("codex", "openai", False), ("claude-code", "anthropic", True)],
)
def test_compatible_configured_system_is_provisional_not_qualified(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    provider: str,
    strict: bool,
) -> None:
    monkeypatch.setenv("PT6_FAKE_HARNESS", harness)
    backend = _backend(harness).model_copy(update={"provider": provider})
    entry = {
        "backend": harness,
        "production_enabled": True,
        "qualification_status": "qualified",
        "strict_native_sandbox_conformance": strict,
        "model_conformance": {
            "status": "passed",
            "harness_version": "0.144.5" if harness == "codex" else "2.1.138",
            "provider": provider,
            "model": "fixture-model",
            "protocol": (
                "codex-app-server-jsonrpc-stdio"
                if harness == "codex"
                else "claude-code-stream-json"
            ),
            "report_digest": f"sha256:{'0' * 64}",
        },
        "harness": {
            "id": harness,
            "command": str(FAKE),
        },
    }
    configuration = {
        "backends": {
            harness: backend.model_dump(mode="json", exclude={"name", "api_key"})
        },
        "execution_environment": {"provider": "inherit"},
        "agent_systems": {
            "schema_version": "villani.agent_system_configuration.v1",
            "systems": {harness: entry},
        },
    }
    identity = build_agent_system_identities(configuration, {harness: backend})[0][0]
    assert identity.production_enabled is True
    assert identity.qualification_status == "provisional"
    assert identity.readiness is not None
    assert identity.readiness.authentication_status == "ready"
    assert identity.capabilities["custom_model"].state.value == "supported"
    assert identity.capabilities["custom_provider"].state.value == "unsupported"
    assert identity.capabilities["local_model"].state.value == "unsupported"
    assert all(
        reference.reference != "Gate C"
        for reference in identity.qualification_references
    )


def test_official_format_event_fixtures_parse_without_screen_scraping() -> None:
    codex_events = [
        json.loads(line)
        for line in (FIXTURES / "codex-events.jsonl").read_text().splitlines()
    ]
    parsed = CodexAppServerRunner._event_from_item(  # noqa: SLF001
        codex_events[0]["method"], codex_events[0]["params"]
    )
    assert parsed and parsed["event_type"] == "command_started"
    claude_events = [
        json.loads(line)
        for line in (FIXTURES / "claude-events.jsonl").read_text().splitlines()
    ]
    tool_names: dict[str, str] = {}
    assert (
        ClaudeCodeRunner._content_events(  # noqa: SLF001
            claude_events[1], tool_names
        )[0]["event_type"]
        == "command_started"
    )
    assert (
        ClaudeCodeRunner._content_events(  # noqa: SLF001
            claude_events[2], tool_names
        )[0]["event_type"]
        == "command_completed"
    )


def test_acp_initialization_session_updates_cancellation_and_path_safety(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "acp worktree 雪"
    worktree.mkdir()
    environment = {**os.environ, "PT6_FAKE_HARNESS": "acp", "PT6_SCENARIO": "success"}
    trace = tmp_path / "acp-events.jsonl"
    client = ACPClient(
        [sys.executable, str(FAKE)],
        worktree,
        environment,
        trace_path=trace,
    )
    try:
        initialized = client.start()
        assert initialized["protocolVersion"] == 1
        assert client.new_session() == "acp-session-pt6"
        result = client.prompt("Do the task", timeout_seconds=2)
        assert result.stop_reason == "end_turn"
        assert result.updates
    finally:
        client.close()
    assert trace.is_file()
    with pytest.raises(Exception, match="outside the isolated worktree"):
        _inside(worktree, worktree.parent / "outside.txt")

    cancel_event = threading.Event()
    cancel_environment = {
        **environment,
        "PT6_SCENARIO": "cancel",
    }
    cancel_client = ACPClient([sys.executable, str(FAKE)], worktree, cancel_environment)
    try:
        cancel_client.start()
        cancel_client.new_session()
        timer = threading.Timer(0.1, cancel_event.set)
        timer.start()
        try:
            cancelled = cancel_client.prompt(
                "Cancel", timeout_seconds=2, cancellation_event=cancel_event
            )
        finally:
            timer.cancel()
        assert cancelled.stop_reason == "cancelled"
    finally:
        cancel_client.close()


@pytest.mark.e2e
@pytest.mark.parametrize("harness", ["codex", "claude-code"])
def test_opt_in_real_harness_smoke(tmp_path: Path, harness: str) -> None:
    if os.environ.get("VILLANI_REAL_HARNESS_TESTS") != "1":
        pytest.skip("VILLANI_REAL_HARNESS_TESTS is not set to 1.")
    command = "codex" if harness == "codex" else "claude"
    discovery = discover_harness(harness, command)
    readiness = discovery.readiness
    if not readiness.installed:
        pytest.skip(f"{discovery.display_name} binary is not installed.")
    if readiness.authentication_status != "ready":
        pytest.skip(f"{discovery.display_name} authentication is not ready.")
    if not readiness.version_supported:
        pytest.skip(
            f"{discovery.display_name} {readiness.exact_version} is outside {readiness.supported_version_range}."
        )
    if harness == "claude-code" and not readiness.details.get(
        "strict_sandbox_available"
    ):
        pytest.skip(
            "Claude Code strict sandboxing is unavailable on this native host; use WSL2 or a container."
        )
    context = _context(tmp_path, harness, "real")
    context.env = dict(os.environ)
    context.backend = context.backend.model_copy(
        update={"model": os.environ.get("VILLANI_REAL_HARNESS_MODEL", "default")}
    )
    runner = (
        CodexAppServerRunner(
            command=command, expected_version=readiness.exact_version or "unknown"
        )
        if harness == "codex"
        else ClaudeCodeRunner(
            command=command,
            expected_version=readiness.exact_version or "unknown",
            strict_native_sandbox_available=True,
        )
    )
    result = runner.run(context)
    assert result.exit_code == 0, result.stderr
