"""Thread-safe idempotent cancellation signal for CLI invocations."""

from __future__ import annotations

import asyncio
import threading

from .models import CliCancellationOrigin


class CliCancellationHandle:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._origin: CliCancellationOrigin | None = None
        self._request_count = 0

    def cancel(
        self, origin: CliCancellationOrigin = CliCancellationOrigin.USER
    ) -> bool:
        """Set cancellation once and return whether this call won the race."""

        with self._lock:
            self._request_count += 1
            if self._event.is_set():
                return False
            self._origin = origin
            self._event.set()
            return True

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def origin(self) -> CliCancellationOrigin | None:
        with self._lock:
            return self._origin

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    async def wait(self, poll_seconds: float = 0.01) -> CliCancellationOrigin:
        while not self._event.is_set():
            await asyncio.sleep(poll_seconds)
        return self.origin or CliCancellationOrigin.USER


__all__ = ["CliCancellationHandle"]
