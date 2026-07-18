"""Small JSON-lines logger with credential and content redaction."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SECRET = re.compile(r"(?i)(bearer\s+\S+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+)")
_BLOCKED_KEYS = {"authorization", "token", "artifact_content", "content_base64", "bytes"}


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _BLOCKED_KEYS else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return _SECRET.sub("[REDACTED]", value)[:1000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


class StructuredLogger:
    def __init__(
        self,
        path: Path | None = None,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        retained_backups: int = 3,
    ) -> None:
        if max_bytes < 1024:
            raise ValueError("max_bytes must be at least 1024")
        if retained_backups < 1:
            raise ValueError("retained_backups must be at least 1")
        self.path = path
        self.max_bytes = max_bytes
        self.retained_backups = retained_backups
        self._lock = threading.Lock()

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        assert self.path is not None
        try:
            current_bytes = self.path.stat().st_size
        except FileNotFoundError:
            return
        if current_bytes + incoming_bytes <= self.max_bytes:
            return
        for index in range(self.retained_backups, 1, -1):
            source = self.path.with_name(f"{self.path.name}.{index - 1}")
            destination = self.path.with_name(f"{self.path.name}.{index}")
            if source.is_file():
                os.replace(source, destination)
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def emit(self, level: str, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": level,
            "event": event,
            **_safe(fields),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            self._rotate_if_needed(len(encoded.encode("utf-8")))
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
