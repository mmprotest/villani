from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from villani_code.repository_state import repository_state_digest


CommandFailureClass = Literal[
    "executable_not_found",
    "path_not_found",
    "permission_denied",
    "shell_syntax_error",
    "timeout",
    "dependency_missing",
    "test_failure",
    "process_failure",
    "policy_denied",
    "unknown",
]


_VOLATILE_FRAGMENT_RE = re.compile(
    r"(?:0x[0-9a-f]+|\b\d+(?:\.\d+)?(?:ms|s|sec|seconds?)\b)",
    re.IGNORECASE,
)


def normalize_command(command: str) -> str:
    return " ".join(str(command).strip().split())


def normalize_stderr(stderr: str) -> str:
    text = str(stderr).replace("\r\n", "\n").replace("\r", "\n")
    text = _VOLATILE_FRAGMENT_RE.sub("<volatile>", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def classify_command_failure(
    *,
    stderr: str,
    stdout: str = "",
    exit_code: int | None = None,
    policy_denied: bool = False,
    timed_out: bool = False,
) -> CommandFailureClass:
    if policy_denied:
        return "policy_denied"
    if timed_out:
        return "timeout"
    combined = f"{stderr}\n{stdout}".casefold()
    if any(
        marker in combined
        for marker in (
            "command not found",
            "is not recognized as an internal or external command",
            "no such file or directory",
            "the system cannot find the file specified",
            "executable not found",
        )
    ):
        if any(
            marker in combined
            for marker in (
                "command not found",
                "not recognized as an internal or external command",
                "executable not found",
            )
        ):
            return "executable_not_found"
        return "path_not_found"
    if any(
        marker in combined
        for marker in ("permission denied", "access is denied", "operation not permitted")
    ):
        return "permission_denied"
    if any(
        marker in combined
        for marker in (
            "syntax error",
            "unexpected token",
            "unexpected at this time",
            "unterminated quoted",
            "parse error",
        )
    ):
        return "shell_syntax_error"
    if any(
        marker in combined
        for marker in (
            "timed out",
            "timeout expired",
            "command exceeded timeout",
        )
    ):
        return "timeout"
    if any(
        marker in combined
        for marker in (
            "module not found",
            "no module named",
            "could not find package",
            "package is not installed",
            "missing dependency",
            "cannot find module",
            "shared library",
        )
    ):
        return "dependency_missing"
    if exit_code not in {None, 0} and any(
        marker in combined
        for marker in (
            "assertion",
            " failed",
            "failures",
            "test result: failed",
            "tests failed",
        )
    ):
        return "test_failure"
    if exit_code not in {None, 0}:
        return "process_failure"
    return "unknown"


def command_failure_fingerprint(
    *,
    command: str,
    cwd: str,
    exit_code: int | None,
    failure_class: CommandFailureClass,
    stderr: str,
) -> str:
    payload = {
        "command": normalize_command(command),
        "cwd": str(cwd).replace("\\", "/"),
        "exit_code": exit_code,
        "failure_class": failure_class,
        "stderr_sha256": hashlib.sha256(
            normalize_stderr(stderr).encode("utf-8", errors="replace")
        ).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CommandFailureRecord:
    failure_id: str
    command: str
    cwd: str
    exit_code: int | None
    failure_class: CommandFailureClass
    fingerprint: str
    repository_state_digest: str
    summary: str


def _decode_bash_result(result: Mapping[str, Any]) -> dict[str, Any]:
    content = result.get("content", "")
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except ValueError:
            decoded = {}
        if isinstance(decoded, dict):
            return decoded
    return {}


class CommandFailureLedger:
    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self._next_failure_number = 1
        self._records_by_preflight: dict[str, CommandFailureRecord] = {}
        self._records_by_fingerprint: dict[str, CommandFailureRecord] = {}
        self._seen_command_preflight: set[str] = set()
        self.command_calls = 0
        self.failed_command_calls = 0
        self.repeated_commands = 0
        self.unique_command_failures = 0
        self.repeated_command_failures = 0
        self.commands_retried_without_state_change = 0

    def _preflight_key(
        self,
        *,
        command: str,
        cwd: str,
        state_digest: str,
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "command": normalize_command(command),
                    "cwd": str(cwd).replace("\\", "/"),
                    "repository_state_digest": state_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def lookup_repeat(
        self,
        *,
        command: str,
        cwd: str,
    ) -> CommandFailureRecord | None:
        self.command_calls += 1
        state_digest = repository_state_digest(self.repo)
        preflight_key = self._preflight_key(
            command=command,
            cwd=cwd,
            state_digest=state_digest,
        )
        if preflight_key in self._seen_command_preflight:
            self.repeated_commands += 1
        self._seen_command_preflight.add(preflight_key)
        record = self._records_by_preflight.get(preflight_key)
        if record is not None:
            self.failed_command_calls += 1
            self.repeated_command_failures += 1
            self.commands_retried_without_state_change += 1
        return record

    def observe(
        self,
        *,
        command: str,
        cwd: str,
        result: Mapping[str, Any],
        policy_denied: bool = False,
    ) -> CommandFailureRecord | None:
        decoded = _decode_bash_result(result)
        exit_code = decoded.get("exit_code")
        if not isinstance(exit_code, int):
            exit_code = None
        stdout = str(decoded.get("stdout", ""))
        stderr = str(decoded.get("stderr", ""))
        timed_out = bool(decoded.get("timed_out", False))
        if result.get("is_error") and not stderr:
            stderr = str(result.get("content", ""))
        failed = bool(
            policy_denied
            or timed_out
            or result.get("is_error")
            or exit_code not in {None, 0}
        )
        if not failed:
            return None
        self.failed_command_calls += 1
        failure_class = classify_command_failure(
            stderr=stderr,
            stdout=stdout,
            exit_code=exit_code,
            policy_denied=policy_denied,
            timed_out=timed_out,
        )
        fingerprint = command_failure_fingerprint(
            command=command,
            cwd=cwd,
            exit_code=exit_code,
            failure_class=failure_class,
            stderr=stderr,
        )
        prior = self._records_by_fingerprint.get(fingerprint)
        if prior is not None:
            self.repeated_command_failures += 1
            return prior
        state_digest = repository_state_digest(self.repo)
        failure_id = f"command-failure-{self._next_failure_number:04d}"
        self._next_failure_number += 1
        summary = f"{normalize_command(command)!r} failed as {failure_class}"
        record = CommandFailureRecord(
            failure_id=failure_id,
            command=normalize_command(command),
            cwd=str(cwd),
            exit_code=exit_code,
            failure_class=failure_class,
            fingerprint=fingerprint,
            repository_state_digest=state_digest,
            summary=summary,
        )
        self._records_by_fingerprint[fingerprint] = record
        self._records_by_preflight[
            self._preflight_key(
                command=command,
                cwd=cwd,
                state_digest=state_digest,
            )
        ] = record
        self.unique_command_failures += 1
        return record

    @staticmethod
    def repeated_result(record: CommandFailureRecord) -> dict[str, Any]:
        return {
            "content": json.dumps(
                {
                    "repeated_failure": True,
                    "unchanged": True,
                    "prior_failure_id": record.failure_id,
                    "failure_class": record.failure_class,
                    "exit_code": record.exit_code,
                    "summary": record.summary,
                    "guidance": (
                        "Repeating the identical command without changing the "
                        "environment or inputs is unlikely to help. Use Villani "
                        "Read, Grep, Search, FindSymbol, or FindReferences for "
                        "repository inspection where appropriate."
                    ),
                },
                indent=2,
            ),
            "is_error": False,
            "suppressed_repeat": True,
        }

    def telemetry(self) -> dict[str, Any]:
        ratio = (
            self.failed_command_calls / self.command_calls
            if self.command_calls
            else 0.0
        )
        return {
            "unique_command_failures": self.unique_command_failures,
            "repeated_command_failures": self.repeated_command_failures,
            "failed_command_ratio": round(ratio, 6),
            "commands_retried_without_state_change": (
                self.commands_retried_without_state_change
            ),
            "repeated_commands": self.repeated_commands,
        }
