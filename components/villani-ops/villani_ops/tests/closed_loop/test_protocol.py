from __future__ import annotations

import json
from pathlib import Path

import pytest

from villani_ops.closed_loop.durable_io import (
    append_jsonl_durable,
    read_jsonl_tolerant,
    write_json_atomic,
)
from villani_ops.closed_loop.schema_validation import (
    SCHEMA_VERSION_TO_PATH,
    ProtocolValidationError,
    collect_protocol_validation_issues,
    parse_protocol_document,
    validate_event_stream,
    validate_jsonl_event_stream,
    validate_protocol_document,
)


def _repository_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "AGENTS.md").is_file() and (
            candidate / "schemas" / "v1" / "event.schema.json"
        ).is_file():
            return candidate
    raise AssertionError("repository root not found")


REPOSITORY_ROOT = _repository_root()
FIXTURE_ROOT = REPOSITORY_ROOT / "integration" / "fixtures" / "protocol" / "v1"
VALID_RUN = FIXTURE_ROOT / "valid_run"
INVALID = FIXTURE_ROOT / "invalid"

VALID_SNAPSHOT_PATHS = (
    VALID_RUN / "task.json",
    VALID_RUN / "manifest.json",
    VALID_RUN / "state.json",
    VALID_RUN / "classification.json",
    VALID_RUN / "attempts" / "attempt_001" / "attempt.json",
    VALID_RUN / "attempts" / "attempt_002" / "attempt.json",
    VALID_RUN / "verification" / "attempt_001.json",
    VALID_RUN / "verification" / "attempt_002.json",
    VALID_RUN / "selection.json",
    VALID_RUN / "materialization.json",
    VALID_RUN / "validation-coverage.json",
    VALID_RUN / "run-summary.json",
    VALID_RUN / "product-run.json",
    VALID_RUN / "evaluation-suite.json",
    VALID_RUN / "evaluation-task.json",
    VALID_RUN / "evaluation-trial.json",
    VALID_RUN / "human-review.json",
    VALID_RUN / "evaluation-report.json",
    *sorted((VALID_RUN / "agent-systems").glob("asys_*.json")),
    VALID_RUN / "attempts" / "attempt_001" / "harness-result.json",
    VALID_RUN / "attempts" / "attempt_002" / "harness-result.json",
    VALID_RUN / "harness-conformance.json",
)


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_all_versioned_root_schemas_are_valid_and_mapped() -> None:
    assert len(SCHEMA_VERSION_TO_PATH) == 21
    assert len(set(SCHEMA_VERSION_TO_PATH.values())) == 21
    for schema_path in SCHEMA_VERSION_TO_PATH.values():
        assert schema_path.is_file()
        schema = _load_json(schema_path)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"] == (f"https://villani.dev/schemas/v1/{schema_path.name}")
        assert schema["additionalProperties"] is False


def test_packaged_schemas_are_semantically_identical_to_normative_root() -> None:
    packaged = (
        REPOSITORY_ROOT
        / "components"
        / "villani-ops"
        / "villani_ops"
        / "schemas"
        / "v1"
    )
    root_schemas = REPOSITORY_ROOT / "schemas" / "v1"
    assert sorted(path.name for path in packaged.glob("*.json")) == sorted(
        path.name for path in root_schemas.glob("*.json")
    )
    for root_schema in root_schemas.glob("*.json"):
        assert json.loads(
            (packaged / root_schema.name).read_text(encoding="utf-8")
        ) == (json.loads(root_schema.read_text(encoding="utf-8")))


def test_complete_valid_bundle_uses_every_protocol_version() -> None:
    versions: set[object] = set()
    for path in VALID_SNAPSHOT_PATHS:
        document = _load_json(path)
        validate_protocol_document(document)
        parsed = parse_protocol_document(document)
        versions.add(parsed.schema_version)

    events = validate_jsonl_event_stream(VALID_RUN / "events.jsonl")
    assert len(events) == 24
    versions.add(events[0].schema_version)

    policy_decisions = read_jsonl_tolerant(VALID_RUN / "policy_decisions.jsonl")
    assert len(policy_decisions) == 2
    for decision in policy_decisions:
        validate_protocol_document(decision)
        versions.add(decision["schema_version"])

    assert versions == set(SCHEMA_VERSION_TO_PATH)
    assert (
        _load_json(VALID_RUN / "verification" / "attempt_001.json")[
            "acceptance_eligible"
        ]
        is False
    )
    assert (
        _load_json(VALID_RUN / "verification" / "attempt_002.json")[
            "acceptance_eligible"
        ]
        is True
    )
    assert _load_json(VALID_RUN / "materialization.json")["status"] == "succeeded"


@pytest.mark.parametrize(
    ("filename", "expected_keyword"),
    [
        ("event_missing_run_id.json", "required"),
        ("event_sequence_zero.json", "minimum"),
        ("attempt_event_without_attempt_id.json", "type"),
        ("verification_error_marked_eligible.json", "acceptance_eligibility"),
        ("selection_contains_ineligible_candidate.json", "selection_eligibility"),
        ("manifest_cost_missing_but_status_complete.json", "accounting_status"),
        ("state_terminal_false_for_completed.json", "terminal_state"),
        ("unknown_top_level_property.json", "additionalProperties"),
    ],
)
def test_invalid_shared_fixture_is_rejected_for_intended_rule(
    filename: str, expected_keyword: str
) -> None:
    issues = collect_protocol_validation_issues(_load_json(INVALID / filename))
    assert expected_keyword in {issue.keyword for issue in issues}
    with pytest.raises(ProtocolValidationError):
        validate_protocol_document(_load_json(INVALID / filename))


def test_event_sequences_must_strictly_increase() -> None:
    events = read_jsonl_tolerant(VALID_RUN / "events.jsonl")[:2]
    events[1]["sequence"] = 1
    with pytest.raises(ProtocolValidationError) as caught:
        validate_event_stream(events)
    assert "event_sequence" in {issue.keyword for issue in caught.value.issues}


def test_tolerates_one_truncated_final_jsonl_line() -> None:
    path = INVALID / "events_truncated_final_line.jsonl"
    assert len(read_jsonl_tolerant(path)) == 2
    assert len(validate_jsonl_event_stream(path)) == 2


def test_rejects_a_malformed_middle_jsonl_line() -> None:
    with pytest.raises(json.JSONDecodeError):
        read_jsonl_tolerant(INVALID / "events_malformed_middle_line.jsonl")


def test_rejects_a_complete_malformed_final_jsonl_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"event_id":BROKEN}', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_jsonl_tolerant(path)


def test_rejects_an_unclosed_final_line_with_an_invalid_token(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"event_id":BROKEN', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_jsonl_tolerant(path)


def test_atomic_snapshot_and_durable_compact_jsonl(tmp_path: Path) -> None:
    snapshot = tmp_path / "bundle" / "state.json"
    write_json_atomic(snapshot, {"value": "first"})
    write_json_atomic(snapshot, {"value": "second"})
    assert _load_json(snapshot) == {"value": "second"}
    assert not list(snapshot.parent.glob(f".{snapshot.name}.*.tmp"))

    stream = tmp_path / "bundle" / "events.jsonl"
    append_jsonl_durable(stream, {"sequence": 1, "payload": {}})
    append_jsonl_durable(stream, {"sequence": 2, "payload": {"ok": True}})
    lines = stream.read_text(encoding="utf-8").splitlines()
    assert lines == [
        '{"sequence":1,"payload":{}}',
        '{"sequence":2,"payload":{"ok":true}}',
    ]
    assert read_jsonl_tolerant(stream) == [
        {"sequence": 1, "payload": {}},
        {"sequence": 2, "payload": {"ok": True}},
    ]
