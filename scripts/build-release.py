#!/usr/bin/env python3
"""Build one local, unsigned, reproducible Villani release-candidate archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Mapping

from packaging.requirements import InvalidRequirement, Requirement

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "release" / "VERSION").read_text(encoding="utf-8").strip()
RELEASE_NOTES = ROOT / "release" / "RELEASE_NOTES.md"
RELEASE_METADATA = ROOT / "release" / "release-metadata.json"
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
COMMANDS = ("villani", "villani-code", "villani-agentd", "vfr")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(paths: list[Path], destination: Path) -> None:
    lines = [f"{sha256(path)}  {path.name}" for path in sorted(paths, key=lambda item: item.name)]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def reproducible_zip(files: Mapping[str, Path], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            executable = name.removesuffix(".exe") in COMMANDS
            info.external_attr = (0o755 if executable else 0o644) << 16
            archive.writestr(info, path.read_bytes())


def normalized_system(value: str) -> str:
    selected = value.lower()
    return {"darwin": "macos", "win32": "windows"}.get(selected, selected)


def normalized_architecture(value: str) -> str:
    selected = value.lower().replace("-", "_")
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(
        selected, selected
    )


def release_sbom() -> dict[str, object]:
    """Build a deterministic CycloneDX inventory from shipped component manifests."""

    inventory: dict[str, dict[str, object]] = {}

    def add(component: dict[str, object]) -> None:
        reference = str(component["bom-ref"])
        inventory.setdefault(reference, component)

    shipped_python = ("villani", "villani-ops", "villani-code", "villani-agentd")
    for component in shipped_python:
        path = ROOT / "components" / component / "pyproject.toml"
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        project = document.get("project", {})
        name = str(project.get("name", path.parent.name))
        version = str(project.get("version", "unknown"))
        add(
            {
                "type": "application",
                "name": name,
                "version": version,
                "bom-ref": f"pkg:pypi/{name}@{version}",
                "hashes": [{"alg": "SHA-256", "content": sha256(path)}],
            }
        )
        for raw_dependency in project.get("dependencies", []):
            try:
                requirement = Requirement(str(raw_dependency))
                dependency_name = requirement.name
                dependency_constraint = str(requirement.specifier) or "unconstrained"
            except InvalidRequirement:
                dependency_name = str(raw_dependency)
                dependency_constraint = "unparsed"
            add(
                {
                    "type": "library",
                    "name": dependency_name,
                    "bom-ref": f"pkg:pypi/{dependency_name}",
                    "properties": [
                        {"name": "villani:declared-by", "value": component},
                        {
                            "name": "villani:declared-constraint",
                            "value": dependency_constraint,
                        },
                    ],
                }
            )

    shipped_node = (
        "villani-flight-recorder",
        "villani-run-model",
        "villani-ui",
        "villani-web",
    )
    for component in shipped_node:
        lock = ROOT / "components" / component / "package-lock.json"
        lock_document = json.loads(lock.read_text(encoding="utf-8"))
        node_version = str(lock_document.get("version", "unknown"))
        add(
            {
                "type": "application",
                "name": component,
                "version": node_version,
                "bom-ref": f"pkg:npm/{component}@{node_version}",
                "hashes": [{"alg": "SHA-256", "content": sha256(lock)}],
            }
        )
        for package_path, package in sorted(
            (lock_document.get("packages") or {}).items()
        ):
            if not package_path or not isinstance(package, dict):
                continue
            package_name = package_path.removeprefix("node_modules/")
            package_version = str(package.get("version") or "unknown")
            add(
                {
                    "type": "library",
                    "name": package_name,
                    "version": package_version,
                    "bom-ref": f"pkg:npm/{package_name}@{package_version}",
                    "scope": "optional" if package.get("optional") else "required",
                    "properties": [
                        {"name": "villani:declared-by", "value": component}
                    ],
                }
            )
    components = [inventory[key] for key in sorted(inventory)]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:00000000-0000-5000-8000-{hashlib.sha256(VERSION.encode()).hexdigest()[:12]}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "villani",
                "version": VERSION,
            }
        },
        "components": components,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def build_archive(runtime: Path, vfr: Path, output_dir: Path, system: str | None = None) -> Path:
    platform_name = normalized_system(system or platform.system())
    if platform_name not in {"windows", "macos", "linux"}:
        raise ValueError(f"unsupported release platform: {platform_name}")
    architecture = normalized_architecture(platform.machine())
    extension = ".exe" if platform_name == "windows" else ""
    staging = output_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    for command in ("villani", "villani-code", "villani-agentd"):
        target = staging / f"{command}{extension}"
        shutil.copy2(runtime, target)
        files[target.name] = target
    vfr_target = staging / f"vfr{extension}"
    shutil.copy2(vfr, vfr_target)
    files[vfr_target.name] = vfr_target
    signing = staging / "release-signing.json"
    _write_json(
        signing,
        {
            "schema_version": "villani.release_signing.v1",
            "status": "unsigned_release_candidate",
            "signing_required_before_publication": True,
        },
    )
    files[signing.name] = signing
    sbom = staging / "SBOM.cdx.json"
    _write_json(sbom, release_sbom())
    files[sbom.name] = sbom
    notes = staging / "RELEASE_NOTES.md"
    shutil.copy2(RELEASE_NOTES, notes)
    files[notes.name] = notes
    manifest_items = []
    for name, path in sorted(files.items()):
        manifest_items.append(
            {
                "path": name,
                "sha256": sha256(path),
                "size_bytes": path.stat().st_size,
                "executable": name.removesuffix(".exe") in COMMANDS,
            }
        )
    manifest = staging / "package-manifest.json"
    _write_json(
        manifest,
        {
            "schema_version": "villani.package_manifest.v1",
            "version": VERSION,
            "operating_system": platform_name,
            "architecture": architecture,
            "files": manifest_items,
            "sbom_path": sbom.name,
            "release_notes_path": notes.name,
            "source_checkout_required": False,
            "sibling_node_modules_required": False,
        },
    )
    files[manifest.name] = manifest
    archive = output_dir / f"villani-{VERSION}-{platform_name}-{architecture}.zip"
    reproducible_zip(files, archive)
    write_checksums([archive], output_dir / "SHA256SUMS")
    metadata = json.loads(RELEASE_METADATA.read_text(encoding="utf-8"))
    _write_json(
        output_dir / "update-feed.json",
        {
            "schema_version": "villani.update_feed.v1",
            "releases": [
                {
                    "version": VERSION,
                    "channel": metadata["channel"],
                    "released_at": metadata["released_at"],
                    "release_notes": RELEASE_NOTES.read_text(encoding="utf-8").strip(),
                    "minimum_config_version": 1,
                    "maximum_config_version": 1,
                    "artifacts": [
                        {
                            "operating_system": platform_name,
                            "architecture": architecture,
                            "url": archive.name,
                            "sha256": sha256(archive),
                        }
                    ],
                }
            ],
            "source_upload_required": False,
        },
    )
    return archive


def build_runtime(work: Path) -> Path:
    name = "villani-runtime"
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--name", name,
            "--paths", str(ROOT / "components" / "villani"),
            "--paths", str(ROOT / "components" / "villani-ops"),
            "--paths", str(ROOT / "components" / "villani-code"),
            "--paths", str(ROOT / "components" / "villani-agentd"),
            "--collect-all", "villani_distribution",
            "--collect-all", "villani_ops",
            "--collect-all", "villani_code",
            "--collect-all", "villani_agentd",
            "--exclude-module", "villani_ops.tests",
            "--exclude-module", "villani_code.tests",
            "--exclude-module", "villani_agentd.tests",
            "--exclude-module", "villani_distribution.tests",
            "--distpath", str(work / "dist"),
            "--workpath", str(work / "build"),
            "--specpath", str(work),
            str(ROOT / "components" / "villani" / "villani_distribution" / "frozen_entry.py"),
        ],
        cwd=ROOT,
        check=True,
    )
    return work / "dist" / f"{name}{'.exe' if os.name == 'nt' else ''}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--vfr", type=Path)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "scripts/sync-console-assets.py", "--check"],
        cwd=ROOT,
        check=True,
    )
    default_vfr = ROOT / "components" / "villani" / "villani_distribution" / "bin" / (
        "vfr.exe" if os.name == "nt" else "vfr"
    )
    vfr = (args.vfr or default_vfr).resolve()
    if not vfr.is_file():
        raise SystemExit("standalone vfr is missing; run scripts/build-vfr-standalone.py")
    if args.runtime:
        runtime = args.runtime.resolve()
        archive = build_archive(runtime, vfr, output)
    else:
        with tempfile.TemporaryDirectory(prefix="villani-pyinstaller-") as temporary:
            runtime = build_runtime(Path(temporary))
            archive = build_archive(runtime, vfr, output)
    print(archive)
    print(output / "SHA256SUMS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
