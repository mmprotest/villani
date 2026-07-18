#!/usr/bin/env python3
"""Verify a Villani archive and run privacy and external malware scans."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO


MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
SECRET_PATTERNS = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("bearer_token", re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/-]{16,}\b", re.I)),
    (
        "literal_credential",
        re.compile(
            rb"(?i)[\"']?(?:api[_-]?key|access[_-]?token|password|secret)"
            rb"[\"']?\s*[:=]\s*[\"'](?!\*{3}REDACTED\*{3}[\"'])"
            rb"[^\"'\r\n]{8,}[\"']"
        ),
    ),
)


def _sha256_stream(handle: BinaryIO) -> tuple[str, int, list[str]]:
    digest = hashlib.sha256()
    size = 0
    findings: set[str] = set()
    overlap = b""
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
        searchable = overlap + chunk
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(searchable):
                findings.add(name)
        overlap = searchable[-512:]
    return digest.hexdigest(), size, sorted(findings)


def _safe_name(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if value.is_absolute() or not value.parts or any(
        part in {"", ".", ".."} for part in value.parts
    ):
        raise ValueError(f"unsafe archive member: {name}")
    return value


def inspect_archive(path: Path) -> dict[str, object]:
    total = 0
    hashes: dict[str, tuple[str, int]] = {}
    secret_findings: list[dict[str, str]] = []
    with zipfile.ZipFile(path) as archive:
        names: set[str] = set()
        for info in archive.infolist():
            name = _safe_name(info.filename).as_posix()
            if name in names:
                raise ValueError(f"duplicate archive member: {name}")
            names.add(name)
            total += info.file_size
            if total > MAX_EXPANDED_BYTES:
                raise ValueError("expanded archive exceeds the release size limit")
            if info.is_dir():
                continue
            with archive.open(info) as handle:
                digest, size, findings = _sha256_stream(handle)
            hashes[name] = (digest, size)
            secret_findings.extend(
                {"path": name, "pattern": finding} for finding in findings
            )
        try:
            manifest = json.loads(archive.read("package-manifest.json"))
            sbom = json.loads(archive.read(str(manifest["sbom_path"])))
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"archive metadata is invalid: {error}") from error

    declared = manifest.get("files")
    if not isinstance(declared, list):
        raise ValueError("package manifest files must be a list")
    expected = {"package-manifest.json"}
    for item in declared:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("package manifest has an invalid file entry")
        name = _safe_name(item["path"]).as_posix()
        expected.add(name)
        actual = hashes.get(name)
        if actual is None:
            raise ValueError(f"manifest file is missing: {name}")
        if actual != (item.get("sha256"), item.get("size_bytes")):
            raise ValueError(f"manifest digest or size mismatch: {name}")
    if set(hashes) != expected:
        raise ValueError(
            "archive contents differ from the package manifest: "
            f"unexpected={sorted(set(hashes) - expected)}, "
            f"missing={sorted(expected - set(hashes))}"
        )
    if sbom.get("bomFormat") != "CycloneDX" or not isinstance(
        sbom.get("components"), list
    ):
        raise ValueError("SBOM is not a CycloneDX component inventory")
    return {
        "passed": not secret_findings,
        "manifest_version": manifest.get("version"),
        "operating_system": manifest.get("operating_system"),
        "architecture": manifest.get("architecture"),
        "files_verified": len(hashes),
        "expanded_size_bytes": total,
        "sbom_components": len(sbom["components"]),
        "secret_findings": secret_findings,
    }


def _defender_command() -> Path | None:
    direct = shutil.which("MpCmdRun.exe")
    if direct:
        return Path(direct)
    candidates: list[Path] = []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "Windows Defender" / "MpCmdRun.exe")
    program_data = os.environ.get("ProgramData")
    if program_data:
        candidates.extend(
            sorted(
                (Path(program_data) / "Microsoft" / "Windows Defender" / "Platform").glob(
                    "*/MpCmdRun.exe"
                ),
                reverse=True,
            )
        )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def malware_scan(path: Path) -> dict[str, object]:
    clam = shutil.which("clamscan")
    if clam:
        version = subprocess.run(
            [clam, "--version"], capture_output=True, text=True, check=False, timeout=30
        )
        completed = subprocess.run(
            [clam, "--infected", "--no-summary", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        return {
            "scanner": "ClamAV",
            "executed": True,
            "passed": completed.returncode == 0,
            "exit_code": completed.returncode,
            "version": version.stdout.strip() or version.stderr.strip(),
            "findings": completed.stdout.strip(),
            "error": completed.stderr.strip() or None,
        }
    defender = _defender_command()
    if defender:
        completed = subprocess.run(
            [
                str(defender),
                "-Scan",
                "-ScanType",
                "3",
                "-File",
                str(path),
                "-DisableRemediation",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        return {
            "scanner": "Microsoft Defender",
            "executed": True,
            "passed": completed.returncode == 0,
            "exit_code": completed.returncode,
            "version": defender.parent.name,
            "findings": completed.stdout.strip(),
            "error": completed.stderr.strip() or None,
        }
    return {
        "scanner": None,
        "executed": False,
        "passed": None,
        "exit_code": None,
        "version": None,
        "findings": None,
        "error": "Install ClamAV or enable Microsoft Defender, then rerun this command.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("local", "official"), default="local")
    args = parser.parse_args()
    archive = args.archive.expanduser().resolve()
    if not archive.is_file():
        raise SystemExit(f"release archive does not exist: {archive}")
    try:
        inspection = inspect_archive(archive)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        inspection = {"passed": False, "error": str(error), "secret_findings": []}
    malware = malware_scan(archive)
    passed = bool(inspection.get("passed")) and (
        bool(malware.get("passed")) if args.mode == "official" else malware.get("passed") is not False
    )
    report = {
        "schema_version": "villani.release_artifact_scan.v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": args.mode,
        "archive": archive.name,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "host": {"system": platform.system(), "architecture": platform.machine()},
        "package_and_secret_scan": inspection,
        "malware_scan": malware,
        "passed": passed,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(args.output)
    if not malware.get("executed"):
        print("External malware scanner unavailable; this is not malware-clean evidence.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
