"""Crash-conscious JSON snapshot and JSONL primitives for canonical run bundles."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _json_compatible(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync; opening directories is unsupported on Windows."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_json_atomic(path: str | os.PathLike[str], value: Any) -> None:
    """Write a JSON snapshot via a flushed, fsynced same-directory replacement."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def append_jsonl_durable(path: str | os.PathLike[str], value: Any) -> None:
    """Append one compact JSON object and durably flush the completed line."""

    document = _json_compatible(value)
    if not isinstance(document, Mapping):
        raise TypeError("JSONL values must be JSON objects")

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    with destination.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _is_truncated_json_object(line: str, error: json.JSONDecodeError) -> bool:
    """Return true only for an object fragment with unclosed structure/string."""

    stripped = line.lstrip()
    if not stripped.startswith("{"):
        return False

    depth = 0
    in_string = False
    escaped = False
    for character in stripped:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return False
    if not in_string and depth <= 0:
        return False
    if in_string:
        return error.msg.startswith("Unterminated string")

    content = line.rstrip("\r\n")
    if error.pos >= len(content):
        return True
    incomplete_token = content[error.pos:].strip()
    return bool(incomplete_token) and any(
        literal.startswith(incomplete_token) and literal != incomplete_token
        for literal in ("true", "false", "null")
    )


def read_jsonl_tolerant(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read JSONL, ignoring only one structurally truncated final physical line."""

    source = Path(path)
    with source.open("r", encoding="utf-8", newline="") as handle:
        lines = handle.readlines()

    documents: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        is_final = index == len(lines) - 1
        try:
            document = json.loads(line)
        except json.JSONDecodeError as error:
            if is_final and _is_truncated_json_object(line, error):
                break
            raise
        if not isinstance(document, dict):
            raise ValueError(f"JSONL line {index + 1} is not a JSON object")
        documents.append(document)
    return documents


def repair_truncated_final_jsonl(path: str | os.PathLike[str]) -> bool:
    """Remove only one structurally truncated final JSONL fragment.

    Recovery must do this before appending, otherwise a new event would be
    concatenated onto the unterminated object. Complete malformed lines are
    never repaired and continue to fail validation.
    """

    source = Path(path)
    if not source.is_file():
        return False
    raw = source.read_bytes()
    if not raw or raw.endswith((b"\n", b"\r")):
        return False
    line_start = raw.rfind(b"\n") + 1
    fragment = raw[line_start:].decode("utf-8")
    try:
        json.loads(fragment)
    except json.JSONDecodeError as error:
        if not _is_truncated_json_object(fragment, error):
            raise
    else:
        return False
    with source.open("r+b") as handle:
        handle.truncate(line_start)
        handle.flush()
        os.fsync(handle.fileno())
    return True
