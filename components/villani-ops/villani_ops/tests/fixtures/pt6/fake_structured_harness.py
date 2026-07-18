#!/usr/bin/env python3
"""Cross-platform fake for PT6 protocol conformance tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


HARNESS = os.environ.get("PT6_FAKE_HARNESS", "codex")
SCENARIO = os.environ.get("PT6_SCENARIO", "success")


def emit(value: object) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def schemas(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    request = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["id", "method"],
        "properties": {"id": {}, "method": {"type": "string"}, "params": {}},
        "additionalProperties": True,
    }
    notification = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["method"],
        "properties": {"method": {"type": "string"}, "params": {}},
        "additionalProperties": True,
    }
    for name, document in (
        ("ClientRequest.json", request),
        ("ClientNotification.json", notification),
        ("ServerRequest.json", request),
        ("ServerNotification.json", notification),
    ):
        (destination / name).write_text(json.dumps(document), encoding="utf-8")
    definitions = {
        name: {"type": "object", "additionalProperties": True}
        for name in (
            "InitializeResponse",
            "ThreadStartResponse",
            "TurnStartResponse",
        )
    }
    if SCENARIO == "schema_change":
        definitions.pop("TurnStartResponse")
    (destination / "codex_app_server_protocol.v2.schemas.json").write_text(
        json.dumps(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "definitions": definitions,
            }
        ),
        encoding="utf-8",
    )


def codex() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print(os.environ.get("PT6_FAKE_VERSION", "codex-cli 0.144.5"))
        return 0
    if arguments[:2] == ["login", "status"]:
        print("Logged in")
        return 0
    if arguments[:2] == ["app-server", "--help"]:
        print("Codex app-server; stdio; generate-json-schema")
        return 0
    if arguments[:3] == ["app-server", "generate-json-schema", "--help"]:
        print("codex app-server generate-json-schema --out")
        return 0
    if arguments[:2] == ["app-server", "generate-json-schema"]:
        destination = Path(arguments[arguments.index("--out") + 1])
        schemas(destination)
        return 0
    if arguments[:2] != ["app-server", "--listen"]:
        return 2
    if SCENARIO == "malformed":
        print("not-json", flush=True)
        return 0
    if SCENARIO == "large_output":
        print("{" + "x" * (8 * 1024 * 1024) + "}", flush=True)
        return 0
    for raw in sys.stdin:
        request = json.loads(raw)
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            emit(
                {
                    "id": request_id,
                    "result": {
                        "codexHome": "/fake/codex",
                        "platformFamily": "fake",
                        "platformOs": "fake-os",
                        "userAgent": "fake-codex/0.144.5",
                    },
                }
            )
        elif method == "thread/start":
            emit(
                {
                    "id": request_id,
                    "result": {
                        "thread": {"id": "thread-pt6"},
                        "model": request["params"].get("model"),
                        "modelProvider": "openai",
                        "reasoningEffort": "medium",
                    },
                }
            )
        elif method == "turn/start":
            emit({"id": request_id, "result": {"turn": {"id": "turn-pt6"}}})
            if SCENARIO in {"success", "known_cost", "secret", "command_recovery"}:
                Path("answer 雪.txt").write_text("patched\n", encoding="utf-8")
            if SCENARIO == "partial_crash":
                Path("partial.txt").write_text("partial\n", encoding="utf-8")
                return 17
            if SCENARIO == "missing_final":
                return 0
            if SCENARIO == "permission":
                emit(
                    {
                        "id": 99,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"command": "echo denied"},
                    }
                )
                continue
            if SCENARIO == "cancel":
                continue
            if SCENARIO == "rate_limit":
                emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-pt6",
                            "turn": {
                                "id": "turn-pt6",
                                "status": "failed",
                                "error": {"codexErrorInfo": "usageLimitExceeded"},
                            },
                        },
                    }
                )
                continue
            if SCENARIO == "command_recovery":
                for index, exit_code in enumerate((1, 0), 1):
                    emit(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": "thread-pt6",
                                "turnId": "turn-pt6",
                                "item": {
                                    "id": f"command-{index}",
                                    "type": "commandExecution",
                                    "command": "fixture-command",
                                    "exitCode": exit_code,
                                    "status": "completed",
                                },
                            },
                        }
                    )
            if SCENARIO == "secret":
                emit(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-pt6",
                            "turnId": "turn-pt6",
                            "item": {
                                "id": "message-secret",
                                "type": "agentMessage",
                                "text": os.environ.get("PT6_SECRET", "missing"),
                            },
                        },
                    }
                )
            emit(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-pt6",
                        "tokenUsage": {
                            "total": {
                                "inputTokens": 11,
                                "outputTokens": 7,
                                "totalTokens": 18,
                            }
                        },
                    },
                }
            )
            emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-pt6",
                        "turn": {"id": "turn-pt6", "status": "completed"},
                    },
                }
            )
        elif request_id == 99:
            emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-pt6",
                        "turn": {"id": "turn-pt6", "status": "completed"},
                    },
                }
            )
        elif method == "turn/interrupt":
            emit({"id": request_id, "result": {}})
            emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-pt6",
                        "turn": {"id": "turn-pt6", "status": "interrupted"},
                    },
                }
            )
    return 0


def claude() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print(os.environ.get("PT6_FAKE_VERSION", "2.1.138 (Claude Code)"))
        return 0
    if arguments[:3] == ["auth", "status", "--json"]:
        print('{"loggedIn":true}')
        return 0
    if arguments == ["--help"]:
        print(
            "--output-format stream-json --permission-mode --settings "
            "--no-session-persistence"
        )
        return 0
    prompt = sys.stdin.read()
    if not prompt:
        return 3
    if SCENARIO == "malformed":
        print("not-json", flush=True)
        return 0
    if SCENARIO == "large_output":
        print("{" + "x" * (8 * 1024 * 1024) + "}", flush=True)
        return 0
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "session-pt6",
            "model": "claude-fixture",
            "tools": ["Read", "Edit", "Bash"],
            "mcp_servers": [],
            "plugins": [],
            "agents": [],
            "permissionMode": "acceptEdits",
            "claude_code_version": "2.1.138",
        }
    )
    if SCENARIO == "cancel":
        time.sleep(30)
        return 0
    if SCENARIO in {"success", "known_cost", "secret", "command_recovery"}:
        Path("answer 雪.txt").write_text("patched\n", encoding="utf-8")
    if SCENARIO == "partial_crash":
        Path("partial.txt").write_text("partial\n", encoding="utf-8")
        return 17
    if SCENARIO == "missing_final":
        return 0
    if SCENARIO == "permission":
        emit(
            {
                "type": "permission_request",
                "request_id": "permission-1",
                "permission": "outside-worktree",
            }
        )
    if SCENARIO == "rate_limit":
        emit({"type": "rate_limit_event", "retry_in_ms": 10})
        emit(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "rate limit 429",
                "session_id": "session-pt6",
            }
        )
        return 0
    if SCENARIO == "command_recovery":
        for index in (1, 2):
            emit(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"tool-{index}",
                                "name": "Bash",
                                "input": {"command": "fixture-command"},
                            }
                        ]
                    },
                }
            )
            emit(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"tool-{index}",
                                "is_error": index == 1,
                            }
                        ]
                    },
                }
            )
    if SCENARIO == "secret":
        emit(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": os.environ.get("PT6_SECRET")}
                    ]
                },
            }
        )
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "session_id": "session-pt6",
        "duration_ms": 25,
        "num_turns": 1,
        "usage": {"input_tokens": 13, "output_tokens": 5},
        "modelUsage": {
            "claude-fixture": {
                "inputTokens": 13,
                "outputTokens": 5,
                "costUSD": 0.0125,
            }
        },
    }
    if SCENARIO == "known_cost":
        result["total_cost_usd"] = 0.0125
    emit(result)
    return 0


def acp() -> int:
    cancelled = False
    for raw in sys.stdin:
        request = json.loads(raw)
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": False,
                            "promptCapabilities": {"image": False},
                        },
                        "agentInfo": {"name": "fixture-acp", "version": "1"},
                    },
                }
            )
        elif method == "session/new":
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"sessionId": "acp-session-pt6"},
                }
            )
        elif method == "session/prompt":
            emit(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "acp-session-pt6",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "working"},
                        },
                    },
                }
            )
            if SCENARIO != "cancel":
                emit(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
        elif method == "session/cancel":
            cancelled = True
        elif method == "$/cancel_request" and cancelled:
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": request["params"]["id"],
                    "result": {"stopReason": "cancelled"},
                }
            )
    return 0


if __name__ == "__main__":
    if HARNESS == "codex":
        raise SystemExit(codex())
    if HARNESS == "claude-code":
        raise SystemExit(claude())
    if HARNESS == "acp":
        raise SystemExit(acp())
    raise SystemExit(2)
