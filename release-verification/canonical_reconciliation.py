"""Canonical cross-surface run reconciliation for the packaged release gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


TERMINAL_EVENTS = {"run_completed", "run_failed", "run_exhausted"}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object at {path}:{line_number}")
        values.append(value)
    return values


def _trace_id(legacy_trace_id: str) -> str:
    return hashlib.sha256(
        f"villani:v2:trace:{legacy_trace_id}".encode("utf-8")
    ).hexdigest()[:32]


def _status(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


def _safe_strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({item for item in value if isinstance(item, str) and item})


def _round_numbers(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 12)
    if isinstance(value, list):
        return [_round_numbers(item) for item in value]
    if isinstance(value, tuple):
        return [_round_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_numbers(item) for key, item in value.items()}
    return value


def _attempt(
    attempt_id: str,
    candidate: Mapping[str, Any],
    *,
    selected_attempt_id: str | None,
    fallback_status: Any = None,
) -> dict[str, Any]:
    verification = candidate.get("verification")
    verification = verification if isinstance(verification, Mapping) else {}
    metadata = verification.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    eligible = candidate.get("candidate_eligibility")
    return {
        "attempt_id": attempt_id,
        "status": _status(candidate.get("status") or fallback_status),
        "backend": candidate.get("backend_name"),
        "model": candidate.get("model"),
        "eligible": eligible if isinstance(eligible, bool) else None,
        "selected": attempt_id == selected_attempt_id,
        "verification_outcome": _status(verification.get("outcome")),
        "verification_authority": verification.get("authority_source")
        or metadata.get("authority_source"),
        "verifier_identity": verification.get("verifier"),
        "input_tokens": candidate.get("input_tokens"),
        "output_tokens": candidate.get("output_tokens"),
        "total_tokens": candidate.get("total_tokens"),
        "cost_usd": candidate.get("cost_usd"),
        "duration_ms": candidate.get("duration_ms"),
        "changed_files": _safe_strings(candidate.get("changed_files")),
        "file_write_count": candidate.get("file_write_count"),
        "failure_category": candidate.get("failure_category"),
    }


def snapshot_from_projection(
    projection: Mapping[str, Any],
    *,
    run_id: str | None = None,
    trace_id: str | None = None,
    status: str | None = None,
    attempt_statuses: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize a Control Plane-compatible projection without inventing values."""

    selected_attempt_id = projection.get("selected_attempt_id")
    selected_attempt_id = (
        selected_attempt_id if isinstance(selected_attempt_id, str) else None
    )
    candidates = projection.get("candidate_outcomes")
    candidates = candidates if isinstance(candidates, Mapping) else {}
    statuses = attempt_statuses or {}
    attempt_ids = sorted({str(item) for item in (*candidates.keys(), *statuses.keys())})
    attempts = [
        _attempt(
            attempt_id,
            (
                candidates.get(attempt_id)
                if isinstance(candidates.get(attempt_id), Mapping)
                else {}
            ),
            selected_attempt_id=selected_attempt_id,
            fallback_status=statuses.get(attempt_id),
        )
        for attempt_id in attempt_ids
    ]
    selected = next((item for item in attempts if item["selected"]), None)
    return _round_numbers(
        {
            "run_id": run_id or projection.get("run_id"),
            "trace_id": trace_id or projection.get("trace_id"),
            "status": _status(status or projection.get("status")),
            "task": projection.get("task_instruction"),
            "success_criteria": projection.get("success_criteria"),
            "repository": projection.get("repository"),
            "agent_name": projection.get("agent_name"),
            "agent_version": projection.get("agent_version"),
            "raw_classification": projection.get("raw_classification"),
            "effective_classification": projection.get("effective_classification"),
            "classification_confidence": projection.get("classification_confidence"),
            "classification_adjustments": projection.get("classification_adjustments")
            or [],
            "policy_version": projection.get("policy_version"),
            "selected_backend": projection.get("selected_backend"),
            "selected_model": projection.get("selected_model"),
            "selected_attempt_id": selected_attempt_id,
            "attempts": attempts,
            "escalation_count": projection.get("escalation_count"),
            "input_tokens": projection.get("input_tokens"),
            "output_tokens": projection.get("output_tokens"),
            "total_tokens": projection.get("total_tokens"),
            "coding_cost_usd": projection.get("coding_cost_usd"),
            "verifier_cost_usd": projection.get("verifier_cost_usd"),
            "total_cost_usd": projection.get("total_cost_usd"),
            "duration_ms": projection.get("duration_ms"),
            "verification_outcome": _status(projection.get("verification_status")),
            "verification_authority": projection.get("verification_authority"),
            "verifier_identity": (
                selected.get("verifier_identity") if selected else None
            ),
            "candidate_eligibility": {
                item["attempt_id"]: item["eligible"] for item in attempts
            },
            "candidate_rankings": projection.get("selection_rankings") or [],
            "selection_reason": projection.get("selection_reason"),
            "file_write_count": projection.get("file_write_count"),
            "attempt_changed_files": {
                item["attempt_id"]: item["changed_files"] for item in attempts
            },
            "selected_materialized_files": _safe_strings(
                projection.get("changed_files")
            ),
            "materialization_status": _status(projection.get("materialization_status")),
            "failure_category": projection.get("failure_category"),
            "terminal_reason": projection.get("terminal_reason"),
            "redaction_status": projection.get("redaction_status"),
            "redacted_field_count": projection.get("redacted_field_count"),
            "withheld_artifact_count": projection.get("withheld_artifact_count"),
            "withheld_artifact_categories": (
                _safe_strings(projection.get("withheld_artifact_categories"))
                if isinstance(
                    projection.get("withheld_artifact_categories"), (list, tuple)
                )
                else None
            ),
        }
    )


def local_snapshot(run_dir: Path) -> dict[str, Any]:
    manifest = _read(run_dir / "manifest.json")
    task = _read(run_dir / "task.json")
    state = _read(run_dir / "state.json")
    classification = _read(run_dir / "classification.json")
    classification_metadata = classification.get("metadata") or {}
    events = _read_jsonl(run_dir / "events.jsonl")
    terminal = next(
        (
            item.get("payload") or {}
            for item in reversed(events)
            if item.get("event_type") in TERMINAL_EVENTS
        ),
        {},
    )
    selection = (
        _read(run_dir / "selection.json")
        if (run_dir / "selection.json").is_file()
        else {}
    )
    materialization = (
        _read(run_dir / "materialization.json")
        if (run_dir / "materialization.json").is_file()
        else {}
    )
    policy_decisions = _read_jsonl(run_dir / "policy_decisions.jsonl")
    attempts: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "attempts").glob("*/attempt.json")):
        attempt = _read(path)
        attempt_id = str(attempt["attempt_id"])
        verification_path = run_dir / "verification" / f"{attempt_id}.json"
        verification = _read(verification_path) if verification_path.is_file() else {}
        metadata = attempt.get("metadata") or {}
        attempts[attempt_id] = {
            "status": attempt.get("status"),
            "backend_name": attempt.get("backend_name"),
            "model": attempt.get("model"),
            "candidate_eligibility": (
                verification.get("acceptance_eligible") if verification else False
            ),
            "verification": verification,
            "input_tokens": attempt.get("input_tokens"),
            "output_tokens": attempt.get("output_tokens"),
            "total_tokens": (
                attempt.get("input_tokens") + attempt.get("output_tokens")
                if isinstance(attempt.get("input_tokens"), int)
                and isinstance(attempt.get("output_tokens"), int)
                else None
            ),
            "cost_usd": attempt.get("cost_usd"),
            "duration_ms": attempt.get("duration_ms"),
            "changed_files": metadata.get("changed_files") or [],
            "file_write_count": metadata.get("total_file_writes"),
            "failure_category": metadata.get("failure_category"),
        }
    policy_version = next(
        (
            item.get("policy_version")
            for item in reversed(policy_decisions)
            if item.get("policy_version")
        ),
        (manifest.get("metadata") or {}).get("policy_configuration", {}).get("version"),
    )
    projection = {
        "run_id": manifest.get("run_id"),
        "trace_id": _trace_id(str(manifest.get("trace_id"))),
        "status": terminal.get("status") or state.get("state"),
        "task_instruction": task.get("instruction"),
        "success_criteria": task.get("success_criteria"),
        "repository": Path(str(task.get("repository_path") or "")).name or None,
        "agent_name": "villani-ops",
        "agent_version": "0.2.0",
        "raw_classification": classification_metadata.get("raw_classification"),
        "effective_classification": classification_metadata.get(
            "effective_classification"
        ),
        "classification_confidence": classification.get("confidence"),
        "classification_adjustments": classification_metadata.get(
            "classification_adjustments"
        )
        or [],
        "policy_version": policy_version,
        "selected_backend": terminal.get("selected_backend"),
        "selected_model": terminal.get("selected_model"),
        "selected_attempt_id": terminal.get("selected_attempt_id")
        or next(iter(selection.get("selected_candidate_ids") or []), None),
        "candidate_outcomes": attempts,
        "escalation_count": terminal.get("escalation_count"),
        "input_tokens": terminal.get("input_tokens"),
        "output_tokens": terminal.get("output_tokens"),
        "total_tokens": terminal.get("total_tokens"),
        "coding_cost_usd": terminal.get("coding_cost_usd"),
        "verifier_cost_usd": terminal.get("verifier_cost_usd"),
        "total_cost_usd": terminal.get("total_cost_usd"),
        "duration_ms": manifest.get("run_wall_clock_duration_ms"),
        "verification_status": terminal.get("verification_status"),
        "verification_authority": next(
            (
                value.get("verification", {})
                .get("metadata", {})
                .get("authority_source")
                for value in attempts.values()
                if value.get("verification", {}).get("attempt_id")
                == terminal.get("selected_attempt_id")
            ),
            None,
        ),
        "selection_rankings": selection.get("rankings") or [],
        "selection_reason": selection.get("reason"),
        "file_write_count": terminal.get("file_write_count"),
        "changed_files": materialization.get("changed_files")
        or terminal.get("changed_files")
        or [],
        "materialization_status": terminal.get("materialization_status")
        or materialization.get("status"),
        "failure_category": terminal.get("failure_category"),
        "terminal_reason": terminal.get("terminal_reason")
        or (state.get("metadata") or {}).get("terminal_reason"),
        # Redaction and artifact withholding are synchronization facts. They
        # remain unknown in the immutable local execution bundle.
        "redaction_status": None,
        "redacted_field_count": None,
        "withheld_artifact_count": None,
        "withheld_artifact_categories": None,
    }
    return snapshot_from_projection(projection)


def project_spool_events(
    event_documents: Iterable[Mapping[str, Any]],
    outcome_document: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Rebuild the canonical projection from Agentd's durable redacted payloads."""

    projection: dict[str, Any] = {}
    run_id: str | None = None
    trace_id: str | None = None
    for event in sorted(
        event_documents,
        key=lambda item: (
            str(item.get("sequence_scope") or ""),
            int(item.get("sequence") or 0),
            str(item.get("event_id") or ""),
        ),
    ):
        run_id = run_id or (
            str(event.get("run_id")) if event.get("run_id") is not None else None
        )
        trace_id = trace_id or (
            str(event.get("trace_id")) if event.get("trace_id") is not None else None
        )
        body = event.get("body")
        body = body if isinstance(body, Mapping) else {}
        attributes = event.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        values = {**attributes, **body}
        name = event.get("name")
        attempt_id = event.get("attempt_id")
        projection.setdefault("run_id", run_id)
        projection.setdefault("trace_id", trace_id)
        if name == "run_created":
            for key in (
                "task_instruction",
                "success_criteria",
                "repository_id",
                "repository",
                "agent_name",
                "agent_version",
            ):
                if values.get(key) is not None:
                    projection[key] = values[key]
        elif name == "classification_completed":
            projection.update(
                raw_classification=values.get("raw_classification"),
                effective_classification=values.get("effective_classification"),
                classification_confidence=values.get("confidence"),
                classification_adjustments=values.get("classification_adjustments", []),
            )
        elif name in {"policy_selected", "retry_selected", "escalation_selected"}:
            for source, target in (
                ("policy_version", "policy_version"),
                ("chosen_backend", "selected_backend"),
                ("chosen_model", "selected_model"),
            ):
                if values.get(source) is not None:
                    projection[target] = values[source]
        elif name in {"attempt_completed", "attempt_failed"}:
            candidates = dict(projection.get("candidate_outcomes") or {})
            candidates[str(attempt_id)] = {
                key: values.get(key)
                for key in (
                    "status",
                    "backend_name",
                    "model",
                    "exit_code",
                    "duration_ms",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "token_accounting_status",
                    "cost_usd",
                    "cost_accounting_status",
                    "file_write_count",
                    "changed_files",
                    "failure_category",
                    "patch_sha256",
                    "patch_bytes",
                    "candidate_configuration",
                    "candidate_configuration_acknowledged",
                    "effective_configuration_sha256",
                )
            }
            projection["candidate_outcomes"] = candidates
        elif name in {"verification_completed", "verification_failed"}:
            candidates = dict(projection.get("candidate_outcomes") or {})
            candidate = dict(candidates.get(str(attempt_id)) or {})
            candidate["verification"] = dict(body)
            candidate["candidate_eligibility"] = values.get(
                "acceptance_eligible", False
            )
            verification_metadata = values.get("metadata")
            verification_metadata = (
                verification_metadata
                if isinstance(verification_metadata, Mapping)
                else {}
            )
            verification_failure = values.get(
                "failure_category"
            ) or verification_metadata.get("failure_category")
            if isinstance(verification_failure, str) and verification_failure:
                candidate["failure_category"] = verification_failure
            candidates[str(attempt_id)] = candidate
            projection["candidate_outcomes"] = candidates
            projection["verification_status"] = values.get("outcome")
            projection["verification_authority"] = values.get("authority_source")
        elif name == "candidate_selected":
            projection.update(
                selected_attempt_id=values.get("selected_attempt_id"),
                selection_reason=values.get("selection_reason"),
                selection_strategy=values.get("selection_strategy"),
                selection_rankings=values.get("rankings", []),
            )
        elif name == "materialization_completed":
            projection.update(
                materialization_status=values.get("materialization_status"),
                changed_files=values.get("changed_files", []),
                patch_digest=values.get("patch_digest"),
            )
        elif name == "artifact_withholding_recorded":
            projection["withheld_artifact_count"] = int(
                projection.get("withheld_artifact_count") or 0
            ) + int(values.get("withheld_artifact_count") or 0)
            projection["withheld_artifact_categories"] = _safe_strings(
                list(projection.get("withheld_artifact_categories") or [])
                + list(values.get("withheld_artifact_categories") or [])
            )
        if name in TERMINAL_EVENTS:
            for key in (
                "status",
                "selected_attempt_id",
                "selected_backend",
                "selected_model",
                "attempt_count",
                "escalation_count",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "token_accounting_status",
                "coding_cost_usd",
                "verifier_cost_usd",
                "total_cost_usd",
                "cost_accounting_status",
                "duration_ms",
                "changed_files",
                "file_write_count",
                "verification_status",
                "materialization_status",
                "terminal_reason",
                "failure_category",
            ):
                if values.get(key) is not None:
                    projection[key] = values[key]
        redaction = values.get("villani_redaction")
        if isinstance(redaction, Mapping):
            previous = projection.get("redaction_status")
            previous = previous if isinstance(previous, Mapping) else {}
            count = int(
                previous.get("redacted_field_count") or previous.get("count") or 0
            ) + int(
                redaction.get("redacted_field_count") or redaction.get("count") or 0
            )
            categories = _safe_strings(
                list(previous.get("categories") or [])
                + list(redaction.get("categories") or [])
            )
            projection.update(
                redaction_applied=count > 0,
                redacted_field_count=count,
                redaction_status={
                    "status": "redacted" if count else "not_redacted",
                    "applied": count > 0,
                    "count": count,
                    "redacted_field_count": count,
                    "categories": categories,
                },
            )
    if outcome_document:
        latency = outcome_document.get("latency_ms")
        if isinstance(latency, int):
            projection["duration_ms"] = latency
        provenance = outcome_document.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        count = provenance.get("withheld_artifact_count")
        categories = provenance.get("withheld_artifact_categories")
        if isinstance(count, int) and count > 0:
            projection["withheld_artifact_count"] = count
        if isinstance(categories, list) and categories:
            projection["withheld_artifact_categories"] = _safe_strings(categories)
    return snapshot_from_projection(projection, run_id=run_id, trace_id=trace_id)


def database_snapshots(
    database_url: str, run_ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    from sqlalchemy import bindparam, create_engine, text

    ids = sorted(set(run_ids))
    if not ids:
        return {}
    statement = text(
        """SELECT id, trace_id, status, canonical_projection
           FROM runs WHERE id IN :run_ids ORDER BY id"""
    ).bindparams(bindparam("run_ids", expanding=True))
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(statement, {"run_ids": ids}).mappings().all()
    finally:
        engine.dispose()
    return {
        str(row["id"]): snapshot_from_projection(
            row["canonical_projection"] or {},
            run_id=str(row["id"]),
            trace_id=str(row["trace_id"]),
            status=str(row["status"]),
        )
        for row in rows
    }


def database_secret_occurrences(database_url: str, secret: str) -> dict[str, int]:
    """Count an exact registered secret in every persisted run-truth document."""

    from sqlalchemy import create_engine, text

    queries = {
        "events.document": "SELECT COUNT(*) FROM events WHERE CAST(document AS TEXT) LIKE :needle",
        "runs.canonical_projection": (
            "SELECT COUNT(*) FROM runs "
            "WHERE CAST(canonical_projection AS TEXT) LIKE :needle"
        ),
        "outcomes.document": (
            "SELECT COUNT(*) FROM outcomes WHERE CAST(document AS TEXT) LIKE :needle"
        ),
        "outcomes.provenance": (
            "SELECT COUNT(*) FROM outcomes WHERE CAST(provenance AS TEXT) LIKE :needle"
        ),
        "artifacts.document": (
            "SELECT COUNT(*) FROM artifacts WHERE CAST(document AS TEXT) LIKE :needle"
        ),
    }
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return {
                name: int(
                    connection.execute(
                        text(statement), {"needle": f"%{secret}%"}
                    ).scalar_one()
                )
                for name, statement in queries.items()
            }
    finally:
        engine.dispose()


def api_snapshot(remote: Mapping[str, Any]) -> dict[str, Any]:
    projection = remote.get("canonical_projection")
    projection = dict(projection) if isinstance(projection, Mapping) else {}
    for key, value in remote.items():
        if key not in {"canonical_projection", "attempts", "outcomes"}:
            projection[key] = value
    attempts = remote.get("attempts")
    attempt_statuses = {
        str(item.get("id")): item.get("status")
        for item in (attempts if isinstance(attempts, list) else [])
        if isinstance(item, Mapping) and item.get("id") is not None
    }
    return snapshot_from_projection(
        projection,
        run_id=str(remote.get("id")),
        trace_id=str(remote.get("trace_id")),
        status=str(remote.get("status")),
        attempt_statuses=attempt_statuses,
    )


def reconcile_sources(sources: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Compare every known value and emit a readable field-by-field diff."""

    fields = sorted({field for snapshot in sources.values() for field in snapshot})
    comparisons: dict[str, Any] = {}
    differences: dict[str, Any] = {}
    for field in fields:
        known = {
            source: _round_numbers(snapshot.get(field))
            for source, snapshot in sources.items()
            if snapshot.get(field) is not None
        }
        known_values = list(known.values())
        values_match = not known_values or all(
            value == known_values[0] for value in known_values[1:]
        )
        comparison = {
            "passed": values_match,
            "known_source_count": len(known),
            "unknown_sources": sorted(set(sources) - set(known)),
            "values": known,
        }
        comparisons[field] = comparison
        if not comparison["passed"]:
            differences[field] = comparison
    required_sources = {
        "local_bundle",
        "agentd_spool",
        "control_plane_database",
        "control_plane_api",
        "villani_web",
        "flight_recorder",
    }
    missing_sources = sorted(required_sources - set(sources))
    return {
        "passed": not differences and not missing_sources,
        "sources": dict(sources),
        "field_comparisons": comparisons,
        "differences": differences,
        "missing_sources": missing_sources,
    }
