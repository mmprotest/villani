#!/usr/bin/env python3
"""Cross-platform, fail-closed Villani packaged release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "release-verification" / "artifacts" / "latest"
PYTHON_COMPONENTS = (
    "villani-ops", "villani-code", "villani-agentd", "villani-control-plane", "villani"
)
NODE_COMPONENTS = ("villani-ui", "villani-run-model", "villani-web", "villani-flight-recorder")
ASSET_RE = re.compile(r"(?:src|href)=[\"']([^\"'#?]+)")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path, log: Path, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "$ " + subprocess.list2cmdline(command) + "\n" + completed.stdout + completed.stderr,
        encoding="utf-8",
    )
    if completed.returncode:
        raise RuntimeError(f"command failed ({completed.returncode}); see {log}")


def component_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in PYTHON_COMPONENTS:
        document = tomllib.loads((ROOT / "components" / name / "pyproject.toml").read_text(encoding="utf-8"))
        result[str(document["project"]["name"])] = str(document["project"]["version"])
    for name in NODE_COMPONENTS:
        document = json.loads((ROOT / "components" / name / "package.json").read_text(encoding="utf-8"))
        result[str(document["name"])] = str(document["version"])
    return result


def validate_compatibility(versions: dict[str, str]) -> dict[str, Any]:
    template = json.loads((ROOT / "release/component-compatibility.json").read_text(encoding="utf-8"))
    expected = template["components"]
    mismatches = {
        name: {"manifest": expected.get(name), "package": version}
        for name, version in versions.items()
        if expected.get(name) != version
    }
    if template["spool_schema_version"] != 4 or template["alembic_head"] != "0a1b2c3d4e5f":
        mismatches["wire_contract"] = "unexpected spool version or Alembic head"
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
        reports.append({"application": application, "html": str(html), "references": references})
    return {"passed": True, "applications": reports}


def build_packages(work: Path) -> tuple[list[Path], dict[str, Any]]:
    package_dir = LATEST / "packages"
    package_dir.mkdir(parents=True, exist_ok=True)
    logs = LATEST / "logs"
    built: list[Path] = []
    for name in NODE_COMPONENTS:
        cwd = ROOT / "components" / name
        run(["npm.cmd" if os.name == "nt" else "npm", "run", "build"], cwd=cwd, log=logs / f"{name}-build.log")
        run(
            ["npm.cmd" if os.name == "nt" else "npm", "pack", "--pack-destination", str(package_dir)],
            cwd=cwd,
            log=logs / f"{name}-pack.log",
        )
    asset_report = validate_frontend_assets()
    for name in PYTHON_COMPONENTS:
        output = work / "python" / name
        output.mkdir(parents=True, exist_ok=True)
        run(
            [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(output)],
            cwd=ROOT / "components" / name,
            log=logs / f"{name}-build.log",
        )
        for artifact in output.iterdir():
            destination = package_dir / artifact.name
            shutil.copy2(artifact, destination)
            built.append(destination)
    built.extend(sorted(package_dir.glob("*.tgz")))
    return built, asset_report


def install_wheels(work: Path, packages: list[Path]) -> None:
    environment = work / "installed"
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheels = [str(path) for path in packages if path.suffix == ".whl"]
    run([str(python), "-m", "pip", "install", *wheels], cwd=ROOT, log=LATEST / "logs/wheel-install.log")
    for command in ("villani", "villani-code", "villani-agentd"):
        executable = environment / (f"Scripts/{command}.exe" if os.name == "nt" else f"bin/{command}")
        if not executable.is_file():
            raise RuntimeError(f"installed entry point is missing: {command}")


def evidence_skeleton(mode: str) -> dict[str, Any]:
    incomplete = {"status": "not_executed", "reason": "connected scenario harness not completed"}
    for name in (
        "redaction-proof.json", "canonical-reconciliation.json", "dead-letter-summary.json",
        "browser-summary.json", "security-summary.json", "test-summary.json"
    ):
        write_json(LATEST / name, incomplete)
    for directory in ("screenshots", "control-plane-api-snapshots", "canonical-run-snapshots", "logs", "packages"):
        (LATEST / directory).mkdir(parents=True, exist_ok=True)
    return {"mode": mode, "connected": incomplete, "browser": incomplete, "reconciliation": incomplete}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "ci", "release"), default="ci")
    args = parser.parse_args(argv)
    if LATEST.exists():
        shutil.rmtree(LATEST)
    LATEST.mkdir(parents=True)
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "schema_version": "villani.release_gate.v1",
        "mode": args.mode,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "release_verdict": "failed",
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
    try:
        versions = component_versions()
        template = validate_compatibility(versions)
        with tempfile.TemporaryDirectory(prefix="villani-release-gate-") as temporary:
            work = Path(temporary)
            packages, assets = build_packages(work)
            install_wheels(work, packages)
        hashes = {path.name: sha256(path) for path in sorted(packages)}
        generated = json.loads(json.dumps(template))
        generated["generated"] = {"build_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "package_hashes": hashes}
        write_json(LATEST / "component-versions.json", versions)
        write_json(LATEST / "component-compatibility.json", generated)
        write_json(LATEST / "package-hashes.json", hashes)
        write_json(LATEST / "frontend-asset-validation.json", assets)
        write_json(LATEST / "build-manifest.json", {"packages": list(hashes), "clean_wheel_install": "passed"})
        report["package_versions"] = versions
        report["package_hashes"] = hashes
        report["build_result"] = "passed"
        report["phases"]["build"] = "passed"
        report["phases"].update(evidence_skeleton(args.mode))
        raise RuntimeError("connected release scenarios, reconciliation, and browser evidence are mandatory and not implemented")
    except Exception as error:
        report["failure"] = str(error)
    report["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    write_json(LATEST / "release-gate-report.json", report)
    (LATEST / "release-gate-report.md").write_text(
        "# Villani release gate\n\nVerdict: **FAILED**\n\n" + str(report.get("failure", "mandatory phase incomplete")) + "\n",
        encoding="utf-8",
    )
    print(LATEST / "release-gate-report.json")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
