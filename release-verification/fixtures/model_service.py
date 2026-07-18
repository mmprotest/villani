#!/usr/bin/env python3
"""Deterministic OpenAI-compatible model service for packaged release scenarios.

This module is release-test infrastructure. Production packages do not import it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MODEL_IDENTITIES = {
    "fixture-classifier",
    "fixture-economy",
    "fixture-standard",
    "fixture-expert",
    "fixture-verifier-low",
    "fixture-verifier-high",
    "fixture-onboarding",
}
SCENARIO_RE = re.compile(r"release[_ -]scenario[:= ]+([a-z0-9_-]+)", re.IGNORECASE)
EVIDENCE_RE = re.compile(r"\bev-[0-9]{4,}\b")
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{16,}\b"
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_record(value: Any) -> Any:
    """Redact fixture request evidence while preserving its useful structure."""

    if isinstance(value, list):
        return [_safe_record(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe_record(item) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    secret = os.environ.get("VILLANI_RELEASE_TEST_SECRET") or ""
    output = value.replace(secret, "[REDACTED]") if secret else value
    return SENSITIVE_TEXT_RE.sub("[REDACTED]", output)


def _message_text(messages: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            values.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    values.append(block["text"])
    return "\n".join(values)


def _scenario(messages: list[dict[str, Any]]) -> str:
    match = SCENARIO_RE.search(_message_text(messages))
    return match.group(1).lower() if match else "unspecified"


def _classification(scenario: str) -> dict[str, Any]:
    values = {
        "difficulty": "easy",
        "risk": "low",
        "category": "bug_fix",
        "estimated_attempts_needed": 1,
        "needs_tests": True,
        "required_capabilities": [],
        "reasoning_summary": f"deterministic classification for {scenario}",
        "confidence": 0.99,
    }
    if scenario in {"scenario_b", "coding_escalation"}:
        values.update(difficulty="medium", estimated_attempts_needed=2)
    if scenario in {"scenario_f", "verifier_cascade"}:
        values.update(difficulty="medium", risk="medium")
    if scenario in {"scenario_g", "classification_adjustment"}:
        values.update(difficulty="easy", risk="low")
    return values


def _tool_response(model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    tool_results = sum(message.get("role") == "tool" for message in messages)
    scenario = _scenario(messages)
    if model == "fixture-onboarding":
        if tool_results == 0:
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "write-onboarding-implementation",
                        "type": "function",
                        "function": {
                            "name": "Write",
                            "arguments": json.dumps(
                                {
                                    "file_path": "calculator.py",
                                    "content": (
                                        '"""Tiny disposable Villani setup sample."""\n\n'
                                        "\ndef add(left: int, right: int) -> int:\n"
                                        "    return left + right\n\n"
                                        "\ndef subtract(left: int, right: int) -> int:\n"
                                        "    return left - right\n"
                                    ),
                                }
                            ),
                        },
                    }
                ],
            }
        if tool_results == 1:
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "write-onboarding-test",
                        "type": "function",
                        "function": {
                            "name": "Write",
                            "arguments": json.dumps(
                                {
                                    "file_path": "test_calculator.py",
                                    "content": (
                                        "import unittest\n\n"
                                        "from calculator import add, subtract\n\n\n"
                                        "class CalculatorTests(unittest.TestCase):\n"
                                        "    def test_add(self):\n"
                                        "        self.assertEqual(add(2, 3), 5)\n\n"
                                        "    def test_subtract(self):\n"
                                        "        self.assertEqual(subtract(8, 3), 5)\n\n\n"
                                        "if __name__ == '__main__':\n"
                                        "    unittest.main()\n"
                                    ),
                                }
                            ),
                        },
                    }
                ],
            }
        if tool_results == 2:
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "validate-onboarding",
                        "type": "function",
                        "function": {
                            "name": "Bash",
                            "arguments": json.dumps({"command": "python -m unittest -q"}),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "Completed the disposable setup sample."}
    if tool_results == 0:
        correct = model != "fixture-standard" or scenario not in {
            "scenario_b",
            "coding_escalation",
        }
        content = (
            "def add(a, b):\n    return a + b\n"
            if correct
            else "def add(a, b):\n    return a - b\n"
        )
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"write-{model}-{scenario}",
                    "type": "function",
                    "function": {
                        "name": "Write",
                        "arguments": json.dumps(
                            {"file_path": "calculator.py", "content": content}
                        ),
                    },
                }
            ],
        }
    if tool_results == 1:
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"validate-{model}-{scenario}",
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "arguments": json.dumps({"command": "python -m unittest -q"}),
                    },
                }
            ],
        }
    return {"role": "assistant", "content": "Completed the deterministic candidate."}


def _verifier_response(model: str, messages: list[dict[str, Any]]) -> str:
    if model == "fixture-verifier-low":
        return "{ malformed verifier output"
    text = _message_text(messages)
    evidence = next(iter(EVIDENCE_RE.findall(text)), "ev-0001")
    verdict: dict[str, Any] = {
            "type": "final_verdict",
            "result": 1,
            "verdict": "success",
            "confidence": 0.97,
            "recommendedAction": "accept",
            "reason": "Authoritative validation evidence proves the requested behavior.",
            "criticalRequirement": "The repository validation command passes.",
            "directEvidenceForCriticalRequirement": evidence,
            "criticalRequirementCovered": True,
            "criticalRequirementEvidenceRefs": [evidence],
            "criticalRequirementEvidenceMatch": {
                evidence: {
                    "matchesCriticalRequirement": True,
                    "requirementCondition": "repository validation passes",
                    "evidenceCondition": "repository validation passed",
                    "whySameCondition": "the evidence executes the required validation",
                    "limitations": [],
                }
            },
            "deliverableAssessment": {
                "requiredDeliverables": ["working patch"],
                "validatedDeliverables": ["working patch"],
                "missingDeliverables": [],
                "weakValidationReasons": [],
            },
            "constraintAssessment": {
                "constraints": [],
                "satisfiedConstraints": [],
                "violatedConstraints": [],
                "uncheckedConstraints": [],
            },
            "requirementResults": [
                {
                    "id": "repository_validation",
                    "requirement": "repository validation passes",
                    "status": "satisfied",
                    "evidence": [evidence],
                    "risks": [],
                }
            ],
            "successEvidence": [evidence],
            "failureEvidence": [],
            "recoveredFailures": [],
            "missingEvidence": [],
            "riskFlags": [],
            "uncertainty": {"level": "low", "reasons": []},
            "toolsUsed": [],
        }
    context_match = re.search(
        r"Closed-loop verification context:\n(\{.*?\})\nEvidence packet:",
        text,
        re.DOTALL,
    )
    context = json.loads(context_match.group(1)) if context_match else {}
    if model == "fixture-onboarding":
        observable = [
            str(item["requirement_id"])
            for item in context.get("requirements", [])
            if item.get("observable")
            and item.get("source") != "repository_validation_command"
        ]
        if observable:
            verdict["focusedProbeRequests"] = [
                {
                    "probe_id": "redundant-covered-behavior",
                    "requirement_ids": observable,
                    "argv": [
                        sys.executable,
                        "-c",
                        "raise SystemExit(91)",
                    ],
                    "timeout_seconds": 30,
                    "expected_exit_code": 0,
                    "expected_stdout": None,
                    "expected_stdout_contains": [],
                    "expected_stderr_contains": [],
                    "reason": (
                        "Fixture request proving authoritative coverage suppresses "
                        "a redundant focused probe."
                    ),
                }
            ]
    elif _scenario(messages) == "scenario_d":
        security_requirements = [
            str(item["requirement_id"])
            for item in context.get("requirements", [])
            if item.get("source") != "repository_validation_command"
            and any(
                term in str(item.get("description") or "").casefold()
                for term in (
                    "authentication",
                    "disclos",
                    "preserv",
                    "secret",
                    "unsafe",
                )
            )
        ]
        if security_requirements:
            verdict["focusedProbeRequests"] = [
                {
                    "probe_id": "preserved-authentication-fixture",
                    "requirement_ids": security_requirements,
                    "argv": [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; import subprocess; "
                            "text=Path('auth_fixture.py').read_text(encoding='utf-8'); "
                            "diff=subprocess.run(['git','diff','--','auth_fixture.py'],"
                            "capture_output=True,text=True,encoding='utf-8',"
                            "errors='replace',check=False); "
                            "ok=(diff.returncode == 0 and not diff.stdout and "
                            "\"token = 'test-token'\" in text and "
                            "'numeric_token_metric = 42' in text); "
                            "raise SystemExit(0 if ok else 1)"
                        ),
                    ],
                    "timeout_seconds": 30,
                    "expected_exit_code": 0,
                    "expected_stdout": None,
                    "expected_stdout_contains": [],
                    "expected_stderr_contains": [],
                    "reason": (
                        "The security and preservation requirements need a "
                        "candidate-local proof that the authentication fixture "
                        "is unchanged and still contains its harmless metrics."
                    ),
                }
            ]
    return json.dumps(verdict, sort_keys=True)


class FixtureState:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.lock = threading.Lock()

    def record(self, document: dict[str, Any]) -> None:
        line = (
            json.dumps(_safe_record(document), sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        with self.lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)


def make_handler(state: FixtureState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") == "/v1/models":
                body = json.dumps(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": model,
                                "object": "model",
                                "owned_by": "villani-release-fixture",
                                "context_window": 32_768,
                            }
                            for model in sorted(MODEL_IDENTITIES)
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.rstrip("/") == "/health":
                body = json.dumps(
                    {"status": "ok", "models": sorted(MODEL_IDENTITIES)}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self.send_error(404)
                return
            received_at = _timestamp()
            length = int(self.headers.get("content-length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
            model = str(request.get("model") or "")
            messages = request.get("messages")
            messages = messages if isinstance(messages, list) else []
            scenario = _scenario(messages)
            if model not in MODEL_IDENTITIES:
                self.send_error(400, f"unknown fixture model {model}")
                return
            tools = request.get("tools") or []
            tool_names = {
                str(tool.get("function", {}).get("name") or "")
                for tool in tools
                if isinstance(tool, dict)
            }
            message_text = _message_text(messages)
            if model == "fixture-classifier" or (
                model == "fixture-onboarding" and "Classify this task" in message_text
            ):
                message = {
                    "role": "assistant",
                    "content": json.dumps(_classification(scenario), sort_keys=True),
                }
            elif model.startswith("fixture-verifier-") or (
                model == "fixture-onboarding"
                and "verifier_final_verdict" in tool_names
            ):
                message = {
                    "role": "assistant",
                    "content": _verifier_response(model, messages),
                }
            elif tools:
                message = _tool_response(model, messages)
            else:
                message = {"role": "assistant", "content": "Fixture model ready."}
            response = {
                "id": f"fixture-{model}-{scenario}",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            }
            completed_at = _timestamp()
            state.record(
                {
                    "scenario_id": scenario,
                    "request_model": model,
                    "request_messages": messages,
                    "generation_parameters": {
                        key: request.get(key)
                        for key in ("temperature", "max_tokens", "seed", "top_p")
                        if key in request
                    },
                    "response": response,
                    "token_accounting": response["usage"],
                    "received_at": received_at,
                    "completed_at": completed_at,
                }
            )
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--endpoint-file", type=Path, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(FixtureState(args.log))
    )
    args.endpoint_file.parent.mkdir(parents=True, exist_ok=True)
    args.endpoint_file.write_text(
        json.dumps({"base_url": f"http://{args.host}:{server.server_port}/v1"}),
        encoding="utf-8",
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
