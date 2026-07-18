"""Production Codex app-server adapter using its installed JSON schema."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft7Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import (  # type: ignore[import-untyped]
    ValidationError as JsonSchemaValidationError,
)

from villani_ops.closed_loop.agent_systems.models import (
    MAXIMUM_BUFFERED_EVENT_BYTES,
)
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


CODEX_APP_SERVER_PROTOCOL = "codex-app-server-jsonrpc-stdio"
CODEX_SCHEMA_FILES = (
    "ClientNotification.json",
    "ClientRequest.json",
    "ServerNotification.json",
    "ServerRequest.json",
    "codex_app_server_protocol.v2.schemas.json",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _credential_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    output: list[str] = []
    for name, value in environment.items():
        lowered = name.casefold()
        if value and any(
            marker in lowered
            for marker in ("key", "token", "secret", "password", "authorization")
        ):
            output.append(str(value))
    return tuple(dict.fromkeys(output))


class CodexSchemaSet:
    """Validate both sides of the exact installed Codex app-server protocol."""

    def __init__(self, directory: Path) -> None:
        missing = [
            name for name in CODEX_SCHEMA_FILES if not (directory / name).is_file()
        ]
        if missing:
            raise StructuredProtocolError(
                "codex_schema_missing",
                f"Codex schema generation omitted: {', '.join(missing)}.",
            )
        self.directory = directory
        self.documents = {
            name: json.loads((directory / name).read_text(encoding="utf-8"))
            for name in CODEX_SCHEMA_FILES
        }
        for document in self.documents.values():
            Draft7Validator.check_schema(document)
        self.client_request = Draft7Validator(self.documents["ClientRequest.json"])
        self.client_notification = Draft7Validator(
            self.documents["ClientNotification.json"]
        )
        self.server_notification = Draft7Validator(
            self.documents["ServerNotification.json"]
        )
        self.server_request = Draft7Validator(self.documents["ServerRequest.json"])
        bundle = self.documents["codex_app_server_protocol.v2.schemas.json"]
        self.bundle = bundle
        encoded = (directory / "codex_app_server_protocol.v2.schemas.json").read_bytes()
        self.digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @staticmethod
    def _validate(
        validator: Draft7Validator, value: Mapping[str, Any], label: str
    ) -> None:
        try:
            validator.validate(dict(value))
        except JsonSchemaValidationError as error:
            path = "/".join(str(item) for item in error.absolute_path) or "/"
            raise StructuredProtocolError(
                "codex_schema_change",
                f"Codex {label} did not match the installed schema at {path}: {error.message}",
            ) from error

    def validate_client_request(self, value: Mapping[str, Any]) -> None:
        self._validate(self.client_request, value, "client request")

    def validate_client_notification(self, value: Mapping[str, Any]) -> None:
        self._validate(self.client_notification, value, "client notification")

    def validate_server_message(self, value: Mapping[str, Any]) -> None:
        if "method" not in value:
            return
        validator = self.server_request if "id" in value else self.server_notification
        self._validate(validator, value, "server message")

    def validate_result(self, definition: str, value: Any) -> None:
        if definition not in self.bundle.get("definitions", {}):
            raise StructuredProtocolError(
                "codex_schema_change",
                f"Installed Codex schema omitted response definition {definition}.",
            )
        schema = {
            "$schema": self.bundle.get("$schema"),
            "$ref": f"#/definitions/{definition}",
            "definitions": self.bundle["definitions"],
        }
        self._validate(Draft7Validator(schema), value, f"{definition} result")


class CodexAppServerRunner:
    name = "codex"
    uses_vendor_auth = True

    def __init__(
        self,
        *,
        command: str = "codex",
        expected_version: str = "unknown",
        reasoning_effort: str | None = None,
    ) -> None:
        self.command = command
        self.expected_version = expected_version
        self.reasoning_effort = reasoning_effort

    def _run_bounded(
        self,
        context: RunnerContext,
        arguments: list[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        command = resolved_structured_command(
            self.command, context.execution_prefix, arguments
        )
        try:
            completed = subprocess.run(
                command,
                cwd=context.repo_path,
                env=dict(context.env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise StructuredProtocolError(
                "codex_probe_timeout",
                f"Codex {' '.join(arguments[:2])} timed out.",
                retryable=True,
            ) from error
        if len(completed.stdout) > 1024 * 1024 or len(completed.stderr) > 1024 * 1024:
            raise StructuredProtocolError(
                "codex_probe_oversized",
                "Codex emitted oversized output during a bounded protocol probe.",
            )
        return completed

    def _schemas(self, context: RunnerContext) -> CodexSchemaSet:
        schema_dir = Path(context.run_dir) / "codex-installed-schema"
        if schema_dir.exists():
            raise StructuredProtocolError(
                "codex_schema_directory_exists",
                "Codex schema destination already exists; refusing stale schema reuse.",
            )
        schema_dir.mkdir(parents=True)
        result = self._run_bounded(
            context,
            ["app-server", "generate-json-schema", "--out", str(schema_dir)],
            timeout=15,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace")[-1000:]
            raise StructuredProtocolError(
                "codex_schema_generation_failed",
                f"Codex could not generate its installed app-server schema: {message}",
            )
        return CodexSchemaSet(schema_dir)

    @staticmethod
    def _failure(error: Any, fallback: str) -> tuple[str, bool]:
        text = json.dumps(error, ensure_ascii=False).casefold()
        if (
            "serveroverloaded" in text
            or "server overloaded" in text
            or "-32001" in text
        ):
            return "transport_overload", True
        if (
            "usagelimitexceeded" in text
            or "rate limit" in text
            or "rate_limit" in text
            or "429" in text
        ):
            return "backend_rate_limited", True
        if "unauthorized" in text or "authentication" in text or "401" in text:
            return "backend_auth_error", False
        if "connection" in text or "disconnected" in text:
            return "protocol_transport_error", True
        return fallback, False

    @staticmethod
    def _event_from_item(
        method: str, params: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        item = params.get("item")
        if not isinstance(item, Mapping):
            return None
        item_type = str(item.get("type") or "unknown")
        started = method == "item/started"
        completed = method == "item/completed"
        timestamp_ms = (
            params.get("startedAtMs") if started else params.get("completedAtMs")
        )
        timestamp = (
            datetime.fromtimestamp(
                float(timestamp_ms) / 1000, tz=timezone.utc
            ).isoformat()
            if isinstance(timestamp_ms, (int, float))
            else _now()
        )
        common = {
            "item_id": item.get("id"),
            "item_type": item_type,
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
        }
        lowered = item_type.casefold().replace("_", "")
        if "commandexecution" in lowered or lowered == "command":
            return {
                "event_type": "command_started" if started else "command_completed",
                "timestamp": timestamp,
                "source_event_id": str(item.get("id") or f"codex:{timestamp}"),
                "payload": {
                    **common,
                    "command": item.get("command") or item.get("cmd"),
                    "exit_code": item.get("exitCode") or item.get("exit_code"),
                    "status": item.get("status"),
                },
            }
        if "filechange" in lowered:
            return {
                "event_type": "file_write" if completed else "file_change_start",
                "timestamp": timestamp,
                "source_event_id": str(item.get("id") or f"codex:{timestamp}"),
                "payload": {**common, "changes": item.get("changes"), "mutation": True},
            }
        if "agentmessage" in lowered:
            return {
                "event_type": "agent_message",
                "timestamp": timestamp,
                "source_event_id": str(item.get("id") or f"codex:{timestamp}"),
                "payload": {**common, "text": item.get("text")},
            }
        if "reasoning" in lowered:
            return {
                "event_type": "reasoning_summary",
                "timestamp": timestamp,
                "source_event_id": str(item.get("id") or f"codex:{timestamp}"),
                "payload": {
                    **common,
                    "summary": item.get("summary"),
                    "safe_to_persist": True,
                },
            }
        if "plan" in lowered:
            return {
                "event_type": "plan_update",
                "timestamp": timestamp,
                "source_event_id": str(item.get("id") or f"codex:{timestamp}"),
                "payload": {**common, "plan": item.get("text") or item.get("plan")},
            }
        return {
            "event_type": "tool_call_started" if started else "tool_call_completed",
            "timestamp": timestamp,
            "source_event_id": str(item.get("id") or f"codex:{timestamp}"),
            "payload": {**common, "status": item.get("status")},
        }

    @staticmethod
    def _acknowledge(context: RunnerContext, applied: Mapping[str, Any]) -> None:
        rendered = json.dumps(
            [context.task_instruction, context.success_criteria],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        applied_document = dict(applied)
        acknowledgement = CandidateExecutionAcknowledgement(
            candidate_id=context.attempt_id,
            requested_dimensions=dict(context.candidate_dimensions),
            applied_dimensions=applied_document,
            unsupported_dimensions={
                key: value
                for key, value in context.candidate_dimensions.items()
                if key not in {"agent", "backend_name", "model", "prompt_strategy_id"}
                and value not in {None, "default"}
            },
            rejected_dimensions={},
            provider_acknowledgement={
                "status": "reported_by_codex_app_server",
                **applied_document,
            },
            runner_acknowledged=True,
            rendered_prompt_digest=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            effective_configuration_digest=hashlib.sha256(
                json.dumps(
                    applied_document, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
            acknowledgement_timestamp=datetime.now(timezone.utc),
        )
        (Path(context.run_dir) / "effective_candidate_configuration.json").write_text(
            acknowledgement.model_dump_json(indent=2), encoding="utf-8"
        )

    def run(self, context: RunnerContext) -> RunnerResult:  # noqa: C901
        started = time.monotonic()
        raw_records: list[dict[str, Any]] = []
        raw_record_bytes = 0
        runtime_events: list[dict[str, Any]] = []
        trace_dir = Path(context.run_dir) / "codex-app-server-trace"
        trace_dir.mkdir(parents=True, exist_ok=True)
        environment = (
            {**os.environ, **context.env}
            if context.inherit_parent_environment
            else dict(context.env)
        )
        secrets = _credential_values(environment)
        process: JsonLineProcess | None = None
        schema: CodexSchemaSet | None = None
        thread_id: str | None = None
        turn_id: str | None = None
        actual_identity: dict[str, Any] = {}
        usage: dict[str, Any] = {}
        final_turn: dict[str, Any] | None = None
        assistant_messages: list[str] = []
        failure_code: str | None = None
        failure_retryable: bool | None = None
        cancelled = False
        error_message = ""

        def record(direction: str, message: Mapping[str, Any]) -> None:
            nonlocal raw_record_bytes
            document = {"direction": direction, "message": dict(message)}
            size = len(
                json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            if raw_record_bytes + size > MAXIMUM_BUFFERED_EVENT_BYTES:
                raise StructuredProtocolError(
                    "protocol_event_limit_exceeded",
                    "Codex raw protocol evidence exceeded the durable event bound.",
                )
            raw_record_bytes += size
            raw_records.append(document)

        def send_request(
            request_id: int, method: str, params: Mapping[str, Any]
        ) -> None:
            assert process is not None and schema is not None
            message = {"id": request_id, "method": method, "params": dict(params)}
            schema.validate_client_request(message)
            record("client", message)
            process.send(message)

        def send_notification(
            method: str, params: Mapping[str, Any] | None = None
        ) -> None:
            assert process is not None and schema is not None
            message: dict[str, Any] = {"method": method}
            if params is not None:
                message["params"] = dict(params)
            schema.validate_client_notification(message)
            record("client", message)
            process.send(message)

        def permission_response(message: Mapping[str, Any]) -> None:
            assert process is not None
            method = str(message.get("method") or "")
            request_id = message.get("id")
            raw_params = message.get("params")
            params: Mapping[str, Any] = (
                raw_params if isinstance(raw_params, Mapping) else {}
            )
            runtime_events.append(
                {
                    "event_type": "permission_request",
                    "timestamp": _now(),
                    "source_event_id": f"codex-permission:{request_id}",
                    "payload": {
                        "request_id": str(request_id),
                        "permission": method,
                        "details": dict(params),
                    },
                }
            )
            if method == "item/commandExecution/requestApproval":
                result: Any = {"decision": "decline"}
            elif method == "item/fileChange/requestApproval":
                result = {"decision": "decline"}
            elif method == "item/permissions/requestApproval":
                result = {"permissions": {}, "scope": "turn"}
            else:
                response = {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "Villani does not permit this non-interactive request.",
                    },
                }
                record("client", response)
                process.send(response)
                return
            response = {"id": request_id, "result": result}
            record("client", response)
            process.send(response)
            runtime_events.append(
                {
                    "event_type": "permission_resolution",
                    "timestamp": _now(),
                    "source_event_id": f"codex-permission-resolution:{request_id}",
                    "payload": {
                        "request_id": str(request_id),
                        "resolution": "declined",
                    },
                }
            )

        def handle(message: Mapping[str, Any]) -> None:
            nonlocal usage, final_turn, thread_id, turn_id
            assert schema is not None
            schema.validate_server_message(message)
            record("server", message)
            if "method" in message and "id" in message:
                permission_response(message)
                return
            method = str(message.get("method") or "")
            raw_params = message.get("params")
            params: Mapping[str, Any] = (
                raw_params if isinstance(raw_params, Mapping) else {}
            )
            if method in {"item/started", "item/completed"}:
                event = self._event_from_item(method, params)
                if event:
                    runtime_events.append(event)
                    if event["event_type"] == "agent_message":
                        text = event["payload"].get("text")
                        if isinstance(text, str):
                            assistant_messages.append(text)
            elif method == "turn/plan/updated":
                runtime_events.append(
                    {
                        "event_type": "plan_update",
                        "timestamp": _now(),
                        "payload": dict(params),
                    }
                )
            elif method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage")
                if isinstance(token_usage, Mapping):
                    raw_total = token_usage.get("total")
                    usage = dict(raw_total) if isinstance(raw_total, Mapping) else {}
                runtime_events.append(
                    {
                        "event_type": "usage_update",
                        "timestamp": _now(),
                        "payload": dict(params),
                    }
                )
            elif method == "turn/completed":
                value = params.get("turn")
                final_turn = dict(value) if isinstance(value, Mapping) else {}
                thread_id = str(params.get("threadId") or thread_id or "") or None
                turn_id = str(final_turn.get("id") or turn_id or "") or None
            elif "warning" in method.casefold() or "error" in method.casefold():
                runtime_events.append(
                    {
                        "event_type": "warning",
                        "timestamp": _now(),
                        "payload": {"method": method, "details": dict(params)},
                    }
                )

        def await_response(request_id: int, definition: str, deadline: float) -> Any:
            assert process is not None and schema is not None
            while deadline_remaining(deadline) > 0:
                if (
                    context.cancellation_event is not None
                    and context.cancellation_event.is_set()
                ):
                    raise StructuredProtocolError(
                        "codex_cancelled", "Codex attempt was cancelled."
                    )
                message = process.receive(min(0.1, deadline_remaining(deadline)))
                if message is None:
                    continue
                if message.get("_villani_eof"):
                    raise StructuredProtocolError(
                        "codex_missing_final_result",
                        "Codex app-server closed before returning a required response.",
                        retryable=True,
                    )
                if message.get("id") == request_id:
                    record("server", message)
                    if "error" in message:
                        code, retryable = self._failure(
                            message.get("error"), "codex_protocol_error"
                        )
                        raise StructuredProtocolError(
                            code,
                            str(redact_data(message.get("error"), secrets=secrets)),
                            retryable,
                        )
                    result = message.get("result")
                    schema.validate_result(definition, result)
                    return result
                handle(message)
            raise StructuredProtocolError(
                "codex_timeout", "Codex app-server exceeded the attempt deadline.", True
            )

        try:
            version_result = self._run_bounded(context, ["--version"], timeout=5)
            observed_version = (
                version_result.stdout.decode("utf-8", errors="replace").strip()
                or version_result.stderr.decode("utf-8", errors="replace").strip()
            )
            if version_result.returncode != 0 or (
                self.expected_version != "unknown"
                and self.expected_version not in observed_version
            ):
                raise StructuredProtocolError(
                    "codex_version_changed",
                    f"Codex identity changed before execution; expected {self.expected_version!r}, observed {observed_version!r}.",
                )
            schema = self._schemas(context)
            command = resolved_structured_command(
                self.command,
                context.execution_prefix,
                ["app-server", "--listen", "stdio://"],
            )
            process = JsonLineProcess(
                command,
                cwd=Path(context.repo_path),
                env=environment,
            )
            deadline = time.monotonic() + context.timeout_seconds
            send_request(
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "villani",
                        "title": "Villani",
                        "version": "PT6",
                    }
                },
            )
            initialize_result = await_response(1, "InitializeResponse", deadline)
            send_notification("initialized")
            thread_params: dict[str, Any] = {
                "cwd": str(Path(context.repo_path).resolve()),
                "modelProvider": "openai",
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "ephemeral": True,
                "developerInstructions": (
                    "Work only inside the supplied isolated candidate worktree. "
                    "The first user text block is the verbatim task and the second "
                    "is the verbatim success criteria. Do not rely on final prose as "
                    "delivery; Villani collects the patch from Git."
                ),
            }
            if context.backend.model != "default":
                thread_params["model"] = context.backend.model
            send_request(2, "thread/start", thread_params)
            thread_result = await_response(2, "ThreadStartResponse", deadline)
            thread = thread_result.get("thread")
            if not isinstance(thread, Mapping) or not thread.get("id"):
                raise StructuredProtocolError(
                    "codex_missing_thread_identity",
                    "Codex thread/start omitted its thread identity.",
                )
            thread_id = str(thread["id"])
            actual_identity = {
                "harness_id": "codex",
                "harness_version": self.expected_version,
                "app_server_version": self.expected_version,
                "protocol": CODEX_APP_SERVER_PROTOCOL,
                "protocol_version": "installed-v2-schema",
                "protocol_schema_digest": schema.digest,
                "thread_id": thread_id,
                "session_id": thread.get("sessionId"),
                "model_id": thread_result.get("model"),
                "provider": thread_result.get("modelProvider"),
                "reasoning_effort": thread_result.get("reasoningEffort"),
                "system_metadata": {
                    "codex_home": initialize_result.get("codexHome"),
                    "platform_family": initialize_result.get("platformFamily"),
                    "platform_os": initialize_result.get("platformOs"),
                    "user_agent": initialize_result.get("userAgent"),
                },
            }
            self._acknowledge(
                context,
                {
                    "agent": "codex",
                    "backend_name": context.backend.name,
                    "model": actual_identity["model_id"],
                    "provider": actual_identity["provider"],
                    "reasoning_effort": actual_identity["reasoning_effort"],
                },
            )
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [
                    {"type": "text", "text": context.task_instruction},
                    {"type": "text", "text": context.success_criteria or ""},
                ],
                "cwd": str(Path(context.repo_path).resolve()),
                "approvalPolicy": "never",
            }
            if context.backend.model != "default":
                turn_params["model"] = context.backend.model
            effort = self.reasoning_effort or context.backend.metadata.get(
                "reasoning_effort"
            )
            if effort:
                turn_params["effort"] = str(effort)
            send_request(3, "turn/start", turn_params)
            turn_result = await_response(3, "TurnStartResponse", deadline)
            turn = turn_result.get("turn")
            if not isinstance(turn, Mapping) or not turn.get("id"):
                raise StructuredProtocolError(
                    "codex_missing_turn_identity",
                    "Codex turn/start omitted its turn identity.",
                )
            turn_id = str(turn["id"])
            actual_identity["turn_id"] = turn_id
            cancellation_sent = False
            while final_turn is None and deadline_remaining(deadline) > 0:
                if (
                    context.cancellation_event is not None
                    and context.cancellation_event.is_set()
                ):
                    if not cancellation_sent:
                        send_request(
                            4,
                            "turn/interrupt",
                            {"threadId": thread_id, "turnId": turn_id},
                        )
                        cancellation_sent = True
                        cancelled = True
                    if deadline_remaining(deadline) <= 2:
                        break
                message = process.receive(min(0.1, deadline_remaining(deadline)))
                if message is None:
                    continue
                if message.get("_villani_eof"):
                    break
                handle(message)
            if final_turn is None:
                if cancelled:
                    error_message = "Codex attempt cancelled before final completion."
                    failure_code = "codex_cancelled"
                elif deadline_remaining(deadline) <= 0:
                    error_message = "Codex app-server exceeded the attempt deadline."
                    failure_code, failure_retryable = "codex_timeout", True
                else:
                    error_message = "Codex app-server exited without turn/completed."
                    failure_code, failure_retryable = "codex_missing_final_result", True
            else:
                status = str(final_turn.get("status") or "")
                if status == "interrupted":
                    cancelled = True
                    failure_code = "codex_cancelled"
                    error_message = "Codex turn was interrupted."
                elif status != "completed":
                    failure_code, failure_retryable = self._failure(
                        final_turn.get("error"), "codex_coding_failure"
                    )
                    error_message = str(final_turn.get("error") or "Codex turn failed.")
        except StructuredProtocolError as error:
            failure_code = error.code
            failure_retryable = error.retryable
            cancelled = cancelled or error.code.endswith("cancelled")
            error_message = error.message
        except (OSError, ValueError, JsonSchemaValidationError) as error:
            failure_code = "codex_protocol_error"
            failure_retryable = False
            error_message = f"Codex integration failed: {type(error).__name__}: {error}"
        finally:
            if process is not None:
                if process.process.poll() is None:
                    process.close_input()
                    process.wait(0.5)
                if process.process.poll() is None:
                    process.terminate()
                stderr = process.stderr
                stderr_truncated = process.stderr_truncated
            else:
                stderr = ""
                stderr_truncated = False
            write_redacted_jsonl(
                trace_dir / "raw-protocol.jsonl", raw_records, secrets=secrets
            )
            write_redacted_jsonl(
                trace_dir / "normalized-events.jsonl",
                runtime_events,
                secrets=secrets,
            )

        total_usage = usage if isinstance(usage, Mapping) else {}
        input_tokens = total_usage.get("inputTokens")
        output_tokens = total_usage.get("outputTokens")
        usage_complete = (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
        )
        input_token_count = input_tokens if usage_complete else 0
        output_token_count = output_tokens if usage_complete else 0
        raw_total_tokens = total_usage.get("totalTokens")
        total_token_count = (
            raw_total_tokens
            if isinstance(raw_total_tokens, int)
            and not isinstance(raw_total_tokens, bool)
            else None
        )
        tool_events = [
            item
            for item in runtime_events
            if str(item.get("event_type", "")).startswith("tool_call_")
        ]
        command_events = [
            item
            for item in runtime_events
            if item.get("event_type") == "command_completed"
        ]
        file_events = [
            item for item in runtime_events if item.get("event_type") == "file_write"
        ]
        return RunnerResult(
            exit_code=130 if cancelled else 1 if failure_code else 0,
            stdout="\n".join(assistant_messages),
            stderr=bounded_utf8_text(
                stderr + (f"\n{error_message}" if error_message else "")
            ),
            input_tokens=input_token_count,
            output_tokens=output_token_count,
            total_tokens=total_token_count,
            token_accounting_status="verified" if usage_complete else "missing",
            token_accounting_warnings=(
                ["Codex token usage was unavailable or incomplete."]
                if not usage_complete
                else []
            ),
            debug_artifact_dir=str(trace_dir),
            duration_ms=max(int((time.monotonic() - started) * 1000), 0),
            model_requests=1 if turn_id else 0,
            model_failures=1 if failure_code else 0,
            total_tool_calls=len(tool_events),
            total_file_writes=len(file_events),
            commands_executed=len(command_events),
            commands_failed=sum(
                item.get("payload", {}).get("exit_code") not in {0, None}
                for item in command_events
            ),
            runtime_events=runtime_events,
            failure_code=failure_code,
            failure_retryable=failure_retryable,
            cancelled=cancelled,
            telemetry={
                "protocol": CODEX_APP_SERVER_PROTOCOL,
                "harness_execution_identity": actual_identity,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "schema_digest": schema.digest if schema else None,
                "raw_protocol_path": "codex-app-server-trace/raw-protocol.jsonl",
                "stderr_truncated": stderr_truncated,
                "completion": final_turn,
            },
        )

    def run_task(self, **kwargs: Any) -> RunnerResult:
        raise NotImplementedError("Codex PT6 execution requires a RunnerContext")


__all__ = ["CODEX_APP_SERVER_PROTOCOL", "CodexAppServerRunner", "CodexSchemaSet"]
