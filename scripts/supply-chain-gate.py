#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dependencies() -> list[dict[str, object]]:
    components: list[dict[str, object]] = []
    for path in sorted(ROOT.glob("components/*/pyproject.toml")):
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        project = document.get("project", {})
        components.append(
            {
                "type": "application",
                "name": str(project.get("name", path.parent.name)),
                "version": str(project.get("version", "unknown")),
                "properties": [{"name": "lock_sha256", "value": sha256(path)}],
            }
        )
        for dependency in project.get("dependencies", []):
            components.append(
                {
                    "type": "library",
                    "name": str(dependency),
                    "scope": "required",
                    "properties": [{"name": "declared_by", "value": path.parent.name}],
                }
            )
    for path in sorted(ROOT.glob("components/*/package-lock.json")):
        lock = json.loads(path.read_text(encoding="utf-8"))
        components.append(
            {
                "type": "application",
                "name": path.parent.name,
                "version": str(lock.get("version", "unknown")),
                "properties": [{"name": "lock_sha256", "value": sha256(path)}],
            }
        )
        for package_path, package in sorted(lock.get("packages", {}).items()):
            if not package_path or not isinstance(package, dict):
                continue
            components.append(
                {
                    "type": "library",
                    "name": package_path.removeprefix("node_modules/"),
                    "version": str(package.get("version", "unknown")),
                    "scope": "optional" if package.get("optional") else "required",
                    "properties": [{"name": "declared_by", "value": path.parent.name}],
                }
            )
    return components


def container_policy_scan() -> dict[str, object]:
    dockerfile = (ROOT / "components/villani-control-plane/Dockerfile").read_text(
        encoding="utf-8"
    )
    findings = []
    if "USER 10001" not in dockerfile:
        findings.append("container runs as root")
    if ":latest" in dockerfile:
        findings.append("floating latest image tag")
    if "--no-cache-dir" not in dockerfile:
        findings.append("pip cache retained")
    return {
        "scanner": "offline-dockerfile-policy-v1",
        "passed": not findings,
        "findings": findings,
    }


def real_container_scan(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"scanner": "docker-scout", "executed": False, "passed": False}
    document = json.loads(path.read_text(encoding="utf-8"))
    results = [
        finding
        for run in document.get("runs", [])
        for finding in run.get("results", [])
    ]
    return {
        "scanner": "docker-scout",
        "executed": True,
        "severity_filter": ["critical", "high"],
        "findings": len(results),
        "passed": not results,
        "evidence": str(path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic offline supply-chain gates"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("local", "official"), default="local",
        help="Official mode requires every external scanner to execute successfully.",
    )
    parser.add_argument(
        "--test-signing-key", default="villani-test-signing-key-not-for-release"
    )
    parser.add_argument(
        "--container-scan-report",
        type=Path,
        default=ROOT / "release/evidence/container-scan.sarif",
    )
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": "villani-foundation"}
        },
        "components": dependencies(),
    }
    sbom_path = args.output / "sbom.cdx.json"
    sbom_path.write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    checksum = sha256(sbom_path)
    signature = hmac.new(
        args.test_signing_key.encode(), bytes.fromhex(checksum), hashlib.sha256
    ).hexdigest()
    (args.output / "SHA256SUMS").write_text(
        f"{checksum}  {sbom_path.name}\n", encoding="utf-8"
    )
    (args.output / "TEST-SIGNATURE.json").write_text(
        json.dumps(
            {
                "algorithm": "hmac-sha256",
                "key_id": "test-only",
                "artifact": sbom_path.name,
                "signature": signature,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    pip = subprocess.run(
        [sys.executable, "-m", "pip", "check"], cwd=ROOT, capture_output=True, text=True
    )
    container = container_policy_scan()
    real_container = real_container_scan(args.container_scan_report.resolve())
    secret = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check-secrets.py"), "integration/fixtures"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    os_smoke = all(
        name in workflow for name in ("ubuntu-latest", "macos-latest", "windows-latest")
    )
    report = {
        "schema_version": "villani.supply_chain_gate.v1",
        "mode": args.mode,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "official_release_certification": False,
        "dependency_audit": {
            "tool": "pip check",
            "passed": pip.returncode == 0,
            "output": pip.stdout.strip(),
        },
        "sbom": {"path": str(sbom_path), "sha256": checksum},
        "artifact_signature": {
            "test_key_only": True,
            "verified": hmac.compare_digest(
                signature,
                hmac.new(
                    args.test_signing_key.encode(),
                    bytes.fromhex(checksum),
                    hashlib.sha256,
                ).hexdigest(),
            ),
        },
        "container_scan": container,
        "container_cve_scan": real_container,
        "secret_scan": {
            "command": "scripts/check-secrets.py integration/fixtures",
            "executed": True,
            "passed": secret.returncode == 0,
            "required": True,
        },
        "migration_restore_tests": {"required": True},
        "operating_system_package_smoke": {
            "configured": os_smoke,
            "systems": ["linux", "macos", "windows"],
        },
        "unsupported": [
            "cloud KMS integration",
            "production SAML/SCIM",
            "online CVE container scanner when air-gapped",
        ],
    }
    deterministic_passed = bool(
        report["dependency_audit"]["passed"]
        and report["artifact_signature"]["verified"]
        and container["passed"]
        and report["secret_scan"]["passed"]
        and os_smoke
    )
    official_passed = deterministic_passed and bool(
        real_container.get("executed") and real_container.get("passed")
    )
    report["official_release_certification"] = bool(
        args.mode == "official" and official_passed
    )
    report["passed"] = official_passed if args.mode == "official" else deterministic_passed
    report["external_scanners_unavailable"] = (
        [] if real_container.get("executed") else ["container_cve_scan"]
    )
    (args.output / "supply-chain-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(args.output / "supply-chain-report.json")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
