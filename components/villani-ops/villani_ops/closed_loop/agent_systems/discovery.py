"""Bounded, non-secret discovery for supported coding harnesses."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import subprocess
from functools import lru_cache
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from villani_ops.subprocess_utils import resolve_command_prefix

from .models import HarnessDiscovery, HarnessReadiness


CODEX_SUPPORTED_VERSION_RANGE = ">=0.144.0,<0.145.0"
CLAUDE_CODE_SUPPORTED_VERSION_RANGE = ">=2.1.138,<2.2.0"
_PROBE_LIMIT = 1024 * 1024
_VERSION = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION.search(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def codex_version_supported(value: str) -> bool:
    parsed = _version_tuple(value)
    return parsed is not None and (0, 144, 0) <= parsed < (0, 145, 0)


def claude_version_supported(value: str) -> bool:
    parsed = _version_tuple(value)
    return parsed is not None and (2, 1, 138) <= parsed < (2, 2, 0)


def _fallback_candidates(command: str, harness_id: str) -> tuple[str, ...]:
    if Path(command).parent != Path("."):
        return (command,)
    suffix = ".exe" if os.name == "nt" else ""
    home = Path.home()
    candidates = [command]
    if harness_id == "codex":
        candidates.append(str(home / ".codex" / ".sandbox-bin" / f"codex{suffix}"))
    elif harness_id == "claude-code":
        candidates.append(str(home / ".local" / "bin" / f"claude{suffix}"))
    return tuple(candidates)


def resolve_harness_command(
    command: str, harness_id: str
) -> tuple[list[str] | None, str]:
    for candidate in _fallback_candidates(command, harness_id):
        prefix = resolve_command_prefix(candidate)
        if prefix:
            executable = next(
                (Path(item) for item in reversed(prefix) if Path(item).is_file()),
                Path(candidate),
            )
            return list(prefix), executable.name
    return None, Path(command).name


def _probe(
    prefix: Sequence[str], arguments: Sequence[str], *, timeout: float = 5
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            [*prefix, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=timeout,
            env=dict(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 124, "", type(error).__name__
    stdout = result.stdout[:_PROBE_LIMIT].decode("utf-8", errors="replace")
    stderr = result.stderr[:_PROBE_LIMIT].decode("utf-8", errors="replace")
    return result.returncode, stdout, stderr


def _codex(command: str) -> HarnessDiscovery:
    prefix, identity = resolve_harness_command(command, "codex")
    version: str | None = None
    version_supported: bool | None = None
    authentication = "unknown"
    protocol_conformant = False
    details: dict[str, Any] = {
        "transport": "stdio",
        "experimental_websocket_used": False,
        "provider_options": ["openai"],
    }
    if prefix:
        code, stdout, stderr = _probe(prefix, ["--version"])
        observed = (stdout or stderr).strip()
        version_match = _VERSION.search(observed)
        version = version_match.group(0) if version_match else None
        version_supported = bool(
            code == 0 and version and codex_version_supported(version)
        )
        help_code, help_out, help_err = _probe(prefix, ["app-server", "--help"])
        schema_code, schema_out, schema_err = _probe(
            prefix, ["app-server", "generate-json-schema", "--help"]
        )
        help_text = f"{help_out}\n{help_err}".casefold()
        schema_text = f"{schema_out}\n{schema_err}".casefold()
        protocol_conformant = (
            help_code == 0
            and schema_code == 0
            and "stdio" in help_text
            and "generate-json-schema" in schema_text
        )
        auth_code, auth_out, auth_err = _probe(prefix, ["login", "status"])
        auth_text = f"{auth_out}\n{auth_err}".casefold()
        authentication = (
            "ready"
            if auth_code == 0 and "not logged in" not in auth_text
            else "not_ready"
        )
        details.update(
            {
                "protocol_probe": "passed" if protocol_conformant else "failed",
                "version_probe_exit_code": code,
            }
        )
    repair = (
        "Install Codex CLI and ensure `codex` is available."
        if prefix is None
        else "Run `codex login`."
        if authentication != "ready"
        else f"Install a Codex version in {CODEX_SUPPORTED_VERSION_RANGE}."
        if not version_supported
        else "Run the PT6 Codex conformance suite before enabling this route."
        if not protocol_conformant
        else "Enable a Codex route with provider `openai`; keep qualification provisional."
    )
    readiness = HarnessReadiness(
        installed=prefix is not None,
        command_identity=identity,
        exact_version=version,
        supported_version_range=CODEX_SUPPORTED_VERSION_RANGE,
        version_supported=version_supported,
        authentication_status=authentication,  # type: ignore[arg-type]
        protocol="codex-app-server-jsonrpc-stdio",
        conformance_status="not_run" if protocol_conformant else "failed",
        qualification_state="provisional" if protocol_conformant else "experimental",
        custom_model_capability="unknown",
        custom_provider_capability="unsupported",
        local_model_capability="unsupported",
        repair_action=repair,
        details=details,
    )
    return HarnessDiscovery(
        harness_id="codex",
        display_name="Codex",
        readiness=readiness,
        detected_at=datetime.now(timezone.utc),
    )


def _claude(command: str) -> HarnessDiscovery:
    prefix, identity = resolve_harness_command(command, "claude-code")
    version: str | None = None
    version_supported: bool | None = None
    authentication = "unknown"
    protocol_conformant = False
    details: dict[str, Any] = {
        "transport": "stream-json",
        "provider_options": ["anthropic"],
        "native_windows_strict_sandbox": False if os.name == "nt" else None,
    }
    if prefix:
        code, stdout, stderr = _probe(prefix, ["--version"])
        observed = (stdout or stderr).strip()
        version_match = _VERSION.search(observed)
        version = version_match.group(0) if version_match else None
        version_supported = bool(
            code == 0 and version and claude_version_supported(version)
        )
        help_code, help_out, help_err = _probe(prefix, ["--help"])
        help_text = f"{help_out}\n{help_err}".casefold()
        protocol_conformant = help_code == 0 and all(
            marker in help_text
            for marker in (
                "stream-json",
                "--output-format",
                "--permission-mode",
                "--settings",
                "--no-session-persistence",
            )
        )
        auth_code, auth_out, _auth_err = _probe(prefix, ["auth", "status", "--json"])
        try:
            auth_document = json.loads(auth_out)
        except json.JSONDecodeError:
            auth_document = {}
        authentication = (
            "ready"
            if auth_code == 0 and auth_document.get("loggedIn") is True
            else "not_ready"
        )
        details.update(
            {
                "protocol_probe": "passed" if protocol_conformant else "failed",
                "version_probe_exit_code": code,
            }
        )
    strict_sandbox = os.name != "nt"
    repair = (
        "Install Claude Code and ensure `claude` is available."
        if prefix is None
        else "Run `claude auth login`."
        if authentication != "ready"
        else f"Install a Claude Code version in {CLAUDE_CODE_SUPPORTED_VERSION_RANGE}."
        if not version_supported
        else "Use WSL2 or an outer container; strict Claude sandboxing is unavailable on native Windows."
        if not strict_sandbox
        else "Run the PT6 Claude Code conformance suite before enabling this route."
        if not protocol_conformant
        else "Enable a Claude Code route with provider `anthropic`; keep qualification provisional."
    )
    readiness = HarnessReadiness(
        installed=prefix is not None,
        command_identity=identity,
        exact_version=version,
        supported_version_range=CLAUDE_CODE_SUPPORTED_VERSION_RANGE,
        version_supported=version_supported,
        authentication_status=authentication,  # type: ignore[arg-type]
        protocol="claude-code-stream-json",
        conformance_status="not_run" if protocol_conformant else "failed",
        qualification_state="provisional" if protocol_conformant else "experimental",
        custom_model_capability="unknown",
        custom_provider_capability="unsupported",
        local_model_capability="unsupported",
        repair_action=repair,
        details={**details, "strict_sandbox_available": strict_sandbox},
    )
    return HarnessDiscovery(
        harness_id="claude-code",
        display_name="Claude Code",
        readiness=readiness,
        detected_at=datetime.now(timezone.utc),
    )


def _villani_code(command: str) -> HarnessDiscovery:
    prefix, identity = resolve_harness_command(command, "villani-code")
    version: str | None
    try:
        from villani_code import __version__ as code_version

        version = code_version
    except ImportError:
        try:
            version = importlib.metadata.version("villani-code")
        except importlib.metadata.PackageNotFoundError:
            version = "1.0.0" if prefix else None
    readiness = HarnessReadiness(
        installed=prefix is not None,
        command_identity=identity,
        exact_version=version,
        supported_version_range=None,
        version_supported=True if prefix else None,
        authentication_status="not_applicable",
        protocol="villani.harness_adapter.v1",
        conformance_status="passed" if prefix else "not_run",
        qualification_state="bootstrap" if prefix else "disabled",
        custom_model_capability="unknown",
        custom_provider_capability="unknown",
        local_model_capability="unknown",
        repair_action=(
            "Villani Code is ready."
            if prefix
            else "Install Villani Code in the active Villani environment."
        ),
        details={"transport": "structured_headless_cli"},
    )
    return HarnessDiscovery(
        harness_id="villani-code",
        display_name="Villani Code",
        readiness=readiness,
        detected_at=datetime.now(timezone.utc),
    )


@lru_cache(maxsize=16)
def _discover_cached(
    villani_command: str, codex_command: str, claude_command: str
) -> tuple[HarnessDiscovery, ...]:
    return (
        _villani_code(villani_command),
        _codex(codex_command),
        _claude(claude_command),
    )


def discover_agent_harnesses(
    commands: Mapping[str, str] | None = None,
) -> tuple[HarnessDiscovery, ...]:
    configured = dict(commands or {})
    return _discover_cached(
        configured.get("villani-code", "villani-code"),
        configured.get("codex", "codex"),
        configured.get("claude-code", "claude"),
    )


def discover_harness(harness_id: str, command: str) -> HarnessDiscovery:
    if harness_id == "villani-code":
        return _villani_code(command)
    if harness_id == "codex":
        return _codex(command)
    if harness_id == "claude-code":
        return _claude(command)
    raise ValueError(f"unsupported harness {harness_id!r}")


__all__ = [
    "CLAUDE_CODE_SUPPORTED_VERSION_RANGE",
    "CODEX_SUPPORTED_VERSION_RANGE",
    "claude_version_supported",
    "codex_version_supported",
    "discover_agent_harnesses",
    "discover_harness",
    "resolve_harness_command",
]
