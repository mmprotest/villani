from __future__ import annotations

import subprocess
from pathlib import Path

from villani_ops.closed_loop.adapters.git_isolation import GitIsolationAdapter
from villani_ops.closed_loop.candidate_bundle import apply_candidate_bundle
from villani_ops.closed_loop.candidate_quality import (
    assess_candidate_patch_quality,
    prepare_candidate_worktree,
)
from villani_ops.closed_loop.interfaces import AttemptContext


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path, files: dict[str, bytes]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Villani Tests")
    _git(repo, "config", "core.autocrlf", "false")
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _quality(
    repo: Path,
    *,
    task: str,
    policy_configuration: dict[str, object] | None = None,
):
    preparation = prepare_candidate_worktree(worktree=repo, task=task)
    quality = assess_candidate_patch_quality(
        worktree=repo,
        candidate_id="attempt-0001",
        task=task,
        preparation=preparation,
        relevant_paths=("src/target.weird", "src/module.txt"),
        policy_configuration=policy_configuration,
    )
    return preparation, quality


def test_line_ending_only_change_is_removed_before_capture(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path, {"src/module.txt": b"alpha\nbeta\n"})
    target = repo / "src" / "module.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")

    preparation, quality = _quality(
        repo,
        task="Keep src/module.txt unchanged",
    )

    assert target.read_bytes() == b"alpha\nbeta\n"
    assert _git(repo, "diff", "--binary", "HEAD", "--").stdout == ""
    assert preparation.line_ending_only_lines > 0
    assert quality.status == "ineligible"
    assert "line_ending_only_rewrite_removed" in quality.reason_codes


def test_semantic_change_survives_whole_file_crlf_conversion(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        {"src/module.txt": b"first\nold value\nthird\n"},
    )
    target = repo / "src" / "module.txt"
    target.write_bytes(b"first\r\nnew value\r\nthird\r\n")

    preparation, quality = _quality(
        repo,
        task="Change the value in src/module.txt",
    )

    assert target.read_bytes() == b"first\nnew value\nthird\n"
    assert _git(repo, "diff", "--numstat", "HEAD", "--").stdout.startswith(
        "1\t1\t"
    )
    assert preparation.line_ending_only_lines > 0
    assert quality.status == "eligible"
    assert quality.semantic_lines_added == 1
    assert quality.semantic_lines_removed == 1


def test_villani_debug_and_ignored_dependencies_are_excluded(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        {
            ".gitignore": b"node_modules/\n",
            "src/module.txt": b"baseline\n",
        },
    )
    villani_debug = repo / ".villani" / "debug.json"
    villani_debug.parent.mkdir()
    villani_debug.write_text("debug", encoding="utf-8")
    dependency = repo / "node_modules" / "pkg" / "index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("generated", encoding="utf-8")

    preparation, quality = _quality(repo, task="Change src/module.txt")

    assert not villani_debug.exists()
    assert not dependency.exists()
    assert ".villani/debug.json" in quality.villani_owned_files
    assert "node_modules/pkg/index.js" in quality.ignored_files
    assert "dependency_directory_excluded" in quality.reason_codes
    assert quality.status == "ineligible"
    assert preparation.generated_files_excluded >= 1


def test_explicitly_required_generated_artifact_remains_eligible(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path, {"README.md": b"baseline\n"})
    artifact = repo / "dist" / "report.json"
    artifact.parent.mkdir()
    artifact.write_text('{"ok": true}\n', encoding="utf-8")

    _, quality = _quality(
        repo,
        task="Create the required artifact dist/report.json",
    )

    assert artifact.exists()
    assert quality.status == "eligible"
    assert "dist/report.json" in quality.generated_files
    assert "generated_artifact_explicitly_required" in quality.reason_codes


def test_scratch_only_candidate_is_ineligible(tmp_path: Path) -> None:
    repo = _repository(tmp_path, {"README.md": b"baseline\n"})
    scratch = repo / "command-output.txt"
    scratch.write_text("temporary command output", encoding="utf-8")

    _, quality = _quality(repo, task="Improve README.md")

    assert not scratch.exists()
    assert quality.status == "ineligible"
    assert "scratch_only_candidate" in quality.reason_codes


def test_narrow_language_neutral_source_patch_is_eligible(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path, {"src/target.weird": b"before\n"})
    (repo / "src" / "target.weird").write_bytes(b"after\n")

    _, quality = _quality(
        repo,
        task="Update src/target.weird",
    )

    assert quality.status == "eligible"
    assert quality.relevant_files_changed == ["src/target.weird"]
    assert quality.relevant_diff_ratio == 1.0
    assert "relevant_patch_present" in quality.reason_codes


def test_bulk_rewrite_policy_supports_warning_and_ineligible(
    tmp_path: Path,
) -> None:
    baseline = "".join(f"value-{index:03d}\n" for index in range(320))
    rewritten = "".join(f"value-{index:03d}   \n" for index in range(320))
    repo = _repository(
        tmp_path,
        {"src/module.txt": baseline.encode("utf-8")},
    )
    (repo / "src" / "module.txt").write_text(
        rewritten,
        encoding="utf-8",
        newline="\n",
    )
    preparation = prepare_candidate_worktree(
        worktree=repo,
        task="Update src/module.txt",
    )

    warning = assess_candidate_patch_quality(
        worktree=repo,
        candidate_id="attempt-warning",
        task="Update src/module.txt",
        preparation=preparation,
        relevant_paths=("src/module.txt",),
    )
    ineligible = assess_candidate_patch_quality(
        worktree=repo,
        candidate_id="attempt-ineligible",
        task="Update src/module.txt",
        preparation=preparation,
        relevant_paths=("src/module.txt",),
        policy_configuration={
            "candidate_patch_quality": {
                "bulk_rewrite_policy": "ineligible",
            }
        },
    )

    assert warning.status == "warning"
    assert ineligible.status == "ineligible"
    assert warning.bulk_rewrite_files == ["src/module.txt"]
    assert "bulk_rewrite_small_semantic_change" in warning.reason_codes


def test_git_isolation_preserves_source_autocrlf_policy(tmp_path: Path) -> None:
    repo = _repository(tmp_path, {"README.md": b"baseline\n"})
    run_dir = tmp_path / "run"
    attempt_dir = run_dir / "attempts" / "attempt-0001"
    attempt_dir.mkdir(parents=True)
    context = AttemptContext(
        run_id="run-0001",
        trace_id="trace-0001",
        task_id="task-0001",
        attempt_id="attempt-0001",
        ordinal=1,
        task="Change README.md",
        repository_path=str(repo),
        success_criteria="README.md changes",
        requires_file_changes=True,
        backend_name="test",
        model=None,
        policy_configuration={},
        run_directory=run_dir,
        attempt_directory=attempt_dir,
    )

    adapter = GitIsolationAdapter()
    isolated = adapter.create(context)
    try:
        configured = _git(
            isolated.copied.worktree_path,
            "config",
            "--get",
            "core.autocrlf",
        )
        assert configured.stdout.strip() == "false"
        assert isolated.metadata["git_core_autocrlf"] == "false"
    finally:
        adapter.cleanup(isolated.copied.worktree_path)


def test_git_isolation_keeps_clean_crlf_source_blob_identity(tmp_path: Path) -> None:
    repo = _repository(tmp_path, {"src/module.txt": b"before\n"})
    _git(repo, "config", "core.autocrlf", "true")
    (repo / "src" / "module.txt").write_bytes(b"before\r\n")
    _git(repo, "add", "src/module.txt")
    assert _git(repo, "status", "--porcelain").stdout == ""

    run_dir = tmp_path / "run"
    attempt_dir = run_dir / "attempts" / "attempt-0001"
    attempt_dir.mkdir(parents=True)
    context = AttemptContext(
        run_id="run-0001",
        trace_id="trace-0001",
        task_id="task-0001",
        attempt_id="attempt-0001",
        ordinal=1,
        task="Change src/module.txt",
        repository_path=str(repo),
        success_criteria="src/module.txt contains after",
        requires_file_changes=True,
        backend_name="test",
        model=None,
        policy_configuration={},
        run_directory=run_dir,
        attempt_directory=attempt_dir,
    )

    adapter = GitIsolationAdapter()
    isolated = adapter.create(context)
    try:
        source_blob = _git(repo, "rev-parse", "HEAD:src/module.txt").stdout.strip()
        isolated_blob = _git(
            isolated.copied.worktree_path,
            "rev-parse",
            "HEAD:src/module.txt",
        ).stdout.strip()
        assert isolated_blob == source_blob
        assert isolated.metadata["git_core_autocrlf"] == "true"

        (isolated.copied.worktree_path / "src" / "module.txt").write_bytes(
            b"after\r\n"
        )
        capture = adapter.capture(isolated)
        assert capture.has_changes is True
        patch = isolated.patch_path.read_text(encoding="utf-8")
        assert f"index {source_blob[:7]}" in patch
        checked = subprocess.run(
            ["git", "apply", "--index", "--check", str(isolated.patch_path)],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        assert checked.returncode == 0, checked.stderr
    finally:
        adapter.cleanup(isolated.copied.worktree_path)


def test_crlf_candidate_patch_reconstructs_exact_tracked_state(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path, {"src/module.txt": b"before\r\n"})
    run_dir = tmp_path / "run"

    def context(attempt_id: str) -> AttemptContext:
        attempt_dir = run_dir / "attempts" / attempt_id
        attempt_dir.mkdir(parents=True)
        return AttemptContext(
            run_id="run-0001",
            trace_id="trace-0001",
            task_id="task-0001",
            attempt_id=attempt_id,
            ordinal=1,
            task="Change src/module.txt",
            repository_path=str(repo),
            success_criteria="src/module.txt contains after",
            requires_file_changes=True,
            backend_name="test",
            model=None,
            policy_configuration={},
            run_directory=run_dir,
            attempt_directory=attempt_dir,
        )

    adapter = GitIsolationAdapter()
    candidate = adapter.create(context("attempt-candidate"))
    reconstructed = adapter.create(context("attempt-reconstructed"))
    try:
        target = candidate.copied.worktree_path / "src" / "module.txt"
        target.write_bytes(b"after\r\n")
        preparation = prepare_candidate_worktree(
            worktree=candidate.copied.worktree_path,
            task="Change src/module.txt",
        )
        quality = assess_candidate_patch_quality(
            worktree=candidate.copied.worktree_path,
            candidate_id="attempt-candidate",
            task="Change src/module.txt",
            preparation=preparation,
            relevant_paths=("src/module.txt",),
        )
        capture = adapter.capture(candidate)

        assert quality.status == "eligible"
        assert capture.has_changes is True
        assert b"-before\r\n+after\r\n" in candidate.patch_path.read_bytes()
        assert (
            _git(candidate.copied.worktree_path, "diff", "--check").stdout
            == ""
        )

        apply_candidate_bundle(
            reconstructed.copied.worktree_path,
            candidate.patch_path.parent,
        )
        assert (
            reconstructed.copied.worktree_path / "src" / "module.txt"
        ).read_bytes() == target.read_bytes()
    finally:
        adapter.cleanup(candidate.copied.worktree_path)
        adapter.cleanup(reconstructed.copied.worktree_path)
