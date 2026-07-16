"""Durable, self-contained candidate bundle creation and rehydration."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from villani_ops.agentic.git_artifacts import is_patch_excluded
from villani_ops.execution_environment.models import (
    CandidateBundleManifest,
    CandidatePatchQuality,
    RepositoryValidationReport,
)
from villani_ops.execution_environment.secrets import registered_secret_values

from .durable_io import write_json_atomic
from .event_writer import redact_data
from .interfaces import AttemptContext


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git(worktree: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=worktree,
        text=text,
        capture_output=True,
        check=False,
    )


def read_patch_text(path: Path, *, errors: str = "strict") -> str:
    """Read a textual Git patch without universal-newline translation."""

    with Path(path).open(
        "r",
        encoding="utf-8",
        errors=errors,
        newline="",
    ) as handle:
        return handle.read()


def candidate_state_sha256(worktree: Path, patch_path: Path) -> str:
    """Hash the canonical filtered patch representing the current candidate."""

    from villani_ops.isolation.copy_git import capture_candidate_patch

    capture_candidate_patch(worktree, patch_path)
    data = patch_path.read_bytes() if patch_path.is_file() else b""
    return hashlib.sha256(data).hexdigest()


def _untracked_paths(worktree: Path) -> list[str]:
    completed = _git(
        worktree,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        text=False,
    )
    if completed.returncode != 0:
        return []
    return sorted(
        path
        for raw in bytes(completed.stdout or b"").split(b"\0")
        if raw
        for path in [raw.decode("utf-8", errors="surrogateescape")]
        if not is_patch_excluded(path)
    )


def _archive_untracked(
    worktree: Path,
    candidate_directory: Path,
    paths: Sequence[str],
    *,
    secrets: tuple[str, ...],
) -> list[dict[str, Any]]:
    archive_root = candidate_directory / "untracked"
    records: list[dict[str, Any]] = []
    secret_bytes = tuple(
        value.encode("utf-8", errors="ignore") for value in secrets if value
    )
    for relative in paths:
        source = worktree / relative
        record: dict[str, Any] = {"path": relative}
        if not source.is_file() or source.is_symlink():
            record["archive_status"] = "not_regular_file"
            records.append(record)
            continue
        data = source.read_bytes()
        if any(secret in data for secret in secret_bytes):
            record["archive_status"] = "secret_redacted"
            records.append(record)
            continue
        destination = archive_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
        record.update(
            {
                "archive_status": "archived",
                "archive_path": destination.relative_to(candidate_directory).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
        records.append(record)
    return records


def write_candidate_bundle(
    *,
    context: AttemptContext,
    worktree: Path,
    patch: str,
    changed_files: Sequence[str],
    source_repository: Mapping[str, Any],
    execution_environment_report: Mapping[str, Any],
    repository_validation: RepositoryValidationReport,
    candidate_patch_quality: CandidatePatchQuality,
    execution_provider: str,
    execution_environment_fingerprint: str,
    secrets: tuple[str, ...] = (),
) -> CandidateBundleManifest:
    attempt_directory = Path(context.attempt_directory).resolve()
    candidate_directory = attempt_directory / "candidate"
    candidate_directory.mkdir(parents=True, exist_ok=True)
    all_secrets = tuple(dict.fromkeys((*registered_secret_values(), *secrets)))
    untracked = _untracked_paths(worktree)
    status = _git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    status_lines = (
        [line for line in status.stdout.splitlines() if line]
        if status.returncode == 0
        else []
    )
    encoded = patch.encode("utf-8")
    (candidate_directory / "patch.diff").write_bytes(encoded)
    write_json_atomic(
        candidate_directory / "changed-files.json",
        {
            "schema_version": "villani.changed_files.v1",
            "changed_files": sorted(dict.fromkeys(str(item) for item in changed_files)),
        },
    )
    archived = _archive_untracked(
        worktree,
        candidate_directory,
        untracked,
        secrets=all_secrets,
    )
    write_json_atomic(
        candidate_directory / "untracked-files.json",
        {
            "schema_version": "villani.untracked_files.v1",
            "untracked_files": untracked,
            "archives": redact_data(archived, secrets=all_secrets),
        },
    )
    write_json_atomic(
        candidate_directory / "repository-validation.json",
        redact_data(repository_validation, secrets=all_secrets),
    )
    write_json_atomic(
        candidate_directory / "execution-environment.json",
        redact_data(dict(execution_environment_report), secrets=all_secrets),
    )
    write_json_atomic(
        candidate_directory / "candidate-patch-quality.json",
        redact_data(candidate_patch_quality, secrets=all_secrets),
    )
    manifest = CandidateBundleManifest(
        schema_version="villani.candidate.v1",
        candidate_id=context.attempt_id,
        run_id=context.run_id,
        attempt_id=context.attempt_id,
        task_id=context.task_id,
        base_commit=(
            str(source_repository["head"]) if source_repository.get("head") else None
        ),
        baseline_sha256=context.baseline_sha256 or "unknown",
        patch_path="patch.diff",
        patch_sha256=hashlib.sha256(encoded).hexdigest(),
        patch_bytes=len(encoded),
        changed_files=sorted(dict.fromkeys(str(item) for item in changed_files)),
        untracked_files=untracked,
        worktree_status={
            "porcelain": status_lines,
            "clean": not status_lines,
        },
        execution_provider=execution_provider,
        execution_environment_fingerprint=execution_environment_fingerprint,
        repository_validation_path="repository-validation.json",
        candidate_patch_quality_path="candidate-patch-quality.json",
        created_at=_timestamp(),
        materialization_status="not_materialized",
    )
    write_json_atomic(
        candidate_directory / "candidate.json",
        redact_data(manifest, secrets=all_secrets),
    )
    return manifest


def apply_candidate_bundle(worktree: Path, candidate_directory: Path) -> None:
    patch = Path(candidate_directory) / "patch.diff"
    if not patch.is_file():
        raise FileNotFoundError("candidate bundle patch.diff is missing")
    if patch.stat().st_size == 0:
        return
    completed = subprocess.run(
        ["git", "apply", "--binary", "--whitespace=nowarn", str(patch.resolve())],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "candidate bundle could not be reconstructed: "
            + (completed.stderr.strip() or "git apply failed")
        )


def update_candidate_materialization_status(
    attempt_directory: Path, status: str
) -> None:
    path = Path(attempt_directory) / "candidate" / "candidate.json"
    if not path.is_file():
        return
    manifest = CandidateBundleManifest.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    write_json_atomic(
        path,
        manifest.model_copy(update={"materialization_status": status}),
    )
