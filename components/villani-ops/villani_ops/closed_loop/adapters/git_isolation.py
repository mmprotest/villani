"""Git-backed attempt isolation and repository identity checks."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from villani_ops.isolation.copy_git import (
    CopiedGitCandidate,
    capture_candidate_patch,
    create_git_baselined_copy,
    remove_tree,
)

from ..durable_io import write_json_atomic
from ..interfaces import AttemptContext


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        capture_output=True,
    )


def repository_identity(repo: Path) -> dict[str, Any]:
    resolved = Path(repo).resolve()
    inside = _git(resolved, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {
            "repository_path": str(resolved),
            "is_git_repository": False,
            "git_root": None,
            "head": None,
            "status_porcelain": None,
        }
    root = _git(resolved, "rev-parse", "--show-toplevel")
    head = _git(resolved, "rev-parse", "HEAD")
    status = _git(resolved, "status", "--porcelain", "--untracked-files=all")
    return {
        "repository_path": str(resolved),
        "is_git_repository": True,
        "git_root": str(Path(root.stdout.strip()).resolve())
        if root.returncode == 0 and root.stdout.strip()
        else None,
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "status_porcelain": status.stdout if status.returncode == 0 else None,
    }


def validate_target_identity(target_repo: Path, baseline: dict[str, Any]) -> None:
    current = repository_identity(target_repo)
    if not baseline.get("is_git_repository"):
        raise ValueError("attempt baseline did not identify a Git target repository")
    if not current.get("is_git_repository"):
        raise ValueError("target repository is no longer a Git repository")
    if (
        Path(str(current["repository_path"])).resolve()
        != Path(str(baseline.get("repository_path"))).resolve()
    ):
        raise ValueError("target repository identity does not match attempt baseline")
    if current.get("git_root") != baseline.get("git_root"):
        raise ValueError("target Git root does not match attempt baseline")
    if current.get("head") != baseline.get("head"):
        raise ValueError("target repository HEAD changed after attempt isolation")
    if current.get("status_porcelain") != baseline.get("status_porcelain"):
        raise ValueError(
            "target repository working state changed after attempt isolation"
        )


def validate_target_lineage(target_repo: Path, baseline: dict[str, Any]) -> None:
    """Validate immutable repository identity while allowing patch worktree changes."""

    current = repository_identity(target_repo)
    if not baseline.get("is_git_repository") or not current.get("is_git_repository"):
        raise ValueError(
            "materialization recovery requires the original Git repository"
        )
    if (
        Path(str(current["repository_path"])).resolve()
        != Path(str(baseline.get("repository_path"))).resolve()
    ):
        raise ValueError("target repository identity does not match attempt baseline")
    if current.get("git_root") != baseline.get("git_root"):
        raise ValueError("target Git root does not match attempt baseline")
    if current.get("head") != baseline.get("head"):
        raise ValueError("target repository HEAD changed after attempt isolation")


@dataclass(frozen=True, slots=True)
class IsolatedAttempt:
    copied: CopiedGitCandidate
    patch_path: Path
    metadata: dict[str, Any]


class GitIsolationAdapter:
    def create(self, context: AttemptContext) -> IsolatedAttempt:
        source = Path(context.repository_path).resolve()
        attempt_dir = Path(context.attempt_directory).resolve()
        baseline = repository_identity(source)
        isolation = context.policy_configuration.get("isolation")
        settings = isolation if isinstance(isolation, dict) else {}
        include_untracked = bool(settings.get("include_untracked_attempt_files", False))
        max_file_size = int(settings.get("max_file_size_bytes", 50 * 1024 * 1024))
        max_total_size = int(settings.get("max_total_size_bytes", 500 * 1024 * 1024))
        copied = create_git_baselined_copy(
            source,
            attempt_dir,
            include_untracked_attempt_files=include_untracked,
            max_file_size_bytes=max_file_size,
            max_total_size_bytes=max_total_size,
        )
        autocrlf = _git(copied.worktree_path, "config", "--get", "core.autocrlf")
        if autocrlf.returncode != 0 or not autocrlf.stdout.strip():
            remove_tree(copied.worktree_path)
            raise RuntimeError(
                "could not determine line-ending behavior for the isolated worktree"
            )
        autocrlf_value = autocrlf.stdout.strip().casefold()
        patch_path = attempt_dir / "patch.diff"
        metadata = {
            "source_repository": baseline,
            "worktree_path": str(copied.worktree_path),
            "patch_path": str(patch_path),
            "isolated": True,
            "isolation_primitive": "create_git_baselined_copy",
            "include_untracked_attempt_files": include_untracked,
            "max_file_size_bytes": max_file_size,
            "max_total_size_bytes": max_total_size,
            "git_core_autocrlf": autocrlf_value,
        }
        write_json_atomic(attempt_dir / "worktree.json", metadata)
        return IsolatedAttempt(copied, patch_path, metadata)

    def capture(self, isolated: IsolatedAttempt):
        return capture_candidate_patch(
            isolated.copied.worktree_path, isolated.patch_path
        )

    def cleanup(self, worktree_path: Path) -> None:
        """Remove only the attempt-owned exported tree after its patch is safe."""

        remove_tree(worktree_path)
