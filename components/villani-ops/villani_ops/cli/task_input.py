"""Resolve the single task input accepted by the public CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Final


EXACTLY_ONE_TASK_SOURCE: Final = (
    "Provide exactly one task source: positional TASK or --task-file PATH."
)


class TaskInputError(ValueError):
    """A user-correctable task-input error."""


def resolve_task_input(
    positional_task: str | None,
    task_file: Path | None,
) -> str:
    """Return one verbatim task string from exactly one supported source."""

    if (positional_task is None) == (task_file is None):
        raise TaskInputError(EXACTLY_ONE_TASK_SOURCE)

    if positional_task is not None:
        if not positional_task.strip():
            raise TaskInputError("Task instruction is empty.")
        return positional_task

    assert task_file is not None
    supplied_path = str(task_file)
    try:
        resolved_path = task_file.resolve()
        if not resolved_path.exists():
            raise TaskInputError(f"Task file does not exist: {supplied_path}")
        if not resolved_path.is_file():
            raise TaskInputError(f"Task file is not a regular file: {supplied_path}")
    except TaskInputError:
        raise
    except (OSError, RuntimeError) as error:
        raise TaskInputError(f"Task file could not be read: {supplied_path}") from error

    try:
        # newline="" prevents Python's universal-newline conversion so the
        # controller receives the exact decoded content, including CRLFs.
        with resolved_path.open("r", encoding="utf-8-sig", newline="") as task_stream:
            task = task_stream.read()
    except UnicodeDecodeError as error:
        raise TaskInputError(
            f"Task file must contain valid UTF-8: {supplied_path}"
        ) from error
    except OSError as error:
        raise TaskInputError(f"Task file could not be read: {supplied_path}") from error

    if not task.strip():
        raise TaskInputError("Task instruction is empty.")
    return task
