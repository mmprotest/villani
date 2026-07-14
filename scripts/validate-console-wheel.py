#!/usr/bin/env python3
"""Validate that an Agentd wheel contains one complete Villani Console build."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath


def find_wheel(value: Path) -> Path:
    candidate = value.resolve()
    if candidate.is_file():
        return candidate
    matches = sorted(candidate.glob("villani_agentd-*.whl"))
    if len(matches) != 1:
        raise SystemExit(
            f"expected one villani-agentd wheel in {candidate}, found {len(matches)}"
        )
    return matches[0]


def validate(wheel: Path) -> list[str]:
    prefix = PurePosixPath("villani_agentd/console_assets")
    manifest_path = str(prefix / "console-assets.json")
    index_path = str(prefix / "index.html")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = {manifest_path, index_path} - names
        if missing:
            raise SystemExit(f"Console wheel content missing: {sorted(missing)}")
        manifest = json.loads(archive.read(manifest_path))
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise SystemExit("Console wheel manifest has no asset inventory")
        expected = {str(prefix / str(name)) for name in files}
        missing_assets = expected - names
        if missing_assets:
            raise SystemExit(f"Console wheel assets missing: {sorted(missing_assets)}")
        for name, digest in files.items():
            actual = hashlib.sha256(archive.read(str(prefix / str(name)))).hexdigest()
            if actual != digest:
                raise SystemExit(f"Console wheel asset digest differs: {name}")
        html = archive.read(index_path).decode("utf-8")
        for name in files:
            asset = str(name)
            if asset.startswith("assets/") and f"/{asset}" not in html:
                raise SystemExit(f"Console index does not reference packaged asset: {asset}")
    return sorted(expected | {manifest_path, index_path})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="Agentd wheel or directory containing it")
    args = parser.parse_args()
    wheel = find_wheel(args.wheel)
    files = validate(wheel)
    print(f"Console wheel verified: {wheel} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
