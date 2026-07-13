from __future__ import annotations
import json
import hashlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from villani_ops.closed_loop.approvals import (
    ApprovalContext,
    ApprovalPolicy,
    ApprovalRecord,
    ApprovalRequirement,
    ApprovalRule,
    ApprovalScope,
    approval_requirements,
    validate_approval,
)
from villani_ops.closed_loop.delivery import (
    DeliveryMaterializerAdapter,
    FakeGitProvider,
    ProvenanceSigner,
    build_statement,
)
from villani_ops.closed_loop.interfaces import (
    EligibleCandidate,
    MaterializationContext,
    Selection,
)
from villani_ops.closed_loop.verification_graph import (
    EvidenceOutput,
    NodeResourceLimits,
    VerificationGraph,
    VerificationGraphExecutor,
    VerificationNode,
)

PATCH = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"


def node(
    node_id: str,
    kind: str,
    *,
    grade: str = "authoritative",
    required: bool = True,
    argv: list[str] | None = None,
    reruns: int = 0,
) -> VerificationNode:
    return VerificationNode(
        node_id=node_id,
        kind=kind,
        required=required,
        resource_limits=NodeResourceLimits(maximum_reruns=reruns),
        evidence_outputs=(
            EvidenceOutput(
                evidence_id=f"{node_id}.evidence", description=node_id, grade=grade
            ),
        ),
        configuration={"argv": argv} if argv else {},
    )


def execute(tmp_path: Path, *nodes: VerificationNode, llm_review=None):
    graph = VerificationGraph(graph_id="fixture", version="1.0.0", nodes=nodes)
    return VerificationGraphExecutor(llm_review=llm_review).execute(
        graph,
        run_id="run",
        attempt_id="attempt",
        repository=tmp_path,
        patch=PATCH,
        configuration={},
    )


def test_llm_evidence_cannot_be_declared_authoritative() -> None:
    with pytest.raises(ValidationError):
        node("review", "independent_llm_review", grade="authoritative")


def test_model_only_or_weak_evidence_is_not_acceptance_eligible(tmp_path: Path) -> None:
    result = execute(
        tmp_path,
        node("review", "independent_llm_review", grade="strong"),
        llm_review=lambda *_: {"passed": True, "grade": "authoritative"},
    )
    assert result.acceptance_eligible is False
    assert result.authoritative_acceptance_present is False


def test_required_failure_blocks_and_authoritative_pass_allows(tmp_path: Path) -> None:
    failed = execute(
        tmp_path,
        node(
            "tests",
            "targeted_test_command",
            argv=[sys.executable, "-c", "raise SystemExit(1)"],
        ),
    )
    passed = execute(
        tmp_path,
        node(
            "tests",
            "targeted_test_command",
            argv=[sys.executable, "-c", "raise SystemExit(0)"],
        ),
    )
    assert failed.required_failures == ("tests",) and not failed.acceptance_eligible
    assert passed.authoritative_acceptance_present and passed.acceptance_eligible


def test_flaky_command_is_conflicting_with_bounded_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = iter(
        [
            subprocess.CompletedProcess([], 1, b"fail", b""),
            subprocess.CompletedProcess([], 0, b"pass", b""),
        ]
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: next(outcomes))
    result = execute(
        tmp_path, node("tests", "repository_command", argv=["fake"], reruns=1)
    )
    assert result.flaky_nodes == ("tests",)
    assert result.conflicting_evidence == ("tests",)
    assert result.total_executions == 2 and result.total_reruns == 1


def test_all_builtin_node_kinds_have_deterministic_results(tmp_path: Path) -> None:
    nodes = (
        node("repo", "repository_command", argv=[sys.executable, "-c", "pass"]),
        node(
            "target",
            "targeted_test_command",
            argv=[sys.executable, "-c", "pass"],
            required=False,
        ),
        node(
            "static",
            "static_type_lint_command",
            argv=[sys.executable, "-c", "pass"],
            required=False,
        ),
        node("hygiene", "patch_hygiene_scope", required=False),
        node("secrets", "secret_scan", required=False),
        node(
            "dependencies", "dependency_security_scan", grade="strong", required=False
        ),
        node("trace", "trace_consistency", required=False),
        node("review", "independent_llm_review", grade="strong", required=False),
    )
    result = execute(tmp_path, *nodes)
    assert {item.kind for item in result.node_results} == {item.kind for item in nodes}
    assert result.acceptance_eligible
    assert (
        next(v for v in result.node_results if v.node_id == "dependencies").status
        == "failed"
    )


def approval_fixture(
    now: datetime,
) -> tuple[ApprovalRequirement, ApprovalContext, ApprovalRecord]:
    context = ApprovalContext(
        run_id="run",
        attempt_id="attempt",
        risk="high",
        repository="repo",
        paths=("src/a.py",),
        tool_actions=("patch_export",),
        evidence_gaps=(),
        cost_usd=2,
        materialization_type="patch_export",
    )
    requirement = ApprovalRequirement(
        rule_id="high", policy_version="7", reasons=("risk",)
    )
    record = ApprovalRecord(
        approval_id="approval",
        run_id="run",
        attempt_id="attempt",
        approver_identity="person@example.test",
        scope=ApprovalScope(
            repository="repo",
            paths=("src/a.py",),
            tool_actions=("patch_export",),
            materialization_type="patch_export",
            maximum_cost_usd=3,
        ),
        decision="approved",
        reason="reviewed",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        policy_version="7",
    )
    return requirement, context, record


def test_approval_policy_matches_all_dimensions_and_rejects_expiry_scope_override() -> (
    None
):
    now = datetime.now(timezone.utc)
    requirement, context, record = approval_fixture(now)
    policy = ApprovalPolicy(
        policy_version="7",
        rules=(
            ApprovalRule(
                rule_id="high",
                risks=("high",),
                repositories=("repo",),
                paths=("src/*",),
                tool_actions=("patch_export",),
                minimum_cost_usd=1,
                materialization_types=("patch_export",),
            ),
        ),
    )
    assert approval_requirements(policy, context) == (
        requirement.model_copy(update={"reasons": ("matched approval rule high",)}),
    )
    assert validate_approval(record, requirement, context, now=now).valid
    assert not validate_approval(
        record.model_copy(update={"expires_at": now}), requirement, context, now=now
    ).valid
    assert not validate_approval(
        record.model_copy(
            update={"scope": record.scope.model_copy(update={"paths": ()})}
        ),
        requirement,
        context,
        now=now,
    ).valid
    assert not validate_approval(
        record, requirement, context, now=now, required_authoritative_failure=True
    ).valid


def test_signed_provenance_covers_required_identity_and_detects_tampering() -> None:
    signer = ProvenanceSigner(b"test-only-key", key_id="test-key")
    statement = build_statement(
        run_id="run",
        attempt_id="attempt",
        patch_sha256="a" * 64,
        graph_id="graph",
        graph_version="2",
        evidence_digests=("e2", "e1"),
        approval_digests=("a1",),
        materializer_name="export",
        materializer_version="1",
        materialization_type="patch_export",
        key_id="test-key",
    )
    signed = signer.sign(statement)
    assert signer.verify(signed)
    assert not signer.verify(
        signed.model_copy(
            update={
                "statement": statement.model_copy(update={"patch_sha256": "b" * 64})
            }
        )
    )


def test_false_acceptance_and_false_rejection_fixture_suites_are_reported() -> None:
    root = Path(__file__).resolve().parents[5]
    false_acceptance = json.loads(
        (
            root / "integration/fixtures/verification_graph/false_acceptance.json"
        ).read_text()
    )
    false_rejection = json.loads(
        (
            root / "integration/fixtures/verification_graph/false_rejection.json"
        ).read_text()
    )
    assert len(false_acceptance["cases"]) == 3 and all(
        not c["expected_eligible"] for c in false_acceptance["cases"]
    )
    assert len(false_rejection["cases"]) == 2 and all(
        c["expected_eligible"] for c in false_rejection["cases"]
    )


def test_fake_pull_request_provider_is_idempotent() -> None:
    provider = FakeGitProvider()
    request = dict(
        idempotency_key="same",
        repository="repo",
        branch="branch",
        title="title",
        patch_sha256="a" * 64,
    )
    assert provider.create_or_get_pull_request(
        **request
    ) == provider.create_or_get_pull_request(**request)
    assert len(provider.requests) == 1


def test_patch_export_delivers_exact_verified_digest_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    digest = hashlib.sha256(PATCH.encode()).hexdigest()
    candidate = EligibleCandidate(
        attempt=SimpleNamespace(
            attempt_id="attempt",
            patch_sha256=digest,
            metadata={"changed_files": ["a.txt"]},
        ),
        verification=SimpleNamespace(),
        patch=PATCH,
    )
    destination = tmp_path / "out" / "candidate.patch"
    context = MaterializationContext(
        run_id="run",
        trace_id="trace",
        repository_path=str(tmp_path),
        selected_candidate=candidate,
        policy_configuration={
            "delivery": {
                "materialization_type": "patch_export",
                "destination": str(destination),
                "idempotency_key": "stable",
            }
        },
        run_directory=tmp_path,
    )
    adapter = DeliveryMaterializerAdapter()
    selection = Selection(selected_attempt_id="attempt", strategy="test", reason="test")
    first = adapter.materialize(selection, context)
    retry = adapter.materialize(selection, context)
    assert first.status == retry.status == "succeeded"
    assert (
        first.final_patch
        == retry.final_patch
        == destination.read_bytes().decode()
        == PATCH
    )
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest
    assert first.changed_files == retry.changed_files == ("a.txt",)
    assert retry.metadata["idempotent_replay"] is True
