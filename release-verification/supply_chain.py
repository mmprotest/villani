#!/usr/bin/env python3
"""Generate fresh, mode-aware Villani supply-chain evidence.

The deterministic checks in this module are intentionally useful without network
access.  External scanners retain an explicit ``unavailable`` state and are only
required by the modes whose policy names them.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_EXTERNAL_SCANNERS = {
    "local": frozenset(),
    "ci": frozenset({"python_vulnerability_scan", "node_vulnerability_scan"}),
    "release": frozenset(
        {
            "python_vulnerability_scan",
            "node_vulnerability_scan",
            "repository_secret_scan",
            "external_sbom",
            "container_vulnerability_scan",
        }
    ),
}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".mjs",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
SECRET_PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "openai_key": re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "github_token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "bearer_token": re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/-]{24,}\b", re.I),
}
FORBIDDEN_LICENSES = ("AGPL", "SSPL", "BUSL", "COMMON CLAUSE")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _capture(command: list[str], *, cwd: Path, timeout: int = 180) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "unavailable", "reason": type(error).__name__}
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-20000:],
        "stderr": (completed.stderr or "")[-20000:],
    }


def _site_packages(python: Path) -> Path:
    command = [
        str(python),
        "-c",
        "import json,site; print(json.dumps(site.getsitepackages()))",
    ]
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    values = json.loads(completed.stdout)
    for value in values:
        path = Path(value)
        if path.is_dir():
            return path
    raise RuntimeError("clean wheel environment has no site-packages directory")


def _python_inventory(python: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    listed = _capture([str(python), "-m", "pip", "list", "--format", "json"], cwd=ROOT)
    if listed["status"] != "passed":
        raise RuntimeError("could not inventory the clean Python environment")
    packages = [
        {"name": str(item["name"]), "version": str(item["version"])}
        for item in json.loads(listed["stdout"])
    ]
    checked = _capture([str(python), "-m", "pip", "check"], cwd=ROOT)
    return sorted(packages, key=lambda item: item["name"].lower()), checked


def _node_inventory() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    packages: dict[tuple[str, str, str], dict[str, Any]] = {}
    manifests: list[dict[str, Any]] = []
    for package_json in sorted((ROOT / "components").glob("*/package.json")):
        document = json.loads(package_json.read_text(encoding="utf-8"))
        if package_json.parent.name not in {
            "villani-ui",
            "villani-run-model",
            "villani-web",
            "villani-flight-recorder",
        }:
            continue
        manifests.append(
            {
                "name": document.get("name"),
                "version": document.get("version"),
                "path": str(package_json.relative_to(ROOT)).replace("\\", "/"),
                "sha256": _sha256(package_json),
            }
        )
        lock_path = package_json.with_name("package-lock.json")
        if not lock_path.is_file():
            continue
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        for lock_name, item in lock.get("packages", {}).items():
            if not lock_name or not isinstance(item, dict):
                continue
            name = lock_name.removeprefix("node_modules/")
            version = str(item.get("version", "unknown"))
            key = (name, version, package_json.parent.name)
            packages[key] = {
                "name": name,
                "version": version,
                "declared_by": package_json.parent.name,
                "license": item.get("license"),
                "optional": bool(item.get("optional", False)),
            }
    return sorted(
        packages.values(), key=lambda item: (item["name"], item["version"])
    ), manifests


def _archive_members(path: Path) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in sorted(archive.infolist(), key=lambda value: value.filename):
                if info.is_dir():
                    continue
                members.append({"path": info.filename, "size": info.file_size})
    else:
        with tarfile.open(path, "r:*") as archive:
            for info in sorted(archive.getmembers(), key=lambda value: value.name):
                if info.isfile():
                    members.append({"path": info.name, "size": info.size})
    return members


def _package_manifests(
    packages: Iterable[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    python: list[dict[str, Any]] = []
    node: list[dict[str, Any]] = []
    for path in sorted(packages):
        document = {
            "artifact": path.name,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
            "files": _archive_members(path),
        }
        (node if path.suffix == ".tgz" else python).append(document)
    return python, node


def _license_inventory(
    site_packages: Path, node: list[dict[str, Any]]
) -> dict[str, Any]:
    python: list[dict[str, Any]] = []
    for metadata in sorted(site_packages.glob("*.dist-info/METADATA")):
        parsed = Parser().parsestr(
            metadata.read_text(encoding="utf-8", errors="replace")
        )
        classifiers = parsed.get_all("Classifier", [])
        classified = [
            value.split("::")[-1].strip()
            for value in classifiers
            if "License ::" in value
        ]
        license_value = (parsed.get("License") or "").strip() or (
            ", ".join(classified) or None
        )
        python.append(
            {
                "name": parsed.get("Name", metadata.parent.name),
                "version": parsed.get("Version", "unknown"),
                "license": license_value,
            }
        )
    combined = python + [
        {
            "name": item["name"],
            "version": item["version"],
            "license": item.get("license"),
        }
        for item in node
    ]
    forbidden = [
        item
        for item in combined
        if item.get("license")
        and any(value in str(item["license"]).upper() for value in FORBIDDEN_LICENSES)
    ]
    return {
        "status": "failed" if forbidden else "passed",
        "policy": {"forbidden_identifiers": list(FORBIDDEN_LICENSES)},
        "python": python,
        "node": [
            {
                "name": item["name"],
                "version": item["version"],
                "license": item.get("license"),
            }
            for item in node
        ],
        "unknown_count": sum(not item.get("license") for item in combined),
        "forbidden": forbidden,
    }


def _iter_archive_bytes(path: Path) -> Iterable[tuple[str, bytes]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if (
                    not info.is_dir()
                    and Path(info.filename).suffix.lower() in TEXT_SUFFIXES
                ):
                    yield info.filename, archive.read(info)
        return
    with tarfile.open(path, "r:*") as archive:
        for info in archive.getmembers():
            file = archive.extractfile(info) if info.isfile() else None
            if file is not None and Path(info.name).suffix.lower() in TEXT_SUFFIXES:
                yield info.name, file.read()


def _package_secret_scan(packages: Iterable[Path]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    scanned_files = 0
    for package in sorted(packages):
        for name, data in _iter_archive_bytes(package):
            scanned_files += 1
            for category, pattern in SECRET_PATTERNS.items():
                if pattern.search(data):
                    findings.append(
                        {"artifact": package.name, "path": name, "category": category}
                    )
    return {
        "status": "failed" if findings else "passed",
        "scanner": "villani-high-confidence-package-secret-scan-v1",
        "scanned_files": scanned_files,
        "findings": findings,
    }


def _source_manifest() -> dict[str, Any]:
    ignored = {
        ".git",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".release-smoke",
        ".agents",
        ".final-hostile",
        ".npm-cache-release",
        ".villani-flight-recorder",
        ".villani-ops",
        "node_modules",
        "__pycache__",
        "artifacts",
        "build",
        "dist",
        "dist-model",
        "playwright-report",
        "test-results",
    }
    paths: list[Path] = []
    git = shutil.which("git")
    if git:
        listed = subprocess.run(
            [
                git,
                "-c",
                "core.quotePath=false",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
        if listed.returncode != 0:
            raise RuntimeError(
                "could not enumerate the release source manifest with Git: "
                + listed.stderr.strip()
            )
        paths = sorted(
            path
            for value in listed.stdout.split("\0")
            if value and (path := ROOT / value).is_file()
        )
    else:
        for directory, directories, names in os.walk(ROOT):
            directories[:] = sorted(
                value
                for value in directories
                if value not in ignored
                and not value.endswith(".egg-info")
                and not value.startswith((".test-", ".release-"))
            )
            root = Path(directory)
            paths.extend(root / name for name in sorted(names))
    files: list[dict[str, Any]] = []
    for path in paths:
        relative = path.relative_to(ROOT)
        files.append(
            {
                "path": str(relative).replace("\\", "/"),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return {"status": "passed", "file_count": len(files), "files": files}


def _stage_source_manifest(source: dict[str, Any], destination: Path) -> None:
    for item in source["files"]:
        relative = Path(item["path"])
        source_path = ROOT / relative
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source_path, destination_path)
        except OSError:
            shutil.copyfile(source_path, destination_path)


def _external_scanners(
    mode: str,
    site_packages: Path,
    source: dict[str, Any],
    output: Path,
) -> dict[str, dict[str, Any]]:
    enabled = mode in {"ci", "release"}
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    pip_audit = shutil.which("pip-audit")
    if not pip_audit:
        adjacent = Path(sys.executable).parent / (
            "pip-audit.exe" if os.name == "nt" else "pip-audit"
        )
        pip_audit = str(adjacent) if adjacent.is_file() else None
    scanners: dict[str, dict[str, Any]] = {}
    if enabled and pip_audit:
        scanners["python_vulnerability_scan"] = _capture(
            [pip_audit, "--path", str(site_packages), "--format", "json"],
            cwd=ROOT,
            timeout=300,
        ) | {"scanner": "pip-audit"}
    else:
        scanners["python_vulnerability_scan"] = {
            "status": "unavailable",
            "scanner": "pip-audit",
            "reason": "network scanners disabled in local mode"
            if not enabled
            else "tool not installed",
        }
    node_results: list[dict[str, Any]] = []
    if enabled and npm:
        for lock in sorted((ROOT / "components").glob("*/package-lock.json")):
            result = _capture(
                [npm, "audit", "--omit=dev", "--json", "--package-lock-only"],
                cwd=lock.parent,
                timeout=300,
            )
            node_results.append({"component": lock.parent.name, **result})
        node_status = (
            "passed"
            if node_results and all(item["status"] == "passed" for item in node_results)
            else "failed"
        )
        scanners["node_vulnerability_scan"] = {
            "status": node_status,
            "scanner": "npm audit",
            "results": node_results,
        }
    else:
        scanners["node_vulnerability_scan"] = {
            "status": "unavailable",
            "scanner": "npm audit",
            "reason": "network scanners disabled in local mode"
            if not enabled
            else "npm not installed",
        }
    gitleaks = shutil.which("gitleaks")
    syft = shutil.which("syft")
    if mode == "release" and (gitleaks or syft):
        with tempfile.TemporaryDirectory(prefix="villani-source-scan-") as temporary:
            staged_source = Path(temporary)
            _stage_source_manifest(source, staged_source)
            if gitleaks:
                report_path = output / "repository-secret-scan.json"
                result = _capture(
                    [
                        gitleaks,
                        "detect",
                        "--source",
                        str(staged_source),
                        "--no-git",
                        "--redact",
                        "--no-banner",
                        "--report-format",
                        "json",
                        "--report-path",
                        str(report_path),
                    ],
                    cwd=ROOT,
                    timeout=600,
                )
                valid_report = False
                finding_count: int | None = None
                if result["status"] == "passed" and report_path.is_file():
                    try:
                        findings = json.loads(report_path.read_text(encoding="utf-8"))
                        valid_report = isinstance(findings, list)
                        finding_count = len(findings) if valid_report else None
                    except (OSError, ValueError, AttributeError):
                        valid_report = False
                if not valid_report:
                    result["status"] = "failed"
                    result["stderr"] = (
                        result.get("stderr", "")
                        + "\nredacted gitleaks JSON report is missing or invalid"
                    ).strip()
                scanners["repository_secret_scan"] = result | {
                    "scanner": "gitleaks",
                    "version": _capture([gitleaks, "version"], cwd=ROOT)[
                        "stdout"
                    ].strip(),
                    "scope": "source_archive_manifest",
                    "source_file_count": source["file_count"],
                    "report": str(report_path) if report_path.is_file() else None,
                    "sha256": _sha256(report_path) if report_path.is_file() else None,
                    "finding_count": finding_count,
                }
            if syft:
                report_path = output / "external-sbom.cdx.json"
                result = _capture(
                    [
                        syft,
                        f"dir:{staged_source}",
                        "-o",
                        f"cyclonedx-json={report_path}",
                    ],
                    cwd=ROOT,
                    timeout=600,
                )
                valid_report = False
                if result["status"] == "passed" and report_path.is_file():
                    try:
                        valid_report = (
                            json.loads(report_path.read_text(encoding="utf-8")).get(
                                "bomFormat"
                            )
                            == "CycloneDX"
                        )
                    except (OSError, ValueError, AttributeError):
                        valid_report = False
                if not valid_report:
                    result["status"] = "failed"
                    result["stderr"] = (
                        result.get("stderr", "")
                        + "\nexternal Syft CycloneDX report is missing or invalid"
                    ).strip()
                scanners["external_sbom"] = result | {
                    "scanner": "syft",
                    "version": _capture([syft, "version"], cwd=ROOT)["stdout"].strip(),
                    "scope": "source_archive_manifest",
                    "source_file_count": source["file_count"],
                    "report": str(report_path) if report_path.is_file() else None,
                    "sha256": _sha256(report_path) if report_path.is_file() else None,
                }
    for name, executable in (
        ("repository_secret_scan", "gitleaks"),
        ("external_sbom", "syft"),
    ):
        if name not in scanners:
            scanners[name] = {
                "status": "unavailable",
                "scanner": executable,
                "reason": "required only in release mode"
                if mode != "release"
                else "tool not installed",
            }
    dockerfile = ROOT / "components" / "villani-control-plane" / "Dockerfile"
    docker = shutil.which("docker")
    trivy = shutil.which("trivy")
    if mode == "release" and dockerfile.is_file() and docker and trivy:
        tag = "villani-control-plane-release-scan"
        built = _capture(
            [
                docker,
                "build",
                "--file",
                str(dockerfile),
                "--tag",
                tag,
                str(ROOT),
            ],
            cwd=ROOT,
            timeout=1200,
        )
        report_path = output / "container-vulnerability-scan.json"
        if built["status"] == "passed":
            scanned = _capture(
                [
                    trivy,
                    "image",
                    "--exit-code",
                    "1",
                    "--severity",
                    "HIGH,CRITICAL",
                    "--format",
                    "json",
                    "--output",
                    str(report_path),
                    tag,
                ],
                cwd=ROOT,
                timeout=1200,
            )
        else:
            scanned = built
        cleanup = _capture(
            [docker, "image", "rm", "--force", tag], cwd=ROOT, timeout=120
        )
        valid_report = False
        if scanned["status"] == "passed" and report_path.is_file():
            try:
                valid_report = isinstance(
                    json.loads(report_path.read_text(encoding="utf-8")).get("Results"),
                    list,
                )
            except (OSError, ValueError, AttributeError):
                valid_report = False
        if scanned["status"] == "passed" and not valid_report:
            scanned["status"] = "failed"
            scanned["stderr"] = (
                scanned.get("stderr", "") + "\nTrivy JSON report is missing or invalid"
            ).strip()
        if built["status"] == "passed" and cleanup["status"] != "passed":
            scanned["status"] = "failed"
            scanned["stderr"] = (
                scanned.get("stderr", "") + "\nrelease scan image cleanup failed"
            ).strip()
        scanners["container_vulnerability_scan"] = scanned | {
            "scanner": "trivy",
            "version": _capture([trivy, "version"], cwd=ROOT)["stdout"].strip(),
            "image": tag,
            "report": str(report_path) if report_path.is_file() else None,
            "sha256": _sha256(report_path) if report_path.is_file() else None,
            "image_cleanup": cleanup["status"],
        }
    else:
        scanners["container_vulnerability_scan"] = {
            "status": "unavailable" if dockerfile.is_file() else "not_applicable",
            "scanner": "trivy",
            "reason": "required only in release mode"
            if mode != "release"
            else "docker or trivy not installed",
        }
    return scanners


def evaluate_external_scanners(
    mode: str, scanners: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Evaluate mode-specific scanner authority without treating absence as success."""
    required = REQUIRED_EXTERNAL_SCANNERS[mode]
    missing = sorted(name for name in required if name not in scanners)
    unavailable = sorted(
        name
        for name in required
        if name not in scanners or scanners[name].get("status") == "unavailable"
    )
    failed = sorted(
        name
        for name in required
        if name not in scanners or scanners[name].get("status") != "passed"
    )
    return {
        "passed": not failed,
        "required_external_scanners": sorted(required),
        "missing_required_scanners": missing,
        "unavailable_required_scanners": unavailable,
        "failed_required_scanners": failed,
    }


def generate(
    *,
    mode: str,
    installed_python: Path,
    packages: list[Path],
    output: Path,
    package_hashes: dict[str, str],
) -> dict[str, Any]:
    """Generate all supply-chain files and return the security summary."""
    output.mkdir(parents=True, exist_ok=True)
    python_inventory, pip_check = _python_inventory(installed_python)
    node_inventory, node_components = _node_inventory()
    python_manifest, node_manifest = _package_manifests(packages)
    site_packages = _site_packages(installed_python)
    licenses = _license_inventory(site_packages, node_inventory)
    secrets = _package_secret_scan(packages)
    source = _source_manifest()
    sbom_components = [
        {
            "type": "library",
            "name": item["name"],
            "version": item["version"],
            "purl": f"pkg:pypi/{item['name']}@{item['version']}",
        }
        for item in python_inventory
    ] + [
        {
            "type": "library",
            "name": item["name"],
            "version": item["version"],
            "purl": f"pkg:npm/{item['name']}@{item['version']}",
        }
        for item in node_inventory
    ]
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "component": {"type": "application", "name": "villani"},
        },
        "components": sbom_components,
    }
    provenance = {
        "schema_version": "villani.provenance.v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "builder": {"python": sys.version, "platform": platform.platform()},
        "runtime_versions": {
            "python": platform.python_version(),
            "node": (
                _capture([shutil.which("node") or "node", "--version"], cwd=ROOT).get(
                    "stdout"
                )
                or ""
            ).strip()
            or None,
            "npm": (
                _capture(
                    [
                        shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm",
                        "--version",
                    ],
                    cwd=ROOT,
                ).get("stdout")
                or ""
            ).strip()
            or None,
        },
        "package_hashes": package_hashes,
    }
    _write(
        output / "python-dependency-inventory.json",
        {"status": "passed", "packages": python_inventory, "pip_check": pip_check},
    )
    _write(
        output / "node-dependency-inventory.json",
        {"status": "passed", "packages": node_inventory, "components": node_components},
    )
    _write(
        output / "python-package-manifest.json",
        {"status": "passed", "packages": python_manifest},
    )
    _write(
        output / "node-package-manifest.json",
        {"status": "passed", "packages": node_manifest},
    )
    _write(output / "sbom.cdx.json", sbom)
    _write(output / "license-scan.json", licenses)
    _write(output / "secret-scan.json", secrets)
    _write(output / "source-archive-manifest.json", source)
    _write(output / "provenance.json", provenance)
    external = _external_scanners(mode, site_packages, source, output)
    scanner_policy = evaluate_external_scanners(mode, external)
    required = REQUIRED_EXTERNAL_SCANNERS[mode]
    deterministic = {
        "clean_environment_dependency_check": pip_check,
        "python_dependency_inventory": {
            "status": "passed",
            "count": len(python_inventory),
        },
        "node_dependency_inventory": {"status": "passed", "count": len(node_inventory)},
        "python_package_manifest": {"status": "passed", "count": len(python_manifest)},
        "node_package_manifest": {"status": "passed", "count": len(node_manifest)},
        "sbom": {
            "status": "passed",
            "component_count": len(sbom_components),
            "path": str(output / "sbom.cdx.json"),
        },
        "license_scan": {
            "status": licenses["status"],
            "unknown_count": licenses["unknown_count"],
            "forbidden_count": len(licenses["forbidden"]),
        },
        "package_secret_scan": {
            key: value for key, value in secrets.items() if key != "findings"
        }
        | {"finding_count": len(secrets["findings"])},
        "source_archive_manifest": {
            "status": "passed",
            "file_count": source["file_count"],
        },
        "provenance": {"status": "passed", "path": str(output / "provenance.json")},
    }
    failed_deterministic = [
        name for name, value in deterministic.items() if value.get("status") != "passed"
    ]
    passed = not failed_deterministic and scanner_policy["passed"]
    summary = {
        "schema_version": "villani.security_summary.v1",
        "mode": mode,
        "status": "passed" if passed else "failed",
        "official_release_certification": bool(mode == "release" and passed),
        "certification_note": "Official release certification was not performed."
        if mode != "release"
        else (
            "Every required release scanner passed."
            if passed
            else "Official certification failed."
        ),
        "deterministic_checks": deterministic,
        "external_scanners": external,
        "required_external_scanners": scanner_policy["required_external_scanners"],
        "missing_required_scanners": scanner_policy["missing_required_scanners"],
        "unavailable_required_scanners": scanner_policy[
            "unavailable_required_scanners"
        ],
        "failed_required_scanners": scanner_policy["failed_required_scanners"],
        "optional_unavailable_scanners": sorted(
            name
            for name, value in external.items()
            if value["status"] == "unavailable" and name not in required
        ),
    }
    _write(output / "security-summary.json", summary)
    return summary
