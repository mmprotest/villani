from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


HELP = """Claude Code
Usage: claude [options] [prompt]
  -p, --print
  --model <model>
  --output-format <text|json|stream-json>
  --verbose
  --json-schema <schema>
  --max-turns <count>
  --no-session-persistence
  --permission-mode <default|acceptEdits|plan>
  --tools <tools>
  --allowedTools <tools>
  --no-chrome
  --bare
  --settings <file>
  --setting-sources <sources>
  --strict-mcp-config
  --mcp-config <file>
  --disable-slash-commands
When --print is used without a prompt argument, the prompt is read from stdin.
"""


def option(arguments: list[str], name: str) -> str:
    index = arguments.index(name)
    return arguments[index + 1]


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def final_document() -> dict[str, object]:
    return {
        "schema_version": "villani.claude_coder_result.v1",
        "status": "completed",
        "summary": "The fake Claude Code completed its isolated coding loop.",
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


def coding(arguments: list[str]) -> int:  # noqa: C901
    prompt = sys.stdin.read()
    worktree = Path.cwd().resolve()
    scenario = os.environ.get("VILLANI_FAKE_CLAUDE_SCENARIO", "success")
    if "--no-session-persistence" not in arguments:
        print("session persistence was not disabled", file=sys.stderr)
        return 30
    if any(item in arguments for item in ("--resume", "--continue", "-c")):
        print("resume or continue is forbidden", file=sys.stderr)
        return 31
    expected_tools = "Bash,Read,Edit,Write,Glob,Grep"
    if option(arguments, "--tools") != expected_tools:
        print("tool restriction mismatch", file=sys.stderr)
        return 32
    if option(arguments, "--allowedTools") != expected_tools:
        print("allowed tool restriction mismatch", file=sys.stderr)
        return 33
    if scenario == "large_prompt":
        if len(prompt.encode("utf-8")) < 100_000:
            print("large prompt was not delivered through stdin", file=sys.stderr)
            return 34
        if any("LARGE_PROMPT_SENTINEL" in item for item in arguments):
            print("large prompt leaked into argv", file=sys.stderr)
            return 35
    if scenario == "model_unavailable":
        print("configured model is unavailable or not found", file=sys.stderr)
        return 2
    if scenario == "permission_denial":
        print("permission denied: acceptEdits denied", file=sys.stderr)
        return 3
    if scenario == "tool_denial":
        print("tool denied: Bash tool is not allowed", file=sys.stderr)
        return 4
    if scenario == "startup_failure":
        print("MCP startup failure: hook startup failed", file=sys.stderr)
        return 5
    if scenario == "provider_auth_failure":
        print("provider authentication failed: login required", file=sys.stderr)
        return 6
    if scenario == "rate_limit":
        print("provider rate limit: overloaded", file=sys.stderr)
        return 7

    session_id = f"fake-claude-session-{os.getpid()}"
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "model": option(arguments, "--model"),
            "tools": expected_tools.split(","),
            "mcp_servers": [],
            "plugins": [],
            "agents": [],
            "permissionMode": "acceptEdits",
            "claude_code_version": "2.1.138",
        }
    )
    emit(
        {
            "type": "assistant",
            "uuid": "assistant-command",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-bash-1",
                        "name": "Bash",
                        "input": {"command": "fixture validation"},
                    }
                ]
            },
        }
    )

    target = worktree / "target.txt"
    if scenario not in {"no_patch", "malformed"}:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(current + "claude change\n", encoding="utf-8")
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
        emit({"type": "future_claude_event", "provider_extension": {"answer": 42}})
    if scenario == "malformed":
        print("{not-json", flush=True)
        time.sleep(2)
        return 8
    if scenario == "child_cancel":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], cwd=worktree
        )
        temporary = worktree / "child.pid.tmp"
        temporary.write_text(str(child.pid), encoding="ascii")
        temporary.replace(worktree / "child.pid")
        time.sleep(60)
        return 0

    echo_names = [
        name
        for name in os.environ.get("VILLANI_FAKE_ECHO_ENV_NAMES", "").split(",")
        if name
    ]
    for name in echo_names:
        print(f"diagnostic {name}={os.environ.get(name, '')}", file=sys.stderr)
    content: list[dict[str, object]] = [
        {
            "type": "thinking",
            "thinking": "fixture hidden chain of thought must never persist",
            "signature": "fixture-thinking-signature",
        }
    ]
    content.extend(
        {
            "type": "text",
            "text": f"{name}={os.environ.get(name, '')}",
        }
        for name in echo_names
    )
    content.append(
        {
            "type": "tool_use",
            "id": "tool-edit-1",
            "name": "Edit",
            "input": {"file_path": "target.txt"},
        }
    )
    emit(
        {
            "type": "assistant",
            "uuid": "assistant-edit",
            "message": {"content": content},
        }
    )
    emit(
        {
            "type": "user",
            "uuid": "user-results",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-bash-1",
                        "is_error": False,
                        "content": "fixture passed",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-edit-1",
                        "is_error": False,
                        "content": "target.txt updated",
                    },
                ]
            },
        }
    )
    emit(
        {
            "type": "assistant",
            "uuid": "assistant-message",
            "message": {"content": [{"type": "text", "text": "Coding complete."}]},
        }
    )
    if scenario == "partial_crash":
        print("Claude Code process crashed after partial output", file=sys.stderr)
        return 9
    if scenario == "timeout_partial":
        time.sleep(60)
        return 0
    if scenario == "missing_final":
        return 0
    structured: object = final_document()
    if scenario == "invalid_schema":
        structured = {"status": "completed", "summary": 3}
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 20,
            "duration_api_ms": 10,
            "num_turns": 1,
            "result": "Coding complete.",
            "structured_output": structured,
            "session_id": session_id,
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": len(prompt.encode("utf-8")),
                "output_tokens": 23,
            },
            "modelUsage": {
                option(arguments, "--model"): {
                    "inputTokens": len(prompt.encode("utf-8")),
                    "outputTokens": 23,
                    "costUSD": 0.0123,
                }
            },
        }
    )
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print("2.1.138 (Claude Code fixture)")
        return 0
    if arguments == ["--help"]:
        if os.environ.get("VILLANI_FAKE_CLAUDE_UNSUPPORTED") == "1":
            print("Claude Code\n  -p, --print\n  --model <model>\n")
        else:
            print(HELP)
        return 0
    if arguments == ["auth", "status"]:
        if os.environ.get("VILLANI_FAKE_CLAUDE_AUTH") == "missing":
            print(json.dumps({"loggedIn": False, "authMethod": "none"}))
            return 1
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        return 0
    if arguments == ["doctor"]:
        if os.environ.get("VILLANI_FAKE_CLAUDE_DOCTOR") == "failed":
            print("hook startup diagnostic failed", file=sys.stderr)
            return 1
        print("Claude Code doctor: healthy")
        return 0
    if "-p" in arguments or "--print" in arguments:
        return coding(arguments)
    print("unsupported fake Claude Code command", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
