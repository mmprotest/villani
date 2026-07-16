from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from villani_ops.closed_loop.protocol_v2 import SPAN_KINDS, TelemetryEnvelopeV2
from villani_ops.closed_loop.schema_validation import (
    SCHEMA_V2_VERSION_TO_PATH,
    collect_protocol_validation_issues,
    parse_protocol_document,
)
from villani_ops.closed_loop.translate_v2 import (
    legacy_trace_id_to_w3c,
    normalized_v2_jsonl,
    translate_v1_run,
)


ROOT = Path(__file__).resolve().parents[5]
FIXTURES = ROOT / "integration" / "fixtures" / "protocol" / "v2"


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_all_nine_v2_schemas_and_strict_models_accept_shared_valid_bytes() -> None:
    assert len(SCHEMA_V2_VERSION_TO_PATH) == 9
    versions: set[str] = set()
    for path in sorted((FIXTURES / "valid").glob("*.json")):
        parsed = parse_protocol_document(_json(path))
        versions.add(parsed.schema_version)
    assert versions == set(SCHEMA_V2_VERSION_TO_PATH)


def test_packaged_v2_schemas_are_semantically_identical_to_normative_root() -> None:
    root_schemas = ROOT / "schemas" / "v2"
    packaged = ROOT / "components" / "villani-ops" / "villani_ops" / "schemas" / "v2"
    assert sorted(path.name for path in root_schemas.glob("*.json")) == sorted(
        path.name for path in packaged.glob("*.json")
    )
    for root_schema in root_schemas.glob("*.json"):
        assert json.loads(root_schema.read_text(encoding="utf-8")) == json.loads(
            (packaged / root_schema.name).read_text(encoding="utf-8")
        )


@pytest.mark.parametrize(
    ("filename", "category"),
    [
        ("agent_capability_duplicate_feature.json", "uniqueItems"),
        ("artifact_embeds_bytes.json", "additionalProperties"),
        ("outcome_unknown_cost_has_value.json", "accounting_status"),
        ("policy_bad_digest.json", "pattern"),
        ("resource_unknown_property.json", "additionalProperties"),
        ("span_bad_kind.json", "pattern"),
        ("telemetry_embeds_artifact_bytes.json", "not"),
        ("telemetry_missing_idempotency.json", "required"),
        ("verifier_capability_missing_evidence.json", "required"),
    ],
)
def test_shared_invalid_bytes_are_rejected_for_common_reason_category(
    filename: str, category: str
) -> None:
    issues = collect_protocol_validation_issues(_json(FIXTURES / "invalid" / filename))
    assert category in {issue.keyword for issue in issues}


def test_cross_language_fixture_byte_manifest() -> None:
    expected = _json(FIXTURES / "fixture-digests.json")
    actual = {
        str(path.relative_to(FIXTURES)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for directory in (FIXTURES / "valid", FIXTURES / "invalid")
        for path in sorted(directory.glob("*.json"))
    }
    assert actual == expected


def test_span_kinds_are_documented_but_unknown_future_kinds_remain_readable() -> None:
    assert {
        "controller_stage",
        "agent_run",
        "verifier",
        "external_service",
    } <= SPAN_KINDS
    document = _json(FIXTURES / "valid" / "span.json")
    document["kind"] = "future_span_kind"
    assert parse_protocol_document(document).kind == "future_span_kind"


@pytest.mark.parametrize("name", ["one_attempt_failed", "two_attempt_completed"])
def test_v1_translation_matches_golden_and_is_byte_stable(name: str) -> None:
    directory = FIXTURES / "translation" / name
    first = normalized_v2_jsonl(translate_v1_run(directory))
    second = normalized_v2_jsonl(translate_v1_run(directory))
    assert first == second
    assert (
        hashlib.sha256(first).hexdigest()
        == (directory / "expected.sha256").read_text(encoding="utf-8").strip()
    )
    records = translate_v1_run(directory)
    assert all(isinstance(record, TelemetryEnvelopeV2) for record in records)
    assert all(record.idempotency_key and record.run_id for record in records)
    assert all(record.attributes["villani.legacy.trace_id"] for record in records)
    assert all(record.occurred_at == record.observed_at for record in records)


def test_legacy_trace_mapping_is_deterministic_w3c_and_collision_namespaced() -> None:
    mapped = legacy_trace_id_to_w3c("trace_legacy")
    assert mapped == legacy_trace_id_to_w3c("trace_legacy")
    assert len(mapped) == 32
    assert mapped != "0" * 32
