from __future__ import annotations

import json
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
    return arguments[arguments.index(name) + 1]


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def classifier_result(workspace: Path, scenario: str) -> object:
    metadata = json.loads(
        (workspace / "input" / "repository-metadata.json").read_text(encoding="utf-8")
    )
    inventory = metadata["tracked_files"]
    difficulty = scenario if scenario in {"easy", "medium", "hard"} else "medium"
    value: dict[str, object] = {
        "difficulty": difficulty,
        "risk": "low" if difficulty == "easy" else "high" if difficulty == "hard" else "medium",
        "category": "implementation",
        "required_capabilities": ["repository_editing", "test_execution"],
        "uncertainty": "low" if difficulty == "easy" else "high" if difficulty == "hard" else "medium",
        "confidence": 0.9 if difficulty == "easy" else 0.6,
        "estimated_attempts_needed": 1 if difficulty == "easy" else 3 if difficulty == "hard" else 2,
        "needs_tests": True,
        "likely_files": inventory[:1],
        "reasoning_summary": f"The fixture classified this pre-execution task as {difficulty}.",
    }
    if scenario == "wrong_type":
        value["needs_tests"] = "true"
    if scenario == "direct_provider_selection":
        value["provider"] = "forbidden"
    return value


def selector_result(workspace: Path, scenario: str) -> object:
    document = json.loads(
        (workspace / "input" / "candidates.json").read_text(encoding="utf-8")
    )
    packets = list(document["candidates"])
    identifiers = [str(item["candidate_id"]) for item in packets]
    preferred = next(
        (
            str(item["candidate_id"])
            for item in packets
            if "preferred.py" in item.get("changed_files", [])
        ),
        identifiers[-1],
    )
    ranking = [preferred, *(item for item in identifiers if item != preferred)]
    if scenario == "unknown_id":
        return {
            "selected_candidate_id": "candidate-unknown",
            "ranking": ["candidate-unknown", *identifiers],
            "reason": "Unknown ID fixture.",
        }
    if scenario == "duplicate_ranking":
        return {
            "selected_candidate_id": identifiers[0],
            "ranking": [identifiers[0], identifiers[0]],
            "reason": "Duplicate ranking fixture.",
        }
    if scenario == "missing_candidate":
        return {
            "selected_candidate_id": identifiers[0],
            "ranking": identifiers[:-1],
            "reason": "Missing candidate fixture.",
        }
    return {
        "selected_candidate_id": preferred,
        "ranking": ranking,
        "reason": "This tied candidate has the clearest supplied evidence and safer scope.",
    }


def run_role(arguments: list[str], scenario: str) -> int:
    prompt = sys.stdin.read()
    workspace = Path(option(arguments, "--cd")).resolve()
    if Path.cwd().resolve() != workspace:
        print("permission denied: role cwd mismatch", file=sys.stderr)
        return 11
    required = {
        "--ephemeral",
        "--json",
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
    }
    if not required.issubset(arguments) or "--sandbox" in arguments:
        print("permission denied: safe role flags missing", file=sys.stderr)
        return 12
    if option(arguments, "--ask-for-approval") != "never":
        print("permission denied: approvals enabled", file=sys.stderr)
        return 13
    overrides = {
        arguments[index + 1]
        for index, value in enumerate(arguments)
        if value == "--config" and index + 1 < len(arguments)
    }
    expected = {
        'default_permissions="villani_verifier_read_only"',
        'permissions.villani_verifier_read_only.filesystem={":minimal"="read",":workspace_roots"={"."="read"}}',
        "permissions.villani_verifier_read_only.network.enabled=false",
        'web_search="disabled"',
        "allow_login_shell=false",
    }
    if overrides != expected:
        print("permission denied: scoped read-only profile missing", file=sys.stderr)
        return 14
    if scenario == "permission_failure":
        print("sandbox permission denied", file=sys.stderr)
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
        return 15
    emit({"type": "thread.started", "thread_id": f"fake-codex-{role}-{Path.cwd().name}"})
    emit({"type": "turn.started"})
    output = Path(option(arguments, "--output-last-message"))
    output.parent.mkdir(parents=True, exist_ok=True)
    if scenario == "malformed":
        output.write_text("{not-json", encoding="utf-8")
    elif scenario != "missing_final":
        result = (
            classifier_result(workspace, scenario)
            if role == "classification"
            else selector_result(workspace, scenario)
        )
        output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    emit(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": len(prompt), "output_tokens": 17},
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
        print("codex-cli 9.9.9-role-fixture")
        return 0
    if arguments == ["exec", "--help"]:
        print("Codex exec\n  --model <MODEL>" if unsupported else HELP)
        return 0
    if arguments == ["login", "status"]:
        if auth_missing:
            print("Not logged in", file=sys.stderr)
            return 1
        print("Logged in using ChatGPT")
        return 0
    if arguments and arguments[0] == "exec":
        return run_role(arguments, scenario)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
