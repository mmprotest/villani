"""Codex/Claude tie selector behind the existing Selector port."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from pydantic import ValidationError

from ..adapters.evidence_selector import (
    EvidenceSelectionPlan,
    finalize_evidence_selection,
    prepare_evidence_selection,
)
from ..agent_systems.role_models import AgentRole
from ..claude_code_cli.driver import ClaudeCodeCliDriver
from ..claude_code_cli.models import ClaudeProbeResult
from ..cli_roles.models import (
    CliRoleFailure,
    DuplicateJsonFieldError,
    normalize_cli_selector_result,
)
from ..cli_roles.prompts import SELECTOR_PROMPT_VERSION, build_selector_prompt
from ..cli_roles.runtime import CliRoleExecution, execute_cli_role
from ..cli_roles.workspace import (
    CliRoleWorkspaceError,
    PreparedCliRoleWorkspace,
    assert_selector_blindness,
    prepare_cli_role_workspace,
)
from ..codex_cli.driver import CodexCliDriver
from ..codex_cli.models import CodexProbeResult
from ..durable_io import write_json_atomic
from ..event_writer import redact_message
from ..interfaces import EligibleCandidate, Selection, SelectionContext


SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas" / "v1"
PATCH_INPUT_LIMIT_BYTES = 1024 * 1024
CLI_SELECTION_STRATEGY = "cli_semantic_evidence_tiebreak_v1"

CliDriver: TypeAlias = CodexCliDriver | ClaudeCodeCliDriver
CliProbe: TypeAlias = CodexProbeResult | ClaudeProbeResult


def _patch_summary(patch: str, changed_files: list[str]) -> dict[str, Any]:
    encoded = patch.encode("utf-8")
    additions = sum(
        1
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1
        for line in patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "file_count": len(changed_files),
        "additions": additions,
        "deletions": deletions,
        "full_patch_supplied": len(encoded) <= PATCH_INPUT_LIMIT_BYTES,
    }


def _validation_summary(candidate: EligibleCandidate) -> dict[str, Any]:
    verification = candidate.verification
    metadata = verification.metadata
    return {
        "repository_validation_status": metadata.get("repository_validation_status"),
        "repository_validation_failure_code": metadata.get(
            "repository_validation_failure_code"
        ),
        "success_evidence": [
            {"kind": item.kind, "summary": item.summary}
            for item in verification.success_evidence
        ],
        "failure_evidence": [
            {"kind": item.kind, "summary": item.summary}
            for item in verification.failure_evidence
        ],
        "missing_evidence": [
            {"kind": item.kind, "summary": item.summary}
            for item in verification.missing_evidence
        ],
    }


def _blind_attempt_identity(value: Any, *, attempt_id: str) -> Any:
    """Remove the controller's real candidate identity from selector evidence."""

    if isinstance(value, str):
        return value.replace(attempt_id, "<opaque-candidate>")
    if isinstance(value, list):
        return [_blind_attempt_identity(item, attempt_id=attempt_id) for item in value]
    if isinstance(value, dict):
        return {
            key: _blind_attempt_identity(item, attempt_id=attempt_id)
            for key, item in value.items()
        }
    return value


def _candidate_packet(
    candidate: EligibleCandidate,
    row: Mapping[str, Any],
    *,
    opaque_id: str,
) -> dict[str, Any]:
    changed_files = [
        str(item) for item in (candidate.attempt.metadata.get("changed_files") or [])
    ]
    patch = candidate.patch
    summary = _patch_summary(patch, changed_files)
    score = row.get("evidence_score")
    dimensions = dict(score) if isinstance(score, Mapping) else {}
    dimensions.pop("final", None)
    packet = {
        "candidate_id": opaque_id,
        "requirement_coverage": [
            {
                "requirement_id": item.requirement_id,
                "description": item.description,
                "outcome": item.outcome,
                "evidence_references": list(item.evidence_ids),
            }
            for item in candidate.verification.requirement_results
        ],
        "authoritative_validation": _validation_summary(candidate),
        "verifier": {
            "decision": 1,
            "reason": candidate.verification.reason,
        },
        "changed_files": changed_files,
        "patch_summary": summary,
        "candidate_patch": patch if summary["full_patch_supplied"] else None,
        "risk_flags": list(candidate.verification.risk_flags),
        "deterministic_evidence": {
            "dimensions": dimensions,
            "direct_behavioral_evidence_count": len(
                row.get("direct_behavioral_evidence", [])
            ),
            "source_level_inference_evidence_count": len(
                row.get("source_level_inference_evidence", [])
            ),
            "missing_requirement_flag_count": len(
                row.get("missing_requirement_flags", [])
            ),
            "risk_flag_count": len(row.get("risk_flags", [])),
        },
    }
    blinded = _blind_attempt_identity(
        packet, attempt_id=str(candidate.attempt.attempt_id)
    )
    assert_selector_blindness(blinded)
    return blinded


def _deterministic_result(
    plan: EvidenceSelectionPlan,
    context: SelectionContext,
    *,
    skipped_reason: str | None = None,
    failure: CliRoleFailure | None = None,
    failure_reason: str | None = None,
    workspace: PreparedCliRoleWorkspace | None = None,
    execution: CliRoleExecution | None = None,
) -> Selection:
    if workspace is not None:
        try:
            write_json_atomic(
                workspace.normalized_result_path,
                {
                    "schema_version": "villani.cli_selector_normalized_result.v1",
                    "status": (
                        "deterministic_fallback" if failure else "deterministic_skip"
                    ),
                    "failure_code": failure.value if failure else None,
                    "fallback_used": failure is not None,
                    "reason": redact_message(failure_reason or skipped_reason or ""),
                },
            )
        except Exception:
            failure = CliRoleFailure.ARTIFACT_PREPARATION_FAILURE
            failure_reason = (
                "CLI selector normalized fallback artifact could not be written."
            )
    metadata = {
        "deterministic_evidence_controls_selection": True,
        "cli_selector_invoked": False
        if execution is None
        else execution.process_spawned,
        "cli_selector_skipped_reason": skipped_reason,
        "cli_selector_fallback": failure is not None,
        "cli_selector_failure": failure.value if failure else None,
        "cli_selector_failure_reason": (
            redact_message(failure_reason) if failure_reason else None
        ),
        "cli_selector_workspace": str(workspace.root) if workspace else None,
    }
    return finalize_evidence_selection(plan, context, metadata=metadata)


class CliSelectorAdapter:
    """Invoke a CLI only for unresolved ties among acceptance-eligible candidates."""

    def __init__(self, driver: CliDriver, *, probe: CliProbe) -> None:
        if driver.system.roles != {AgentRole.SELECTION}:
            raise ValueError("CLI selector requires a selection-only system")
        self.driver = driver
        self.probe = probe

    def select(
        self,
        eligible_candidates: tuple[EligibleCandidate, ...],
        context: SelectionContext,
    ) -> Selection:
        plan = prepare_evidence_selection(eligible_candidates, context)
        if len(plan.eligible) == 1:
            return _deterministic_result(
                plan,
                context,
                skipped_reason="one acceptance-eligible candidate is already unambiguous",
            )
        if plan.deterministic_winner_is_unambiguous:
            return _deterministic_result(
                plan,
                context,
                skipped_reason="deterministic evidence produced an unambiguous winner",
            )

        supplied_ids = tuple(str(row["candidate_id"]) for row in plan.ranked)
        actual_to_opaque = {
            attempt_id: f"candidate-{uuid.uuid4().hex}" for attempt_id in supplied_ids
        }
        opaque_to_actual = {
            opaque: actual for actual, opaque in actual_to_opaque.items()
        }
        rows_by_id = {str(row["candidate_id"]): row for row in plan.ranked}
        packets = [
            _candidate_packet(
                plan.candidate_by_id[attempt_id],
                rows_by_id[attempt_id],
                opaque_id=actual_to_opaque[attempt_id],
            )
            for attempt_id in supplied_ids
        ]
        packets.sort(key=lambda item: str(item["candidate_id"]))
        candidate_document = {
            "schema_version": "villani.cli_selector_candidates.v1",
            "candidates": packets,
        }
        assert_selector_blindness(candidate_document)
        task_document = {
            "schema_version": "villani.cli_selector_task.v1",
            "task": context.task,
        }
        criteria_document = {
            "schema_version": "villani.cli_selector_success_criteria.v1",
            "success_criteria": context.success_criteria,
        }
        selection_policy_document = {
            "schema_version": "villani.cli_selector_call_policy.v1",
            "acceptance_eligible_candidate_count": len(plan.eligible),
            "deterministic_winner_unambiguous": False,
            "deterministic_top_tie_count": len(plan.tied_winner_ids),
            "semantic_selection_configured": True,
        }
        prompt = build_selector_prompt(
            task=task_document,
            success_criteria=criteria_document,
            selection_policy=selection_policy_document,
            candidates=candidate_document,
        )
        workspace: PreparedCliRoleWorkspace | None = None
        try:
            worktrees = tuple(
                Path(candidate.attempt.worktree_path).resolve()
                for candidate in plan.eligible
                if Path(candidate.attempt.worktree_path).is_dir()
            )
            workspace = prepare_cli_role_workspace(
                role="selection",
                invocation_id=f"selection-{uuid.uuid4().hex}",
                run_directory=Path(context.run_directory),
                target_repository=Path(context.repository_path),
                candidate_worktrees=worktrees,
                input_documents={
                    "task.json": ("verbatim_task", task_document),
                    "success-criteria.json": (
                        "verbatim_success_criteria",
                        criteria_document,
                    ),
                    "selection-policy.json": (
                        "deterministic_selection_call_policy",
                        selection_policy_document,
                    ),
                    "candidates.json": (
                        "acceptance_eligible_candidates",
                        candidate_document,
                    ),
                },
                prompt_bytes=prompt.bytes,
                output_schema_source=SCHEMA_ROOT / "cli-selector-result.schema.json",
                raw_result_filename="selector-result.json",
                normalized_result_filename="normalized-result.json",
                blindness={
                    "provider_identity_included": False,
                    "model_identity_included": False,
                    "cli_driver_included": False,
                    "cost_included": False,
                    "route_rank_included": False,
                    "attempt_order_included": False,
                    "token_count_included": False,
                    "coder_transcript_included": False,
                    "rejected_candidates_included": False,
                    "hidden_expected_patch_included": False,
                },
            )
        except (OSError, ValueError, CliRoleWorkspaceError) as error:
            return _deterministic_result(
                plan,
                context,
                failure=CliRoleFailure.ARTIFACT_PREPARATION_FAILURE,
                failure_reason=str(error),
                workspace=workspace,
            )

        execution = execute_cli_role(
            driver=self.driver,
            probe=self.probe,
            role=AgentRole.SELECTION,
            workspace=workspace,
            run_id=context.run_id,
            cancellation_event=context.cancellation_event,
        )
        try:
            write_json_atomic(
                workspace.agent_directory / "opaque-candidate-map.json",
                {
                    "schema_version": "villani.cli_selector_private_mapping.v1",
                    "created_after_process_exit": True,
                    "mapping": opaque_to_actual,
                },
            )
        except Exception as error:
            return _deterministic_result(
                plan,
                context,
                failure=CliRoleFailure.ARTIFACT_PREPARATION_FAILURE,
                failure_reason=f"private candidate mapping could not be preserved: {error}",
                workspace=workspace,
                execution=execution,
            )
        if execution.failure is not None:
            return _deterministic_result(
                plan,
                context,
                failure=execution.failure,
                failure_reason=execution.reason,
                workspace=workspace,
                execution=execution,
            )
        try:
            result = normalize_cli_selector_result(
                execution.raw_text,
                supplied_candidate_ids=set(opaque_to_actual),
            )
        except (json.JSONDecodeError, DuplicateJsonFieldError) as error:
            return _deterministic_result(
                plan,
                context,
                failure=CliRoleFailure.MALFORMED_OUTPUT,
                failure_reason=f"CLI selector output was malformed: {error}",
                workspace=workspace,
                execution=execution,
            )
        except (ValueError, ValidationError) as error:
            return _deterministic_result(
                plan,
                context,
                failure=CliRoleFailure.SCHEMA_FAILURE,
                failure_reason=f"CLI selector output failed schema normalization: {error}",
                workspace=workspace,
                execution=execution,
            )

        selected_attempt_id = opaque_to_actual[result.selected_candidate_id]
        normalized = {
            "schema_version": "villani.cli_selector_normalized_result.v1",
            "status": "succeeded",
            "failure_code": None,
            "prompt_version": SELECTOR_PROMPT_VERSION,
            "selected_attempt_id": selected_attempt_id,
            "ranking": [opaque_to_actual[item] for item in result.ranking],
            "reason": result.reason,
            "fallback_used": False,
            "input_manifest_verified": execution.input_integrity_proved,
            "target_repository_unchanged": execution.target_unchanged,
            "candidate_worktrees_unchanged": execution.candidates_unchanged,
        }
        try:
            write_json_atomic(workspace.normalized_result_path, normalized)
        except Exception as error:
            return _deterministic_result(
                plan,
                context,
                failure=CliRoleFailure.ARTIFACT_PREPARATION_FAILURE,
                failure_reason=f"CLI selector normalized artifact could not be written: {error}",
                workspace=workspace,
                execution=execution,
            )
        metadata = {
            "deterministic_acceptance_controls_eligibility": True,
            "deterministic_evidence_controls_selection": False,
            "cli_semantic_tiebreak_controls_selection": True,
            "cli_selector_invoked": True,
            "cli_selector_fallback": False,
            "cli_selector_failure": None,
            "cli_selector_workspace": str(workspace.root),
            "cli_selector_input_manifest": str(workspace.manifest_path),
            "cli_selector_raw_result": str(workspace.raw_result_path),
            "cli_selector_normalized_result": str(workspace.normalized_result_path),
            "cli_selector_independence_evidence": str(
                workspace.agent_directory / "independence.json"
            ),
            "opaque_candidate_mapping": str(
                workspace.agent_directory / "opaque-candidate-map.json"
            ),
        }
        return finalize_evidence_selection(
            plan,
            context,
            selected_attempt_id=selected_attempt_id,
            strategy=CLI_SELECTION_STRATEGY,
            selection_reason=result.reason,
            metadata=metadata,
        )


__all__ = ["CLI_SELECTION_STRATEGY", "CliSelectorAdapter"]
