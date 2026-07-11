from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from villani_agentd.adapters import AdapterContext, get_adapter
from villani_agentd.adapters.implementations import CodexAdapter
from villani_agentd.otlp import normalize_otlp_traces
from villani_agentd.spool import SpoolError
from villani_agentd.trace_context import parse_traceparent, propagated_environment
from villani_ops.closed_loop.protocol_v2 import TelemetryEnvelopeV2

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
    monkeypatch.setattr(
        "villani_agentd.adapters.implementations.shutil.which", lambda _name: "codex"
    )

    def probe(command):
        if "--version" in command:
            return 0, "codex-cli 9.8.7\n", ""
        return 0, "Usage: codex exec (terminal output only)", ""

    result = CodexAdapter().detect(probe)
    assert result.detected_version == "codex-cli 9.8.7"
    assert result.available is False
    assert result.missing_capabilities == ("documented_exec_json",)


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
