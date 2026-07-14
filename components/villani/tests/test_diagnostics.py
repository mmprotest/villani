from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from villani_distribution import diagnostics
from villani_distribution.cli import app
from villani_distribution.diagnostics import DiagnosticCheck
from villani_distribution.onboarding import (
    BackendProbe,
    ProviderDetection,
    build_configuration,
    write_configuration_atomic,
)
from villani_distribution.services import ServiceStatus


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
    assert all(
        item["recovery_action"]
        for item in value["checks"]
        if item["status"] == "fail"
    )
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
        "test_backend",
        lambda *_args, **_kwargs: BackendProbe(
            True, "connection", "Model fixture-coder is available.", "fixture-coder"
        ),
    )
    monkeypatch.setattr(
        diagnostics,
        "_console_check",
        lambda _url: DiagnosticCheck("browser_ui", "pass", "Villani Console is available."),
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


def test_lm_studio_recovery_is_concrete(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        provider = "local"
        base_url = "http://127.0.0.1:1234/v1"
        model = "missing-model"
        api_key_env = None
        metadata = {"setup_provider_identifier": "lm-studio", "capability_status": "unrated"}
        billing_mode = "unknown"

        @staticmethod
        def api_key_configured() -> bool:
            return False

        @staticmethod
        def api_key_status() -> str:
            return "missing"

    monkeypatch.setattr(
        diagnostics,
        "test_backend",
        lambda *_args, **_kwargs: BackendProbe(False, "model", "missing", "missing-model"),
    )
    check = next(
        item
        for item in diagnostics._backend_checks({"default": Backend()})
        if item.identifier == "model_availability:default"
    )
    assert check.message == "LM Studio is reachable but no model is loaded."
    assert check.recovery_action == "Load a model, then run: villani doctor"
