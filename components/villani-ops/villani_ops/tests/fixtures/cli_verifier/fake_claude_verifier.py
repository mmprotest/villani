from __future__ import annotations

import json
import os
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
    return arguments[arguments.index(name) + 1]


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def requirement_ids(workspace: Path) -> list[str]:
    value = json.loads(
        (workspace / "input" / "success-criteria.json").read_text(encoding="utf-8")
    )
    return [str(item["requirement_id"]) for item in value["requirements"]]


def result_document(workspace: Path, scenario: str) -> object:
    identifiers = requirement_ids(workspace)
    if scenario == "wrong_decision_type":
        return {
            "decision": True,
            "reason": "Wrong decision type fixture.",
            "requirements_proved": identifiers,
            "requirements_not_proved": [],
            "blocking_issues": [],
        }
    if scenario == "unknown_requirement":
        return {
            "decision": 1,
            "reason": "Unknown requirement fixture.",
            "requirements_proved": [*identifiers, "req-unknown0000000"],
            "requirements_not_proved": [],
            "blocking_issues": [],
        }
    if scenario == "missing_field":
        return {
            "decision": 1,
            "reason": "Missing required field fixture.",
            "requirements_proved": identifiers,
            "requirements_not_proved": [],
        }
    if scenario == "extra_field":
        return {
            "decision": 1,
            "reason": "Unexpected field fixture.",
            "requirements_proved": identifiers,
            "requirements_not_proved": [],
            "blocking_issues": [],
            "provider": "forbidden-extra-field",
        }
    if scenario in {"reject", "insufficient"}:
        return {
            "decision": 0,
            "reason": "The supplied evidence is insufficient or contradictory.",
            "requirements_proved": [],
            "requirements_not_proved": identifiers,
            "blocking_issues": (
                []
                if scenario == "insufficient"
                else [
                    {
                        "code": "requirement-failed",
                        "summary": "The candidate diff contradicts the requirement.",
                        "evidence_reference": "input/candidate.patch",
                    }
                ]
            ),
        }
    return {
        "decision": 1,
        "reason": "The supplied diff and authoritative validation prove the change acceptable.",
        "requirements_proved": identifiers,
        "requirements_not_proved": [],
        "blocking_issues": [],
    }


def verify(arguments: list[str], fixture_scenario: str | None = None) -> int:
    prompt = sys.stdin.read()
    workspace = Path.cwd().resolve()
    scenario = fixture_scenario or os.environ.get(
        "VILLANI_FAKE_CLAUDE_VERIFIER_SCENARIO", "success"
    )
    if "--no-session-persistence" not in arguments:
        print("session persistence was not disabled", file=sys.stderr)
        return 30
    if option(arguments, "--permission-mode") != "plan":
        print("permission denied: verifier is not in plan mode", file=sys.stderr)
        return 31
    expected_tools = "Read,Glob,Grep"
    if option(arguments, "--tools") != expected_tools:
        print("tool restriction mismatch", file=sys.stderr)
        return 32
    if option(arguments, "--allowedTools") != expected_tools:
        print("allowed tool restriction mismatch", file=sys.stderr)
        return 33
    forbidden = {"Bash", "Edit", "Write"}
    if forbidden.intersection(option(arguments, "--tools").split(",")):
        print("write-capable tool exposed", file=sys.stderr)
        return 34
    required_flags = {
        "--bare",
        "--setting-sources=",
        "--strict-mcp-config",
        "--disable-slash-commands",
    }
    if not required_flags.issubset(arguments):
        print("ambient feature controls missing", file=sys.stderr)
        return 35
    settings = json.loads(Path(option(arguments, "--settings")).read_text())
    mcp = json.loads(Path(option(arguments, "--mcp-config")).read_text())
    if settings.get("autoMemoryEnabled") is not False or mcp != {"mcpServers": {}}:
        print("ambient settings were not disabled", file=sys.stderr)
        return 36
    if "independent semantic verifier" not in prompt:
        print("controlled verifier prompt missing", file=sys.stderr)
        return 37
    if scenario == "permission_failure":
        print("permission denied: Read tool denied", file=sys.stderr)
        return 3
    if scenario == "process_crash":
        print("Claude verifier process crashed", file=sys.stderr)
        return 9
    if scenario in {"timeout", "cancellation"}:
        time.sleep(60)
        return 0

    session = f"fake-claude-verifier-{os.getpid()}"
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session,
            "tools": expected_tools.split(","),
            "mcp_servers": [],
            "plugins": [],
            "agents": [],
            "permissionMode": "plan",
            "claude_code_version": "2.1.138",
        }
    )
    if scenario == "attempt_edit":
        candidates = sorted((workspace / "input" / "original-repository").rglob("*"))
        target = next((item for item in candidates if item.is_file()), None)
        blocked = False
        if target is not None:
            if target.stat().st_mode & 0o222 == 0:
                blocked = True
            else:
                try:
                    target.write_text("forbidden verifier edit\n", encoding="utf-8")
                except OSError:
                    blocked = True
        scenario = "success" if blocked else "reject"
    if scenario == "malformed":
        print("{not-json", flush=True)
        return 8
    if scenario == "missing_final":
        return 0
    structured = result_document(workspace, scenario)
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Verification complete.",
            "structured_output": structured,
            "session_id": session,
            "usage": {
                "input_tokens": len(prompt.encode("utf-8")),
                "output_tokens": 21,
            },
        }
    )
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    fixture_scenario = None
    fixture_auth_missing = False
    fixture_unsupported = False
    while arguments and arguments[0].startswith("--fixture-"):
        if len(arguments) >= 2 and arguments[0] == "--fixture-scenario":
            fixture_scenario = arguments[1]
            arguments = arguments[2:]
        elif arguments[0] == "--fixture-auth-missing":
            fixture_auth_missing = True
            arguments = arguments[1:]
        elif arguments[0] == "--fixture-unsupported":
            fixture_unsupported = True
            arguments = arguments[1:]
        else:
            return 65
    if arguments == ["--version"]:
        print("2.1.138 (Claude Code verifier fixture)")
        return 0
    if arguments == ["--help"]:
        if (
            fixture_unsupported
            or os.environ.get("VILLANI_FAKE_CLAUDE_VERIFIER_UNSUPPORTED") == "1"
        ):
            print("Claude Code\n  -p, --print\n  --model <model>")
        else:
            print(HELP)
        return 0
    if arguments == ["auth", "status"]:
        if (
            fixture_auth_missing
            or os.environ.get("VILLANI_FAKE_CLAUDE_VERIFIER_AUTH") == "missing"
        ):
            print(json.dumps({"loggedIn": False, "authMethod": "none"}))
            return 1
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        return 0
    if arguments == ["doctor"]:
        print("Claude Code doctor: healthy")
        return 0
    if "-p" in arguments or "--print" in arguments:
        return verify(arguments, fixture_scenario)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
