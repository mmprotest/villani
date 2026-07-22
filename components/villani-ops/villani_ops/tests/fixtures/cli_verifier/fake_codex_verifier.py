from __future__ import annotations

import json
import os
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


def requirement_ids(workspace: Path) -> list[str]:
    value = json.loads(
        (workspace / "input" / "success-criteria.json").read_text(encoding="utf-8")
    )
    return [str(item["requirement_id"]) for item in value["requirements"]]


def result_document(workspace: Path, scenario: str) -> object:
    identifiers = requirement_ids(workspace)
    if scenario == "wrong_decision_type":
        return {
            "decision": "1",
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
            "reason": (
                "The supplied evidence does not prove every requirement."
                if scenario == "insufficient"
                else "The candidate does not satisfy the supplied requirement."
            ),
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
    workspace = Path(option(arguments, "--cd")).resolve()
    scenario = fixture_scenario or os.environ.get(
        "VILLANI_FAKE_CODEX_VERIFIER_SCENARIO", "success"
    )
    if Path.cwd().resolve() != workspace:
        print("permission denied: verifier cwd mismatch", file=sys.stderr)
        return 12
    required = {
        "--ephemeral",
        "--json",
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
    }
    if not required.issubset(arguments) or "--sandbox" in arguments:
        print("permission denied: verifier policy flags missing", file=sys.stderr)
        return 13
    overrides = {
        arguments[index + 1]
        for index, value in enumerate(arguments)
        if value == "--config" and index + 1 < len(arguments)
    }
    expected_overrides = {
        'default_permissions="villani_verifier_read_only"',
        'permissions.villani_verifier_read_only.filesystem={":minimal"="read",":workspace_roots"={"."="read"}}',
        "permissions.villani_verifier_read_only.network.enabled=false",
        'web_search="disabled"',
        "allow_login_shell=false",
    }
    if overrides != expected_overrides:
        print("permission denied: scoped read-only profile missing", file=sys.stderr)
        return 17
    if option(arguments, "--ask-for-approval") != "never":
        print("permission denied: interactive approval enabled", file=sys.stderr)
        return 14
    if "independent semantic verifier" not in prompt:
        print("controlled verifier prompt missing", file=sys.stderr)
        return 15
    manifest = json.loads(
        (workspace / "input" / "manifest.json").read_text(encoding="utf-8")
    )
    if not all(value is False for value in manifest["blindness"].values()):
        print("input manifest blindness violation", file=sys.stderr)
        return 16
    if scenario == "permission_failure":
        print("sandbox permission denied", file=sys.stderr)
        return 3
    if scenario == "process_crash":
        print("verifier process crashed", file=sys.stderr)
        return 7
    if scenario in {"timeout", "cancellation"}:
        time.sleep(60)
        return 0

    session = f"fake-codex-verifier-{os.getpid()}"
    emit({"type": "thread.started", "thread_id": session})
    emit({"type": "turn.started"})
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
    output = Path(option(arguments, "--output-last-message"))
    output.parent.mkdir(parents=True, exist_ok=True)
    if scenario == "malformed":
        output.write_text("{not-json", encoding="utf-8")
    elif scenario == "duplicate_field":
        ids = requirement_ids(workspace)
        output.write_text(
            '{"decision":0,"decision":1,"reason":"duplicate",'
            f'"requirements_proved":{json.dumps(ids)},'
            '"requirements_not_proved":[],"blocking_issues":[]}',
            encoding="utf-8",
        )
    elif scenario != "missing_final":
        output.write_text(
            json.dumps(result_document(workspace, scenario), ensure_ascii=False),
            encoding="utf-8",
        )
    emit(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": len(prompt.encode("utf-8")),
                "output_tokens": 19,
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
        print("codex-cli 9.9.9-verifier-fixture")
        return 0
    if arguments == ["exec", "--help"]:
        if (
            fixture_unsupported
            or os.environ.get("VILLANI_FAKE_CODEX_VERIFIER_UNSUPPORTED") == "1"
        ):
            print("Codex exec\n  --model <MODEL>")
        else:
            print(HELP)
        return 0
    if arguments == ["login", "status"]:
        if (
            fixture_auth_missing
            or os.environ.get("VILLANI_FAKE_CODEX_VERIFIER_AUTH") == "missing"
        ):
            print("Not logged in", file=sys.stderr)
            return 1
        print("Logged in using ChatGPT")
        return 0
    if arguments and arguments[0] == "exec":
        return verify(arguments, fixture_scenario)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
