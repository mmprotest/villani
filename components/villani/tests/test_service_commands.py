from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from villani_distribution import cli, services
from villani_distribution.cli import app
from villani_distribution.services import ServiceStatus


def status(
    home: Path,
    *,
    running: bool,
    pid: int | None = None,
    stale: bool = False,
    automatic: bool = False,
) -> ServiceStatus:
    return ServiceStatus(
        "win32",
        automatic,
        str(home / "service" / "windows-task.json"),
        running if automatic else False,
        running=running,
        automatic_start=automatic,
        pid=pid,
        stale_pid=stale,
        log_path=str(home / "agentd" / "agentd.log"),
        console_url="http://127.0.0.1:32123/console" if running else None,
    )


def test_start_is_safe_when_service_is_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = status(tmp_path, running=True, pid=42)
    monkeypatch.setattr(services, "service_status", lambda _env=None: current)
    monkeypatch.setattr(
        services,
        "start_background",
        lambda *_args, **_kwargs: pytest.fail("duplicate process was started"),
    )
    result = services.start_service(environ={"VILLANI_HOME": str(tmp_path)})
    assert result is current


def test_stopped_service_starts_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter([status(tmp_path, running=False), status(tmp_path, running=True, pid=91)])
    monkeypatch.setattr(services, "service_status", lambda _env=None: next(values))
    monkeypatch.setattr(services, "check_upgrade", lambda *_args, **_kwargs: None)
    calls: list[Path] = []
    monkeypatch.setattr(
        services,
        "start_background",
        lambda _config, paths, **_kwargs: calls.append(paths.root) or {"pid": 91},
    )
    monkeypatch.setattr(services, "_write_state", lambda *_args, **_kwargs: None)
    result = services.start_service(environ={"VILLANI_HOME": str(tmp_path)})
    assert result.running is True
    assert calls == [tmp_path / "agentd"]


def test_stale_pid_files_are_removed_before_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = services._paths({"VILLANI_HOME": str(tmp_path)})
    paths.root.mkdir(parents=True)
    paths.endpoint.write_text('{"endpoint":"http://127.0.0.1:9","pid":99999999}', encoding="utf-8")
    paths.token.write_text("stale", encoding="utf-8")
    values = iter(
        [
            status(tmp_path, running=False, pid=99_999_999, stale=True),
            status(tmp_path, running=True, pid=92),
        ]
    )
    monkeypatch.setattr(services, "service_status", lambda _env=None: next(values))
    monkeypatch.setattr(services, "check_upgrade", lambda *_args, **_kwargs: None)

    def start(_config: object, _paths: object, **_kwargs: object) -> dict[str, int]:
        assert not paths.endpoint.exists()
        assert not paths.token.exists()
        return {"pid": 92}

    monkeypatch.setattr(services, "start_background", start)
    monkeypatch.setattr(services, "_write_state", lambda *_args, **_kwargs: None)
    assert services.start_service(environ={"VILLANI_HOME": str(tmp_path)}).running


def test_stop_is_safe_when_service_is_already_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = status(tmp_path, running=False)
    monkeypatch.setattr(services, "service_status", lambda _env=None: current)
    monkeypatch.setattr(
        services,
        "stop_background",
        lambda *_args, **_kwargs: pytest.fail("stop called for an absent process"),
    )
    assert services.stop_service(environ={"VILLANI_HOME": str(tmp_path)}) is current


def test_open_refuses_to_launch_a_dead_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "service_status", lambda: status(tmp_path, running=False))
    launches: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, **_kwargs: launches.append(url))
    result = CliRunner().invoke(app, ["open"])
    assert result.exit_code == 2
    assert "Villani Service is stopped" in result.output
    assert "villani service start" in result.output
    assert launches == []


def test_open_launches_only_the_single_console_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = status(tmp_path, running=True, pid=5)
    monkeypatch.setattr(cli, "service_status", lambda: current)
    monkeypatch.setattr(cli, "_probe_console", lambda _url: True)
    launches: list[str] = []
    monkeypatch.setattr(
        cli.webbrowser, "open", lambda url, **_kwargs: launches.append(url) or True
    )
    result = CliRunner().invoke(app, ["open"])
    assert result.exit_code == 0
    assert launches == [current.console_url]
    assert "Flight Recorder" not in result.output


def test_service_status_json_includes_recovery_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = status(tmp_path, running=False)
    monkeypatch.setattr(cli, "service_status", lambda: current)
    result = CliRunner().invoke(app, ["service", "status", "--json"])
    assert result.exit_code == 0
    value = json.loads(result.output)
    assert value["running"] is False
    assert value["log_path"].endswith("agentd.log")
    assert "last_error" in value
    assert "automatic_start" in value


def test_real_service_lifecycle_and_console_are_cross_platform(tmp_path: Path) -> None:
    env = {"VILLANI_HOME": str(tmp_path / "home")}
    started = None
    try:
        started = services.start_service(environ=env)
        assert started.running is True
        assert started.pid is not None
        repeated = services.start_service(environ=env)
        assert repeated.pid == started.pid
        assert repeated.console_url == started.console_url
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(str(started.console_url), timeout=5) as response:
            body = response.read().decode("utf-8")
        assert response.status == 200
        assert "Villani Console" in body
        assert "Villani Service is running" in body
    finally:
        if started is not None:
            stopped = services.stop_service(environ=env)
            assert stopped.running is False
            assert not services._paths(env).endpoint.exists()
