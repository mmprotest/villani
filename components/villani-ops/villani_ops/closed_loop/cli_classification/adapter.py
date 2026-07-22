"""Read-only Codex/Claude classifier behind the existing Classifier port."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from pydantic import ValidationError

from villani_ops.classification.classifier import (
    adjust_classification_from_task_shape,
)
from villani_ops.classification.context import (
    collect_relevant_file_snippets,
    is_skipped_repo_file,
)
from villani_ops.core.task import TaskClassification

from ..agent_systems.role_models import AgentRole
from ..claude_code_cli.driver import ClaudeCodeCliDriver
from ..claude_code_cli.models import ClaudeProbeResult
from ..cli_roles.models import (
    CliRoleFailure,
    DuplicateJsonFieldError,
    normalize_cli_classifier_result,
)
from ..cli_roles.prompts import CLASSIFIER_PROMPT_VERSION, build_classifier_prompt
from ..cli_roles.runtime import CliRoleExecution, execute_cli_role
from ..cli_roles.workspace import (
    CliRoleWorkspaceError,
    PreparedCliRoleWorkspace,
    prepare_cli_role_workspace,
)
from ..codex_cli.driver import CodexCliDriver
from ..codex_cli.models import CodexProbeResult
from ..durable_io import write_json_atomic
from ..event_writer import redact_message
from ..interfaces import Classification, ClassificationContext


SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas" / "v1"
PACKAGE_FILES = frozenset(
    {
        "Cargo.toml",
        "go.mod",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
    }
)

CliDriver: TypeAlias = CodexCliDriver | ClaudeCodeCliDriver
CliProbe: TypeAlias = CodexProbeResult | ClaudeProbeResult


def _repository_inventory(
    repository: Path, *, limit: int = 200
) -> tuple[list[str], bool]:
    values: list[str] = []
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repository,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            values = [
                item.decode("utf-8", errors="replace").replace("\\", "/")
                for item in result.stdout.split(b"\0")
                if item
            ]
    except (OSError, subprocess.SubprocessError):
        values = []
    if not values:
        values = [
            path.relative_to(repository).as_posix()
            for path in repository.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and not is_skipped_repo_file(path.relative_to(repository))
        ]
    ordered = sorted(dict.fromkeys(values))
    return ordered[:limit], len(ordered) > limit


def _repository_metadata(
    repository: Path, inventory: list[str], truncated: bool
) -> dict[str, Any]:
    extensions = Counter(Path(item).suffix.lower() or "<none>" for item in inventory)
    clean: bool | None = None
    changed_count: int | None = None
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=repository,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            clean = not lines
            changed_count = len(lines)
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "schema_version": "villani.cli_classifier_repository_metadata.v1",
        "tracked_file_count_supplied": len(inventory),
        "inventory_truncated": truncated,
        "tracked_files": inventory,
        "package_files": [
            item for item in inventory if Path(item).name in PACKAGE_FILES
        ],
        "extension_counts": dict(sorted(extensions.items())),
        "tracked_state_clean": clean,
        "tracked_changed_file_count": changed_count,
    }


def _policy_projection(context: ClassificationContext) -> dict[str, Any]:
    raw = context.policy_configuration.get("classification_policy")
    policy = raw if isinstance(raw, Mapping) else {}
    adjustments: list[dict[str, Any]] = []
    configured = policy.get("adjustments")
    for value in configured if isinstance(configured, list) else []:
        if not isinstance(value, Mapping):
            continue
        adjustments.append(
            {
                key: value[key]
                for key in (
                    "field",
                    "after",
                    "rule_id",
                    "reason",
                    "allow_reduction",
                )
                if key in value
            }
        )
    return {
        "schema_version": "villani.cli_classifier_policy_metadata.v1",
        "requires_file_changes": context.requires_file_changes,
        "difficulty_floor": policy.get("difficulty_floor"),
        "risk_floor": policy.get("risk_floor"),
        "configured_adjustments": adjustments,
    }


def _fallback(
    failure: CliRoleFailure,
    reason: str,
    *,
    workspace: PreparedCliRoleWorkspace | None,
    execution: CliRoleExecution | None = None,
) -> Classification:
    public_reason = redact_message(reason)
    normalized_path: str | None = None
    workspace_path: str | None = None
    if workspace is not None:
        workspace_path = str(workspace.root)
        normalized_path = str(workspace.normalized_result_path)
        try:
            write_json_atomic(
                workspace.normalized_result_path,
                {
                    "schema_version": "villani.cli_classifier_normalized_result.v1",
                    "status": "infrastructure_failure",
                    "failure_code": failure.value,
                    "fallback_used": True,
                    "effective_classification": {
                        "difficulty": "hard",
                        "risk": "high",
                        "category": "unknown",
                        "required_capabilities": [],
                        "confidence": 0.0,
                    },
                    "reason": public_reason,
                },
            )
        except Exception:
            normalized_path = None
    return Classification(
        difficulty="hard",
        risk="high",
        category="unknown",
        required_capabilities=(),
        estimated_attempts_needed=1,
        needs_tests=True,
        confidence=0.0,
        reasoning_summary=(
            "CLI classifier failed to produce a valid result; used conservative fallback."
        ),
        signals={"uncertainty": "high"},
        metadata={
            "classifier_version": "villani_cli_classifier_adapter_v1",
            "classification_fallback": True,
            "classification_fallback_reason": public_reason,
            "cli_classifier_failure": failure.value,
            "cli_classifier_workspace": workspace_path,
            "cli_classifier_normalized_result": normalized_path,
            "cli_classifier_process_spawned": (
                execution.process_spawned if execution is not None else False
            ),
            "model_classification": {
                "difficulty": "hard",
                "risk": "high",
                "category": "unknown",
                "required_capabilities": [],
                "confidence": 0.0,
            },
        },
    )


class CliClassifierAdapter:
    """Normalize isolated CLI classification into the canonical Classification."""

    def __init__(self, driver: CliDriver, *, probe: CliProbe) -> None:
        if driver.system.roles != {AgentRole.CLASSIFICATION}:
            raise ValueError("CLI classifier requires a classification-only system")
        self.driver = driver
        self.probe = probe

    def classify(self, task: str, context: ClassificationContext) -> Classification:
        workspace: PreparedCliRoleWorkspace | None = None
        if context.run_directory is None:
            return _fallback(
                CliRoleFailure.ARTIFACT_PREPARATION_FAILURE,
                "CLI classification requires a governed run directory.",
                workspace=None,
            )
        repository = Path(context.repository_path).resolve()
        try:
            inventory, truncated = _repository_inventory(repository)
            repository_metadata = _repository_metadata(repository, inventory, truncated)
            task_document = {
                "schema_version": "villani.cli_classifier_task.v1",
                "task": task,
            }
            criteria_document = {
                "schema_version": "villani.cli_classifier_success_criteria.v1",
                "success_criteria": context.success_criteria,
            }
            policy_document = _policy_projection(context)
            prompt = build_classifier_prompt(
                task=task_document,
                success_criteria=criteria_document,
                repository_metadata=repository_metadata,
                policy_metadata=policy_document,
            )
            invocation_id = f"classification-{uuid.uuid4().hex}"
            workspace = prepare_cli_role_workspace(
                role="classification",
                invocation_id=invocation_id,
                run_directory=Path(context.run_directory),
                target_repository=repository,
                input_documents={
                    "task.json": ("verbatim_task", task_document),
                    "success-criteria.json": (
                        "verbatim_success_criteria",
                        criteria_document,
                    ),
                    "repository-metadata.json": (
                        "conservative_repository_metadata",
                        repository_metadata,
                    ),
                    "policy-metadata.json": (
                        "risk_policy_metadata",
                        policy_document,
                    ),
                },
                prompt_bytes=prompt.bytes,
                output_schema_source=SCHEMA_ROOT / "cli-classifier-result.schema.json",
                raw_result_filename="classifier-result.json",
                normalized_result_filename="normalized-result.json",
                blindness={
                    "candidate_output": False,
                    "future_patch": False,
                    "expected_solution": False,
                    "benchmark_id": False,
                    "hidden_validation": False,
                    "other_classifier_output": False,
                    "provider_recommendation": False,
                    "coder_worktree": False,
                    "verifier_artifacts": False,
                    "selector_artifacts": False,
                },
            )
        except (OSError, ValueError, CliRoleWorkspaceError) as error:
            return _fallback(
                CliRoleFailure.ARTIFACT_PREPARATION_FAILURE,
                str(error),
                workspace=workspace,
            )

        execution = execute_cli_role(
            driver=self.driver,
            probe=self.probe,
            role=AgentRole.CLASSIFICATION,
            workspace=workspace,
            run_id=context.run_id,
            cancellation_event=context.cancellation_event,
        )
        if execution.failure is not None:
            return _fallback(
                execution.failure,
                execution.reason,
                workspace=workspace,
                execution=execution,
            )
        try:
            raw = normalize_cli_classifier_result(
                execution.raw_text,
                repository_inventory=set(inventory),
            )
        except (json.JSONDecodeError, DuplicateJsonFieldError) as error:
            return _fallback(
                CliRoleFailure.MALFORMED_OUTPUT,
                f"CLI classifier result was malformed: {error}",
                workspace=workspace,
                execution=execution,
            )
        except (ValueError, ValidationError) as error:
            return _fallback(
                CliRoleFailure.SCHEMA_FAILURE,
                f"CLI classifier result normalization failed: {error}",
                workspace=workspace,
                execution=execution,
            )

        task_text = "\n".join((task, context.success_criteria))
        snippets = collect_relevant_file_snippets(
            repository,
            task_text,
            inventory,
            likely_files=raw.likely_files,
        )
        model_classification = TaskClassification(
            difficulty=raw.difficulty,
            risk=raw.risk,
            category=raw.category,
            required_capabilities=raw.required_capabilities,
            estimated_attempts_needed=raw.estimated_attempts_needed,
            needs_tests=raw.needs_tests,
            likely_files=raw.likely_files,
            confidence=raw.confidence,
            reasoning_summary=raw.reasoning_summary,
        )
        effective = adjust_classification_from_task_shape(
            model_classification, task_text, snippets
        )
        normalized = {
            "schema_version": "villani.cli_classifier_normalized_result.v1",
            "status": "succeeded",
            "failure_code": None,
            "prompt_version": CLASSIFIER_PROMPT_VERSION,
            "raw_classification": raw.model_dump(mode="json"),
            "effective_classification": effective.model_dump(mode="json"),
            "fallback_used": False,
            "input_manifest_verified": execution.input_integrity_proved,
            "target_repository_unchanged": execution.target_unchanged,
        }
        try:
            write_json_atomic(workspace.normalized_result_path, normalized)
        except Exception as error:
            return _fallback(
                CliRoleFailure.ARTIFACT_PREPARATION_FAILURE,
                f"CLI classifier normalized artifact could not be written: {error}",
                workspace=workspace,
                execution=execution,
            )
        return Classification(
            difficulty=effective.difficulty,
            risk=effective.risk,
            category=effective.category,
            required_capabilities=tuple(effective.required_capabilities),
            estimated_attempts_needed=effective.estimated_attempts_needed,
            needs_tests=effective.needs_tests,
            confidence=effective.confidence,
            reasoning_summary=effective.reasoning_summary,
            signals={
                **effective.task_shape_signals,
                "uncertainty": raw.uncertainty,
            },
            metadata={
                "classifier_version": "villani_cli_classifier_adapter_v1",
                "classification_fallback": False,
                "cli_classifier_workspace": str(workspace.root),
                "cli_classifier_input_manifest": str(workspace.manifest_path),
                "cli_classifier_raw_result": str(workspace.raw_result_path),
                "cli_classifier_normalized_result": str(
                    workspace.normalized_result_path
                ),
                "cli_classifier_independence_evidence": str(
                    workspace.agent_directory / "independence.json"
                ),
                "cli_classifier_process_spawned": execution.process_spawned,
                "model_classification": {
                    "difficulty": raw.difficulty,
                    "risk": raw.risk,
                    "category": raw.category,
                    "required_capabilities": raw.required_capabilities,
                    "confidence": raw.confidence,
                    "uncertainty": raw.uncertainty,
                },
                "original_difficulty": effective.original_difficulty,
                "original_risk": effective.original_risk,
                "classification_adjustment_notes": effective.adjustment_notes,
                "relevant_file_paths": effective.relevant_file_paths,
            },
        )


__all__ = ["CliClassifierAdapter"]
