from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fake_codex_roles import classifier_result, selector_result


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
    return arguments[arguments.index(name) + 1]


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def run_role(arguments: list[str], scenario: str) -> int:
    prompt = sys.stdin.read()
    workspace = Path.cwd().resolve()
    if "--no-session-persistence" not in arguments:
        print("session persistence enabled", file=sys.stderr)
        return 20
    if option(arguments, "--permission-mode") != "plan":
        print("permission denied: plan mode missing", file=sys.stderr)
        return 21
    if option(arguments, "--tools") != "" or option(arguments, "--allowedTools") != "":
        print("tool restriction mismatch", file=sys.stderr)
        return 22
    required = {
        "--bare",
        "--setting-sources=",
        "--strict-mcp-config",
        "--disable-slash-commands",
    }
    if not required.issubset(arguments):
        print("ambient controls missing", file=sys.stderr)
        return 23
    settings = json.loads(Path(option(arguments, "--settings")).read_text())
    mcp = json.loads(Path(option(arguments, "--mcp-config")).read_text())
    if settings.get("autoMemoryEnabled") is not False or mcp != {"mcpServers": {}}:
        print("ambient settings enabled", file=sys.stderr)
        return 24
    if scenario == "permission_failure":
        print("permission denied: tool denied", file=sys.stderr)
        return 3
    if scenario == "timeout":
        time.sleep(60)
        return 0
    if scenario == "process_crash":
        print("role process crashed", file=sys.stderr)
        return 7
    role = "classification" if (workspace / "input" / "repository-metadata.json").is_file() else "selection"
    expected_prompt = "cli_classifier_prompt" if role == "classification" else "cli_selector_prompt"
    if expected_prompt not in prompt:
        print("controlled role prompt missing", file=sys.stderr)
        return 25
    session = f"fake-claude-{role}-{workspace.name}"
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session,
            "tools": [],
            "mcp_servers": [],
            "plugins": [],
            "agents": [],
            "permissionMode": "plan",
            "claude_code_version": "2.1.138",
        }
    )
    if scenario == "malformed":
        print("{not-json", flush=True)
        return 8
    if scenario == "missing_final":
        return 0
    result = (
        classifier_result(workspace, scenario)
        if role == "classification"
        else selector_result(workspace, scenario)
    )
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": f"{role} complete",
            "structured_output": result,
            "session_id": session,
            "usage": {"input_tokens": len(prompt), "output_tokens": 19},
        }
    )
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    scenario = "success"
    auth_missing = False
    unsupported = False
    while arguments and arguments[0].startswith("--fixture-"):
        if arguments[0] == "--fixture-scenario" and len(arguments) >= 2:
            scenario = arguments[1]
            arguments = arguments[2:]
        elif arguments[0] == "--fixture-auth-missing":
            auth_missing = True
            arguments = arguments[1:]
        elif arguments[0] == "--fixture-unsupported":
            unsupported = True
            arguments = arguments[1:]
        else:
            return 65
    if arguments == ["--version"]:
        print("2.1.138 (Claude Code role fixture)")
        return 0
    if arguments == ["--help"]:
        print("Claude Code\n  -p, --print\n  --model <model>" if unsupported else HELP)
        return 0
    if arguments == ["auth", "status"]:
        if auth_missing:
            print(json.dumps({"loggedIn": False, "authMethod": "none"}))
            return 1
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        return 0
    if arguments == ["doctor"]:
        print("Claude Code doctor: healthy")
        return 0
    if "-p" in arguments:
        return run_role(arguments, scenario)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
