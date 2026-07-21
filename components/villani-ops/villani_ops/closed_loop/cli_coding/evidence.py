"""One Git-derived candidate evidence pipeline for every CLI coding driver."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from villani_ops.agentic.git_artifacts import is_patch_excluded
from villani_ops.closed_loop.adapters.git_isolation import (
    GitIsolationAdapter,
    IsolatedAttempt,
    validate_target_identity,
)
from villani_ops.closed_loop.candidate_bundle import write_candidate_bundle
from villani_ops.closed_loop.cli_runtime import CliProcessResult
from villani_ops.closed_loop.durable_io import write_json_atomic
from villani_ops.closed_loop.event_writer import redact_data
from villani_ops.closed_loop.interfaces import AttemptContext
from villani_ops.execution_environment.models import (
    CandidatePatchQuality,
    RepositoryValidationReport,
)
from villani_ops.isolation.copy_git import capture_candidate_patch


class ParsedCliEvents(Protocol):
    normalized_rows: tuple[dict[str, Any], ...]
    input_tokens: int | None
    output_tokens: int | None


_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class PreparedCandidate:
    isolated: IsolatedAttempt
    worktree: Path
    source_baseline: dict[str, Any]
    baseline_document: dict[str, Any]
    external_symlinks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CollectedCandidateEvidence:
    patch_path: Path
    patch_bytes: bytes
    patch: str
    changed_files: tuple[str, ...]
    changed_document: dict[str, Any]
    status_document: dict[str, Any]
    forbidden_paths: tuple[str, ...]
    unsafe_paths: tuple[str, ...]
    path_violation: bool
    target_identity_error: str | None
    quality: CandidatePatchQuality
    repository_validation: RepositoryValidationReport
    candidate_manifest: Any
    fingerprint: str
    verifier_trace_directory: Path


def _git(repo: Path, *arguments: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        text=text,
        capture_output=True,
        check=False,
    )


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def relative_to_run(path: Path, context: AttemptContext) -> str:
    return (
        Path(path)
        .resolve()
        .relative_to(Path(context.run_directory).resolve())
        .as_posix()
    )


def _safe_path(worktree: Path, relative: str) -> bool:
    candidate = Path(relative.replace("\\", "/"))
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        return False
    try:
        resolved = (worktree / candidate).resolve(strict=False)
        resolved.relative_to(worktree.resolve())
    except (OSError, ValueError):
        return False
    return True


def _status_document(worktree: Path, *, schema_prefix: str) -> dict[str, Any]:
    completed = _git(
        worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        text=False,
    )
    if completed.returncode != 0:
        return {
            "schema_version": f"villani.{schema_prefix}_repository_status.v1",
            "status": "failed",
            "error": bytes(completed.stderr or b"").decode("utf-8", errors="replace"),
            "entries": [],
            "forbidden_paths_touched": [],
            "unsafe_paths": [],
            "untracked_files": [],
        }
    fields = (
        bytes(completed.stdout or b"").decode("utf-8", errors="replace").split("\0")
    )
    entries: list[dict[str, Any]] = []
    paths: list[str] = []
    untracked: list[str] = []
    index = 0
    while index < len(fields):
        raw = fields[index]
        index += 1
        if not raw:
            continue
        status = raw[:2]
        path = raw[3:] if len(raw) > 3 else ""
        row_paths = [path] if path else []
        if ("R" in status or "C" in status) and index < len(fields) and fields[index]:
            row_paths.append(fields[index])
            index += 1
        entries.append({"status": status, "paths": row_paths})
        paths.extend(row_paths)
        if status == "??":
            untracked.extend(row_paths)
    unique_paths = sorted(dict.fromkeys(paths))
    return {
        "schema_version": f"villani.{schema_prefix}_repository_status.v1",
        "status": "captured",
        "entries": entries,
        "forbidden_paths_touched": sorted(
            path for path in unique_paths if is_patch_excluded(path)
        ),
        "unsafe_paths": sorted(
            path for path in unique_paths if not _safe_path(worktree, path)
        ),
        "untracked_files": sorted(dict.fromkeys(untracked)),
    }


def _external_symlinks(worktree: Path) -> tuple[str, ...]:
    unsafe: list[str] = []
    for path in worktree.rglob("*"):
        if not path.is_symlink():
            continue
        try:
            path.resolve(strict=False).relative_to(worktree.resolve())
        except (OSError, ValueError):
            unsafe.append(path.relative_to(worktree).as_posix())
    return tuple(sorted(unsafe))


def _patch_quality(
    context: AttemptContext,
    patch: str,
    changed_files: list[str],
    status: Mapping[str, Any],
    *,
    path_violation: bool,
) -> CandidatePatchQuality:
    untracked = [str(item) for item in status.get("untracked_files", [])]
    added = sum(
        1
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1
        for line in patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    if path_violation:
        quality_status = "ineligible"
        reasons = ["path_violation"]
    elif not patch.strip():
        quality_status = "ineligible"
        reasons = ["empty_patch"]
    else:
        quality_status = "eligible"
        reasons = ["git_derived_patch_present"]
    return CandidatePatchQuality(
        schema_version="villani.candidate_patch_quality.v1",
        candidate_id=context.attempt_id,
        status=quality_status,
        tracked_files_changed=sorted(
            path for path in changed_files if path not in set(untracked)
        ),
        relevant_files_changed=sorted(changed_files),
        untracked_files=sorted(untracked),
        ignored_files=[],
        villani_owned_files=[
            str(item) for item in status.get("forbidden_paths_touched", [])
        ],
        generated_files=[],
        semantic_lines_added=added,
        semantic_lines_removed=removed,
        line_ending_only_lines=0,
        whitespace_only_lines=0,
        file_mode_only_changes=[],
        bulk_rewrite_files=[],
        relevant_diff_ratio=1.0 if patch.strip() else 0.0,
        reason_codes=reasons,
    )


def sanitize_and_parse_final(
    path: Path,
    *,
    model: type[_ResultModel],
    maximum_bytes: int,
    secrets: tuple[str, ...],
) -> tuple[_ResultModel | None, str | None]:
    if not path.is_file():
        return None, "final structured output is missing"
    try:
        data = path.read_bytes()
    except OSError as error:
        return None, f"final structured output could not be read: {error}"
    if len(data) > maximum_bytes:
        bounded = data[:maximum_bytes].decode("utf-8", errors="replace")
        path.write_text(str(redact_data(bounded, secrets=secrets)), encoding="utf-8")
        return None, f"final structured output exceeded {maximum_bytes} bytes"
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        path.write_text(
            str(redact_data(data.decode("utf-8", errors="replace"), secrets=secrets)),
            encoding="utf-8",
        )
        return None, f"final structured output is not UTF-8 at byte {error.start}"
    try:
        result = model.model_validate_json(text)
    except ValidationError as error:
        path.write_text(str(redact_data(text, secrets=secrets)), encoding="utf-8")
        issue = error.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(item) for item in issue.get("loc", ())) or "value"
        return None, f"{location}: {issue.get('msg', 'invalid structured output')}"
    safe = redact_data(result.model_dump(mode="json"), secrets=secrets)
    write_json_atomic(path, safe)
    return model.model_validate(safe), None


def write_normalized_events(path: Path, parsed: ParsedCliEvents) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in parsed.normalized_rows
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def _write_verifier_trace_projection(
    *,
    directory: Path,
    context: AttemptContext,
    worktree: Path,
    changed_files: list[str],
    parsed: ParsedCliEvents,
    provider_identity: Mapping[str, Any],
    process: CliProcessResult,
    secrets: tuple[str, ...],
    schema_prefix: str,
    execution_provider: str,
    normalized_events_reference: str,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        directory / "session_meta.json",
        redact_data(
            {
                "schema_version": f"villani.{schema_prefix}_verifier_trace.v1",
                "run_id": context.run_id,
                "attempt_id": context.attempt_id,
                "objective": context.task,
                "repo": str(worktree),
                "provider": execution_provider,
                "model": provider_identity.get("model")
                or provider_identity.get("configured_model"),
                "source_events": normalized_events_reference,
                "source_provider_identity": "../provider.json",
                "created_at": process.started_at.isoformat().replace("+00:00", "Z"),
            },
            secrets=secrets,
        ),
    )
    commands: list[Mapping[str, Any]] = []
    tool_calls: list[Mapping[str, Any]] = []
    model_responses: list[Mapping[str, Any]] = []
    for row in parsed.normalized_rows:
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload")
        values = payload if isinstance(payload, Mapping) else {}
        if event_type in {"command_completed", "command_failed"}:
            commands.append(
                {
                    "event_id": row.get("source_event_id"),
                    "ts": row.get("timestamp"),
                    "command": values.get("command"),
                    "command_role": values.get("command_role") or "unknown",
                    "candidate_state": "post_mutation",
                    "cwd": str(worktree),
                    "exit_code": values.get("exit_code"),
                    "stdout": values.get("output") or "",
                    "stderr": "",
                }
            )
        elif event_type in {
            "tool_call_started",
            "tool_call_completed",
            "subagent_started",
            "subagent_completed",
        }:
            tool_calls.append(
                {
                    "tool_call_id": row.get("source_event_id"),
                    "tool_name": values.get("tool") or "provider_tool",
                    "tool_category": "provider_tool",
                    "started_at": row.get("timestamp"),
                    "status": (
                        "completed" if event_type.endswith("completed") else "started"
                    ),
                    "args": {},
                    "result_summary": "Exposed by provider structured events.",
                }
            )
        elif event_type == "agent_message":
            model_responses.append(
                {
                    "event_id": row.get("source_event_id"),
                    "ts": row.get("timestamp"),
                    "text": values.get("text") or "",
                }
            )
    _write_jsonl(directory / "commands.jsonl", commands)
    _write_jsonl(directory / "tool_calls.jsonl", tool_calls)
    _write_jsonl(directory / "model_responses.jsonl", model_responses)
    _write_jsonl(directory / "patches.jsonl", [])
    _write_jsonl(directory / "validations.jsonl", [])
    summary = {
        "schema_version": f"villani.{schema_prefix}_verifier_trace_summary.v1",
        "status": (
            "completed" if process.infrastructure_state == "succeeded" else "failed"
        ),
        "duration_ms": process.duration_ms,
        "changed_files": changed_files,
        "tokens_input": parsed.input_tokens,
        "tokens_output": parsed.output_tokens,
        "source": f"{execution_provider}_structured_event_projection",
        "hidden_reasoning_included": False,
    }
    write_json_atomic(directory / "summary.json", summary)
    write_json_atomic(directory / "final_summary.json", summary)


def prepare_candidate(
    *,
    context: AttemptContext,
    isolation: GitIsolationAdapter,
    repository_directory: Path,
    secrets: tuple[str, ...],
    schema_prefix: str,
) -> PreparedCandidate:
    isolated = isolation.create(context)
    worktree = isolated.copied.worktree_path.resolve()
    source_baseline = dict(isolated.metadata["source_repository"])
    worktree_head = _git(worktree, "rev-parse", "HEAD")
    worktree_tree = _git(worktree, "rev-parse", "HEAD^{tree}")
    baseline_document = {
        "schema_version": f"villani.{schema_prefix}_repository_baseline.v1",
        "run_id": context.run_id,
        "attempt_id": context.attempt_id,
        "source_repository": source_baseline,
        "worktree": str(worktree),
        "worktree_head": (
            worktree_head.stdout.strip() if worktree_head.returncode == 0 else None
        ),
        "worktree_tree": (
            worktree_tree.stdout.strip() if worktree_tree.returncode == 0 else None
        ),
        "controller_baseline_sha256": context.baseline_sha256,
    }
    baseline_document["baseline_digest"] = _sha256_json(baseline_document)
    write_json_atomic(
        repository_directory / "baseline.json",
        redact_data(baseline_document, secrets=secrets),
    )
    return PreparedCandidate(
        isolated=isolated,
        worktree=worktree,
        source_baseline=source_baseline,
        baseline_document=baseline_document,
        external_symlinks=_external_symlinks(worktree),
    )


def collect_candidate_evidence(
    *,
    context: AttemptContext,
    prepared: PreparedCandidate,
    repository_directory: Path,
    process: CliProcessResult,
    parsed: ParsedCliEvents,
    provider_identity: Mapping[str, Any],
    execution_provider: str,
    environment_policy: str,
    secrets: tuple[str, ...],
    schema_prefix: str,
) -> CollectedCandidateEvidence:
    worktree = prepared.worktree
    status_document = _status_document(worktree, schema_prefix=schema_prefix)
    status_document["external_symlinks"] = list(prepared.external_symlinks)
    forbidden = tuple(str(item) for item in status_document["forbidden_paths_touched"])
    unsafe = tuple(str(item) for item in status_document["unsafe_paths"])
    target_error: str | None = None
    try:
        validate_target_identity(
            Path(context.repository_path), prepared.source_baseline
        )
    except ValueError as error:
        target_error = str(error)
    path_violation = bool(forbidden or unsafe or target_error)
    status_document.update(
        {
            "target_identity_preserved": target_error is None,
            "target_identity_error": target_error,
            "path_violation": path_violation,
        }
    )
    write_json_atomic(
        repository_directory / "status.json",
        redact_data(status_document, secrets=secrets),
    )

    capture = capture_candidate_patch(
        worktree, repository_directory / "candidate.patch"
    )
    patch_path = repository_directory / "candidate.patch"
    patch_bytes = patch_path.read_bytes() if patch_path.is_file() else b""
    prepared.isolated.patch_path.write_bytes(patch_bytes)
    patch = patch_bytes.decode("utf-8", errors="replace")
    changed_files = sorted(dict.fromkeys(capture.changed_files))
    changed_document = {
        "schema_version": "villani.changed_files.v1",
        "changed_files": changed_files,
        "added_files": capture.added_files,
        "deleted_files": capture.deleted_files,
        "modified_files": capture.modified_files,
        "renamed_files": capture.renamed_files,
        "name_status": capture.name_status,
        "has_non_empty_patch": bool(patch_bytes.strip()),
        "candidate_digest": f"sha256:{hashlib.sha256(patch_bytes).hexdigest()}",
        "capture_failure": capture.failure_reason,
    }
    write_json_atomic(repository_directory / "changed-files.json", changed_document)
    write_json_atomic(
        repository_directory / "cleanup.json",
        {
            "status": "retained_for_verification",
            "worktree": str(worktree),
            "process_tree_cleanup": process.cleanup_status,
        },
    )

    verifier_trace_directory = (
        Path(context.attempt_directory) / "agent" / "verifier-trace"
    )
    _write_verifier_trace_projection(
        directory=verifier_trace_directory,
        context=context,
        worktree=worktree,
        changed_files=changed_files,
        parsed=parsed,
        provider_identity=provider_identity,
        process=process,
        secrets=secrets,
        schema_prefix=schema_prefix,
        execution_provider=execution_provider,
        normalized_events_reference="../normalized-events.jsonl",
    )

    quality = _patch_quality(
        context,
        patch,
        changed_files,
        status_document,
        path_violation=path_violation,
    )
    fingerprint = _sha256_json(
        {
            "provider": provider_identity,
            "worktree": str(worktree),
            "baseline": prepared.baseline_document["baseline_digest"],
        }
    )
    repository_validation = RepositoryValidationReport(
        schema_version="villani.repository_validation.v2",
        run_id=context.run_id,
        attempt_id=context.attempt_id,
        candidate_id=context.attempt_id,
        execution_environment_fingerprint=fingerprint,
        execution_provider=execution_provider,
        commands=[],
        status="unavailable",
        authoritative=False,
        completed_at=process.completed_at.isoformat().replace("+00:00", "Z"),
        failure_code="repository_validation_unavailable",
    )
    attempt_directory = Path(context.attempt_directory)
    write_json_atomic(
        attempt_directory / "repository-validation.json", repository_validation
    )
    write_json_atomic(attempt_directory / "candidate-patch-quality.json", quality)
    candidate_manifest = write_candidate_bundle(
        context=context,
        worktree=worktree,
        patch=patch,
        changed_files=changed_files,
        source_repository=prepared.source_baseline,
        execution_environment_report={
            "schema_version": f"villani.{schema_prefix}_execution_environment.v1",
            "provider": execution_provider,
            "fingerprint": fingerprint,
            "environment_policy": environment_policy,
            "values_persisted": False,
        },
        repository_validation=repository_validation,
        candidate_patch_quality=quality,
        execution_provider=execution_provider,
        execution_environment_fingerprint=fingerprint,
        secrets=secrets,
    )
    if capture.failure_reason:
        changed_document["capture_failure"] = capture.failure_reason
    return CollectedCandidateEvidence(
        patch_path=patch_path,
        patch_bytes=patch_bytes,
        patch=patch,
        changed_files=tuple(changed_files),
        changed_document=changed_document,
        status_document=status_document,
        forbidden_paths=forbidden,
        unsafe_paths=unsafe,
        path_violation=path_violation,
        target_identity_error=target_error,
        quality=quality,
        repository_validation=repository_validation,
        candidate_manifest=candidate_manifest,
        fingerprint=fingerprint,
        verifier_trace_directory=verifier_trace_directory,
    )
