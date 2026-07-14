"""Crash-safe SQLite event spool and content-addressed artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from villani_ops.closed_loop.protocol_v2 import ArtifactDescriptorV2, OutcomeV2, TelemetryEnvelopeV2
from villani_ops.closed_loop.schema_validation import parse_protocol_document
from villani_ops.closed_loop.translate_v2 import legacy_trace_id_to_w3c

from .config import AgentdPaths, Limits
from .redaction import redact_remote_document, unsafe_artifact_categories
from .spool_schema import SpoolSchemaError, migrate_spool_schema


class SpoolError(RuntimeError):
    status_code = 400


class CollisionError(SpoolError):
    status_code = 409


class LimitError(SpoolError):
    status_code = 413


class ArtifactWithheldError(SpoolError):
    status_code = 422

    def __init__(self, categories: tuple[str, ...]) -> None:
        self.categories = categories
        super().__init__(
            "artifact content was withheld: " + ",".join(categories)
        )


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
        unsafe_categories = unsafe_artifact_categories(content)
        if unsafe_categories:
            self._record_artifact_withholding(run_id, parsed, unsafe_categories)
            raise ArtifactWithheldError(unsafe_categories)
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

    def _record_artifact_withholding(
        self,
        run_id: str,
        descriptor: ArtifactDescriptorV2,
        categories: tuple[str, ...],
    ) -> None:
        """Persist only a safe notice when an artifact is rejected locally."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT trace_id FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        legacy_trace = str(row["trace_id"] if row is not None else run_id)
        digest = hashlib.sha256(
            f"{run_id}:{descriptor.artifact_id}:withheld".encode()
        ).hexdigest()
        now = datetime.now(timezone.utc)
        notice = TelemetryEnvelopeV2(
            schema_version="villani.telemetry_envelope.v2",
            event_id=f"evt2_{digest[:32]}",
            idempotency_key=f"agentd:artifact-withheld:{digest}",
            occurred_at=now,
            observed_at=now,
            sequence=int(digest[:8], 16),
            sequence_scope=f"agentd:artifact-withholding:{run_id}",
            organization_id=None,
            workspace_id=None,
            project_id=None,
            repository_id=None,
            run_id=run_id,
            trace_id=legacy_trace_id_to_w3c(legacy_trace),
            span_id=digest[32:48],
            parent_span_id=None,
            attempt_id=None,
            source="villani-agentd",
            kind="file_operation",
            name="artifact_withholding_recorded",
            status="ok",
            resource={
                "schema_version": "villani.resource.v2",
                "service_name": "villani-agentd",
                "service_version": None,
                "deployment_environment": "local",
                "host_id": None,
                "process_id": None,
                "attributes": {},
            },
            attributes={},
            body={
                "withheld_artifact_count": 1,
                "withheld_artifact_categories": list(categories),
                "logical_role": descriptor.logical_role,
            },
        )
        self.ingest_events((notice.model_dump(mode="json"),))

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

    def console_run_states(self, connected: bool) -> dict[str, str]:
        """Return the one public synchronization state for every local run."""

        with self._connect() as connection:
            runs = connection.execute(
                "SELECT run_id,upload_state,final_payload_json FROM runs"
            ).fetchall()
            events = connection.execute(
                "SELECT run_id,upload_state,payload_json FROM events"
            ).fetchall()
            artifacts = connection.execute(
                "SELECT run_id,upload_state FROM artifacts"
            ).fetchall()
            imports = connection.execute("SELECT run_id FROM local_run_imports").fetchall()
        run_ids = {
            str(row["run_id"])
            for row in [*runs, *events, *artifacts, *imports]
            if row["run_id"]
        }
        if not connected:
            return {run_id: "LOCAL" for run_id in run_ids}

        states: dict[str, list[str]] = {run_id: [] for run_id in run_ids}
        redacted: set[str] = set()
        for row in runs:
            states[str(row["run_id"])].append(str(row["upload_state"]))
            payload = str(row["final_payload_json"] or "")
            if "villani_redaction" in payload or "artifact_withholding" in payload:
                redacted.add(str(row["run_id"]))
        for row in events:
            run_id = str(row["run_id"])
            states[run_id].append(str(row["upload_state"]))
            payload = str(row["payload_json"] or "")
            if "villani_redaction" in payload or "artifact_withholding_recorded" in payload:
                redacted.add(run_id)
        for row in artifacts:
            states[str(row["run_id"])].append(str(row["upload_state"]))

        result: dict[str, str] = {}
        for run_id, upload_states in states.items():
            if "dead_letter" in upload_states:
                result[run_id] = "SYNC FAILED"
            elif run_id in redacted:
                result[run_id] = "REDACTED"
            elif any(value in {"offline", "retry"} for value in upload_states):
                result[run_id] = "SYNC PENDING"
            elif upload_states and all(value == "acknowledged" for value in upload_states):
                result[run_id] = "SYNCHRONIZED"
            else:
                result[run_id] = "SYNC PENDING"
        return result

    @staticmethod
    def _console_value(document: Any, names: tuple[str, ...]) -> Any:
        def present(value: Any) -> bool:
            return value is not None and value != ""

        if isinstance(document, Mapping):
            for name in names:
                value = document.get(name)
                if present(value):
                    return value
            for value in document.values():
                found = SQLiteSpool._console_value(value, names)
                if present(found):
                    return found
        elif isinstance(document, list):
            for value in document:
                found = SQLiteSpool._console_value(value, names)
                if present(found):
                    return found
        return None

    def console_runs(self, connected: bool) -> list[dict[str, Any]]:
        """Project redacted spool metadata into the shared Console history schema."""

        with self._connect() as connection:
            runs = connection.execute(
                """SELECT run_id,created_at,finalized_at,final_payload_json
                   FROM runs ORDER BY COALESCE(finalized_at,created_at) DESC"""
            ).fetchall()
            events = connection.execute(
                """SELECT run_id,occurred_at,payload_json FROM events
                   ORDER BY occurred_at,sequence"""
            ).fetchall()
            imports = {
                str(row["run_id"]): dict(row)
                for row in connection.execute(
                    """SELECT run_id,last_attempt_at,completion_status,last_error_category
                       FROM local_run_imports"""
                ).fetchall()
            }
        by_run: dict[str, list[dict[str, Any]]] = {}
        for row in events:
            try:
                document = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                continue
            if isinstance(document, dict):
                by_run.setdefault(str(row["run_id"]), []).append(document)
        states = self.console_run_states(connected)
        values: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in runs:
            run_id = str(row["run_id"])
            seen.add(run_id)
            documents = by_run.get(run_id, [])
            final: dict[str, Any] = {}
            if row["final_payload_json"]:
                try:
                    loaded = json.loads(str(row["final_payload_json"]))
                    final = loaded if isinstance(loaded, dict) else {}
                except json.JSONDecodeError:
                    pass
            search_documents: list[Any] = [final, *reversed(documents)]
            value = lambda names: next(  # noqa: E731
                (
                    found
                    for document in search_documents
                    if (found := self._console_value(document, names)) is not None
                    and found != ""
                ),
                None,
            )
            status = value(("final_state", "completion_status", "status", "state"))
            task = value(("task_instruction", "instruction", "task_text"))
            repository = value(("repository_path", "repository", "repo_path"))
            model = value(("selected_model", "model"))
            cost = value(("total_cost_usd", "cost_usd"))
            updated_at = row["finalized_at"]
            if not updated_at and documents:
                updated_at = self._console_value(documents[-1], ("observed_at", "occurred_at"))
            imported = imports.get(run_id, {})
            values.append(
                {
                    "id": run_id,
                    "logical_id": run_id,
                    "kind": "run",
                    "source": "villani",
                    "source_label": "Villani",
                    "provider": "villani",
                    "repository": str(repository) if isinstance(repository, str) else None,
                    "task": str(task) if isinstance(task, str) else None,
                    "status": str(status or imported.get("completion_status") or "unknown"),
                    "model": str(model) if isinstance(model, str) else None,
                    "started_at": str(row["created_at"] or "") or None,
                    "updated_at": str(updated_at or imported.get("last_attempt_at") or "") or None,
                    "duration_ms": None,
                    "cost": (
                        cost
                        if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                        else None
                    ),
                    "currency": "USD" if isinstance(cost, (int, float)) else None,
                    "cost_available": isinstance(cost, (int, float)) and not isinstance(cost, bool),
                    "synchronization_state": states.get(run_id, "LOCAL"),
                    "deep_link": f"/console/runs/{urllib.parse.quote(run_id, safe='')}",
                }
            )
        for run_id, imported in imports.items():
            if run_id in seen:
                continue
            values.append(
                {
                    "id": run_id,
                    "logical_id": run_id,
                    "kind": "run",
                    "source": "villani",
                    "source_label": "Villani",
                    "provider": "villani",
                    "repository": None,
                    "task": None,
                    "status": str(imported.get("completion_status") or "unknown"),
                    "model": None,
                    "started_at": None,
                    "updated_at": str(imported.get("last_attempt_at") or "") or None,
                    "duration_ms": None,
                    "cost": None,
                    "currency": None,
                    "cost_available": False,
                    "synchronization_state": states.get(run_id, "LOCAL"),
                    "deep_link": f"/console/runs/{urllib.parse.quote(run_id, safe='')}",
                }
            )
        return values

    def console_recovery_events(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT event_id,run_id,occurred_at,payload_json FROM events
                   ORDER BY occurred_at DESC LIMIT 500"""
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("name") or payload.get("kind") or "")
            if not any(word in name.lower() for word in ("recover", "retry", "resume", "restart")):
                continue
            result.append(
                {
                    "id": str(row["event_id"]),
                    "run_id": str(row["run_id"]),
                    "timestamp": str(row["occurred_at"]),
                    "name": name,
                    "status": str(payload.get("status") or "recorded"),
                }
            )
            if len(result) >= limit:
                break
        return result

    def pending_finalizations(self, limit: int, now: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT r.run_id,r.final_payload_json,r.retry_count FROM runs AS r
                   WHERE r.final_payload_json IS NOT NULL
                     AND r.upload_state IN ('offline','retry')
                     AND (r.next_retry_at IS NULL OR r.next_retry_at<=?)
                     AND NOT EXISTS (
                         SELECT 1 FROM events AS e WHERE e.run_id=r.run_id
                     )
                   ORDER BY r.finalized_at,r.run_id LIMIT ?""",
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
                """SELECT a.artifact_id,a.run_id,a.descriptor_json,
                          a.storage_reference,a.retry_count
                     FROM artifacts AS a
                    WHERE a.upload_state IN ('offline','retry')
                      AND (a.next_retry_at IS NULL OR a.next_retry_at<=?)
                      AND NOT EXISTS (
                          SELECT 1 FROM events AS e WHERE e.run_id=a.run_id
                      )
                    ORDER BY a.artifact_id LIMIT ?""",
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
