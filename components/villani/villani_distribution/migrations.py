from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import yaml

from villani_ops.closed_loop.durable_io import write_json_atomic

SUPPORTED_CONFIG_VERSION = 1
SUPPORTED_SPOOL_VERSION = 1
SUPPORTED_PROTOCOL_MAJORS = {1, 2}


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationReport:
    config_version: int | None
    spool_version_before: int | None
    spool_version_after: int | None
    checked_run_bundles: int
    protocol_majors: tuple[int, ...]


def _config_version(home: Path) -> int | None:
    path = home / "config.yaml"
    if not path.is_file():
        return None
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise MigrationError(f"configuration migration check failed: {error}") from error
    if not isinstance(document, dict):
        raise MigrationError("configuration migration check failed: config must be an object")
    raw_version = document.get("config_version", 1)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise MigrationError("configuration config_version must be an integer")
    if raw_version > SUPPORTED_CONFIG_VERSION:
        raise MigrationError(
            f"configuration version {raw_version} is newer than supported version {SUPPORTED_CONFIG_VERSION}"
        )
    return raw_version


def _spool_version(home: Path, *, apply: bool) -> tuple[int | None, int | None]:
    path = home / "agentd" / "spool.sqlite3"
    if not path.is_file():
        return None, None
    try:
        with sqlite3.connect(path) as connection:
            before = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if before > SUPPORTED_SPOOL_VERSION:
                raise MigrationError(
                    f"spool version {before} is newer than supported version {SUPPORTED_SPOOL_VERSION}"
                )
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if before == 0 and tables and not {"runs", "events", "artifacts"}.issubset(tables):
                raise MigrationError("legacy spool has an unsupported table layout")
            after = before
            if apply and before == 0 and tables:
                connection.execute(f"PRAGMA user_version={SUPPORTED_SPOOL_VERSION}")
                connection.commit()
                after = SUPPORTED_SPOOL_VERSION
            return before, after
    except sqlite3.Error as error:
        raise MigrationError(f"spool migration check failed: {error}") from error


def _protocol_major(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    suffix = value.rsplit(".v", 1)
    if len(suffix) != 2 or not suffix[1].isdigit():
        return None
    return int(suffix[1])


def _run_protocols(home: Path) -> tuple[int, tuple[int, ...]]:
    root = home / "runs"
    if not root.is_dir():
        return 0, ()
    majors: set[int] = set()
    checked = 0
    for directory in sorted(root.iterdir()):
        manifest = directory / "manifest.json"
        if not directory.is_dir() or not manifest.is_file():
            continue
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MigrationError(f"run {directory.name} manifest is unreadable: {error}") from error
        if not isinstance(document, dict):
            raise MigrationError(f"run {directory.name} manifest must be an object")
        major = _protocol_major(document.get("schema_version"))
        if major is None:
            raise MigrationError(f"run {directory.name} has no recognizable protocol version")
        if major not in SUPPORTED_PROTOCOL_MAJORS:
            raise MigrationError(f"run {directory.name} uses unsupported protocol v{major}")
        majors.add(major)
        checked += 1
    return checked, tuple(sorted(majors))


def check_upgrade(home: Path, *, apply: bool = True) -> MigrationReport:
    home = home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    config_version = _config_version(home)
    before, after = _spool_version(home, apply=apply)
    checked, majors = _run_protocols(home)
    report = MigrationReport(config_version, before, after, checked, majors)
    if apply:
        write_json_atomic(
            home / "migration-state.json",
            {
                "schema_version": "villani.migration_state.v1",
                "distribution_version": "0.3.0rc1",
                **asdict(report),
            },
        )
    return report
