"""Construction and integrity checks for controlled classifier/selector inputs."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

FORBIDDEN_SELECTOR_KEYS = frozenset(
    {
        "attempt_number",
        "attempt_order",
        "backend",
        "cli_driver",
        "coder_session_id",
        "coder_transcript",
        "competing_candidate",
        "cost",
        "elapsed_time",
        "expected_patch",
        "hidden_solution",
        "model",
        "provider",
        "rank",
        "rejected_candidate",
        "route_rank",
        "route_score",
        "runtime_prestige",
        "token_count",
    }
)


class CliRoleWorkspaceError(RuntimeError):
    def __init__(self, message: str, *, workspace: Path | None = None) -> None:
        super().__init__(message)
        self.workspace = workspace


@dataclass(frozen=True, slots=True)
class PreparedCliRoleWorkspace:
    role: str
    invocation_id: str
    root: Path
    input_directory: Path
    output_directory: Path
    agent_directory: Path
    manifest_path: Path
    manifest_sha256: str
    prompt_bytes: bytes
    prompt_sha256: str
    prompt_reference: str
    output_schema_path: Path
    raw_result_path: Path
    normalized_result_path: Path
    target_repository: Path
    target_state_before: str
    candidate_states_before: tuple[tuple[Path, str], ...] = ()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_artifact(root: Path, path: Path) -> str:
    if path.is_symlink():
        raise CliRoleWorkspaceError(
            f"role input artifact must not be a symlink: {path.name}", workspace=root
        )
    resolved = path.resolve()
    input_root = (root / "input").resolve()
    if not resolved.is_relative_to(input_root):
        raise CliRoleWorkspaceError(
            "role input artifact escaped the controlled input directory", workspace=root
        )
    return resolved.relative_to(root.resolve()).as_posix()


def _forbidden_key(value: Any, *, forbidden: frozenset[str]) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in forbidden:
                return str(key)
            found = _forbidden_key(child, forbidden=forbidden)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _forbidden_key(child, forbidden=forbidden)
            if found is not None:
                return found
    return None


def assert_selector_blindness(value: Any) -> None:
    found = _forbidden_key(value, forbidden=FORBIDDEN_SELECTOR_KEYS)
    if found is not None:
        raise CliRoleWorkspaceError(
            f"selector input contains forbidden identity or ordering key: {found}"
        )


def repository_state_digest(repository: Path) -> str:
    """Hash repository-visible file state without mutating it."""

    root = Path(repository).resolve()
    digest = hashlib.sha256()
    if not root.is_dir():
        raise CliRoleWorkspaceError(f"repository does not exist: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if ".git" in path.relative_to(root).parts or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        if path.is_file():
            try:
                digest.update(path.read_bytes())
            except OSError as error:
                raise CliRoleWorkspaceError(
                    f"could not hash repository state: {error}"
                ) from error
    return digest.hexdigest()


def _make_inputs_read_only(input_directory: Path) -> None:
    for path in sorted(input_directory.rglob("*"), reverse=True):
        try:
            if path.is_file():
                path.chmod(stat.S_IREAD)
            elif path.is_dir():
                path.chmod(stat.S_IREAD | stat.S_IEXEC)
        except OSError as error:
            raise CliRoleWorkspaceError(
                f"could not make role input read-only: {error}",
                workspace=input_directory.parent,
            ) from error


def prepare_cli_role_workspace(
    *,
    role: str,
    invocation_id: str,
    run_directory: Path,
    target_repository: Path,
    input_documents: Mapping[str, tuple[str, Any]],
    prompt_bytes: bytes,
    output_schema_source: Path,
    raw_result_filename: str,
    normalized_result_filename: str,
    blindness: Mapping[str, bool],
    candidate_worktrees: tuple[Path, ...] = (),
) -> PreparedCliRoleWorkspace:
    run_root = Path(run_directory).resolve()
    target = Path(target_repository).resolve()
    root = (run_root / role / invocation_id).resolve()
    candidates = tuple(Path(item).resolve() for item in candidate_worktrees)
    if root.is_relative_to(target) or any(
        root.is_relative_to(item) for item in candidates
    ):
        raise CliRoleWorkspaceError(
            f"{role} workspace must be outside target and candidate repositories",
            workspace=root,
        )
    if root.exists():
        raise CliRoleWorkspaceError(
            f"fresh {role} workspace already exists", workspace=root
        )
    input_directory = root / "input"
    output_directory = root / "output"
    agent_directory = root / "agent"
    try:
        input_directory.mkdir(parents=True)
        output_directory.mkdir()
        agent_directory.mkdir()
    except OSError as error:
        raise CliRoleWorkspaceError(
            f"could not create {role} workspace: {error}", workspace=root
        ) from error

    target_state = repository_state_digest(target)
    candidate_states = tuple(
        (candidate, repository_state_digest(candidate)) for candidate in candidates
    )
    artifacts: list[dict[str, Any]] = []
    try:
        for filename, (kind, document) in input_documents.items():
            if Path(filename).name != filename:
                raise CliRoleWorkspaceError(
                    f"invalid controlled input filename: {filename}", workspace=root
                )
            data = canonical_json_bytes(document)
            path = input_directory / filename
            path.write_bytes(data)
            artifacts.append(
                {
                    "path": _safe_artifact(root, path),
                    "kind": kind,
                    "sha256": sha256_bytes(data),
                    "bytes": len(data),
                }
            )
        if not output_schema_source.is_file():
            raise CliRoleWorkspaceError(
                f"packaged {role} output schema is missing", workspace=root
            )
        schema_data = output_schema_source.read_bytes()
        schema_path = input_directory / output_schema_source.name
        schema_path.write_bytes(schema_data)
        artifacts.append(
            {
                "path": _safe_artifact(root, schema_path),
                "kind": "output_schema",
                "sha256": sha256_bytes(schema_data),
                "bytes": len(schema_data),
            }
        )
        prompt_path = input_directory / "role-prompt.txt"
        prompt_path.write_bytes(prompt_bytes)
        artifacts.append(
            {
                "path": _safe_artifact(root, prompt_path),
                "kind": "controlled_prompt",
                "sha256": sha256_bytes(prompt_bytes),
                "bytes": len(prompt_bytes),
            }
        )
        manifest = {
            "schema_version": f"villani.cli_{role}_input_manifest.v1",
            "role": role,
            "invocation_id": invocation_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "artifacts": sorted(artifacts, key=lambda item: item["path"]),
            "blindness": dict(blindness),
            "security": {
                "input_read_only": True,
                "target_repository_access": False,
                "candidate_worktree_access": False,
                "session_persistence": False,
                "session_resume": False,
                "ambient_project_instructions": False,
                "ambient_plugins": False,
                "ambient_hooks": False,
                "ambient_mcp": False,
                "ambient_memory": False,
            },
        }
        manifest_data = canonical_json_bytes(manifest)
        manifest_path = input_directory / "manifest.json"
        manifest_path.write_bytes(manifest_data)
        _safe_artifact(root, manifest_path)
        _make_inputs_read_only(input_directory)
    except CliRoleWorkspaceError:
        raise
    except OSError as error:
        raise CliRoleWorkspaceError(
            f"could not prepare {role} artifacts: {error}", workspace=root
        ) from error

    return PreparedCliRoleWorkspace(
        role=role,
        invocation_id=invocation_id,
        root=root,
        input_directory=input_directory,
        output_directory=output_directory,
        agent_directory=agent_directory,
        manifest_path=manifest_path,
        manifest_sha256=sha256_bytes(manifest_data),
        prompt_bytes=prompt_bytes,
        prompt_sha256=f"sha256:{sha256_bytes(prompt_bytes)}",
        prompt_reference="input/role-prompt.txt",
        output_schema_path=schema_path,
        raw_result_path=output_directory / raw_result_filename,
        normalized_result_path=output_directory / normalized_result_filename,
        target_repository=target,
        target_state_before=target_state,
        candidate_states_before=candidate_states,
    )


def verify_cli_role_manifest(
    workspace: PreparedCliRoleWorkspace,
) -> tuple[bool, str]:
    try:
        manifest_bytes = workspace.manifest_path.read_bytes()
        if sha256_bytes(manifest_bytes) != workspace.manifest_sha256:
            return False, "input manifest digest changed"
        manifest = json.loads(manifest_bytes)
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            return False, "input manifest artifact list is missing"
        expected: set[str] = {"input/manifest.json"}
        for record in artifacts:
            if not isinstance(record, Mapping):
                return False, "input manifest contains malformed artifact record"
            relative = str(record.get("path") or "")
            path = (workspace.root / relative).resolve()
            if not relative.startswith("input/") or not path.is_relative_to(
                workspace.input_directory.resolve()
            ):
                return False, "input manifest contains path traversal"
            if path.is_symlink() or not path.is_file():
                return False, f"input artifact is missing or symlinked: {relative}"
            data = path.read_bytes()
            if record.get("sha256") != sha256_bytes(data) or record.get("bytes") != len(
                data
            ):
                return False, f"input artifact digest mismatch: {relative}"
            expected.add(relative)
        actual = {
            path.relative_to(workspace.root).as_posix()
            for path in workspace.input_directory.rglob("*")
            if path.is_file()
        }
        if actual != expected:
            return False, "controlled input directory contains unmanifested artifacts"
        return True, "input manifest and artifact digests verified"
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return False, f"input manifest verification failed: {error}"


def restore_controller_writes(workspace: PreparedCliRoleWorkspace) -> None:
    """Restore controller ownership after the agent process has exited."""

    try:
        workspace.input_directory.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    except OSError:
        pass


__all__ = [
    "CliRoleWorkspaceError",
    "FORBIDDEN_SELECTOR_KEYS",
    "PreparedCliRoleWorkspace",
    "assert_selector_blindness",
    "canonical_json_bytes",
    "prepare_cli_role_workspace",
    "repository_state_digest",
    "restore_controller_writes",
    "sha256_bytes",
    "verify_cli_role_manifest",
]
