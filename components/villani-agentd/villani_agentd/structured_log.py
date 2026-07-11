"""Small JSON-lines logger with credential and content redaction."""

from __future__ import annotations

import json
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
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()

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
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
