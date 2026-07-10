"""Preserve redacted Villani Code traces and translate available runtime events."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..event_writer import redact_data
from ..interfaces import RuntimeEvent


_CANONICAL_RUNTIME_TYPES = {
    "model_call_started",
    "model_call_completed",
    "model_call_failed",
    "tool_call_started",
    "tool_call_completed",
    "tool_call_failed",
    "command_started",
    "command_completed",
    "command_failed",
    "file_read",
    "file_write",
}


def _timestamp(row: dict[str, Any]) -> datetime | None:
    value = next(
        (
            row.get(key)
            for key in (
                "timestamp",
                "ts",
                "created_at",
                "createdAt",
                "started_at",
                "startedAt",
                "ended_at",
                "endedAt",
            )
            if row.get(key) is not None
        ),
        None,
    )
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _source_id(row: dict[str, Any], source: str, index: int) -> str:
    for key in ("event_id", "eventId", "tool_call_id", "toolCallId", "id"):
        if row.get(key):
            return str(row[key])
    return f"{source}:{index}"


def _read_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    if not path.is_file():
        return ()
    rows: list[tuple[int, dict[str, Any]]] = []
    for index, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append((index, value))
    return rows


def _event_type(source: str, row: dict[str, Any]) -> str | None:
    explicit = str(
        row.get("event_type") or row.get("eventType") or row.get("event") or ""
    ).lower()
    if explicit in _CANONICAL_RUNTIME_TYPES:
        return explicit
    status = str(row.get("status") or "").lower()
    failed = status in {"failed", "failure", "error"} or (
        row.get("exit_code") is not None and row.get("exit_code") != 0
    )
    if source == "model_responses.jsonl":
        return "model_call_failed" if failed else "model_call_completed"
    if source == "tool_calls.jsonl":
        return "tool_call_failed" if failed else "tool_call_completed"
    if source == "commands.jsonl":
        return "command_failed" if failed else "command_completed"
    if source == "patches.jsonl":
        return "file_write"
    return None


def translate_runtime_events(
    trace_dir: Path,
    *,
    secrets: tuple[str, ...] = (),
) -> tuple[RuntimeEvent, ...]:
    events: list[RuntimeEvent] = []
    for source in (
        "events.jsonl",
        "model_responses.jsonl",
        "tool_calls.jsonl",
        "commands.jsonl",
        "patches.jsonl",
    ):
        for index, row in _read_jsonl(Path(trace_dir) / source):
            timestamp = _timestamp(row)
            if timestamp is None:
                continue
            event_type = _event_type(source, row)
            if event_type is not None:
                events.append(
                    RuntimeEvent(
                        event_type=event_type,
                        timestamp=timestamp,
                        payload={
                            "source_file": source,
                            "source_payload": redact_data(row, secrets=secrets),
                        },
                        source_event_id=_source_id(row, source, index),
                    )
                )
            if source == "tool_calls.jsonl":
                category = str(
                    row.get("tool_category") or row.get("toolCategory") or ""
                ).lower()
                if category in {"file_read", "file_write", "file_mutation"}:
                    file_type = "file_read" if category == "file_read" else "file_write"
                    events.append(
                        RuntimeEvent(
                            event_type=file_type,
                            timestamp=timestamp,
                            payload={
                                "source_file": source,
                                "source_payload": redact_data(row, secrets=secrets),
                            },
                            source_event_id=_source_id(row, source, index),
                        )
                    )
    return tuple(events)


def _redacted_text(path: Path, secrets: tuple[str, ...]) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        try:
            return json.dumps(
                redact_data(json.loads(text), secrets=secrets),
                indent=2,
                ensure_ascii=False,
            )
        except json.JSONDecodeError:
            pass
    if path.suffix == ".jsonl":
        lines: list[str] = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                value = line
            lines.append(
                json.dumps(redact_data(value, secrets=secrets), ensure_ascii=False)
                if not isinstance(value, str)
                else str(redact_data(value, secrets=secrets))
            )
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return str(redact_data(text, secrets=secrets))


def preserve_raw_trace(
    source: Path,
    destination: Path,
    *,
    secrets: tuple[str, ...] = (),
) -> Path:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_redacted_text(path, secrets), encoding="utf-8")
    return destination


def sanitize_artifact_tree(root: Path, *, secrets: tuple[str, ...] = ()) -> None:
    root = Path(root)
    if not root.exists():
        return
    for path in root.rglob("*"):
        if not path.is_file() or "worktree" in path.relative_to(root).parts:
            continue
        path.write_text(_redacted_text(path, secrets), encoding="utf-8")
