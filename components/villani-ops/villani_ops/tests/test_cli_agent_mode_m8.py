from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from villani_ops.closed_loop.agent_systems.role_models import AgentRole
from villani_ops.closed_loop.cli_runtime.failure_presentation import (
    build_cli_failure_presentation,
)

from . import test_claude_code_cli_coding as claude_coding
from . import test_cli_classification_selection as role_tests
from . import test_cli_verification as verifier_tests
from . import test_codex_cli_coding as codex_coding


ROOT = Path(__file__).resolve().parents[4]
RELEASE = ROOT / "release-verification"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _workspace_facts(root: Path) -> tuple[int, str, Path, dict[str, Any]]:
    process_paths = sorted(root.rglob("agent/process-result.json"))
    assert len(process_paths) == 1, process_paths
    process_path = process_paths[0]
    process = _json(process_path)
    invocation = _json(process_path.with_name("invocation.json"))
    independence_path = process_path.with_name("independence.json")
    if independence_path.is_file():
        independence = _json(independence_path)
        session = str(
            independence.get("session_id")
            or independence.get("verifier_session_id")
            or ""
        )
    else:
        normalized = _json(process_path.with_name("normalized-result.json"))
        session = str(normalized.get("thread_id") or normalized.get("session_id") or "")
    assert isinstance(process.get("pid"), int)
    assert session
    return int(process["pid"]), session, process_path.parent.parent, invocation


def test_fake_e2e_manifest_has_exact_numbered_scenarios_and_live_nodes() -> None:
    document = _json(RELEASE / "cli-agent-mode-scenarios.json")
    scenarios = document["scenarios"]
    assert [item["id"] for item in scenarios] == list(range(1, 31))
    assert len({item["name"] for item in scenarios}) == 30
    for item in scenarios:
        assert item["test_nodes"]
        for node in item["test_nodes"]:
            relative = str(node).split("::", 1)[0]
            assert (ROOT / "components" / "villani-ops" / relative).is_file()


def test_conformance_matrix_has_exact_rows_columns_and_fail_closed_rule() -> None:
    document = _json(RELEASE / "cli-agent-mode-conformance-matrix.json")
    assert document["columns"] == [
        "configured",
        "executable_present",
        "auth_ready",
        "version_supported",
        "structured_output",
        "permissions",
        "cancellation",
        "artifact_completeness",
        "normalized_events",
        "isolation",
        "role_contract",
        "fake_conformance",
        "real_smoke_status",
        "production_enabled",
    ]
    assert [item["id"] for item in document["rows"]] == [
        "api_classification",
        "api_coding",
        "api_verification",
        "api_selection",
        "codex_classification",
        "codex_coding",
        "codex_verification",
        "codex_selection",
        "claude_classification",
        "claude_coding",
        "claude_verification",
        "claude_selection",
        "deterministic_selection",
    ]


def test_verifier_blindness_canary_is_absent_from_workspace_and_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = verifier_tests._candidate(tmp_path, coder="codex")
    canaries = {
        "provider": "CANARY_PROVIDER_OPENAI",
        "model": "CANARY_MODEL_PRESTIGE",
        "driver": "CANARY_DRIVER_CODEX",
        "candidate_order": "CANARY_ORDER_FIRST",
        "candidate_rank": "CANARY_RANK_ONE",
        "cost": "CANARY_COST_999",
        "tokens": "CANARY_TOKENS_12345",
        "runtime_duration": "CANARY_DURATION_9876",
        "competing_candidate": "CANARY_COMPETING_PATCH",
        "coder_transcript": "CANARY_CODER_TRANSCRIPT",
        "selector_output": "CANARY_SELECTOR_OUTPUT",
    }
    outside = case.context.run_directory / "canary-values-elsewhere.json"
    outside.write_text(json.dumps(canaries), encoding="utf-8")
    metadata = dict(case.result.metadata)
    metadata.update(canaries)
    candidate = replace(
        case,
        result=replace(case.result, metadata=metadata),
    )
    verification = verifier_tests._adapter("claude", scenario="success").verify(
        candidate.context, candidate.result
    )
    assert verification.acceptance_eligible is True
    workspace = verifier_tests._workspace(candidate, verification)
    supplied = b"\n".join(
        path.read_bytes() for path in sorted(workspace.rglob("*")) if path.is_file()
    )
    assert outside.is_file()
    for value in canaries.values():
        assert value.encode() not in supplied
    prompt = (workspace / "input" / "verifier-prompt.txt").read_text(encoding="utf-8")
    assert all(value not in prompt for value in canaries.values())


def test_infrastructure_failed_candidate_never_enters_selector_packet(
    tmp_path: Path,
) -> None:
    tmp_path.mkdir(exist_ok=True)
    _repository, context, candidates = role_tests._selection_case(tmp_path)
    failed = role_tests._candidate(
        tmp_path, "attempt_infrastructure_failed", "must-not-enter.py", eligible=False
    )
    failed.verification.metadata["infrastructure_failure"] = True
    failed.verification.reason = "Provider process crashed before verification."
    selection = role_tests._selector("codex").select((*candidates, failed), context)
    assert selection.selected_attempt_id in {"attempt_alpha", "attempt_beta"}
    workspace = Path(str(selection.metadata["cli_selector_workspace"]))
    packet = (workspace / "input" / "candidates.json").read_text(encoding="utf-8")
    assert "must-not-enter.py" not in packet
    assert "attempt_infrastructure_failed" not in packet


def test_five_sequential_role_processes_have_unique_processes_sessions_and_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    classification_root = tmp_path / "01 classification"
    classification_root.mkdir()
    repository = role_tests._repository(classification_root)
    classification = role_tests._classifier("codex").classify(
        "Update target.py and run its tests.",
        role_tests._classification_context(classification_root, repository),
    )
    classification_workspace = Path(
        str(classification.metadata["cli_classifier_workspace"])
    )

    codex_result, codex_context, _ = codex_coding._run_scenario(
        tmp_path / "02 codex coding", monkeypatch, "success"
    )
    assert codex_result.status == "completed"
    claude_result, claude_context, _ = claude_coding._run_scenario(
        tmp_path / "03 claude coding", monkeypatch, "success"
    )
    assert claude_result.status == "completed"

    verifier_case, verification = verifier_tests._run(
        tmp_path / "04 verification",
        monkeypatch,
        verifier="claude",
        scenario="success",
        coder="codex",
    )
    verifier_workspace = verifier_tests._workspace(verifier_case, verification)

    selection_root = tmp_path / "05 selection"
    selection_root.mkdir()
    _repository, selection_context, candidates = role_tests._selection_case(
        selection_root
    )
    selection = role_tests._selector("codex").select(candidates, selection_context)
    selection_workspace = Path(str(selection.metadata["cli_selector_workspace"]))

    roots = [
        classification_workspace,
        codex_context.attempt_directory,
        claude_context.attempt_directory,
        verifier_workspace,
        selection_workspace,
    ]
    facts = [_workspace_facts(Path(root)) for root in roots]
    assert len({pid for pid, _session, _workspace, _invocation in facts}) == 5
    assert len({session for _pid, session, _workspace, _invocation in facts}) == 5
    assert (
        len({workspace.resolve() for _pid, _session, workspace, _invocation in facts})
        == 5
    )
    for _pid, _session, _workspace, invocation in facts:
        arguments = [str(item) for item in invocation["arguments"]]
        assert "--resume" not in arguments
        assert "--continue" not in arguments


def test_failure_projection_contains_every_required_public_fact() -> None:
    projection = build_cli_failure_presentation(
        role=AgentRole.VERIFICATION,
        agent_system_id="claude-verifier",
        process={
            "infrastructure_state": "timed_out",
            "failures": [
                {
                    "code": "timeout",
                    "message": "Verifier process exceeded its configured timeout.",
                }
            ],
        },
        target_repository_modified=False,
        partial_patch_preserved=False,
        automatic_fallback_performed=False,
        evidence_path="verification/vfy_fixture/agent/process-result.json",
    ).model_dump(mode="json")
    assert set(projection) == {
        "schema_version",
        "stage",
        "role",
        "agent_system_id",
        "safe_error_summary",
        "target_repository_modified",
        "partial_patch_preserved",
        "automatic_fallback_performed",
        "exact_repair_action",
        "evidence_path",
    }
    assert projection["stage"] == projection["role"] == "verification"
    assert projection["target_repository_modified"] is False
    assert projection["partial_patch_preserved"] is False
    assert projection["automatic_fallback_performed"] is False
    assert "villani agents doctor claude-verifier" in projection["exact_repair_action"]


def test_release_inputs_have_no_quota_or_new_provider_surface() -> None:
    matrix = _json(RELEASE / "cli-agent-mode-conformance-matrix.json")
    assert {row["system"] for row in matrix["rows"]} == {
        "api",
        "codex",
        "claude",
        "deterministic",
    }
    forbidden_key_fragments = (
        "quota",
        "usage_reset",
        "billing_control",
        "account_switch",
        "team",
        "route_score",
    )

    def keys(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [str(key) for key in value] + [
                nested for child in value.values() for nested in keys(child)
            ]
        if isinstance(value, list):
            return [nested for child in value for nested in keys(child)]
        return []

    documents = [
        matrix,
        _json(RELEASE / "cli-agent-mode-scenarios.json"),
    ]
    assert not {
        key
        for document in documents
        for key in keys(document)
        if any(fragment in key.casefold() for fragment in forbidden_key_fragments)
    }


def test_release_commands_are_bounded_consent_gated_and_never_use_a_shell() -> None:
    gate = (RELEASE / "run_cli_agent_gate.py").read_text(encoding="utf-8")
    smoke = (RELEASE / "run_cli_agent_smoke.py").read_text(encoding="utf-8")
    for source in (gate, smoke):
        assert "shell=False" in source
        assert "shell=True" not in source
        assert "2_000_000" in source
    assert "I_ACCEPT_EXTERNAL_USAGE" in smoke
    assert "--detect-only" in smoke
    assert "disposable_repositories_only" in smoke
    assert "maximum_event_line_bytes" in gate
    assert "user_home_path" in gate
