"""Crash-safe SQLite event spool and content-addressed artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from villani_ops.closed_loop.protocol_v2 import ArtifactDescriptorV2, OutcomeV2, TelemetryEnvelopeV2
from villani_ops.closed_loop.schema_validation import parse_protocol_document

from .config import AgentdPaths, Limits
from .redaction import redact_remote_document
from .spool_schema import SpoolSchemaError, migrate_spool_schema


class SpoolError(RuntimeError):
    status_code = 400


class CollisionError(SpoolError):
    status_code = 409


class LimitError(SpoolError):
    status_code = 413


@dataclass(frozen=True, slots=True)
class BatchResult:
    inserted: int
    duplicates: int


def _normalized_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class SQLiteSpool:
    def __init__(self, paths: AgentdPaths, limits: Limits) -> None:
        self.paths = paths
        self.limits = limits
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.artifacts.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.paths.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            try:
                migrate_spool_schema(connection)
            except SpoolSchemaError as error:
                raise SpoolError(str(error)) from error

    def record_local_import(
        self,
        run_id: str,
        *,
        highest_event_sequence: int,
        finalization_digest: str | None,
        attempted_at: str,
        error_category: str | None,
        completion_status: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO local_run_imports(
                       run_id,highest_event_sequence,finalization_digest,last_attempt_at,
                       last_error_category,completion_status
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       highest_event_sequence=MAX(
                           local_run_imports.highest_event_sequence,
                           excluded.highest_event_sequence
                       ),
                       finalization_digest=COALESCE(
                           excluded.finalization_digest,
                           local_run_imports.finalization_digest
                       ),
                       last_attempt_at=excluded.last_attempt_at,
                       last_error_category=excluded.last_error_category,
                       completion_status=excluded.completion_status""",
                (
                    run_id,
                    highest_event_sequence,
                    finalization_digest,
                    attempted_at,
                    error_category,
                    completion_status,
                ),
            )

    def local_import_records(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT run_id,highest_event_sequence,finalization_digest,last_attempt_at,
                          last_error_category,completion_status
                   FROM local_run_imports ORDER BY run_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def local_import_record(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT run_id,highest_event_sequence,finalization_digest,last_attempt_at,
                          last_error_category,completion_status
                   FROM local_run_imports WHERE run_id=?""",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def register_run(self, run_id: str, trace_id: str | None, created_at: str) -> bool:
        if not run_id:
            raise SpoolError("run_id is required")
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO runs(run_id, trace_id, created_at) VALUES(?,?,?)",
                (run_id, trace_id, created_at),
            )
            return cursor.rowcount == 1

    def ingest_events(self, documents: Iterable[Mapping[str, Any]]) -> BatchResult:
        prepared: list[tuple[TelemetryEnvelopeV2, str, str]] = []
        for document in documents:
            redacted = redact_remote_document(dict(document))
            remote_document = redacted.value
            if redacted.count and isinstance(remote_document, dict):
                body = dict(remote_document.get("body") or {})
                body["villani_redaction"] = {
                    "status": "redacted",
                    "count": redacted.count,
                    "categories": list(redacted.categories),
                }
                remote_document["body"] = body
            parsed = parse_protocol_document(remote_document)
            if not isinstance(parsed, TelemetryEnvelopeV2):
                raise SpoolError("event batches accept only villani.telemetry_envelope.v2")
            body_size = len(_normalized_json(parsed.body).encode("utf-8"))
            if body_size > self.limits.event_body_bytes:
                raise LimitError(f"event body exceeds {self.limits.event_body_bytes} bytes")
            payload = _normalized_json(parsed.model_dump(mode="json"))
            prepared.append((parsed, payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()))

        inserted = 0
        duplicates = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current_size = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0) FROM events"
                    ).fetchone()[0]
                )
                new_size = 0
                for event, payload, payload_digest in prepared:
                    existing = connection.execute(
                        "SELECT payload_sha256 FROM events WHERE event_id=?", (event.event_id,)
                    ).fetchone()
                    if existing is not None:
                        if existing["payload_sha256"] != payload_digest:
                            raise CollisionError(
                                f"event_id {event.event_id!r} already has different content"
                            )
                        duplicates += 1
                        continue
                    sequence = connection.execute(
                        """SELECT event_id, payload_sha256 FROM events
                           WHERE run_id=? AND sequence_scope=? AND sequence=?""",
                        (event.run_id, event.sequence_scope, event.sequence),
                    ).fetchone()
                    if sequence is not None:
                        raise CollisionError(
                            "sequence already belongs to a different event: "
                            f"{event.run_id}/{event.sequence_scope}/{event.sequence}"
                        )
                    payload_size = len(payload.encode("utf-8"))
                    if current_size + new_size + payload_size > self.limits.spool_bytes:
                        raise LimitError(f"event spool exceeds {self.limits.spool_bytes} bytes")
                    connection.execute(
                        """INSERT INTO events(
                            event_id, run_id, sequence_scope, sequence, occurred_at,
                            observed_at, payload_json, payload_sha256, upload_state,
                            retry_count, next_retry_at
                        ) VALUES(?,?,?,?,?,?,?,?, 'offline', 0, NULL)""",
                        (
                            event.event_id,
                            event.run_id,
                            event.sequence_scope,
                            event.sequence,
                            event.occurred_at.isoformat().replace("+00:00", "Z"),
                            event.observed_at.isoformat().replace("+00:00", "Z"),
                            payload,
                            payload_digest,
                        ),
                    )
                    new_size += payload_size
                    connection.execute(
                        "INSERT OR IGNORE INTO runs(run_id, trace_id, created_at) VALUES(?,?,?)",
                        (
                            event.run_id,
                            event.trace_id,
                            event.observed_at.isoformat().replace("+00:00", "Z"),
                        ),
                    )
                    inserted += 1
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return BatchResult(inserted=inserted, duplicates=duplicates)

    def register_artifact(
        self, run_id: str, descriptor_document: Mapping[str, Any], content: bytes
    ) -> ArtifactDescriptorV2:
        parsed = parse_protocol_document(dict(descriptor_document))
        if not isinstance(parsed, ArtifactDescriptorV2):
            raise SpoolError("artifact registration requires villani.artifact_descriptor.v2")
        if len(content) > self.limits.artifact_file_bytes:
            raise LimitError(f"artifact exceeds {self.limits.artifact_file_bytes} bytes")
        digest = hashlib.sha256(content).hexdigest()
        if digest != parsed.digest.value:
            raise SpoolError("artifact SHA-256 digest mismatch")
        if len(content) != parsed.size_bytes:
            raise SpoolError("artifact size mismatch")

        reference = f"sha256/{digest[:2]}/{digest}"
        destination = self.paths.artifacts / digest[:2] / digest
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                total = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                )
                existing = connection.execute(
                    "SELECT descriptor_json FROM artifacts WHERE artifact_id=?",
                    (parsed.artifact_id,),
                ).fetchone()
                stored = parsed.model_copy(update={"storage_reference": reference})
                descriptor_json = _normalized_json(stored.model_dump(mode="json"))
                if existing is not None:
                    if existing["descriptor_json"] != descriptor_json:
                        raise CollisionError(
                            f"artifact_id {parsed.artifact_id!r} already has different content"
                        )
                    connection.execute("COMMIT")
                    return stored
                if total + len(content) > self.limits.total_run_artifact_bytes:
                    raise LimitError(
                        f"run artifact total exceeds {self.limits.total_run_artifact_bytes} bytes"
                    )

                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{digest}.", suffix=".tmp", dir=destination.parent
                    )
                    temporary = Path(temporary_name)
                    try:
                        with os.fdopen(descriptor, "wb") as handle:
                            handle.write(content)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, destination)
                    except BaseException:
                        temporary.unlink(missing_ok=True)
                        raise
                connection.execute(
                    """INSERT INTO artifacts(
                        artifact_id, run_id, digest, size_bytes, descriptor_json,
                        storage_reference, upload_state
                    ) VALUES(?,?,?,?,?,?, 'offline')""",
                    (
                        parsed.artifact_id,
                        run_id,
                        digest,
                        len(content),
                        descriptor_json,
                        reference,
                    ),
                )
                connection.execute("COMMIT")
                return stored
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def finalize_run(self, run_id: str, payload: Mapping[str, Any], finalized_at: str) -> None:
        outcome = payload.get("outcome")
        if outcome is not None:
            parsed = parse_protocol_document(outcome)
            if not isinstance(parsed, OutcomeV2):
                raise SpoolError("final outcome must use villani.outcome.v2")
            if parsed.run_id != run_id:
                raise SpoolError("outcome run_id does not match path run_id")
        normalized = _normalized_json(dict(payload))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO runs(run_id, trace_id, created_at) VALUES(?,?,?)",
                    (run_id, None, finalized_at),
                )
                existing = connection.execute(
                    "SELECT final_payload_json FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if existing is not None and existing["final_payload_json"] is not None:
                    if existing["final_payload_json"] != normalized:
                        raise CollisionError(
                            f"run {run_id!r} already has a different final outcome"
                        )
                else:
                    connection.execute(
                        """UPDATE runs SET finalized_at=?, final_payload_json=?,
                           upload_state=?,retry_count=0,next_retry_at=NULL,
                           last_error=NULL,dead_lettered_at=NULL WHERE run_id=?""",
                        (
                            finalized_at,
                            normalized,
                            "offline" if outcome is not None else "acknowledged",
                            run_id,
                        ),
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            event_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            artifact_count = int(connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
            run_count = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE upload_state IN ('offline','retry')"
                ).fetchone()[0]
            )
            pending_outcomes = int(
                connection.execute(
                    """SELECT COUNT(*) FROM runs WHERE final_payload_json IS NOT NULL
                       AND upload_state IN ('offline','retry')"""
                ).fetchone()[0]
            )
            dead_letters = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE upload_state='dead_letter'"
                ).fetchone()[0]
            ) + int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE upload_state='dead_letter'"
                ).fetchone()[0]
            )
        return {
            "runs": run_count,
            "events": event_count,
            "artifacts": artifact_count,
            "pending_events": pending,
            "pending_outcomes": pending_outcomes,
            "dead_letters": dead_letters,
            "upload_mode": "offline",
        }

    def pending_finalizations(self, limit: int, now: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT run_id,final_payload_json,retry_count FROM runs
                   WHERE final_payload_json IS NOT NULL
                     AND upload_state IN ('offline','retry')
                     AND (next_retry_at IS NULL OR next_retry_at<=?)
                   ORDER BY finalized_at,run_id LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "payload": json.loads(row["final_payload_json"]),
                "retry_count": row["retry_count"],
            }
            for row in rows
        ]

    def acknowledge_finalization(self, run_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE runs SET upload_state='acknowledged',next_retry_at=NULL,
                   last_error=NULL WHERE run_id=?""",
                (run_id,),
            )

    def retry_finalization(self, run_id: str, next_retry_at: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE runs SET upload_state='retry',retry_count=retry_count+1,
                   next_retry_at=?,last_error=? WHERE run_id=?""",
                (next_retry_at, error[:500], run_id),
            )

    def dead_letter_finalization(self, run_id: str, now: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE runs SET upload_state='dead_letter',dead_lettered_at=?,
                   last_error=? WHERE run_id=?""",
                (now, error[:500], run_id),
            )

    def pending_events(self, limit: int, now: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT event_id,payload_json,retry_count FROM events
                   WHERE upload_state IN ('offline','retry')
                     AND (next_retry_at IS NULL OR next_retry_at<=?)
                   ORDER BY sequence_scope,sequence,event_id LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "document": json.loads(row["payload_json"]),
                "retry_count": row["retry_count"],
            }
            for row in rows
        ]

    def acknowledge_events(self, event_ids: Iterable[str]) -> None:
        values = list(event_ids)
        if not values:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    "DELETE FROM events WHERE event_id=?", ((value,) for value in values)
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def retry_events(self, event_ids: Iterable[str], next_retry_at: str, error: str) -> None:
        with self._connect() as connection:
            connection.executemany(
                """UPDATE events SET upload_state='retry',retry_count=retry_count+1,
                   next_retry_at=?,last_error=? WHERE event_id=?""",
                ((next_retry_at, error[:500], value) for value in event_ids),
            )

    def dead_letter_events(self, event_ids: Iterable[str], now: str, error: str) -> None:
        with self._connect() as connection:
            connection.executemany(
                """UPDATE events SET upload_state='dead_letter',dead_lettered_at=?,last_error=?
                   WHERE event_id=?""",
                ((now, error[:500], value) for value in event_ids),
            )

    def pending_artifacts(self, limit: int, now: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT artifact_id,run_id,descriptor_json,storage_reference,retry_count FROM artifacts
                   WHERE upload_state IN ('offline','retry')
                     AND (next_retry_at IS NULL OR next_retry_at<=?)
                   ORDER BY artifact_id LIMIT ?""",
                (now, limit),
            ).fetchall()
        return [dict(row) | {"descriptor": json.loads(row["descriptor_json"])} for row in rows]

    def acknowledge_artifact(self, artifact_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE artifacts SET upload_state='acknowledged',next_retry_at=NULL,last_error=NULL WHERE artifact_id=?",
                (artifact_id,),
            )

    def retry_artifact(self, artifact_id: str, next_retry_at: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE artifacts SET upload_state='retry',retry_count=retry_count+1,
                   next_retry_at=?,last_error=? WHERE artifact_id=?""",
                (next_retry_at, error[:500], artifact_id),
            )

    def dead_letter_artifact(self, artifact_id: str, now: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE artifacts SET upload_state='dead_letter',dead_lettered_at=?,last_error=?
                   WHERE artifact_id=?""",
                (now, error[:500], artifact_id),
            )

    def integrity_check(self) -> str:
        with self._connect() as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
