from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import difflib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from villani_code.command_environment import build_agent_command_environment
from villani_code.indexing import DEFAULT_IGNORE, RepoIndex
from villani_code.patch_apply import (
    PatchApplyError,
    apply_unified_diff_with_diagnostics,
    extract_unified_diff_targets,
    parse_unified_diff,
)
from villani_code.repository_state import file_sha256
from villani_code.retrieval import Retriever


class ReadOnlyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh: bool = False


class LsInput(ReadOnlyInput):
    path: str = "."
    ignore: list[str] = Field(default_factory=lambda: [".git", ".venv", "__pycache__"])


class ReadInput(ReadOnlyInput):
    file_path: str
    max_bytes: int = Field(default=200000, ge=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    include_line_numbers: bool = True


class GrepInput(ReadOnlyInput):
    pattern: str
    path: str = "."
    include_hidden: bool = False
    before_context: int = Field(default=2, ge=0, le=100)
    after_context: int = Field(default=2, ge=0, le=100)
    max_results: int = Field(default=200, ge=1, le=10_000)
    max_output_chars: int = Field(default=40_000, ge=256)


class GlobInput(ReadOnlyInput):
    pattern: str


class SearchInput(ReadOnlyInput):
    query: str
    path: str = "."
    context_lines: int = Field(default=2, ge=0, le=50)
    limit: int = Field(default=20, ge=1, le=200)
    max_snippet_chars: int = Field(default=1_200, ge=80, le=10_000)


class FindSymbolInput(ReadOnlyInput):
    symbol: str
    path: str = "."
    limit: int = Field(default=20, ge=1, le=200)


class FindReferencesInput(ReadOnlyInput):
    symbol: str
    path: str = "."
    limit: int = Field(default=100, ge=1, le=1_000)
    context_lines: int = Field(default=2, ge=0, le=50)


class BashInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    cwd: str = "."
    timeout_sec: int = 30


class WriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str
    content: str
    mkdirs: bool = True


class PatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str = ""
    unified_diff: str
    expected_sha256: str | None = None
    expected_file_digests: dict[str, str] = Field(default_factory=dict)


class PatchRangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    replacement: str
    expected_sha256: str


class WebFetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    timeout_sec: int = 20


class GitSimpleInput(ReadOnlyInput):
    args: list[str] = Field(default_factory=list)


class SubmitPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_summary: str
    candidate_files: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    recommended_steps: list[str]
    open_questions: list[dict[str, Any]] = Field(default_factory=list)
    risk_level: str = "medium"
    confidence_score: float = 0.5


TOOL_MODELS: dict[str, type[BaseModel]] = {
    "Ls": LsInput,
    "Read": ReadInput,
    "Grep": GrepInput,
    "Glob": GlobInput,
    "Search": SearchInput,
    "FindSymbol": FindSymbolInput,
    "FindReferences": FindReferencesInput,
    "Bash": BashInput,
    "Write": WriteInput,
    "Patch": PatchInput,
    "PatchRange": PatchRangeInput,
    "WebFetch": WebFetchInput,
    "GitStatus": GitSimpleInput,
    "GitDiff": GitSimpleInput,
    "GitLog": GitSimpleInput,
    "GitBranch": GitSimpleInput,
    "GitCheckout": GitSimpleInput,
    "GitCommit": GitSimpleInput,
    "SubmitPlan": SubmitPlanInput,
}

DENYLIST = ["rm -rf", "del /s", "format ", "mkfs", "dd if=", "curl ", "wget "]


def _error(message: str) -> dict[str, Any]:
    return {"content": message, "is_error": True}


def _ok(content: str) -> dict[str, Any]:
    return {"content": content, "is_error": False}


def _json_content(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False)


def normalized_tool_arguments(
    name: str,
    raw_input: dict[str, Any],
) -> dict[str, Any]:
    model = TOOL_MODELS.get(name)
    if model is None:
        return dict(raw_input)
    return model.model_validate(raw_input).model_dump(mode="json")


def tool_specs(memory_enabled: bool = False) -> list[dict[str, Any]]:
    descriptions = {
        "Read": "Read a bounded file line range with line numbers and a content digest.",
        "Grep": "Search text with bounded, deduplicated context and file digests.",
        "Search": "Use the repository BM25 index to retrieve bounded relevant snippets.",
        "FindSymbol": "Find indexed symbol definitions without scanning whole files.",
        "FindReferences": "Find indexed definitions and language-neutral lexical references.",
        "Patch": "Apply a unified diff, preferably with the expected preimage digest.",
        "PatchRange": "Replace one exact line range through the unified-diff patch machinery.",
    }
    specs: list[dict[str, Any]] = []
    for name, model in TOOL_MODELS.items():
        specs.append(
            {
                "name": name,
                "description": descriptions.get(
                    name,
                    f"{name} tool for Villani Code.",
                ),
                "input_schema": model.model_json_schema(),
            }
        )
    if memory_enabled:
        from villani_code.task_memory import memory_tool_specs

        specs.extend(memory_tool_specs())
    return specs


def execute_tool(
    name: str,
    raw_input: dict[str, Any],
    repo: Path,
    unsafe: bool = False,
    debug_callback: Any | None = None,
    tool_call_id: str = "",
    private_roots: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    model = TOOL_MODELS.get(name)
    if not model:
        return _error(f"Unknown tool: {name}")
    try:
        parsed = model.model_validate(raw_input)
    except Exception as exc:
        return _error(f"Invalid input for {name}: {exc}")

    try:
        if name == "Ls":
            return _ok(_run_ls(parsed, repo))
        if name == "Read":
            return _ok(_run_read(parsed, repo, debug_callback=debug_callback, tool_call_id=tool_call_id))
        if name == "Grep":
            return _ok(_run_grep(parsed, repo, debug_callback, tool_call_id, private_roots))
        if name == "Glob":
            return _ok(_run_glob(parsed, repo))
        if name == "Search":
            return _ok(_run_search(parsed, repo, debug_callback, tool_call_id, private_roots))
        if name == "FindSymbol":
            return _ok(_run_find_symbol(parsed, repo))
        if name == "FindReferences":
            return _ok(_run_find_references(parsed, repo))
        if name == "Bash":
            return _ok(_run_bash(parsed, repo, unsafe=unsafe, debug_callback=debug_callback, tool_call_id=tool_call_id, private_roots=private_roots))
        if name == "Write":
            return _ok(_run_write(parsed, repo, debug_callback=debug_callback, tool_call_id=tool_call_id))
        if name == "Patch":
            return _ok(_run_patch(parsed, repo, debug_callback=debug_callback, tool_call_id=tool_call_id))
        if name == "PatchRange":
            return _ok(
                _run_patch_range(
                    parsed,
                    repo,
                    debug_callback=debug_callback,
                    tool_call_id=tool_call_id,
                )
            )
        if name == "WebFetch":
            return _ok(_run_webfetch(parsed))
        if name.startswith("Git"):
            return _ok(_run_git(name, parsed, repo, debug_callback, tool_call_id, private_roots))
        if name == "SubmitPlan":
            return _ok("Plan artifact submitted")
    except PatchApplyError as exc:
        return _error(
            _json_content(exc.details)
            if getattr(exc, "details", None)
            else str(exc)
        )
    except Exception as exc:
        return _error(str(exc))
    return _error("Unhandled tool")


def _safe_path(repo: Path, raw: str) -> Path:
    path = (repo / raw).resolve()
    repo_resolved = repo.resolve()
    try:
        path.relative_to(repo_resolved)
    except ValueError:
        raise ValueError("Path escapes repository")
    return path


def _run_ls(data: LsInput, repo: Path) -> str:
    target = _safe_path(repo, data.path)
    lines = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if entry.name in data.ignore:
            continue
        lines.append(f"{entry.name}{'/' if entry.is_dir() else ''}")
    return "\n".join(lines)


def _run_read(data: ReadInput, repo: Path, debug_callback: Any | None = None, tool_call_id: str = "") -> str:
    path = _safe_path(repo, data.file_path)
    if data.max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if data.start_line is not None and data.start_line < 1:
        raise ValueError("start_line must be at least 1")
    if data.end_line is not None and data.end_line < 1:
        raise ValueError("end_line must be at least 1")
    if (
        data.start_line is not None
        and data.end_line is not None
        and data.end_line < data.start_line
    ):
        raise ValueError("end_line must be greater than or equal to start_line")
    full_raw = path.read_bytes()
    text = full_raw.decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    total_lines = len(all_lines)
    start = data.start_line or 1
    if total_lines == 0:
        if start != 1:
            raise ValueError("start_line is outside the empty file")
        requested_end = 0
        selected: list[tuple[int, str]] = []
    else:
        if start > total_lines:
            raise ValueError(
                f"start_line {start} exceeds total file line count {total_lines}"
            )
        requested_end = min(data.end_line or total_lines, total_lines)
        selected = [
            (line_number, all_lines[line_number - 1])
            for line_number in range(start, requested_end + 1)
        ]
    returned: list[tuple[int, str]] = []
    output_bytes = 0
    for line_number, line in selected:
        line_bytes = len((line + "\n").encode("utf-8", errors="replace"))
        if returned and output_bytes + line_bytes > data.max_bytes:
            break
        if not returned and line_bytes > data.max_bytes:
            encoded = line.encode("utf-8", errors="replace")[: data.max_bytes]
            line = encoded.decode("utf-8", errors="ignore")
            line_bytes = len(line.encode("utf-8", errors="replace"))
        returned.append((line_number, line))
        output_bytes += line_bytes
        if output_bytes >= data.max_bytes:
            break
    content_sha256 = hashlib.sha256(full_raw).hexdigest()
    truncated = len(returned) < len(selected)
    payload: dict[str, Any] = {
        "path": data.file_path.replace("\\", "/"),
        "start_line": start,
        "end_line": requested_end,
        "returned_end_line": returned[-1][0] if returned else 0,
        "total_lines": total_lines,
        "content_sha256": content_sha256,
        "lines": (
            [{"line": line_number, "text": line} for line_number, line in returned]
            if data.include_line_numbers
            else [line for _, line in returned]
        ),
        "truncated": truncated,
    }
    if callable(debug_callback):
        debug_callback(
            "file_read",
            {
                "file_path": data.file_path,
                "size_bytes": output_bytes,
                "ok": True,
                "tool_call_id": tool_call_id,
                "content_sha256": content_sha256,
                "start_line": start,
                "end_line": requested_end,
                "lines_read": len(returned),
                "total_lines": total_lines,
                "truncated": truncated,
            },
        )
    return _json_content(payload)


def _command_environment(
    repo: Path,
    command: str | list[str],
    cwd: Path,
    debug_callback: Any | None,
    tool_call_id: str,
    private_roots: tuple[Path, ...] | None,
    *,
    shell: bool = False,
) -> dict[str, str]:
    built = build_agent_command_environment(workspace=repo, private_roots=private_roots)
    if callable(debug_callback):
        executable = (
            os.environ.get("COMSPEC", "cmd.exe")
            if shell and os.name == "nt"
            else "/bin/sh"
            if shell
            else shutil.which(str(command[0]), path=built.values.get("PATH")) or str(command[0])
            if isinstance(command, list) and command
            else ""
        )
        diagnostics = built.diagnostics
        debug_callback(
            "command_environment_sanitized",
            {
                "sanitization_ran": diagnostics.sanitization_ran,
                "discovered_private_roots": list(diagnostics.discovered_private_roots),
                "environment_variables_removed": list(diagnostics.environment_variables_removed),
                "path_entries_removed": diagnostics.path_entries_removed,
                "runner_owned_variables_considered": list(
                    diagnostics.runner_owned_variables_considered
                ),
                "possible_private_path_variables_flagged": list(
                    diagnostics.possible_private_path_variables_flagged
                ),
                "cwd": str(cwd),
                "executable": executable,
                "tool_call_id": tool_call_id,
            },
        )
    return built.values


def _run_grep(
    data: GrepInput,
    repo: Path,
    debug_callback: Any | None = None,
    tool_call_id: str = "",
    private_roots: tuple[Path, ...] | None = None,
) -> str:
    base = _safe_path(repo, data.path)
    try:
        pattern = re.compile(data.pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc
    matches: list[dict[str, Any]] = []
    truncated = False
    for path in _iter_text_paths(base, repo, include_hidden=data.include_hidden):
        raw = path.read_bytes()
        if b"\0" in raw[:8_000]:
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        matching_indexes = [
            index
            for index, line in enumerate(lines)
            if pattern.search(line)
        ]
        matching_index_set = set(matching_indexes)
        emitted_context: set[int] = set()
        relative = path.relative_to(repo).as_posix()
        digest = hashlib.sha256(raw).hexdigest()
        for index in matching_indexes:
            before: list[dict[str, Any]] = []
            after: list[dict[str, Any]] = []
            for context_index in range(
                max(0, index - data.before_context),
                index,
            ):
                if (
                    context_index in emitted_context
                    or context_index in matching_index_set
                ):
                    continue
                emitted_context.add(context_index)
                before.append(
                    {
                        "line": context_index + 1,
                        "text": lines[context_index],
                    }
                )
            for context_index in range(
                index + 1,
                min(len(lines), index + data.after_context + 1),
            ):
                if (
                    context_index in emitted_context
                    or context_index in matching_index_set
                ):
                    continue
                emitted_context.add(context_index)
                after.append(
                    {
                        "line": context_index + 1,
                        "text": lines[context_index],
                    }
                )
            matches.append(
                {
                    "path": relative,
                    "line": index + 1,
                    "matched_text": lines[index],
                    "context_before": before,
                    "context_after": after,
                    "file_sha256": digest,
                }
            )
            if len(matches) >= data.max_results:
                truncated = True
                break
            candidate = _json_content(
                {"matches": matches, "truncated": False}
            )
            if len(candidate) > data.max_output_chars:
                matches.pop()
                truncated = True
                break
        if truncated:
            break
    return _json_content(
        {
            "pattern": data.pattern,
            "path": data.path.replace("\\", "/"),
            "matches": matches,
            "truncated": truncated,
        }
    )


def _run_glob(data: GlobInput, repo: Path) -> str:
    hits = [str(Path(p).relative_to(repo)) for p in glob.glob(str(repo / data.pattern), recursive=True)]
    return "\n".join(sorted(hits))


def _run_search(
    data: SearchInput,
    repo: Path,
    debug_callback: Any | None = None,
    tool_call_id: str = "",
    private_roots: tuple[Path, ...] | None = None,
) -> str:
    base = _safe_path(repo, data.path)
    index = RepoIndex.build(repo, DEFAULT_IGNORE)
    retriever = Retriever(index)
    prefix = (
        base.relative_to(repo).as_posix().rstrip("/") + "/"
        if base != repo
        else ""
    )
    results: list[dict[str, Any]] = []
    for hit in retriever.query(data.query, k=max(data.limit * 4, data.limit)):
        if prefix and hit.path != prefix.rstrip("/") and not hit.path.startswith(
            prefix
        ):
            continue
        results.append(
            {
                "score": round(hit.score, 8),
                "reason": hit.reason,
                "path": hit.path,
                "matching_symbols": list(hit.matching_symbols),
                "snippet": hit.snippet[: data.max_snippet_chars],
                "file_sha256": hit.file_sha256
                or file_sha256(repo / hit.path),
            }
        )
        if len(results) >= data.limit:
            break
    return _json_content(
        {
            "query": data.query,
            "path": data.path.replace("\\", "/"),
            "index_digest": index.fingerprint,
            "results": results,
            "truncated": len(results) >= data.limit,
        }
    )


def _iter_text_paths(
    base: Path,
    repo: Path,
    *,
    include_hidden: bool = False,
) -> list[Path]:
    candidates = [base] if base.is_file() else sorted(base.rglob("*"))
    output: list[Path] = []
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(repo)
        if DEFAULT_IGNORE.should_ignore(relative):
            continue
        if not include_hidden and any(
            part.startswith(".") for part in relative.parts
        ):
            continue
        if path.stat().st_size > 2_000_000:
            continue
        output.append(path)
    return output


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    if not symbol.strip():
        raise ValueError("symbol must not be empty")
    return re.compile(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])")


def _bounded_line_snippet(
    lines: list[str],
    line_index: int,
    context_lines: int,
) -> list[dict[str, Any]]:
    start = max(0, line_index - context_lines)
    end = min(len(lines), line_index + context_lines + 1)
    return [
        {"line": index + 1, "text": lines[index]}
        for index in range(start, end)
    ]


def _path_in_scope(path: str, scoped: Path, repo: Path) -> bool:
    if scoped == repo:
        return True
    prefix = scoped.relative_to(repo).as_posix().rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _run_find_symbol(data: FindSymbolInput, repo: Path) -> str:
    scoped = _safe_path(repo, data.path)
    index = RepoIndex.build(repo, DEFAULT_IGNORE)
    symbol_pattern = _symbol_pattern(data.symbol)
    results: list[dict[str, Any]] = []
    for file_info in index.iter_files():
        if not _path_in_scope(file_info.path, scoped, repo):
            continue
        indexed_matches = [
            symbol
            for symbol in file_info.symbols
            if symbol.casefold() == data.symbol.casefold()
        ]
        if not indexed_matches:
            continue
        path = repo / file_info.path
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        line_index = next(
            (
                index_value
                for index_value, line in enumerate(lines)
                if symbol_pattern.search(line)
            ),
            0,
        )
        results.append(
            {
                "path": file_info.path,
                "symbol": indexed_matches[0],
                "line": line_index + 1 if lines else None,
                "symbol_kind": "indexed_symbol",
                "index_evidence": {
                    "index_digest": index.fingerprint,
                    "indexed_symbols": indexed_matches,
                },
                "snippet": _bounded_line_snippet(lines, line_index, 2)
                if lines
                else [],
                "file_sha256": file_info.content_sha256
                or file_sha256(path),
            }
        )
        if len(results) >= data.limit:
            break
    return _json_content(
        {
            "symbol": data.symbol,
            "path": data.path.replace("\\", "/"),
            "index_digest": index.fingerprint,
            "results": results,
            "truncated": len(results) >= data.limit,
        }
    )


def _run_find_references(data: FindReferencesInput, repo: Path) -> str:
    scoped = _safe_path(repo, data.path)
    index = RepoIndex.build(repo, DEFAULT_IGNORE)
    pattern = _symbol_pattern(data.symbol)
    references: list[dict[str, Any]] = []
    for file_info in index.iter_files():
        if not _path_in_scope(file_info.path, scoped, repo):
            continue
        path = repo / file_info.path
        raw = path.read_bytes()
        if b"\0" in raw[:8_000]:
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        definition_available = any(
            symbol.casefold() == data.symbol.casefold()
            for symbol in file_info.symbols
        )
        definition_emitted = False
        for line_index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            reference_type = "lexical_reference"
            if definition_available and not definition_emitted:
                reference_type = "exact_definition"
                definition_emitted = True
            references.append(
                {
                    "path": file_info.path,
                    "symbol": data.symbol,
                    "line": line_index + 1,
                    "reference_type": reference_type,
                    "index_evidence": (
                        "symbol metadata plus lexical location"
                        if reference_type == "exact_definition"
                        else "language-neutral lexical match"
                    ),
                    "snippet": _bounded_line_snippet(
                        lines,
                        line_index,
                        data.context_lines,
                    ),
                    "file_sha256": file_info.content_sha256
                    or hashlib.sha256(raw).hexdigest(),
                }
            )
            if len(references) >= data.limit:
                break
        if len(references) >= data.limit:
            break
    references.sort(
        key=lambda item: (
            item["reference_type"] != "exact_definition",
            item["path"],
            item["line"],
        )
    )
    return _json_content(
        {
            "symbol": data.symbol,
            "path": data.path.replace("\\", "/"),
            "index_digest": index.fingerprint,
            "references": references,
            "truncated": len(references) >= data.limit,
        }
    )


def _run_bash(data: BashInput, repo: Path, unsafe: bool, debug_callback: Any | None = None, tool_call_id: str = "", private_roots: tuple[Path, ...] | None = None) -> str:
    lowered = data.command.lower()
    if not unsafe:
        for bad in DENYLIST:
            if bad in lowered:
                raise ValueError(f"Refusing command: {bad.strip()}")
    cwd = _safe_path(repo, data.cwd)
    if callable(debug_callback):
        debug_callback("command_started", {"command": data.command, "cwd": data.cwd, "tool_call_id": tool_call_id})
    env = _command_environment(repo, data.command, cwd, debug_callback, tool_call_id, private_roots, shell=True)
    try:
        proc = subprocess.run(data.command, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=data.timeout_sec, env=env)
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else str(exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else str(exc.stderr or "")
        )
        timeout_message = (
            stderr
            or f"Command exceeded timeout of {data.timeout_sec} second(s)."
        )
        if callable(debug_callback):
            debug_callback(
                "command_finished",
                {
                    "command": data.command,
                    "cwd": data.cwd,
                    "exit_code": 124,
                    "stdout": stdout,
                    "stderr": timeout_message,
                    "truncated": False,
                    "timed_out": True,
                    "tool_call_id": tool_call_id,
                },
            )
        return _json_content(
            {
                "command": data.command,
                "exit_code": None,
                "stdout": stdout,
                "stderr": timeout_message,
                "timed_out": True,
            }
        )
    if callable(debug_callback):
        debug_callback(
            "command_finished",
            {
                "command": data.command,
                "cwd": data.cwd,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "truncated": False,
                "tool_call_id": tool_call_id,
            },
        )
    return _json_content({"command": data.command, "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})


def _run_write(data: WriteInput, repo: Path, debug_callback: Any | None = None, tool_call_id: str = "") -> str:
    path = _safe_path(repo, data.file_path)
    if data.mkdirs:
        path.parent.mkdir(parents=True, exist_ok=True)
    content = data.content
    if path.is_file():
        original = path.read_bytes()
        newline = (
            "\r\n"
            if b"\r\n" in original
            else "\r"
            if b"\r" in original
            else "\n"
        )
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        content = normalized.replace("\n", newline)
    path.write_bytes(content.encode("utf-8"))
    if callable(debug_callback):
        debug_callback(
            "file_write",
            {
                "file_path": data.file_path,
                "size_bytes": len(content.encode("utf-8")),
                "ok": True,
                "tool_call_id": tool_call_id,
            },
        )
    return f"Wrote {path}"


def _run_patch(data: PatchInput, repo: Path, debug_callback: Any | None = None, tool_call_id: str = "") -> str:
    if data.file_path:
        _safe_path(repo, data.file_path)
    requested_paths: list[str] = []
    hunks_attempted = 0
    try:
        parsed_patches = parse_unified_diff(data.unified_diff)
        hunks_attempted = sum(len(file_patch.hunks) for file_patch in parsed_patches)
        requested_paths = extract_unified_diff_targets(data.unified_diff, default_file_path=data.file_path or None)
    except PatchApplyError:
        if data.file_path:
            requested_paths = [data.file_path]
    try:
        touched, diagnostics = apply_unified_diff_with_diagnostics(
            repo,
            data.unified_diff,
            default_file_path=data.file_path or None,
            expected_file_digests={
                **data.expected_file_digests,
                **(
                    {data.file_path: data.expected_sha256}
                    if data.file_path and data.expected_sha256
                    else {}
                ),
            },
        )
    except PatchApplyError as exc:
        if callable(debug_callback):
            target_paths = requested_paths or ([data.file_path] if data.file_path else [""])
            for file_path in target_paths:
                debug_callback(
                    "patch_applied",
                    {
                        "file_path": file_path,
                        "ok": False,
                        "failure_reason": str(exc),
                        "hunks_attempted": hunks_attempted or None,
                        "hunks_failed": hunks_attempted or None,
                        "tool_call_id": tool_call_id,
                    },
                )
        if not exc.details:
            target = data.file_path or (requested_paths[0] if requested_paths else "")
            actual_digest = (
                file_sha256(_safe_path(repo, target))
                if target
                else "unknown"
            )
            raise PatchApplyError(
                str(exc),
                details={
                    "file": target,
                    "expected_digest": data.expected_sha256,
                    "actual_digest": actual_digest,
                    "failed_hunk": None,
                    "nearest_context": [],
                    "retry_guidance": (
                        "Re-read the target file, confirm the current digest, "
                        "and regenerate a narrow valid unified diff."
                    ),
                },
            ) from exc
        raise
    if callable(debug_callback):
        for file_path in touched:
            debug_callback(
                "patch_applied",
                {
                    "file_path": file_path,
                    "ok": True,
                    "used_fallback": file_path in diagnostics.fallback_files,
                    "tool_call_id": tool_call_id,
                },
            )
    if diagnostics.fallback_files:
        return (
            f"Patch applied to {len(touched)} file(s); "
            f"whitespace-insensitive fallback used for {len(diagnostics.fallback_files)} file(s)"
        )
    return f"Patch applied to {len(touched)} file(s)"


def _run_patch_range(
    data: PatchRangeInput,
    repo: Path,
    debug_callback: Any | None = None,
    tool_call_id: str = "",
) -> str:
    path = _safe_path(repo, data.file_path)
    if not path.is_file():
        raise ValueError(f"PatchRange target does not exist: {data.file_path}")
    if data.start_line < 1 or data.end_line < data.start_line:
        raise ValueError(
            "PatchRange requires start_line >= 1 and end_line >= start_line"
        )
    raw = path.read_bytes()
    original_text = raw.decode("utf-8", errors="surrogateescape")
    original_lines = original_text.splitlines()
    if data.end_line > len(original_lines):
        raise ValueError(
            f"PatchRange end_line {data.end_line} exceeds total line count {len(original_lines)}"
        )
    replacement_lines = data.replacement.replace("\r\n", "\n").replace(
        "\r", "\n"
    ).splitlines()
    updated_lines = [
        *original_lines[: data.start_line - 1],
        *replacement_lines,
        *original_lines[data.end_line :],
    ]
    diff_lines = list(
        difflib.unified_diff(
            original_lines,
            updated_lines,
            fromfile=f"a/{data.file_path}",
            tofile=f"b/{data.file_path}",
            lineterm="",
        )
    )
    if not diff_lines:
        return "PatchRange produced no change"
    patch = PatchInput(
        file_path=data.file_path,
        unified_diff="\n".join(diff_lines) + "\n",
        expected_sha256=data.expected_sha256,
    )
    return _run_patch(
        patch,
        repo,
        debug_callback=debug_callback,
        tool_call_id=tool_call_id,
    )


def _run_webfetch(data: WebFetchInput) -> str:
    u = urlparse(data.url)
    if u.scheme not in {"http", "https"}:
        raise ValueError("Unsupported URL scheme")
    r = httpx.get(data.url, timeout=data.timeout_sec)
    return r.text[:10000]


def _run_git(name: str, data: GitSimpleInput, repo: Path, debug_callback: Any | None = None, tool_call_id: str = "", private_roots: tuple[Path, ...] | None = None) -> str:
    mapping = {
        "GitStatus": ["status", "--short"],
        "GitDiff": ["diff"],
        "GitLog": ["log", "--oneline", "-20"],
        "GitBranch": ["branch"],
        "GitCheckout": ["checkout"],
        "GitCommit": ["commit"],
    }
    cmd = ["git", *mapping[name], *data.args]
    env = _command_environment(repo, cmd, repo, debug_callback, tool_call_id, private_roots)
    proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, env=env)
    return proc.stdout or proc.stderr
