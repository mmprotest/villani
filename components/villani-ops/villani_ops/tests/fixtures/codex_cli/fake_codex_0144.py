"""Deterministic Codex 0.144.6 compatibility fixture.

The fixture deliberately exposes approval configuration only through global
options while ``exec --help`` omits ``--ask-for-approval``.  Control arguments
are consumed by this launcher before the remaining argv is interpreted as
Codex argv.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _control(arguments: list[str], name: str, default: str) -> str:
    if name not in arguments:
        return default
    index = arguments.index(name)
    if index + 1 >= len(arguments):
        raise SystemExit(f"{name} requires a value")
    value = arguments[index + 1]
    del arguments[index : index + 2]
    return value


def _emit(value: str, *, ansi_crlf: bool) -> None:
    normalized = value.strip().replace("\r\n", "\n").replace("\r", "\n")
    if ansi_crlf:
        payload = f"\x1b[32m{normalized}\x1b[0m\r\n"
    else:
        payload = f"{normalized}\n"
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.flush()


def _global_help(*, advertise_global_approval: bool) -> str:
    approval = (
        """
  -a, --ask-for-approval <POLICY>
          Configure when the model requires human approval
          [possible values: untrusted, on-failure, on-request, never]
"""
        if advertise_global_approval
        else ""
    )
    return f"""
Codex CLI

Usage: codex [OPTIONS] [PROMPT]
       codex [OPTIONS] <COMMAND> [ARGS]

Options:
  -c, --config <key=value>
          Override a configuration value
      --strict-config
          Reject unknown configuration keys
{approval}
  -s, --sandbox <SANDBOX_MODE>
          [possible values: read-only, workspace-write, danger-full-access]
  -V, --version
          Print version
  -h, --help
          Print help
"""


def _exec_help(*, omit: str) -> str:
    structured = (
        ""
        if omit == "structured"
        else """
      --json
      --output-schema <FILE>
      --output-last-message <FILE>
"""
    )
    if omit == "sandbox":
        sandbox = ""
    elif omit == "read-only":
        sandbox = """
  -s, --sandbox <SANDBOX_MODE>
          [possible values: workspace-write, danger-full-access]
"""
    else:
        sandbox = """
  -s, --sandbox <SANDBOX_MODE>
          [possible values: read-only, workspace-write, danger-full-access]
"""
    return f"""
Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]

Options:
  -c, --config <key=value>
  -m, --model <MODEL>
      --cd <DIR>
      --ephemeral
      --ignore-user-config
      --ignore-rules
      --skip-git-repo-check
{structured}
{sandbox}
  -h, --help
          Print help
"""


def _before_exec(arguments: list[str], option: str) -> bool:
    if "exec" not in arguments or option not in arguments:
        return False
    return arguments.index(option) < arguments.index("exec")


def main() -> int:
    arguments = sys.argv[1:]
    config_mode = _control(arguments, "--fixture-config", "accept")
    global_mode = _control(arguments, "--fixture-global", "accept")
    help_mode = _control(arguments, "--fixture-help", "plain")
    auth_mode = _control(arguments, "--fixture-auth", "ready")
    omit = _control(arguments, "--fixture-omit", "none")
    model_marker = _control(arguments, "--fixture-model-marker", "")
    ansi_crlf = help_mode == "ansi_crlf"

    if arguments == ["--version"]:
        _emit("codex-cli 0.144.6", ansi_crlf=ansi_crlf)
        return 0
    if arguments == ["login", "status"]:
        if auth_mode == "ready":
            _emit("Logged in using ChatGPT", ansi_crlf=ansi_crlf)
            return 0
        print("Not logged in", file=sys.stderr)
        return 1
    if arguments == ["--help"]:
        _emit(
            _global_help(advertise_global_approval=global_mode != "hidden"),
            ansi_crlf=ansi_crlf,
        )
        return 0

    is_help = "exec" in arguments and "--help" in arguments
    approval_overrides = [
        arguments[index + 1]
        for index, item in enumerate(arguments[:-1])
        if item in {"-c", "--config"}
        and arguments[index + 1].startswith("approval_policy")
    ]
    has_exact_config = 'approval_policy="never"' in approval_overrides
    has_unknown_approval_key = bool(approval_overrides) and not has_exact_config
    has_global_approval = (
        _before_exec(arguments, "--ask-for-approval")
        and arguments[arguments.index("--ask-for-approval") + 1] == "never"
    )

    if is_help and has_unknown_approval_key and "--strict-config" in arguments:
        print("unknown configuration key: approval policy", file=sys.stderr)
        return 2
    if is_help and has_exact_config and "--strict-config" in arguments:
        if config_mode == "timeout":
            time.sleep(2.0)
            return 3
        if config_mode == "reject":
            print("unknown configuration key: approval_policy", file=sys.stderr)
            return 2
        _emit(_exec_help(omit=omit), ansi_crlf=ansi_crlf)
        return 0
    if is_help and has_global_approval:
        if global_mode != "accept":
            print("unsupported global approval policy", file=sys.stderr)
            return 2
        _emit(_exec_help(omit=omit), ansi_crlf=ansi_crlf)
        return 0
    if arguments == ["exec", "--help"]:
        _emit(_exec_help(omit=omit), ansi_crlf=ansi_crlf)
        return 0

    if "exec" not in arguments:
        print(f"unexpected fixture argv: {arguments!r}", file=sys.stderr)
        return 2
    exec_index = arguments.index("exec")
    if "--ask-for-approval" in arguments[exec_index + 1 :]:
        print("--ask-for-approval is a global option", file=sys.stderr)
        return 2
    if any(
        option in arguments
        for option in (
            "--dangerously-bypass-approvals-and-sandbox",
            "--yolo",
            "--full-auto",
        )
    ):
        print("unsafe approval or sandbox bypass", file=sys.stderr)
        return 2
    if not has_exact_config and not has_global_approval:
        print("unattended approval policy was not configured", file=sys.stderr)
        return 2

    if model_marker:
        marker = Path(model_marker)
        marker_path = marker / f"{os.getpid()}.json" if marker.is_dir() else marker
        marker_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "argv": arguments,
                    "stdin": sys.stdin.buffer.read().decode("utf-8"),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    if "--output-last-message" in arguments:
        final_path = Path(arguments[arguments.index("--output-last-message") + 1])
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text("{}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "type": "thread.started",
                "thread_id": f"fixture-{os.getpid()}",
            }
        )
    )
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "turn.completed", "usage": {}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
