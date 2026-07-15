from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from villani_agentd.adapters import AdapterContext, get_adapter
from villani_agentd.adapters import contract as contract_module
from villani_agentd.adapters.contract import ProbeResult, subprocess_probe
from villani_agentd.adapters.implementations import CodexAdapter
from villani_agentd.otlp import normalize_otlp_traces
from villani_agentd.spool import SpoolError
from villani_agentd.trace_context import parse_traceparent, propagated_environment
from villani_ops.closed_loop.protocol_v2 import TelemetryEnvelopeV2
from villani_ops.executables import ExecutableResolution
from villani_ops.execution_environment.secrets import _process_alive

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"
NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def context() -> AdapterContext:
    return AdapterContext("run_fixture", "1" * 32, "2" * 16, NOW)


@pytest.mark.parametrize(
    "name", ["generic-process", "generic-jsonl", "villani-code", "codex", "claude-code"]
)
def test_synthetic_fixtures_are_valid_and_byte_stable(name: str) -> None:
    data = (FIXTURES / f"{name}.jsonl").read_bytes()
    normalized = []
    parser = get_adapter(name).create_parser(context())
    for chunk in (data[:13], data[13:37], data[37:]):
        normalized.extend(parser.feed(chunk))
    normalized.extend(parser.finish())
    replay_parser = get_adapter(name).create_parser(context())
    replay = replay_parser.feed(data) + replay_parser.finish()
    assert normalized == replay
    assert normalized
    for event in normalized:
        assert TelemetryEnvelopeV2.model_validate(event.model_dump(mode="json"))
        assert event.attributes["villani.native.event_id"]


def test_partial_malformed_truncated_duplicates_revisions_and_secrets() -> None:
    parser = get_adapter("generic-jsonl").create_parser(context())
    assert parser.feed('{"id":"same","type":"model_completed","tokens":1') == []
    events = parser.feed('}\nnot-json\n{"id":"same","type":"model_completed","tokens":2}\n')
    events += parser.feed('{"id":"secret","type":"tool_result","output":"Bearer abcdefghijk')
    events += parser.finish()
    assert len(events) == 4
    same = [event for event in events if event.attributes["villani.native.event_id"] == "same"]
    assert [event.attributes["villani.native.revision"] for event in same] == [0, 1]
    assert same[0].body["tokens"] == 1
    assert events[-1].body["truncated_final_record"] is True
    assert "abcdefghijk" not in json.dumps([event.model_dump(mode="json") for event in events])
    duplicate = parser.feed('{"id":"same","type":"model_completed","tokens":2}\n')
    assert duplicate == []


def test_tool_call_parent_is_preserved() -> None:
    parser = get_adapter("generic-jsonl").create_parser(context())
    events = parser.feed(
        '{"id":"tool-1","type":"tool_started"}\n'
        '{"id":"command-1","parent_id":"tool-1","type":"command_started"}\n'
    )
    assert events[1].parent_span_id == events[0].span_id


def test_generic_jsonl_accepts_v2_or_configured_mapping() -> None:
    source = (
        get_adapter("generic-process")
        .create_parser(context())
        .feed('{"id":"native-v2","type":"command_completed"}\n')[0]
    )
    direct = (
        get_adapter("generic-jsonl")
        .create_parser(context())
        .feed(json.dumps(source.model_dump(mode="json")) + "\n")
    )
    assert direct == [source]
    mapped_context = AdapterContext(
        "run_fixture",
        "1" * 32,
        "2" * 16,
        NOW,
        field_mapping={
            "event_id": "native.uid",
            "event_type": "meta.category",
            "timestamp": "time",
        },
    )
    mapped = (
        get_adapter("generic-jsonl")
        .create_parser(mapped_context)
        .feed(
            '{"native":{"uid":"mapped-1"},"meta":{"category":"file_write"},"time":"2026-07-11T00:00:01Z"}\n'
        )[0]
    )
    assert mapped.attributes["villani.native.event_id"] == "mapped-1"
    assert mapped.kind == "file_operation"


def test_machine_adapter_commands_use_only_documented_modes() -> None:
    assert get_adapter("codex").construct_command(["fix it"]) == [
        "codex",
        "exec",
        "--json",
        "fix it",
    ]
    assert get_adapter("claude-code").construct_command(["fix it"]) == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "fix it",
    ]


def test_provider_detection_requires_documented_machine_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path(sys.executable).parent / "codex-fixture"
    monkeypatch.setattr(
        "villani_agentd.adapters.implementations.resolve_installed_executable",
        lambda name: ExecutableResolution(
            name,
            executable,
            "PATH",
            (executable,),
            "fixture",
            Path(sys.executable),
            Path(sys.executable).parent,
            True,
        ),
    )

    def probe(command):
        if "--version" in command:
            return 0, "codex-cli 9.8.7\n", ""
        return 0, "Usage: codex exec (terminal output only)", ""

    result = CodexAdapter().detect(probe)
    assert result.detected_version == "codex-cli 9.8.7"
    assert result.available is False
    assert result.missing_capabilities == ("documented_exec_json",)


def test_slow_version_probe_confirms_presence_without_reporting_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path(sys.executable).parent / "codex-fixture"
    monkeypatch.setattr(
        "villani_agentd.adapters.implementations.resolve_installed_executable",
        lambda name: ExecutableResolution(
            name,
            executable,
            "interpreter_scripts",
            (executable,),
            "fixture",
            Path(sys.executable),
            Path(sys.executable).parent,
            False,
        ),
    )

    def probe(command):
        if "--version" in command:
            return ProbeResult("timed_out", None, "", "", 1.25)
        return ProbeResult("completed", 0, "Usage: codex exec --json\n", "", 1.25)

    result = CodexAdapter().detect(probe)
    report = result.as_dict()
    assert result.available is True
    assert report["executable_status"] == "present"
    assert report["executable_path"] == str(executable)
    assert report["probe_status"] == "version_timed_out"
    assert report["probe_timeout_seconds"] == 1.25
    assert "presence was confirmed" in str(report["warning"])
    assert "executable" not in result.missing_capabilities


def test_timed_out_probe_terminates_its_confirmed_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "probe-ready.json"
    child = tmp_path / "probe-child.py"
    child.write_text(
        "import time\nwhile True:\n    time.sleep(1)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "probe-parent.py"
    parent.write_text(
        f"""import json, os, pathlib, subprocess, sys, time
ready = pathlib.Path({str(ready)!r})
descendant = subprocess.Popen([sys.executable, {str(child)!r}])
temporary = ready.with_name(ready.name + '.tmp')
with temporary.open('w', encoding='utf-8') as handle:
    json.dump({{'parent_pid': os.getpid(), 'child_pid': descendant.pid}}, handle)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, ready)
time.sleep(30)
""",
        encoding="utf-8",
    )
    real_popen = subprocess.Popen
    observed: dict[str, int] = {}

    class ReadyPopen(real_popen):
        def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    document = json.loads(ready.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(0.02)
                    continue
                observed.update(
                    parent_pid=int(document["parent_pid"]),
                    child_pid=int(document["child_pid"]),
                )
                break
            else:
                raise AssertionError("probe descendant did not become ready")
            return super().communicate(input=input, timeout=0.2)

    class SubprocessProxy:
        Popen = ReadyPopen

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(subprocess, name)

    monkeypatch.setattr(contract_module, "subprocess", SubprocessProxy())

    def alive(pid: int) -> bool:
        if not _process_alive(pid):
            return False
        if os.name != "nt":
            try:
                fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            except OSError:
                return True
            if len(fields) >= 3 and fields[2] == "Z":
                return False
        return True

    try:
        result = subprocess_probe([sys.executable, str(parent)], timeout_seconds=5)
        assert result.status == "timed_out"
        assert observed
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(alive(pid) for pid in observed.values()):
            time.sleep(0.02)
        assert not alive(observed["parent_pid"])
        assert not alive(observed["child_pid"])
    finally:
        for pid in observed.values():
            if not alive(pid):
                continue
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            else:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass


def test_trace_context_preserves_valid_parent_and_replaces_invalid() -> None:
    parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    env, trace, parent_span = propagated_environment(
        "1" * 32, "2" * 16, "run", {"traceparent": parent}
    )
    assert env["traceparent"] == parent
    assert (trace, parent_span) == ("a" * 32, "b" * 16)
    replaced, trace, parent_span = propagated_environment(
        "1" * 32, "2" * 16, "run", {"TRACEPARENT": "invalid"}
    )
    assert parse_traceparent(replaced["traceparent"]) == ("1" * 32, "2" * 16)
    assert (trace, parent_span) == ("1" * 32, None)


def _otlp() -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "fixture"}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "test"},
                        "spans": [
                            {
                                "traceId": "1" * 32,
                                "spanId": "2" * 16,
                                "name": "chat fixture",
                                "startTimeUnixNano": "1783728000000000000",
                                "endTimeUnixNano": "1783728001000000000",
                                "attributes": [
                                    {
                                        "key": "gen_ai.provider.name",
                                        "value": {"stringValue": "fixture-provider"},
                                    },
                                    {
                                        "key": "gen_ai.usage.input_tokens",
                                        "value": {"intValue": "12"},
                                    },
                                    {"key": "custom.unknown", "value": {"stringValue": "kept"}},
                                ],
                                "status": {"code": "STATUS_CODE_OK"},
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_otlp_genai_mapping_preserves_unknown_attributes_and_is_stable() -> None:
    first = normalize_otlp_traces(_otlp())
    second = normalize_otlp_traces(_otlp())
    assert first == second
    assert first[0].kind == "model_call"
    assert first[0].body["gen_ai"]["input_tokens"] == 12
    assert first[0].attributes["custom.unknown"] == "kept"


def test_otlp_malformed_is_rejected_deterministically() -> None:
    with pytest.raises(SpoolError, match="traceId/spanId"):
        normalize_otlp_traces(
            {
                "resourceSpans": [
                    {
                        "scopeSpans": [
                            {
                                "spans": [
                                    {"traceId": "bad", "spanId": "bad", "startTimeUnixNano": "1"}
                                ]
                            }
                        ]
                    }
                ]
            }
        )
