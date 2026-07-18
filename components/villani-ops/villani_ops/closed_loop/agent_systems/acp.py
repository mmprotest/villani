"""Reusable Agent Client Protocol v1 JSON-RPC stdio client.

ACP is deliberately not selected by any production harness automatically. An
adapter must first demonstrate identity and evidence parity in conformance.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import MAXIMUM_BUFFERED_EVENT_BYTES, MAXIMUM_HARNESS_MESSAGE_BYTES
from villani_ops.runners.structured_protocol import (
    JsonLineProcess,
    StructuredProtocolError,
    deadline_remaining,
    terminate_process_tree,
    write_redacted_jsonl,
)


ACP_PROTOCOL_VERSION = 1
ACP_TRANSPORT = "acp-jsonrpc-stdio"


def _inside(root: Path, value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise StructuredProtocolError(
            "acp_path_outside_worktree",
            "ACP requested a path outside the isolated worktree.",
        ) from error
    return resolved


class _Terminal:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        output_limit: int,
    ) -> None:
        if not command:
            raise StructuredProtocolError(
                "acp_terminal_invalid", "ACP terminal command cannot be empty."
            )
        options: dict[str, Any] = {
            "cwd": str(cwd),
            "env": dict(environment),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "shell": False,
        }
        if os.name == "posix":
            options["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.process: subprocess.Popen[bytes] = subprocess.Popen(
            list(command), **options
        )
        self.limit = min(max(output_limit, 1), MAXIMUM_HARNESS_MESSAGE_BYTES)
        self.total_bytes = 0
        self._buffer = bytearray()
        self._cursor = 0
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        try:
            for chunk in iter(lambda: stdout.read(65_536), b""):
                with self._lock:
                    self.total_bytes += len(chunk)
                    remaining = max(self.limit - len(self._buffer), 0)
                    if remaining:
                        self._buffer.extend(chunk[:remaining])
        except OSError:
            return

    def output(self) -> dict[str, Any]:
        with self._lock:
            value = bytes(self._buffer[self._cursor :])
            self._cursor = len(self._buffer)
            truncated = self.total_bytes > len(self._buffer)
        return {
            "output": value.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "exitStatus": (
                {"exitCode": self.process.returncode}
                if self.process.poll() is not None
                else None
            ),
        }

    def wait(self, timeout_ms: int | None) -> dict[str, Any]:
        timeout = None if timeout_ms is None else max(timeout_ms, 0) / 1000
        try:
            exit_code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"exitStatus": None}
        self._reader.join(timeout=1)
        return {"exitStatus": {"exitCode": exit_code}}

    def kill(self) -> None:
        terminate_process_tree(self.process)
        self._reader.join(timeout=1)


@dataclass(frozen=True, slots=True)
class ACPRunResult:
    session_id: str
    stop_reason: str
    prompt_result: dict[str, Any]
    capabilities: dict[str, Any]
    updates: tuple[dict[str, Any], ...]
    raw_event_path: str | None
    cancelled: bool


PermissionDecider = Callable[[Mapping[str, Any]], str | None]


@dataclass(slots=True)
class ACPClient:
    command: Sequence[str]
    worktree: Path
    environment: Mapping[str, str]
    trace_path: Path | None = None
    permission_decider: PermissionDecider | None = None
    secrets: tuple[str, ...] = ()
    process: JsonLineProcess | None = field(default=None, init=False)
    capabilities: dict[str, Any] = field(default_factory=dict, init=False)
    session_id: str | None = field(default=None, init=False)
    updates: list[dict[str, Any]] = field(default_factory=list, init=False)
    raw_events: list[dict[str, Any]] = field(default_factory=list, init=False)
    _raw_bytes: int = field(default=0, init=False)
    _request_id: int = field(default=0, init=False)
    _terminals: dict[str, _Terminal] = field(default_factory=dict, init=False)

    def _record(self, direction: str, message: Mapping[str, Any]) -> None:
        document = {"direction": direction, "message": dict(message)}
        size = len(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if self._raw_bytes + size > MAXIMUM_BUFFERED_EVENT_BYTES:
            raise StructuredProtocolError(
                "acp_event_limit_exceeded",
                "ACP raw evidence exceeded the durable event bound.",
            )
        self._raw_bytes += size
        self.raw_events.append(document)

    def _send(self, message: Mapping[str, Any]) -> None:
        if self.process is None:
            raise RuntimeError("ACP client is not running")
        self._record("client", message)
        self.process.send(message)

    def _notification(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _request(
        self, method: str, params: Mapping[str, Any], *, deadline: float
    ) -> Any:
        self._request_id += 1
        request_id = self._request_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        while deadline_remaining(deadline) > 0:
            assert self.process is not None
            message = self.process.receive(min(0.1, deadline_remaining(deadline)))
            if message is None:
                continue
            if message.get("_villani_eof"):
                raise StructuredProtocolError(
                    "acp_missing_result",
                    f"ACP agent exited before replying to {method}.",
                    True,
                )
            self._record("agent", message)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    error = message.get("error")
                    code = error.get("code") if isinstance(error, Mapping) else None
                    raise StructuredProtocolError(
                        "acp_request_cancelled"
                        if code == -32800
                        else "acp_protocol_error",
                        f"ACP {method} failed: {error}",
                    )
                return message.get("result")
            self._handle(message)
        raise StructuredProtocolError(
            "acp_timeout", f"ACP {method} exceeded its deadline.", True
        )

    def _respond(
        self,
        request_id: Any,
        *,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is None:
            message["result"] = result
        else:
            message["error"] = dict(error)
        self._send(message)

    def _terminal(self, method: str, params: Mapping[str, Any]) -> Any:
        if method == "terminal/create":
            raw_command = params.get("command")
            arguments = params.get("args")
            if isinstance(raw_command, list):
                command = [str(item) for item in raw_command]
            elif isinstance(raw_command, str):
                command = [raw_command]
                if isinstance(arguments, list):
                    command.extend(str(item) for item in arguments)
            else:
                raise StructuredProtocolError(
                    "acp_terminal_invalid", "ACP terminal/create omitted command."
                )
            cwd = _inside(self.worktree, str(params.get("cwd") or self.worktree))
            requested_environment = params.get("env")
            environment = dict(self.environment)
            if isinstance(requested_environment, Mapping):
                environment.update(
                    {
                        str(key): str(value)
                        for key, value in requested_environment.items()
                    }
                )
            terminal_id = f"term_{uuid.uuid4().hex}"
            self._terminals[terminal_id] = _Terminal(
                command,
                cwd=cwd,
                environment=environment,
                output_limit=int(
                    params.get("outputByteLimit") or MAXIMUM_HARNESS_MESSAGE_BYTES
                ),
            )
            return {"terminalId": terminal_id}
        terminal_id = str(params.get("terminalId") or "")
        terminal = self._terminals.get(terminal_id)
        if terminal is None:
            raise StructuredProtocolError(
                "acp_terminal_unknown", "ACP referenced an unknown terminal."
            )
        if method == "terminal/output":
            return terminal.output()
        if method == "terminal/wait_for_exit":
            timeout_value = params.get("timeoutMs")
            return terminal.wait(
                int(timeout_value) if isinstance(timeout_value, int) else None
            )
        if method == "terminal/kill":
            terminal.kill()
            return {}
        if method == "terminal/release":
            if terminal.process.poll() is None:
                terminal.kill()
            self._terminals.pop(terminal_id, None)
            return {}
        raise StructuredProtocolError(
            "acp_terminal_method_unknown", f"Unsupported ACP method {method}."
        )

    def _handle(self, message: Mapping[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params")
        params = dict(params) if isinstance(params, Mapping) else {}
        if method == "session/update":
            self.updates.append(params)
            return
        if "id" not in message:
            return
        request_id = message.get("id")
        try:
            if method == "fs/read_text_file":
                path = _inside(self.worktree, str(params.get("path") or ""))
                payload = path.read_bytes()
                if len(payload) > MAXIMUM_HARNESS_MESSAGE_BYTES:
                    raise StructuredProtocolError(
                        "acp_file_oversized",
                        "ACP file read exceeded the message bound.",
                    )
                result: Any = {"content": payload.decode("utf-8")}
            elif method == "fs/write_text_file":
                path = _inside(self.worktree, str(params.get("path") or ""))
                content = params.get("content")
                if not isinstance(content, str):
                    raise StructuredProtocolError(
                        "acp_file_invalid", "ACP file write requires text content."
                    )
                if len(content.encode("utf-8")) > MAXIMUM_HARNESS_MESSAGE_BYTES:
                    raise StructuredProtocolError(
                        "acp_file_oversized",
                        "ACP file write exceeded the message bound.",
                    )
                if not path.parent.is_dir():
                    raise StructuredProtocolError(
                        "acp_path_missing", "ACP file parent directory does not exist."
                    )
                path.write_text(content, encoding="utf-8")
                result = {}
            elif method.startswith("terminal/"):
                result = self._terminal(method, params)
            elif method == "session/request_permission":
                option_id = (
                    self.permission_decider(params)
                    if self.permission_decider is not None
                    else None
                )
                result = (
                    {"outcome": {"outcome": "selected", "optionId": option_id}}
                    if option_id
                    else {"outcome": {"outcome": "cancelled"}}
                )
            else:
                self._respond(
                    request_id,
                    error={
                        "code": -32601,
                        "message": "Method not supported by Villani.",
                    },
                )
                return
            self._respond(request_id, result=result)
        except (OSError, UnicodeDecodeError, StructuredProtocolError) as error:
            self._respond(
                request_id,
                error={
                    "code": -32602,
                    "message": str(error),
                    "data": {"villaniCode": getattr(error, "code", "acp_io_error")},
                },
            )

    def start(self, *, timeout_seconds: float = 10) -> dict[str, Any]:
        self.worktree = self.worktree.resolve()
        if not self.worktree.is_dir():
            raise ValueError("ACP worktree must exist")
        self.process = JsonLineProcess(
            self.command,
            cwd=self.worktree,
            env=self.environment,
        )
        deadline = time.monotonic() + timeout_seconds
        result = self._request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
                "clientInfo": {"name": "villani", "title": "Villani", "version": "PT6"},
            },
            deadline=deadline,
        )
        if not isinstance(result, Mapping):
            raise StructuredProtocolError(
                "acp_initialize_malformed", "ACP initialize result must be an object."
            )
        negotiated = result.get("protocolVersion")
        if negotiated != ACP_PROTOCOL_VERSION:
            raise StructuredProtocolError(
                "acp_protocol_version_unsupported",
                f"ACP agent negotiated unsupported protocol version {negotiated!r}.",
            )
        raw_capabilities = result.get("agentCapabilities")
        self.capabilities = (
            dict(raw_capabilities) if isinstance(raw_capabilities, Mapping) else {}
        )
        return dict(result)

    def new_session(
        self,
        *,
        mcp_servers: Sequence[Mapping[str, Any]] = (),
        timeout_seconds: float = 10,
    ) -> str:
        result = self._request(
            "session/new",
            {
                "cwd": str(self.worktree),
                "mcpServers": [dict(item) for item in mcp_servers],
            },
            deadline=time.monotonic() + timeout_seconds,
        )
        if not isinstance(result, Mapping) or not result.get("sessionId"):
            raise StructuredProtocolError(
                "acp_session_identity_missing", "ACP session/new omitted sessionId."
            )
        self.session_id = str(result["sessionId"])
        return self.session_id

    def prompt(
        self,
        text: str,
        *,
        timeout_seconds: float,
        cancellation_event: Any | None = None,
    ) -> ACPRunResult:
        if self.session_id is None:
            raise RuntimeError("ACP session has not been created")
        deadline = time.monotonic() + timeout_seconds
        self._request_id += 1
        request_id = self._request_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            }
        )
        cancelled = False
        cancellation_sent = False
        result: Any = None
        while deadline_remaining(deadline) > 0:
            if (
                cancellation_event is not None
                and cancellation_event.is_set()
                and not cancellation_sent
            ):
                cancelled = True
                cancellation_sent = True
                self._notification("session/cancel", {"sessionId": self.session_id})
                self._notification("$/cancel_request", {"id": request_id})
            assert self.process is not None
            message = self.process.receive(min(0.1, deadline_remaining(deadline)))
            if message is None:
                continue
            if message.get("_villani_eof"):
                raise StructuredProtocolError(
                    "acp_missing_final_result",
                    "ACP agent exited without a session/prompt result.",
                    True,
                )
            self._record("agent", message)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    error = message.get("error")
                    code = error.get("code") if isinstance(error, Mapping) else None
                    if cancelled and code == -32800:
                        result = {"stopReason": "cancelled"}
                        break
                    raise StructuredProtocolError(
                        "acp_prompt_error", f"ACP prompt failed: {error}"
                    )
                result = message.get("result")
                break
            self._handle(message)
        if result is None:
            raise StructuredProtocolError(
                "acp_timeout", "ACP prompt exceeded its deadline.", True
            )
        if not isinstance(result, Mapping) or not result.get("stopReason"):
            raise StructuredProtocolError(
                "acp_missing_final_result",
                "ACP session/prompt omitted its stopReason.",
            )
        stop_reason = str(result["stopReason"])
        if cancelled and stop_reason != "cancelled":
            raise StructuredProtocolError(
                "acp_cancellation_not_acknowledged",
                "ACP agent did not complete cancellation with stopReason=cancelled.",
            )
        self._persist()
        return ACPRunResult(
            session_id=self.session_id,
            stop_reason=stop_reason,
            prompt_result=dict(result),
            capabilities=dict(self.capabilities),
            updates=tuple(self.updates),
            raw_event_path=str(self.trace_path) if self.trace_path else None,
            cancelled=cancelled,
        )

    def _persist(self) -> None:
        if self.trace_path is not None:
            write_redacted_jsonl(self.trace_path, self.raw_events, secrets=self.secrets)

    def close(self) -> None:
        for terminal in tuple(self._terminals.values()):
            if terminal.process.poll() is None:
                terminal.kill()
        self._terminals.clear()
        if self.process is not None:
            self.process.close_input()
            if self.process.process.poll() is None:
                self.process.wait(0.5)
            if self.process.process.poll() is None:
                self.process.terminate()
            self.process = None
        self._persist()

    def __enter__(self) -> "ACPClient":
        self.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


__all__ = ["ACPClient", "ACPRunResult", "ACP_PROTOCOL_VERSION", "ACP_TRANSPORT"]
