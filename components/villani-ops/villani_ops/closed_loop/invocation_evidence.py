"""Provider-neutral public evidence for independently supervised CLI processes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, ValidationError

from .agent_systems.role_models import AgentRole
from .cli_runtime.models import CliInvocationRecord, CliProcessResult
from .cli_runtime.failure_presentation import (
    build_cli_failure_presentation,
    write_cli_failure_presentation,
)
from .protocol import StrictProtocolModel


ROLE_INVOCATION_EVIDENCE_SCHEMA_VERSION = "villani.role_invocation_evidence.v1"
ROLE_INVOCATION_INDEX_SCHEMA_VERSION = "villani.role_invocation_index.v1"


def _safe_display_path(path: str | None) -> str | None:
    """Return a useful executable path without publishing a home directory."""

    if not path:
        return None
    value = str(Path(path))
    try:
        home = str(Path.home().resolve())
    except OSError:
        return value
    if value.casefold().startswith(home.casefold()):
        suffix = value[len(home) :].lstrip("/\\")
        return str(Path("<home>") / suffix) if suffix else "<home>"
    return value


class InvocationAccounting(StrictProtocolModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    accounting_status: Literal["complete", "unknown"]


class InvocationCost(StrictProtocolModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = Field(default=None, ge=0)
    currency: str | None = None
    accounting_status: Literal["complete", "unknown"]
    source: str | None = None


class RoleInvocationEvidence(StrictProtocolModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["villani.role_invocation_evidence.v1"] = (
        "villani.role_invocation_evidence.v1"
    )
    invocation_id: str = Field(pattern=r"^rinv_[0-9a-f]{64}$")
    role: AgentRole
    agent_system_id: str = Field(min_length=1)
    driver: Literal["codex", "claude_code"]
    executable_path_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    safe_display_path: str = Field(min_length=1)
    cli_version: str | None = None
    configured_model: str = Field(min_length=1)
    resolved_model: str | None = None
    instruction_policy: str = Field(min_length=1)
    permission_policy: str = Field(min_length=1)
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    duration_ms: int = Field(ge=0)
    exit_code: int | None = None
    infrastructure_state: Literal["succeeded", "failed", "cancelled", "timed_out"]
    usage: InvocationAccounting
    cost: InvocationCost
    artifact_links: list[str]
    restricted_process_artifact: str = Field(min_length=1)


class RoleInvocationIndex(StrictProtocolModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["villani.role_invocation_index.v1"] = (
        "villani.role_invocation_index.v1"
    )
    invocations: list[RoleInvocationEvidence]
    roles: dict[AgentRole, list[str]]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _path_digest(value: str) -> str:
    normalized = str(Path(value)).replace("\\", "/").casefold()
    return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _normalized_observations(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not path.is_file():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return result
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, Mapping):
            continue
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if isinstance(payload.get("model"), str) and payload.get("model"):
            result["resolved_model"] = str(payload["model"])
        for key in ("input_tokens", "output_tokens"):
            candidate = payload.get(key)
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate >= 0
            ):
                result[key] = candidate
        cost = payload.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            result["cost_usd"] = float(cost)
    return result


def _process_paths(run_directory: Path) -> list[Path]:
    paths: list[Path] = []
    resolved_run = run_directory.resolve()
    for role_root in ("classification", "attempts", "verification", "selection"):
        root = run_directory / role_root
        if not root.is_dir():
            continue
        for path in root.glob("*/agent/process-result.json"):
            resolved = path.resolve()
            try:
                resolved.relative_to(resolved_run)
            except ValueError:
                continue
            paths.append(resolved)
    return sorted(set(paths), key=lambda item: item.as_posix())


def collect_role_invocations(
    run_directory: Path,
    planned_identities: Sequence[Mapping[str, Any]],
) -> RoleInvocationIndex:
    """Collect only validated supervisor records inside the canonical run root."""

    root = run_directory.resolve()
    planned_by_role = {str(item.get("role")): dict(item) for item in planned_identities}
    invocations: list[RoleInvocationEvidence] = []
    roles: dict[AgentRole, list[str]] = {role: [] for role in AgentRole}
    for process_path in _process_paths(root):
        invocation_path = process_path.with_name("invocation.json")
        process = _read_json(process_path)
        invocation = _read_json(invocation_path)
        try:
            process = CliProcessResult.model_validate(process).model_dump(mode="json")
            invocation = CliInvocationRecord.model_validate(invocation).model_dump(
                mode="json"
            )
        except ValidationError:
            continue
        workspace = invocation.get("role_workspace_identity")
        if not isinstance(workspace, Mapping):
            continue
        try:
            role = AgentRole(str(workspace.get("role")))
        except ValueError:
            continue
        planned = planned_by_role.get(role.value, {})
        driver = str(workspace.get("driver") or planned.get("driver") or "")
        if driver not in {"codex", "claude_code"}:
            continue
        system_id = str(
            workspace.get("agent_system_id") or planned.get("agent_system_id") or ""
        )
        configured_model = str(
            workspace.get("configured_model") or planned.get("model") or ""
        )
        if not system_id or not configured_model:
            continue
        executable = str(invocation.get("executable") or "unknown")
        executable_identity = invocation.get("executable_identity")
        supplied_digest = (
            executable_identity.get("sha256")
            if isinstance(executable_identity, Mapping)
            else None
        )
        digest = (
            str(supplied_digest)
            if isinstance(supplied_digest, str)
            and supplied_digest.startswith("sha256:")
            and len(supplied_digest) == 71
            else _path_digest(executable)
        )
        normalized_path = process_path.with_name("normalized-events.jsonl")
        observations = _normalized_observations(normalized_path)
        input_tokens = observations.get("input_tokens")
        output_tokens = observations.get("output_tokens")
        token_complete = isinstance(input_tokens, int) and isinstance(
            output_tokens, int
        )
        cost_value = observations.get("cost_usd")
        artifacts = [
            candidate
            for candidate in (
                invocation_path,
                process_path,
                process_path.with_name("stdout.log"),
                process_path.with_name("stderr.log"),
                process_path.with_name("raw-events.jsonl"),
                normalized_path,
            )
            if candidate.is_file()
        ]
        if process.get("infrastructure_state") != "succeeded":
            independence = _read_json(process_path.with_name("independence.json"))
            repository_status = _read_json(
                process_path.parent.parent / "repository" / "status.json"
            )
            target_unchanged = independence.get("target_repository_unchanged")
            if not isinstance(target_unchanged, bool):
                target_unchanged = repository_status.get("target_identity_preserved")
            if not isinstance(target_unchanged, bool):
                target_unchanged = False
            normalized_result = _read_json(
                process_path.parent.parent / "output" / "normalized-result.json"
            )
            patch_candidates = (
                process_path.parent.parent / "repository" / "candidate.patch",
                process_path.parent.parent / "patch.diff",
            )
            partial_patch_preserved = role == AgentRole.CODING and any(
                candidate.is_file() and bool(candidate.read_bytes().strip())
                for candidate in patch_candidates
            )
            failure_path = process_path.with_name("infrastructure-failure.json")
            failure = build_cli_failure_presentation(
                role=role,
                agent_system_id=system_id,
                process=process,
                target_repository_modified=not target_unchanged,
                partial_patch_preserved=partial_patch_preserved,
                automatic_fallback_performed=bool(
                    normalized_result.get("fallback_used")
                ),
                evidence_path=_relative(root, process_path),
            )
            write_cli_failure_presentation(failure_path, failure)
            artifacts.append(failure_path)
        identity_bytes = json.dumps(
            {
                "path": _relative(root, process_path),
                "started_at": process.get("started_at"),
                "system_id": system_id,
                "role": role.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        invocation_id = f"rinv_{hashlib.sha256(identity_bytes).hexdigest()}"
        record = RoleInvocationEvidence(
            invocation_id=invocation_id,
            role=role,
            agent_system_id=system_id,
            driver=driver,  # type: ignore[arg-type]
            executable_path_digest=digest,
            safe_display_path=_safe_display_path(executable) or executable,
            cli_version=(
                str(workspace.get("cli_version"))
                if workspace.get("cli_version")
                else None
            ),
            configured_model=configured_model,
            resolved_model=observations.get("resolved_model"),
            instruction_policy=str(
                workspace.get("instruction_policy") or "villani_controlled"
            ),
            permission_policy=str(
                workspace.get("permission_policy")
                or ("workspace_write" if role == AgentRole.CODING else "read_only")
            ),
            started_at=str(process.get("started_at") or invocation.get("started_at")),
            completed_at=str(process.get("completed_at")),
            duration_ms=int(process.get("duration_ms") or 0),
            exit_code=(
                int(process["exit_code"])
                if isinstance(process.get("exit_code"), int)
                else None
            ),
            infrastructure_state=str(process.get("infrastructure_state")),  # type: ignore[arg-type]
            usage=InvocationAccounting(
                input_tokens=input_tokens if token_complete else None,
                output_tokens=output_tokens if token_complete else None,
                accounting_status="complete" if token_complete else "unknown",
            ),
            cost=InvocationCost(
                value=float(cost_value)
                if isinstance(cost_value, (int, float))
                else None,
                currency="USD" if isinstance(cost_value, (int, float)) else None,
                accounting_status=(
                    "complete" if isinstance(cost_value, (int, float)) else "unknown"
                ),
                source=(
                    "cli_authoritative_total_cost_usd"
                    if isinstance(cost_value, (int, float))
                    else None
                ),
            ),
            artifact_links=[_relative(root, item) for item in artifacts],
            restricted_process_artifact=_relative(root, process_path),
        )
        invocations.append(record)
        roles[role].append(invocation_id)
    return RoleInvocationIndex(invocations=invocations, roles=roles)


__all__ = [
    "InvocationAccounting",
    "InvocationCost",
    "RoleInvocationEvidence",
    "RoleInvocationIndex",
    "collect_role_invocations",
]
