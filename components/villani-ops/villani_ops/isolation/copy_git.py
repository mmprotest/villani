from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import stat
import subprocess
from fnmatch import fnmatch

from villani_ops.agentic.git_artifacts import (
    DEFAULT_PATCH_EXCLUDES,
    GitPatchCaptureResult,
    capture_git_patch,
    ensure_git_baseline,
)


@dataclass(frozen=True)
class CopiedGitCandidate:
    source_repo: Path
    candidate_dir: Path
    worktree_path: Path
    patch_path: Path


class AttemptIsolationError(RuntimeError):
    """The tracked export cannot be created within the configured safety bounds."""


DEFAULT_ATTEMPT_EXCLUDES = frozenset(
    {
        ".git",
        ".villani",
        ".villani-ops",
        ".env",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
    }
)
KNOWN_SECRET_FILENAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "service-account.json",
        "service_account.json",
    }
)
KNOWN_SECRET_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
DEFAULT_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_TOTAL_SIZE_BYTES = 500 * 1024 * 1024


def remove_tree(path: Path) -> None:
    if Path(path).is_symlink():
        Path(path).unlink(missing_ok=True)
        return
    def make_writable_and_retry(function, name, _exc_info):
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
        function(name)

    try:
        shutil.rmtree(Path(path), onerror=make_writable_and_retry)
    except FileNotFoundError:
        pass


def source_is_git_repo(path: Path) -> bool:
    """Return True when path is inside a valid Git worktree without mutating it."""
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=Path(path),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        return False
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(path),
        text=True,
        capture_output=True,
    )
    if root.returncode != 0 or not root.stdout.strip():
        return False
    source = Path(path).resolve()
    git_root = Path(root.stdout.strip()).resolve()
    if source == git_root or (source / ".git").exists():
        return True
    try:
        relative = source.relative_to(git_root).as_posix()
    except ValueError:
        return False
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", relative],
        cwd=git_root,
        capture_output=True,
    )
    return tracked.returncode == 0 and bool(tracked.stdout)


def _git_paths(source: Path, *, include_untracked: bool) -> list[str]:
    command = ["git", "ls-files", "-z"]
    if include_untracked:
        command.extend(["--others", "--exclude-standard"])
    result = subprocess.run(command, cwd=source, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise AttemptIsolationError(
            f"cannot list files for isolated attempt: {stderr or 'not a Git worktree'}"
        )
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def _excluded(relative: str, extra: list[str] | None) -> bool:
    parts = Path(relative).parts
    name = Path(relative).name
    lowered_parts = tuple(part.lower() for part in parts)
    lowered_name = name.lower()
    if any(part in DEFAULT_ATTEMPT_EXCLUDES for part in lowered_parts):
        return True
    if lowered_name == ".env" or lowered_name.startswith(".env."):
        return True
    if (
        lowered_name in KNOWN_SECRET_FILENAMES
        or lowered_name.endswith(KNOWN_SECRET_SUFFIXES)
    ):
        return True
    for pattern in extra or []:
        normalized = pattern.replace("\\", "/").lstrip("./")
        candidate = relative.replace("\\", "/")
        if fnmatch(candidate, normalized) or fnmatch(name, normalized):
            return True
    return False


def _snapshot_paths(source: Path) -> list[str]:
    """Enumerate a legacy non-Git tree without following directory symlinks."""

    paths: list[str] = []
    for root, directories, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        kept_directories: list[str] = []
        for name in sorted(directories):
            relative = (relative_root / name).as_posix()
            if _excluded(relative, None):
                continue
            candidate = root_path / name
            if candidate.is_symlink():
                paths.append(relative)
            else:
                kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(filenames):
            relative = (relative_root / name).as_posix()
            if not _excluded(relative, None):
                paths.append(relative)
    return sorted(paths)


def _safe_relative_path(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise AttemptIsolationError(f"Git returned an unsafe tracked path: {relative!r}")
    return path


def _export_paths(
    source: Path,
    destination: Path,
    paths: list[str],
    *,
    excludes: list[str] | None,
    max_file_size_bytes: int,
    max_total_size_bytes: int,
) -> tuple[int, int]:
    selected: list[tuple[Path, Path, int]] = []
    total = 0
    for relative in paths:
        if _excluded(relative, excludes):
            continue
        rel = _safe_relative_path(relative)
        origin = source / rel
        if not origin.exists() and not origin.is_symlink():
            raise AttemptIsolationError(f"tracked attempt file disappeared: {rel.as_posix()}")
        if origin.is_dir() and not origin.is_symlink():
            # Git links (submodules) are not recursively exported. A coding
            # attempt may not import an unbounded, separately controlled tree.
            continue
        size = 0 if origin.is_symlink() else origin.stat().st_size
        if size > max_file_size_bytes:
            raise AttemptIsolationError(
                f"attempt worktree file exceeds max_file_size_bytes ({max_file_size_bytes}): {rel.as_posix()}"
            )
        total += size
        if total > max_total_size_bytes:
            raise AttemptIsolationError(
                f"attempt worktree exceeds max_total_size_bytes ({max_total_size_bytes})"
            )
        selected.append((origin, destination / rel, size))

    destination.mkdir(parents=True, exist_ok=True)
    for origin, target, _size in selected:
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_symlink():
            # Never resolve the source symlink: its target can point outside
            # the repository and must remain only a link in the attempt tree.
            os.symlink(
                os.readlink(origin),
                target,
                target_is_directory=origin.is_dir(),
            )
        else:
            shutil.copy2(origin, target, follow_symlinks=False)
    return len(selected), total


def copy_worktree(
    src: Path,
    dst: Path,
    *,
    excludes: list[str] | None = None,
    include_untracked_attempt_files: bool = False,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = DEFAULT_MAX_TOTAL_SIZE_BYTES,
) -> tuple[int, int]:
    """Export a bounded attempt tree while preserving the canonical Git default.

    Git sources export tracked files (plus explicitly requested untracked files).
    Legacy non-Git sources use a bounded snapshot with the same safety exclusions.
    """

    if max_file_size_bytes < 1 or max_total_size_bytes < 1:
        raise AttemptIsolationError("attempt worktree size limits must be positive")
    source = Path(src).resolve()
    destination = Path(dst)
    if destination.exists():
        remove_tree(destination)
    if source_is_git_repo(source):
        root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=source,
            text=True,
            capture_output=True,
        )
        if root_result.returncode != 0 or not root_result.stdout.strip():
            raise AttemptIsolationError("cannot resolve the Git worktree root")
        export_source = Path(root_result.stdout.strip()).resolve()
        paths = _git_paths(
            export_source, include_untracked=include_untracked_attempt_files
        )
    else:
        export_source = source
        paths = _snapshot_paths(export_source)
    try:
        return _export_paths(
            export_source,
            destination,
            paths,
            excludes=excludes,
            max_file_size_bytes=max_file_size_bytes,
            max_total_size_bytes=max_total_size_bytes,
        )
    except Exception:
        remove_tree(destination)
        raise


def create_git_baselined_copy(
    source_repo: Path,
    candidate_dir: Path,
    *,
    excludes: list[str] | None = None,
    include_untracked_attempt_files: bool = False,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    max_total_size_bytes: int = DEFAULT_MAX_TOTAL_SIZE_BYTES,
) -> CopiedGitCandidate:
    """
    Copy source_repo into candidate_dir / "worktree" and initialize a temporary Git baseline.

    The source directory is never mutated; Git is initialized only in the copied worktree.
    """
    source_repo = Path(source_repo).resolve()
    candidate_dir = Path(candidate_dir).resolve()
    worktree_path = candidate_dir / "worktree"
    patch_path = candidate_dir / "diff.patch"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists() or worktree_path.is_symlink():
        remove_tree(worktree_path)
    copy_worktree(
        source_repo,
        worktree_path,
        excludes=excludes,
        include_untracked_attempt_files=include_untracked_attempt_files,
        max_file_size_bytes=max_file_size_bytes,
        max_total_size_bytes=max_total_size_bytes,
    )
    ensure_git_baseline(worktree_path)
    return CopiedGitCandidate(
        source_repo=source_repo,
        candidate_dir=candidate_dir,
        worktree_path=worktree_path,
        patch_path=patch_path,
    )


def capture_candidate_patch(
    worktree_path: Path,
    patch_path: Path,
    *,
    excludes: list[str] | None = None,
) -> GitPatchCaptureResult:
    """Capture a candidate patch using the adaptive Git artifact implementation."""
    return capture_git_patch(
        Path(worktree_path),
        Path(patch_path),
        exclude_patterns=excludes or DEFAULT_PATCH_EXCLUDES,
    )
