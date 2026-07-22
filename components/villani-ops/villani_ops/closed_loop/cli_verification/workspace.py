"""Blind, immutable input workspace construction for CLI verification."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from villani_ops.execution_environment.secrets import registered_secret_values
from villani_ops.isolation.copy_git import copy_worktree, remove_tree

from ..adapters.git_isolation import validate_target_identity
from ..durable_io import write_json_atomic
from ..interfaces import AttemptContext, AttemptResult
from ..repository_validation import load_repository_validation_report
from ..verification_evidence import RequirementDefinition, extract_requirements
from .models import CliVerifierFailure
from .prompt import CliVerifierPrompt, build_cli_verifier_prompt


SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas" / "v1"
_FORBIDDEN_MANIFEST_KEYS = frozenset(
    {
        "provider",
        "provider_identity",
        "model",
        "cost",
        "rank",
        "candidate_id",
        "candidate_number",
        "attempt_id",
        "run_id",
        "session_id",
        "coder_transcript",
        "selector_output",
        "competing_candidates",
        "expected_patch",
        "hidden_solution",
    }
)


class WorkspacePreparationError(RuntimeError):
    def __init__(
        self,
        failure: CliVerifierFailure,
        message: str,
        *,
        workspace: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure
        self.workspace = workspace


@dataclass(frozen=True, slots=True)
class PreparedVerifierWorkspace:
    root: Path
    input_directory: Path
    output_directory: Path
    agent_directory: Path
    manifest_path: Path
    manifest_sha256: str
    prompt: CliVerifierPrompt
    definitions: tuple[RequirementDefinition, ...]
    target_state_before: str
    candidate_state_before: str
    target_repository: Path
    candidate_worktree: Path


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        check=False,
    )


def repository_state_digest(repo: Path) -> str:
    """Digest current tracked bytes, untracked bytes, and Git status without mutation."""

    root = Path(repo).resolve()
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    digest = hashlib.sha256()
    if inside.returncode == 0 and inside.stdout.strip() == b"true":
        listed = _git(
            root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
        )
        status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if listed.returncode != 0 or status.returncode != 0:
            raise OSError("could not snapshot Git repository state")
        digest.update(b"git-state-v1\0")
        digest.update(status.stdout)
        for raw in sorted(item for item in listed.stdout.split(b"\0") if item):
            relative = raw.decode("utf-8", errors="surrogateescape")
            path = root / relative
            digest.update(raw)
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0")
                digest.update(
                    os.readlink(path).encode("utf-8", errors="surrogateescape")
                )
            elif path.is_file():
                digest.update(path.read_bytes())
            else:
                digest.update(b"missing-or-non-regular")
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    digest.update(b"tree-state-v1\0")
    for directory, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories if name not in {".git", ".villani"}
        )
        directory_path = Path(directory)
        for name in sorted(filenames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(
                    os.readlink(path).encode("utf-8", errors="surrogateescape")
                )
            else:
                digest.update(path.read_bytes())
            digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _safe_changed_files(values: object, *, workspace: Path) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise WorkspacePreparationError(
            CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE,
            "candidate changed-file manifest is missing",
            workspace=workspace,
        )
    output: list[str] = []
    for raw in values:
        value = str(raw).replace("\\", "/")
        path = PurePosixPath(value)
        if (
            not value
            or value.startswith("/")
            or path.is_absolute()
            or ".." in path.parts
            or ":" in path.parts[0]
        ):
            raise WorkspacePreparationError(
                CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE,
                f"unsafe changed-file path {value!r}",
                workspace=workspace,
            )
        if value not in output:
            output.append(value)
    return sorted(output)


def _blind_values(
    context: AttemptContext, attempt_result: AttemptResult
) -> tuple[str, ...]:
    metadata = attempt_result.metadata
    values: list[object] = [
        context.repository_path,
        attempt_result.worktree_path,
        attempt_result.model,
        attempt_result.runner_name,
        attempt_result.cost_usd,
        metadata.get("agent_system_id"),
        metadata.get("codex_thread_id"),
        metadata.get("claude_code_session_id"),
        metadata.get("provider_identity_path"),
    ]
    return tuple(
        sorted(
            {str(value) for value in values if value not in {None, ""}},
            key=len,
            reverse=True,
        )
    )


def _blind_text(value: object, *, blind_values: Sequence[str]) -> str:
    text = str(value or "")
    for secret in (*registered_secret_values(), *blind_values):
        if secret:
            text = text.replace(secret, "[REDACTED_VERIFIER_BLIND_FIELD]")
    return text


def _validation_projection(
    context: AttemptContext,
    attempt_result: AttemptResult,
) -> dict[str, Any]:
    blind_values = _blind_values(context, attempt_result)
    try:
        report = load_repository_validation_report(Path(context.attempt_directory))
    except Exception:
        report = None
    if report is None:
        return {
            "schema_version": "villani.cli_verifier_validation_evidence.v1",
            "status": str(
                attempt_result.metadata.get("repository_validation_status")
                or "unavailable"
            ),
            "authoritative": bool(
                attempt_result.metadata.get("repository_validation_authoritative")
            ),
            "failure_code": _blind_text(
                attempt_result.metadata.get("repository_validation_failure_code"),
                blind_values=blind_values,
            )
            or None,
            "commands": [],
        }
    return {
        "schema_version": "villani.cli_verifier_validation_evidence.v1",
        "status": report.status,
        "authoritative": report.authoritative,
        "failure_code": report.failure_code,
        "commands": [
            {
                "validation_id": item.validation_id,
                "argv": [
                    _blind_text(argument, blind_values=blind_values)
                    for argument in item.argv
                ],
                "status": item.status,
                "exit_code": item.exit_code,
                "stdout": _blind_text(item.stdout, blind_values=blind_values),
                "stderr": _blind_text(item.stderr, blind_values=blind_values),
                "stdout_truncated": item.stdout_truncated,
                "stderr_truncated": item.stderr_truncated,
                "failure_code": item.failure_code,
            }
            for item in report.commands
        ],
    }


def _debug_projection(attempt_result: AttemptResult) -> dict[str, Any]:
    value = attempt_result.metadata.get("candidate_quality_report")
    source = value if isinstance(value, Mapping) else {}
    return {
        "schema_version": "villani.cli_verifier_debug_artifacts.v1",
        "coder_transcript_included": False,
        "artifacts": ["candidate-patch-quality.json"] if source else [],
    }


_QUALITY_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "tracked_files_changed",
        "relevant_files_changed",
        "untracked_files",
        "ignored_files",
        "villani_owned_files",
        "generated_files",
        "semantic_lines_added",
        "semantic_lines_removed",
        "line_ending_only_lines",
        "whitespace_only_lines",
        "file_mode_only_changes",
        "bulk_rewrite_files",
        "relevant_diff_ratio",
        "reason_codes",
    }
)


def _blind_json(value: Any, *, blind_values: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _blind_text(value, blind_values=blind_values)
    if isinstance(value, list):
        return [_blind_json(item, blind_values=blind_values) for item in value]
    if isinstance(value, tuple):
        return [_blind_json(item, blind_values=blind_values) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _blind_json(item, blind_values=blind_values)
            for key, item in value.items()
        }
    return value


def _quality_projection(
    context: AttemptContext, attempt_result: AttemptResult
) -> dict[str, Any]:
    value = attempt_result.metadata.get("candidate_quality_report")
    source = dict(value) if isinstance(value, Mapping) else {}
    neutral = {key: source[key] for key in _QUALITY_FIELDS if key in source}
    return _blind_json(
        neutral,
        blind_values=_blind_values(context, attempt_result),
    )


def _artifact_kind(relative: str) -> str:
    if relative.startswith("input/original-repository/"):
        return "original_repository_file"
    return {
        "input/task.json": "task",
        "input/success-criteria.json": "success_criteria",
        "input/candidate.patch": "candidate_patch",
        "input/changed-files.json": "changed_files",
        "input/validation-evidence.json": "validation_evidence",
        "input/verifier-result.schema.json": "output_schema",
        "input/verifier-prompt.txt": "verifier_prompt",
    }.get(relative, "debug_artifact")


def _artifact_records(root: Path, input_directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    secret_bytes = tuple(
        item.encode("utf-8", errors="ignore")
        for item in registered_secret_values()
        if item
    )
    for path in sorted(item for item in input_directory.rglob("*") if item.is_file()):
        if path.name == "manifest.json":
            continue
        if path.is_symlink():
            raise WorkspacePreparationError(
                CliVerifierFailure.INPUT_MANIFEST_VIOLATION,
                f"input artifact is a symlink: {path}",
                workspace=root,
            )
        data = path.read_bytes()
        if any(secret in data for secret in secret_bytes):
            raise WorkspacePreparationError(
                CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE,
                "a registered secret was detected in verifier input",
                workspace=root,
            )
        relative = path.relative_to(root).as_posix()
        records.append(
            {
                "path": relative,
                "kind": _artifact_kind(relative),
                "sha256": _sha256(data),
                "bytes": len(data),
            }
        )
    return records


def _manifest_has_forbidden_key(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_MANIFEST_KEYS:
                return str(key)
            nested = _manifest_has_forbidden_key(item)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _manifest_has_forbidden_key(item)
            if nested is not None:
                return nested
    return None


def _make_read_only(root: Path) -> None:
    paths = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for path in paths:
        if path.is_symlink():
            raise WorkspacePreparationError(
                CliVerifierFailure.INPUT_MANIFEST_VIOLATION,
                f"input symlink detected after preparation: {path}",
                workspace=root.parent,
            )
        mode = stat.S_IREAD | (stat.S_IEXEC if path.is_dir() else 0)
        path.chmod(mode)
    root.chmod(stat.S_IREAD | stat.S_IEXEC)


def prepare_verifier_workspace(
    context: AttemptContext,
    attempt_result: AttemptResult,
) -> PreparedVerifierWorkspace:
    verification_root = Path(context.run_directory).resolve() / "verification"
    target = Path(context.repository_path).resolve()
    candidate = Path(attempt_result.worktree_path).resolve()
    if verification_root.is_relative_to(target) or verification_root.is_relative_to(
        candidate
    ):
        raise WorkspacePreparationError(
            CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE,
            "verifier workspace must be outside target and candidate repositories",
        )
    verification_root.mkdir(parents=True, exist_ok=True)
    root = verification_root / f"vfy_{uuid.uuid4().hex}"
    input_directory = root / "input"
    output_directory = root / "output"
    agent_directory = root / "agent"
    for path in (input_directory, output_directory, agent_directory):
        path.mkdir(parents=True, exist_ok=False)

    definitions = tuple(
        extract_requirements(
            task_instruction=context.task,
            success_criteria=context.success_criteria,
            policy_configuration=context.policy_configuration,
        )
    )
    prompt = build_cli_verifier_prompt()
    try:
        target_state = repository_state_digest(target)
        candidate_state = repository_state_digest(candidate)
    except Exception as error:
        raise WorkspacePreparationError(
            CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE,
            f"repository state snapshot failed: {type(error).__name__}: {error}",
            workspace=root,
        ) from error

    changed_files = _safe_changed_files(
        attempt_result.metadata.get("changed_files", ()), workspace=root
    )
    task_document = {
        "schema_version": "villani.cli_verifier_task.v1",
        "task": context.task,
    }
    criteria_document = {
        "schema_version": "villani.cli_verifier_success_criteria.v1",
        "success_criteria": context.success_criteria,
        "requirements": [
            {
                "requirement_id": item.requirement_id,
                "description": item.description,
                "critical": item.critical,
                "observable": item.observable,
            }
            for item in definitions
        ],
    }
    write_json_atomic(input_directory / "task.json", task_document)
    write_json_atomic(input_directory / "success-criteria.json", criteria_document)
    patch_bytes = (attempt_result.patch or "").encode("utf-8")
    (input_directory / "candidate.patch").write_bytes(patch_bytes)
    write_json_atomic(
        input_directory / "changed-files.json",
        {
            "schema_version": "villani.cli_verifier_changed_files.v1",
            "changed_files": changed_files,
        },
    )
    write_json_atomic(
        input_directory / "validation-evidence.json",
        _validation_projection(context, attempt_result),
    )
    debug_directory = input_directory / "debug-artifacts"
    debug_directory.mkdir()
    write_json_atomic(debug_directory / "index.json", _debug_projection(attempt_result))
    write_json_atomic(
        debug_directory / "candidate-patch-quality.json",
        _quality_projection(context, attempt_result),
    )
    write_json_atomic(
        debug_directory / "session_meta.json",
        {
            "schema_version": "villani.cli_verifier_debug_boundary.v1",
            "source": "verifier_input_boundary",
            "coder_transcript_included": False,
        },
    )
    (input_directory / "verifier-prompt.txt").write_bytes(prompt.bytes)
    schema_source = SCHEMA_ROOT / "cli-verifier-result.schema.json"
    if not schema_source.is_file():
        raise WorkspacePreparationError(
            CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE,
            "packaged CLI verifier result schema is missing",
            workspace=root,
        )
    (input_directory / "verifier-result.schema.json").write_bytes(
        schema_source.read_bytes()
    )

    original_repository = input_directory / "original-repository"
    try:
        isolation = context.policy_configuration.get("isolation")
        settings = isolation if isinstance(isolation, Mapping) else {}
        baseline = attempt_result.metadata.get("worktree")
        baseline = baseline if isinstance(baseline, Mapping) else {}
        source_identity = baseline.get("source_repository")
        if isinstance(source_identity, dict) and source_identity:
            validate_target_identity(target, dict(source_identity))
        copy_worktree(
            target,
            original_repository,
            include_untracked_attempt_files=bool(
                baseline.get(
                    "include_untracked_attempt_files",
                    settings.get("include_untracked_attempt_files", False),
                )
            ),
            max_file_size_bytes=int(
                settings.get("max_file_size_bytes", 50 * 1024 * 1024)
            ),
            max_total_size_bytes=int(
                settings.get("max_total_size_bytes", 500 * 1024 * 1024)
            ),
        )
        symlinks = [
            path for path in original_repository.rglob("*") if path.is_symlink()
        ]
        if symlinks:
            remove_tree(original_repository)
            raise OSError("baseline contains a symlink and cannot be exposed safely")
    except Exception as error:
        if original_repository.exists() or original_repository.is_symlink():
            remove_tree(original_repository)
        raise WorkspacePreparationError(
            CliVerifierFailure.BASELINE_COPY_FAILURE,
            f"original repository baseline copy failed: {type(error).__name__}: {error}",
            workspace=root,
        ) from error

    artifacts = _artifact_records(root, input_directory)
    baseline_artifacts = [
        item for item in artifacts if item["kind"] == "original_repository_file"
    ]
    tree_digest = _sha256(
        _json_bytes(
            {
                "files": [
                    {"path": item["path"], "sha256": item["sha256"]}
                    for item in baseline_artifacts
                ]
            }
        )
    )
    manifest = {
        "schema_version": "villani.cli_verifier_input_manifest.v1",
        "digest_scope": "all_supplied_artifacts_except_manifest_self",
        "artifacts": artifacts,
        "original_repository": {
            "representation": "immutable_read_only_copy",
            "root": "input/original-repository",
            "file_count": len(baseline_artifacts),
            "tree_sha256": tree_digest,
        },
        "access_policy": {
            "readable_root": "input",
            "writable_roots": ["output", "agent"],
            "target_repository_referenced": False,
            "candidate_worktree_referenced": False,
        },
        "blindness": {
            "coder_transcript_included": False,
            "coder_session_included": False,
            "provider_identity_included": False,
            "model_identity_included": False,
            "cost_included": False,
            "elapsed_time_included": False,
            "rank_included": False,
            "candidate_number_included": False,
            "competing_candidate_included": False,
            "selector_output_included": False,
            "expected_patch_included": False,
            "hidden_solution_included": False,
        },
    }
    forbidden_key = _manifest_has_forbidden_key(manifest)
    if forbidden_key is not None:
        raise WorkspacePreparationError(
            CliVerifierFailure.INPUT_MANIFEST_VIOLATION,
            f"input manifest contains forbidden field {forbidden_key!r}",
            workspace=root,
        )
    manifest_path = input_directory / "manifest.json"
    write_json_atomic(manifest_path, manifest)
    manifest_sha256 = _sha256(manifest_path.read_bytes())
    _make_read_only(input_directory)
    return PreparedVerifierWorkspace(
        root=root,
        input_directory=input_directory,
        output_directory=output_directory,
        agent_directory=agent_directory,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        prompt=prompt,
        definitions=definitions,
        target_state_before=target_state,
        candidate_state_before=candidate_state,
        target_repository=target,
        candidate_worktree=candidate,
    )


def verify_input_manifest(workspace: PreparedVerifierWorkspace) -> tuple[bool, str]:
    try:
        manifest_bytes = workspace.manifest_path.read_bytes()
        if _sha256(manifest_bytes) != workspace.manifest_sha256:
            return False, "input manifest changed after preparation"
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        artifacts = manifest["artifacts"]
        if not isinstance(artifacts, list):
            return False, "input manifest artifacts field is not a list"
        expected_paths = {"input/manifest.json"}
        for item in artifacts:
            if not isinstance(item, Mapping):
                return False, "input manifest contains a malformed artifact record"
            relative = str(item["path"])
            pure = PurePosixPath(relative)
            if (
                not relative.startswith("input/")
                or pure.is_absolute()
                or ".." in pure.parts
            ):
                return False, f"input manifest contains unsafe path: {relative}"
            expected_paths.add(relative)
            path = workspace.root / relative
            if not path.is_file() or path.is_symlink():
                return (
                    False,
                    f"supplied artifact disappeared or became unsafe: {item['path']}",
                )
            data = path.read_bytes()
            if _sha256(data) != item["sha256"] or len(data) != item["bytes"]:
                return False, f"supplied artifact changed: {item['path']}"
        actual_paths: set[str] = set()
        for path in workspace.input_directory.rglob("*"):
            if path.is_symlink():
                return False, f"unsafe symlink appeared in verifier input: {path.name}"
            if path.is_file():
                actual_paths.add(path.relative_to(workspace.root).as_posix())
                if path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                    return False, f"input artifact became writable: {path.name}"
        if actual_paths != expected_paths:
            return False, "verifier input artifact set no longer matches its manifest"
        if _manifest_has_forbidden_key(manifest) is not None:
            return False, "input manifest contains a forbidden field"
    except Exception as error:
        return (
            False,
            f"input manifest verification failed: {type(error).__name__}: {error}",
        )
    return True, "Every supplied input artifact retained its recorded digest."


__all__ = [
    "PreparedVerifierWorkspace",
    "WorkspacePreparationError",
    "prepare_verifier_workspace",
    "repository_state_digest",
    "verify_input_manifest",
]
