from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Callable, Iterable


_TOKEN_RE = re.compile(r"[a-z0-9_./-]+")
_NON_RELEVANT_PARTS = {
    ".git",
    ".villani",
    ".villani_code",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
}


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(str(value).casefold().replace("\\", "/"))
        if len(token) >= 3
    }


def _normalize_relative(value: str) -> str:
    normalized = str(value).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _has_material_patch_change(patch: str) -> bool:
    removed: list[str] = []
    added: list[str] = []
    for line in patch.split("\n"):
        if line.startswith(("--- ", "+++ ")):
            continue
        if line.startswith("-"):
            removed.append(line[1:].rstrip("\r"))
        elif line.startswith("+"):
            added.append(line[1:].rstrip("\r"))
    if not removed and not added:
        return False
    return removed != added


def _contains_infrastructure_failure(values: Iterable[str]) -> bool:
    markers = (
        "environment",
        "executable_not_found",
        "infrastructure",
        "policy_denied",
        "provider",
        "timeout",
        "timed_out",
    )
    return any(
        marker in str(value).casefold()
        for value in values
        for marker in markers
    )


def path_relevance(path: str, objective: str, known_relevant: Iterable[str]) -> float:
    normalized = _normalize_relative(path)
    parts = [part for part in normalized.split("/") if part]
    if not normalized or any(part in _NON_RELEVANT_PARTS for part in parts):
        return 0.0
    known = {
        _normalize_relative(str(item))
        for item in known_relevant
        if str(item).strip()
    }
    if normalized in known:
        return 1.0
    path_tokens = _tokens(normalized)
    objective_tokens = _tokens(objective)
    if path_tokens & objective_tokens:
        return 0.8
    if any(
        normalized.startswith(item.rstrip("/") + "/")
        or item.startswith(normalized.rstrip("/") + "/")
        for item in known
    ):
        return 0.7
    return 0.4


class UsefulProgressTracker:
    def __init__(
        self,
        *,
        repo: Path,
        objective: str,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.repo = repo.resolve()
        self.objective = objective
        self._clock = clock
        self._started = clock()
        self._tool_calls = 0
        self._turn = 0
        self._tokens = 0
        self._last_progress_turn = 0
        self._tokens_at_last_progress = 0
        self._known_relevant: set[str] = set()
        self._read_files: set[str] = set()
        self._relevant_read_files: set[str] = set()
        self._first_relevant_file_time: float | None = None
        self._first_relevant_file_tool_call: int | None = None
        self._first_relevant_patch_time: float | None = None
        self._first_relevant_patch_tool_call: int | None = None
        self._first_relevant_patch_tokens: int | None = None
        self._last_patch_digest = ""
        self.relevant_patch_revisions = 0
        self.validation_improvement_count = 0
        self._last_validation_failures: set[str] | None = None

    def set_known_relevant(self, paths: Iterable[str]) -> None:
        self._known_relevant.update(
            _normalize_relative(str(path))
            for path in paths
            if str(path).strip()
        )

    def record_turn(self, turn: int) -> None:
        self._turn = max(self._turn, int(turn))

    def record_tokens(self, tokens: int) -> None:
        self._tokens += max(0, int(tokens))

    def start_tool_call(self) -> None:
        self._tool_calls += 1

    def _mark_progress(self) -> None:
        self._last_progress_turn = self._turn
        self._tokens_at_last_progress = self._tokens

    def observe_read(self, path: str) -> None:
        normalized = _normalize_relative(path)
        if not normalized:
            return
        is_new = normalized not in self._read_files
        self._read_files.add(normalized)
        relevance = path_relevance(
            normalized,
            self.objective,
            self._known_relevant,
        )
        if is_new and relevance >= 0.7:
            self._relevant_read_files.add(normalized)
            self._known_relevant.add(normalized)
            if self._first_relevant_file_time is None:
                self._first_relevant_file_time = self._clock() - self._started
                self._first_relevant_file_tool_call = self._tool_calls
            self._mark_progress()

    def observe_patch(self, patch: str, paths: Iterable[str]) -> None:
        digest = hashlib.sha256(
            patch.encode("utf-8", errors="replace")
        ).hexdigest()
        if (
            not patch.strip()
            or digest == self._last_patch_digest
            or not _has_material_patch_change(patch)
        ):
            return
        relevant = any(
            path_relevance(path, self.objective, self._known_relevant) >= 0.7
            for path in paths
        )
        self._last_patch_digest = digest
        if not relevant:
            return
        self.relevant_patch_revisions += 1
        if self._first_relevant_patch_time is None:
            self._first_relevant_patch_time = self._clock() - self._started
            self._first_relevant_patch_tool_call = self._tool_calls
            self._first_relevant_patch_tokens = self._tokens
        self._mark_progress()

    def observe_validation(self, failures: Iterable[str]) -> None:
        current = {str(item) for item in failures if str(item).strip()}
        if (
            self._last_validation_failures is not None
            and (
                len(current) < len(self._last_validation_failures)
                or (
                    _contains_infrastructure_failure(
                        self._last_validation_failures
                    )
                    and not _contains_infrastructure_failure(current)
                )
            )
        ):
            self.validation_improvement_count += 1
            self._mark_progress()
        self._last_validation_failures = current

    def telemetry(self) -> dict[str, object]:
        return {
            "time_to_first_relevant_file": self._first_relevant_file_time,
            "tool_calls_to_first_relevant_file": (
                self._first_relevant_file_tool_call
            ),
            "time_to_first_relevant_patch": self._first_relevant_patch_time,
            "tool_calls_to_first_relevant_patch": (
                self._first_relevant_patch_tool_call
            ),
            "tokens_to_first_relevant_patch": self._first_relevant_patch_tokens,
            "unique_files_read": len(self._read_files),
            "unique_relevant_files_read": len(self._relevant_read_files),
            "files_read": sorted(self._read_files),
            "relevant_files_read": sorted(self._relevant_read_files),
            "tokens_after_last_relevant_progress": max(
                0,
                self._tokens - self._tokens_at_last_progress,
            ),
            "turns_after_last_relevant_progress": max(
                0,
                self._turn - self._last_progress_turn,
            ),
            "relevant_patch_revisions": self.relevant_patch_revisions,
            "validation_improvement_count": self.validation_improvement_count,
        }
