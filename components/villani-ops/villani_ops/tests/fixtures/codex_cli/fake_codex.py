from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


HELP = """Codex exec
Usage: codex exec [OPTIONS] [PROMPT]
  --ephemeral
  --json
  --model <MODEL>
  --sandbox <read-only|workspace-write>
  --cd <DIR>
  --output-schema <FILE>
  --output-last-message <FILE>
  --ask-for-approval <untrusted|on-request|never>
  --config <key=value>
  --strict-config
  --ignore-user-config
  --ignore-rules
  -  Read prompt from stdin
"""


def option(arguments: list[str], name: str) -> str:
    index = arguments.index(name)
    return arguments[index + 1]


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def final_document() -> dict[str, object]:
    return {
        "schema_version": "villani.codex_coder_result.v1",
        "status": "completed",
        "summary": "The fake Codex completed its isolated coding loop.",
        "tests_run": [
            {
                "command": "python -m pytest -q",
                "reported_exit_status": 0,
                "reported_result": "fixture validation passed",
            }
        ],
        "known_limitations": [],
        "files_the_agent_believes_changed": ["target.txt"],
    }


def write_final(arguments: list[str], value: object | None = None) -> None:
    path = Path(option(arguments, "--output-last-message"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(final_document() if value is None else value, ensure_ascii=False),
        encoding="utf-8",
    )


def coding(arguments: list[str]) -> int:
    prompt = sys.stdin.read()
    worktree = Path(option(arguments, "--cd")).resolve()
    if Path.cwd().resolve() != worktree:
        print("permission failure: working directory mismatch", file=sys.stderr)
        return 12
    scenario = os.environ.get("VILLANI_FAKE_CODEX_SCENARIO", "success")
    if scenario == "model_unavailable":
        print("model is unavailable or not found", file=sys.stderr)
        return 2
    if scenario == "permission_failure":
        print("sandbox permission denied for workspace-write", file=sys.stderr)
        return 3
    if scenario == "provider_auth_failure":
        print("provider authentication failed: login required", file=sys.stderr)
        return 4
    if scenario == "rate_limit":
        print("provider rate limit: too many requests", file=sys.stderr)
        return 5

    thread_id = f"fake-thread-{os.getpid()}"
    emit({"type": "thread.started", "thread_id": thread_id})
    emit({"type": "turn.started"})
    emit(
        {
            "type": "item.started",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "fixture validation",
                "status": "in_progress",
            },
        }
    )

    target = worktree / "target.txt"
    if scenario not in {"no_patch", "malformed"}:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(current + "codex change\n", encoding="utf-8")
    if scenario == "untracked":
        (worktree / "new file ü.txt").write_text("new content\n", encoding="utf-8")
    if scenario == "rename_delete":
        (worktree / "rename_me.txt").rename(worktree / "renamed ü.txt")
        (worktree / "delete_me.txt").unlink()
    if scenario == "path_violation":
        forbidden = worktree / ".villani" / "forbidden.txt"
        forbidden.parent.mkdir(parents=True, exist_ok=True)
        forbidden.write_text("must not enter candidate patch\n", encoding="utf-8")
    if scenario == "unknown_event":
        emit({"type": "future.event", "new_field": {"safe": True}})
    if scenario == "malformed":
        print("{not-json", flush=True)
        time.sleep(2)
        return 4
    if scenario == "child_cancel":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=worktree,
        )
        temporary_pid_path = worktree / "child.pid.tmp"
        temporary_pid_path.write_text(str(child.pid), encoding="ascii")
        temporary_pid_path.replace(worktree / "child.pid")
        time.sleep(60)
        return 0

    echo_names = [
        name
        for name in os.environ.get("VILLANI_FAKE_ECHO_ENV_NAMES", "").split(",")
        if name
    ]
    for name in echo_names:
        emit(
            {
                "type": "item.completed",
                "item": {
                    "id": f"env-{name}",
                    "type": "agent_message",
                    "text": f"{name}={os.environ.get(name, '')}",
                },
            }
        )

    emit(
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "fixture validation",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "fixture passed",
            },
        }
    )
    emit(
        {
            "type": "item.completed",
            "item": {
                "id": "file-1",
                "type": "file_change",
                "changes": [{"path": "target.txt", "kind": "update"}],
            },
        }
    )
    emit(
        {
            "type": "item.completed",
            "item": {
                "id": "message-1",
                "type": "agent_message",
                "text": "Coding complete.",
            },
        }
    )
    if scenario == "partial_crash":
        print("provider process crashed after partial output", file=sys.stderr)
        return 7
    if scenario == "timeout_partial":
        time.sleep(60)
        return 0
    if scenario == "invalid_final":
        write_final(arguments, {"status": "completed", "summary": 3})
    elif scenario != "missing_final":
        write_final(arguments)
    emit(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": len(prompt.encode("utf-8")),
                "cached_input_tokens": 0,
                "output_tokens": 17,
                "reasoning_output_tokens": 0,
            },
        }
    )
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print("codex-cli 9.9.9-fixture")
        return 0
    if arguments == ["exec", "--help"]:
        if os.environ.get("VILLANI_FAKE_CODEX_UNSUPPORTED") == "1":
            print("Codex exec\n  --model <MODEL>\n")
        else:
            print(HELP)
        return 0
    if arguments == ["login", "status"]:
        if os.environ.get("VILLANI_FAKE_CODEX_AUTH") == "missing":
            print("Not logged in", file=sys.stderr)
            return 1
        print("Logged in using ChatGPT")
        return 0
    if arguments and arguments[0] == "exec":
        return coding(arguments)
    print("unsupported fake Codex command", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
