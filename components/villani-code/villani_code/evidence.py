from __future__ import annotations

import json
from typing import Any


def parse_command_evidence(content: str) -> list[dict[str, Any]]:
    """Parse command execution artifacts from tool-result content.

    Supports JSON command records and legacy plain-text "command:"/"exit:" output.
    Returns a list of normalized records: {"command": str, "exit": int}.
    """

    text = str(content or "")
    parsed = _parse_json_records(text)
    if parsed:
        return parsed
    return _parse_legacy_records(text)


def normalize_artifact(record: dict[str, Any]) -> str | None:
    command = str(record.get("command", "")).strip()
    exit_code = _coerce_int(record.get("exit"))
    if not command or exit_code is None:
        return None
    return f"{command} (exit={exit_code})"


def _parse_json_records(content: str) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(content)
    except Exception:
        return []

    records: list[dict[str, Any]] = []
    if isinstance(decoded, dict):
        record = _normalize_json_record(decoded)
        if record:
            records.append(record)
        return records

    if isinstance(decoded, list):
        for item in decoded:
            if not isinstance(item, dict):
                continue
            record = _normalize_json_record(item)
            if record:
                records.append(record)
    return records


def _normalize_json_record(item: dict[str, Any]) -> dict[str, Any] | None:
    if "command" not in item:
        return None
    command = str(item.get("command", "")).strip()
    exit_code = _coerce_int(item.get("exit_code", item.get("exit")))
    if not command or exit_code is None:
        return None
    return {"command": command, "exit": exit_code}


def _parse_legacy_records(content: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    command: str | None = None
    exit_code: int | None = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("command:"):
            if command is not None and exit_code is not None:
                records.append({"command": command, "exit": exit_code})
            command = line.split(":", 1)[1].strip()
            exit_code = None
            continue
        if line.lower().startswith("exit:"):
            exit_code = _coerce_int(line.split(":", 1)[1].strip())
            if command is not None and exit_code is not None:
                records.append({"command": command, "exit": exit_code})
                command = None
                exit_code = None
    return records


def _coerce_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
