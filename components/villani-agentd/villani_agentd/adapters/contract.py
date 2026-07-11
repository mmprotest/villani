from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from villani_ops.closed_loop.protocol_v2 import OutcomeV2, TelemetryEnvelopeV2


@dataclass(frozen=True, slots=True)
class DetectionResult:
    adapter: str
    adapter_version: str
    available: bool
    detected_version: str | None
    capabilities: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    source_mode: str = "documented_machine_readable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "available": self.available,
            "detected_version": self.detected_version,
            "capabilities": list(self.capabilities),
            "missing_capabilities": list(self.missing_capabilities),
            "source_mode": self.source_mode,
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


Probe = Callable[[Sequence[str]], tuple[int, str, str]]


def subprocess_probe(command: Sequence[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            list(command), shell=False, text=True, capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, "", type(error).__name__
    return result.returncode, result.stdout, result.stderr


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
