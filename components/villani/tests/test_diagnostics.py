from __future__ import annotations

import json
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest
from typer.testing import CliRunner

from villani_distribution import diagnostics
from villani_distribution.cli import app
from villani_distribution.diagnostics import DiagnosticCheck
from villani_distribution.onboarding import (
    ProviderDetection,
    build_configuration,
    write_configuration_atomic,
)
from villani_distribution.services import ServiceStatus
from villani_ops.core.backend import Backend
from villani_ops.diagnostics import probe_backend, resolve_doctor_repository


def _git_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Villani Tests"],
        ["git", "config", "user.email", "tests@example.invalid"],
    ):
        completed = subprocess.run(command, cwd=path, text=True, capture_output=True)
        assert completed.returncode == 0, completed.stderr
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return path.resolve()


@contextmanager
def _model_server(*, expected_key: str | None = None) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            authorized = (
                expected_key is None
                or self.headers.get("Authorization") == f"Bearer {expected_key}"
            )
            status = 200 if authorized else 401
            value = {"data": [{"id": "fixture-coder"}]} if authorized else {"error": "unauthorized"}
            payload = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def stopped(home: Path) -> ServiceStatus:
    return ServiceStatus(
        "win32",
        False,
        str(home / "service" / "windows-task.json"),
        False,
        running=False,
        log_path=str(home / "agentd" / "agentd.log"),
    )


def running(home: Path) -> ServiceStatus:
    return ServiceStatus(
        "win32",
        False,
        str(home / "service" / "windows-task.json"),
        False,
        running=True,
        pid=123,
        log_path=str(home / "agentd" / "agentd.log"),
        console_url="http://127.0.0.1:45678/console",
    )


def local_detection() -> ProviderDetection:
    return ProviderDetection(
        "lm-studio",
        "LM Studio",
        "http://127.0.0.1:1234/v1",
        "connected",
        ("fixture-coder",),
        diagnostic_message="LM Studio is reachable with 1 model(s).",
    )


def test_doctor_json_is_stable_and_every_failed_check_has_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    monkeypatch.setattr(diagnostics, "service_status", lambda _env=None: stopped(home))
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    value = json.loads(result.output)
    assert value["schema_version"] == "villani.doctor.v1"
    assert value["healthy"] is False
    assert value["summary"]["failed"] >= 3
    assert all(item["recovery_action"] for item in value["checks"] if item["status"] == "fail")
    configuration = next(item for item in value["checks"] if item["identifier"] == "configuration")
    assert configuration["message"] == "No coding backend is configured."
    assert configuration["recovery_action"] == "Run: villani setup"
    service = next(item for item in value["checks"] if item["identifier"] == "service")
    assert service["message"] == "Villani Service is stopped."
    assert service["recovery_action"] == "Run: villani service start"


def test_doctor_human_report_prints_recovery_on_following_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    monkeypatch.setattr(diagnostics, "service_status", lambda _env=None: stopped(home))
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "No coding backend is configured.\nRun: villani setup" in result.output
    assert "Villani Service is stopped.\nRun: villani service start" in result.output


def test_doctor_reports_future_schema_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("config_version: 99\nbackends: {}\n", encoding="utf-8")
    monkeypatch.setenv("VILLANI_HOME", str(home))
    monkeypatch.setattr(diagnostics, "service_status", lambda _env=None: stopped(home))
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    value = json.loads(result.output)
    check = next(item for item in value["checks"] if item["identifier"] == "configuration")
    assert "newer than supported" in check["message"]
    assert check["recovery_action"] == "Run: villani setup"


def test_healthy_doctor_covers_all_required_diagnostic_domains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    repository = Path.cwd().resolve()
    write_configuration_atomic(
        home / "config.yaml",
        build_configuration(local_detection(), "fixture-coder", repository=repository),
    )
    monkeypatch.setattr(diagnostics, "service_status", lambda _env=None: running(home))
    monkeypatch.setattr(
        diagnostics,
        "build_repository_diagnostics",
        lambda *_args, **_kwargs: (
            True,
            {
                "schema_version": "villani.doctor.v1",
                "repository": str(repository),
                "required_capabilities": {
                    "git": True,
                    "repository": True,
                    "disk": True,
                    "execution_provider": True,
                    "coding_adapter": True,
                    "backends": True,
                },
                "git": {"usable": True},
                "disk": {"usable": True},
                "service": {},
                "daemon": {},
                "adapters": [],
                "coding_commands": [],
                "backend_connectivity": [
                    {
                        "name": "default",
                        "setup_provider_identifier": "lm-studio",
                        "credential_status": "not_required",
                        "endpoint_reachable": True,
                        "model_available": True,
                        "probe_status": "ok",
                        "usable": True,
                        "model_tokens_spent": 0,
                    }
                ],
                "credentials": [],
                "execution_providers": [],
                "execution_environment_fingerprint": "fixture",
                "repository_inspection": {
                    "detected_test_tools": [],
                    "likely_test_commands": [],
                },
                "detected_test_tools": [],
                "likely_test_commands": [],
                "inferred_commands_executed": False,
            },
        ),
    )
    monkeypatch.setattr(
        diagnostics,
        "_console_check",
        lambda _url, **_kwargs: DiagnosticCheck(
            "browser_ui", "pass", "Villani Console is available."
        ),
    )
    report = diagnostics.run_doctor(environ={"VILLANI_HOME": str(home)})
    assert report.healthy is True
    identifiers = {item.identifier.split(":", 1)[0] for item in report.checks}
    assert {
        "version",
        "component_compatibility",
        "configuration",
        "service",
        "spool",
        "configured_backends",
        "model_server_reachability",
        "model_availability",
        "git",
        "repository_access",
        "package_health",
        "browser_ui",
        "pending_synchronization",
        "dead_letters",
        "storage_permissions",
    } <= identifiers


def test_lm_studio_recovery_is_concrete() -> None:
    class Backend:
        provider = "local"
        base_url = "http://127.0.0.1:1234/v1"
        model = "missing-model"
        api_key_env = None
        metadata = {"setup_provider_identifier": "lm-studio", "capability_status": "unrated"}
        billing_mode = "unknown"

    check = next(
        item
        for item in diagnostics._backend_checks(
            {"default": Backend()},
            [
                {
                    "name": "default",
                    "setup_provider_identifier": "lm-studio",
                    "credential_status": "not_required",
                    "endpoint_reachable": True,
                    "model_available": False,
                    "probe_status": "model_missing",
                    "usable": False,
                    "model_tokens_spent": 0,
                }
            ],
        )
        if item.identifier == "model_availability:default"
    )
    assert check.message == "LM Studio is reachable but no model is loaded."
    assert check.recovery_action == "Load a model, then run: villani doctor"


def test_repository_resolution_explicit_saved_current_and_unavailable(
    tmp_path: Path,
) -> None:
    explicit = _git_repository(tmp_path / "explicit")
    saved = _git_repository(tmp_path / "saved")
    current = _git_repository(tmp_path / "current")
    missing = tmp_path / "missing"

    assert resolve_doctor_repository(explicit=explicit, saved=saved, cwd=current) == (
        explicit,
        "explicit",
    )
    assert resolve_doctor_repository(explicit=None, saved=saved, cwd=current) == (
        saved,
        "saved_setup",
    )
    assert resolve_doctor_repository(explicit=None, saved=missing, cwd=current) == (
        current,
        "current_directory",
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_doctor_repository(explicit=None, saved=missing, cwd=empty) == (
        None,
        "unavailable",
    )


@pytest.mark.parametrize("kind", ["missing", "file", "directory"])
def test_explicit_invalid_repository_is_usage_error(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    candidate = tmp_path / kind
    if kind == "file":
        candidate.write_text("not a repository", encoding="utf-8")
    elif kind == "directory":
        candidate.mkdir()
    result = CliRunner().invoke(app, ["doctor", "--repo", str(candidate), "--json"])
    assert result.exit_code == 2
    assert "repository" in result.output.lower()


def test_stopped_and_stale_services_are_recoverable_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    repository = _git_repository(tmp_path / "repo")
    write_configuration_atomic(
        home / "config.yaml",
        build_configuration(local_detection(), "fixture-coder", repository=repository),
    )
    ready = {
        "schema_version": "villani.doctor.v1",
        "repository": str(repository),
        "required_capabilities": {
            "git": True,
            "repository": True,
            "disk": True,
            "execution_provider": True,
            "coding_adapter": True,
            "backends": True,
        },
        "git": {"usable": True},
        "disk": {"usable": True},
        "service": {},
        "daemon": {},
        "adapters": [],
        "coding_commands": [],
        "backend_connectivity": [
            {
                "name": "default",
                "setup_provider_identifier": "lm-studio",
                "credential_status": "not_required",
                "endpoint_reachable": True,
                "model_available": True,
                "probe_status": "ok",
                "usable": True,
                "model_tokens_spent": 0,
            }
        ],
        "credentials": [],
        "execution_providers": [],
        "execution_environment_fingerprint": "fixture",
        "repository_inspection": {
            "detected_test_tools": [],
            "likely_test_commands": [],
        },
        "detected_test_tools": [],
        "likely_test_commands": [],
        "inferred_commands_executed": False,
    }
    monkeypatch.setattr(
        diagnostics,
        "build_repository_diagnostics",
        lambda *_args, **_kwargs: (True, dict(ready)),
    )
    monkeypatch.setattr(
        diagnostics,
        "_console_check",
        lambda _url, **_kwargs: DiagnosticCheck(
            "browser_ui", "warn", "Villani Console is unavailable.", "Run: villani service start"
        ),
    )
    for service in (
        stopped(home),
        ServiceStatus(
            "win32",
            True,
            str(home / "service" / "windows-task.json"),
            False,
            running=False,
            pid=999_999,
            stale_pid=True,
            log_path=str(home / "agentd" / "agentd.log"),
        ),
    ):
        monkeypatch.setattr(diagnostics, "service_status", lambda _env=None, value=service: value)
        report = diagnostics.run_doctor(repository=repository, environ={"VILLANI_HOME": str(home)})
        service_check = next(item for item in report.checks if item.identifier == "service")
        assert service_check.status == "warn"
        assert service_check.recovery_action == "Run: villani service start"
        assert report.healthy is True


def test_backend_diagnostics_cover_runtime_credentials_and_zero_token_spend() -> None:
    secret = "fixture-doctor-secret-never-serialize"
    with _model_server() as endpoint:
        local = probe_backend(
            Backend(
                name="local",
                provider="local",
                base_url=endpoint,
                model="fixture-coder",
            ),
            environ={},
        )
    with _model_server(expected_key=secret) as endpoint:
        cloud_backend = Backend(
            name="cloud",
            provider="openai",
            base_url=endpoint,
            model="fixture-coder",
            api_key_env="OPENAI_API_KEY",
        )
        missing = probe_backend(cloud_backend, environ={})
        reachable = probe_backend(cloud_backend, environ={"OPENAI_API_KEY": secret})
        rejected = probe_backend(cloud_backend, environ={"OPENAI_API_KEY": "wrong-fixture-secret"})
    assert local["usable"] is True
    assert local["credential_status"] == "not_required"
    assert missing["credential_status"] == "env_var_missing"
    assert missing["probe_status"] == "credential_missing"
    assert reachable["usable"] is True
    assert rejected["probe_status"] == "authentication_failed"
    assert all(item["model_tokens_spent"] == 0 for item in (local, missing, reachable, rejected))
    assert secret not in json.dumps([local, missing, reachable, rejected])


def test_json_contract_contains_both_doctor_generations_without_inference() -> None:
    details = {
        "repository": None,
        "required_capabilities": {},
        "git": {},
        "disk": {},
        "service": {},
        "daemon": {},
        "adapters": [],
        "coding_commands": [],
        "backend_connectivity": [],
        "credentials": [],
        "execution_providers": [],
        "execution_environment_fingerprint": None,
        "repository_inspection": {},
        "detected_test_tools": [],
        "likely_test_commands": [],
        "inferred_commands_executed": False,
    }
    report = diagnostics.DiagnosticReport(
        "2026-01-01T00:00:00Z",
        True,
        (DiagnosticCheck("fixture", "warn", "Optional fixture is stopped."),),
        details,
    ).as_dict()
    assert report["schema_version"] == "villani.doctor.v1"
    assert report["healthy"] is report["ok"] is True
    assert report["summary"] == {"passed": 0, "warnings": 1, "failed": 0}
    assert report["inferred_commands_executed"] is False
    assert set(details) <= set(report)


def test_adapter_probe_timeout_is_a_presence_warning_not_absence() -> None:
    checks = diagnostics._adapter_checks(
        [
            {
                "adapter": "villani-code",
                "available": True,
                "executable_status": "present",
                "executable_path": "/fixture/bin/villani-code",
                "probe_status": "version_timed_out",
                "probe_timeout_seconds": 1.5,
                "runtime_status": "successful_recent_run",
                "last_successful_use": {
                    "run_id": "run_fixture",
                    "attempt_id": "attempt_001",
                },
            }
        ]
    )
    assert len(checks) == 1
    check = checks[0]
    assert check.status == "warn"
    assert "executable is present" in check.message
    assert "1.5 seconds" in check.message
    assert "successful recent coding run" in check.message
    assert check.details["model_tokens_spent"] == 0
