"""Deterministic built-in verification graph executor."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .models import (
    EvidenceOutput,
    GradedEvidence,
    NodeExecutionAccounting,
    VerificationGraph,
    VerificationGraphResult,
    VerificationNode,
    VerificationNodeResult,
)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class VerificationGraphExecutor:
    def __init__(
        self,
        *,
        dependency_scan: Callable[[VerificationNode, Path], Mapping[str, Any]]
        | None = None,
        llm_review: Callable[[VerificationNode, Mapping[str, Any]], Mapping[str, Any]]
        | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.dependency_scan = dependency_scan
        self.llm_review = llm_review
        self.now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _condition(
        node: VerificationNode, configuration: Mapping[str, Any], patch: str
    ) -> bool:
        kind = str(node.condition.get("type") or "always")
        if kind == "always":
            return True
        if kind == "has_patch":
            return bool(patch.strip())
        if kind == "configuration_flag":
            key = str(node.condition.get("key") or "")
            return configuration.get(key) == node.condition.get("equals", True)
        raise ValueError(f"unsupported verification condition: {kind}")

    @staticmethod
    def _evidence(
        node: VerificationNode,
        output: EvidenceOutput,
        *,
        passed: bool | None,
        summary: str,
        grade: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> GradedEvidence:
        actual_grade = grade or output.grade
        if node.kind == "independent_llm_review" and actual_grade == "authoritative":
            actual_grade = "strong"
        payload = {
            "node_id": node.node_id,
            "evidence_id": output.evidence_id,
            "grade": actual_grade,
            "passed": passed,
            "summary": summary,
            "details": dict(details or {}),
        }
        return GradedEvidence(
            **payload,
            digest_sha256=_digest(payload),
        )

    def _command(
        self, node: VerificationNode, repository: Path
    ) -> VerificationNodeResult:
        argv = node.configuration.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            raise ValueError(f"command node {node.node_id} requires shell-free argv")
        outcomes: list[bool] = []
        output = bytearray()
        duration_ms = 0
        truncated = False
        for _ in range(node.resource_limits.maximum_reruns + 1):
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    argv,
                    cwd=repository,
                    shell=False,
                    capture_output=True,
                    timeout=node.resource_limits.timeout_seconds,
                    check=False,
                )
                passed = completed.returncode == 0
                raw = bytes(completed.stdout or b"") + bytes(completed.stderr or b"")
            except subprocess.TimeoutExpired as error:
                passed = False
                raw = (
                    bytes(error.stdout or b"")
                    + bytes(error.stderr or b"")
                    + b"\ntimeout"
                )
            duration_ms += max(int((time.monotonic() - started) * 1000), 0)
            outcomes.append(passed)
            remaining = max(0, node.resource_limits.maximum_output_bytes - len(output))
            output.extend(raw[:remaining])
            truncated = truncated or len(raw) > remaining
        flaky = len(set(outcomes)) > 1
        passed = all(outcomes) and not flaky
        status = "conflicting" if flaky else "passed" if passed else "failed"
        grade = "conflicting" if flaky else None
        summary = output.decode("utf-8", errors="replace")[-2000:] or status
        evidence = tuple(
            self._evidence(node, item, passed=passed, summary=summary, grade=grade)
            for item in node.evidence_outputs
        )
        return VerificationNodeResult(
            node_id=node.node_id,
            kind=node.kind,
            required=node.required,
            status=status,
            reason=(
                "command outcomes disagreed across bounded reruns"
                if flaky
                else f"command {status}"
            ),
            evidence=evidence,
            flaky=flaky,
            accounting=NodeExecutionAccounting(
                executions=len(outcomes),
                reruns=max(len(outcomes) - 1, 0),
                duration_ms=duration_ms,
                output_bytes=len(output),
                output_truncated=truncated,
            ),
        )

    def _patch_hygiene(
        self, node: VerificationNode, patch: str
    ) -> VerificationNodeResult:
        changed = re.findall(r"^\+\+\+ b/(.+)$", patch, flags=re.MULTILINE)
        denied = tuple(
            str(item)
            for item in node.configuration.get("denied_paths", [".git/", ".villani/"])
        )
        allowed = tuple(
            str(item) for item in node.configuration.get("allowed_paths", [])
        )
        violations = [
            path for path in changed if any(path.startswith(item) for item in denied)
        ]
        if allowed:
            violations.extend(
                path
                for path in changed
                if not any(path.startswith(item) for item in allowed)
            )
        passed = bool(patch.strip()) and not violations
        return self._simple(
            node,
            passed,
            "patch scope is clean"
            if passed
            else f"patch scope violations: {sorted(set(violations))}",
            {"changed_files": changed, "violations": sorted(set(violations))},
        )

    def _secret_scan(
        self, node: VerificationNode, patch: str
    ) -> VerificationNodeResult:
        patterns = [
            r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}",
            r"sk-[A-Za-z0-9_-]{16,}",
        ]
        findings = [
            match.group(0)[:24]
            for pattern in patterns
            for match in re.finditer(pattern, patch)
        ]
        return self._simple(
            node,
            not findings,
            "secret scan passed"
            if not findings
            else "potential secret material detected",
            {"finding_count": len(findings)},
        )

    def _trace(
        self, node: VerificationNode, context: Mapping[str, Any], patch: str
    ) -> VerificationNodeResult:
        trace = context.get("trace")
        trace_values = trace if isinstance(trace, Mapping) else {}
        expected_patch = hashlib.sha256(patch.encode()).hexdigest()
        conflicts: list[str] = []
        for key in ("run_id", "attempt_id"):
            if trace_values.get(key) not in {None, context.get(key)}:
                conflicts.append(key)
        if trace_values.get("patch_sha256") not in {None, expected_patch}:
            conflicts.append("patch_sha256")
        return self._simple(
            node,
            not conflicts,
            "trace identities and patch digest are consistent"
            if not conflicts
            else f"trace conflicts: {conflicts}",
            {"conflicts": conflicts, "patch_sha256": expected_patch},
            grade="conflicting" if conflicts else None,
        )

    def _simple(
        self,
        node: VerificationNode,
        passed: bool,
        summary: str,
        details: Mapping[str, Any],
        grade: str | None = None,
    ) -> VerificationNodeResult:
        evidence = tuple(
            self._evidence(
                node, item, passed=passed, summary=summary, details=details, grade=grade
            )
            for item in node.evidence_outputs
        )
        return VerificationNodeResult(
            node_id=node.node_id,
            kind=node.kind,
            required=node.required,
            status="passed"
            if passed
            else "conflicting"
            if grade == "conflicting"
            else "failed",
            reason=summary,
            evidence=evidence,
            accounting=NodeExecutionAccounting(
                executions=1, reruns=0, duration_ms=0, output_bytes=0
            ),
        )

    def _execute_node(
        self,
        node: VerificationNode,
        repository: Path,
        patch: str,
        context: Mapping[str, Any],
    ) -> VerificationNodeResult:
        if node.kind in {
            "repository_command",
            "targeted_test_command",
            "static_type_lint_command",
        }:
            return self._command(node, repository)
        if node.kind == "patch_hygiene_scope":
            return self._patch_hygiene(node, patch)
        if node.kind == "secret_scan":
            return self._secret_scan(node, patch)
        if node.kind == "trace_consistency":
            return self._trace(node, context, patch)
        if node.kind == "dependency_security_scan":
            if self.dependency_scan is None:
                return self._simple(
                    node,
                    False,
                    "dependency/security scan adapter unavailable",
                    {},
                    grade="missing",
                )
            value = dict(self.dependency_scan(node, repository))
            return self._simple(
                node,
                bool(value.get("passed")),
                str(value.get("summary") or "dependency/security scan completed"),
                value,
            )
        if self.llm_review is None:
            return self._simple(
                node, False, "independent LLM review unavailable", {}, grade="missing"
            )
        value = dict(self.llm_review(node, context))
        grade = str(value.get("grade") or "weak")
        if grade == "authoritative":
            grade = "strong"
        return self._simple(
            node,
            bool(value.get("passed")),
            str(value.get("summary") or "LLM review completed"),
            value,
            grade=grade,
        )

    def execute(
        self,
        graph: VerificationGraph,
        *,
        run_id: str,
        attempt_id: str,
        repository: Path,
        patch: str,
        configuration: Mapping[str, Any],
        trace: Mapping[str, Any] | None = None,
    ) -> VerificationGraphResult:
        results: dict[str, VerificationNodeResult] = {}
        context = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "trace": dict(trace or {}),
            "configuration": dict(configuration),
        }
        pending = list(graph.nodes)
        while pending:
            progressed = False
            for node in list(pending):
                if not set(node.dependencies) <= set(results):
                    continue
                progressed = True
                pending.remove(node)
                dependencies_ok = all(
                    results[item].status == "passed" for item in node.dependencies
                )
                if not dependencies_ok or not self._condition(
                    node, configuration, patch
                ):
                    evidence = tuple(
                        self._evidence(
                            node,
                            item,
                            passed=None,
                            summary="node skipped",
                            grade="missing",
                        )
                        for item in node.evidence_outputs
                    )
                    results[node.node_id] = VerificationNodeResult(
                        node_id=node.node_id,
                        kind=node.kind,
                        required=node.required,
                        status="skipped",
                        reason="dependency or condition was not satisfied",
                        evidence=evidence,
                        accounting=NodeExecutionAccounting(
                            executions=0, reruns=0, duration_ms=0, output_bytes=0
                        ),
                    )
                    continue
                try:
                    results[node.node_id] = self._execute_node(
                        node, repository, patch, context
                    )
                except Exception as error:
                    evidence = tuple(
                        self._evidence(
                            node,
                            item,
                            passed=False,
                            summary=str(error),
                            grade="missing",
                        )
                        for item in node.evidence_outputs
                    )
                    results[node.node_id] = VerificationNodeResult(
                        node_id=node.node_id,
                        kind=node.kind,
                        required=node.required,
                        status="error",
                        reason=str(error),
                        evidence=evidence,
                        accounting=NodeExecutionAccounting(
                            executions=1, reruns=0, duration_ms=0, output_bytes=0
                        ),
                    )
            if not progressed:  # graph validation should make this unreachable
                raise RuntimeError("verification graph could not make progress")
        ordered = tuple(results[item.node_id] for item in graph.nodes)
        required_failures = tuple(
            item.node_id
            for item in ordered
            if item.required and item.status in {"failed", "error", "conflicting"}
        )
        missing_required = tuple(
            item.node_id
            for item in ordered
            if item.required
            and (
                item.status == "skipped"
                or any(e.grade == "missing" for e in item.evidence)
            )
        )
        conflicting = tuple(
            item.node_id
            for item in ordered
            if item.status == "conflicting"
            or any(e.grade == "conflicting" for e in item.evidence)
        )
        authoritative = any(
            item.required
            and item.kind != "independent_llm_review"
            and item.status == "passed"
            and any(e.grade == "authoritative" and e.passed for e in item.evidence)
            for item in ordered
        )
        llm_pass = any(
            item.kind == "independent_llm_review" and item.status == "passed"
            for item in ordered
        )
        llm_fail = any(
            item.kind == "independent_llm_review"
            and item.status in {"failed", "error", "conflicting"}
            for item in ordered
        )
        deterministic_pass = any(
            item.kind != "independent_llm_review" and item.status == "passed"
            for item in ordered
        )
        deterministic_fail = any(
            item.kind != "independent_llm_review"
            and item.status in {"failed", "error", "conflicting"}
            for item in ordered
        )
        disagreement = (
            (llm_pass and deterministic_fail)
            or (llm_fail and deterministic_pass)
            or bool(conflicting)
        )
        eligible = (
            not required_failures
            and not missing_required
            and not conflicting
            and authoritative
        )
        return VerificationGraphResult(
            graph_id=graph.graph_id,
            graph_version=graph.version,
            run_id=run_id,
            attempt_id=attempt_id,
            completed_at=self.now(),
            node_results=ordered,
            acceptance_eligible=eligible,
            authoritative_acceptance_present=authoritative,
            required_failures=required_failures,
            missing_required_evidence=missing_required,
            conflicting_evidence=conflicting,
            verifier_disagreement=disagreement,
            flaky_nodes=tuple(item.node_id for item in ordered if item.flaky),
            total_executions=sum(item.accounting.executions for item in ordered),
            total_reruns=sum(item.accounting.reruns for item in ordered),
        )
