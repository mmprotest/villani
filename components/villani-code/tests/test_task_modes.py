from villani_code.planning import TaskMode, classify_task_mode


def test_task_mode_classification_failing_test() -> None:
    assert classify_task_mode("fix failing test in parser") == TaskMode.FIX_FAILING_TEST


def test_task_mode_classification_lint_type() -> None:
    assert classify_task_mode("fix lint and type error") == TaskMode.FIX_LINT_OR_TYPE


def test_task_mode_classification_refactor() -> None:
    assert classify_task_mode("narrow refactor around parser") == TaskMode.NARROW_REFACTOR


def test_task_mode_classification_docs() -> None:
    assert classify_task_mode("update docs in README") == TaskMode.DOCS_UPDATE_SAFE


def test_task_mode_classification_inspect_plan() -> None:
    assert classify_task_mode("inspect repo and plan no edits") == TaskMode.INSPECT_AND_PLAN
