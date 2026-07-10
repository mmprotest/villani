from __future__ import annotations

import json
from pathlib import Path

from villani_code.evidence import parse_command_evidence
from villani_code.execution import ExecutionBudget
from villani_code.state import Runner


class BashThenDoneClient:
    def __init__(self, command: str) -> None:
        self.command = command
        self.calls = 0

    def create_message(self, _payload, stream=False):
        self.calls += 1
        if self.calls == 1:
            return {
                "id": "1",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": self.command, "cwd": ".", "timeout_sec": 10},
                    }
                ],
            }
        return {
            "id": "2",
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
        }


class DummyClient:
    def create_message(self, _payload, stream=False):
        return {"content": [{"type": "text", "text": "ok"}]}


def test_parse_command_evidence_json_bash_output() -> None:
    content = json.dumps(
        {
            "command": 'python -c "import x"',
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }
    )
    parsed = parse_command_evidence(content)
    assert parsed == [{"command": 'python -c "import x"', "exit": 0}]


def test_parse_command_evidence_legacy_plain_text() -> None:
    parsed = parse_command_evidence("command: pytest -q\nexit: 0")
    assert parsed == [{"command": "pytest -q", "exit": 0}]


def test_parse_command_evidence_malformed_content_is_safe() -> None:
    assert parse_command_evidence("broken {json") == []
    assert parse_command_evidence("imports are working") == []


def test_runner_execution_captures_json_bash_validation_artifact(tmp_path: Path) -> None:
    runner = Runner(client=BashThenDoneClient("python -c \"print('ok')\""), repo=tmp_path, model="m", stream=False)

    result = runner.run("run validation", execution_budget=ExecutionBudget(max_turns=3, max_tool_calls=3, max_seconds=10, max_no_edit_turns=3, max_reconsecutive_recon_turns=3))

    assert 'python -c "print(\'ok\')" (exit=0)' in result["execution"]["validation_artifacts"]
