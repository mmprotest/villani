from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
RELEASE_VERIFICATION = ROOT / "release-verification"


def _load(name: str, filename: str) -> ModuleType:
    path = RELEASE_VERIFICATION / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_release_scanners_fail_closed_when_required_tools_are_unavailable() -> None:
    supply_chain = _load("release_supply_chain_contract", "supply_chain.py")
    scanners = {
        "python_vulnerability_scan": {"status": "passed"},
        "node_vulnerability_scan": {"status": "passed"},
        "repository_secret_scan": {"status": "unavailable"},
        "external_sbom": {"status": "unavailable"},
        "container_vulnerability_scan": {"status": "unavailable"},
    }

    ci = supply_chain.evaluate_external_scanners("ci", scanners)
    release = supply_chain.evaluate_external_scanners("release", scanners)

    assert ci["passed"] is True
    assert release["passed"] is False
    assert release["unavailable_required_scanners"] == [
        "container_vulnerability_scan",
        "external_sbom",
        "repository_secret_scan",
    ]
    assert (
        release["failed_required_scanners"] == release["unavailable_required_scanners"]
    )


def test_missing_required_scanner_is_a_release_failure() -> None:
    supply_chain = _load("release_supply_chain_missing_contract", "supply_chain.py")
    result = supply_chain.evaluate_external_scanners(
        "ci", {"python_vulnerability_scan": {"status": "passed"}}
    )

    assert result["passed"] is False
    assert result["missing_required_scanners"] == ["node_vulnerability_scan"]
    assert result["failed_required_scanners"] == ["node_vulnerability_scan"]


def test_scanner_capture_tolerates_non_console_output_bytes(tmp_path: Path) -> None:
    supply_chain = _load("release_supply_chain_encoding", "supply_chain.py")

    result = supply_chain._capture(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes([0x81]))",
        ],
        cwd=tmp_path,
    )

    assert result["status"] == "passed"
    assert result["stdout"] == "\ufffd"


def test_source_manifest_excludes_flight_recorder_runtime_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supply_chain = _load("release_supply_chain_source_scope", "supply_chain.py")
    (tmp_path / "source.txt").write_text("release source", encoding="utf-8")
    runtime = tmp_path / "component" / ".villani-flight-recorder" / "replays"
    runtime.mkdir(parents=True)
    (runtime / "generated.html").write_text("runtime secret", encoding="utf-8")
    monkeypatch.setattr(supply_chain, "ROOT", tmp_path)
    monkeypatch.setattr(supply_chain.shutil, "which", lambda _: None)

    manifest = supply_chain._source_manifest()
    staged = tmp_path / "staged"
    supply_chain._stage_source_manifest(manifest, staged)

    assert [item["path"] for item in manifest["files"]] == ["source.txt"]
    assert (staged / "source.txt").read_text(encoding="utf-8") == "release source"
    assert not (staged / "component" / ".villani-flight-recorder").exists()


def test_zero_synchronized_runs_and_missing_screenshots_cannot_pass() -> None:
    sys.path.insert(0, str(RELEASE_VERIFICATION))
    try:
        gate = _load("release_gate_contract", "run_release_gate.py")
    finally:
        sys.path.remove(str(RELEASE_VERIFICATION))
    connected = {
        "status": "passed",
        "scenario_count": 8,
        "passed_scenarios": 8,
        "synchronized_run_count": 0,
        "dead_letter_count": 0,
    }

    with pytest.raises(RuntimeError, match="zero synchronized runs"):
        gate._validate_connected_summary(connected, {"count": 0})
    with pytest.raises(RuntimeError, match="screenshot set mismatch"):
        gate._validate_screenshots(
            {
                "screenshots": [],
                "screenshot_count": 0,
                "viewport_coverage": [],
            }
        )


def test_responsive_screenshot_dimensions_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(RELEASE_VERIFICATION))
    try:
        gate = _load("release_gate_screenshot_dimensions", "run_release_gate.py")
    finally:
        sys.path.remove(str(RELEASE_VERIFICATION))
    monkeypatch.setattr(gate, "LATEST", tmp_path)
    screenshot_dir = tmp_path / "screenshots"
    screenshot_dir.mkdir()
    names = [
        "01-villani-web-overview.png",
        "02-runs-list.png",
        "03-easy-successful-run.png",
        "04-escalated-run-overview.png",
        "05-candidate-comparison.png",
        "06-verification-evidence.png",
        "07-classification-adjustment.png",
        "08-redaction-withheld-artifact.png",
        "09-heuristic-only-failed-run.png",
        "10-flight-recorder-overview.png",
        "11-replay-timeline.png",
        "12-event-stream.png",
        "13-evidence-panel.png",
        "14-file-activity.png",
        "15-flight-candidate-comparison.png",
        "16-overview-1280x800.png",
        "17-overview-1920x1080.png",
    ]
    screenshots = []
    for name in names:
        width, height = (1280, 720) if name.startswith("16-") else (1920, 1080)
        contents = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + width.to_bytes(4, "big")
            + height.to_bytes(4, "big")
        )
        path = screenshot_dir / name
        path.write_bytes(contents)
        screenshots.append(
            {
                "name": name,
                "sha256": hashlib.sha256(contents).hexdigest(),
                "width": width,
                "height": height,
            }
        )

    with pytest.raises(RuntimeError, match="dimensions are 1280x720"):
        gate._validate_screenshots(
            {
                "screenshots": screenshots,
                "screenshot_count": 17,
                "viewport_coverage": ["1280x800", "1440x900", "1920x1080"],
            }
        )


def test_isolated_source_copy_removes_dependencies_builds_and_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(RELEASE_VERIFICATION))
    try:
        gate = _load("release_gate_isolated_source", "run_release_gate.py")
    finally:
        sys.path.remove(str(RELEASE_VERIFICATION))
    source = tmp_path / "source"
    output = tmp_path / "evidence"
    destination = tmp_path / "isolated"
    files = {
        "components/villani-web/src/app.ts": "export const app = true;",
        "components/villani-web/node_modules/leak/index.js": "leak",
        "components/villani-web/dist/index.html": "generated",
        "components/villani-flight-recorder/.cache/value": "cache",
        ".venv/pyvenv.cfg": "environment",
        "release-verification/artifacts/latest/report.json": "{}",
        "state.sqlite3": "database",
    }
    for relative, contents in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    listing = "\0".join(files) + "\0"
    monkeypatch.setattr(gate, "LATEST", output)
    monkeypatch.setattr(
        gate,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=listing, stderr=""
        ),
    )

    report = gate.create_isolated_source(source, destination)

    assert report["status"] == "passed"
    assert report["copied_file_count"] == 1
    assert (destination / "components/villani-web/src/app.ts").is_file()
    assert not (destination / "components/villani-web/node_modules").exists()
    assert not (destination / "components/villani-web/dist").exists()
    assert not (destination / ".venv").exists()
    assert not (destination / "state.sqlite3").exists()


def test_node_application_boundaries_use_only_declared_shared_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(RELEASE_VERIFICATION))
    try:
        gate = _load("release_gate_node_boundaries", "run_release_gate.py")
    finally:
        sys.path.remove(str(RELEASE_VERIFICATION))
    monkeypatch.setattr(gate, "LATEST", tmp_path / "evidence")
    for name in gate.NODE_COMPONENTS:
        component = tmp_path / "components" / name
        (component / "src").mkdir(parents=True)
        package_name = {
            "villani-ui": "@villani/ui",
            "villani-run-model": "@villani/run-model",
        }.get(name, name)
        dependencies = {}
        if name in {"villani-web", "villani-flight-recorder"}:
            dependencies = {
                "@villani/run-model": "file:../villani-run-model",
                "@villani/ui": "file:../villani-ui",
            }
        (component / "package.json").write_text(
            json.dumps(
                {
                    "name": package_name,
                    "version": "0.1.0",
                    "dependencies": dependencies,
                }
            ),
            encoding="utf-8",
        )
        (component / "src/index.ts").write_text(
            'export const value = "ok";\n', encoding="utf-8"
        )
    web_source = tmp_path / "components/villani-web/src/index.ts"
    web_source.write_text(
        'import "../../villani-flight-recorder/src/index";\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="leaves package boundary"):
        gate.validate_node_boundaries(tmp_path)

    web_source.write_text(
        'import { canonicalRunSnapshot } from "@villani/run-model";\n'
        "export { canonicalRunSnapshot };\n",
        encoding="utf-8",
    )
    report = gate.validate_node_boundaries(tmp_path)

    assert report["status"] == "passed"
    assert report["violations"] == []


def test_real_node_sources_have_no_cross_application_raw_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(RELEASE_VERIFICATION))
    try:
        gate = _load("release_gate_real_node_boundaries", "run_release_gate.py")
    finally:
        sys.path.remove(str(RELEASE_VERIFICATION))
    monkeypatch.setattr(gate, "LATEST", tmp_path)

    report = gate.validate_node_boundaries(ROOT)

    assert report["status"] == "passed"
    applications = {item["component"]: item for item in report["packages"]}
    assert "@villani/run-model" in applications["villani-web"]["declared_dependencies"]
    assert (
        "@villani/run-model"
        in applications["villani-flight-recorder"]["declared_dependencies"]
    )


def test_sibling_node_modules_are_removed_before_each_application_build(
    tmp_path: Path,
) -> None:
    sys.path.insert(0, str(RELEASE_VERIFICATION))
    try:
        gate = _load("release_gate_sibling_dependencies", "run_release_gate.py")
    finally:
        sys.path.remove(str(RELEASE_VERIFICATION))
    for name in gate.NODE_COMPONENTS:
        (tmp_path / "components" / name / "node_modules").mkdir(parents=True)

    for application in ("villani-web", "villani-flight-recorder"):
        for path in gate._node_modules_paths(tmp_path):
            if path.parent.name != application:
                shutil.rmtree(path)
        gate._assert_no_sibling_node_modules(tmp_path, application)
        assert gate._node_modules_paths(tmp_path) == [
            tmp_path / "components" / application / "node_modules"
        ]
        shutil.rmtree(tmp_path / "components" / application / "node_modules")
        for name in gate.NODE_COMPONENTS:
            if name != application:
                (tmp_path / "components" / name / "node_modules").mkdir(
                    parents=True, exist_ok=True
                )
