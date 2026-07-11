from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from pydantic import ValidationError
from villani_ops.closed_loop.protocol_v2 import OutcomeV2, TelemetryEnvelopeV2

from ..process import terminate_process_tree
from .contract import AdapterContext, DetectionResult, Probe, SensitiveFieldPolicy, subprocess_probe
from .normalize import normalize_record, redact, stable_hex


class JsonLineParser:
    def __init__(self, adapter: "BaseAdapter", context: AdapterContext) -> None:
        self.adapter = adapter
        self.context = context
        self.buffer = ""
        self.sequence = 0
        self._seen: dict[str, list[str]] = {}
        self._span_by_native_id: dict[str, str] = {}

    def feed(self, data: bytes | str) -> list[TelemetryEnvelopeV2]:
        self.buffer += data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        parts = self.buffer.split("\n")
        self.buffer = parts.pop()
        output: list[TelemetryEnvelopeV2] = []
        for line in parts:
            output.extend(self._line(line, truncated=False))
        return output

    def finish(self) -> list[TelemetryEnvelopeV2]:
        if not self.buffer.strip():
            self.buffer = ""
            return []
        line, self.buffer = self.buffer, ""
        return self._line(line, truncated=True)

    def _line(self, line: str, *, truncated: bool) -> list[TelemetryEnvelopeV2]:
        if not line.strip():
            return []
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("record is not an object")
        except (json.JSONDecodeError, ValueError) as error:
            value = {
                "type": "adapter_parse_error",
                "error": type(error).__name__,
                "malformed_record": True,
                "truncated_final_record": truncated,
                "line_bytes": len(line.encode("utf-8")),
            }
        if (
            self.adapter.name == "generic-jsonl"
            and value.get("schema_version") == "villani.telemetry_envelope.v2"
        ):
            try:
                envelope = TelemetryEnvelopeV2.model_validate(value)
                policy = self.adapter.sensitive_field_policy()
                return [
                    envelope.model_copy(
                        update={
                            "attributes": redact(envelope.attributes, policy),
                            "body": redact(envelope.body, policy),
                        }
                    )
                ]
            except ValidationError as error:
                value = {
                    "type": "adapter_parse_error",
                    "error": "invalid_v2_envelope",
                    "reason": error.errors()[0]["type"],
                }
        mapped = self.adapter.map_record(value, self.context.field_mapping)
        native_id = self.adapter.native_id(mapped)
        digest = stable_hex(json.dumps(mapped, sort_keys=True, separators=(",", ":")), length=32)
        revisions = self._seen.setdefault(native_id, [])
        if digest in revisions:
            return []
        revision = len(revisions)
        revisions.append(digest)
        self.sequence += 1
        parent_native = self.adapter.parent_native_id(mapped)
        parent_span = self._span_by_native_id.get(parent_native) if parent_native else None
        event = normalize_record(
            self.adapter.name,
            self.adapter.version,
            self.context,
            mapped,
            sequence=self.sequence,
            native_id=native_id,
            revision=revision,
            policy=self.adapter.sensitive_field_policy(),
            provider=self.adapter.provider(mapped),
            parent_span_id=parent_span,
        )
        self._span_by_native_id[native_id] = event.span_id
        return [event]


class BaseAdapter:
    name = "base"
    version = "1.0"
    executable: str | None = None
    required_help_terms: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = (
        "streaming_events",
        "final_outcome",
        "cancellation",
        "sensitive_field_redaction",
    )

    def capability_declaration(self) -> tuple[str, ...]:
        return self.capabilities

    def detect(self, probe: Probe = subprocess_probe) -> DetectionResult:
        if self.executable is None:
            return DetectionResult(
                self.name, self.version, True, self.version, self.capabilities, ()
            )
        path = shutil.which(self.executable)
        if path is None:
            return DetectionResult(
                self.name, self.version, False, None, self.capabilities, ("executable",)
            )
        _code, stdout, stderr = probe([path, "--version"])
        detected = (stdout.strip() or stderr.strip() or "unknown").splitlines()[0]
        _code, help_out, help_err = probe(self.help_command(path))
        help_text = help_out + "\n" + help_err
        missing = tuple(
            dict.fromkeys(
                self.missing_capability(term)
                for term in self.required_help_terms
                if term not in help_text
            )
        )
        return DetectionResult(
            self.name, self.version, not missing, detected, self.capabilities, missing
        )

    def help_command(self, executable: str) -> list[str]:
        return [executable, "--help"]

    def missing_capability(self, _term: str) -> str:
        return "machine_readable_output"

    def construct_command(self, command: Sequence[str]) -> list[str]:
        return list(command)

    def create_parser(self, context: AdapterContext) -> JsonLineParser:
        return JsonLineParser(self, context)

    def native_id(self, record: Mapping[str, Any]) -> str:
        for key in (
            "event_id",
            "id",
            "uuid",
            "request_id",
            "tool_call_id",
            "tool_use_id",
            "thread_id",
            "session_id",
        ):
            value = record.get(key)
            if value is not None and str(value):
                return str(value)
        item = record.get("item")
        if isinstance(item, Mapping) and item.get("id") is not None:
            return str(item["id"])
        payload = record.get("payload")
        if isinstance(payload, Mapping):
            for key in ("request_id", "tool_call_id", "event_id"):
                if payload.get(key) is not None:
                    return str(payload[key])
        return stable_hex(json.dumps(record, sort_keys=True, separators=(",", ":")), length=24)

    def parent_native_id(self, record: Mapping[str, Any]) -> str | None:
        raw_payload = record.get("payload")
        payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else record
        for key in ("parent_id", "parent_event_id", "tool_use_id", "tool_call_id"):
            value = payload.get(key)
            if value is not None and str(value) != self.native_id(record):
                return str(value)
        return None

    def provider(self, record: Mapping[str, Any]) -> str:
        value = record.get("provider") or record.get("system")
        payload = record.get("payload")
        if not value and isinstance(payload, Mapping):
            value = payload.get("provider") or payload.get("model_provider")
        return str(value or self.name)

    def map_record(
        self, record: Mapping[str, Any], mapping: Mapping[str, str] | None
    ) -> dict[str, Any]:
        output = dict(record)
        if mapping:
            for target, source in mapping.items():
                current: Any = record
                for part in source.split("."):
                    current = current.get(part) if isinstance(current, Mapping) else None
                if current is not None:
                    output[target] = current
        return output

    def parse_final_outcome(self, run_id: str, exit_code: int, cancelled: bool) -> OutcomeV2:
        return OutcomeV2(
            schema_version="villani.outcome.v2",
            run_id=run_id,
            attempt_id=None,
            verification_status=None,
            accepted=None,
            materialized=None,
            merged=None,
            reverted=None,
            ci_state="cancelled" if cancelled else None,
            developer_disposition=None,
            defect_association=None,
            cost=None,
            currency=None,
            cost_accounting_status="unknown",
            latency_ms=None,
            latency_accounting_status="unknown",
            provenance_status="recorded",
            provenance={"adapter": self.name, "exit_code": exit_code},
        )

    def cancel(self, process: subprocess.Popen[bytes]) -> None:
        terminate_process_tree(process)

    def sensitive_field_policy(self) -> SensitiveFieldPolicy:
        return SensitiveFieldPolicy()


class GenericProcessAdapter(BaseAdapter):
    name = "generic-process"
    capabilities = BaseAdapter.capabilities + (
        "bounded_stdout",
        "bounded_stderr",
        "process_lifecycle",
    )


class GenericJsonlAdapter(BaseAdapter):
    name = "generic-jsonl"
    capabilities = BaseAdapter.capabilities + ("villani_v2_envelopes", "configured_field_mapping")


class VillaniCodeAdapter(BaseAdapter):
    name = "villani-code"
    executable = "villani-code"
    required_help_terms = ("--debug",)
    capabilities = BaseAdapter.capabilities + ("native_runtime_events", "native_debug_events")

    def missing_capability(self, _term: str) -> str:
        return "native_debug_events"


class CodexAdapter(BaseAdapter):
    name = "codex"
    executable = "codex"
    required_help_terms = ("--json",)
    capabilities = BaseAdapter.capabilities + ("documented_exec_json",)

    def help_command(self, executable: str) -> list[str]:
        return [executable, "exec", "--help"]

    def missing_capability(self, _term: str) -> str:
        return "documented_exec_json"

    def construct_command(self, command: Sequence[str]) -> list[str]:
        return [self.executable or "codex", "exec", "--json", *command]

    def provider(self, record: Mapping[str, Any]) -> str:
        return str(record.get("provider") or "openai")


class ClaudeCodeAdapter(BaseAdapter):
    name = "claude-code"
    executable = "claude"
    required_help_terms = ("--output-format", "stream-json")
    capabilities = BaseAdapter.capabilities + ("documented_stream_json",)

    def missing_capability(self, _term: str) -> str:
        return "documented_stream_json"

    def construct_command(self, command: Sequence[str]) -> list[str]:
        return [
            self.executable or "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            *command,
        ]

    def provider(self, record: Mapping[str, Any]) -> str:
        return str(record.get("provider") or "anthropic")


ADAPTERS: dict[str, BaseAdapter] = {
    adapter.name: adapter
    for adapter in (
        GenericProcessAdapter(),
        GenericJsonlAdapter(),
        VillaniCodeAdapter(),
        CodexAdapter(),
        ClaudeCodeAdapter(),
    )
}
ADAPTERS["generic"] = ADAPTERS["generic-process"]


def get_adapter(name: str) -> BaseAdapter:
    try:
        return ADAPTERS[name]
    except KeyError as error:
        raise ValueError(f"unknown adapter: {name}") from error
