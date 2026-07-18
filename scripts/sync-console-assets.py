#!/usr/bin/env python3
"""Synchronize the built Villani Console into the Agentd wheel package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "components" / "villani-web" / "dist"
DEFAULT_DESTINATION = (
    ROOT / "components" / "villani-agentd" / "villani_agentd" / "console_assets"
)
MANIFEST = "console-assets.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != MANIFEST
    }


def expected(source: Path) -> dict[str, str]:
    values = inventory(source)
    if "index.html" not in values:
        raise RuntimeError(f"Console build is missing {source / 'index.html'}")
    references = [name for name in values if name.startswith("assets/")]
    if not references:
        raise RuntimeError("Console build contains no frontend assets")
    return values


def _staging_directory(destination: Path) -> Path:
    for _attempt in range(10):
        candidate = destination.parent / (
            f".{destination.name}.{secrets.token_hex(8)}"
        )
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a Console asset staging directory")


def synchronize(
    source: Path, destination: Path, *, force: bool = False
) -> dict[str, str]:
    values = expected(source)
    manifest_path = destination / MANIFEST
    try:
        packaged_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        packaged_manifest = None
    if not force and inventory(destination) == values and packaged_manifest == {
        "schema_version": "villani.console_assets.v1",
        "files": values,
    }:
        return values
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _staging_directory(destination)
    backup = destination.with_name(f".{destination.name}.previous")
    try:
        for relative in values:
            source_path = source / Path(relative)
            target = temporary / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
        (temporary / MANIFEST).write_text(
            json.dumps(
                {
                    "schema_version": "villani.console_assets.v1",
                    "files": values,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            os.replace(destination, backup)
        os.replace(temporary, destination)
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    source_values = expected(source)
    if args.check:
        destination_values = inventory(destination)
        if source_values != destination_values:
            missing = sorted(set(source_values) - set(destination_values))
            extra = sorted(set(destination_values) - set(source_values))
            changed = sorted(
                name
                for name in set(source_values) & set(destination_values)
                if source_values[name] != destination_values[name]
            )
            raise SystemExit(
                "Packaged Console assets are stale: "
                f"missing={missing}, extra={extra}, changed={changed}"
            )
        print(f"Console assets verified: {len(source_values)} files")
        return 0
    values = synchronize(source, destination, force=args.force)
    print(f"Console assets synchronized: {len(values)} files -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
