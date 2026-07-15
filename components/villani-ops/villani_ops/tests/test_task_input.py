from __future__ import annotations

from pathlib import Path

import pytest

from villani_ops.cli.task_input import (
    EXACTLY_ONE_TASK_SOURCE,
    TaskInputError,
    resolve_task_input,
)


def _write_bytes(path: Path, content: str) -> Path:
    path.write_bytes(content.encode("utf-8"))
    return path


def test_resolves_single_line_positional_task_verbatim() -> None:
    assert resolve_task_input("Fix the bug", None) == "Fix the bug"


def test_resolves_multiline_positional_task_verbatim() -> None:
    task = "  First paragraph.\r\n\r\nSecond paragraph.  \n"
    assert resolve_task_input(task, None) == task


@pytest.mark.parametrize("task", ["", " \t\r\n"], ids=["empty", "whitespace"])
def test_rejects_empty_positional_task(task: str) -> None:
    with pytest.raises(TaskInputError) as raised:
        resolve_task_input(task, None)
    assert str(raised.value) == "Task instruction is empty."


def test_reads_standard_utf8_task_file(tmp_path: Path) -> None:
    task = "Fix the serializer."
    path = _write_bytes(tmp_path / "task.md", task)
    assert resolve_task_input(None, path) == task


def test_resolves_relative_task_file_without_changing_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = "Relative task file."
    _write_bytes(tmp_path / "task.md", task)
    monkeypatch.chdir(tmp_path)
    working_directory = Path.cwd()
    assert resolve_task_input(None, Path("task.md")) == task
    assert Path.cwd() == working_directory


def test_removes_utf8_bom_from_task_file(tmp_path: Path) -> None:
    task = "Fix Unicode: café 日本語"
    path = tmp_path / "task.md"
    path.write_bytes(b"\xef\xbb\xbf" + task.encode("utf-8"))
    assert resolve_task_input(None, path) == task


@pytest.mark.parametrize(
    "content",
    [
        "# Heading\n\n- Preserve `code`\n- Keep **Markdown**\n",
        "First paragraph.\n\n\nThird paragraph.\n",
        "    indented line\n\tindented with a tab\n",
        "Keep \"double quotes\" and 'single quotes'.\n",
        "Keep `inline code` and ```fenced markers```.\n",
        "Unicode: café, naïve, 日本語, 🚀\n",
        "First line\r\n\r\nSecond line\r\n",
        "First line\n\nSecond line\n",
        "Literal $VILLANI_TASK_VALUE must not expand.\n",
        "Literal $(Write-Output 'not executed') | Out-Null\n",
    ],
    ids=[
        "markdown",
        "internal-blank-lines",
        "leading-indentation",
        "quotes",
        "backticks",
        "unicode",
        "windows-line-endings",
        "posix-line-endings",
        "environment-variable-text",
        "shell-command-text",
    ],
)
def test_preserves_task_file_content_exactly(tmp_path: Path, content: str) -> None:
    path = _write_bytes(tmp_path / "task.md", content)
    assert resolve_task_input(None, path) == content


def test_rejects_both_task_sources(tmp_path: Path) -> None:
    path = _write_bytes(tmp_path / "task.md", "File task")
    with pytest.raises(TaskInputError) as raised:
        resolve_task_input("Positional task", path)
    assert str(raised.value) == EXACTLY_ONE_TASK_SOURCE


def test_rejects_missing_task_source() -> None:
    with pytest.raises(TaskInputError) as raised:
        resolve_task_input(None, None)
    assert str(raised.value) == EXACTLY_ONE_TASK_SOURCE


def test_rejects_missing_task_file_and_names_supplied_path(tmp_path: Path) -> None:
    path = tmp_path / "missing-task.md"
    with pytest.raises(TaskInputError) as raised:
        resolve_task_input(None, path)
    assert str(raised.value) == f"Task file does not exist: {path}"


def test_rejects_directory_as_task_file(tmp_path: Path) -> None:
    with pytest.raises(TaskInputError) as raised:
        resolve_task_input(None, tmp_path)
    assert str(raised.value) == f"Task file is not a regular file: {tmp_path}"


def test_reports_unreadable_task_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_bytes(tmp_path / "task.md", "Unreadable")
    original_open = Path.open

    def deny_task_file(candidate: Path, *args: object, **kwargs: object):
        if candidate == path.resolve():
            raise PermissionError("simulated permission failure")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_task_file)
    with pytest.raises(TaskInputError) as raised:
        resolve_task_input(None, path)
    assert str(raised.value) == f"Task file could not be read: {path}"


def test_rejects_invalid_utf8_without_exposing_bytes(tmp_path: Path) -> None:
    path = tmp_path / "task.md"
    path.write_bytes(b"valid prefix\xff\xfe")
    with pytest.raises(TaskInputError) as raised:
        resolve_task_input(None, path)
    message = str(raised.value)
    assert message == f"Task file must contain valid UTF-8: {path}"
    assert "\\xff" not in message
    assert "\\xfe" not in message


@pytest.mark.parametrize("content", [b"", b" \t\r\n"], ids=["empty", "whitespace"])
def test_rejects_empty_task_file(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "task.md"
    path.write_bytes(content)
    with pytest.raises(TaskInputError) as raised:
        resolve_task_input(None, path)
    assert str(raised.value) == "Task instruction is empty."
