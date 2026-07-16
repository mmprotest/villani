"""Deterministic candidate cleanup and patch-quality assessment."""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from villani_ops.execution_environment.models import CandidatePatchQuality


_VILLANI_OWNED_PARTS = {
    ".villani",
    ".villani-ops",
    ".villani_code",
}
_CACHE_PARTS = {
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
_DEPENDENCY_PARTS = {
    ".venv",
    "node_modules",
    "venv",
}
_GENERATED_PARTS = {
    "build",
    "coverage",
    "dist",
    "generated",
    "htmlcov",
    "out",
    "target",
}
_COMMAND_OUTPUT_NAMES = {
    "command-output.txt",
    "command_output.txt",
    "output.log",
    "stderr.log",
    "stdout.log",
    "test-output.txt",
    "test-results.txt",
}
_WORD_RE = re.compile(r"[a-z0-9_.\-/]+")


@dataclass(frozen=True, slots=True)
class CandidatePreparation:
    line_ending_only_lines: int
    generated_files_excluded: int
    observed_tracked_files: tuple[str, ...]
    observed_untracked_files: tuple[str, ...]
    observed_ignored_files: tuple[str, ...]
    observed_villani_owned_files: tuple[str, ...]
    observed_generated_files: tuple[str, ...]
    observed_scratch_files: tuple[str, ...]
    observed_dependency_files: tuple[str, ...]
    observed_debug_files: tuple[str, ...]
    observed_probe_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LineStats:
    raw_added: int
    raw_removed: int
    semantic_added: int
    semantic_removed: int
    whitespace_only: int
    line_ending_only: int
    baseline_lines: int


def _git(
    worktree: Path,
    *args: str,
    text: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=worktree,
        text=text,
        capture_output=True,
        check=False,
    )


def _nul_paths(completed: subprocess.CompletedProcess) -> list[str]:
    if completed.returncode != 0:
        return []
    raw = (
        completed.stdout.encode("utf-8", errors="surrogateescape")
        if isinstance(completed.stdout, str)
        else bytes(completed.stdout or b"")
    )
    return sorted(
        value.decode("utf-8", errors="surrogateescape")
        for value in raw.split(b"\0")
        if value
    )


def _changed_tracked_paths(worktree: Path) -> list[str]:
    return _nul_paths(
        _git(
            worktree,
            "diff",
            "--name-only",
            "-z",
            "HEAD",
            "--",
            text=False,
        )
    )


def _untracked_paths(worktree: Path) -> list[str]:
    return _nul_paths(
        _git(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            text=False,
        )
    )


def _ignored_paths(worktree: Path) -> list[str]:
    return _nul_paths(
        _git(
            worktree,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            text=False,
        )
    )


def _parts(path: str) -> tuple[str, ...]:
    return tuple(
        part.casefold()
        for part in path.replace("\\", "/").split("/")
        if part
    )


def _normalize_relative(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _explicitly_required(path: str, task: str) -> bool:
    normalized_path = _normalize_relative(path).casefold()
    normalized_task = task.replace("\\", "/").casefold()
    if normalized_path and normalized_path in normalized_task:
        return True
    name = Path(normalized_path).name
    return bool(name and name in _WORD_RE.findall(normalized_task))


def _is_villani_owned(path: str) -> bool:
    return bool(set(_parts(path)) & _VILLANI_OWNED_PARTS)


def _is_dependency(path: str) -> bool:
    return bool(set(_parts(path)) & _DEPENDENCY_PARTS)


def _is_generated(path: str) -> bool:
    parts = _parts(path)
    name = Path(path).name.casefold()
    return bool(
        set(parts) & (_GENERATED_PARTS | _CACHE_PARTS | _DEPENDENCY_PARTS)
        or name.endswith((".map", ".min"))
        or ".generated." in name
    )


def _is_cache(path: str) -> bool:
    return bool(set(_parts(path)) & _CACHE_PARTS)


def _is_scratch(path: str) -> bool:
    name = Path(path).name.casefold()
    stem = Path(name).stem
    return bool(
        name in _COMMAND_OUTPUT_NAMES
        or stem.startswith(("scratch", "tmp", "temp"))
        or stem.endswith(("-scratch", "_scratch", "-tmp", "_tmp"))
    )


def _is_debug_log(path: str) -> bool:
    name = Path(path).name.casefold()
    stem = Path(name).stem
    return name.endswith(".log") or stem.startswith("debug")


def _is_probe_file(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    name = Path(normalized).name
    return bool(
        ".villani-probe" in normalized
        or ".villani_probe" in normalized
        or name.startswith(("probe-", "probe_"))
    )


def _remove_untracked_path(worktree: Path, relative: str) -> None:
    target = (worktree / relative).resolve()
    target.relative_to(worktree.resolve())
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
    elif target.is_dir():
        shutil.rmtree(target)


def _base_bytes(worktree: Path, relative: str) -> bytes | None:
    completed = _git(
        worktree,
        "show",
        f"HEAD:{relative}",
        text=False,
    )
    if completed.returncode != 0:
        return None
    return bytes(completed.stdout or b"")


def _normalize_newlines(value: bytes) -> bytes:
    return value.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _line_change_counts(
    before: list[bytes],
    after: list[bytes],
) -> tuple[int, int]:
    added = 0
    removed = 0
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    return added, removed


def _line_stats(before: bytes, after: bytes) -> _LineStats:
    if b"\0" in before[:8_000] or b"\0" in after[:8_000]:
        return _LineStats(0, 0, 0, 0, 0, 0, 0)
    raw_before = before.splitlines(keepends=True)
    raw_after = after.splitlines(keepends=True)
    normalized_before = _normalize_newlines(before).splitlines()
    normalized_after = _normalize_newlines(after).splitlines()
    semantic_before = [b"".join(line.split()) for line in normalized_before]
    semantic_after = [b"".join(line.split()) for line in normalized_after]
    raw_added, raw_removed = _line_change_counts(raw_before, raw_after)
    normalized_added, normalized_removed = _line_change_counts(
        normalized_before,
        normalized_after,
    )
    semantic_added, semantic_removed = _line_change_counts(
        semantic_before,
        semantic_after,
    )
    line_ending_only = max(
        0,
        raw_added
        + raw_removed
        - normalized_added
        - normalized_removed,
    )
    whitespace_only = max(
        0,
        normalized_added
        + normalized_removed
        - semantic_added
        - semantic_removed,
    )
    return _LineStats(
        raw_added=raw_added,
        raw_removed=raw_removed,
        semantic_added=semantic_added,
        semantic_removed=semantic_removed,
        whitespace_only=whitespace_only,
        line_ending_only=line_ending_only,
        baseline_lines=len(normalized_before),
    )


def _restore_original_newline_style(base: bytes, current: bytes) -> bytes:
    normalized = _normalize_newlines(current)
    if _normalize_newlines(base) == normalized:
        return base
    newline = (
        b"\r\n"
        if b"\r\n" in base
        else b"\r"
        if b"\r" in base
        else b"\n"
    )
    return normalized.replace(b"\n", newline)


def prepare_candidate_worktree(
    *,
    worktree: Path,
    task: str,
) -> CandidatePreparation:
    """Remove Villani-owned output and normalize tracked text before validation."""

    worktree = Path(worktree).resolve()
    observed_tracked = _changed_tracked_paths(worktree)
    observed_untracked = _untracked_paths(worktree)
    observed_ignored = _ignored_paths(worktree)
    observed_all = sorted(
        dict.fromkeys(
            [
                *observed_tracked,
                *observed_untracked,
                *observed_ignored,
            ]
        )
    )
    villani = sorted(path for path in observed_all if _is_villani_owned(path))
    generated = sorted(path for path in observed_all if _is_generated(path))
    scratch = sorted(path for path in observed_all if _is_scratch(path))
    dependencies = sorted(path for path in observed_all if _is_dependency(path))
    debug = sorted(path for path in observed_all if _is_debug_log(path))
    probes = sorted(path for path in observed_all if _is_probe_file(path))
    prohibited_untracked = (
        set(villani)
        | set(scratch)
        | set(dependencies)
        | set(debug)
        | set(probes)
        | {path for path in observed_all if _is_cache(path)}
    )
    untracked_or_ignored = set(observed_untracked) | set(observed_ignored)
    for relative in sorted(prohibited_untracked & untracked_or_ignored):
        if (
            relative in generated
            and _explicitly_required(relative, task)
            and relative not in villani
            and relative not in dependencies
            and relative not in debug
            and relative not in probes
        ):
            continue
        _remove_untracked_path(worktree, relative)

    tracked_prohibited = [
        path
        for path in observed_tracked
        if (
            _is_villani_owned(path)
            or _is_dependency(path)
            or _is_cache(path)
            or _is_scratch(path)
            or _is_debug_log(path)
            or _is_probe_file(path)
        )
    ]
    if tracked_prohibited:
        _git(
            worktree,
            "restore",
            "--source=HEAD",
            "--worktree",
            "--",
            *tracked_prohibited,
        )

    line_ending_only_lines = 0
    for relative in _changed_tracked_paths(worktree):
        path = worktree / relative
        base = _base_bytes(worktree, relative)
        if base is None or not path.is_file() or path.is_symlink():
            continue
        current = path.read_bytes()
        stats = _line_stats(base, current)
        line_ending_only_lines += stats.line_ending_only
        normalized = _restore_original_newline_style(base, current)
        if normalized != current:
            path.write_bytes(normalized)

    excluded_count = len(
        [
            path
            for path in generated
            if path in prohibited_untracked
            and path in untracked_or_ignored
            and not (
                _explicitly_required(path, task)
                and path not in villani
                and path not in dependencies
                and path not in debug
                and path not in probes
            )
        ]
    )
    return CandidatePreparation(
        line_ending_only_lines=line_ending_only_lines,
        generated_files_excluded=excluded_count,
        observed_tracked_files=tuple(observed_tracked),
        observed_untracked_files=tuple(observed_untracked),
        observed_ignored_files=tuple(observed_ignored),
        observed_villani_owned_files=tuple(villani),
        observed_generated_files=tuple(generated),
        observed_scratch_files=tuple(scratch),
        observed_dependency_files=tuple(dependencies),
        observed_debug_files=tuple(debug),
        observed_probe_files=tuple(probes),
    )


def _mode_only_changes(worktree: Path) -> list[str]:
    completed = _git(worktree, "diff", "--summary", "HEAD", "--")
    if completed.returncode != 0:
        return []
    output: list[str] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("mode change ", "old mode ", "new mode ")):
            path = stripped.split()[-1]
            if path not in output:
                output.append(path)
    return sorted(output)


def _is_relevant(
    path: str,
    *,
    task: str,
    relevant_paths: set[str],
) -> bool:
    normalized = _normalize_relative(path)
    if normalized in relevant_paths or _explicitly_required(normalized, task):
        return True
    parts = set(_parts(normalized))
    if parts & {"app", "lib", "source", "src", "test", "tests"}:
        return True
    return not (
        _is_villani_owned(normalized)
        or _is_dependency(normalized)
        or _is_scratch(normalized)
        or _is_debug_log(normalized)
        or _is_probe_file(normalized)
    )


def assess_candidate_patch_quality(
    *,
    worktree: Path,
    candidate_id: str,
    task: str,
    preparation: CandidatePreparation,
    relevant_paths: Iterable[str] = (),
    policy_configuration: Mapping[str, Any] | None = None,
) -> CandidatePatchQuality:
    worktree = Path(worktree).resolve()
    tracked = _changed_tracked_paths(worktree)
    untracked = _untracked_paths(worktree)
    ignored = sorted(dict.fromkeys(preparation.observed_ignored_files))
    known_relevant = {
        _normalize_relative(str(path))
        for path in relevant_paths
        if str(path).strip()
    }
    relevant = sorted(
        path
        for path in [*tracked, *untracked]
        if _is_relevant(path, task=task, relevant_paths=known_relevant)
    )
    villani = sorted(
        dict.fromkeys(
            [
                *preparation.observed_villani_owned_files,
                *[path for path in tracked if _is_villani_owned(path)],
                *[path for path in untracked if _is_villani_owned(path)],
            ]
        )
    )
    generated = sorted(
        dict.fromkeys(
            [
                *preparation.observed_generated_files,
                *[path for path in tracked if _is_generated(path)],
                *[path for path in untracked if _is_generated(path)],
            ]
        )
    )

    semantic_added = 0
    semantic_removed = 0
    whitespace_only = 0
    semantic_by_path: dict[str, int] = {}
    bulk_rewrite_files: list[str] = []
    for relative in tracked:
        base = _base_bytes(worktree, relative) or b""
        path = worktree / relative
        current = path.read_bytes() if path.is_file() else b""
        stats = _line_stats(base, current)
        semantic_added += stats.semantic_added
        semantic_removed += stats.semantic_removed
        whitespace_only += stats.whitespace_only
        semantic_by_path[relative] = (
            stats.semantic_added + stats.semantic_removed
        )
        raw_total = stats.raw_added + stats.raw_removed
        semantic_total = stats.semantic_added + stats.semantic_removed
        if (
            raw_total >= 200
            and raw_total >= max(1, int(stats.baseline_lines * 0.7))
            and semantic_total <= max(20, int(raw_total * 0.1))
        ):
            bulk_rewrite_files.append(relative)

    file_mode_only = _mode_only_changes(worktree)
    total_semantic = semantic_added + semantic_removed
    relevant_semantic = sum(
        count
        for path, count in semantic_by_path.items()
        if path in relevant
    )
    if total_semantic:
        relevant_ratio = relevant_semantic / total_semantic
    elif relevant and (tracked or untracked):
        relevant_ratio = 1.0
    else:
        relevant_ratio = 0.0

    reason_codes: list[str] = []
    status: Literal["eligible", "ineligible", "warning"] = "eligible"
    current_paths = [*tracked, *untracked]
    observed_paths = sorted(
        dict.fromkeys(
            [
                *preparation.observed_untracked_files,
                *preparation.observed_ignored_files,
                *preparation.observed_tracked_files,
                *tracked,
            ]
        )
    )
    explicitly_allowed_generated = [
        path for path in generated if _explicitly_required(path, task)
    ]
    if explicitly_allowed_generated:
        reason_codes.append("generated_artifact_explicitly_required")
    if not current_paths:
        status = "ineligible"
        if preparation.line_ending_only_lines > 0:
            reason_codes.append("line_ending_only_rewrite_removed")
        elif preparation.observed_villani_owned_files and set(
            observed_paths
        ).issubset(set(preparation.observed_villani_owned_files)):
            reason_codes.append("only_villani_owned_files")
        elif preparation.observed_ignored_files and set(observed_paths).issubset(
            set(preparation.observed_ignored_files)
        ):
            reason_codes.append("only_ignored_files")
        elif preparation.observed_scratch_files and set(observed_paths).issubset(
            set(preparation.observed_scratch_files)
        ):
            reason_codes.append("scratch_only_candidate")
        else:
            reason_codes.append("empty_patch")
    if preparation.observed_dependency_files:
        reason_codes.append("dependency_directory_excluded")
    if preparation.observed_debug_files:
        reason_codes.append("debug_log_excluded")
    if preparation.observed_probe_files:
        reason_codes.append("temporary_probe_file_excluded")
    if tracked and total_semantic == 0 and not file_mode_only:
        status = "ineligible"
        reason_codes.append("whitespace_only_patch")
    if file_mode_only and total_semantic == 0 and not untracked:
        status = "warning"
        reason_codes.append("file_mode_only_change")
    if bulk_rewrite_files:
        configured = (policy_configuration or {}).get("candidate_patch_quality")
        settings = configured if isinstance(configured, Mapping) else {}
        bulk_policy = str(settings.get("bulk_rewrite_policy", "warning"))
        status = "ineligible" if bulk_policy == "ineligible" else "warning"
        reason_codes.append("bulk_rewrite_small_semantic_change")
    generated_not_explicit = [
        path for path in generated if path not in explicitly_allowed_generated
    ]
    if generated_not_explicit and current_paths:
        if status == "eligible":
            status = "warning"
        reason_codes.append("generated_file_present")
    tracked_debug = [path for path in tracked if _is_debug_log(path)]
    if tracked_debug:
        status = "ineligible"
        reason_codes.append("candidate_contains_debug_logs")
    if current_paths and not relevant:
        status = "ineligible"
        reason_codes.append("no_relevant_source_or_test_change")
    elif relevant and status == "eligible":
        reason_codes.append("relevant_patch_present")
    if relevant_ratio < 0.25 and total_semantic > 0:
        if status == "eligible":
            status = "warning"
        reason_codes.append("low_relevant_diff_ratio")

    return CandidatePatchQuality(
        schema_version="villani.candidate_patch_quality.v1",
        candidate_id=candidate_id,
        status=status,
        tracked_files_changed=tracked,
        relevant_files_changed=relevant,
        untracked_files=untracked,
        ignored_files=ignored,
        villani_owned_files=villani,
        generated_files=generated,
        semantic_lines_added=semantic_added,
        semantic_lines_removed=semantic_removed,
        line_ending_only_lines=preparation.line_ending_only_lines,
        whitespace_only_lines=whitespace_only,
        file_mode_only_changes=file_mode_only,
        bulk_rewrite_files=sorted(bulk_rewrite_files),
        relevant_diff_ratio=round(relevant_ratio, 6),
        reason_codes=sorted(dict.fromkeys(reason_codes)),
    )
