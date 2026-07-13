from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from villani_distribution import services, vfr
from villani_distribution.cli import app
from villani_distribution.migrations import MigrationError, check_upgrade
from villani_agentd.config import AgentdPaths, Limits
from villani_agentd.spool import SQLiteSpool


def test_public_help_lists_distribution_service_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "install-service" in result.output
    assert "service" in result.output
    assert "uninstall-service" in result.output


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_user_service_definitions_and_uninstall_preserve_data(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "service-root"
    data = home / "runs" / "run-1" / "artifact.txt"
    data.parent.mkdir(parents=True)
    data.write_text("preserve", encoding="utf-8")
    fake_agentd = tmp_path / ("villani-agentd.exe" if platform == "win32" else "villani-agentd")
    fake_agentd.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(services, "_agentd_executable", lambda: fake_agentd)
    env = {
        "VILLANI_HOME": str(home),
        "VILLANI_SERVICE_PLATFORM": platform,
        "VILLANI_SERVICE_TEST_ROOT": str(root),
        "VILLANI_SERVICE_DRY_RUN": "1",
    }
    installed = services.install_service(env)
    assert installed.installed is True
    definition = Path(installed.definition)
    assert definition.is_file()
    content = definition.read_bytes()
    assert b"villani-agentd" in content
    assert b"service-run" in content
    removed = services.uninstall_service(environ=env)
    assert removed.installed is False
    assert data.read_text(encoding="utf-8") == "preserve"


def test_delete_data_requires_explicit_confirmation(tmp_path: Path) -> None:
    env = {
        "VILLANI_HOME": str(tmp_path / "home"),
        "VILLANI_SERVICE_PLATFORM": "linux",
        "VILLANI_SERVICE_TEST_ROOT": str(tmp_path / "service"),
        "VILLANI_SERVICE_DRY_RUN": "1",
    }
    Path(env["VILLANI_HOME"]).mkdir()
    with pytest.raises(services.ServiceError, match="confirm-delete-data"):
        services.uninstall_service(delete_data=True, environ=env)
    services.uninstall_service(delete_data=True, confirm_delete_data=True, environ=env)
    assert not Path(env["VILLANI_HOME"]).exists()


def _legacy_spool(path: Path) -> None:
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE runs(run_id TEXT PRIMARY KEY);
            CREATE TABLE events(event_id TEXT PRIMARY KEY, payload_json TEXT);
            CREATE TABLE artifacts(artifact_id TEXT PRIMARY KEY);
            INSERT INTO events VALUES('evt-old', '{"old":true}');
            """
        )


def test_previous_package_fixture_migrates_without_rewriting_config_or_runs(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    original_config = "policy:\n  version: bootstrap_v1\nbackends: {}\n"
    config.write_text(original_config, encoding="utf-8")
    run = home / "runs" / "run-old"
    run.mkdir(parents=True)
    manifest = run / "manifest.json"
    original_manifest = '{"schema_version":"villani.run_manifest.v1","run_id":"run-old"}\n'
    manifest.write_text(original_manifest, encoding="utf-8")
    spool = home / "agentd" / "spool.sqlite3"
    _legacy_spool(spool)
    report = check_upgrade(home, apply=True)
    assert (report.spool_version_before, report.spool_version_after) == (0, 4)
    assert report.protocol_majors == (1,)
    assert config.read_text(encoding="utf-8") == original_config
    assert manifest.read_text(encoding="utf-8") == original_manifest
    with sqlite3.connect(spool) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT event_id FROM events").fetchone()[0] == "evt-old"


@pytest.mark.parametrize("version", [0, 1, 2, 3, 4])
def test_distribution_and_agentd_share_spool_v4_contract(
    version: int, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    spool = home / "agentd" / "spool.sqlite3"
    _legacy_spool(spool)
    with sqlite3.connect(spool) as connection:
        connection.execute(f"PRAGMA user_version={version}")
        connection.execute("INSERT INTO runs(run_id) VALUES('preserved-run')")
        connection.commit()

    dry_run = check_upgrade(home, apply=False)
    assert (dry_run.spool_version_before, dry_run.spool_version_after) == (version, version)
    with sqlite3.connect(spool) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version

    applied = check_upgrade(home, apply=True)
    assert (applied.spool_version_before, applied.spool_version_after) == (version, 4)
    repeated = check_upgrade(home, apply=True)
    assert (repeated.spool_version_before, repeated.spool_version_after) == (4, 4)
    SQLiteSpool(AgentdPaths(home / "agentd"), Limits())
    with sqlite3.connect(spool) as connection:
        assert connection.execute("SELECT run_id FROM runs").fetchone()[0] == "preserved-run"


def test_distribution_rejects_future_agentd_spool(tmp_path: Path) -> None:
    home = tmp_path / "home"
    spool = home / "agentd" / "spool.sqlite3"
    _legacy_spool(spool)
    with sqlite3.connect(spool) as connection:
        connection.execute("PRAGMA user_version=5")
    with pytest.raises(MigrationError, match="newer than supported version 4"):
        check_upgrade(home)


def test_newer_upgrade_versions_are_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("config_version: 99\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="newer"):
        check_upgrade(home)


def test_vfr_launcher_executes_only_bundled_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(vfr, "command_prefix", lambda: ["/bundle/vfr-native"])
    monkeypatch.setattr(
        vfr.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 7),
    )
    assert vfr.main(["--help"]) == 7
    assert calls == [["/bundle/vfr-native", "--help"]]
    assert "node" not in calls[0]


def _release_module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "build-release.py"
    spec = importlib.util.spec_from_file_location("build_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_archive_and_checksums_are_reproducible(tmp_path: Path) -> None:
    release = _release_module()
    runtime = tmp_path / "runtime"
    native_vfr = tmp_path / "vfr"
    runtime.write_bytes(b"python-runtime-fixture")
    native_vfr.write_bytes(b"vfr-runtime-fixture")
    first = release.build_archive(runtime, native_vfr, tmp_path / "one", "linux")
    second = release.build_archive(runtime, native_vfr, tmp_path / "two", "linux")
    assert first.read_bytes() == second.read_bytes()
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert (tmp_path / "one" / "SHA256SUMS").read_text().startswith(digest)
