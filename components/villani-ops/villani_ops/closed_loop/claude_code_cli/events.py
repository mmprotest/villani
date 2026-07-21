"""Translate Claude Code stream-JSON without terminal scraping or hidden reasoning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from villani_ops.closed_loop.event_writer import redact_data
from villani_ops.closed_loop.interfaces import RuntimeEvent


class ClaudeEventParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedClaudeEvents:
    runtime_events: tuple[RuntimeEvent, ...]
    normalized_rows: tuple[dict[str, Any], ...]
    session_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_cost_usd: float | None
    reported_model: str | None
    system_metadata: dict[str, Any]
    final_result: dict[str, Any] | None
    structured_output: Any | None
    structured_output_parse_error: str | None


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


def _without_hidden_reasoning(value: Any) -> Any:
    """Remove thinking/signature payloads while retaining event structure."""

    if isinstance(value, Mapping):
        if str(value.get("type") or "").casefold() in {
            "thinking",
            "redacted_thinking",
        }:
            return {"type": value.get("type"), "omitted": "hidden_reasoning"}
        return {
            str(key): (
                "[OMITTED_HIDDEN_REASONING]"
                if str(key).casefold()
                in {"thinking", "reasoning", "signature", "thinking_signature"}
                else _without_hidden_reasoning(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_hidden_reasoning(item) for item in value]
    return value


def sanitize_claude_event_artifacts(
    paths: tuple[Path, ...], *, secrets: tuple[str, ...] = ()
) -> None:
    """Redact secrets and hidden-reasoning fields from durable provider streams."""

    for path in paths:
        if not path.is_file():
            continue
        output: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                safe = redact_data(line, secrets=secrets)
                output.append(str(safe))
                continue
            safe = redact_data(_without_hidden_reasoning(value), secrets=secrets)
            output.append(json.dumps(safe, ensure_ascii=False, sort_keys=True))
        path.write_text(
            "".join(f"{line}\n" for line in output),
            encoding="utf-8",
            newline="\n",
        )


def _safe_mapping(value: Any, secrets: tuple[str, ...]) -> dict[str, Any]:
    redacted = redact_data(_without_hidden_reasoning(value), secrets=secrets)
    return dict(redacted) if isinstance(redacted, Mapping) else {}


def _usage_counts(value: Any) -> tuple[int | None, int | None]:
    usage = value if isinstance(value, Mapping) else {}
    raw_input = usage.get("input_tokens")
    raw_output = usage.get("output_tokens")
    input_tokens = (
        int(raw_input)
        if isinstance(raw_input, int) and not isinstance(raw_input, bool)
        else None
    )
    output_tokens = (
        int(raw_output)
        if isinstance(raw_output, int) and not isinstance(raw_output, bool)
        else None
    )
    return input_tokens, output_tokens


def parse_claude_events(
    path: Path,
    *,
    started_at: datetime,
    run_id: str,
    attempt_id: str,
    worktree_path: str,
    baseline_sha256: str | None,
    secrets: tuple[str, ...] = (),
) -> ParsedClaudeEvents:
    runtime: list[RuntimeEvent] = []
    normalized: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    session_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    reported_model: str | None = None
    system_metadata: dict[str, Any] = {}
    final_result: dict[str, Any] | None = None
    structured_output: Any | None = None
    structured_output_parse_error: str | None = None

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
        source_id = str(
            raw.get("id")
            or raw.get("uuid")
            or raw.get("request_id")
            or f"claude:{line_number}{suffix}"
        )
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
        return ParsedClaudeEvents(
            (), (), None, None, None, None, None, {}, None, None, None
        )
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8", errors="strict").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            raw_value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ClaudeEventParseError(
                f"Claude stream-JSON line {line_number} is malformed at column {error.colno}"
            ) from error
        if not isinstance(raw_value, dict):
            raise ClaudeEventParseError(
                f"Claude stream-JSON line {line_number} must be an object"
            )
        raw = _without_hidden_reasoning(raw_value)
        event_type = str(raw.get("type") or "")
        subtype = str(raw.get("subtype") or "")

        if event_type == "system" and subtype == "init":
            session_id = str(raw.get("session_id") or "") or None
            reported_model = str(raw.get("model") or "") or None
            system_metadata = _safe_mapping(
                {
                    "session_id": session_id,
                    "model": reported_model,
                    "tools": raw.get("tools") or [],
                    "mcp_servers": raw.get("mcp_servers") or [],
                    "plugins": raw.get("plugins") or [],
                    "agents": raw.get("agents") or [],
                    "permission_mode": raw.get("permissionMode")
                    or raw.get("permission_mode"),
                    "claude_code_version": raw.get("claude_code_version"),
                },
                secrets,
            )
            emit("session_started", system_metadata, raw, line_number)
            continue

        if event_type in {"assistant", "user"}:
            body = raw.get("message")
            message = body if isinstance(body, Mapping) else {}
            message_input, message_output = _usage_counts(message.get("usage"))
            if message_input is not None or message_output is not None:
                input_tokens = (
                    message_input if message_input is not None else input_tokens
                )
                output_tokens = (
                    message_output if message_output is not None else output_tokens
                )
                emit(
                    "usage_update",
                    {
                        "input_tokens": message_input,
                        "output_tokens": message_output,
                    },
                    raw,
                    line_number,
                    suffix=":usage",
                )
            content = message.get("content")
            if not isinstance(content, list):
                content = (
                    raw.get("content") if isinstance(raw.get("content"), list) else []
                )
            emitted = False
            for index, raw_item in enumerate(content):
                if not isinstance(raw_item, Mapping):
                    continue
                item = _without_hidden_reasoning(raw_item)
                kind = str(item.get("type") or "")
                item_id = str(
                    item.get("id")
                    or item.get("tool_use_id")
                    or f"claude:{line_number}:{index}"
                )
                if kind in {"thinking", "redacted_thinking"}:
                    continue
                if kind == "text":
                    emit(
                        "agent_message",
                        {"text": item.get("text")},
                        {**raw, "id": item_id},
                        line_number,
                        suffix=f":{index}",
                    )
                    emitted = True
                    continue
                if kind == "tool_use":
                    name = str(item.get("name") or "unknown")
                    tool_names[item_id] = name
                    raw_input = item.get("input")
                    tool_input = raw_input if isinstance(raw_input, Mapping) else {}
                    lowered = name.casefold()
                    tool_payload: dict[str, Any] = {
                        "tool_call_id": item_id,
                        "tool": name,
                        "run_id": run_id,
                        "attempt_id": attempt_id,
                        "worktree_path": worktree_path,
                        "baseline_sha256": baseline_sha256,
                    }
                    if lowered == "bash":
                        name_out = "command_started"
                        payload = {
                            **tool_payload,
                            "command": tool_input.get("command"),
                            "command_role": "unknown",
                            "candidate_state": "post_mutation",
                        }
                    elif lowered == "read":
                        name_out = "file_read"
                        payload = {
                            **tool_payload,
                            "path": tool_input.get("file_path")
                            or tool_input.get("path"),
                        }
                    elif lowered in {"edit", "write", "notebookedit"}:
                        name_out = "file_write_started"
                        payload = {
                            **tool_payload,
                            "path": tool_input.get("file_path")
                            or tool_input.get("path"),
                            "mutation": True,
                        }
                    elif lowered in {"agent", "task"}:
                        name_out = "subagent_started"
                        payload = tool_payload
                    else:
                        name_out = "tool_call_started"
                        payload = tool_payload
                    emit(
                        name_out,
                        payload,
                        {**raw, "id": item_id},
                        line_number,
                        suffix=f":{index}",
                    )
                    emitted = True
                    continue
                if kind == "tool_result":
                    tool_id = str(item.get("tool_use_id") or item_id)
                    name = tool_names.get(tool_id, "unknown")
                    lowered = name.casefold()
                    is_error = bool(item.get("is_error"))
                    result_payload: dict[str, Any] = {
                        "tool_call_id": tool_id,
                        "tool": name,
                        "is_error": is_error,
                        "status": "failed" if is_error else "completed",
                        "output": item.get("content"),
                    }
                    if lowered == "bash":
                        name_out = "command_failed" if is_error else "command_completed"
                    elif lowered == "read":
                        name_out = "file_read"
                    elif lowered in {"edit", "write", "notebookedit"}:
                        name_out = "file_write"
                        result_payload["mutation"] = True
                    elif lowered in {"agent", "task"}:
                        name_out = "subagent_completed"
                    else:
                        name_out = "tool_call_completed"
                    emit(
                        name_out,
                        result_payload,
                        {**raw, "id": f"{tool_id}:result"},
                        line_number,
                        suffix=f":{index}",
                    )
                    emitted = True
            if not emitted:
                emit(
                    "claude_code.raw_event",
                    {
                        "namespace": "claude_code.raw",
                        "raw_type": event_type,
                        "event": raw,
                    },
                    raw,
                    line_number,
                )
            continue

        if event_type in {"rate_limit_event", "retry"}:
            emit("retry", raw, raw, line_number)
            continue
        if event_type == "warning":
            emit("warning", {"message": raw.get("message")}, raw, line_number)
            continue
        if event_type in {"error", "provider_error"}:
            emit(
                "provider_error",
                {"error": raw.get("error") or raw.get("message")},
                raw,
                line_number,
            )
            continue
        if event_type in {"cancelled", "cancellation"}:
            emit("cancellation", raw, raw, line_number)
            continue
        if event_type == "result":
            final_result = _safe_mapping(raw, secrets)
            result_input, result_output = _usage_counts(raw.get("usage"))
            input_tokens = result_input if result_input is not None else input_tokens
            output_tokens = (
                result_output if result_output is not None else output_tokens
            )
            raw_cost = raw.get("total_cost_usd")
            if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
                total_cost_usd = float(raw_cost)
            if result_input is not None or result_output is not None:
                emit(
                    "usage_update",
                    {
                        "input_tokens": result_input,
                        "output_tokens": result_output,
                        "total_cost_usd": total_cost_usd,
                    },
                    raw,
                    line_number,
                    suffix=":usage",
                )
            structured_output = raw.get("structured_output")
            if structured_output is None and isinstance(raw.get("result"), str):
                rendered = str(raw["result"]).strip()
                if rendered.startswith(("{", "[")):
                    try:
                        structured_output = json.loads(rendered)
                    except json.JSONDecodeError as error:
                        structured_output_parse_error = (
                            f"result JSON is malformed at column {error.colno}"
                        )
            failed = bool(raw.get("is_error")) or subtype in {"error", "failed"}
            emit(
                "provider_error" if failed else "turn_completed",
                {
                    "subtype": subtype,
                    "is_error": bool(raw.get("is_error")),
                    "num_turns": raw.get("num_turns"),
                },
                raw,
                line_number,
            )
            continue

        emit(
            "claude_code.raw_event",
            {
                "namespace": "claude_code.raw",
                "raw_type": event_type or "unknown",
                "event": raw,
            },
            raw,
            line_number,
        )

    return ParsedClaudeEvents(
        tuple(runtime),
        tuple(normalized),
        session_id,
        input_tokens,
        output_tokens,
        total_cost_usd,
        reported_model,
        system_metadata,
        final_result,
        structured_output,
        structured_output_parse_error,
    )


__all__ = [
    "ClaudeEventParseError",
    "ParsedClaudeEvents",
    "parse_claude_events",
    "sanitize_claude_event_artifacts",
]
