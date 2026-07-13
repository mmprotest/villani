"""Versioned, side-effect-free Agentd SQLite spool schema contract."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

CURRENT_SPOOL_SCHEMA_VERSION = 4
MINIMUM_SUPPORTED_SPOOL_SCHEMA_VERSION = 0


class SpoolSchemaError(RuntimeError):
    """Raised when a spool cannot be opened or migrated safely."""


@dataclass(frozen=True, slots=True)
class SpoolSchemaReport:
    version_before: int
    version_after: int
    mutated: bool


_REQUIRED_LEGACY_TABLES = {"runs", "events", "artifacts"}

_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "runs": (
        ("trace_id", "TEXT"),
        ("created_at", "TEXT NOT NULL DEFAULT ''"),
        ("finalized_at", "TEXT"),
        ("final_payload_json", "TEXT"),
        ("upload_state", "TEXT NOT NULL DEFAULT 'offline'"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("next_retry_at", "TEXT"),
        ("last_error", "TEXT"),
        ("dead_lettered_at", "TEXT"),
    ),
    "events": (
        ("run_id", "TEXT NOT NULL DEFAULT ''"),
        ("sequence_scope", "TEXT NOT NULL DEFAULT ''"),
        ("sequence", "INTEGER NOT NULL DEFAULT 0"),
        ("occurred_at", "TEXT NOT NULL DEFAULT ''"),
        ("observed_at", "TEXT NOT NULL DEFAULT ''"),
        ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("payload_sha256", "TEXT NOT NULL DEFAULT ''"),
        ("upload_state", "TEXT NOT NULL DEFAULT 'offline'"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("next_retry_at", "TEXT"),
        ("last_error", "TEXT"),
        ("dead_lettered_at", "TEXT"),
    ),
    "artifacts": (
        ("run_id", "TEXT NOT NULL DEFAULT ''"),
        ("digest", "TEXT NOT NULL DEFAULT ''"),
        ("size_bytes", "INTEGER NOT NULL DEFAULT 0"),
        ("descriptor_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("storage_reference", "TEXT NOT NULL DEFAULT ''"),
        ("upload_state", "TEXT NOT NULL DEFAULT 'offline'"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("next_retry_at", "TEXT"),
        ("last_error", "TEXT"),
        ("dead_lettered_at", "TEXT"),
    ),
}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def inspect_spool_schema(connection: sqlite3.Connection) -> SpoolSchemaReport:
    """Validate compatibility without changing the connection or database."""

    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SPOOL_SCHEMA_VERSION:
        raise SpoolSchemaError(
            f"spool schema version {version} is newer than supported version "
            f"{CURRENT_SPOOL_SCHEMA_VERSION}"
        )
    if version < MINIMUM_SUPPORTED_SPOOL_SCHEMA_VERSION:
        raise SpoolSchemaError(f"spool schema version {version} is no longer supported")
    tables = _tables(connection)
    if version == 0 and tables and not _REQUIRED_LEGACY_TABLES.issubset(tables):
        raise SpoolSchemaError("legacy spool has an unsupported table layout")
    return SpoolSchemaReport(version, version, False)


def migrate_spool_schema(connection: sqlite3.Connection) -> SpoolSchemaReport:
    """Migrate a compatible spool to v4 in one idempotent transaction."""

    before = inspect_spool_schema(connection).version_before
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE IF NOT EXISTS artifacts (artifact_id TEXT PRIMARY KEY)")
        for table, definitions in _COLUMNS.items():
            existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in definitions:
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        # Sparse v0 layouts did not always carry sequence/digest columns.
        # Give preserved legacy rows deterministic non-colliding placeholders;
        # their original payload and public identities remain untouched.
        connection.execute(
            """UPDATE events SET
                 run_id=CASE WHEN run_id='' THEN '__legacy__' ELSE run_id END,
                 sequence_scope=CASE WHEN sequence_scope='' THEN 'legacy' ELSE sequence_scope END,
                 sequence=CASE WHEN sequence=0 THEN rowid ELSE sequence END
               WHERE run_id='' OR sequence_scope='' OR sequence=0"""
        )
        connection.execute(
            """UPDATE artifacts SET
                 run_id=CASE WHEN run_id='' THEN '__legacy__' ELSE run_id END,
                 digest=CASE WHEN digest='' THEN artifact_id ELSE digest END
               WHERE run_id='' OR digest=''"""
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_run_sequence "
            "ON events(run_id, sequence_scope, sequence)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS events_upload_state "
            "ON events(upload_state, next_retry_at)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_run_digest "
            "ON artifacts(run_id, digest)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS local_run_imports (
                run_id TEXT PRIMARY KEY,
                highest_event_sequence INTEGER NOT NULL DEFAULT 0,
                finalization_digest TEXT,
                last_attempt_at TEXT NOT NULL,
                last_error_category TEXT,
                completion_status TEXT NOT NULL
            )"""
        )
        connection.execute(f"PRAGMA user_version={CURRENT_SPOOL_SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return SpoolSchemaReport(
        before,
        CURRENT_SPOOL_SCHEMA_VERSION,
        before != CURRENT_SPOOL_SCHEMA_VERSION,
    )
