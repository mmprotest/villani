"""Repository, lineage, and complete execution identity helpers for PT7."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from villani_ops.execution_environment import provider_from_configuration

from ..agent_systems.models import AgentSystemIdentity
from .models import QualificationSystemIdentity


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )


@dataclass(frozen=True, slots=True)
class RepositoryQualificationContext:
    path: Path
    repository_id: str
    head: str | None
    root_commit: str | None
    is_git_repository: bool


def repository_qualification_context(
    repository: str | Path,
) -> RepositoryQualificationContext:
    """Return the same non-secret repository identity used by founder bundles."""

    path = Path(repository).expanduser().resolve()
    inside = _git(path, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        identity = "repo_" + hashlib.sha256(str(path).encode()).hexdigest()[:20]
        return RepositoryQualificationContext(path, identity, None, None, False)
    remote = _git(path, "remote", "get-url", "origin")
    remote_value = remote.stdout.strip() if remote.returncode == 0 else ""
    if remote_value:
        sanitized = re.sub(r"(?i)://[^/@]+@", "://", remote_value)
        identity = "repo_" + hashlib.sha256(sanitized.encode()).hexdigest()[:20]
    else:
        identity = "repo_" + hashlib.sha256(str(path).encode()).hexdigest()[:20]
    head_result = _git(path, "rev-parse", "HEAD")
    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    roots = _git(path, "rev-list", "--max-parents=0", "HEAD")
    root_commit = (
        sorted(line for line in roots.stdout.splitlines() if line.strip())[0]
        if roots.returncode == 0 and roots.stdout.strip()
        else None
    )
    return RepositoryQualificationContext(path, identity, head, root_commit, True)


def commit_is_ancestor(
    context: RepositoryQualificationContext, candidate_commit: str
) -> bool:
    if not context.is_git_repository or context.head is None:
        return False
    result = _git(
        context.path,
        "merge-base",
        "--is-ancestor",
        candidate_commit,
        context.head,
    )
    return result.returncode == 0


def execution_environment_fingerprint(
    identity: AgentSystemIdentity,
    repository: RepositoryQualificationContext,
    configuration: Mapping[str, Any],
    *,
    backend_execution_selection: str | None = None,
) -> str:
    """Measure the configured execution environment without executing setup."""

    if identity.execution.environment_fingerprint:
        return identity.execution.environment_fingerprint
    provider = provider_from_configuration(
        configuration, selection=backend_execution_selection
    )
    return str(provider.fingerprint(repository.path))


def qualification_system_identity(
    identity: AgentSystemIdentity,
    *,
    environment_fingerprint: str,
) -> QualificationSystemIdentity:
    versions = {
        "harness": identity.harness.version,
        "adapter": identity.harness.adapter_version,
        "protocol": identity.harness.protocol_version,
        "model": identity.model_provider.model_revision
        or identity.model_provider.model_id,
        "execution_provider": identity.execution.execution_provider,
        "verification_policy": identity.route_profile.verification_policy,
    }
    if identity.model_provider.serving_engine:
        versions["serving_engine"] = (
            identity.model_provider.serving_engine_version
            or identity.model_provider.serving_engine
        )
    payload = {
        "system_id": identity.system_id,
        "route_name": identity.route_name,
        "harness_id": identity.harness.harness_id,
        "harness_version": identity.harness.version,
        "adapter_id": identity.harness.adapter_id,
        "adapter_version": identity.harness.adapter_version,
        "protocol": identity.harness.protocol,
        "protocol_version": identity.harness.protocol_version,
        "provider": identity.model_provider.provider,
        "model_id": identity.model_provider.model_id,
        "model_revision": identity.model_provider.model_revision,
        "serving_engine": identity.model_provider.serving_engine,
        "serving_engine_version": identity.model_provider.serving_engine_version,
        "execution_provider": identity.execution.execution_provider,
        "execution_environment_fingerprint": environment_fingerprint,
        "verification_policy_version": identity.route_profile.verification_policy,
        "software_versions": versions,
    }
    return QualificationSystemIdentity(
        **payload,
        identity_digest=canonical_digest(payload),
    )


def exact_conformance_status(identity: AgentSystemIdentity) -> str:
    readiness = identity.readiness
    if readiness is not None and readiness.conformance_status in {"passed", "failed"}:
        return readiness.conformance_status
    configured = identity.configuration.get("model_conformance")
    if isinstance(configured, Mapping):
        digest = str(configured.get("report_digest") or "")
        exact = bool(
            configured.get("status") == "passed"
            and configured.get("harness_version") == identity.harness.version
            and configured.get("provider") == identity.model_provider.provider
            and configured.get("model") == identity.model_provider.model_id
            and configured.get("protocol")
            == (
                readiness.protocol
                if readiness is not None
                else identity.harness.protocol
            )
            and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        )
        return "passed" if exact else "failed"
    return readiness.conformance_status if readiness is not None else "not_run"


__all__ = [
    "RepositoryQualificationContext",
    "canonical_digest",
    "commit_is_ancestor",
    "exact_conformance_status",
    "execution_environment_fingerprint",
    "qualification_system_identity",
    "repository_qualification_context",
]
