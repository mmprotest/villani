"""Human and machine-readable public Villani diagnostics."""

from __future__ import annotations

import importlib
import importlib.metadata
import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from villani_agentd.config import AgentdPaths, Limits
from villani_agentd.spool import SQLiteSpool
from villani_ops.diagnostics import (
    build_repository_diagnostics,
    resolve_doctor_repository,
)
from villani_ops.closed_loop.agent_systems.discovery import discover_agent_harnesses
from villani_ops.closed_loop.agent_systems.management import (
    DoctorStatus,
    diagnose_registry,
    write_management_evidence,
)
from villani_ops.closed_loop.agent_systems.registry import build_agent_system_registry
from villani_ops.closed_loop.agent_systems.role_models import (
    AgentRole,
    CliAgentSystemConfig,
)
from villani_ops.closed_loop.durable_io import write_json_atomic
from villani_ops.self_service import PackageManifest, load_entitlement
from pydantic import ValidationError

from . import __version__
from .migrations import MigrationError, check_upgrade
from .onboarding import (
    SetupError,
    load_configuration,
    utc_now,
    validate_configuration,
)
from .services import ServiceError, service_status, villani_home
from .update_system import UpdateError, UpdateManager


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    identifier: str
    status: str
    message: str
    recovery_action: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    repositories_modified: bool = False
    evidence_path: str = "diagnostics/doctor-latest.json"

    def as_dict(self, *, evidence_path: str | None = None) -> dict[str, Any]:
        value = asdict(self)
        value["repositories_modified"] = False
        value["evidence_path"] = evidence_path or self.evidence_path
        if self.status == "fail" and not self.recovery_action:
            value["recovery_action"] = "Run: villani doctor --json"
        return value


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    generated_at: str
    healthy: bool
    checks: tuple[DiagnosticCheck, ...]
    details: dict[str, Any] = field(default_factory=dict)
    evidence_path: str = "diagnostics/doctor-latest.json"

    def as_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": "villani.doctor.v1",
            "generated_at": self.generated_at,
            "healthy": self.healthy,
            "ok": self.healthy,
            "summary": {
                "passed": sum(item.status == "pass" for item in self.checks),
                "warnings": sum(item.status == "warn" for item in self.checks),
                "failed": sum(item.status == "fail" for item in self.checks),
            },
            "checks": [item.as_dict(evidence_path=self.evidence_path) for item in self.checks],
            "repositories_modified": False,
            "evidence_path": self.evidence_path,
        }
        value.update(self.details)
        value["schema_version"] = "villani.doctor.v1"
        value["healthy"] = self.healthy
        value["ok"] = self.healthy
        value["summary"] = {
            "passed": sum(item.status == "pass" for item in self.checks),
            "warnings": sum(item.status == "warn" for item in self.checks),
            "failed": sum(item.status == "fail" for item in self.checks),
        }
        value["checks"] = [item.as_dict(evidence_path=self.evidence_path) for item in self.checks]
        value["repositories_modified"] = False
        value["evidence_path"] = self.evidence_path
        return value


_COMPONENTS = {
    "villani": ("villani_distribution", __version__),
    "villani-ops": ("villani_ops", __version__),
    "villani-code": ("villani_code", __version__),
    "villani-agentd": ("villani_agentd", __version__),
}


def _version(distribution: str, fallback: str = "unknown") -> str:
    component = _COMPONENTS.get(distribution)
    if component is not None:
        try:
            value = getattr(importlib.import_module(component[0]), "__version__", None)
        except ImportError:
            value = None
        if isinstance(value, str) and value:
            return value
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return fallback


def _component_check() -> DiagnosticCheck:
    versions: dict[str, str] = {}
    failures: list[str] = []
    for distribution, (module, expected) in _COMPONENTS.items():
        try:
            importlib.import_module(module)
        except ImportError:
            failures.append(f"{distribution} is not importable")
            continue
        installed = _version(distribution, expected)
        versions[distribution] = installed
        if installed != expected:
            failures.append(f"{distribution} {installed} (expected {expected})")
    if failures:
        return DiagnosticCheck(
            "component_compatibility",
            "fail",
            "Installed Villani components are incompatible: " + "; ".join(failures),
            "Reinstall Villani from one release package, then run: villani doctor",
            {"versions": versions},
        )
    return DiagnosticCheck(
        "component_compatibility",
        "pass",
        "Installed Villani components are compatible.",
        details={"versions": versions},
    )


def _package_health_check() -> DiagnosticCheck:
    modules: dict[str, str] = {}
    failures: list[str] = []
    for public_name, (module, _expected) in _COMPONENTS.items():
        try:
            imported = importlib.import_module(module)
            location = getattr(imported, "__file__", None)
            modules[public_name] = str(location) if location else "built-in"
        except ImportError:
            failures.append(public_name)
    if failures:
        return DiagnosticCheck(
            "package_health",
            "fail",
            "Villani package imports are incomplete: " + ", ".join(failures),
            "Reinstall Villani, then run: villani doctor",
            {"modules": modules},
        )
    return DiagnosticCheck(
        "package_health",
        "pass",
        "Villani package imports are healthy.",
        details={"modules": modules},
    )


def _repository_check(repository: Path | None, source: str, *, explicit: bool) -> DiagnosticCheck:
    if repository is None:
        return DiagnosticCheck(
            "repository_access",
            "fail" if explicit else "warn",
            "No accessible Git repository is available for inspection.",
            "Supply one with: villani doctor --repo PATH",
            {"path": None, "source": source, "requested": explicit},
        )
    return DiagnosticCheck(
        "repository_access",
        "pass",
        f"Repository is accessible at {repository}.",
        details={"path": str(repository), "source": source, "requested": explicit},
    )


def _git_check() -> DiagnosticCheck:
    executable = shutil.which("git")
    if not executable:
        return DiagnosticCheck(
            "git",
            "fail",
            "Git is not installed or is not on PATH.",
            "Install Git, open a new terminal, then run: villani doctor",
        )
    try:
        completed = subprocess.run(
            [executable, "--version"],
            text=True,
            capture_output=True,
            check=False,
            shell=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is None or completed.returncode != 0:
        return DiagnosticCheck(
            "git",
            "fail",
            "Git could not be executed.",
            "Repair the Git installation, then run: villani doctor",
        )
    return DiagnosticCheck(
        "git", "pass", completed.stdout.strip(), details={"executable": executable}
    )


def _configuration_checks(
    home: Path,
) -> tuple[list[DiagnosticCheck], dict[str, Any] | None, dict[str, Any]]:
    path = home / "config.yaml"
    if not path.is_file():
        return (
            [
                DiagnosticCheck(
                    "configuration",
                    "fail",
                    "No coding backend is configured.",
                    "Run: villani setup",
                    {"path": str(path)},
                )
            ],
            None,
            {},
        )
    try:
        configuration = load_configuration(path)
        backends = validate_configuration(configuration)
    except SetupError as error:
        return (
            [
                DiagnosticCheck(
                    "configuration",
                    "fail",
                    f"Villani configuration is invalid: {error}",
                    "Run: villani setup",
                    {"path": str(path)},
                )
            ],
            None,
            {},
        )
    checks = [
        DiagnosticCheck(
            "configuration",
            "pass",
            f"Configuration schema v{configuration.get('config_version', 1)} is valid.",
            details={"path": str(path), "backend_count": len(backends)},
        )
    ]
    return checks, configuration, backends


def _backend_checks(
    backends: Mapping[str, Any],
    reports: list[dict[str, Any]],
    *,
    role_systems_configured: bool = False,
) -> list[DiagnosticCheck]:
    if not backends:
        if role_systems_configured:
            return [
                DiagnosticCheck(
                    "configured_backends",
                    "pass",
                    "The active execution profile uses configured CLI agent systems.",
                    details={"backend_count": 0, "profile_driven": True},
                )
            ]
        return [
            DiagnosticCheck(
                "configured_backends",
                "fail",
                "No coding backend is configured.",
                "Run: villani setup",
            )
        ]
    checks: list[DiagnosticCheck] = [
        DiagnosticCheck(
            "configured_backends",
            "pass",
            f"{len(backends)} coding backend(s) are configured.",
            details={
                "backends": [
                    {"name": name, "provider": str(backend.provider), "model": backend.model}
                    for name, backend in sorted(backends.items())
                ]
            },
        )
    ]
    by_name = {str(item.get("name")): item for item in reports}
    for name, backend in sorted(backends.items()):
        probe = by_name.get(name, {})
        credential_missing = probe.get("credential_status") in {
            "missing",
            "env_var_missing",
        }
        recovery = "Check the endpoint, load the selected model, then run: villani doctor"
        if credential_missing:
            variable = backend.api_key_env or "the configured credential variable"
            checks.append(
                DiagnosticCheck(
                    f"model_server_reachability:{name}",
                    "fail",
                    f"Backend {name} cannot resolve its configured credential.",
                    f"Set {variable}, then run: villani doctor",
                    {**probe, "model_tokens_spent": 0},
                )
            )
            checks.append(
                DiagnosticCheck(
                    f"model_availability:{name}",
                    "fail",
                    f"Model {backend.model} cannot be checked until the credential is configured.",
                    f"Set {variable}, then run: villani doctor",
                    {**probe, "model_tokens_spent": 0},
                )
            )
            continue
        display = {
            "lm-studio": "LM Studio",
            "ollama": "Ollama",
            "llama-cpp": "llama.cpp",
            "vllm": "vLLM",
            "openai": "OpenAI",
        }.get(str(probe.get("setup_provider_identifier") or backend.provider), name)
        if probe.get("usable") is True:
            checks.append(
                DiagnosticCheck(
                    f"model_server_reachability:{name}",
                    "pass",
                    f"{display} is reachable.",
                    details={
                        **probe,
                        "provider": str(backend.provider),
                        "endpoint": backend.base_url,
                        "model": backend.model,
                        "capability": backend.metadata.get("capability_status", "unrated"),
                        "pricing": backend.billing_mode,
                        "model_tokens_spent": 0,
                    },
                )
            )
            checks.append(
                DiagnosticCheck(
                    f"model_availability:{name}",
                    "pass",
                    f"Model {backend.model} is available.",
                    details={**probe, "model": backend.model, "model_tokens_spent": 0},
                )
            )
        else:
            reachable = bool(probe.get("endpoint_reachable"))
            checks.append(
                DiagnosticCheck(
                    f"model_server_reachability:{name}",
                    "pass" if reachable else "fail",
                    (f"{display} is reachable." if reachable else f"{display} is not reachable."),
                    None if reachable else recovery,
                    {**probe, "model_tokens_spent": 0},
                )
            )
            message = f"Model {backend.model} is unavailable."
            if probe.get("setup_provider_identifier") == "lm-studio":
                message = "LM Studio is reachable but no model is loaded."
                recovery = "Load a model, then run: villani doctor"
            checks.append(
                DiagnosticCheck(
                    f"model_availability:{name}",
                    "fail",
                    message,
                    recovery,
                    {
                        "provider": str(backend.provider),
                        "endpoint": backend.base_url,
                        "model": backend.model,
                        "probe_stage": probe.get("probe_status"),
                        "model_tokens_spent": 0,
                    },
                )
            )
    return checks


def _adapter_checks(reports: list[dict[str, Any]]) -> list[DiagnosticCheck]:
    """Project adapter presence, bounded probe health, and runtime health separately."""

    report = next(
        (item for item in reports if item.get("adapter") == "villani-code"),
        None,
    )
    if report is None:
        return []
    details = {**report, "model_tokens_spent": 0}
    executable_status = report.get("executable_status")
    probe_status = str(report.get("probe_status") or "unknown")
    runtime_status = report.get("runtime_status")
    if executable_status == "missing":
        return [
            DiagnosticCheck(
                "coding_adapter_probe:villani-code",
                "fail",
                "The villani-code executable is missing.",
                "Reinstall Villani Code, then run: villani doctor",
                details,
            )
        ]
    if probe_status.endswith("timed_out"):
        timeout = report.get("probe_timeout_seconds")
        timeout_text = f" after {timeout:g} seconds" if isinstance(timeout, (int, float)) else ""
        runtime_text = (
            " A successful recent coding run confirms runtime availability."
            if runtime_status == "successful_recent_run"
            else ""
        )
        return [
            DiagnosticCheck(
                "coding_adapter_probe:villani-code",
                "warn",
                (
                    "The villani-code executable is present, but its bounded diagnostic probe "
                    f"timed out{timeout_text}.{runtime_text}"
                ),
                "Retry diagnostics with: villani doctor",
                details,
            )
        ]
    if probe_status.endswith("failed") or probe_status == "error":
        return [
            DiagnosticCheck(
                "coding_adapter_probe:villani-code",
                "warn",
                "The villani-code executable is present, but its diagnostic probe failed.",
                "Retry diagnostics with: villani doctor",
                details,
            )
        ]
    message = "The villani-code executable is present and its diagnostic probe is healthy."
    if runtime_status == "successful_recent_run":
        message += " A successful recent coding run also confirms runtime availability."
    return [
        DiagnosticCheck(
            "coding_adapter_probe:villani-code",
            "pass",
            message,
            details=details,
        )
    ]


def _storage_check(home: Path) -> DiagnosticCheck:
    probe = home / ".doctor-write-probe"
    try:
        home.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("ok")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
    except OSError:
        probe.unlink(missing_ok=True)
        return DiagnosticCheck(
            "storage_permissions",
            "fail",
            f"Villani cannot safely write to {home}.",
            "Fix ownership and write permissions for the Villani home, then run: villani doctor",
            {"path": str(home)},
        )
    return DiagnosticCheck(
        "storage_permissions",
        "pass",
        f"Villani storage is writable at {home}.",
        details={"path": str(home)},
    )


def _console_check(url: str | None, *, service_running: bool = False) -> DiagnosticCheck:
    if not url:
        return DiagnosticCheck(
            "browser_ui",
            "fail" if service_running else "warn",
            "Villani Console is unavailable because Villani Service is stopped.",
            "Run: villani service start",
        )
    request = urllib.request.Request(url, headers={"Accept": "text/html"}, method="GET")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=3
        ) as response:
            body = response.read(65_536).decode("utf-8", errors="replace")
            healthy = response.status == 200 and "Villani Console" in body
    except (OSError, urllib.error.URLError):
        healthy = False
    if not healthy:
        return DiagnosticCheck(
            "browser_ui",
            "fail",
            "Villani Console did not answer its local health check.",
            "Run: villani service restart",
            {"url": url},
        )
    return DiagnosticCheck(
        "browser_ui", "pass", "Villani Console is available.", details={"url": url}
    )


def _migration_check(home: Path) -> DiagnosticCheck:
    try:
        report = check_upgrade(home, apply=False)
    except MigrationError as error:
        return DiagnosticCheck(
            "migrations",
            "fail",
            f"Migration compatibility failed: {error}",
            "Run: villani update rollback",
        )
    return DiagnosticCheck(
        "migrations",
        "pass",
        "Configuration, spool, and recorded run protocols are readable.",
        details={
            **asdict(report),
            "preview_only": True,
            "destructive": False,
        },
    )


def _update_check(home: Path) -> DiagnosticCheck:
    try:
        state = UpdateManager(home).status()
    except UpdateError as error:
        return DiagnosticCheck(
            "update_state",
            "fail",
            f"Update state is invalid: {error}",
            "Run: villani update channel stable",
        )
    if state.status == "failed":
        return DiagnosticCheck(
            "update_state",
            "fail",
            f"The last update failed: {state.error or 'no failure detail was recorded'}",
            "Run: villani update rollback",
            state.model_dump(mode="json"),
        )
    if state.status == "available":
        return DiagnosticCheck(
            "update_state",
            "warn",
            f"Villani {state.available_version} is available; no update is forced.",
            "Run: villani update preview",
            state.model_dump(mode="json"),
        )
    return DiagnosticCheck(
        "update_state",
        "pass",
        f"Update channel {state.policy.channel} is user controlled; no update is forced.",
        details=state.model_dump(mode="json"),
    )


def _entitlement_check(home: Path, environ: Mapping[str, str]) -> DiagnosticCheck:
    state = load_entitlement(home, environ=environ)
    if state.status in {"invalid", "expired"}:
        return DiagnosticCheck(
            "entitlements",
            "warn",
            (
                "The local Pro license is invalid or expired. Core safety and all recorded "
                "evidence remain available."
            ),
            state.repair_action or "Run: villani license status",
            state.model_dump(mode="json"),
        )
    if state.status == "offline_grace":
        return DiagnosticCheck(
            "entitlements",
            "warn",
            "Villani Pro is in offline grace; evidence and core safety are unaffected.",
            "Run: villani license status",
            state.model_dump(mode="json"),
        )
    return DiagnosticCheck(
        "entitlements",
        "pass",
        f"Villani {state.tier.title()} entitlement is {state.status} and was checked locally.",
        details=state.model_dump(mode="json"),
    )


def _managed_installation_check(home: Path) -> DiagnosticCheck:
    root = home / "current"
    manifest_path = root / "package-manifest.json"
    if not root.is_dir():
        return DiagnosticCheck(
            "installation_manifest",
            "pass",
            "Villani is installed through a package environment rather than the managed side-by-side directory.",
            details={"managed_installation": False},
        )
    if not manifest_path.is_file():
        return DiagnosticCheck(
            "installation_manifest",
            "fail",
            "The managed installation has no package manifest.",
            "Run: villani update rollback",
            {"manifest": str(manifest_path)},
        )
    try:
        manifest = PackageManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.files:
            relative = Path(item.path)
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError(f"missing package member {item.path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != item.sha256 or path.stat().st_size != item.size_bytes:
                raise ValueError(f"package member digest mismatch for {item.path}")
    except (OSError, ValueError, ValidationError) as error:
        return DiagnosticCheck(
            "installation_manifest",
            "fail",
            f"Managed package verification failed: {error}",
            "Run: villani update rollback",
            {"manifest": str(manifest_path)},
        )
    return DiagnosticCheck(
        "installation_manifest",
        "pass",
        f"Managed package {manifest.version} contents match their manifest.",
        details={
            "version": manifest.version,
            "operating_system": manifest.operating_system,
            "architecture": manifest.architecture,
            "file_count": len(manifest.files),
        },
    )


def _harness_checks(
    required_harness_ids: frozenset[str] = frozenset({"villani-code"}),
) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    try:
        discoveries = discover_agent_harnesses()
    except (OSError, RuntimeError, ValueError) as error:
        return [
            DiagnosticCheck(
                "harness_discovery",
                "fail",
                f"Coding-system discovery failed: {error}",
                "Run: villani agents doctor",
            )
        ]
    for discovery in discoveries:
        readiness = discovery.readiness
        required = discovery.harness_id in required_harness_ids
        identifier = discovery.harness_id
        checks.append(
            DiagnosticCheck(
                f"harness:{identifier}",
                "pass" if readiness.installed else ("fail" if required else "warn"),
                (
                    f"{discovery.display_name} {readiness.exact_version or ''} is installed."
                    if readiness.installed
                    else f"{discovery.display_name} is not installed."
                ),
                None if readiness.installed else readiness.repair_action,
                discovery.model_dump(mode="json"),
            )
        )
        if readiness.installed:
            authentication_ready = readiness.authentication_status in {
                "ready",
                "not_applicable",
            }
            checks.append(
                DiagnosticCheck(
                    f"authentication:{identifier}",
                    "pass" if authentication_ready else "warn",
                    f"{discovery.display_name} authentication is {readiness.authentication_status}.",
                    None if authentication_ready else readiness.repair_action,
                    {"authentication_status": readiness.authentication_status},
                )
            )
            protocol_ready = bool(
                readiness.conformance_status == "passed"
                or readiness.details.get("protocol_probe") == "passed"
            )
            checks.append(
                DiagnosticCheck(
                    f"protocol:{identifier}",
                    "pass" if protocol_ready else "warn",
                    f"{discovery.display_name} protocol conformance is {readiness.conformance_status}.",
                    None if protocol_ready else readiness.repair_action,
                    {
                        "protocol": readiness.protocol,
                        "conformance_status": readiness.conformance_status,
                        "protocol_probe": readiness.details.get("protocol_probe"),
                    },
                )
            )
            qualified = readiness.qualification_state in {"qualified", "bootstrap"}
            checks.append(
                DiagnosticCheck(
                    f"qualification:{identifier}",
                    "pass" if qualified else "warn",
                    f"{discovery.display_name} qualification is {readiness.qualification_state}.",
                    None if qualified else readiness.repair_action,
                    {"qualification_state": readiness.qualification_state},
                )
            )
    return checks


def _stale_runs_check(home: Path) -> DiagnosticCheck:
    root = home / "runs"
    stale: list[str] = []
    if root.is_dir():
        now = datetime.now(timezone.utc).timestamp()
        for directory in root.iterdir():
            state_path = directory / "state.json"
            if not directory.is_dir() or not state_path.is_file():
                continue
            try:
                value = json.loads(state_path.read_text(encoding="utf-8"))
                terminal = bool(value.get("terminal")) if isinstance(value, dict) else False
                age = now - state_path.stat().st_mtime
            except (OSError, json.JSONDecodeError):
                continue
            if not terminal and age > 24 * 60 * 60:
                stale.append(directory.name)
    return DiagnosticCheck(
        "stale_runs",
        "warn" if stale else "pass",
        (
            f"{len(stale)} run(s) have been non-terminal for more than 24 hours."
            if stale
            else "No stale non-terminal runs were detected."
        ),
        "Run: villani runs" if stale else None,
        {"count": len(stale), "run_ids": stale[:100]},
    )


def _log_retention_check(home: Path) -> DiagnosticCheck:
    path = home / "agentd" / "agentd.log"
    size = path.stat().st_size if path.is_file() else 0
    limit = 5 * 1024 * 1024
    return DiagnosticCheck(
        "bounded_logs",
        "fail" if size > limit + 4096 else "pass",
        (
            f"The active service log is {size} bytes and exceeds its 5 MiB bound."
            if size > limit + 4096
            else "Service logs are within the tested 5 MiB active-file bound."
        ),
        "Run: villani cleanup --apply" if size > limit + 4096 else None,
        {"size_bytes": size, "active_limit_bytes": limit, "retained_backups": 3},
    )


def run_doctor(
    *,
    repository: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    installation_only: bool = False,
) -> DiagnosticReport:
    env = dict(os.environ if environ is None else environ)
    home = villani_home(env)
    evidence_path = home / "diagnostics" / "doctor-latest.json"
    checks: list[DiagnosticCheck] = [
        DiagnosticCheck(
            "version",
            "pass",
            f"Villani {_version('villani', __version__)}",
        ),
        _component_check(),
        _package_health_check(),
        _managed_installation_check(home),
        _migration_check(home),
        _update_check(home),
        _entitlement_check(home, env),
        _storage_check(home),
        _log_retention_check(home),
    ]
    if installation_only:
        healthy = not any(item.status == "fail" for item in checks)
        report = DiagnosticReport(
            utc_now(),
            healthy,
            tuple(checks),
            {"installation_only": True, "inferred_commands_executed": False},
            str(evidence_path),
        )
        try:
            write_json_atomic(evidence_path, report.as_dict())
        except OSError:
            pass
        return report
    configuration_checks, configuration, backends = _configuration_checks(home)
    checks.extend(configuration_checks)
    setup = configuration.get("setup") if isinstance(configuration, Mapping) else None
    saved_repository = setup.get("repository") if isinstance(setup, Mapping) else None
    selected_repository, repository_source = resolve_doctor_repository(
        explicit=repository,
        saved=saved_repository,
        cwd=cwd or Path.cwd(),
    )
    role_systems_configured = False
    cli_coding_active = False
    role_diagnostic_document = None
    if isinstance(configuration, Mapping):
        role_evidence_path = home / "diagnostics" / "agent-systems" / "doctor-latest.json"
        try:
            role_registry = build_agent_system_registry(configuration, backends)
            role_bindings = role_registry.resolve_profile()
            bound_systems = {
                role: role_registry.inspect_configured(role_bindings.system_id_for(role))
                for role in AgentRole
            }
            role_systems_configured = any(
                isinstance(system, CliAgentSystemConfig) for system in bound_systems.values()
            )
            cli_coding_active = isinstance(bound_systems[AgentRole.CODING], CliAgentSystemConfig)
            role_diagnostic_document = diagnose_registry(
                role_registry,
                evidence_path=str(role_evidence_path),
            )
            write_management_evidence(role_evidence_path, role_diagnostic_document)
            for diagnostic in role_diagnostic_document.systems:
                checks.append(
                    DiagnosticCheck(
                        f"agent_system:{diagnostic.system_id}",
                        "pass" if diagnostic.status == DoctorStatus.READY else "fail",
                        (
                            f"{diagnostic.display_name} is ready for "
                            f"{len(diagnostic.configured_roles)} configured role(s)."
                            if diagnostic.status == DoctorStatus.READY
                            else f"{diagnostic.display_name} is {diagnostic.status.value}."
                        ),
                        None
                        if diagnostic.status == DoctorStatus.READY
                        else f"Run: {diagnostic.exact_next_action}",
                        diagnostic.model_dump(mode="json"),
                    )
                )
        except (OSError, TypeError, ValueError, ValidationError) as error:
            checks.append(
                DiagnosticCheck(
                    "agent_system_profile",
                    "fail",
                    f"The active execution profile cannot be diagnosed: {error}",
                    "Run: villani profiles inspect api, then activate a ready profile",
                    {"repositories_modified": False},
                )
            )
    try:
        status = service_status(env)
    except (OSError, ServiceError) as error:
        status = None
        checks.append(
            DiagnosticCheck(
                "service",
                "fail",
                f"Villani Service status could not be read: {error}",
                "Run: villani service restart",
            )
        )
    else:
        assert status is not None
        if status.running:
            checks.append(
                DiagnosticCheck(
                    "service",
                    "pass",
                    "Villani Service is running.",
                    details=status.as_dict(),
                )
            )
        elif status.pid is not None and not status.stale_pid:
            checks.append(
                DiagnosticCheck(
                    "service",
                    "fail",
                    "Villani Service has an unresponsive process.",
                    "Run: villani service stop, inspect the log, then run: villani service start",
                    status.as_dict(),
                )
            )
        elif status.last_error and status.installed:
            checks.append(
                DiagnosticCheck(
                    "service",
                    "fail",
                    "Villani Service installation is present but unhealthy.",
                    "Inspect the service log, then run: villani service restart",
                    status.as_dict(),
                )
            )
        else:
            message = "Villani Service is stopped."
            if status.stale_pid:
                message = "Villani Service is stopped; a stale PID record was detected."
            checks.append(
                DiagnosticCheck(
                    "service",
                    "warn",
                    message,
                    "Run: villani service start",
                    status.as_dict(),
                )
            )
    paths = AgentdPaths(home / "agentd")
    try:
        spool = SQLiteSpool(paths, Limits())
        integrity = spool.integrity_check()
        spool_status = spool.status()
    except Exception:
        integrity, spool_status = "error", {}
    if integrity == "ok":
        checks.append(
            DiagnosticCheck(
                "spool",
                "pass",
                "Local run storage is healthy.",
                details={"integrity": integrity, **spool_status},
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                "spool",
                "fail",
                "Local run storage failed its integrity check.",
                f"Stop the service, preserve {paths.database}, and inspect {paths.log}.",
                {"integrity": integrity},
            )
        )
    pending = int(spool_status.get("pending_events", 0)) + int(
        spool_status.get("pending_outcomes", 0)
    )
    checks.append(
        DiagnosticCheck(
            "pending_synchronization",
            "warn" if pending else "pass",
            (
                f"{pending} item(s) are pending synchronization."
                if pending
                else "No synchronization is pending."
            ),
            "Leave Villani Service running and run: villani doctor" if pending else None,
            {"pending": pending},
        )
    )
    dead_letters = int(spool_status.get("dead_letters", 0))
    checks.append(
        DiagnosticCheck(
            "dead_letters",
            "fail" if dead_letters else "pass",
            (
                f"{dead_letters} synchronization item(s) require attention."
                if dead_letters
                else "No dead letters are present."
            ),
            f"Inspect {paths.log}, resolve the sync error, then run: villani doctor"
            if dead_letters
            else None,
            {"count": dead_letters},
        )
    )
    try:
        _core_healthy, core = build_repository_diagnostics(
            selected_repository,
            configuration or {},
            repository_required=repository is not None,
            environ=env,
            service=status.as_dict() if status else {},
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        core = {
            "schema_version": "villani.doctor.v1",
            "repository": str(selected_repository) if selected_repository else None,
            "required_capabilities": {},
            "git": {"usable": False, "error": error.__class__.__name__},
            "disk": {},
            "service": status.as_dict() if status else {},
            "daemon": {},
            "adapters": [],
            "coding_commands": [],
            "backend_connectivity": [],
            "credentials": [],
            "execution_providers": [],
            "execution_environment_fingerprint": None,
            "repository_inspection": {
                "schema_version": "villani.repository_inspection.v1",
                "repository": str(selected_repository) if selected_repository else None,
                "detected_test_tools": [],
                "likely_test_commands": [],
            },
            "detected_test_tools": [],
            "likely_test_commands": [],
            "inferred_commands_executed": False,
        }
        checks.append(
            DiagnosticCheck(
                "direct_run_readiness",
                "fail",
                f"Direct-run diagnostics could not complete: {error.__class__.__name__}.",
                "Repair the configuration, then run: villani doctor",
            )
        )
    checks.extend(
        _backend_checks(
            backends,
            core["backend_connectivity"],
            role_systems_configured=role_systems_configured,
        )
    )
    if not cli_coding_active:
        checks.extend(_adapter_checks(core.get("adapters", [])))
    checks.extend(
        _harness_checks(frozenset() if cli_coding_active else frozenset({"villani-code"}))
    )
    capabilities = core.get("required_capabilities", {})
    recovery_by_capability = {
        "disk": "Free at least 100 MiB, then run: villani doctor",
        "execution_provider": "Repair the configured execution environment, then run: villani doctor",
        "coding_adapter": "Reinstall Villani Code, then run: villani doctor",
    }
    for capability, recovery in recovery_by_capability.items():
        if capabilities.get(capability) is False and not (
            capability == "coding_adapter" and cli_coding_active
        ):
            checks.append(
                DiagnosticCheck(
                    f"required_capability:{capability}",
                    "fail",
                    f"Required capability {capability} is unavailable.",
                    recovery,
                )
            )
    disk_usable = capabilities.get("disk") is not False
    checks.append(
        DiagnosticCheck(
            "disk",
            "pass" if disk_usable else "fail",
            "Disk capacity is sufficient for isolated work."
            if disk_usable
            else "Disk capacity is insufficient for isolated work.",
            None if disk_usable else "Free at least 100 MiB, then run: villani doctor",
            core.get("disk", {}),
        )
    )
    isolation_usable = capabilities.get("execution_provider") is not False
    checks.append(
        DiagnosticCheck(
            "isolation",
            "pass" if isolation_usable else "fail",
            "The configured execution provider can create isolated attempts."
            if isolation_usable
            else "The configured execution provider cannot provide required isolation.",
            None
            if isolation_usable
            else "Repair the configured execution environment, then run: villani doctor",
            {"execution_providers": core.get("execution_providers", [])},
        )
    )
    validation_commands = core.get("likely_test_commands", [])
    checks.append(
        DiagnosticCheck(
            "validation",
            "pass" if validation_commands else "warn",
            (
                f"{len(validation_commands)} repository validation command(s) were detected without execution."
                if validation_commands
                else "No repository validation command was detected; alternative evidence will be required."
            ),
            None
            if validation_commands
            else "Run a task with: villani run TASK --validation-command COMMAND",
            {"commands": validation_commands, "executed": False},
        )
    )
    checks.append(
        DiagnosticCheck(
            "permissions",
            "pass" if capabilities.get("repository") is not False else "fail",
            "Repository and local storage permissions are sufficient."
            if capabilities.get("repository") is not False
            else "Repository permissions are insufficient.",
            None
            if capabilities.get("repository") is not False
            else "Fix read/write permissions, then run: villani doctor",
        )
    )
    checks.append(_git_check())
    checks.append(
        _repository_check(
            selected_repository,
            repository_source,
            explicit=repository is not None,
        )
    )
    checks.append(
        _console_check(
            status.console_url if status else None,
            service_running=bool(status and status.running),
        )
    )
    checks.append(_stale_runs_check(home))
    healthy = not any(item.status == "fail" for item in checks)
    core["repository_source"] = repository_source
    core["agent_systems"] = (
        role_diagnostic_document.model_dump(mode="json")
        if role_diagnostic_document is not None
        else None
    )
    report = DiagnosticReport(utc_now(), healthy, tuple(checks), core, str(evidence_path))
    try:
        write_json_atomic(evidence_path, report.as_dict())
    except OSError:
        pass
    return report


def render_human(report: DiagnosticReport) -> str:
    labels = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = ["Villani Doctor", ""]
    for check in report.checks:
        lines.append(f"[{labels[check.status]}] {check.message}")
        if check.recovery_action:
            lines.append(check.recovery_action)
        if check.status == "fail":
            lines.append("Repositories modified: no")
            lines.append(f"Evidence: {report.evidence_path}")
    lines.extend(("", "Villani is ready." if report.healthy else "Villani needs attention."))
    return "\n".join(lines)


def render_json(report: DiagnosticReport) -> str:
    return json.dumps(report.as_dict(), sort_keys=True)
