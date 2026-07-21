from __future__ import annotations

import json
from pathlib import Path

import pytest

from villani_ops.closed_loop import (
    ProtocolValidationError,
    read_jsonl_tolerant,
    validate_jsonl_event_stream,
    validate_protocol_document,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "integration" / "fixtures" / "protocol" / "v1"
VALID_RUN = FIXTURE_ROOT / "valid_run"


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_shared_protocol_bundle_validates_from_repository_root() -> None:
    snapshots = (
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
        VALID_RUN / "agent-system-config.json",
        VALID_RUN / "role-bindings.json",
        VALID_RUN / "agent-invocation-identity.json",
        VALID_RUN / "attempts" / "attempt_001" / "agent" / "invocation.json",
        VALID_RUN / "attempts" / "attempt_001" / "agent" / "process-result.json",
        VALID_RUN / "attempts" / "attempt_001" / "agent" / "output-tail.json",
        VALID_RUN / "attempts" / "attempt_001" / "agent" / "coder-result.json",
        VALID_RUN
        / "agent-systems"
        / "asys_d605dea1f6503cf9996864423c705228b426ccee3c2e02869084ac9bbbbda575.json",
        VALID_RUN
        / "agent-systems"
        / "asys_80147fac99d0bfffb4605d4a447ad9a0b6d6e947426c95efcf7168cc6ec94dfa.json",
        VALID_RUN / "attempts" / "attempt_001" / "harness-result.json",
        VALID_RUN / "attempts" / "attempt_002" / "harness-result.json",
        VALID_RUN / "harness-conformance.json",
        VALID_RUN / "qualification-observation.json",
        VALID_RUN / "qualification-invalidation.json",
        VALID_RUN / "qualification-snapshot.json",
        VALID_RUN / "gate-c.json",
        VALID_RUN / "economics-observation.json",
        VALID_RUN / "economics-snapshot.json",
        VALID_RUN / "online-evidence-update.json",
        VALID_RUN / "route-plan.json",
        VALID_RUN / "route-policy.json",
        VALID_RUN / "route-policy-evaluation.json",
        VALID_RUN / "route-policy-publication.json",
        VALID_RUN / "adaptive-verification-plan.json",
        VALID_RUN / "binary-verification-decision.json",
        VALID_RUN / "review-package.json",
        VALID_RUN / "human-outcome.json",
        VALID_RUN / "supervision-metrics.json",
        VALID_RUN / "gate-d.json",
    )
    for snapshot in snapshots:
        validate_protocol_document(_load_json(snapshot))

    for decision in read_jsonl_tolerant(VALID_RUN / "policy_decisions.jsonl"):
        validate_protocol_document(decision)
    assert len(validate_jsonl_event_stream(VALID_RUN / "events.jsonl")) == 24


def test_shared_invalid_json_documents_fail_from_repository_root() -> None:
    invalid_root = FIXTURE_ROOT / "invalid"
    invalid_documents = sorted(invalid_root.glob("*.json"))
    assert len(invalid_documents) == 14
    for invalid_document in invalid_documents:
        with pytest.raises(ProtocolValidationError):
            validate_protocol_document(_load_json(invalid_document))
