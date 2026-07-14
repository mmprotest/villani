"""Human and machine-readable public Villani diagnostics."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from villani_agentd.config import AgentdPaths, Limits
from villani_agentd.spool import SQLiteSpool
from villani_ops.diagnostics import (
    build_repository_diagnostics,
    resolve_doctor_repository,
)

from .onboarding import (
    SetupError,
    load_configuration,
    utc_now,
    validate_configuration,
)
from .services import ServiceError, service_status, villani_home


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    identifier: str
    status: str
    message: str
    recovery_action: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    generated_at: str
    healthy: bool
    checks: tuple[DiagnosticCheck, ...]
    details: dict[str, Any] = field(default_factory=dict)

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
            "checks": [item.as_dict() for item in self.checks],
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
        value["checks"] = [item.as_dict() for item in self.checks]
        return value


_COMPONENTS = {
    "villani": ("villani_distribution", "0.3.0rc1"),
    "villani-ops": ("villani_ops", "0.2.0"),
    "villani-code": ("villani_code", "0.1.0rc1"),
    "villani-agentd": ("villani_agentd", "0.1.0"),
}


def _version(distribution: str, fallback: str = "unknown") -> str:
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
            "Supply one with: villani doctor --repo PATH" if not explicit else None,
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
    backends: Mapping[str, Any], reports: list[dict[str, Any]]
) -> list[DiagnosticCheck]:
    if not backends:
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


def run_doctor(
    *,
    repository: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> DiagnosticReport:
    env = dict(os.environ if environ is None else environ)
    home = villani_home(env)
    checks: list[DiagnosticCheck] = [
        DiagnosticCheck(
            "version",
            "pass",
            f"Villani {_version('villani', '0.3.0rc1')}",
        ),
        _component_check(),
        _package_health_check(),
    ]
    configuration_checks, configuration, backends = _configuration_checks(home)
    checks.extend(configuration_checks)
    setup = configuration.get("setup") if isinstance(configuration, Mapping) else None
    saved_repository = setup.get("repository") if isinstance(setup, Mapping) else None
    selected_repository, repository_source = resolve_doctor_repository(
        explicit=repository,
        saved=saved_repository,
        cwd=cwd or Path.cwd(),
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
    checks.extend(_backend_checks(backends, core["backend_connectivity"]))
    capabilities = core.get("required_capabilities", {})
    recovery_by_capability = {
        "disk": "Free at least 100 MiB, then run: villani doctor",
        "execution_provider": "Repair the configured execution environment, then run: villani doctor",
        "coding_adapter": "Reinstall Villani Code, then run: villani doctor",
    }
    for capability, recovery in recovery_by_capability.items():
        if capabilities.get(capability) is False:
            checks.append(
                DiagnosticCheck(
                    f"required_capability:{capability}",
                    "fail",
                    f"Required capability {capability} is unavailable.",
                    recovery,
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
    checks.append(_storage_check(home))
    checks.append(
        _console_check(
            status.console_url if status else None,
            service_running=bool(status and status.running),
        )
    )
    healthy = not any(item.status == "fail" for item in checks)
    core["repository_source"] = repository_source
    return DiagnosticReport(utc_now(), healthy, tuple(checks), core)


def render_human(report: DiagnosticReport) -> str:
    labels = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = ["Villani Doctor", ""]
    for check in report.checks:
        lines.append(f"[{labels[check.status]}] {check.message}")
        if check.recovery_action:
            lines.append(check.recovery_action)
    lines.extend(("", "Villani is ready." if report.healthy else "Villani needs attention."))
    return "\n".join(lines)


def render_json(report: DiagnosticReport) -> str:
    return json.dumps(report.as_dict(), sort_keys=True)
