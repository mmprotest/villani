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
import zipfile
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.0rc1"
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


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
            info.external_attr = (0o755 if path.suffix != ".json" else 0o644) << 16
            archive.writestr(info, path.read_bytes())


def build_archive(runtime: Path, vfr: Path, output_dir: Path, system: str | None = None) -> Path:
    platform_name = (system or platform.system()).lower()
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
    signing.write_text(
        json.dumps(
            {
                "schema_version": "villani.release_signing.v1",
                "status": "unsigned_release_candidate",
                "signing_required_before_publication": True,
            },
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    files[signing.name] = signing
    archive = output_dir / f"villani-{VERSION}-{platform_name}-{platform.machine().lower()}.zip"
    reproducible_zip(files, archive)
    write_checksums([archive], output_dir / "SHA256SUMS")
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
