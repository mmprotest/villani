"""Crash-conscious storage for one canonical closed-loop run bundle."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .durable_io import append_jsonl_durable, write_json_atomic
from .protocol import EventEnvelope, StrictProtocolModel
from .schema_validation import validate_protocol_document


class RunStoreError(RuntimeError):
    """Raised when the canonical run bundle cannot be safely persisted."""


class RunStore:
    """Own all mutable durable I/O and event sequence allocation for one run."""

    def __init__(self, runs_root: str | Path, run_id: str) -> None:
        self.runs_root = Path(runs_root)
        self.run_id = run_id
        self.run_directory = self.runs_root / run_id
        self._lock = threading.RLock()
        self._last_sequence = 0

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._last_sequence

    def create(self) -> None:
        try:
            self.runs_root.mkdir(parents=True, exist_ok=True)
            self.run_directory.mkdir(exist_ok=False)
            (self.run_directory / "attempts").mkdir()
            (self.run_directory / "verification").mkdir()
        except OSError as error:
            raise RunStoreError(f"cannot create run directory: {error}") from error

    def open_existing(self, *, last_sequence: int) -> None:
        if not self.run_directory.is_dir():
            raise RunStoreError(f"run directory does not exist: {self.run_directory}")
        if last_sequence < 1:
            raise RunStoreError("an existing run must contain at least one event")
        with self._lock:
            self._last_sequence = last_sequence

    @contextmanager
    def recovery_lock(self):
        """Acquire a non-blocking OS-backed lock scoped to this run identity."""

        lock_root = self.runs_root / ".locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / f"{self.run_id}.lock"
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise RunStoreError(
                        f"recovery lock is already held for run {self.run_id}"
                    ) from error
            else:  # pragma: no cover - exercised by Linux CI
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise RunStoreError(
                        f"recovery lock is already held for run {self.run_id}"
                    ) from error
            locked = True
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - exercised by Linux CI
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    def _path(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RunStoreError(f"run artifact path must be relative: {relative}")
        return self.run_directory / relative

    def write_protocol(
        self,
        relative_path: str | Path,
        value: StrictProtocolModel | Mapping[str, Any],
    ) -> None:
        document = (
            value.model_dump(mode="json")
            if isinstance(value, StrictProtocolModel)
            else dict(value)
        )
        try:
            validate_protocol_document(document)
            with self._lock:
                write_json_atomic(self._path(relative_path), document)
        except Exception as error:
            if isinstance(error, RunStoreError):
                raise
            raise RunStoreError(
                f"cannot persist protocol snapshot {relative_path}: {error}"
            ) from error

    def write_json(self, relative_path: str | Path, value: Any) -> None:
        try:
            with self._lock:
                write_json_atomic(self._path(relative_path), value)
        except Exception as error:
            if isinstance(error, RunStoreError):
                raise
            raise RunStoreError(
                f"cannot persist JSON artifact {relative_path}: {error}"
            ) from error

    def write_text(self, relative_path: str | Path, value: str) -> None:
        destination = self._path(relative_path)
        try:
            with self._lock:
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    dir=destination.parent,
                )
                temporary_path = Path(temporary_name)
                try:
                    with os.fdopen(
                        descriptor, "w", encoding="utf-8", newline="\n"
                    ) as handle:
                        handle.write(value)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary_path, destination)
                except BaseException:
                    temporary_path.unlink(missing_ok=True)
                    raise
        except Exception as error:
            if isinstance(error, RunStoreError):
                raise
            raise RunStoreError(
                f"cannot persist text artifact {relative_path}: {error}"
            ) from error

    def append_policy_decision(self, value: StrictProtocolModel) -> None:
        document = value.model_dump(mode="json")
        try:
            validate_protocol_document(document)
            with self._lock:
                append_jsonl_durable(self._path("policy_decisions.jsonl"), document)
        except Exception as error:
            if isinstance(error, RunStoreError):
                raise
            raise RunStoreError(f"cannot append policy decision: {error}") from error

    def append_event(
        self,
        *,
        timestamp: datetime,
        trace_id: str,
        attempt_id: str | None,
        parent_event_id: str | None,
        source: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> EventEnvelope:
        """Allocate and append an event atomically under the run-store lock."""

        try:
            with self._lock:
                sequence = self._last_sequence + 1
                event = EventEnvelope(
                    schema_version="villani.event.v1",
                    event_id=f"evt_{sequence:06d}",
                    sequence=sequence,
                    timestamp=timestamp,
                    trace_id=trace_id,
                    run_id=self.run_id,
                    attempt_id=attempt_id,
                    parent_event_id=parent_event_id,
                    source=source,
                    event_type=event_type,
                    payload=dict(payload),
                )
                document = event.model_dump(mode="json")
                validate_protocol_document(document)
                append_jsonl_durable(self._path("events.jsonl"), document)
                self._last_sequence = sequence
                return event
        except Exception as error:
            if isinstance(error, RunStoreError):
                raise
            raise RunStoreError(f"cannot append event {event_type}: {error}") from error


def json_safe_copy(value: Any) -> Any:
    """Return a detached JSON-compatible copy or raise for illegal dependency data."""

    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
