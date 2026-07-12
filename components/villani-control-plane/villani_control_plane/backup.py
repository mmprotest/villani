from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_sqlite(source: Path, destination: Path) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)
    manifest = {
        "schema_version": "villani.backup.v1",
        "format": "sqlite-online-backup",
        "sha256": sha256(destination),
        "size_bytes": destination.stat().st_size,
    }
    destination.with_suffix(destination.suffix + ".manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def restore_sqlite(backup: Path, destination: Path) -> dict[str, object]:
    manifest_path = backup.with_suffix(backup.suffix + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256(backup) != manifest["sha256"]:
        raise ValueError("backup checksum mismatch")
    if destination.exists():
        raise FileExistsError("restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(backup, destination)
    with sqlite3.connect(destination) as database:
        result = database.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        destination.unlink(missing_ok=True)
        raise ValueError("restored database integrity check failed")
    return {**manifest, "restore_integrity": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backup or restore a local Villani SQLite database"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("source", type=Path)
    backup.add_argument("destination", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    result = (
        backup_sqlite(args.source, args.destination)
        if args.command == "backup"
        else restore_sqlite(args.source, args.destination)
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
