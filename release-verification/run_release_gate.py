#!/usr/bin/env python3
"""Cross-platform, fail-closed Villani packaged release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import venv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from supply_chain import generate as generate_supply_chain

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "release-verification" / "artifacts" / "latest"
PYTHON_COMPONENTS = (
    "villani-ops",
    "villani-code",
    "villani-agentd",
    "villani-control-plane",
    "villani",
)
NODE_COMPONENTS = (
    "villani-ui",
    "villani-run-model",
    "villani-web",
    "villani-flight-recorder",
)
ASSET_RE = re.compile(r"(?:src|href)=[\"']([^\"'#?]+)")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    command: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None
) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "$ "
        + subprocess.list2cmdline(command)
        + "\n"
        + completed.stdout
        + completed.stderr,
        encoding="utf-8",
    )
    if completed.returncode:
        raise RuntimeError(f"command failed ({completed.returncode}); see {log}")


def component_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in PYTHON_COMPONENTS:
        document = tomllib.loads(
            (ROOT / "components" / name / "pyproject.toml").read_text(encoding="utf-8")
        )
        result[str(document["project"]["name"])] = str(document["project"]["version"])
    for name in NODE_COMPONENTS:
        document = json.loads(
            (ROOT / "components" / name / "package.json").read_text(encoding="utf-8")
        )
        result[str(document["name"])] = str(document["version"])
    result["shared-protocol"] = "2"
    return result


def validate_compatibility(versions: dict[str, str]) -> dict[str, Any]:
    template = json.loads(
        (ROOT / "release/component-compatibility.json").read_text(encoding="utf-8")
    )
    expected = template["components"]
    mismatches = {
        name: {"manifest": expected.get(name), "package": version}
        for name, version in versions.items()
        if expected.get(name) != version
    }
    if set(expected) != set(versions):
        mismatches["component_set"] = {
            "manifest_only": sorted(set(expected) - set(versions)),
            "packages_only": sorted(set(versions) - set(expected)),
        }
    if (
        template["spool_schema_version"] != 4
        or template["alembic_head"] != "0a1b2c3d4e5f"
    ):
        mismatches["wire_contract"] = "unexpected spool version or Alembic head"
    minimum_python = tuple(
        int(value) for value in str(template["minimum_python"]).split(".")
    )
    if sys.version_info[:2] < minimum_python:
        mismatches["python"] = {
            "minimum": template["minimum_python"],
            "actual": platform.python_version(),
        }
    node = shutil.which("node")
    if not node:
        mismatches["node"] = "node executable is unavailable"
    else:
        actual_node = (
            subprocess.run(
                [node, "--version"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=True,
            )
            .stdout.strip()
            .lstrip("v")
        )
        if int(actual_node.split(".", 1)[0]) < int(template["node"]):
            mismatches["node"] = {"minimum": template["node"], "actual": actual_node}
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(
        platform.system()
    )
    if system not in template["supported_operating_systems"]:
        mismatches["platform"] = system or platform.system()
    if mismatches:
        raise RuntimeError(f"component compatibility mismatch: {mismatches}")
    return template


def validate_frontend_assets() -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for application in ("villani-web",):
        root = ROOT / "components" / application / "dist"
        html = root / "index.html"
        if not html.is_file():
            raise RuntimeError(f"{application} dist/index.html is missing")
        references = []
        for reference in ASSET_RE.findall(html.read_text(encoding="utf-8")):
            if reference.startswith(("http:", "https:", "data:", "mailto:")):
                continue
            target = root / reference.lstrip("/")
            references.append({"reference": reference, "exists": target.is_file()})
            if not target.is_file():
                raise RuntimeError(f"{html} references missing asset {reference}")
        reports.append(
            {"application": application, "html": str(html), "references": references}
        )
    return {"passed": True, "applications": reports}


def _stage_node_package(component: Path, stage: Path) -> None:
    """Create publishable package input without mutating the source manifest."""
    document = json.loads((component / "package.json").read_text(encoding="utf-8"))
    for section in ("dependencies", "optionalDependencies", "peerDependencies"):
        dependencies = document.get(section, {})
        for dependency, specification in list(dependencies.items()):
            if not isinstance(specification, str) or not specification.startswith(
                "file:"
            ):
                continue
            target = (component / specification.removeprefix("file:")).resolve()
            target_document = json.loads(
                (target / "package.json").read_text(encoding="utf-8")
            )
            if target_document.get("name") != dependency:
                raise RuntimeError(f"local Node dependency name mismatch: {dependency}")
            dependencies[dependency] = str(target_document["version"])
    files = list(document.get("files", []))
    if not files:
        files = [name for name in ("dist", "dist-model") if (component / name).exists()]
        document["files"] = files
    stage.mkdir(parents=True, exist_ok=True)
    write_json(stage / "package.json", document)
    for name in files:
        source = component / name
        destination = stage / name
        if not source.exists():
            raise RuntimeError(f"declared Node package content is missing: {source}")
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    for name in ("README.md", "LICENSE", "LICENSE.md"):
        if (component / name).is_file():
            shutil.copy2(component / name, stage / name)


def install_packed_node_packages(
    work: Path, package_dir: Path, env: dict[str, str]
) -> dict[str, Any]:
    consumer = work / "node-consumer"
    consumer.mkdir(parents=True)
    write_json(
        consumer / "package.json", {"name": "villani-release-consumer", "private": True}
    )
    archives = sorted(package_dir.glob("*.tgz"))
    npm = "npm.cmd" if os.name == "nt" else "npm"
    run(
        [
            npm,
            "install",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            *map(str, archives),
        ],
        cwd=consumer,
        log=LATEST / "logs/node-package-install.log",
        env=env,
    )
    run(
        [
            shutil.which("node") or "node",
            "--input-type=module",
            "--eval",
            "await import('@villani/run-model'); await import('@villani/ui');",
        ],
        cwd=consumer,
        log=LATEST / "logs/node-package-import.log",
        env=env,
    )
    run(
        [
            shutil.which("node") or "node",
            str(
                consumer
                / "node_modules"
                / "villani-flight-recorder"
                / "dist"
                / "cli.js"
            ),
            "--help",
        ],
        cwd=consumer,
        log=LATEST / "logs/node-package-cli.log",
        env=env,
    )
    web_root = consumer / "node_modules" / "villani-web" / "dist"
    web_html = web_root / "index.html"
    if not web_html.is_file():
        raise RuntimeError("installed packed villani-web is missing dist/index.html")
    references = []
    for reference in ASSET_RE.findall(web_html.read_text(encoding="utf-8")):
        if reference.startswith(("http:", "https:", "data:", "mailto:")):
            continue
        exists = (web_root / reference.lstrip("/")).is_file()
        references.append({"reference": reference, "exists": exists})
        if not exists:
            raise RuntimeError(
                f"installed packed villani-web references missing asset {reference}"
            )
    return {
        "status": "passed",
        "packages": [path.name for path in archives],
        "web_assets": references,
    }


def build_packages(work: Path) -> tuple[list[Path], dict[str, Any]]:
    package_dir = LATEST / "packages"
    package_dir.mkdir(parents=True, exist_ok=True)
    logs = LATEST / "logs"
    built: list[Path] = []
    npm_environment = os.environ.copy()
    npm_environment["npm_config_cache"] = str(work / "npm-cache")
    npm = "npm.cmd" if os.name == "nt" else "npm"
    for name in NODE_COMPONENTS:
        cwd = ROOT / "components" / name
        if (cwd / "package-lock.json").is_file():
            run(
                [npm, "ci", "--no-audit", "--no-fund"],
                cwd=cwd,
                log=logs / f"{name}-install.log",
                env=npm_environment,
            )
        run(
            [npm, "run", "build"],
            cwd=cwd,
            log=logs / f"{name}-build.log",
            env=npm_environment,
        )
        if name == "villani-web":
            run(
                [npm, "exec", "playwright", "install", "chromium"],
                cwd=cwd,
                log=logs / "playwright-browser-install.log",
                env=npm_environment,
            )
        stage = work / "node-stage" / name
        _stage_node_package(cwd, stage)
        run(
            [npm, "pack", "--ignore-scripts", "--pack-destination", str(package_dir)],
            cwd=stage,
            log=logs / f"{name}-pack.log",
            env=npm_environment,
        )
    asset_report = validate_frontend_assets()
    asset_report["packed_node_install"] = install_packed_node_packages(
        work, package_dir, npm_environment
    )
    for name in PYTHON_COMPONENTS:
        output = work / "python" / name
        output.mkdir(parents=True, exist_ok=True)
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--sdist",
                "--outdir",
                str(output),
            ],
            cwd=ROOT / "components" / name,
            log=logs / f"{name}-build.log",
        )
        for artifact in output.iterdir():
            destination = package_dir / artifact.name
            shutil.copy2(artifact, destination)
            built.append(destination)
    built.extend(sorted(package_dir.glob("*.tgz")))
    return built, asset_report


def install_wheels(work: Path, packages: list[Path]) -> Path:
    environment = work / "installed"
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run(
        [str(python), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=ROOT,
        log=LATEST / "logs/wheel-environment-pip-upgrade.log",
    )
    wheels = [str(path) for path in packages if path.suffix == ".whl"]
    run(
        [str(python), "-m", "pip", "install", *wheels],
        cwd=ROOT,
        log=LATEST / "logs/wheel-install.log",
    )
    for command in ("villani", "villani-code", "villani-agentd"):
        executable = environment / (
            f"Scripts/{command}.exe" if os.name == "nt" else f"bin/{command}"
        )
        if not executable.is_file():
            raise RuntimeError(f"installed entry point is missing: {command}")
    return python


def evidence_skeleton(mode: str) -> dict[str, Any]:
    incomplete = {
        "status": "not_executed",
        "reason": "connected scenario harness not completed",
    }
    for name in (
        "redaction-proof.json",
        "canonical-reconciliation.json",
        "dead-letter-summary.json",
        "browser-summary.json",
        "security-summary.json",
        "test-summary.json",
        "postgres-migration-summary.json",
        "verifier-routing-summary.json",
        "candidate-diversity-summary.json",
        "classification-adjustment-summary.json",
    ):
        write_json(LATEST / name, incomplete)
    for directory in (
        "screenshots",
        "control-plane-api-snapshots",
        "canonical-run-snapshots",
        "logs",
        "packages",
    ):
        (LATEST / directory).mkdir(parents=True, exist_ok=True)
    return {
        "mode": mode,
        "connected": incomplete,
        "browser": incomplete,
        "reconciliation": incomplete,
    }


def _summary(name: str) -> dict[str, Any]:
    path = LATEST / name
    if not path.is_file():
        raise RuntimeError(f"required release evidence is missing: {name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"required release evidence is not an object: {name}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _validate_connected_summary(
    connected: dict[str, Any], dead_letters: dict[str, Any]
) -> None:
    _require(connected.get("status") == "passed", "connected packaged scenarios failed")
    _require(
        connected.get("scenario_count") == 8 and connected.get("passed_scenarios") == 8,
        "all eight connected scenarios did not pass",
    )
    _require(
        connected.get("synchronized_run_count", 0) > 0,
        "zero synchronized runs cannot pass the release gate",
    )
    _require(
        connected.get("dead_letter_count") == 0 and dead_letters.get("count") == 0,
        "unexpected dead letters exist",
    )


def _validate_screenshots(browser: dict[str, Any]) -> None:
    required = {
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
    }
    documented = {str(item.get("name")) for item in browser.get("screenshots", [])}
    _require(
        required == documented,
        f"browser screenshot set mismatch: missing={sorted(required - documented)} extra={sorted(documented - required)}",
    )
    expected_dimensions = {
        "16-overview-1280x800.png": (1280, 800),
        "17-overview-1920x1080.png": (1920, 1080),
    }
    for item in browser["screenshots"]:
        path = LATEST / "screenshots" / item["name"]
        _require(
            path.is_file() and path.stat().st_size > 0,
            f"browser screenshot is missing or empty: {item['name']}",
        )
        _require(
            sha256(path) == item.get("sha256"),
            f"browser screenshot hash mismatch: {item['name']}",
        )
        contents = path.read_bytes()[:24]
        _require(
            len(contents) == 24 and contents[1:4] == b"PNG",
            f"browser screenshot is not PNG: {item['name']}",
        )
        dimensions = (
            int.from_bytes(contents[16:20], "big"),
            int.from_bytes(contents[20:24], "big"),
        )
        _require(
            item.get("width") == dimensions[0] and item.get("height") == dimensions[1],
            f"browser screenshot metadata mismatch: {item['name']}",
        )
        if item["name"] in expected_dimensions:
            _require(
                dimensions == expected_dimensions[item["name"]],
                f"browser screenshot dimensions are {dimensions[0]}x{dimensions[1]} for {item['name']}",
            )
    _require(
        browser.get("screenshot_count") == len(required),
        "browser screenshot count is not 17",
    )
    _require(
        set(browser.get("viewport_coverage", []))
        == {"1280x800", "1440x900", "1920x1080"},
        "browser viewport coverage is incomplete",
    )


def _test_summary(connected: dict[str, Any], browser: dict[str, Any]) -> dict[str, Any]:
    scenarios = connected.get("scenarios", [])
    assertion_count = sum(len(item.get("assertions", {})) for item in scenarios)
    passed_assertions = sum(
        sum(value is True for value in item.get("assertions", {}).values())
        for item in scenarios
    )
    return {
        "status": "passed",
        "scope": "packaged connected release gate",
        "scenario_count": connected.get("scenario_count", 0),
        "passed_scenarios": connected.get("passed_scenarios", 0),
        "failed_scenarios": connected.get("scenario_count", 0)
        - connected.get("passed_scenarios", 0),
        "scenario_assertions": {
            "total": assertion_count,
            "passed": passed_assertions,
            "failed": assertion_count - passed_assertions,
        },
        "browser_assertions": browser.get("assertions", {}),
        "browser_screenshot_count": browser.get("screenshot_count", 0),
        "note": "Component and full-suite results are enforced by their dedicated CI jobs; this file records the packaged connected gate only.",
    }


def _markdown(report: dict[str, Any]) -> str:
    verdict = report["release_verdict"]
    return (
        "# Villani release gate\n\n"
        f"Verdict: **{verdict}**\n\n"
        f"Mode: `{report['mode']}`\n\n"
        f"Scenarios: {report.get('passed_scenario_count', 0)}/{report.get('scenario_count', 0)} passed  \n"
        f"Synchronized runs: {report.get('synchronized_run_count', 0)}  \n"
        f"Dead letters: {report.get('dead_letter_count', 0)}  \n"
        f"API reconciliation: {report.get('api_reconciliation_status')}  \n"
        f"Villani Web reconciliation: {report.get('villani_web_reconciliation_status')}  \n"
        f"Flight Recorder reconciliation: {report.get('flight_recorder_reconciliation_status')}  \n"
        f"Browser: {report.get('browser_result')}  \n"
        f"Security: {report.get('security_scan_status')}\n\n"
        + (f"Failure: {report['failure']}\n\n" if report.get("failure") else "")
        + str(report.get("certification_note", ""))
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "ci", "release"), default="ci")
    args = parser.parse_args(argv)
    if LATEST.exists():
        shutil.rmtree(LATEST)
    LATEST.mkdir(parents=True)
    evidence_skeleton(args.mode)
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "schema_version": "villani.release_gate.v1",
        "mode": args.mode,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "release_verdict": "RELEASE GATE FAILED",
        "phases": {},
        "synchronized_run_count": 0,
        "completed_run_count": 0,
        "exhausted_run_count": 0,
        "dead_letter_count": 0,
        "redacted_field_count": 0,
        "withheld_artifact_count": 0,
        "api_reconciliation_status": "not_executed",
        "villani_web_reconciliation_status": "not_executed",
        "flight_recorder_reconciliation_status": "not_executed",
        "browser_result": "not_executed",
        "security_scan_status": "not_executed",
    }
    exit_code = 1
    try:
        versions = component_versions()
        template = validate_compatibility(versions)
        with tempfile.TemporaryDirectory(prefix="villani-release-gate-") as temporary:
            work = Path(temporary)
            packages, assets = build_packages(work)
            installed_python = install_wheels(work, packages)
            hashes = {path.name: sha256(path) for path in sorted(packages)}
            generated = json.loads(json.dumps(template))
            generated["generated"] = {
                "build_timestamp": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "package_hashes": hashes,
                "python": platform.python_version(),
                "node": subprocess.run(
                    [shutil.which("node") or "node", "--version"],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=True,
                ).stdout.strip(),
                "platform": platform.platform(),
            }
            write_json(LATEST / "component-versions.json", versions)
            write_json(LATEST / "component-compatibility.json", generated)
            write_json(LATEST / "package-hashes.json", hashes)
            write_json(LATEST / "frontend-asset-validation.json", assets)
            write_json(
                LATEST / "build-manifest.json",
                {
                    "status": "passed",
                    "packages": [
                        {
                            "name": name,
                            "sha256": digest,
                            "size": (LATEST / "packages" / name).stat().st_size,
                        }
                        for name, digest in sorted(hashes.items())
                    ],
                    "clean_wheel_install": "passed",
                    "clean_packed_node_install": assets["packed_node_install"][
                        "status"
                    ],
                    "editable_installs": False,
                    "node_packages_packed": len(
                        [name for name in hashes if name.endswith(".tgz")]
                    ),
                    "python_wheels": len(
                        [name for name in hashes if name.endswith(".whl")]
                    ),
                    "python_source_distributions": len(
                        [name for name in hashes if name.endswith(".tar.gz")]
                    ),
                },
            )
            report["package_versions"] = versions
            report["package_hashes"] = hashes
            report["build_result"] = "passed"
            report["phases"]["build"] = "passed"
            connected_work = work / "connected"
            connected_work.mkdir()
            run(
                [
                    str(installed_python),
                    str(ROOT / "release-verification" / "connected_product.py"),
                    "--python",
                    str(installed_python),
                    "--work",
                    str(connected_work),
                    "--artifacts",
                    str(LATEST),
                    "--mode",
                    args.mode,
                ],
                cwd=ROOT,
                log=LATEST / "logs/connected-product.log",
                env=os.environ.copy(),
            )
            connected = json.loads(
                (LATEST / "connected-product-summary.json").read_text(encoding="utf-8")
            )
            reconciliation = json.loads(
                (LATEST / "canonical-reconciliation.json").read_text(encoding="utf-8")
            )
            browser = _summary("browser-summary.json")
            redaction = _summary("redaction-proof.json")
            dead_letters = _summary("dead-letter-summary.json")
            postgres = _summary("postgres-migration-summary.json")
            verifier = _summary("verifier-routing-summary.json")
            diversity = _summary("candidate-diversity-summary.json")
            classification = _summary("classification-adjustment-summary.json")
            report["phases"]["connected"] = connected["status"]
            report["phases"]["reconciliation"] = reconciliation["status"]
            report["synchronized_run_count"] = connected["synchronized_run_count"]
            report["completed_run_count"] = connected["completed_run_count"]
            report["exhausted_run_count"] = connected["exhausted_run_count"]
            report["dead_letter_count"] = connected["dead_letter_count"]
            report["redacted_field_count"] = connected["redacted_field_count"]
            report["withheld_artifact_count"] = connected["withheld_artifact_count"]
            report["scenario_count"] = connected["scenario_count"]
            report["passed_scenario_count"] = connected["passed_scenarios"]
            report["failed_scenario_count"] = (
                connected["scenario_count"] - connected["passed_scenarios"]
            )
            report["api_reconciliation_status"] = reconciliation["status"]
            report["villani_web_reconciliation_status"] = browser.get(
                "villani_web_reconciliation", "failed"
            )
            report["flight_recorder_reconciliation_status"] = browser.get(
                "flight_recorder_reconciliation", "failed"
            )
            report["browser_result"] = browser.get("status", "failed")
            report["alembic_head"] = postgres.get("alembic_head")
            report["spool_schema_version"] = template["spool_schema_version"]
            report["verifier_routing_result"] = verifier.get("status")
            report["candidate_diversity_result"] = diversity.get("status")
            report["classification_adjustment_result"] = classification.get("status")
            _validate_connected_summary(connected, dead_letters)
            _require(
                reconciliation.get("status") == "passed",
                "canonical six-source reconciliation failed",
            )
            _require(browser.get("status") == "passed", "connected browser gate failed")
            _require(
                browser.get("villani_web_reconciliation") == "passed",
                "Villani Web reconciliation failed",
            )
            _require(
                browser.get("flight_recorder_reconciliation") == "passed",
                "Flight Recorder reconciliation failed",
            )
            _validate_screenshots(browser)
            _require(
                redaction.get("status") == "passed"
                and redaction.get("registered_secret_absent") is True,
                "redaction or artifact-withholding proof failed",
            )
            _require(
                postgres.get("status") == "passed"
                and postgres.get("alembic_head") == template["alembic_head"],
                "PostgreSQL migration proof failed",
            )
            _require(
                postgres.get("fresh_database_upgrade") == "passed"
                and all(postgres.get("checks", {}).values()),
                "populated pre-composite PostgreSQL proof is incomplete",
            )
            _require(
                verifier.get("status") == "passed", "verifier-routing proof failed"
            )
            _require(
                diversity.get("status") == "passed"
                and diversity.get("counted_diversity") == 2,
                "candidate-diversity proof failed",
            )
            _require(
                classification.get("status") == "passed",
                "classification-adjustment proof failed",
            )
            report["phases"].update(
                {
                    "browser": "passed",
                    "redaction": "passed",
                    "postgresql": "passed",
                    "verifier_routing": "passed",
                    "candidate_diversity": "passed",
                    "classification_adjustment": "passed",
                }
            )
            security = generate_supply_chain(
                mode=args.mode,
                installed_python=installed_python,
                packages=packages,
                output=LATEST,
                package_hashes=hashes,
            )
            report["security_scan_status"] = security["status"]
            report["official_release_certification"] = security[
                "official_release_certification"
            ]
            report["certification_note"] = security["certification_note"]
            report["phases"]["security"] = security["status"]
            _require(
                security["status"] == "passed",
                "required supply-chain scanner or deterministic security check failed",
            )
            tests = _test_summary(connected, browser)
            write_json(LATEST / "test-summary.json", tests)
            report["test_summary"] = tests
            report["phases"]["evidence"] = "passed"
            report["release_verdict"] = "RELEASE GATE PASSED"
            exit_code = 0
    except Exception as error:
        report["failure"] = str(error)
    report["finished_at"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    write_json(LATEST / "release-gate-report.json", report)
    (LATEST / "release-gate-report.md").write_text(_markdown(report), encoding="utf-8")
    print(LATEST / "release-gate-report.json")
    print(report["release_verdict"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
