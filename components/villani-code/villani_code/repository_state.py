from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Iterable


_RUNTIME_DIRECTORY_NAMES = {
    ".git",
    ".villani",
    ".villani_code",
    ".pytest_cache",
    "__pycache__",
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return sha256_bytes(path.read_bytes())


def _is_runtime_path(path: str) -> bool:
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    return any(part in _RUNTIME_DIRECTORY_NAMES for part in parts)


def _git_bytes(repo: Path, *args: str) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return bytes(completed.stdout)


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if _is_runtime_path(relative):
            continue
        yield path


def repository_state_digest(repo: Path) -> str:
    """Return a content-derived digest for the current candidate repository state."""

    resolved = repo.resolve()
    head = _git_bytes(resolved, "rev-parse", "HEAD")
    diff = _git_bytes(resolved, "diff", "--binary", "HEAD", "--")
    untracked = _git_bytes(
        resolved,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if head is not None and diff is not None and untracked is not None:
        digest = hashlib.sha256()
        digest.update(b"head\0")
        digest.update(head)
        digest.update(b"diff\0")
        digest.update(diff)
        digest.update(b"untracked\0")
        for raw_path in sorted(value for value in untracked.split(b"\0") if value):
            relative = raw_path.decode("utf-8", errors="surrogateescape")
            if _is_runtime_path(relative):
                continue
            digest.update(raw_path)
            digest.update(b"\0")
            path = resolved / relative
            if path.is_file() and not path.is_symlink():
                digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    digest = hashlib.sha256()
    for path in _iter_files(resolved):
        relative = path.relative_to(resolved).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

