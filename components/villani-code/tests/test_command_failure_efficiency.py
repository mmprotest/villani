from __future__ import annotations

import json
from pathlib import Path

from villani_code.command_failures import (
    CommandFailureLedger,
    classify_command_failure,
)
from villani_code.state_tooling import execute_tool_with_lifecycle


class _Runner:
    def __init__(self, repo: Path):
        self.repo = repo
        self.unsafe = False
        self._debug_recorder = None
        self._task_memory = None
        self._tool_result_ledger = None
        self._context_ledger = None
        self._progress_tracker = None
        self._command_failure_ledger = CommandFailureLedger(repo)
        self.events: list[dict[str, object]] = []

    def event_callback(self, event: dict[str, object]) -> None:
        self.events.append(event)

    def _build_tool_result_event_payload(
        self,
        tool_name: str,
        tool_use_id: str,
        result: dict[str, object],
    ) -> dict[str, object]:
        return {**result, "tool_name": tool_name, "tool_use_id": tool_use_id}


def test_first_failure_is_full_and_identical_retry_is_compact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def failed_execute(name, raw_input, repo, **kwargs):
        calls.append(str(raw_input["command"]))
        return {
            "content": json.dumps(
                {
                    "command": raw_input["command"],
                    "exit_code": 7,
                    "stdout": "",
                    "stderr": "stable failure details",
                }
            ),
            "is_error": False,
        }

    monkeypatch.setattr(
        "villani_code.state_tooling.execute_tool",
        failed_execute,
    )
    runner = _Runner(tmp_path)

    first = execute_tool_with_lifecycle(
        runner=runner,
        tool_name="Bash",
        tool_input={"command": "failing command", "cwd": "."},
        tool_use_id="bash-1",
        turn_index=1,
    )
    repeated = execute_tool_with_lifecycle(
        runner=runner,
        tool_name="Bash",
        tool_input={"command": "failing command", "cwd": "."},
        tool_use_id="bash-2",
        turn_index=2,
    )
    changed = execute_tool_with_lifecycle(
        runner=runner,
        tool_name="Bash",
        tool_input={"command": "failing command --changed", "cwd": "."},
        tool_use_id="bash-3",
        turn_index=3,
    )

    assert "stable failure details" in str(first["content"])
    repeated_payload = json.loads(str(repeated["content"]))
    assert repeated_payload["repeated_failure"] is True
    assert "stable failure details" not in str(repeated["content"])
    assert "stable failure details" in str(changed["content"])
    assert calls == ["failing command", "failing command --changed"]
    telemetry = runner._command_failure_ledger.telemetry()
    assert telemetry["unique_command_failures"] == 2
    assert telemetry["repeated_command_failures"] == 1
    assert telemetry["commands_retried_without_state_change"] == 1


def test_language_neutral_failure_classifier_distinguishes_classes() -> None:
    assert (
        classify_command_failure(
            stderr="command not found",
            exit_code=127,
        )
        == "executable_not_found"
    )
    assert (
        classify_command_failure(
            stderr="permission denied",
            exit_code=1,
        )
        == "permission_denied"
    )
    assert (
        classify_command_failure(
            stderr="command exceeded timeout",
            exit_code=None,
            timed_out=True,
        )
        == "timeout"
    )
    assert (
        classify_command_failure(
            stderr="assertion failed",
            exit_code=1,
        )
        == "test_failure"
    )
    assert (
        classify_command_failure(
            stderr="blocked",
            exit_code=None,
            policy_denied=True,
        )
        == "policy_denied"
    )


def test_repeated_successful_command_is_measured_but_not_suppressed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def successful_execute(name, raw_input, repo, **kwargs):
        calls.append(str(raw_input["command"]))
        return {
            "content": json.dumps(
                {
                    "command": raw_input["command"],
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                }
            ),
            "is_error": False,
        }

    monkeypatch.setattr(
        "villani_code.state_tooling.execute_tool",
        successful_execute,
    )
    runner = _Runner(tmp_path)

    for tool_use_id in ("bash-1", "bash-2"):
        execute_tool_with_lifecycle(
            runner=runner,
            tool_name="Bash",
            tool_input={"command": "safe observation", "cwd": "."},
            tool_use_id=tool_use_id,
            turn_index=1,
        )

    assert calls == ["safe observation", "safe observation"]
    telemetry = runner._command_failure_ledger.telemetry()
    assert telemetry["repeated_commands"] == 1
    assert telemetry["repeated_command_failures"] == 0
