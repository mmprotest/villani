"""Fail-closed execution policy checks and hostile-workspace inspection."""

from __future__ import annotations

import fnmatch
import os
import stat
import tarfile
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Sequence

from .models import ActionPolicy, NetworkPolicy


class ExecutionPolicyDenied(RuntimeError):
    """A structured policy denial safe to persist without secret content."""

    def __init__(self, *, policy: str, action: str, reason: str) -> None:
        self.event = {
            "schema_version": "villani.execution_policy_event.v1",
            "decision": "deny",
            "policy": policy,
            "action": action,
            "reason": reason,
        }
        super().__init__(f"execution policy denied {action}: {reason}")


def _matches(value: str, patterns: Sequence[str]) -> bool:
    folded = value.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in patterns)


def check_command(command: Sequence[str], policy: ActionPolicy) -> dict[str, Any]:
    if not command:
        raise ExecutionPolicyDenied(
            policy="command", action="execute", reason="empty command"
        )
    executable = str(command[0])
    candidates = (executable, Path(executable).name)
    if any(_matches(value, policy.command_deny) for value in candidates):
        raise ExecutionPolicyDenied(
            policy="command",
            action=Path(executable).name or executable,
            reason="matched command deny policy",
        )
    if policy.command_allow and not any(
        _matches(value, policy.command_allow) for value in candidates
    ):
        raise ExecutionPolicyDenied(
            policy="command",
            action=Path(executable).name or executable,
            reason="not present in command allow policy",
        )
    return {
        "policy": "command",
        "decision": "allow",
        "action": Path(executable).name or executable,
    }


def check_path(path: Path, root: Path, policy: ActionPolicy) -> dict[str, Any]:
    root = root.resolve()
    try:
        resolved = path.resolve(strict=False)
        relative = resolved.relative_to(root).as_posix()
    except (OSError, ValueError) as error:
        raise ExecutionPolicyDenied(
            policy="path", action=str(path), reason="path traversal outside workspace"
        ) from error
    candidates = (relative, str(resolved))
    if any(_matches(value, policy.path_deny) for value in candidates):
        raise ExecutionPolicyDenied(
            policy="path", action=relative, reason="matched path deny policy"
        )
    if policy.path_allow and not any(
        _matches(value, policy.path_allow) for value in candidates
    ):
        raise ExecutionPolicyDenied(
            policy="path", action=relative, reason="not present in path allow policy"
        )
    return {"policy": "path", "decision": "allow", "action": relative}


def _archive_totals(path: Path, policy: ActionPolicy) -> tuple[int, int, int]:
    entries = compressed = expanded = 0
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for zip_item in archive.infolist():
                    entries += 1
                    compressed += max(zip_item.compress_size, 1)
                    expanded += zip_item.file_size
        elif tarfile.is_tarfile(path):
            compressed = max(path.stat().st_size, 1)
            with tarfile.open(path, mode="r:*") as archive:
                for tar_item in archive:
                    entries += 1
                    expanded += max(tar_item.size, 0)
        else:
            return 0, 0, 0
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise ExecutionPolicyDenied(
            policy="archive", action=path.name, reason="malformed archive"
        ) from error
    if entries > policy.max_archive_entries:
        raise ExecutionPolicyDenied(
            policy="archive", action=path.name, reason="archive entry limit exceeded"
        )
    if expanded > policy.max_archive_uncompressed_bytes:
        raise ExecutionPolicyDenied(
            policy="archive", action=path.name, reason="archive expanded-size limit exceeded"
        )
    if compressed and expanded / compressed > policy.max_archive_ratio:
        raise ExecutionPolicyDenied(
            policy="archive", action=path.name, reason="archive compression-ratio limit exceeded"
        )
    return entries, compressed, expanded


def check_file_mode(mode: int, name: str) -> None:
    """Reject filesystem entry types that must never enter an execution sandbox."""
    if stat.S_ISSOCK(mode):
        raise ExecutionPolicyDenied(
            policy="path", action=name, reason="socket file is not allowed"
        )
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
        raise ExecutionPolicyDenied(
            policy="path", action=name, reason="device or fifo is not allowed"
        )


def inspect_workspace(root: Path, policy: ActionPolicy) -> dict[str, Any]:
    root = root.resolve()
    files = archives = total = 0
    for base, directories, names in os.walk(root, followlinks=False):
        base_path = Path(base)
        for name in [*directories, *names]:
            path = base_path / name
            try:
                metadata = path.lstat()
            except OSError as error:
                raise ExecutionPolicyDenied(
                    policy="path", action=path.name, reason="path cannot be inspected"
                ) from error
            mode = metadata.st_mode
            if stat.S_ISLNK(mode) and not policy.allow_symlinks:
                raise ExecutionPolicyDenied(
                    policy="path", action=path.name, reason="symlink is not allowed"
                )
            check_file_mode(mode, path.name)
            check_path(path, root, policy)
            if not stat.S_ISREG(mode):
                continue
            files += 1
            total += metadata.st_size
            if metadata.st_size > policy.max_file_bytes:
                raise ExecutionPolicyDenied(
                    policy="path", action=path.name, reason="oversized file"
                )
            archive_entries, _compressed, _expanded = _archive_totals(path, policy)
            if archive_entries:
                archives += 1
    return {
        "schema_version": "villani.workspace_inspection.v1",
        "files": files,
        "archives": archives,
        "total_bytes": total,
        "decision": "allow",
    }


def _host_matches(host: str, patterns: Sequence[str]) -> bool:
    normalized = host.rstrip(".").casefold()
    return any(
        normalized == pattern.rstrip(".").casefold()
        or normalized.endswith("." + pattern.lstrip("*.").rstrip(".").casefold())
        for pattern in patterns
    )


def check_domain(host: str, network: NetworkPolicy, policy: ActionPolicy) -> None:
    denied = [*network.denied_domains, *network.denied_hosts, *policy.domain_deny]
    allowed = [*network.allowed_domains, *network.allowed_hosts, *policy.domain_allow]
    if _host_matches(host, denied):
        raise ExecutionPolicyDenied(
            policy="domain", action=host, reason="matched domain deny policy"
        )
    if allowed and not _host_matches(host, allowed):
        raise ExecutionPolicyDenied(
            policy="domain", action=host, reason="not present in domain allow policy"
        )


def inspect_command_domains(
    command: Sequence[str], network: NetworkPolicy, policy: ActionPolicy
) -> list[str]:
    hosts: list[str] = []
    for value in command:
        parsed = urllib.parse.urlparse(str(value))
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            check_domain(parsed.hostname, network, policy)
            hosts.append(parsed.hostname)
    return hosts
