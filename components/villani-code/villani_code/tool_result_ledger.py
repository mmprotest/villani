from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from villani_code.repository_state import file_sha256, repository_state_digest


READ_ONLY_TOOLS = frozenset(
    {
        "Ls",
        "Read",
        "Grep",
        "Glob",
        "Search",
        "FindSymbol",
        "FindReferences",
        "GitStatus",
        "GitDiff",
        "GitLog",
    }
)


@dataclass(frozen=True, slots=True)
class ToolLedgerKey:
    key: str
    repository_state_digest: str
    target_digest: str


@dataclass(frozen=True, slots=True)
class ToolResultRecord:
    result_id: str
    tool_name: str
    normalized_arguments: dict[str, Any]
    repository_state_digest: str
    target_digest: str
    content_digest: str
    summary: str


def _json_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _target_digest(
    repo: Path,
    tool_name: str,
    normalized_arguments: Mapping[str, Any],
    state_digest: str,
) -> str:
    if tool_name == "Read":
        relative = str(normalized_arguments.get("file_path", ""))
        target = (repo / relative).resolve()
        if not _is_within_repo(repo, target):
            return "outside_repository"
        return file_sha256(target)
    if tool_name in {"Ls", "Grep"}:
        relative = str(normalized_arguments.get("path", "."))
        target = (repo / relative).resolve()
        if not _is_within_repo(repo, target):
            return "outside_repository"
        return _path_digest(target)
    if tool_name == "Glob":
        pattern = str(normalized_arguments.get("pattern", ""))
        digest = hashlib.sha256()
        for path in sorted(repo.glob(pattern)):
            try:
                relative = path.relative_to(repo).as_posix()
            except ValueError:
                continue
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            if path.is_file():
                digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()
    return state_digest


def _is_within_repo(repo: Path, target: Path) -> bool:
    try:
        target.relative_to(repo.resolve())
    except ValueError:
        return False
    return True


def _path_digest(path: Path) -> str:
    if path.is_file():
        return file_sha256(path)
    if not path.is_dir():
        return "missing"
    digest = hashlib.sha256()
    for candidate in sorted(path.rglob("*")):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if any(
            part in {".git", ".villani", ".villani_code"}
            for part in candidate.parts
        ):
            continue
        relative = candidate.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_tool_ledger_key(
    repo: Path,
    tool_name: str,
    normalized_arguments: Mapping[str, Any],
) -> ToolLedgerKey:
    state_digest = repository_state_digest(repo)
    target_digest = _target_digest(
        repo,
        tool_name,
        normalized_arguments,
        state_digest,
    )
    arguments = {
        str(key): value
        for key, value in normalized_arguments.items()
        if key != "refresh"
    }
    key = _json_digest(
        {
            "tool_name": tool_name,
            "arguments": arguments,
            "repository_state_digest": state_digest,
            "target_digest": target_digest,
        }
    )
    return ToolLedgerKey(
        key=key,
        repository_state_digest=state_digest,
        target_digest=target_digest,
    )


def summarize_tool_result(
    tool_name: str,
    normalized_arguments: Mapping[str, Any],
    content: str,
) -> str:
    try:
        decoded = json.loads(content)
    except (TypeError, ValueError):
        decoded = None
    if tool_name == "Read":
        path = str(normalized_arguments.get("file_path", ""))
        start = normalized_arguments.get("start_line")
        end = normalized_arguments.get("end_line")
        if isinstance(decoded, dict):
            returned = len(decoded.get("lines", []))
            start = decoded.get("start_line", start)
            end = decoded.get("end_line", end)
            return f"Same {returned}-line range from {path} ({start}-{end}); file digest unchanged."
        return f"Same read from {path}; file digest unchanged."
    if tool_name in {"Grep", "Search", "FindSymbol", "FindReferences"}:
        query = (
            normalized_arguments.get("pattern")
            or normalized_arguments.get("query")
            or normalized_arguments.get("symbol")
            or ""
        )
        count = 0
        if isinstance(decoded, dict):
            values = (
                decoded.get("matches")
                or decoded.get("results")
                or decoded.get("references")
                or []
            )
            count = len(values) if isinstance(values, list) else 0
        return f"Same {tool_name} result for {query!r} ({count} item(s)); repository state unchanged."
    if tool_name == "GitDiff":
        return "Same candidate diff; repository state unchanged."
    if tool_name == "GitStatus":
        return "Same Git status; repository state unchanged."
    if tool_name == "GitLog":
        return "Same Git history result; repository state unchanged."
    if tool_name == "Glob":
        return f"Same glob result for {normalized_arguments.get('pattern', '')!r}; repository state unchanged."
    if tool_name == "Ls":
        return f"Same directory listing for {normalized_arguments.get('path', '.')!r}; repository state unchanged."
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return first_line[:180] or f"Same {tool_name} result; repository state unchanged."


class ToolResultLedger:
    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self._records_by_key: dict[str, ToolResultRecord] = {}
        self._next_result_number = 1
        self.duplicate_tool_results = 0
        self.duplicate_file_reads = 0
        self.duplicate_searches = 0
        self.unique_files_read: set[str] = set()

    def lookup(
        self,
        tool_name: str,
        normalized_arguments: Mapping[str, Any],
    ) -> tuple[ToolResultRecord | None, ToolLedgerKey | None]:
        if tool_name not in READ_ONLY_TOOLS:
            return None, None
        key = build_tool_ledger_key(self.repo, tool_name, normalized_arguments)
        if bool(normalized_arguments.get("refresh", False)):
            return None, key
        record = self._records_by_key.get(key.key)
        if record is not None:
            self.duplicate_tool_results += 1
            if tool_name == "Read":
                self.duplicate_file_reads += 1
            if tool_name in {"Grep", "Search", "FindSymbol", "FindReferences"}:
                self.duplicate_searches += 1
        return record, key

    def register(
        self,
        *,
        tool_name: str,
        normalized_arguments: Mapping[str, Any],
        content: str,
        key: ToolLedgerKey | None = None,
    ) -> ToolResultRecord | None:
        if tool_name not in READ_ONLY_TOOLS:
            return None
        resolved_key = key or build_tool_ledger_key(
            self.repo,
            tool_name,
            normalized_arguments,
        )
        result_id = f"tool-result-{self._next_result_number:04d}"
        self._next_result_number += 1
        record = ToolResultRecord(
            result_id=result_id,
            tool_name=tool_name,
            normalized_arguments=dict(normalized_arguments),
            repository_state_digest=resolved_key.repository_state_digest,
            target_digest=resolved_key.target_digest,
            content_digest=hashlib.sha256(
                content.encode("utf-8", errors="replace")
            ).hexdigest(),
            summary=summarize_tool_result(
                tool_name,
                normalized_arguments,
                content,
            ),
        )
        self._records_by_key[resolved_key.key] = record
        if tool_name == "Read":
            path = str(normalized_arguments.get("file_path", "")).replace(
                "\\", "/"
            )
            if path:
                self.unique_files_read.add(path)
        return record

    @staticmethod
    def duplicate_payload(record: ToolResultRecord) -> dict[str, Any]:
        return {
            "unchanged": True,
            "prior_result_id": record.result_id,
            "summary": record.summary,
            "repository_state_digest": record.repository_state_digest,
            "target_digest": record.target_digest,
        }

    def telemetry(self) -> dict[str, Any]:
        return {
            "duplicate_tool_results": self.duplicate_tool_results,
            "duplicate_file_reads": self.duplicate_file_reads,
            "duplicate_searches": self.duplicate_searches,
            "unique_files_read": len(self.unique_files_read),
        }
