from __future__ import annotations

from pathlib import Path

from villani_ops.closed_loop.schema_validation import (
    parse_protocol_document,
    validate_protocol_document,
)
from villani_ops.closed_loop.validation_coverage import (
    ValidationCoverageReport,
    build_validation_coverage,
    legacy_validation_coverage,
)
from villani_ops.execution_environment.models import (
    CandidatePatchQuality,
    RepositoryValidationCommandResult,
    RepositoryValidationReport,
)


TIMESTAMP = "2026-07-17T00:00:00Z"


def _report(argv: list[str] | None = None) -> RepositoryValidationReport:
    command = RepositoryValidationCommandResult(
        validation_id="repository_validation_001",
        argv=argv or ["python", "-m", "pytest", "-q"],
        command_role="repository_validation",
        status="passed",
        exit_code=0,
        duration_ms=20,
        stdout="1 test passed",
        stderr="",
        stdout_bytes=13,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        execution_environment_fingerprint="environment",
        execution_provider="inherit",
        worktree_path="/candidate",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
        started_at=TIMESTAMP,
        completed_at=TIMESTAMP,
        failure_code="repository_validation_passed",
    )
    return RepositoryValidationReport(
        schema_version="villani.repository_validation.v2",
        run_id="run_coverage",
        attempt_id="attempt_001",
        candidate_id="candidate_001",
        execution_environment_fingerprint="environment",
        execution_provider="inherit",
        commands=[command],
        status="passed",
        authoritative=True,
        completed_at=TIMESTAMP,
        failure_code="repository_validation_passed",
    )


def _quality() -> CandidatePatchQuality:
    return CandidatePatchQuality(
        schema_version="villani.candidate_patch_quality.v1",
        candidate_id="candidate_001",
        status="eligible",
        tracked_files_changed=["src/names.py", "tests/test_names.py"],
        relevant_files_changed=["src/names.py", "tests/test_names.py"],
        untracked_files=[],
        ignored_files=[],
        villani_owned_files=[],
        generated_files=[],
        semantic_lines_added=4,
        semantic_lines_removed=1,
        line_ending_only_lines=0,
        whitespace_only_lines=0,
        file_mode_only_changes=[],
        bulk_rewrite_files=[],
        relevant_diff_ratio=1.0,
        reason_codes=[],
    )


def _configuration() -> dict[str, object]:
    return {
        "repository_validation_commands": [
            {
                "validation_id": "repository_validation_001",
                "argv": ["python", "-m", "pytest", "-q"],
            }
        ]
    }


def test_validation_coverage_links_changed_behavior_test_and_execution(
    tmp_path: Path, monkeypatch
) -> None:
    def added_text(_: Path, relative: str, *, untracked: bool) -> str:
        del untracked
        if relative == "src/names.py":
            return "normalize_name strips surrounding whitespace"
        return "test normalize_name strips surrounding whitespace"

    monkeypatch.setattr(
        "villani_ops.closed_loop.validation_coverage._git_added_text", added_text
    )
    report = build_validation_coverage(
        worktree=tmp_path,
        task_instruction="normalize_name must trim surrounding whitespace.",
        success_criteria=(
            "normalize_name must trim surrounding whitespace. "
            "The existing timezone offsets must remain unchanged."
        ),
        policy_configuration=_configuration(),
        repository_validation=_report(),
        candidate_quality=_quality(),
    )

    covered_descriptions = {
        requirement_id
        for command in report.commands
        for requirement_id in command.requirement_ids_covered
    }
    assert covered_descriptions == set(report.requirements_covered)
    assert report.requirements_not_covered
    assert report.commands[0].changed_test_files_plausibly_included == [
        "tests/test_names.py"
    ]
    assert "changed_behavior_test_validation_graph" in report.commands[0].coverage_provenance
    validate_protocol_document(report.model_dump(mode="json"))
    assert isinstance(
        parse_protocol_document(report.model_dump(mode="json")),
        ValidationCoverageReport,
    )


def test_generic_passing_suite_does_not_prove_unrelated_requirement(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "villani_ops.closed_loop.validation_coverage._git_added_text",
        lambda *_args, **_kwargs: "normalize_name strips whitespace",
    )
    report = build_validation_coverage(
        worktree=tmp_path,
        task_instruction="normalize_name must trim surrounding whitespace.",
        success_criteria="The timezone offset must remain unchanged.",
        policy_configuration=_configuration(),
        repository_validation=_report(),
        candidate_quality=_quality(),
    )

    assert report.requirements_not_covered
    assert set(report.requirements_covered).isdisjoint(report.requirements_not_covered)


def test_legacy_migration_never_infers_behavior_coverage() -> None:
    report = legacy_validation_coverage(
        repository_validation=_report(),
        task_instruction="normalize_name must trim surrounding whitespace.",
        success_criteria="The timezone offset must remain unchanged.",
        policy_configuration=_configuration(),
    )

    assert report.migration == {
        "source_schema_version": "villani.repository_validation.v2",
        "mode": "conservative_read_projection",
        "behavior_coverage_inferred": False,
    }
    assert report.requirements_not_covered
    assert all(
        "changed_behavior_test_validation_graph" not in command.coverage_provenance
        for command in report.commands
    )


def test_command_identity_is_stable_and_safe_display_redacts_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "villani_ops.closed_loop.validation_coverage._git_added_text",
        lambda *_args, **_kwargs: "normalize_name strips whitespace",
    )
    argv = ["runner", "--api-key", "do-not-record", "test"]
    first = build_validation_coverage(
        worktree=tmp_path,
        task_instruction="normalize_name must trim surrounding whitespace.",
        success_criteria="Tests must pass.",
        policy_configuration=_configuration(),
        repository_validation=_report(argv),
        candidate_quality=_quality(),
    )
    second = build_validation_coverage(
        worktree=tmp_path,
        task_instruction="normalize_name must trim surrounding whitespace.",
        success_criteria="Tests must pass.",
        policy_configuration=_configuration(),
        repository_validation=_report(argv),
        candidate_quality=_quality(),
    )

    assert first.commands[0].command_identity == second.commands[0].command_identity
    serialized = first.model_dump_json()
    assert "do-not-record" not in serialized
    assert "[REDACTED]" in serialized
