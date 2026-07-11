"""Versioned verification graph and graded evidence contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EvidenceGrade = Literal["authoritative", "strong", "weak", "missing", "conflicting"]
NodeKind = Literal[
    "repository_command",
    "targeted_test_command",
    "static_type_lint_command",
    "patch_hygiene_scope",
    "secret_scan",
    "dependency_security_scan",
    "trace_consistency",
    "independent_llm_review",
]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeResourceLimits(FrozenModel):
    timeout_seconds: float = Field(default=300, gt=0, le=86_400)
    maximum_output_bytes: int = Field(default=1_048_576, ge=1)
    maximum_reruns: int = Field(default=0, ge=0, le=5)
    cpu_count: float | None = Field(default=None, gt=0)
    memory_bytes: int | None = Field(default=None, ge=1)


class EvidenceOutput(FrozenModel):
    evidence_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    grade: EvidenceGrade


class VerificationNode(FrozenModel):
    node_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    kind: NodeKind
    dependencies: tuple[str, ...] = ()
    condition: dict[str, Any] = Field(default_factory=lambda: {"type": "always"})
    resource_limits: NodeResourceLimits = NodeResourceLimits()
    required: bool = True
    evidence_outputs: tuple[EvidenceOutput, ...] = ()
    configuration: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_llm_grade(self) -> "VerificationNode":
        if self.kind == "independent_llm_review" and any(
            item.grade == "authoritative" for item in self.evidence_outputs
        ):
            raise ValueError("LLM review evidence cannot be authoritative")
        return self


class VerificationGraph(FrozenModel):
    schema_version: Literal["villani.verification_graph.v1"] = (
        "villani.verification_graph.v1"
    )
    graph_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    nodes: tuple[VerificationNode, ...]

    @model_validator(mode="after")
    def validate_graph(self) -> "VerificationGraph":
        ids = [item.node_id for item in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("verification node ids must be unique")
        known = set(ids)
        for node in self.nodes:
            missing = set(node.dependencies) - known
            if missing:
                raise ValueError(
                    f"node {node.node_id} has unknown dependencies: {sorted(missing)}"
                )
            if node.node_id in node.dependencies:
                raise ValueError("verification node cannot depend on itself")
        pending = {item.node_id: set(item.dependencies) for item in self.nodes}
        resolved: set[str] = set()
        while pending:
            ready = {key for key, deps in pending.items() if deps <= resolved}
            if not ready:
                raise ValueError("verification graph contains a dependency cycle")
            resolved.update(ready)
            for key in ready:
                pending.pop(key)
        return self


class GradedEvidence(FrozenModel):
    evidence_id: str
    node_id: str
    grade: EvidenceGrade
    passed: bool | None
    summary: str
    digest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_path: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class NodeExecutionAccounting(FrozenModel):
    executions: int = Field(ge=0)
    reruns: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    output_truncated: bool = False


class VerificationNodeResult(FrozenModel):
    node_id: str
    kind: NodeKind
    required: bool
    status: Literal["passed", "failed", "skipped", "error", "conflicting"]
    reason: str
    evidence: tuple[GradedEvidence, ...] = ()
    flaky: bool = False
    accounting: NodeExecutionAccounting


class VerificationGraphResult(FrozenModel):
    schema_version: Literal["villani.verification_graph_result.v1"] = (
        "villani.verification_graph_result.v1"
    )
    graph_id: str
    graph_version: str
    run_id: str
    attempt_id: str
    completed_at: datetime
    node_results: tuple[VerificationNodeResult, ...]
    acceptance_eligible: bool
    authoritative_acceptance_present: bool
    required_failures: tuple[str, ...]
    missing_required_evidence: tuple[str, ...]
    conflicting_evidence: tuple[str, ...]
    verifier_disagreement: bool
    flaky_nodes: tuple[str, ...]
    total_executions: int = Field(ge=0)
    total_reruns: int = Field(ge=0)
    eligibility_rule_version: Literal["villani.evidence_eligibility.v1"] = (
        "villani.evidence_eligibility.v1"
    )
