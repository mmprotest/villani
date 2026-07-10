"""Safe materialization of exactly one selected canonical attempt patch."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from villani_ops.materialize import apply_patch_safely

from ..durable_io import write_json_atomic
from ..event_writer import redact_data, redact_message
from ..interfaces import (
    DependencyFailure,
    Materialization,
    MaterializationContext,
    Selection,
)
from ..protocol import FailureDetail, MaterializationSnapshot
from .git_isolation import validate_target_identity


class PatchMaterializerAdapter:
    def __init__(
        self,
        *,
        apply_service: Callable[[Path, Path], dict[str, Any]] = apply_patch_safely,
    ) -> None:
        self._apply_service = apply_service

    def materialize(
        self,
        selection: Selection,
        context: MaterializationContext,
    ) -> Materialization:
        started = datetime.now(timezone.utc)
        run_dir = Path(context.run_directory).resolve()
        selected = context.selected_candidate
        attempt_id = selected.attempt.attempt_id
        attempt_dir = (run_dir / "attempts" / attempt_id).resolve()
        source_value = selected.attempt.patch_path or "missing.patch"
        source_patch = Path(source_value)
        if not source_patch.is_absolute():
            source_patch = (run_dir / source_patch).resolve()
        target_repo = Path(context.repository_path).resolve()
        failure: DependencyFailure | None = None
        changed_files: tuple[str, ...] = ()
        final_patch: str | None = None
        final_report = ""
        apply_artifact = None
        try:
            if selection.selected_attempt_id != attempt_id:
                raise ValueError("materialization context is not the selected attempt")
            if not source_patch.is_relative_to(attempt_dir):
                raise ValueError(
                    "selected patch path resolves outside the canonical attempt directory"
                )
            if not source_patch.is_file():
                raise FileNotFoundError("selected recorded patch does not exist")
            patch_text = source_patch.read_text(encoding="utf-8", errors="replace")
            if not patch_text.strip():
                raise ValueError("selected recorded patch is empty")
            patch_hash = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
            if selected.attempt.patch_sha256 != patch_hash:
                raise ValueError("selected patch hash does not match attempt snapshot")
            if patch_text != selected.patch:
                raise ValueError("selected patch bytes differ from controller candidate")
            worktree = selected.attempt.metadata.get("worktree")
            if not isinstance(worktree, dict):
                raise ValueError("attempt is missing worktree baseline metadata")
            baseline = worktree.get("source_repository")
            if not isinstance(baseline, dict):
                raise ValueError("attempt is missing target repository identity metadata")
            validate_target_identity(target_repo, baseline)
            apply_artifact = self._apply_service(target_repo, source_patch)
            if apply_artifact.get("exit_code") != 0:
                raise RuntimeError("safe patch apply did not report success")
            changed_files = tuple(str(item) for item in apply_artifact.get("changed_files") or [])
            final_patch = patch_text
            final_report = (
                "# Materialization report\n\n"
                f"Applied selected attempt `{attempt_id}` to `{target_repo}`.\n"
            )
            (run_dir / "final.patch").write_text(final_patch, encoding="utf-8")
            (run_dir / "final_report.md").write_text(final_report, encoding="utf-8")
            status = "succeeded"
        except Exception as error:
            status = "failed"
            message = redact_message(str(error))
            failure = DependencyFailure(
                code="safe_apply_failed",
                message=message,
                details={"exception_class": error.__class__.__name__},
            )
            final_report = f"# Materialization report\n\nFailed: {message}\n"

        completed = datetime.now(timezone.utc)
        patch_hash = (
            hashlib.sha256(final_patch.encode("utf-8")).hexdigest()
            if final_patch is not None
            else None
        )
        snapshot = MaterializationSnapshot(
            schema_version="villani.materialization.v1",
            materialization_id="materialization_001",
            run_id=context.run_id,
            trace_id=context.trace_id,
            selection_id="selection_001",
            selected_attempt_id=attempt_id,
            started_at=started,
            completed_at=completed,
            status=status,
            source_patch_path=str(source_value),
            target_repository_path=str(target_repo),
            materialized_patch_path="final.patch" if status == "succeeded" else None,
            patch_sha256=patch_hash,
            changed_files=list(changed_files),
            failure=(
                FailureDetail(
                    code=failure.code,
                    message=failure.message,
                    details=dict(failure.details),
                )
                if failure is not None
                else None
            ),
            metadata={
                "safe_apply": redact_data(apply_artifact) if apply_artifact else None,
                "repository_identity_validated": status == "succeeded",
            },
        )
        write_json_atomic(
            run_dir / "materialization.json", snapshot.model_dump(mode="json")
        )
        return Materialization(
            status=status,  # type: ignore[arg-type]
            final_patch=final_patch,
            final_report=final_report,
            changed_files=changed_files,
            failure=failure,
            metadata={
                "safe_apply": redact_data(apply_artifact) if apply_artifact else None,
                "repository_identity_validated": status == "succeeded",
            },
        )
