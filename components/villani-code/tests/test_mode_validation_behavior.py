from villani_code.planning import TaskMode
from villani_code.project_memory import ValidationConfig, ValidationStep
from villani_code.validation_loop import plan_validation


def _cfg() -> ValidationConfig:
    return ValidationConfig(
        steps=[
            ValidationStep("fmt", "echo fmt", "format", 1, False),
            ValidationStep("lint", "echo lint", "lint", 1, False),
            ValidationStep("type", "echo type", "typecheck", 2, False),
            ValidationStep("pytest", "python -m pytest", "test", 3, False, scope_hint="targeted"),
            ValidationStep("inspect", "git diff --stat", "inspection", 1, False),
        ]
    )


def test_docs_only_validation_skip_code_checks() -> None:
    plan = plan_validation(_cfg(), ["README.md"], task_mode=TaskMode.DOCS_UPDATE_SAFE.value)
    names = [s.step.name for s in plan.selected_steps]
    assert "pytest" not in names
    assert "type" not in names


def test_inspect_repo_mode_no_write_validation() -> None:
    plan = plan_validation(_cfg(), [], task_mode=TaskMode.INSPECT_AND_PLAN.value)
    names = [s.step.name for s in plan.selected_steps]
    assert names == ["fmt", "lint", "inspect"] or names == ["inspect"]
