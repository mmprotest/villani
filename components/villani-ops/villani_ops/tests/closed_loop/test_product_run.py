from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from villani_ops.closed_loop.product_run import (
    ProductRun,
    build_product_run,
    project_product_stage,
)
from villani_ops.closed_loop.schema_validation import validate_protocol_document


def _repository_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "AGENTS.md").is_file():
            return candidate
    raise AssertionError("repository root not found")


VALID_RUN = (
    _repository_root() / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"
)


def test_valid_legacy_bundle_projects_one_shared_ready_verdict() -> None:
    product = build_product_run(VALID_RUN)

    assert product.schema_version == "villani.product_run.v1"
    assert product.current_stage == "Ready"
    assert product.final_verdict == "Ready to apply"
    assert product.changed_files == ["calculator.py"]
    assert product.cost.value == 0.05
    assert product.cost.accounting_status == "complete"
    assert product.duration.value_ms is None
    assert product.duration.accounting_status == "unknown"
    assert not {
        "apply_change",
        "create_branch",
        "open_pull_request",
    }.intersection(action.id for action in product.available_actions)
    validate_protocol_document(product.model_dump(mode="json"))


def test_projection_fails_closed_when_selection_proof_is_missing(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shutil.copytree(VALID_RUN, run)
    selection = json.loads((run / "selection.json").read_text(encoding="utf-8"))
    selection["selected_candidate_ids"] = []
    (run / "selection.json").write_text(json.dumps(selection), encoding="utf-8")

    product = build_product_run(run)

    assert product.final_verdict == "Needs review"
    assert not {
        "apply_change",
        "create_branch",
        "open_pull_request",
    }.intersection(action.id for action in product.available_actions)


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("retry_selected", "The first route could not prove the change. Retrying."),
        ("escalation_selected", "Retrying with a stronger qualified route."),
        ("verification_retry_started", "Verification needs another check."),
    ],
)
def test_retry_language_comes_from_shared_projection(
    event_type: str, expected: str
) -> None:
    stage, sentence = project_product_stage(
        {"event_type": event_type, "payload": {}}, "Working"
    )
    assert stage == "Working"
    assert sentence == expected


def test_contract_rejects_delivery_action_for_unproved_verdict() -> None:
    value = build_product_run(VALID_RUN).model_dump(mode="json")
    value["final_verdict"] = "Could not prove"
    value["available_actions"] = [
        {
            "id": "apply_change",
            "label": "Apply change",
            "method": "POST",
            "href": "/approval",
        }
    ]
    with pytest.raises(ValidationError):
        ProductRun.model_validate(value)


def test_checks_summary_includes_repository_checks_and_focused_probes(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "run"
    shutil.copytree(VALID_RUN, bundle)
    summary_path = bundle / "run-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["focused_probes"] = {
        "passed": 2,
        "failed": 1,
        "not_run": 0,
        "unavailable": 0,
        "accounting_status": "complete",
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    product = build_product_run(bundle)

    assert product.checks_summary.model_dump() == {
        "passed": 3,
        "failed": 1,
        "not_run": 0,
        "unavailable": 0,
        "accounting_status": "complete",
    }
