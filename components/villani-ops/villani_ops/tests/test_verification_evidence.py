from __future__ import annotations

import pytest
from pydantic import ValidationError

from villani_ops.closed_loop.verification_evidence import (
    CandidateEligibility,
    RequirementEvidence,
    RepositoryValidationDecisionInput,
    SemanticReviewDecisionInput,
    compute_final_verification_decision,
    evidence_matrix,
    extract_requirements,
    stable_requirement_id,
)
from villani_ops.closed_loop.schema_validation import (
    parse_protocol_document,
    validate_protocol_document,
)


def _candidate(
    status: str = "eligible",
    *,
    completed: bool = True,
) -> CandidateEligibility:
    return CandidateEligibility(
        status=status,
        runner_completed_sufficiently=completed,
        reason=status,
    )


def _repository(
    status: str = "passed",
    *,
    authoritative: bool = True,
    required: bool = True,
    failure_code: str | None = None,
) -> RepositoryValidationDecisionInput:
    return RepositoryValidationDecisionInput(
        status=status,
        authoritative=authoritative,
        required=required,
        failure_code=failure_code,
    )


def _semantic(
    result: int | None = 1,
    verdict: str = "success",
    *,
    schema_valid: bool = True,
    critical_failure: bool = False,
) -> SemanticReviewDecisionInput:
    return SemanticReviewDecisionInput(
        raw_result=result,
        verdict=verdict,
        recommended_action="accept" if result == 1 else "reject",
        schema_valid=schema_valid,
        critical_failure_reported=critical_failure,
    )


def _requirement(
    *,
    deterministic: str = "passed",
    semantic: str = "passed",
    final: str = "passed",
    evidence_type: str = "repository_validation",
    contradiction: bool = False,
) -> RequirementEvidence:
    return RequirementEvidence(
        requirement_id="req-exact",
        description="The command must return the exact value.",
        critical=True,
        evidence_type=evidence_type,
        evidence_ids=["evidence-1"],
        deterministic_status=deterministic,
        semantic_status=semantic,
        contradiction=contradiction,
        final_status=final,
        reason="test evidence",
    )


def _decision(
    *,
    candidate: CandidateEligibility | None = None,
    repository: RepositoryValidationDecisionInput | None = None,
    requirements: list[RequirementEvidence] | None = None,
    semantic: SemanticReviewDecisionInput | None = None,
    invocation: str = "completed",
):
    return compute_final_verification_decision(
        candidate or _candidate(),
        repository or _repository(),
        requirements or [_requirement()],
        semantic or _semantic(),
        invocation,
    )


def test_all_acceptance_gates_produce_result_one() -> None:
    decision = _decision()
    assert decision.result == 1
    assert decision.reason_code == "accepted"


def test_repository_validation_failure_overrides_semantic_success() -> None:
    decision = _decision(repository=_repository("failed"))
    assert decision.result == 0
    assert decision.reason_code == "repository_validation_failed"


def test_exact_output_probe_failure_overrides_semantic_success() -> None:
    decision = _decision(
        requirements=[
            _requirement(
                deterministic="failed",
                semantic="passed",
                final="failed",
                evidence_type="focused_probe",
                contradiction=True,
            )
        ]
    )
    assert decision.result == 0
    assert decision.reason_code == "focused_probe_failed"


def test_exact_output_probe_pass_allows_acceptance() -> None:
    decision = _decision(requirements=[_requirement(evidence_type="focused_probe")])
    assert decision.result == 1


def test_directly_testable_critical_requirement_with_semantic_only_fails_closed() -> (
    None
):
    decision = _decision(
        requirements=[
            _requirement(
                deterministic="missing",
                semantic="passed",
                final="missing",
                evidence_type="focused_probe",
            )
        ]
    )
    assert decision.result == 0
    assert decision.reason_code == "focused_probe_missing"


def test_non_executable_requirement_with_source_evidence_is_permitted() -> None:
    requirement = _requirement(evidence_type="source_inspection")
    requirement = requirement.model_copy(
        update={"description": "Create the required configuration artifact."}
    )
    assert _decision(requirements=[requirement]).result == 1


@pytest.mark.parametrize(
    ("invocation", "schema_valid", "reason_code"),
    [
        ("completed", False, "verifier_malformed_output"),
        ("timeout", False, "verifier_tool_failure"),
    ],
)
def test_verifier_failures_retry_without_acceptance(
    invocation: str,
    schema_valid: bool,
    reason_code: str,
) -> None:
    decision = _decision(
        semantic=_semantic(None, "error", schema_valid=schema_valid),
        invocation=invocation,
    )
    assert decision.result == 0
    assert decision.reason_code == reason_code
    assert decision.recommended_action == "retry_verifier"
    assert decision.retry_scope == "verification"


def test_probe_timeout_is_infrastructure_not_implementation_failure() -> None:
    decision = _decision(
        requirements=[
            _requirement(
                deterministic="infrastructure_error",
                semantic="unclear",
                final="infrastructure_error",
                evidence_type="focused_probe",
            )
        ],
        semantic=_semantic(0, "unclear"),
    )
    assert decision.result == 0
    assert decision.reason_code == "verifier_tool_failure"
    assert decision.recommended_action == "retry_verifier"


def test_repository_environment_mismatch_is_infrastructure_failure() -> None:
    decision = _decision(
        repository=_repository(
            "infrastructure_error",
            failure_code="repository_validation_environment_mismatch",
        )
    )
    assert decision.result == 0
    assert decision.reason_code == "repository_validation_infrastructure_error"
    assert decision.retry_scope == "repository_validation"


@pytest.mark.parametrize(
    ("candidate", "reason_code"),
    [
        (_candidate("empty_patch"), "empty_patch"),
        (_candidate("ineligible", completed=False), "candidate_ineligible"),
    ],
)
def test_candidate_eligibility_failures_are_deterministic(
    candidate: CandidateEligibility,
    reason_code: str,
) -> None:
    decision = _decision(candidate=candidate)
    assert decision.result == 0
    assert decision.reason_code == reason_code


def test_raw_llm_success_disagrees_with_failed_deterministic_evidence() -> None:
    requirement = _requirement(
        deterministic="failed",
        semantic="passed",
        final="failed",
        evidence_type="focused_probe",
        contradiction=True,
    )
    decision = _decision(requirements=[requirement])
    matrix = evidence_matrix(
        run_id="run-1",
        attempt_id="attempt-1",
        requirements=[requirement],
        repository_validation_status="passed",
        candidate_eligibility_status="eligible",
        semantic_verifier_status="success",
        decision=decision,
    )
    assert matrix.final_result == 0
    assert matrix.contradictions_present is True
    validate_protocol_document(matrix.model_dump(mode="json"))
    assert isinstance(
        parse_protocol_document(matrix.model_dump(mode="json")),
        type(matrix),
    )


def test_raw_llm_rejection_remains_a_veto_when_deterministic_gates_pass() -> None:
    decision = _decision(semantic=_semantic(0, "failure"))
    assert decision.result == 0
    assert decision.reason_code == "semantic_verifier_rejected"


def test_critical_requirement_requires_an_evidence_reference() -> None:
    with pytest.raises(ValidationError):
        RequirementEvidence(
            requirement_id="req-1",
            description="Critical behavior.",
            critical=True,
            evidence_type="semantic_reasoning",
            evidence_ids=[],
            deterministic_status="missing",
            semantic_status="unclear",
            contradiction=False,
            final_status="missing",
            reason="missing",
        )


def test_requirement_ids_are_stable_after_list_normalization() -> None:
    assert stable_requirement_id("- Must return the exact value.") == (
        stable_requirement_id("Must return the exact value")
    )


def test_requirement_extraction_marks_observable_and_negative_constraints_critical() -> (
    None
):
    requirements = extract_requirements(
        task_instruction=(
            "The API must return the exact text `ok`.\nDo not change public.py."
        ),
        success_criteria="tests/test_api.py must pass.",
        policy_configuration={
            "repository_validation_commands": [
                {
                    "validation_id": "api-tests",
                    "argv": ["pytest", "tests/test_api.py"],
                }
            ]
        },
    )

    assert any(
        item.observable and "exact text" in item.description for item in requirements
    )
    assert any(
        item.critical and item.description.startswith("Do not") for item in requirements
    )
    assert any(item.source == "repository_validation_command" for item in requirements)
