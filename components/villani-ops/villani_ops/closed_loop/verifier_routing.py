"""Independent, fail-closed verifier routing for the canonical closed loop."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .interfaces import AttemptContext, AttemptResult, EvidenceItem, Verification, Verifier


class VerifierPolicyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str = Field(min_length=1)
    model: str | None = None
    capability_score: float = Field(ge=0, le=100)
    price_per_call_usd: float | None = Field(default=None, ge=0)
    expected_latency_ms: int | None = Field(default=None, ge=0)
    authority: Literal["advisory", "acceptance"] = "acceptance"
    available: bool = True


class VerifierRoutingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = "verifier-routing-v1"
    low_risk_minimum_capability: float = Field(default=20, ge=0, le=100)
    medium_risk_minimum_capability: float = Field(default=50, ge=0, le=100)
    high_risk_minimum_capability: float = Field(default=80, ge=0, le=100)
    large_patch_lines: int = Field(default=500, ge=1)
    many_changed_files: int = Field(default=12, ge=1)
    sensitive_file_capability_floor: float = Field(default=80, ge=0, le=100)
    minimum_authority: Literal["advisory", "acceptance"] = "acceptance"
    escalate_on_unclear: bool = True
    escalate_on_malformed: bool = True
    escalate_on_timeout: bool = True
    escalate_on_disagreement: bool = True


class VerifierRoutingContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    risk: Literal["low", "medium", "high"] = "high"
    difficulty: Literal["easy", "medium", "hard"] = "hard"
    authoritative_validation_passed: bool = False
    active_validation_failure: bool = False
    missing_evidence: bool = False
    patch_lines: int = Field(default=0, ge=0)
    changed_file_count: int = Field(default=0, ge=0)
    sensitive_file_change: bool = False
    previous_outcome: str | None = None
    previous_confidence: float | None = Field(default=None, ge=0, le=1)
    malformed_output: bool = False
    timeout: bool = False
    disagreement: bool = False


@dataclass(frozen=True, slots=True)
class VerifierRoute:
    entry: VerifierPolicyEntry
    verifier: Verifier


def required_capability(
    policy: VerifierRoutingPolicy, context: VerifierRoutingContext
) -> tuple[float, tuple[str, ...]]:
    reasons: list[str] = []
    minimum = {
        "low": policy.low_risk_minimum_capability,
        "medium": policy.medium_risk_minimum_capability,
        "high": policy.high_risk_minimum_capability,
    }[context.risk]
    reasons.append(f"risk_{context.risk}")
    if context.difficulty == "hard":
        minimum = max(minimum, policy.high_risk_minimum_capability)
        reasons.append("hard_difficulty")
    elif context.difficulty == "medium":
        minimum = max(minimum, policy.medium_risk_minimum_capability)
        reasons.append("medium_difficulty")
    if context.authoritative_validation_passed:
        reasons.append("authoritative_repository_validation_passed")
    if context.active_validation_failure:
        minimum = 101
        reasons.append("active_validation_failure_blocks_acceptance")
    if context.missing_evidence:
        minimum = max(minimum, policy.medium_risk_minimum_capability)
        reasons.append("missing_evidence")
    if context.patch_lines >= policy.large_patch_lines:
        minimum = max(minimum, policy.high_risk_minimum_capability)
        reasons.append("large_patch")
    if context.changed_file_count >= policy.many_changed_files:
        minimum = max(minimum, policy.high_risk_minimum_capability)
        reasons.append("many_changed_files")
    if context.sensitive_file_change:
        minimum = max(minimum, policy.sensitive_file_capability_floor)
        reasons.append("sensitive_file_change")
    return minimum, tuple(reasons)


def select_routes(
    policy: VerifierRoutingPolicy,
    context: VerifierRoutingContext,
    routes: Sequence[VerifierRoute],
) -> tuple[tuple[VerifierRoute, ...], float, tuple[str, ...]]:
    """Return the cheapest eligible route followed by stronger fallbacks."""

    minimum, reasons = required_capability(policy, context)
    authority_rank = {"advisory": 0, "acceptance": 1}
    eligible = [
        route
        for route in routes
        if route.entry.available
        and route.entry.capability_score >= minimum
        and authority_rank[route.entry.authority]
        >= authority_rank[policy.minimum_authority]
    ]
    eligible.sort(
        key=lambda route: (
            route.entry.price_per_call_usd is None,
            route.entry.price_per_call_usd
            if route.entry.price_per_call_usd is not None
            else float("inf"),
            route.entry.capability_score,
            route.entry.expected_latency_ms
            if route.entry.expected_latency_ms is not None
            else 2**31,
            route.entry.backend,
        )
    )
    return tuple(eligible), minimum, reasons


def _routing_context(context: AttemptContext, result: AttemptResult) -> VerifierRoutingContext:
    raw = context.classification
    risk = str(raw.get("risk") or "high")
    difficulty = str(raw.get("difficulty") or "hard")
    changed = result.metadata.get("changed_files")
    changed_files = [str(item) for item in changed] if isinstance(changed, list) else []
    sensitive = any(
        name.lower().endswith(
            (".pem", ".key", ".p12", ".pfx", "security.py", "auth.py", "permissions.py")
        )
        for name in changed_files
    )
    repository_validations = [
        event
        for event in result.runtime_events
        if event.payload.get("command_role") == "repository_validation"
    ]
    validation_failed = any(
        event.event_type == "command_failed"
        or event.payload.get("exit_code") not in {None, 0}
        for event in repository_validations
    )
    validation_passed = bool(repository_validations) and not validation_failed and any(
        event.payload.get("exit_code") == 0 for event in repository_validations
    )
    return VerifierRoutingContext(
        risk=risk if risk in {"low", "medium", "high"} else "high",  # type: ignore[arg-type]
        difficulty=(
            difficulty if difficulty in {"easy", "medium", "hard"} else "hard"
        ),  # type: ignore[arg-type]
        authoritative_validation_passed=validation_passed,
        active_validation_failure=validation_failed,
        missing_evidence=not validation_passed,
        patch_lines=len((result.patch or "").splitlines()),
        changed_file_count=len(set(changed_files)),
        sensitive_file_change=sensitive,
    )


def _failure_verification(reason: str, metadata: Mapping[str, Any]) -> Verification:
    return Verification(
        verifier="verifier_router",
        outcome="error",
        acceptance_eligible=False,
        confidence=None,
        reason=reason,
        recommended_action="fail",
        missing_evidence=(
            EvidenceItem(
                evidence_id="verifier_authority_unavailable",
                kind="verifier_routing",
                summary=reason,
            ),
        ),
        risk_flags=("acceptance_blocker:no_eligible_verifier_authority",),
        metadata=dict(metadata),
    )


class VerifierCascade:
    """Invoke the cheapest eligible verifier and escalate on unsafe outcomes."""

    def __init__(
        self,
        routes: Sequence[VerifierRoute],
        policy: VerifierRoutingPolicy | None = None,
    ) -> None:
        self.routes = tuple(routes)
        self.policy = policy or VerifierRoutingPolicy()

    @staticmethod
    def _escalation_reason(policy: VerifierRoutingPolicy, result: Verification) -> str | None:
        status = str(result.metadata.get("invocation_status") or "")
        if policy.escalate_on_malformed and status == "malformed_output":
            return "malformed_output"
        if policy.escalate_on_timeout and status == "timeout":
            return "timeout"
        if result.outcome == "error":
            return "verifier_error"
        if policy.escalate_on_unclear and result.outcome == "unclear":
            return "ambiguous_outcome"
        if result.recommended_action in {"retry_verifier", "escalate"}:
            return result.recommended_action
        return None

    def verify(self, context: AttemptContext, result: AttemptResult) -> Verification:
        facts = _routing_context(context, result)
        routes, minimum, reasons = select_routes(self.policy, facts, self.routes)
        base_metadata = {
            "verifier_policy_version": self.policy.version,
            "minimum_capability": minimum,
            "selection_reasons": list(reasons),
        }
        if facts.active_validation_failure:
            return _failure_verification(
                "Failed authoritative repository validation blocks acceptance.",
                {**base_metadata, "active_validation_failure": True},
            )
        if not routes:
            return _failure_verification(
                "No available verifier meets the configured acceptance authority.",
                base_metadata,
            )

        calls: list[dict[str, Any]] = []
        usage: list[Mapping[str, Any]] = []
        previous: Verification | None = None
        disagreement = False
        awaiting_disagreement_resolution = False
        disagreement_resolution: str | None = None
        for retry_number, route in enumerate(routes):
            is_disagreement_resolver = awaiting_disagreement_resolution
            awaiting_disagreement_resolution = False
            started = time.monotonic()
            try:
                returned = route.verifier.verify(context, result)
                if not isinstance(returned, Verification):
                    raise TypeError("verifier returned an invalid Verification")
            except Exception as error:
                returned = _failure_verification(
                    "Verifier invocation failed closed.",
                    {
                        "invocation_status": "error",
                        "exception_class": type(error).__name__,
                    },
                )
            duration_ms = max(int((time.monotonic() - started) * 1000), 0)
            usage.extend(returned.llm_usage)
            new_disagreement = bool(
                previous is not None
                and previous.outcome in {"accepted", "rejected"}
                and returned.outcome in {"accepted", "rejected"}
                and previous.outcome != returned.outcome
            )
            disagreement = disagreement or new_disagreement
            escalation = self._escalation_reason(self.policy, returned)
            call_usage = list(returned.llm_usage)
            calls.append(
                {
                    "backend": route.entry.backend,
                    "model": route.entry.model,
                    "capability": route.entry.capability_score,
                    "selection_reason": (
                        "cheapest_eligible"
                        if retry_number == 0
                        else "stronger_after_"
                        + str(calls[-1].get("escalation_reason"))
                    ),
                    "authority": route.entry.authority,
                    "input_tokens": sum(int(item.get("input_tokens") or 0) for item in call_usage),
                    "output_tokens": sum(int(item.get("output_tokens") or 0) for item in call_usage),
                    "total_tokens": sum(int(item.get("total_tokens") or 0) for item in call_usage),
                    "cost_usd": sum(float(item.get("cost") or 0) for item in call_usage),
                    "duration_ms": duration_ms,
                    "outcome": returned.outcome,
                    "confidence": returned.confidence,
                    "retry_number": retry_number,
                    "escalation_reason": escalation,
                    "invocation_status": returned.metadata.get("invocation_status"),
                    "malformed_output": status_is(returned, "malformed_output"),
                    "timeout": status_is(returned, "timeout"),
                }
            )
            previous = returned
            if escalation is None:
                if (
                    new_disagreement
                    and self.policy.escalate_on_disagreement
                    and retry_number + 1 < len(routes)
                ):
                    calls[-1]["escalation_reason"] = "verifier_disagreement"
                    awaiting_disagreement_resolution = True
                    continue
                if new_disagreement and self.policy.escalate_on_disagreement:
                    return replace(
                        returned,
                        outcome="unclear",
                        acceptance_eligible=False,
                        confidence=None,
                        reason="Verifier disagreement remained unresolved.",
                        recommended_action="fail",
                        risk_flags=tuple(returned.risk_flags)
                        + ("acceptance_blocker:verifier_disagreement",),
                        metadata={
                            **dict(returned.metadata),
                            **base_metadata,
                            "verifier_calls": calls,
                            "verifier_disagreement": True,
                            "verifier_disagreement_resolution": "unresolved",
                            "verifier_route_complete": False,
                        },
                        llm_usage=tuple(usage),
                    )
                if is_disagreement_resolver:
                    disagreement_resolution = "stronger_verifier"
                return replace(
                    returned,
                    verifier=route.entry.backend,
                    metadata={
                        **dict(returned.metadata),
                        **base_metadata,
                        "verifier_calls": calls,
                        "verifier_disagreement": disagreement,
                        "verifier_disagreement_resolution": disagreement_resolution,
                        "verifier_route_complete": True,
                    },
                    llm_usage=tuple(usage),
                )
        assert previous is not None
        return replace(
            previous,
            acceptance_eligible=False,
            metadata={
                **dict(previous.metadata),
                **base_metadata,
                "verifier_calls": calls,
                "verifier_disagreement": disagreement,
                "verifier_disagreement_resolution": (
                    "unresolved" if disagreement else None
                ),
                "verifier_route_complete": False,
            },
            llm_usage=tuple(usage),
        )


def status_is(result: Verification, value: str) -> bool:
    return result.metadata.get("invocation_status") == value
