"""Ephemeral secret acquisition and selected-process injection."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from .models import SecretRequest


_REGISTERED: set[str] = set()
_REGISTERED_LOCK = threading.RLock()


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        exit_code = wintypes.DWORD()
        try:
            return bool(
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ) and (exit_code.value == 259)
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    return True


def _scavenge_crashed_secret_roots() -> None:
    root = Path(tempfile.gettempdir())
    try:
        candidates = list(root.glob("villani-secret-*-*"))
    except OSError:
        return
    for candidate in candidates:
        parts = candidate.name.split("-", 3)
        try:
            pid = int(parts[2])
        except (IndexError, ValueError):
            continue
        if not _process_alive(pid):
            shutil.rmtree(candidate, ignore_errors=True)


def register_secret_values(values: Sequence[str]) -> None:
    with _REGISTERED_LOCK:
        _REGISTERED.update(value for value in values if value)


def registered_secret_values() -> tuple[str, ...]:
    configured_names = {
        name.strip()
        for name in os.environ.get("VILLANI_REGISTERED_SECRET_ENV_VARS", "").split(",")
        if name.strip()
    }
    configured_values = {
        value
        for name in configured_names
        if (value := os.environ.get(name))
    }
    with _REGISTERED_LOCK:
        return tuple(
            sorted(_REGISTERED | configured_values, key=len, reverse=True)
        )


@dataclass(slots=True)
class SecretLease:
    values: tuple[str, ...]
    environment: dict[str, str]
    files: dict[str, Path]
    descriptions: list[dict[str, str]]
    temporary_root: Path | None = None
    _cleaned: bool = field(default=False, init=False)

    def durable_report(self) -> dict[str, object]:
        return {
            "schema_version": "villani.secret_lease.v1",
            "secrets": list(self.descriptions),
            "values_persisted": False,
            "temporary_files_cleaned": self._cleaned,
        }

    def cleanup(self) -> None:
        if self._cleaned:
            return
        for path in self.files.values():
            try:
                size = path.stat().st_size
                with path.open("r+b", buffering=0) as handle:
                    handle.write(b"\0" * min(size, 1_048_576))
            except OSError:
                pass
        if self.temporary_root is not None:
            shutil.rmtree(self.temporary_root, ignore_errors=True)
        self.environment.clear()
        self.files.clear()
        self._cleaned = True


class SecretBroker(Protocol):
    def acquire(self, requests: Sequence[SecretRequest]) -> SecretLease: ...


class LocalSecretBroker:
    """Broker environment and shell-free command sources into one ephemeral lease."""

    def __init__(
        self,
        *,
        source_environment: Mapping[str, str] | None = None,
        command_timeout_seconds: int = 30,
        command_output_bytes: int = 65_536,
    ) -> None:
        self.source_environment = dict(
            os.environ if source_environment is None else source_environment
        )
        self.command_timeout_seconds = command_timeout_seconds
        self.command_output_bytes = command_output_bytes
        _scavenge_crashed_secret_roots()

    def _value(self, request: SecretRequest) -> str | None:
        if request.source == "environment":
            value = self.source_environment.get(
                request.environment_variable or request.name
            )
            if (
                value is not None
                and len(value.encode("utf-8")) > self.command_output_bytes
            ):
                raise RuntimeError(f"secret value exceeded limit for {request.name}")
            return value
        try:
            result = subprocess.run(
                list(request.command_argv or []),
                shell=False,
                env=self.source_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                f"secret provider command failed for {request.name}: {error.__class__.__name__}"
            ) from None
        if result.returncode != 0:
            raise RuntimeError(
                f"secret provider command failed for {request.name} with exit code {result.returncode}"
            )
        if len(result.stdout) > self.command_output_bytes:
            raise RuntimeError(
                f"secret provider command output exceeded limit for {request.name}"
            )
        return result.stdout.decode("utf-8", errors="strict").rstrip("\r\n")

    def acquire(self, requests: Sequence[SecretRequest]) -> SecretLease:
        values: list[str] = []
        environment: dict[str, str] = {}
        files: dict[str, Path] = {}
        descriptions: list[dict[str, str]] = []
        temporary_root: Path | None = None
        try:
            for request in requests:
                value = self._value(request)
                if not value:
                    if request.required:
                        raise RuntimeError(
                            f"required secret {request.name} is unavailable"
                        )
                    continue
                values.append(value)
                target_name = request.target_name or request.name
                if request.target == "environment":
                    if "\0" in value:
                        raise RuntimeError(
                            f"secret value for {request.name} cannot be injected into an environment variable"
                        )
                    environment[target_name] = value
                else:
                    if temporary_root is None:
                        temporary_root = Path(
                            tempfile.mkdtemp(prefix=f"villani-secret-{os.getpid()}-")
                        )
                        try:
                            temporary_root.chmod(0o700)
                        except OSError:
                            pass
                    path = temporary_root / target_name
                    path.write_bytes(value.encode("utf-8"))
                    try:
                        path.chmod(0o600)
                    except OSError:
                        pass
                    files[target_name] = path
                descriptions.append(
                    {
                        "name": request.name,
                        "source": request.source,
                        "target": request.target,
                        "target_name": target_name,
                    }
                )
            register_secret_values(values)
            return SecretLease(
                values=tuple(values),
                environment=environment,
                files=files,
                descriptions=descriptions,
                temporary_root=temporary_root,
            )
        except BaseException:
            SecretLease(
                values=tuple(values),
                environment=environment,
                files=files,
                descriptions=descriptions,
                temporary_root=temporary_root,
            ).cleanup()
            raise
