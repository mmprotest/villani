from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from villani_distribution.maintenance import cleanup
from villani_distribution.support_bundle import SupportBundleBuilder
from villani_distribution.update_system import UpdateError, UpdateManager


ROOT = Path(__file__).resolve().parents[3]


def _release_module():
    path = ROOT / "scripts" / "build-release.py"
    spec = importlib.util.spec_from_file_location("pt10_build_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _package_smoke_module():
    path = ROOT / "scripts" / "ci-package-smoke.py"
    spec = importlib.util.spec_from_file_location("pt10_package_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_managed_command_prefix_preserves_launcher_paths_with_spaces(
    tmp_path: Path,
) -> None:
    module = _package_smoke_module()
    home = tmp_path / "home with spaces"
    prefix = module.command_prefix(home, "villani")
    if os.name == "nt":
        assert prefix[1:5] == ["/d", "/s", "/c", "call"]
        assert prefix[5] == str(home / "bin" / "villani.cmd")
    else:
        assert prefix == [str(home / "bin" / "villani")]


def _artifact_scan_module():
    path = ROOT / "scripts" / "scan-release-artifact.py"
    spec = importlib.util.spec_from_file_location("pt10_artifact_scan", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact(root: Path, marker: bytes) -> Path:
    root.mkdir(parents=True)
    runtime = root / "runtime"
    vfr = root / "vfr"
    runtime.write_bytes(b"runtime-" + marker)
    vfr.write_bytes(b"vfr-" + marker)
    return _release_module().build_archive(runtime, vfr, root / "release")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_verified_install_update_and_explicit_rollback_are_atomic(tmp_path: Path) -> None:
    first = _artifact(tmp_path / "first", b"first")
    second = _artifact(tmp_path / "second", b"second")
    home = tmp_path / "home"
    (home / "config.yaml").parent.mkdir(parents=True)
    (home / "config.yaml").write_text("config_version: 1\n", encoding="utf-8")
    manager = UpdateManager(home)

    installed = manager.install_artifact(
        str(first), _digest(first), verifier=lambda _root, _version: (True, "proved")
    )
    assert installed.status == "verified"
    command = home / "current" / f"villani{'.exe' if os.name == 'nt' else ''}"
    assert command.read_bytes() == b"runtime-first"
    if os.name == "nt":
        runners = list((home / "runners").glob("*/villani.exe"))
        assert len(runners) == 1
        launcher = (home / "bin" / "villani.cmd").read_text(encoding="utf-8")
        assert "\\runners\\" in launcher and "\\current\\" not in launcher

    upgraded = manager.install_artifact(
        str(second), _digest(second), verifier=lambda _root, _version: (True, "proved")
    )
    assert upgraded.previous_installation is not None
    assert command.read_bytes() == b"runtime-second"
    rolled_back = manager.rollback(verifier=lambda _root, _version: (True, "proved"))
    assert rolled_back.status == "rolled_back"
    assert command.read_bytes() == b"runtime-first"
    assert rolled_back.repositories_modified is False


def test_release_manifest_sbom_and_streamed_secret_scan_are_verifiable(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path / "artifact", b"safe")
    report = _artifact_scan_module().inspect_archive(artifact)
    assert report["passed"] is True
    assert report["manifest_version"] == "1.0.0"
    assert report["files_verified"] == 8
    assert report["sbom_components"] > 10
    assert report["secret_findings"] == []


def test_performance_contract_covers_every_pt10_reliability_target() -> None:
    document = json.loads(
        (ROOT / "release" / "performance-targets.json").read_text(encoding="utf-8")
    )
    targets = {item["id"]: item for item in document["targets"]}
    assert document["version"] == "1.0.0"
    assert {
        "shell_version_render_p95",
        "installation_doctor_startup_p95",
        "idempotent_submission_duplicate_runs",
        "durable_acknowledged_event_loss",
        "console_reconnect",
        "structured_log_active_file",
        "structured_log_backups",
        "cleanup_default_mutations",
        "retention_run_deletions",
        "crash_recovery_repository_mutations",
        "idle_cpu_time",
        "idle_model_or_gpu_processes",
    } == set(targets)
    assert all(item["evidence"] for item in targets.values())


def test_failed_startup_verification_restores_previous_install_and_config(
    tmp_path: Path,
) -> None:
    first = _artifact(tmp_path / "first", b"first")
    second = _artifact(tmp_path / "second", b"second")
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("config_version: 1\nname: before\n", encoding="utf-8")
    manager = UpdateManager(home)
    manager.install_artifact(
        str(first), _digest(first), verifier=lambda _root, _version: (True, "proved")
    )
    before = config.read_bytes()
    with pytest.raises(UpdateError, match="rolled back"):
        manager.install_artifact(
            str(second),
            _digest(second),
            verifier=lambda _root, _version: (False, "doctor failed"),
        )
    command = home / "current" / f"villani{'.exe' if os.name == 'nt' else ''}"
    assert command.read_bytes() == b"runtime-first"
    assert config.read_bytes() == before
    assert manager.status().status == "failed"


def test_package_manifest_rejects_unlisted_archive_content_before_switch(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path / "artifact", b"safe")
    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("unexpected.bin", b"not declared")
    home = tmp_path / "home"
    with pytest.raises(UpdateError, match="differ from the manifest"):
        UpdateManager(home).install_artifact(
            str(artifact),
            _digest(artifact),
            verifier=lambda _root, _version: (True, "unused"),
        )
    assert not (home / "current").exists()


def test_concurrent_update_owner_fails_closed_before_installation_mutation(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path / "artifact", b"safe")
    home = tmp_path / "home"
    home.mkdir()
    (home / "update.lock").write_text(
        json.dumps(
            {
                "schema_version": "villani.update_lock.v1",
                "owner_pid": os.getpid(),
                "token": "active-owner",
                "repositories_modified": False,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(UpdateError, match="another update process is active"):
        UpdateManager(home).install_artifact(
            str(artifact),
            _digest(artifact),
            verifier=lambda _root, _version: (True, "must not run"),
        )
    assert not (home / "current").exists()
    assert not (home / "downloads").exists()


def test_interrupted_switch_is_recovered_fail_closed_on_next_status(tmp_path: Path) -> None:
    first = _artifact(tmp_path / "first", b"first")
    second = _artifact(tmp_path / "second", b"second")
    manager = UpdateManager(tmp_path / "home")
    manager.install_artifact(
        str(first), _digest(first), verifier=lambda _root, _version: (True, "proved")
    )
    state = manager.install_artifact(
        str(second), _digest(second), verifier=lambda _root, _version: (True, "proved")
    )
    assert state.previous_installation
    journal_path, journal = next(
        (path, value)
        for path in manager.transactions.glob("*/transaction.json")
        for value in [json.loads(path.read_text())]
        if value.get("previous_installation") == state.previous_installation
    )
    journal["phase"] = "verifying"
    journal["owner_pid"] = 2_147_483_647
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    recovered = UpdateManager(manager.home).status()
    command = manager.current / f"villani{'.exe' if os.name == 'nt' else ''}"
    assert recovered.status == "failed"
    assert "interrupted update was rolled back" in (recovered.error or "").lower()
    assert command.read_bytes() == b"runtime-first"


def test_support_bundle_preview_and_archive_are_opt_in_and_privacy_preserving(
    tmp_path: Path,
) -> None:
    home = tmp_path / "Users" / "secret-user" / "villani-home"
    selected = home / "runs" / "run_selected"
    unselected = home / "runs" / "run_unselected"
    selected.mkdir(parents=True)
    unselected.mkdir(parents=True)
    (selected / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "run_selected",
                "status": "ACCEPTED",
                "repository": "private-repository",
                "repository_path": str(tmp_path / "private-repository"),
                "task": "secret prompt",
                "diff": "+ proprietary source",
                "api_key": "sk-not-a-real-key-but-sensitive",
            }
        ),
        encoding="utf-8",
    )
    (unselected / "manifest.json").write_text(
        json.dumps({"run_id": "run_unselected", "status": "FAILED"}),
        encoding="utf-8",
    )
    builder = SupportBundleBuilder(home)
    doctor = {
        "evidence_path": str(home / "diagnostics"),
        "healthy": False,
        "windows_message": (
            "Active installation: C:\\Users\\secret-user\\My Projects\\"
            "private-repository\\artifact-home\\current"
        ),
        "posix_message": (
            "Repository at /Users/secret-user/My Projects/private-repository/current"
        ),
    }
    preview = builder.preview(
        run_ids=["run_selected"],
        doctor=doctor,
    )
    assert preview.preview is True
    assert preview.uploaded is False
    assert preview.explicit_run_ids == ["run_selected"]
    assert any(not item.included for item in preview.items)

    archive, manifest = builder.create(
        run_ids=["run_selected"],
        doctor=doctor,
    )
    assert manifest.archive_sha256 == _digest(archive)
    assert manifest.prompts_included is False and manifest.source_included is False
    with zipfile.ZipFile(archive) as value:
        names = set(value.namelist())
        payload = b"\n".join(value.read(name) for name in names).decode("utf-8", errors="replace")
    assert "runs/run_selected/manifest.json" in names
    assert "run_unselected" not in payload
    for sensitive in (
        "private-repository",
        "secret prompt",
        "proprietary source",
        "sk-not-a-real-key",
        "secret-user",
        str(tmp_path),
        "My Projects",
        "artifact-home",
    ):
        assert sensitive not in payload


def test_cleanup_is_dry_run_by_default_and_never_targets_runs_or_configuration(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    old = home / "downloads" / "old.zip"
    run = home / "runs" / "run_1" / "manifest.json"
    config = home / "config.yaml"
    old.parent.mkdir(parents=True)
    run.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    run.write_text("{}", encoding="utf-8")
    config.write_text("config_version: 1\n", encoding="utf-8")
    timestamp = time.time() - timedelta(days=31).total_seconds()
    os.utime(old, (timestamp, timestamp))

    preview = cleanup(home)
    assert preview.applied is False and old.is_file()
    applied = cleanup(home, apply=True, now=datetime.now(timezone.utc))
    assert applied.applied is True and not old.exists()
    assert run.is_file() and config.is_file()
    assert applied.runs_deleted == 0 and applied.repositories_modified is False
