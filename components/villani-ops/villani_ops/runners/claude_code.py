"""Production Claude Code adapter over the official stream-json CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from villani_ops.closed_loop.agent_systems.discovery import claude_version_supported
from villani_ops.closed_loop.agent_systems.models import MAXIMUM_BUFFERED_EVENT_BYTES
from villani_ops.closed_loop.event_writer import redact_data
from villani_ops.runners.base import (
    CandidateExecutionAcknowledgement,
    RunnerContext,
    RunnerResult,
)

from .structured_protocol import (
    JsonLineProcess,
    StructuredProtocolError,
    bounded_utf8_text,
    deadline_remaining,
    resolved_structured_command,
    write_redacted_jsonl,
)


CLAUDE_CODE_PROTOCOL = "claude-code-stream-json"
_VERSION = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _credential_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    values: list[str] = []
    for name, value in environment.items():
        lowered = name.casefold()
        if value and any(
            marker in lowered
            for marker in ("key", "token", "secret", "password", "authorization")
        ):
            values.append(str(value))
    return tuple(dict.fromkeys(values))


def _bounded_probe(
    command: list[str], *, cwd: Path, environment: Mapping[str, str]
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired as error:
        raise StructuredProtocolError(
            "claude_probe_timeout", "Claude Code version probe timed out.", True
        ) from error
    output = (result.stdout + b"\n" + result.stderr)[: 1024 * 1024].decode(
        "utf-8", errors="replace"
    )
    return result.returncode, output.strip()


class ClaudeCodeRunner:
    name = "claude-code"
    uses_vendor_auth = True

    def __init__(
        self,
        *,
        command: str = "claude",
        expected_version: str = "unknown",
        strict_native_sandbox_available: bool = False,
        resume_same_attempt: bool = False,
    ) -> None:
        self.command = command
        self.expected_version = expected_version
        self.strict_native_sandbox_available = strict_native_sandbox_available
        self.resume_same_attempt = resume_same_attempt
        self._resume_sessions: dict[str, str] = {}

    @staticmethod
    def _prompt(context: RunnerContext) -> str:
        criteria = context.success_criteria or ""
        return (
            "Villani supplies two verbatim fields. Work only inside the current "
            "isolated candidate worktree. Delivery is the Git patch, not final prose.\n"
            f"TASK_BYTES={len(context.task_instruction.encode('utf-8'))}\n"
            "---BEGIN VERBATIM TASK---\n"
            f"{context.task_instruction}\n"
            "---END VERBATIM TASK---\n"
            f"CRITERIA_BYTES={len(criteria.encode('utf-8'))}\n"
            "---BEGIN VERBATIM SUCCESS CRITERIA---\n"
            f"{criteria}\n"
            "---END VERBATIM SUCCESS CRITERIA---\n"
        )

    @staticmethod
    def _acknowledge(context: RunnerContext, applied: Mapping[str, Any]) -> None:
        rendered = json.dumps(
            [context.task_instruction, context.success_criteria],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        document = dict(applied)
        acknowledgement = CandidateExecutionAcknowledgement(
            candidate_id=context.attempt_id,
            requested_dimensions=dict(context.candidate_dimensions),
            applied_dimensions=document,
            unsupported_dimensions={
                key: value
                for key, value in context.candidate_dimensions.items()
                if key not in {"agent", "backend_name", "model", "prompt_strategy_id"}
                and value not in {None, "default"}
            },
            rejected_dimensions={},
            provider_acknowledgement={
                "status": "reported_by_claude_code_stream",
                **document,
            },
            runner_acknowledged=True,
            rendered_prompt_digest=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            effective_configuration_digest=hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
            acknowledgement_timestamp=datetime.now(timezone.utc),
        )
        (Path(context.run_dir) / "effective_candidate_configuration.json").write_text(
            acknowledgement.model_dump_json(indent=2), encoding="utf-8"
        )

    def _arguments(self, context: RunnerContext, settings_path: Path) -> list[str]:
        arguments = [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
            "--settings",
            str(settings_path),
            "--setting-sources=",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-chrome",
            "--tools",
            "Bash,Edit,Read,Write,Glob,Grep,NotebookEdit",
        ]
        if not self.resume_same_attempt:
            arguments.append("--no-session-persistence")
        if context.backend.model != "default":
            arguments.extend(["--model", context.backend.model])
        effort = context.backend.metadata.get("reasoning_effort")
        if effort:
            arguments.extend(["--effort", str(effort)])
        maximum_cost = context.backend.metadata.get("max_budget_usd")
        if maximum_cost is not None:
            arguments.extend(["--max-budget-usd", str(maximum_cost)])
        resume_id = self._resume_sessions.get(context.attempt_id)
        if self.resume_same_attempt and resume_id:
            arguments.extend(["--resume", resume_id])
        elif self.resume_same_attempt:
            arguments.extend(
                [
                    "--session-id",
                    str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"villani:{context.attempt_id}")
                    ),
                ]
            )
        return arguments

    @staticmethod
    def _failure(value: Any, fallback: str) -> tuple[str, bool]:
        text = json.dumps(value, ensure_ascii=False).casefold()
        if any(marker in text for marker in ("rate limit", "rate_limit", "429")):
            return "backend_rate_limited", True
        if any(marker in text for marker in ("overloaded", "529")):
            return "transport_overload", True
        if any(marker in text for marker in ("authentication", "unauthorized", "401")):
            return "backend_auth_error", False
        return fallback, False

    @staticmethod
    def _content_events(
        message: Mapping[str, Any], tool_names: dict[str, str]
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        message_type = str(message.get("type") or "")
        body = message.get("message")
        content = (
            body.get("content") if isinstance(body, Mapping) else message.get("content")
        )
        if not isinstance(content, list):
            return output
        for index, item in enumerate(content):
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("type") or "")
            item_id = str(
                item.get("id") or item.get("tool_use_id") or f"{message_type}:{index}"
            )
            if kind == "text":
                output.append(
                    {
                        "event_type": "agent_message",
                        "timestamp": _now(),
                        "source_event_id": item_id,
                        "payload": {"text": item.get("text")},
                    }
                )
            elif kind == "thinking":
                output.append(
                    {
                        "event_type": "reasoning_summary",
                        "timestamp": _now(),
                        "source_event_id": item_id,
                        "payload": {
                            "summary": item.get("thinking") or item.get("summary"),
                            "safe_to_persist": False,
                        },
                    }
                )
            elif kind == "tool_use":
                name = str(item.get("name") or "unknown")
                tool_names[item_id] = name
                raw_input = item.get("input")
                input_value: Mapping[str, Any] = (
                    raw_input if isinstance(raw_input, Mapping) else {}
                )
                lowered = name.casefold()
                if lowered in {"bash", "shell", "terminal"}:
                    event_type = "command_started"
                    payload = {
                        "tool_call_id": item_id,
                        "tool": name,
                        "command": input_value.get("command"),
                    }
                elif lowered in {"edit", "write", "notebookedit"}:
                    event_type = "file_change_start"
                    payload = {
                        "tool_call_id": item_id,
                        "tool": name,
                        "path": input_value.get("file_path") or input_value.get("path"),
                        "mutation": True,
                    }
                elif lowered in {"agent", "task"}:
                    event_type = "subagent_started"
                    payload = {"tool_call_id": item_id, "tool": name}
                else:
                    event_type = "tool_call_started"
                    payload = {"tool_call_id": item_id, "tool": name}
                output.append(
                    {
                        "event_type": event_type,
                        "timestamp": _now(),
                        "source_event_id": item_id,
                        "payload": payload,
                    }
                )
            elif kind == "tool_result":
                name = tool_names.get(item_id, "unknown")
                lowered = name.casefold()
                is_error = bool(item.get("is_error"))
                if lowered in {"bash", "shell", "terminal"}:
                    event_type = "command_completed"
                elif lowered in {"edit", "write", "notebookedit"}:
                    event_type = "file_write"
                elif lowered in {"agent", "task"}:
                    event_type = "subagent_completed"
                else:
                    event_type = "tool_call_completed"
                output.append(
                    {
                        "event_type": event_type,
                        "timestamp": _now(),
                        "source_event_id": f"{item_id}:result",
                        "payload": {
                            "tool_call_id": item_id,
                            "tool": name,
                            "is_error": is_error,
                            "status": "failed" if is_error else "completed",
                        },
                    }
                )
        return output

    def run(self, context: RunnerContext) -> RunnerResult:  # noqa: C901
        started = time.monotonic()
        trace_dir = Path(context.run_dir) / "claude-code-trace"
        trace_dir.mkdir(parents=True, exist_ok=True)
        environment = (
            {**os.environ, **context.env}
            if context.inherit_parent_environment
            else dict(context.env)
        )
        secrets = _credential_values(environment)
        raw_records: list[dict[str, Any]] = []
        raw_bytes = 0
        runtime_events: list[dict[str, Any]] = []
        tool_names: dict[str, str] = {}
        assistant_messages: list[str] = []
        system_metadata: dict[str, Any] = {}
        final_result: dict[str, Any] | None = None
        process: JsonLineProcess | None = None
        failure_code: str | None = None
        failure_retryable: bool | None = None
        error_message = ""
        cancelled = False
        observed_version: str | None = None
        prompt_path = Path(context.run_dir) / "claude-input.txt"

        def record(message: Mapping[str, Any]) -> None:
            nonlocal raw_bytes
            document = {"direction": "server", "message": dict(message)}
            size = len(
                json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            if raw_bytes + size > MAXIMUM_BUFFERED_EVENT_BYTES:
                raise StructuredProtocolError(
                    "protocol_event_limit_exceeded",
                    "Claude Code raw evidence exceeded the durable event bound.",
                )
            raw_bytes += size
            raw_records.append(document)

        try:
            if (
                os.name == "nt"
                and not context.execution_prefix
                and not self.strict_native_sandbox_available
            ):
                raise StructuredProtocolError(
                    "claude_strict_sandbox_unavailable",
                    "Claude Code strict sandboxing is unavailable on native Windows; use WSL2 or an outer container.",
                )
            prefix = resolved_structured_command(
                self.command, context.execution_prefix, []
            )
            version_code, version_output = _bounded_probe(
                [*prefix, "--version"],
                cwd=Path(context.repo_path),
                environment=environment,
            )
            match = _VERSION.search(version_output)
            observed_version = match.group(1) if match else None
            if (
                version_code != 0
                or observed_version is None
                or not claude_version_supported(observed_version)
                or (
                    self.expected_version != "unknown"
                    and observed_version != self.expected_version
                )
            ):
                raise StructuredProtocolError(
                    "claude_unsupported_version",
                    f"Claude Code version is outside the supported range or changed; expected {self.expected_version!r}, observed {observed_version!r}.",
                )
            settings = {
                "permissions": {"defaultMode": "acceptEdits"},
                "sandbox": {
                    "enabled": True,
                    "failIfUnavailable": True,
                    "allowUnsandboxedCommands": False,
                },
            }
            settings_path = Path(context.run_dir) / "claude-settings.json"
            settings_path.write_text(
                json.dumps(settings, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            prompt = self._prompt(context)
            prompt_path.write_text(prompt, encoding="utf-8", newline="\n")
            prompt_digest = (
                f"sha256:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}"
            )
            arguments = self._arguments(context, settings_path)
            command = resolved_structured_command(
                self.command, context.execution_prefix, arguments
            )
            process = JsonLineProcess(
                command,
                cwd=Path(context.repo_path),
                env=environment,
            )
            input_bytes = process.send_input_file(
                prompt_path, cancellation_event=context.cancellation_event
            )
            process.close_input()
            prompt_path.unlink(missing_ok=True)
            (Path(context.run_dir) / "claude-input-metadata.json").write_text(
                json.dumps(
                    {"sha256": prompt_digest, "size_bytes": input_bytes},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            deadline = time.monotonic() + context.timeout_seconds
            while deadline_remaining(deadline) > 0:
                if (
                    context.cancellation_event is not None
                    and context.cancellation_event.is_set()
                ):
                    cancelled = True
                    failure_code = "claude_cancelled"
                    error_message = "Claude Code attempt was cancelled."
                    runtime_events.append(
                        {
                            "event_type": "cancellation",
                            "timestamp": _now(),
                            "payload": {
                                "mechanism": "bounded_process_tree_termination"
                            },
                        }
                    )
                    process.terminate()
                    break
                message = process.receive(min(0.1, deadline_remaining(deadline)))
                if message is None:
                    continue
                if message.get("_villani_eof"):
                    break
                record(message)
                event_type = str(message.get("type") or "")
                if event_type == "system" and message.get("subtype") == "init":
                    system_metadata = {
                        "session_id": message.get("session_id"),
                        "model": message.get("model"),
                        "tools": message.get("tools") or [],
                        "mcp_servers": message.get("mcp_servers") or [],
                        "plugins": message.get("plugins") or [],
                        "agents": message.get("agents") or [],
                        "permission_mode": message.get("permissionMode")
                        or message.get("permission_mode"),
                        "claude_code_version": message.get("claude_code_version"),
                    }
                    session_id = system_metadata.get("session_id")
                    if isinstance(session_id, str) and self.resume_same_attempt:
                        self._resume_sessions[context.attempt_id] = session_id
                    runtime_events.append(
                        {
                            "event_type": "session_started",
                            "timestamp": _now(),
                            "source_event_id": f"claude-session:{session_id}",
                            "payload": dict(system_metadata),
                        }
                    )
                elif event_type in {"assistant", "user"}:
                    events = self._content_events(message, tool_names)
                    runtime_events.extend(events)
                    assistant_messages.extend(
                        str(item.get("payload", {}).get("text"))
                        for item in events
                        if item.get("event_type") == "agent_message"
                        and item.get("payload", {}).get("text") is not None
                    )
                elif event_type in {"rate_limit_event", "retry"}:
                    runtime_events.append(
                        {
                            "event_type": "retry",
                            "timestamp": _now(),
                            "payload": dict(message),
                        }
                    )
                elif event_type == "result":
                    final_result = dict(message)
                    runtime_events.append(
                        {
                            "event_type": "session_complete",
                            "timestamp": _now(),
                            "payload": {
                                "subtype": message.get("subtype"),
                                "is_error": message.get("is_error"),
                                "num_turns": message.get("num_turns"),
                            },
                        }
                    )
                    break
                elif event_type in {"permission_request", "permission"}:
                    request_id = str(
                        message.get("request_id") or message.get("id") or "unknown"
                    )
                    runtime_events.extend(
                        [
                            {
                                "event_type": "permission_request",
                                "timestamp": _now(),
                                "source_event_id": request_id,
                                "payload": {
                                    "request_id": request_id,
                                    "permission": message.get("permission")
                                    or "unknown",
                                },
                            },
                            {
                                "event_type": "permission_resolution",
                                "timestamp": _now(),
                                "source_event_id": f"{request_id}:resolution",
                                "payload": {
                                    "request_id": request_id,
                                    "resolution": "denied_non_interactively",
                                },
                            },
                        ]
                    )
            if not cancelled and final_result is None:
                if deadline_remaining(deadline) <= 0:
                    failure_code, failure_retryable = "claude_timeout", True
                    error_message = "Claude Code exceeded the attempt deadline."
                else:
                    failure_code, failure_retryable = (
                        "claude_missing_final_result",
                        True,
                    )
                    error_message = "Claude Code exited without a final result event."
            elif final_result is not None and bool(final_result.get("is_error")):
                failure_code, failure_retryable = self._failure(
                    final_result, "claude_coding_failure"
                )
                error_message = str(final_result.get("result") or "Claude Code failed.")
            if final_result is not None:
                actual_model = str(
                    system_metadata.get("model") or context.backend.model
                )
                self._acknowledge(
                    context,
                    {
                        "agent": "claude-code",
                        "backend_name": context.backend.name,
                        "model": actual_model,
                        "provider": "anthropic",
                        "permission_mode": system_metadata.get("permission_mode"),
                    },
                )
        except StructuredProtocolError as error:
            failure_code = error.code
            failure_retryable = error.retryable
            cancelled = cancelled or error.code.endswith("cancelled")
            error_message = error.message
        except (OSError, ValueError) as error:
            failure_code = "claude_protocol_error"
            failure_retryable = False
            error_message = (
                f"Claude Code integration failed: {type(error).__name__}: {error}"
            )
        finally:
            prompt_path.unlink(missing_ok=True)
            if process is not None:
                if process.process.poll() is None:
                    process.wait(0.5)
                if process.process.poll() is None:
                    process.terminate()
                stderr = process.stderr
                stderr_truncated = process.stderr_truncated
            else:
                stderr = ""
                stderr_truncated = False
            write_redacted_jsonl(
                trace_dir / "raw-stream.jsonl", raw_records, secrets=secrets
            )
            write_redacted_jsonl(
                trace_dir / "normalized-events.jsonl",
                runtime_events,
                secrets=secrets,
            )

        result_document = final_result or {}
        usage = result_document.get("usage")
        usage = dict(usage) if isinstance(usage, Mapping) else {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        usage_complete = (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
        )
        per_model = result_document.get("modelUsage") or result_document.get(
            "model_usage"
        )
        per_model_usage = dict(per_model) if isinstance(per_model, Mapping) else {}
        total_cost = result_document.get("total_cost_usd")
        authoritative_cost: float | None = None
        if isinstance(total_cost, (int, float)) and not isinstance(total_cost, bool):
            authoritative_cost = float(total_cost)
        known_cost = authoritative_cost is not None
        input_token_count = (
            input_tokens
            if isinstance(input_tokens, int) and not isinstance(input_tokens, bool)
            else 0
        )
        output_token_count = (
            output_tokens
            if isinstance(output_tokens, int) and not isinstance(output_tokens, bool)
            else 0
        )
        per_model_cost: dict[str, float] = {}
        for model, raw in per_model_usage.items():
            if not isinstance(raw, Mapping):
                continue
            value = raw.get("costUSD")
            if value is None:
                value = raw.get("cost_usd")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                per_model_cost[str(model)] = float(value)
        command_events = [
            event
            for event in runtime_events
            if event.get("event_type") == "command_completed"
        ]
        file_events = [
            event for event in runtime_events if event.get("event_type") == "file_write"
        ]
        tool_events = [
            event
            for event in runtime_events
            if str(event.get("event_type") or "").endswith(("_started", "_completed"))
        ]
        actual_identity = {
            "harness_id": "claude-code",
            "harness_version": observed_version or self.expected_version,
            "protocol": CLAUDE_CODE_PROTOCOL,
            "protocol_version": "stream-json",
            "session_id": system_metadata.get("session_id"),
            "model_id": system_metadata.get("model") or context.backend.model,
            "provider": "anthropic",
            "reasoning_effort": context.backend.metadata.get("reasoning_effort"),
            "system_metadata": {
                "tools": system_metadata.get("tools") or [],
                "mcp_servers": system_metadata.get("mcp_servers") or [],
                "plugins": system_metadata.get("plugins") or [],
                "agents": system_metadata.get("agents") or [],
                "permission_mode": system_metadata.get("permission_mode"),
                "strict_sandbox": True,
                "no_session_persistence": not self.resume_same_attempt,
            },
        }
        return RunnerResult(
            exit_code=130 if cancelled else 1 if failure_code else 0,
            stdout=bounded_utf8_text(
                str(result_document.get("result") or "\n".join(assistant_messages))
            ),
            stderr=bounded_utf8_text(
                stderr + (f"\n{error_message}" if error_message else "")
            ),
            input_tokens=input_token_count,
            output_tokens=output_token_count,
            total_tokens=(
                input_token_count + output_token_count if usage_complete else None
            ),
            total_cost=authoritative_cost,
            cost_currency="USD" if known_cost else None,
            cost_accounting_status="complete" if known_cost else "unknown",
            cost_source="claude_code_authoritative_total_cost_usd"
            if known_cost
            else None,
            per_model_usage={
                **per_model_usage,
                "_cost_usd": per_model_cost,
            }
            if per_model_usage or per_model_cost
            else {},
            token_accounting_status="verified" if usage_complete else "missing",
            token_accounting_warnings=(
                []
                if usage_complete
                else ["Claude Code token usage was unavailable or incomplete."]
            ),
            debug_artifact_dir=str(trace_dir),
            duration_ms=max(int((time.monotonic() - started) * 1000), 0),
            model_requests=1 if system_metadata else 0,
            model_failures=1 if failure_code else 0,
            total_tool_calls=len(tool_events),
            total_file_writes=len(file_events),
            commands_executed=len(command_events),
            commands_failed=sum(
                event.get("payload", {}).get("is_error") is True
                for event in command_events
            ),
            runtime_events=runtime_events,
            failure_code=failure_code,
            failure_retryable=failure_retryable,
            cancelled=cancelled,
            telemetry={
                "protocol": CLAUDE_CODE_PROTOCOL,
                "harness_execution_identity": actual_identity,
                "raw_protocol_path": "claude-code-trace/raw-stream.jsonl",
                "completion": redact_data(final_result, secrets=secrets),
                "system_metadata": redact_data(system_metadata, secrets=secrets),
                "stderr_truncated": stderr_truncated,
                "resume_scope": "same_attempt_only"
                if self.resume_same_attempt
                else "disabled",
            },
        )

    def run_task(self, **kwargs: Any) -> RunnerResult:
        raise NotImplementedError("Claude Code PT6 execution requires a RunnerContext")


__all__ = ["CLAUDE_CODE_PROTOCOL", "ClaudeCodeRunner"]
