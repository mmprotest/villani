from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from villani_ops.execution_environment import (
    CandidateCommandResult,
    ExecutionEnvironmentConfig,
    InheritProvider,
    SetupCommandProvider,
    SetupLimits,
    execute_candidate_command,
    inspect_repository,
)
from villani_ops.cli.unified import _doctor_report
from villani_ops.execution_environment.secrets import (
    register_secret_values,
    registered_secret_values,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_explicit_registered_secret_environment_names_are_resolved_at_use_time(
    monkeypatch,
) -> None:
    monkeypatch.setenv("RELEASE_CANARY", "runtime-only-secret-value")
    monkeypatch.setenv(
        "VILLANI_REGISTERED_SECRET_ENV_VARS", " RELEASE_CANARY,UNSET_SECRET "
    )

    values = registered_secret_values()

    assert "runtime-only-secret-value" in values
    assert "RELEASE_CANARY" not in values


def test_inherit_preserves_repository_and_user_toolchains_but_removes_private_and_secrets(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "attempt"
    repo_toolchain = repo / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    user_toolchain = tmp_path / "user-tools" / "bin"
    private = tmp_path / "villani-runtime"
    for path in (repo_toolchain, user_toolchain, private / "bin", worktree):
        path.mkdir(parents=True)
    source = {
        "PATH": os.pathsep.join(
            (str(private / "bin"), str(repo_toolchain), str(user_toolchain))
        ),
        "OPENAI_API_KEY": "must-not-be-recorded",
        "VILLANI_RUNTIME_ROOT": str(private),
        "VIRTUAL_ENV": str(repo / ".venv"),
        "USER_SETTING": "kept",
    }
    provider = InheritProvider(
        ExecutionEnvironmentConfig(private_paths=[str(private)]),
        source_environment=source,
    )

    prepared = provider.prepare(repository=repo, worktree=worktree)
    environment = provider.command_environment(prepared)

    assert environment["PATH"].split(os.pathsep) == [
        str(repo_toolchain),
        str(user_toolchain),
    ]
    assert environment["VIRTUAL_ENV"] == str(repo / ".venv")
    assert environment["USER_SETTING"] == "kept"
    assert "OPENAI_API_KEY" not in environment
    assert "VILLANI_RUNTIME_ROOT" not in environment
    report = json.dumps(prepared.durable_report())
    assert "must-not-be-recorded" not in report
    assert {item.name for item in prepared.removals} >= {
        "PATH",
        "OPENAI_API_KEY",
        "VILLANI_RUNTIME_ROOT",
    }


def test_setup_cache_changes_with_lockfile_and_never_caches_worktree(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("example==1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "requirements.txt")
    _git(repo, "commit", "-m", "baseline")
    config = ExecutionEnvironmentConfig(
        provider="setup-command",
        setup_argv=[
            sys.executable,
            "-c",
            "from pathlib import Path; Path('setup.marker').write_text('ok')",
        ],
    )
    cache = tmp_path / "cache"
    provider = SetupCommandProvider(
        config,
        source_environment={"PATH": os.environ.get("PATH", "")},
        cache_root=cache,
    )

    first_tree = tmp_path / "attempt-1"
    first_tree.mkdir()
    first = provider.prepare(repository=repo, worktree=first_tree)
    assert not first.cache_hit and (first_tree / "setup.marker").is_file()

    second_tree = tmp_path / "attempt-2"
    second_tree.mkdir()
    second = provider.prepare(repository=repo, worktree=second_tree)
    assert second.cache_hit
    assert (second_tree / "setup.marker").is_file()
    assert not any(path.is_dir() for path in cache.rglob("attempt-*"))

    (repo / "requirements.txt").write_text("example==2\n", encoding="utf-8")
    third_tree = tmp_path / "attempt-3"
    third_tree.mkdir()
    third = provider.prepare(repository=repo, worktree=third_tree)
    assert not third.cache_hit
    assert third.cache_key != first.cache_key
    assert (third_tree / "setup.marker").is_file()


def test_repository_inspection_is_advisory_and_detects_supported_ecosystems(
    tmp_path: Path,
) -> None:
    for name in (
        "pyproject.toml",
        "package-lock.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "flake.nix",
    ):
        (tmp_path / name).write_text("pytest\n", encoding="utf-8")
    (tmp_path / ".devcontainer").mkdir()
    (tmp_path / ".devcontainer" / "devcontainer.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / ".villani.yaml").write_text(
        "execution_environment: {}\n", encoding="utf-8"
    )

    report = inspect_repository(tmp_path)

    assert {item["name"] for item in report["ecosystems"]} >= {
        "python",
        "node",
        "cargo",
        "go",
        "maven",
        "gradle",
    }
    assert report["recommendations_are_advisory"] is True
    assert report["inferred_commands_executed"] is False
    assert report["explicit_villani_config"] == [".villani.yaml"]


def test_shell_setup_requires_separate_explicit_configuration() -> None:
    try:
        ExecutionEnvironmentConfig(
            provider="setup-command", setup_argv=["echo", "ok"], shell=True
        )
    except ValueError as error:
        assert "shell_command" in str(error)
    else:  # pragma: no cover
        raise AssertionError("shell setup was accepted without shell_command")


def test_setup_command_bounds_output_and_timeout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    config = ExecutionEnvironmentConfig(
        provider="setup-command",
        setup_argv=[sys.executable, "-c", "pass"],
        cache=False,
        limits=SetupLimits(timeout_seconds=1, stdout_bytes=32, stderr_bytes=16),
    )
    provider = SetupCommandProvider(config, source_environment={})
    prepared = provider.prepare(repository=repo, worktree=worktree)

    output = provider.execute(prepared, [sys.executable, "-c", "print('x' * 1000)"])
    assert output.exit_code == 0
    assert output.stdout_truncated is True
    assert len(output.stdout.encode()) <= 32

    timed = provider.execute(
        prepared, [sys.executable, "-c", "import time; time.sleep(5)"]
    )
    assert timed.exit_code == 124
    assert timed.timed_out is True


def test_candidate_execution_reuses_exact_path_fingerprint_and_private_tool(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    tools = tmp_path / "prepared-tools"
    repo.mkdir()
    worktree.mkdir()
    tools.mkdir()
    tool_name = (
        f"candidate-tool-{uuid.uuid4().hex}.exe"
        if os.name == "nt"
        else f"candidate-tool-{uuid.uuid4().hex}"
    )
    tool = tools / tool_name
    source_tool = shutil.which("whoami.exe" if os.name == "nt" else "true")
    assert source_tool is not None
    shutil.copy2(source_tool, tool)
    tool.chmod(tool.stat().st_mode | 0o111)
    path_value = str(tools)
    provider = InheritProvider(
        ExecutionEnvironmentConfig(),
        source_environment={"PATH": path_value},
    )
    prepared = provider.prepare(repository=repo, worktree=worktree)

    assert shutil.which(tool_name, path=os.environ.get("PATH", "")) is None
    result = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[tool_name],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="prepared_path",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )

    assert isinstance(result, CandidateCommandResult)
    assert result.status == "passed"
    assert result.execution_environment_fingerprint == prepared.fingerprint
    assert result.worktree_path == str(worktree.resolve())

    path_result = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[
            sys.executable,
            "-c",
            "import os; print(os.environ.get('PATH', ''))",
        ],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="exact_path",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )
    assert path_result.status == "passed"
    assert path_result.stdout.strip() == path_value


def test_setup_command_state_is_reused_by_candidate_execution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    provider = SetupCommandProvider(
        ExecutionEnvironmentConfig(
            provider="setup-command",
            setup_argv=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('setup.marker').write_text('ready')",
            ],
            cache=False,
        ),
        source_environment={"PATH": os.environ.get("PATH", "")},
    )
    prepared = provider.prepare(repository=repo, worktree=worktree)

    result = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('setup.marker').read_text() == 'ready'",
        ],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="setup_reused",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )

    assert result.status == "passed"
    assert result.execution_environment_fingerprint == prepared.fingerprint


def test_candidate_execution_distinguishes_test_and_infrastructure_failures(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    provider = InheritProvider(
        ExecutionEnvironmentConfig(limits={"timeout_seconds": 1}),
        source_environment={"PATH": os.environ.get("PATH", "")},
    )
    prepared = provider.prepare(repository=repo, worktree=worktree)

    failed = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[sys.executable, "-c", "raise SystemExit(3)"],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="test_failure",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )
    candidate_missing_artifact = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('No such file or directory'); raise SystemExit(2)",
        ],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="candidate_missing_artifact",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )
    missing = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[f"missing-{uuid.uuid4().hex}"],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="missing",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )
    timed = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[sys.executable, "-c", "import time; time.sleep(5)"],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="timeout",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )

    assert failed.status == "failed"
    assert failed.failure_code == "repository_validation_test_failure"
    assert candidate_missing_artifact.status == "failed"
    assert (
        candidate_missing_artifact.failure_code == "repository_validation_test_failure"
    )
    assert missing.status == "infrastructure_error"
    assert missing.failure_code == "repository_validation_executable_missing"
    assert timed.status == "timed_out"
    assert timed.failure_code == "repository_validation_timeout"


def test_candidate_execution_policy_denial_and_fingerprint_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    provider = InheritProvider(
        ExecutionEnvironmentConfig(
            policy={"command_deny": [Path(sys.executable).name]}
        ),
        source_environment={"PATH": os.environ.get("PATH", "")},
    )
    prepared = provider.prepare(repository=repo, worktree=worktree)
    denied = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[sys.executable, "-c", "pass"],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="denied",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )

    assert denied.status == "policy_denied"
    assert denied.failure_code == "repository_validation_policy_denied"

    provider.config = ExecutionEnvironmentConfig()
    prepared.fingerprint = "b" * 64
    mismatch = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[sys.executable, "-c", "pass"],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="mismatch",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )
    assert mismatch.status == "infrastructure_error"
    assert mismatch.failure_code == "repository_validation_environment_mismatch"


def test_candidate_execution_redacts_registered_secret_output(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    worktree.mkdir()
    secret = f"candidate-secret-{uuid.uuid4().hex}"
    register_secret_values([secret])
    provider = InheritProvider(
        ExecutionEnvironmentConfig(),
        source_environment={"PATH": os.environ.get("PATH", "")},
    )
    prepared = provider.prepare(repository=repo, worktree=worktree)

    result = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=[sys.executable, "-c", f"print({secret!r})"],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="redaction",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )

    assert secret not in result.stdout
    assert "[REDACTED]" in result.stdout


def test_doctor_report_has_stable_v1_shape_and_fails_missing_requirements(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init")
    healthy, report = _doctor_report(
        tmp_path,
        {
            "execution_environment": {"provider": "inherit"},
            "backends": {},
        },
    )

    assert healthy is False
    assert report["schema_version"] == "villani.doctor.v1"
    assert set(report) == {
        "schema_version",
        "repository",
        "healthy",
        "ok",
        "required_capabilities",
        "git",
        "disk",
        "service",
        "daemon",
        "adapters",
        "coding_commands",
        "backend_connectivity",
        "credentials",
        "execution_providers",
        "execution_environment_fingerprint",
        "repository_inspection",
        "detected_test_tools",
        "likely_test_commands",
        "inferred_commands_executed",
    }
    assert report["required_capabilities"]["coding_adapter"] is False
    assert report["required_capabilities"]["backends"] is False
