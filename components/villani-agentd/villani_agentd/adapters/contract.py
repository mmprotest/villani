from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from villani_ops.closed_loop.protocol_v2 import OutcomeV2, TelemetryEnvelopeV2

from ..platform_process import windows_creation_flags
from ..process import terminate_process_tree


DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    timeout_seconds: float

    @property
    def completed(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True, slots=True)
class DetectionResult:
    adapter: str
    adapter_version: str
    available: bool
    detected_version: str | None
    capabilities: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    source_mode: str = "documented_machine_readable"
    executable_path: str | None = None
    executable_status: str = "not_applicable"
    probe_status: str = "not_required"
    probe_command: tuple[str, ...] = ()
    probe_timeout_seconds: float | None = None
    probe_exit_code: int | None = None
    warning: str | None = None
    runtime_status: str = "not_observed"
    last_successful_use: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "available": self.available,
            "detected_version": self.detected_version,
            "capabilities": list(self.capabilities),
            "missing_capabilities": list(self.missing_capabilities),
            "source_mode": self.source_mode,
            "executable_path": self.executable_path,
            "executable_status": self.executable_status,
            "probe_status": self.probe_status,
            "probe_command": list(self.probe_command),
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "probe_exit_code": self.probe_exit_code,
            "warning": self.warning,
            "runtime_status": self.runtime_status,
            "last_successful_use": self.last_successful_use,
        }


@dataclass(frozen=True, slots=True)
class SensitiveFieldPolicy:
    blocked_field_fragments: tuple[str, ...] = (
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "token",
    )
    redact_secret_shaped_text: bool = True


@dataclass(frozen=True, slots=True)
class AdapterContext:
    run_id: str
    trace_id: str
    root_span_id: str
    observed_at: datetime
    attempt_id: str | None = None
    field_mapping: Mapping[str, str] | None = None


Probe = Callable[[Sequence[str]], ProbeResult | tuple[int, str, str]]


def subprocess_probe(
    command: Sequence[str], timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS
) -> ProbeResult:
    """Run a model-free probe with bounded, process-tree-safe capture."""

    windows = os.name == "nt"
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            list(command),
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=not windows,
            creationflags=windows_creation_flags() if windows else 0,
        )
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        if process is not None:
            terminate_process_tree(process)  # type: ignore[arg-type]
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        else:
            stdout, stderr = "", ""
        return ProbeResult(
            "timed_out",
            process.returncode if process is not None else None,
            stdout or "",
            stderr or "",
            timeout_seconds,
        )
    except OSError:
        return ProbeResult("spawn_failed", None, "", "", timeout_seconds)
    return ProbeResult(
        "completed",
        process.returncode,
        stdout or "",
        stderr or "",
        timeout_seconds,
    )


class StreamingEventParser(Protocol):
    def feed(self, data: bytes | str) -> list[TelemetryEnvelopeV2]: ...
    def finish(self) -> list[TelemetryEnvelopeV2]: ...


class AgentAdapter(Protocol):
    name: str
    version: str

    def detect(self, probe: Probe = subprocess_probe) -> DetectionResult: ...
    def capability_declaration(self) -> tuple[str, ...]: ...
    def construct_command(self, command: Sequence[str]) -> list[str]: ...
    def create_parser(self, context: AdapterContext) -> StreamingEventParser: ...
    def parse_final_outcome(self, run_id: str, exit_code: int, cancelled: bool) -> OutcomeV2: ...
    def cancel(self, process: subprocess.Popen[bytes]) -> None: ...
    def sensitive_field_policy(self) -> SensitiveFieldPolicy: ...
