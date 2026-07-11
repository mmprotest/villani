"""Approval enforcement immediately before any materialization side effect."""

from __future__ import annotations
import re
from pathlib import Path
from ..durable_io import write_json_atomic
from ..interfaces import (
    DependencyFailure,
    Materialization,
    MaterializationContext,
    Materializer,
    Selection,
)
from .models import ApprovalContext, ApprovalPolicy, ApprovalRecord
from .policy import approval_requirements, validate_approval


class ApprovalGuardedMaterializer:
    @property
    def plugin_manifest(self):
        return getattr(self.wrapped, "plugin_manifest", None)

    def __init__(self, wrapped: Materializer) -> None:
        self.wrapped = wrapped

    def materialize(
        self, selection: Selection, context: MaterializationContext
    ) -> Materialization:
        raw_policy = context.policy_configuration.get("approval_policy")
        if raw_policy is None:
            return self.wrapped.materialize(selection, context)
        try:
            policy = ApprovalPolicy.model_validate(raw_policy)
            candidate = context.selected_candidate
            delivery = context.policy_configuration.get("delivery", {})
            delivery = delivery if isinstance(delivery, dict) else {}
            materialization_type = str(
                delivery.get("materialization_type") or "local_patch_apply"
            )
            metadata = candidate.attempt.metadata
            paths = tuple(str(v) for v in metadata.get("changed_files", ()))
            if not paths:
                paths = tuple(
                    dict.fromkeys(
                        re.findall(r"^\+\+\+ b/(.+)$", candidate.patch, re.MULTILINE)
                        + re.findall(r"^--- a/(.+)$", candidate.patch, re.MULTILINE)
                    )
                )
            gaps = tuple(
                str(v)
                for v in candidate.verification.metadata.get(
                    "missing_required_evidence", ()
                )
            )
            approval_context = ApprovalContext(
                run_id=context.run_id,
                attempt_id=candidate.attempt.attempt_id,
                risk=str(context.risk or "unknown"),
                repository=str(Path(context.repository_path).resolve()),
                paths=paths,
                tool_actions=(materialization_type,),
                evidence_gaps=gaps,
                cost_usd=candidate.attempt.cost_usd,
                materialization_type=materialization_type,
            )
            requirements = approval_requirements(policy, approval_context)
            raw_records = context.policy_configuration.get("approval_records", ())
            records = (
                tuple(ApprovalRecord.model_validate(v) for v in raw_records)
                if isinstance(raw_records, (list, tuple))
                else ()
            )
            validations = []
            authoritative_failure = bool(
                candidate.verification.metadata.get("required_failures")
            )
            for requirement in requirements:
                matches = [
                    validate_approval(
                        record,
                        requirement,
                        approval_context,
                        required_authoritative_failure=authoritative_failure,
                    )
                    for record in records
                ]
                validations.append(
                    {
                        "requirement": requirement.model_dump(mode="json"),
                        "valid": any(v.valid for v in matches),
                        "results": [v.model_dump(mode="json") for v in matches],
                    }
                )
            write_json_atomic(
                Path(context.run_directory) / "approvals.json",
                {
                    "policy": policy.model_dump(mode="json"),
                    "records": [r.model_dump(mode="json") for r in records],
                    "validations": validations,
                },
            )
            if any(not v["valid"] for v in validations):
                raise PermissionError(
                    "required delivery approval is missing, expired, denied, or out of scope"
                )
            return self.wrapped.materialize(selection, context)
        except Exception as error:
            return Materialization(
                status="failed",
                final_patch=None,
                final_report=f"Approval blocked delivery: {error}",
                failure=DependencyFailure(code="approval_required", message=str(error)),
            )
