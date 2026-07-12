"""Bounded, idempotent import of canonical local runs into the agentd spool."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from villani_ops.closed_loop.durable_io import read_jsonl_tolerant
from villani_ops.closed_loop.event_sink import (
    SAFE_CANONICAL_ARTIFACTS,
    build_canonical_outcome,
    contains_registered_secret,
)
from villani_ops.closed_loop.protocol_v2 import ArtifactDescriptorV2, DigestV2
from villani_ops.closed_loop.schema_validation import (
    ProtocolValidationError,
    parse_protocol_document,
    validate_event_stream,
)
from villani_ops.closed_loop.translate_v2 import translate_v1_event

from .config import AgentdPaths, Limits
from .spool import CollisionError, LimitError, SQLiteSpool, SpoolError


DIAGNOSTIC_CATEGORIES = (
    "imported",
    "already_imported",
    "incomplete",
    "malformed",
    "unsupported_protocol",
    "sensitive_content_rejected",
    "temporarily_failed",
)


@dataclass(frozen=True, slots=True)
class ImportDiagnostic:
    run_id: str
    category: str
    imported_events: int = 0
    imported_artifacts: int = 0
    finalized: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "category": self.category,
            "imported_events": self.imported_events,
            "imported_artifacts": self.imported_artifacts,
            "finalized": self.finalized,
        }


class SensitiveContentError(ValueError):
    """A canonical input contains a locally registered secret."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_bytes(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"unsafe or missing canonical file: {path.name}")
    size = path.stat().st_size
    if size > limit:
        raise LimitError(f"canonical file {path.name} exceeds {limit} bytes")
    return path.read_bytes()


def _object(path: Path, limit: int) -> tuple[dict[str, Any], bytes]:
    content = _bounded_bytes(path, limit)
    value = json.loads(content.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path.name}")
    parse_protocol_document(value)
    return value, content


def _unsupported(error: BaseException) -> bool:
    return "unsupported schema_version" in str(error) or "newer than supported" in str(error)


class LocalRunImporter:
    def __init__(
        self,
        paths: AgentdPaths,
        limits: Limits,
        *,
        runs_root: Path | None = None,
        batch_size: int = 100,
    ) -> None:
        if batch_size < 1:
            raise ValueError("local-run backfill batch size must be positive")
        self.paths = paths
        self.limits = limits
        self.runs_root = runs_root or paths.root.parent / "runs"
        self.batch_size = batch_size
        self.spool = SQLiteSpool(paths, limits)

    def run_once(self) -> dict[str, Any]:
        diagnostics: list[ImportDiagnostic] = []
        if self.runs_root.is_dir() and not self.runs_root.is_symlink():
            candidates = sorted(
                (path for path in self.runs_root.iterdir() if path.is_dir()),
                key=self._candidate_order,
            )[: self.batch_size]
            for run_directory in candidates:
                diagnostics.append(self._import_one(run_directory))
        counts = {category: 0 for category in DIAGNOSTIC_CATEGORIES}
        for diagnostic in diagnostics:
            counts[diagnostic.category] += 1
        return {
            "runs_root": str(self.runs_root),
            "processed": len(diagnostics),
            "counts": counts,
            "diagnostics": [item.as_dict() for item in diagnostics],
        }

    def _candidate_order(self, path: Path) -> tuple[int, str, str]:
        tracking = self.spool.local_import_record(path.name)
        if tracking is None:
            return (0, "", path.name)
        completed = tracking.get("completion_status") in {"imported", "already_imported"}
        return (2 if completed else 1, str(tracking.get("last_attempt_at") or ""), path.name)

    def _record(
        self,
        run_id: str,
        category: str,
        sequence: int,
        digest: str | None = None,
    ) -> None:
        self.spool.record_local_import(
            run_id,
            highest_event_sequence=sequence,
            finalization_digest=digest,
            attempted_at=_utc_now(),
            error_category=None if category in {"imported", "already_imported"} else category,
            completion_status=category,
        )

    def _import_one(self, run_directory: Path) -> ImportDiagnostic:
        run_id = run_directory.name
        highest = 0
        prior = self.spool.local_import_record(run_id)
        try:
            if run_directory.is_symlink():
                raise ValueError("canonical run directory must not be a symlink")
            required = [
                run_directory / name for name in ("manifest.json", "state.json", "events.jsonl")
            ]
            if any(not path.is_file() for path in required):
                self._record(run_id, "incomplete", 0)
                return ImportDiagnostic(run_id, "incomplete")
            manifest, manifest_bytes = _object(required[0], self.limits.event_body_bytes)
            state, state_bytes = _object(required[1], self.limits.event_body_bytes)
            events_bytes = _bounded_bytes(required[2], self.limits.spool_bytes)
            if any(
                contains_registered_secret(content)
                for content in (manifest_bytes, state_bytes, events_bytes)
            ):
                raise SensitiveContentError("registered secret found in canonical run input")
            canonical_id = str(manifest.get("run_id") or "")
            if not canonical_id or canonical_id != str(state.get("run_id") or ""):
                raise ValueError("manifest and state run identities do not match")
            run_id = canonical_id
            if run_directory.name != run_id:
                raise ValueError("run directory name does not match canonical run identity")
            terminal = bool(state.get("terminal")) and manifest.get("completed_at") is not None
            final_digest = None
            if terminal:
                digest_input = manifest_bytes + b"\0" + state_bytes
                for relative in ("selection.json", "materialization.json"):
                    path = run_directory / relative
                    if path.is_file() and not path.is_symlink():
                        digest_input += b"\0" + _bounded_bytes(path, self.limits.event_body_bytes)
                final_digest = hashlib.sha256(digest_input).hexdigest()
                if (
                    prior is not None
                    and prior.get("finalization_digest") == final_digest
                    and int(prior.get("highest_event_sequence") or 0)
                    >= int(state.get("last_sequence") or 0)
                ):
                    self._record(
                        run_id, "already_imported", int(state["last_sequence"]), final_digest
                    )
                    return ImportDiagnostic(run_id, "already_imported", finalized=True)
            events = validate_event_stream(read_jsonl_tolerant(required[2]))
            translated = [translate_v1_event(event).model_dump(mode="json") for event in events]
            highest = events[-1].sequence if events else 0
            trace_id = events[0].trace_id if events else None
            created_at = (
                events[0].timestamp.isoformat().replace("+00:00", "Z")
                if events
                else str(manifest.get("created_at") or _utc_now())
            )
            self.spool.register_run(run_id, trace_id, created_at)
            result = self.spool.ingest_events(translated)

            artifact_count = 0
            rejected_sensitive = False
            for relative in SAFE_CANONICAL_ARTIFACTS if terminal else ():
                path = run_directory / relative
                if not path.exists():
                    continue
                content = _bounded_bytes(path, self.limits.artifact_file_bytes)
                if contains_registered_secret(content):
                    rejected_sensitive = True
                    continue
                digest = hashlib.sha256(content).hexdigest()
                descriptor = ArtifactDescriptorV2(
                    schema_version="villani.artifact_descriptor.v2",
                    artifact_id=f"artifact_{hashlib.sha256((run_id + ':' + relative).encode()).hexdigest()[:24]}",
                    digest=DigestV2(algorithm="sha256", value=digest),
                    size_bytes=len(content),
                    media_type="application/json",
                    logical_role="canonical_metadata",
                    sensitivity="internal",
                    retention_class="run",
                    encryption_status="unknown",
                    storage_reference=None,
                    provenance_status="recorded",
                    attributes={"villani.local.relative_path": relative},
                )
                self.spool.register_artifact(run_id, descriptor.model_dump(mode="json"), content)
                artifact_count += 1

            if terminal:
                outcome = build_canonical_outcome(run_directory)
                self.spool.finalize_run(
                    run_id,
                    {"outcome": outcome.model_dump(mode="json")},
                    str(manifest["completed_at"]),
                )

            imported = (
                result.inserted > 0
                or prior is None
                or (terminal and prior.get("finalization_digest") != final_digest)
            )
            category = "imported" if imported else "already_imported"
            if rejected_sensitive:
                category = "sensitive_content_rejected"
            self._record(run_id, category, highest, final_digest)
            return ImportDiagnostic(
                run_id,
                category,
                imported_events=result.inserted,
                imported_artifacts=artifact_count,
                finalized=terminal,
            )
        except SensitiveContentError:
            category = "sensitive_content_rejected"
        except ProtocolValidationError as error:
            category = "unsupported_protocol" if _unsupported(error) else "malformed"
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            category = "unsupported_protocol" if _unsupported(error) else "malformed"
        except (CollisionError, LimitError, OSError, SpoolError):
            category = "temporarily_failed"
        self._record(run_id, category, highest)
        return ImportDiagnostic(run_id, category)
