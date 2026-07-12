from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest

from villani_ops.closed_loop.failure_classification import classify_runner_failure
from villani_ops.agentic.runner import _provider_failure
from villani_ops.closed_loop.costs import actual_attempt_cost
from villani_ops.closed_loop.interfaces import ClassificationContext
from villani_ops.cli.unified import _ClassifierAdapter
from villani_ops.core.task import TaskClassification
from villani_ops.core.backend import Backend
from villani_ops.llm.client import LLMCallResult
from villani_ops.isolation.copy_git import (
    AttemptIsolationError,
    copy_worktree,
    create_git_baselined_copy,
    remove_tree,
)
from villani_ops.closed_loop.adapters.git_isolation import GitIsolationAdapter
from villani_ops.providers import (
    ProviderConfigurationError,
    validate_closed_loop_backend,
    villani_code_provider,
)


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr


def _git_repo(tmp_path: Path) -> tuple[Path, Path | None]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Villani tests")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        ".env\n.venv/\nnode_modules/\nignored.txt\n", encoding="utf-8"
    )
    (repo / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("do-not-copy", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "marker").write_text("do-not-copy", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignored", encoding="utf-8")
    external = tmp_path / "external-secret.txt"
    external.write_text("outside repository", encoding="utf-8")
    link = repo / "external-link.txt"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        link = None
    _git(repo, "add", "tracked.txt", ".gitignore", *( ["external-link.txt"] if link else [] ))
    _git(repo, "commit", "-qm", "fixture")
    return repo, link


def test_provider_contract_and_villani_code_mapping() -> None:
    local = Backend(
        name="local",
        provider="local",
        base_url="http://127.0.0.1:8000/v1",
        model="stub",
    )
    validate_closed_loop_backend(local)
    assert villani_code_provider("local") == "openai"
    assert villani_code_provider("openai-compatible") == "openai"

    openai = Backend(name="cloud", provider="openai", model="gpt")
    assert openai.base_url == "https://api.openai.com/v1"
    with pytest.raises(ProviderConfigurationError, match="requires an API key"):
        validate_closed_loop_backend(openai)

    with pytest.raises(ProviderConfigurationError, match="requires an explicit base_url"):
        validate_closed_loop_backend(
            Backend(name="missing-url", provider="openai-compatible", model="stub")
        )


def test_local_compute_pricing_preserves_configured_currency() -> None:
    backend = Backend(
        name="local-aud",
        provider="local",
        base_url="http://127.0.0.1:8000/v1",
        model="stub",
        billing_mode="compute_time",
        compute_cost_per_hour=18.0,
        currency="AUD",
    )
    cost = actual_attempt_cost(
        backend,
        input_tokens=None,
        output_tokens=None,
        duration_seconds=600,
    )
    assert cost.total == pytest.approx(3.0)
    assert cost.currency == "AUD"


@pytest.mark.parametrize(
    ("exit_code", "stderr", "expected"),
    [
        (127, "", "executable_not_found"),
        (2, "invalid provider value", "provider_config_error"),
        (1, "connection refused", "backend_connection_error"),
        (1, "401 unauthorized", "backend_auth_error"),
        (1, "429 too many requests", "backend_rate_limited"),
        (1, "tests failed", "runner_nonzero_exit"),
    ],
)
def test_runner_failure_categories(exit_code: int | None, stderr: str, expected: str) -> None:
    assert classify_runner_failure(exit_code, "", stderr) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("connection refused"), "backend_connection_error"),
        (
            httpx.ConnectError("local model server is not running"),
            "backend_connection_error",
        ),
        (
            httpx.ConnectTimeout("timed out while establishing connection"),
            "backend_connection_error",
        ),
        (RuntimeError("runner produced invalid local state"), "runner_error"),
    ],
)
def test_agentic_provider_failure_categories(error: Exception, expected: str) -> None:
    backend = type(
        "Backend",
        (),
        {"name": "local", "base_url": "http://127.0.0.1:9/v1", "model": "m"},
    )()

    kind, _message, _recoverable = _provider_failure(error, backend)

    assert kind == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileNotFoundError("missing executable"), "executable_not_found"),
        (ProviderConfigurationError("invalid provider configuration"), "provider_config_error"),
        (
            subprocess.CalledProcessError(1, ["runner"], stderr="connection refused"),
            "backend_connection_error",
        ),
        (
            subprocess.CalledProcessError(1, ["runner"], stderr="401 unauthorized"),
            "backend_auth_error",
        ),
        (
            subprocess.CalledProcessError(1, ["runner"], stderr="429 rate limited"),
            "backend_rate_limited",
        ),
        (
            subprocess.CalledProcessError(2, ["runner"], stderr="invalid provider"),
            "provider_config_error",
        ),
        (
            subprocess.CalledProcessError(9, ["runner"], stderr="unexpected exit"),
            "runner_nonzero_exit",
        ),
    ],
)
def test_legacy_runner_failure_categories(error: Exception, expected: str) -> None:
    kind, _message, _recoverable = _provider_failure(error, object())
    assert kind == expected


def test_attempt_export_excludes_ignored_files_and_preserves_external_symlink(
    tmp_path: Path,
) -> None:
    repo, link = _git_repo(tmp_path)
    destination = tmp_path / "attempt"
    copy_worktree(repo, destination)
    assert (destination / "tracked.txt").is_file()
    assert not (destination / ".env").exists()
    assert not (destination / ".venv").exists()
    assert not (destination / "node_modules").exists()
    assert not (destination / "ignored.txt").exists()
    if link is not None:
        copied_link = destination / link.name
        assert copied_link.is_symlink()
        assert os.readlink(copied_link) == os.readlink(link)
        assert not (destination / "external-secret.txt").exists()
    remove_tree(destination)
    assert not destination.exists()


def test_attempt_export_enforces_file_and_total_limits(tmp_path: Path) -> None:
    repo, _ = _git_repo(tmp_path)
    with pytest.raises(AttemptIsolationError, match="max_file_size_bytes"):
        copy_worktree(repo, tmp_path / "too-small", max_file_size_bytes=1)
    with pytest.raises(AttemptIsolationError, match="max_total_size_bytes"):
        copy_worktree(repo, tmp_path / "too-small-total", max_total_size_bytes=1)


def test_legacy_non_git_snapshot_is_bounded_and_excludes_private_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "source.py").write_text("print('safe')\n", encoding="utf-8")
    for directory in (
        ".villani",
        ".villani-ops",
        ".venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
    ):
        private = source / directory
        private.mkdir()
        (private / "private.txt").write_text("do-not-copy", encoding="utf-8")
    for filename in (".env.local", ".npmrc", "id_rsa", "service-account.json"):
        (source / filename).write_text("secret", encoding="utf-8")
    external = tmp_path / "external"
    external.write_text("outside", encoding="utf-8")
    link = source / "external-link"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        link = None

    destination = tmp_path / "snapshot"
    copied, total = copy_worktree(source, destination)

    assert copied >= 1 and total == (source / "source.py").stat().st_size
    assert (destination / "source.py").is_file()
    assert not any((destination / name).exists() for name in (
        ".villani", ".villani-ops", ".venv", "node_modules", "build", "dist"
    ))
    assert not any((destination / name).exists() for name in (
        ".env.local", ".npmrc", "id_rsa", "service-account.json"
    ))
    if link is not None:
        assert (destination / link.name).is_symlink()
        assert os.readlink(destination / link.name) == os.readlink(link)


def test_legacy_non_git_snapshot_rejects_oversized_content(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "large.bin").write_bytes(b"1234")
    with pytest.raises(AttemptIsolationError, match="max_file_size_bytes"):
        copy_worktree(source, tmp_path / "file-limit", max_file_size_bytes=3)
    (source / "large.bin").write_bytes(b"12")
    (source / "second.bin").write_bytes(b"34")
    with pytest.raises(AttemptIsolationError, match="max_total_size_bytes"):
        copy_worktree(source, tmp_path / "total-limit", max_total_size_bytes=3)


def test_baselined_attempt_worktree_can_be_removed_after_capture(tmp_path: Path) -> None:
    repo, _ = _git_repo(tmp_path)
    candidate = create_git_baselined_copy(repo, tmp_path / "candidate")
    assert candidate.worktree_path.is_dir()
    GitIsolationAdapter().cleanup(candidate.worktree_path)
    assert not candidate.worktree_path.exists()
    remove_tree(candidate.candidate_dir)


def _classification_context(tmp_path: Path, backend: str = "primary") -> ClassificationContext:
    return ClassificationContext(
        run_id="run",
        trace_id="trace",
        task_id="task",
        repository_path=str(tmp_path),
        success_criteria="tests pass",
        requires_file_changes=True,
        policy_configuration={"policy": {"classifier_retry_limit": 1}},
        classification_backend_name=backend,
        classification_backend_model="stub",
    )


def _classifier_backends() -> dict[str, Backend]:
    return {
        name: Backend(
            name=name,
            provider="local",
            base_url="http://127.0.0.1:8000/v1",
            model="stub",
            roles=["classification"],
        )
        for name in ("primary", "fallback")
    }


def _successful_classifier_result(backend: Backend) -> LLMCallResult:
    return LLMCallResult(
        parsed_json={
            "difficulty": "medium",
            "risk": "medium",
            "category": "maintenance",
            "estimated_attempts_needed": 1,
            "needs_tests": True,
            "confidence": 0.9,
            "reasoning_summary": "deterministic test classification",
        },
        raw_text='{"difficulty":"medium"}',
        input_tokens=3,
        output_tokens=2,
        backend_name=backend.name,
        model=backend.model,
        usage={"prompt_tokens": 3, "completion_tokens": 2},
    )


def test_classifier_retries_transient_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = 0

    def fake_classify(_self, _task, _backends, *, backend_override=None, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary connection refused")
        assert backend_override is not None
        return TaskClassification.model_validate(_successful_classifier_result(backend_override).parsed_json), _successful_classifier_result(backend_override)

    monkeypatch.setattr("villani_ops.cli.unified.TaskClassifier.classify", fake_classify)
    adapter = _ClassifierAdapter(_classifier_backends(), {"policy": {"classifier_retry_limit": 1}})
    result = adapter.classify("fix the test", _classification_context(tmp_path))
    assert result.difficulty == "medium"
    assert calls == 2
    assert len(result.metadata["classifier_attempts"]) == 2
    assert result.metadata.get("classification_fallback") is not True


def test_classifier_uses_configured_alternate_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_classify(_self, _task, _backends, *, backend_override=None, **_kwargs):
        assert backend_override is not None
        seen.append(backend_override.name)
        if backend_override.name == "primary":
            raise RuntimeError("primary unavailable")
        result = _successful_classifier_result(backend_override)
        return TaskClassification.model_validate(result.parsed_json), result

    monkeypatch.setattr("villani_ops.cli.unified.TaskClassifier.classify", fake_classify)
    configuration = {
        "policy": {
            "classifier_retry_limit": 0,
            "classifier_fallback_backends": ["fallback"],
        }
    }
    adapter = _ClassifierAdapter(_classifier_backends(), configuration)
    result = adapter.classify("fix the test", _classification_context(tmp_path))
    assert seen == ["primary", "fallback"]
    assert result.metadata["classification_backend"]["name"] == "fallback"


def test_classifier_conservative_fallback_is_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def always_fail(*_args, **_kwargs):
        raise RuntimeError("all classifier backends unavailable")

    monkeypatch.setattr("villani_ops.cli.unified.TaskClassifier.classify", always_fail)
    configuration = {
        "policy": {
            "classifier_retry_limit": 0,
            "classifier_fallback_backends": ["fallback"],
        }
    }
    adapter = _ClassifierAdapter(_classifier_backends(), configuration)
    result = adapter.classify("fix the test", _classification_context(tmp_path))
    assert result.difficulty == "hard"
    assert result.risk == "high"
    assert result.metadata["classification_fallback"] is True
    assert len(result.metadata["classifier_attempts"]) == 2
