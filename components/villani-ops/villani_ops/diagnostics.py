"""Shared, zero-inference diagnostics for the public Villani doctor command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from villani_ops.core.backend import Backend
from villani_ops.execution_environment import (
    ExecutionEnvironmentConfig,
    inspect_repository,
    preflight_report,
    provider_from_configuration,
)
from villani_ops.llm.transport import trust_environment_for_backend
from villani_ops.subprocess_utils import resolve_command_prefix


class RepositoryDiagnosticError(ValueError):
    """An explicitly requested repository cannot be inspected safely."""


def _git_repository(path: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            shell=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0 or not completed.stdout.strip():
        return False
    try:
        top_level = Path(completed.stdout.strip()).resolve()
    except OSError:
        return False
    return os.path.normcase(str(top_level)) == os.path.normcase(str(path.resolve()))


def resolve_doctor_repository(
    *,
    explicit: Path | None,
    saved: str | Path | None,
    cwd: Path | None = None,
) -> tuple[Path | None, str]:
    """Resolve explicit, saved, or current repositories without mutation."""

    if explicit is not None:
        candidate = explicit.expanduser().resolve()
        if not candidate.exists():
            raise RepositoryDiagnosticError(f"repository does not exist: {candidate}")
        if not candidate.is_dir():
            raise RepositoryDiagnosticError(
                f"repository is not a directory: {candidate}"
            )
        if not _git_repository(candidate):
            raise RepositoryDiagnosticError(
                f"repository is not an accessible Git repository: {candidate}"
            )
        return candidate, "explicit"

    if saved:
        candidate = Path(saved).expanduser().resolve()
        if candidate.is_dir() and _git_repository(candidate):
            return candidate, "saved_setup"
    current = (cwd or Path.cwd()).expanduser().resolve()
    if current.is_dir() and _git_repository(current):
        return current, "current_directory"
    return None, "unavailable"


def parse_backends(configuration: Mapping[str, Any]) -> dict[str, Backend]:
    raw = configuration.get("backends")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("config backends must be a mapping keyed by backend name")
    parsed: dict[str, Backend] = {}
    for name, value in raw.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"backend {name!r} must be a YAML object")
        try:
            parsed[str(name)] = Backend.model_validate(
                {"name": str(name), **dict(value)}
            )
        except ValidationError as error:
            raise ValueError(f"backend {name!r} is invalid: {error}") from error
    return parsed


def _credential_status(backend: Backend, environ: Mapping[str, str]) -> str:
    if backend._usable_direct_credential(backend.api_key):
        return "direct_key_configured"
    if backend.api_key_env and backend.api_key_env.strip():
        return (
            "env_var_present"
            if backend.runtime_credential_available(environ)
            else "env_var_missing"
        )
    return "missing"


def probe_backend(
    backend: Backend,
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 3,
) -> dict[str, Any]:
    """Probe documented non-generation endpoints and spend zero model tokens."""

    env = os.environ if environ is None else environ
    provider = str(backend.provider)
    supported = provider in {"openai", "openai-compatible", "local"} and bool(
        backend.base_url
    )
    credential = _credential_status(backend, env)
    if (
        provider in {"local", "openai-compatible"}
        and not backend.credential_reference_configured()
    ) or bool(backend.metadata.get("allow_dummy_api_key")):
        credential = "not_required"
    credential_ok = credential in {
        "direct_key_configured",
        "env_var_present",
        "not_required",
    }
    result: dict[str, Any] = {
        "name": backend.name,
        "provider": provider,
        "model": backend.model,
        "endpoint": backend.base_url,
        "setup_provider_identifier": backend.metadata.get("setup_provider_identifier"),
        "enabled": backend.enabled,
        "credential_reference": backend.api_key_env,
        "credential_status": credential,
        "probe": "models" if supported else "unsupported",
        "probe_status": "unsupported" if not supported else "unreachable",
        "endpoint_reachable": False,
        "model_available": None,
        "usable": False,
        "model_tokens_spent": 0,
    }
    if not credential_ok:
        result["probe_status"] = "credential_missing"
        result["reason"] = (
            f"credential environment variable {backend.api_key_env} is missing or empty"
            if backend.api_key_env
            else "no usable credential reference is configured"
        )
        return result
    if not supported:
        result["usable"] = True
        result["reason"] = "provider exposes no configured health/models probe"
        return result

    base = str(backend.base_url).rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    origin_health = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/health", "", "")
    )
    endpoints = [("models", base + "/models"), ("health", base + "/health")]
    if origin_health != base + "/health":
        endpoints.append(("health", origin_health))
    headers = {"Accept": "application/json"}
    key = backend.resolved_api_key(env)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    opener = (
        urllib.request.build_opener()
        if trust_environment_for_backend(base)
        else urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    unsupported_status: int | None = None
    for probe_name, endpoint in endpoints:
        request = urllib.request.Request(endpoint, headers=headers, method="GET")
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(65_536)
                result["probe"] = probe_name
                result["endpoint_reachable"] = True
                result["probe_status"] = (
                    "ok" if 200 <= response.status < 300 else "error"
                )
                if probe_name == "models" and result["probe_status"] == "ok":
                    try:
                        document = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        document = None
                    values = (
                        document.get("data") if isinstance(document, Mapping) else None
                    )
                    model_ids = (
                        {
                            str(item.get("id"))
                            for item in values
                            if isinstance(item, Mapping) and item.get("id")
                        }
                        if isinstance(values, list)
                        else set()
                    )
                    result["model_available"] = backend.model in model_ids
                    if not result["model_available"]:
                        result["probe_status"] = "model_missing"
                break
        except urllib.error.HTTPError as error:
            if error.code in {404, 405, 501}:
                unsupported_status = error.code
                continue
            result["endpoint_reachable"] = True
            result["probe_status"] = (
                "authentication_failed" if error.code in {401, 403} else "error"
            )
            result["http_status"] = error.code
            break
        except (OSError, urllib.error.URLError) as error:
            result["reason"] = error.__class__.__name__
            break
    else:
        result["probe"] = "unsupported"
        result["probe_status"] = "unsupported"
        result["reason"] = (
            "configured endpoint exposes no model-free health/models probe"
        )
        result["http_status"] = unsupported_status
    result["usable"] = credential_ok and result["probe_status"] in {
        "ok",
        "unsupported",
    }
    return result


def daemon_diagnostic() -> dict[str, Any]:
    try:
        from villani_agentd.client import LocalClient

        health = LocalClient.from_files().health()
        return {
            "installed": True,
            "running": health.get("status") == "ok",
            "status": health.get("status"),
        }
    except ImportError:
        return {"installed": False, "running": False, "status": "unavailable"}
    except Exception as error:
        return {
            "installed": True,
            "running": False,
            "status": "not_running",
            "reason": error.__class__.__name__,
        }


def _recent_villani_code_success(environ: Mapping[str, str]) -> dict[str, Any] | None:
    home = Path(environ.get("VILLANI_HOME") or Path.home() / ".villani")
    runs = home / "runs"
    try:
        directories = sorted(
            (item for item in runs.iterdir() if item.is_dir()),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )[:100]
    except OSError:
        return None
    for directory in directories:
        try:
            manifest = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        attempt_ids = (
            manifest.get("attempt_ids") if isinstance(manifest, Mapping) else None
        )
        if not isinstance(attempt_ids, list):
            continue
        for attempt_id in reversed(attempt_ids):
            if not isinstance(attempt_id, str):
                continue
            try:
                attempt = json.loads(
                    (directory / "attempts" / attempt_id / "attempt.json").read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(attempt, Mapping):
                continue
            if (
                attempt.get("runner_name") in {"villani_code", "villani-code"}
                and attempt.get("status") == "completed"
                and attempt.get("exit_code") == 0
            ):
                return {
                    "run_id": manifest.get("run_id"),
                    "attempt_id": attempt_id,
                    "completed_at": attempt.get("completed_at"),
                }
    return None


def adapter_diagnostics(
    *, environ: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    env = os.environ if environ is None else environ
    try:
        from villani_agentd.adapters import ADAPTERS

        recent_villani_code = _recent_villani_code_success(env)
        reports: list[dict[str, Any]] = []
        for name, adapter in sorted(ADAPTERS.items()):
            if name == "generic":
                continue
            try:
                report = adapter.detect().as_dict()
                if name == "villani-code" and recent_villani_code is not None:
                    report["runtime_status"] = "successful_recent_run"
                    report["last_successful_use"] = recent_villani_code
                    if report.get("executable_status") == "present" and str(
                        report.get("probe_status", "")
                    ).endswith("timed_out"):
                        report["available"] = True
                reports.append(report)
            except Exception as error:
                reports.append(
                    {
                        "adapter": name,
                        "available": False,
                        "executable_status": "unknown",
                        "probe_status": "error",
                        "runtime_status": "not_observed",
                        "detected_version": None,
                        "capabilities": [],
                        "missing_capabilities": [
                            f"probe_error:{error.__class__.__name__}"
                        ],
                    }
                )
        return reports
    except ImportError:
        return [
            {
                "name": "villani-agentd",
                "available": False,
                "missing_capabilities": ["package_not_installed"],
            }
        ]


def build_repository_diagnostics(
    repository: Path | None,
    configuration: Mapping[str, Any],
    *,
    repository_required: bool = True,
    environ: Mapping[str, str] | None = None,
    service: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Build the stable repository/backend doctor contract."""

    env = os.environ if environ is None else environ
    diagnostic_root = repository or Path.cwd().resolve()
    if repository is not None:
        git = subprocess.run(
            ["git", "status", "--porcelain=v1", "--branch"],
            cwd=repository,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
        )
        git_usable = git.returncode == 0
        git_stdout = git.stdout
        git_stderr = git.stderr
        inspection = inspect_repository(repository)
    else:
        git_usable = False
        git_stdout = ""
        git_stderr = "No repository was selected."
        inspection = {
            "schema_version": "villani.repository_inspection.v1",
            "repository": None,
            "detected_test_tools": [],
            "likely_test_commands": [],
        }
    usage = shutil.disk_usage(diagnostic_root)
    provider = provider_from_configuration(configuration)
    provider_entries: list[tuple[str | None, Any]] = [(None, provider)]
    named = configuration.get("execution_environments")
    if isinstance(named, Mapping):
        for name in sorted(named):
            provider_entries.append(
                (
                    str(name),
                    provider_from_configuration(configuration, selection=str(name)),
                )
            )
    provider_reports = []
    provider_available: dict[str | None, bool] = {}
    for name, configured_provider in provider_entries:
        item = configured_provider.capability_report()
        if repository is None:
            item["fingerprint"] = None
            item["fingerprint_error"] = "repository_unavailable"
        else:
            try:
                item["fingerprint"] = configured_provider.fingerprint(repository)
                item["fingerprint_error"] = None
            except (
                OSError,
                RuntimeError,
                ValueError,
                subprocess.SubprocessError,
            ) as error:
                item["fingerprint"] = None
                item["fingerprint_error"] = error.__class__.__name__
                item["available"] = False
        item["selection"] = name or "default"
        provider_reports.append(item)
        provider_available[name] = bool(item.get("available"))

    backends = parse_backends(configuration)
    backend_reports = [
        probe_backend(item, environ=env)
        for item in sorted(backends.values(), key=lambda item: item.name)
    ]
    coding_commands = []
    for backend in sorted(backends.values(), key=lambda item: item.name):
        if backend.enabled and "coding" in backend.roles:
            command = backend.command_name or "villani-code"
            execution = ExecutionEnvironmentConfig.from_configuration(
                configuration, backend.execution_environment
            )
            coding_commands.append(
                {
                    "backend": backend.name,
                    "command": command,
                    "execution_environment": backend.execution_environment or "default",
                    "available": (
                        provider_available.get(backend.execution_environment, False)
                        if execution.provider in {"container", "devcontainer"}
                        else resolve_command_prefix(command) is not None
                    ),
                }
            )
    preflight = (
        preflight_report(repository, configuration)
        if repository is not None
        else {"execution_environment_fingerprint": None}
    )
    required_checks = {
        "git": git_usable if repository_required else True,
        "repository": repository is not None if repository_required else True,
        "disk": usage.free >= 100 * 1024 * 1024,
        "execution_provider": bool(provider.capability_report().get("available"))
        and (
            bool(preflight.get("execution_environment_fingerprint"))
            if repository is not None
            else True
        ),
        "coding_adapter": bool(coding_commands)
        and all(item["available"] for item in coding_commands),
        "backends": bool(backend_reports)
        and all((not item["enabled"]) or item["usable"] for item in backend_reports),
    }
    if not bool(provider.config.required):
        required_checks["execution_provider"] = True
    healthy = all(required_checks.values())
    return healthy, {
        "schema_version": "villani.doctor.v1",
        "repository": str(repository) if repository else None,
        "healthy": healthy,
        "ok": healthy,
        "required_capabilities": required_checks,
        "git": {
            "usable": git_usable,
            "status_porcelain_branch": git_stdout,
            "error": git_stderr,
        },
        "disk": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "usable": required_checks["disk"],
        },
        "service": dict(service or {}),
        "daemon": daemon_diagnostic(),
        "adapters": adapter_diagnostics(environ=env),
        "coding_commands": coding_commands,
        "backend_connectivity": backend_reports,
        "credentials": [
            {
                "backend": item["name"],
                "reference": item.get("credential_reference"),
                "status": item["credential_status"],
            }
            for item in backend_reports
        ],
        "execution_providers": provider_reports,
        "execution_environment_fingerprint": preflight[
            "execution_environment_fingerprint"
        ],
        "repository_inspection": inspection,
        "detected_test_tools": inspection["detected_test_tools"],
        "likely_test_commands": inspection["likely_test_commands"],
        "inferred_commands_executed": False,
    }
