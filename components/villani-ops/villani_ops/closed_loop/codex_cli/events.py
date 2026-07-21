"""Translate documented Codex JSONL events without exposing hidden reasoning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from villani_ops.closed_loop.event_writer import redact_data
from villani_ops.closed_loop.interfaces import RuntimeEvent


class CodexEventParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedCodexEvents:
    runtime_events: tuple[RuntimeEvent, ...]
    normalized_rows: tuple[dict[str, Any], ...]
    thread_id: str | None
    input_tokens: int | None
    output_tokens: int | None


def _event_timestamp(value: Mapping[str, Any], fallback: datetime) -> datetime:
    for key in ("timestamp", "created_at", "started_at", "completed_at"):
        raw = value.get(key)
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    return fallback


def _safe_mapping(value: Any, secrets: tuple[str, ...]) -> dict[str, Any]:
    redacted = redact_data(value, secrets=secrets)
    return dict(redacted) if isinstance(redacted, Mapping) else {}


def parse_codex_events(
    path: Path,
    *,
    started_at: datetime,
    run_id: str,
    attempt_id: str,
    worktree_path: str,
    baseline_sha256: str | None,
    secrets: tuple[str, ...] = (),
) -> ParsedCodexEvents:
    runtime: list[RuntimeEvent] = []
    normalized: list[dict[str, Any]] = []
    thread_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def emit(
        name: str,
        payload: Mapping[str, Any],
        raw: Mapping[str, Any],
        line_number: int,
        *,
        suffix: str = "",
    ) -> None:
        timestamp = _event_timestamp(
            raw, started_at + timedelta(microseconds=len(runtime))
        )
        item = raw.get("item")
        item_id = item.get("id") if isinstance(item, Mapping) else None
        source_id = str(raw.get("id") or item_id or f"codex:{line_number}{suffix}")
        safe_payload = _safe_mapping(payload, secrets)
        runtime.append(
            RuntimeEvent(
                event_type=name,
                timestamp=timestamp,
                payload=safe_payload,
                source_event_id=source_id,
            )
        )
        normalized.append(
            {
                "sequence": len(normalized) + 1,
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "event_type": name,
                "source_event_id": source_id,
                "payload": safe_payload,
            }
        )

    if not Path(path).is_file():
        return ParsedCodexEvents((), (), None, None, None)
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8", errors="strict").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            raw_value = json.loads(line)
        except json.JSONDecodeError as error:
            raise CodexEventParseError(
                f"Codex JSONL line {line_number} is malformed at column {error.colno}"
            ) from error
        if not isinstance(raw_value, dict):
            raise CodexEventParseError(
                f"Codex JSONL line {line_number} must be an object"
            )
        raw = raw_value
        event_type = str(raw.get("type") or "")
        if event_type == "thread.started":
            thread_id = str(raw.get("thread_id") or raw.get("threadId") or "") or None
            emit("session_started", {"thread_id": thread_id}, raw, line_number)
            continue
        if event_type == "turn.started":
            emit("turn_started", {}, raw, line_number)
            continue
        if event_type == "turn.completed":
            raw_usage = raw.get("usage")
            usage: Mapping[str, Any] = (
                raw_usage if isinstance(raw_usage, Mapping) else {}
            )
            if isinstance(usage.get("input_tokens"), int):
                input_tokens = int(usage["input_tokens"])
            if isinstance(usage.get("output_tokens"), int):
                output_tokens = int(usage["output_tokens"])
            if usage:
                emit("usage_update", usage, raw, line_number, suffix=":usage")
            emit("turn_completed", {}, raw, line_number)
            continue
        if event_type == "turn.failed":
            emit("turn_failed", {"error": raw.get("error")}, raw, line_number)
            continue
        if event_type == "error":
            emit(
                "provider_error",
                {"error": raw.get("message") or raw.get("error")},
                raw,
                line_number,
            )
            continue
        if event_type == "warning":
            emit("warning", {"message": raw.get("message")}, raw, line_number)
            continue

        item = raw.get("item") if isinstance(raw.get("item"), Mapping) else None
        if event_type.startswith("item.") and item is not None:
            phase = event_type.split(".", 1)[1]
            item_type = str(item.get("type") or "")
            if item_type == "agent_message" and phase == "completed":
                emit("agent_message", {"text": item.get("text")}, raw, line_number)
                continue
            if item_type == "reasoning" and phase == "completed":
                summary = item.get("summary") or item.get("text")
                emit(
                    "reasoning_summary",
                    {"summary": summary, "source_visibility": "codex_jsonl"},
                    raw,
                    line_number,
                )
                continue
            if item_type in {"plan", "plan_update", "todo_list"}:
                emit(
                    "plan_update",
                    {"phase": phase, "plan": item.get("items") or item.get("text")},
                    raw,
                    line_number,
                )
                continue
            if item_type == "command_execution":
                base = {
                    "command": item.get("command"),
                    "command_role": "unknown",
                    "exit_code": item.get("exit_code"),
                    "status": item.get("status"),
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "worktree_path": worktree_path,
                    "baseline_sha256": baseline_sha256,
                    "candidate_state": "post_mutation",
                }
                output = item.get("aggregated_output") or item.get("output")
                if phase == "started":
                    emit("command_started", base, raw, line_number)
                elif phase == "updated" and output is not None:
                    emit("command_output", {**base, "output": output}, raw, line_number)
                else:
                    name = (
                        "command_failed"
                        if item.get("status") == "failed"
                        or item.get("exit_code") not in {None, 0}
                        else "command_completed"
                    )
                    emit(name, {**base, "output": output}, raw, line_number)
                continue
            if item_type in {"file_change", "file_changes"}:
                emit(
                    "file_write",
                    {"phase": phase, "changes": item.get("changes"), "mutation": True},
                    raw,
                    line_number,
                )
                continue
            if item_type in {"mcp_tool_call", "tool_call", "web_search"}:
                name = (
                    "tool_call_started" if phase == "started" else "tool_call_completed"
                )
                emit(
                    name,
                    {"phase": phase, "tool": item_type, "item": item},
                    raw,
                    line_number,
                )
                continue

        emit(
            "codex.raw_event",
            {
                "namespace": "codex.raw",
                "raw_type": event_type or "unknown",
                "event": raw,
            },
            raw,
            line_number,
        )

    return ParsedCodexEvents(
        tuple(runtime), tuple(normalized), thread_id, input_tokens, output_tokens
    )


__all__ = ["CodexEventParseError", "ParsedCodexEvents", "parse_codex_events"]
