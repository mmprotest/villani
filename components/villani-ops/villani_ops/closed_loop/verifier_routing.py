"""Independent, fail-closed verifier routing for the canonical closed loop."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .interfaces import (
    AttemptContext,
    AttemptResult,
    EvidenceItem,
    Verification,
    Verifier,
)


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
    sensitive_paths: list[str] = Field(default_factory=list)
    minimum_authority: Literal["advisory", "acceptance"] = "acceptance"
    escalate_on_unclear: bool = True
    escalate_on_malformed: bool = True
    escalate_on_timeout: bool = True
    escalate_on_disagreement: bool = True

    @property
    def normalized_sensitive_paths(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.sensitive_paths)))


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


def _routing_context(
    context: AttemptContext,
    result: AttemptResult,
    policy: VerifierRoutingPolicy | None = None,
) -> VerifierRoutingContext:
    raw = context.classification
    risk = str(raw.get("risk") or "high")
    difficulty = str(raw.get("difficulty") or "hard")
    changed = result.metadata.get("changed_files")
    changed_files = [str(item) for item in changed] if isinstance(changed, list) else []
    configured_patterns = (policy or VerifierRoutingPolicy()).normalized_sensitive_paths
    sensitive = any(
        fnmatchcase(
            name.replace("\\", "/").lstrip("./").casefold(),
            pattern.replace("\\", "/").casefold(),
        )
        for name in changed_files
        for pattern in configured_patterns
    )
    declared_status = str(result.metadata.get("repository_validation_status") or "")
    declared_authoritative = bool(
        result.metadata.get("repository_validation_authoritative")
    )
    if declared_status in {
        "passed",
        "failed",
        "unavailable",
        "infrastructure_error",
    }:
        validation_passed = declared_status == "passed" and declared_authoritative
        validation_failed = declared_status == "failed" and declared_authoritative
    else:
        expected_worktree = str(Path(result.worktree_path).resolve())

        def legacy_event_is_structured(event: Any) -> bool:
            if event.event_type not in {"command_completed", "command_failed"}:
                return False
            payload = event.payload
            required = {
                "run_id",
                "attempt_id",
                "worktree_path",
                "baseline_sha256",
                "candidate_state",
                "exit_code",
            }
            return bool(
                required.issubset(payload)
                and payload.get("run_id") == context.run_id
                and payload.get("attempt_id") == context.attempt_id
                and str(Path(str(payload.get("worktree_path"))).resolve())
                == expected_worktree
                and context.baseline_sha256
                and payload.get("baseline_sha256") == context.baseline_sha256
                and payload.get("candidate_state") == "post_mutation"
                and payload.get("command_role") == "repository_validation"
            )

        legacy_events = [
            event
            for event in result.runtime_events
            if (
                event.event_type
                in {
                    "repository_validation_completed",
                    "repository_validation_failed",
                    "repository_validation_infrastructure_error",
                }
                and event.payload.get("command_role") == "repository_validation"
            )
            or legacy_event_is_structured(event)
        ]
        validation_failed = any(
            event.event_type
            in {
                "repository_validation_failed",
                "command_failed",
            }
            for event in legacy_events
        )
        validation_passed = (
            bool(legacy_events)
            and not validation_failed
            and all(
                event.event_type
                in {"repository_validation_completed", "command_completed"}
                for event in legacy_events
            )
        )
    return VerifierRoutingContext(
        risk=risk if risk in {"low", "medium", "high"} else "high",  # type: ignore[arg-type]
        difficulty=(difficulty if difficulty in {"easy", "medium", "hard"} else "hard"),  # type: ignore[arg-type]
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


def _deterministic_rejection(
    reason: str, metadata: Mapping[str, Any]
) -> Verification:
    return Verification(
        verifier="verifier_router",
        outcome="rejected",
        acceptance_eligible=False,
        confidence=1.0,
        reason=reason,
        recommended_action="reject",
        failure_evidence=(
            EvidenceItem(
                evidence_id="deterministic_verification_failure",
                kind="deterministic_verification",
                summary=reason,
            ),
        ),
        risk_flags=("acceptance_blocker:repository_validation_failed",),
        metadata={
            **dict(metadata),
            "semantic_verifier_invoked": False,
            "verifier_calls": [],
            "verification_cost": None,
            "verification_cost_accounting_status": "not_applicable",
            "redundant_semantic_call_avoided": True,
        },
        llm_usage=(),
    )


def _usage_total(
    usage: Sequence[Mapping[str, Any]],
    *,
    value_key: str,
    status_key: str,
) -> tuple[int | float | None, str]:
    if not usage:
        return None, "not_applicable"
    known: list[int | float] = []
    unknown = False
    applicable = False
    for item in usage:
        status = str(item.get(status_key) or "unknown")
        value = item.get(value_key)
        if status == "not_applicable":
            continue
        applicable = True
        if status in {"complete", "partial"} and isinstance(value, (int, float)):
            known.append(value)
            if status == "partial":
                unknown = True
        else:
            unknown = True
    if not applicable:
        return None, "not_applicable"
    if known:
        return sum(known), "partial" if unknown else "complete"
    return None, "unknown"


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
    def _escalation_reason(
        policy: VerifierRoutingPolicy, result: Verification
    ) -> str | None:
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
        facts = _routing_context(context, result, self.policy)
        routes, minimum, reasons = select_routes(self.policy, facts, self.routes)
        base_metadata = {
            "verifier_policy_version": self.policy.version,
            "minimum_capability": minimum,
            "selection_reasons": list(reasons),
        }
        if facts.active_validation_failure:
            return _deterministic_rejection(
                "Authoritative repository validation already failed; semantic verification was not invoked.",
                base_metadata,
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
        independent_primary: Verification | None = None
        independent_primary_route: tuple[str, str | None] | None = None
        cascade_context = replace(
            context,
            policy_configuration={
                **dict(context.policy_configuration),
                "_adaptive_verification_cascade_active": True,
            },
        )
        for retry_number, route in enumerate(routes):
            is_disagreement_resolver = awaiting_disagreement_resolution
            awaiting_disagreement_resolution = False
            started = time.monotonic()
            try:
                returned = route.verifier.verify(cascade_context, result)
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
            if facts.active_validation_failure and returned.acceptance_eligible:
                returned = replace(
                    returned,
                    outcome="rejected",
                    acceptance_eligible=False,
                    reason=(
                        "Authoritative repository validation failed on the candidate."
                    ),
                    recommended_action="reject",
                    risk_flags=tuple(returned.risk_flags)
                    + ("acceptance_blocker:repository_validation_failed",),
                    metadata={
                        **dict(returned.metadata),
                        "raw_semantic_acceptance_eligible": True,
                        "computed_final_result": 0,
                        "computed_final_reason_code": ("repository_validation_failed"),
                        "verifier_disagreement": True,
                    },
                )
            duration_ms = max(int((time.monotonic() - started) * 1000), 0)
            usage.extend(returned.llm_usage)
            if independent_primary is not None:
                current_identity = (route.entry.backend, route.entry.model)
                raw_agreement = bool(
                    returned.metadata.get("pre_adaptive_acceptance_eligible")
                    or returned.acceptance_eligible
                )
                if current_identity == independent_primary_route:
                    returned = replace(
                        returned,
                        outcome="error",
                        acceptance_eligible=False,
                        reason="Independent verification requires a distinct verifier identity.",
                        recommended_action="fail",
                        risk_flags=tuple(returned.risk_flags)
                        + ("acceptance_blocker:independent_verifier_not_distinct",),
                    )
                elif raw_agreement:
                    finalize_independent = getattr(
                        route.verifier, "finalize_independent_verification", None
                    )
                    if callable(finalize_independent):
                        returned = finalize_independent(
                            cascade_context,
                            result,
                            replace(
                                returned,
                                metadata={
                                    **dict(returned.metadata),
                                    "independent_verifier_completed": True,
                                    "independent_primary_verifier": {
                                        "backend": independent_primary_route[0]
                                        if independent_primary_route
                                        else None,
                                        "model": independent_primary_route[1]
                                        if independent_primary_route
                                        else None,
                                    },
                                },
                            ),
                        )
                    else:
                        returned = replace(
                            returned,
                            outcome="error",
                            acceptance_eligible=False,
                            reason=(
                                "The verifier route cannot persist independent "
                                "acceptance evidence."
                            ),
                            recommended_action="fail",
                            risk_flags=tuple(returned.risk_flags)
                            + (
                                "acceptance_blocker:independent_evidence_unavailable",
                            ),
                        )
            new_disagreement = bool(
                previous is not None
                and previous.outcome in {"accepted", "rejected"}
                and returned.outcome in {"accepted", "rejected"}
                and previous.outcome != returned.outcome
            )
            disagreement = disagreement or new_disagreement
            escalation = self._escalation_reason(self.policy, returned)
            call_usage = list(returned.llm_usage)
            input_tokens, input_status = _usage_total(
                call_usage,
                value_key="input_tokens",
                status_key="token_accounting_status",
            )
            output_tokens, output_status = _usage_total(
                call_usage,
                value_key="output_tokens",
                status_key="token_accounting_status",
            )
            total_tokens, total_status = _usage_total(
                call_usage,
                value_key="total_tokens",
                status_key="token_accounting_status",
            )
            cost_usd, cost_status = _usage_total(
                call_usage,
                value_key="cost",
                status_key="cost_accounting_status",
            )
            calls.append(
                {
                    "backend": route.entry.backend,
                    "model": route.entry.model,
                    "capability": route.entry.capability_score,
                    "selection_reason": (
                        "cheapest_eligible"
                        if retry_number == 0
                        else "stronger_after_" + str(calls[-1].get("escalation_reason"))
                    ),
                    "authority": route.entry.authority,
                    "input_tokens": input_tokens,
                    "input_token_accounting_status": input_status,
                    "output_tokens": output_tokens,
                    "output_token_accounting_status": output_status,
                    "total_tokens": total_tokens,
                    "total_token_accounting_status": total_status,
                    "cost_usd": cost_usd,
                    "cost_accounting_status": cost_status,
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
            if returned.metadata.get("focused_probe_requests_pending"):
                return replace(
                    returned,
                    verifier=route.entry.backend,
                    metadata={
                        **dict(returned.metadata),
                        **base_metadata,
                        "verifier_calls": calls,
                        "verifier_disagreement": disagreement
                        or bool(returned.metadata.get("verifier_disagreement")),
                        "verifier_disagreement_resolution": None,
                        "verifier_route_complete": False,
                        "verifier_route_index": retry_number,
                        "verifier_route_awaiting_focused_probes": True,
                    },
                    llm_usage=tuple(usage),
                )
            if bool(returned.metadata.get("adaptive_pending_independent_verifier")):
                calls[-1]["escalation_reason"] = "independent_verifier_required"
                if retry_number + 1 < len(routes):
                    independent_primary = returned
                    independent_primary_route = (
                        route.entry.backend,
                        route.entry.model,
                    )
                    continue
                return replace(
                    returned,
                    outcome="rejected",
                    acceptance_eligible=False,
                    reason="Critical-risk proof requires a distinct independent verifier.",
                    recommended_action="fail",
                    risk_flags=tuple(returned.risk_flags)
                    + ("acceptance_blocker:independent_verifier_unavailable",),
                    metadata={
                        **dict(returned.metadata),
                        **base_metadata,
                        "verifier_calls": calls,
                        "verifier_route_complete": False,
                    },
                    llm_usage=tuple(usage),
                )
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
                        "verifier_disagreement": disagreement
                        or bool(returned.metadata.get("verifier_disagreement")),
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
                "verifier_disagreement": disagreement
                or bool(previous.metadata.get("verifier_disagreement")),
                "verifier_disagreement_resolution": (
                    "unresolved" if disagreement else None
                ),
                "verifier_route_complete": False,
            },
            llm_usage=tuple(usage),
        )

    def finalize_with_focused_probes(
        self,
        context: AttemptContext,
        result: AttemptResult,
        initial_verification: Verification,
    ) -> Verification:
        """Finalize the route that proposed probes without another model call."""

        route_index = initial_verification.metadata.get("verifier_route_index")
        if not isinstance(route_index, int) or not (
            0 <= route_index < len(self.routes)
        ):
            return initial_verification
        finalize = getattr(
            self.routes[route_index].verifier,
            "finalize_with_focused_probes",
            None,
        )
        if not callable(finalize):
            return initial_verification
        finalize_context = replace(
            context,
            policy_configuration={
                **dict(context.policy_configuration),
                "_adaptive_verification_cascade_active": True,
            },
        )
        returned = finalize(finalize_context, result, initial_verification)
        if not isinstance(returned, Verification):
            raise TypeError("verifier returned an invalid focused-probe finalization")
        if bool(returned.metadata.get("adaptive_pending_independent_verifier")):
            primary_identity = (
                self.routes[route_index].entry.backend,
                self.routes[route_index].entry.model,
            )
            cascade_context = replace(
                context,
                policy_configuration={
                    **dict(context.policy_configuration),
                    "_adaptive_verification_cascade_active": True,
                },
            )
            for second_index in range(route_index + 1, len(self.routes)):
                second_route = self.routes[second_index]
                if (
                    second_route.entry.backend,
                    second_route.entry.model,
                ) == primary_identity:
                    continue
                try:
                    second = second_route.verifier.verify(cascade_context, result)
                except Exception as error:
                    second = _failure_verification(
                        "Independent verifier invocation failed closed.",
                        {
                            "invocation_status": "error",
                            "exception_class": type(error).__name__,
                        },
                    )
                raw_agreement = bool(
                    second.metadata.get("pre_adaptive_acceptance_eligible")
                    or second.acceptance_eligible
                )
                if not raw_agreement:
                    continue
                finalize_independent = getattr(
                    second_route.verifier,
                    "finalize_independent_verification",
                    None,
                )
                if not callable(finalize_independent):
                    continue
                combined = finalize_independent(
                    cascade_context,
                    result,
                    replace(
                        second,
                        metadata={
                            **dict(second.metadata),
                            "independent_verifier_completed": True,
                            "independent_primary_verifier": {
                                "backend": primary_identity[0],
                                "model": primary_identity[1],
                            },
                        },
                    ),
                )
                return replace(
                    combined,
                    verifier=second_route.entry.backend,
                    metadata={
                        **dict(returned.metadata),
                        **dict(combined.metadata),
                        "verifier_route_complete": True,
                        "verifier_route_awaiting_focused_probes": False,
                        "independent_verifier_completed": True,
                    },
                    llm_usage=tuple(initial_verification.llm_usage)
                    + tuple(second.llm_usage),
                )
            return replace(
                returned,
                outcome="rejected",
                acceptance_eligible=False,
                reason="Critical-risk proof requires a distinct independent verifier.",
                recommended_action="fail",
                risk_flags=tuple(returned.risk_flags)
                + ("acceptance_blocker:independent_verifier_unavailable",),
                metadata={
                    **dict(returned.metadata),
                    "verifier_route_complete": False,
                    "verifier_route_awaiting_focused_probes": False,
                },
                llm_usage=initial_verification.llm_usage,
            )
        metadata = {
            **dict(initial_verification.metadata),
            **dict(returned.metadata),
            "verifier_route_complete": True,
            "verifier_route_awaiting_focused_probes": False,
        }
        return replace(
            returned,
            verifier=self.routes[route_index].entry.backend,
            metadata=metadata,
            llm_usage=initial_verification.llm_usage,
        )


def status_is(result: Verification, value: str) -> bool:
    return result.metadata.get("invocation_status") == value
