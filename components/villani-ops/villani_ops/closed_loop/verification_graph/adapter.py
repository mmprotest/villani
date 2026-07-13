"""Closed-loop adapter for the versioned verification graph."""

from __future__ import annotations
from pathlib import Path
from typing import Literal
from ..durable_io import write_json_atomic
from ..interfaces import (
    AttemptContext,
    AttemptResult,
    EvidenceItem,
    Requirement,
    Verification,
)
from ..plugins.builtins import VERIFIER_MANIFEST
from .executor import VerificationGraphExecutor
from .models import VerificationGraph


class VerificationGraphVerifierAdapter:
    plugin_manifest = VERIFIER_MANIFEST

    def __init__(
        self,
        graph: VerificationGraph,
        *,
        executor: VerificationGraphExecutor | None = None,
    ) -> None:
        self.graph, self.executor = graph, executor or VerificationGraphExecutor()

    def verify(
        self, attempt_context: AttemptContext, attempt_result: AttemptResult
    ) -> Verification:
        result = self.executor.execute(
            self.graph,
            run_id=attempt_context.run_id,
            attempt_id=attempt_context.attempt_id,
            repository=Path(attempt_result.worktree_path),
            patch=attempt_result.patch or "",
            configuration=attempt_context.policy_configuration,
            trace=attempt_result.trace,
        )
        run_dir = Path(attempt_context.run_directory)
        artifact = (
            run_dir / "verification" / "graphs" / f"{attempt_context.attempt_id}.json"
        )
        write_json_atomic(artifact, result.model_dump(mode="json"))
        requirements, success, failure, missing = [], [], [], []
        for node in result.node_results:
            if node.required:
                outcome: Literal["passed", "failed", "missing", "not_applicable"] = (
                    "passed"
                    if node.status == "passed"
                    else "missing"
                    if node.status == "skipped"
                    else "failed"
                )
                requirements.append(
                    Requirement(
                        node.node_id,
                        node.kind.replace("_", " "),
                        outcome,
                        tuple(e.evidence_id for e in node.evidence),
                    )
                )
            for evidence in node.evidence:
                item = EvidenceItem(
                    evidence.evidence_id,
                    node.kind,
                    evidence.summary,
                    artifact.relative_to(run_dir).as_posix(),
                    {
                        "grade": evidence.grade,
                        "digest_sha256": evidence.digest_sha256,
                        "node_id": node.node_id,
                    },
                )
                if evidence.grade == "missing" or evidence.passed is None:
                    if node.required:
                        missing.append(item)
                elif evidence.passed:
                    success.append(item)
                else:
                    failure.append(item)
        blockers = list(
            result.required_failures
            + result.missing_required_evidence
            + result.conflicting_evidence
        )
        return Verification(
            verifier="verification_graph",
            outcome="accepted" if result.acceptance_eligible else "rejected",
            acceptance_eligible=result.acceptance_eligible,
            confidence=1.0
            if result.authoritative_acceptance_present
            and not result.verifier_disagreement
            else 0.5,
            reason="Verification graph accepted the candidate."
            if result.acceptance_eligible
            else "Verification graph blocked acceptance: " + ", ".join(blockers),
            recommended_action="accept" if result.acceptance_eligible else "reject",
            requirement_results=tuple(requirements),
            success_evidence=tuple(success),
            failure_evidence=tuple(failure),
            missing_evidence=tuple(missing),
            risk_flags=tuple(f"acceptance_blocker:{v}" for v in blockers),
            raw_verifier_artifact=artifact.relative_to(run_dir).as_posix(),
            metadata={
                "verification_graph_id": result.graph_id,
                "verification_graph_version": result.graph_version,
                "eligibility_rule_version": result.eligibility_rule_version,
                "authoritative_acceptance_present": result.authoritative_acceptance_present,
                "required_failures": list(result.required_failures),
                "missing_required_evidence": list(result.missing_required_evidence),
                "conflicting_evidence": list(result.conflicting_evidence),
                "verifier_disagreement": result.verifier_disagreement,
                "flaky_nodes": list(result.flaky_nodes),
                "total_executions": result.total_executions,
                "total_reruns": result.total_reruns,
                "verification_mode": "authoritative_verification_graph",
                "authority_source": "authoritative_verification_graph",
            },
        )
