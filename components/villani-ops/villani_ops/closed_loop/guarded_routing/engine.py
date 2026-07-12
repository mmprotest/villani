from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping

from ..interfaces import BackendOption, BudgetContext, PolicyDecision
from .models import (
    CircuitBreakerState,
    ControlledAlternative,
    GuardedRoutingDecision,
    TaskRoute,
)
from .resolution import resolve_routing_configuration


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class GuardedTaskRouter:
    def __init__(self, configuration: Mapping[str, Any]) -> None:
        self.configuration, self.precedence = resolve_routing_configuration(
            configuration
        )

    @property
    def mode(self) -> str:
        return str(self.configuration.get("mode") or "observe")

    def _policy(self) -> tuple[Mapping[str, Any] | None, str]:
        active = self.configuration.get("active_policy")
        if isinstance(active, Mapping) and active.get("state") == "active":
            return active, "active_policy"
        lkg = self.configuration.get("last_known_good_policy")
        if isinstance(lkg, Mapping) and lkg.get("state") == "active":
            return lkg, "last_known_good_policy"
        return None, "bootstrap_policy"

    def _route(self, value: Mapping[str, Any]) -> TaskRoute:
        return TaskRoute(
            agent_adapter=str(value.get("agent_adapter") or "villani-code"),
            backend_name=str(value["backend_name"]),
            model=str(value["model"]),
            execution_provider=str(value.get("execution_provider") or "inherit"),
            maximum_attempts=int(value.get("maximum_attempts") or 1),
            candidate_strategy=str(
                value.get("candidate_strategy") or "deterministic_evidence_v1"
            ),
            verifier_graph_version=str(
                value.get("verifier_graph_version")
                or "villani_ops_verifier_pipeline_v1"
            ),
            escalation_sequence=tuple(
                str(item)
                for item in value.get("escalation_sequence", [value["backend_name"]])
            ),
        )

    def _alternatives(
        self, policy: Mapping[str, Any] | None, bootstrap: PolicyDecision
    ) -> tuple[ControlledAlternative, ...]:
        rules = policy.get("rules") if isinstance(policy, Mapping) else None
        raw = rules.get("alternatives") if isinstance(rules, Mapping) else None
        values = raw if isinstance(raw, list) else []
        bootstrap_by_name = {
            item.backend_name: item for item in bootstrap.considered_backends
        }
        alternatives: list[ControlledAlternative] = []
        if not values:
            for item in bootstrap.considered_backends:
                alternatives.append(self._from_bootstrap(item))
            return tuple(alternatives)
        constraints = self.configuration.get("constraints")
        global_constraints = constraints if isinstance(constraints, Mapping) else {}
        for value in values:
            if (
                not isinstance(value, Mapping)
                or not value.get("backend_name")
                or not value.get("model")
            ):
                continue
            route = self._route(value)
            bootstrap_option = bootstrap_by_name.get(route.backend_name)
            reasons: list[str] = []
            if bootstrap_option is None or not bootstrap_option.eligible:
                reasons.append("bootstrap_ineligible")
            if bootstrap_option is not None and bootstrap_option.model not in {
                None,
                route.model,
            }:
                reasons.append("model_mismatch")
            required_residency = global_constraints.get("residency")
            residencies = value.get("residencies", [])
            if required_residency and required_residency not in residencies:
                reasons.append("residency_constraint")
            if bool(global_constraints.get("security_sensitive")) and not bool(
                value.get("security_approved")
            ):
                reasons.append("security_constraint")
            maximum_cost = global_constraints.get("maximum_cost_usd")
            estimated_cost = value.get("estimated_cost_usd")
            if maximum_cost is not None and (
                not isinstance(estimated_cost, (int, float))
                or estimated_cost > maximum_cost
            ):
                reasons.append("cost_constraint")
            if not bool(value.get("user_allowed", True)):
                reasons.append("user_constraint")
            alternatives.append(
                ControlledAlternative(
                    route=route,
                    eligible=not reasons,
                    constraints={
                        **dict(global_constraints),
                        "security_approved": value.get("security_approved"),
                        "residencies": residencies,
                    },
                    rejection_reasons=tuple(reasons),
                    estimated_cost_usd=float(estimated_cost)
                    if isinstance(estimated_cost, (int, float))
                    else None,
                    expected_success=float(value["expected_success"])
                    if isinstance(value.get("expected_success"), (int, float))
                    else None,
                    expected_latency_ms=float(value["expected_latency_ms"])
                    if isinstance(value.get("expected_latency_ms"), (int, float))
                    else None,
                    uncertainty=float(value.get("uncertainty", 1.0)),
                )
            )
        return tuple(alternatives)

    @staticmethod
    def _from_bootstrap(item: BackendOption) -> ControlledAlternative:
        return ControlledAlternative(
            route=TaskRoute(
                agent_adapter="villani-code",
                backend_name=item.backend_name,
                model=item.model or "unknown",
                execution_provider="inherit",
                maximum_attempts=1,
                candidate_strategy="deterministic_evidence_v1",
                verifier_graph_version="villani_ops_verifier_pipeline_v1",
                escalation_sequence=(item.backend_name,),
            ),
            eligible=item.eligible,
            constraints=dict(item.cost_components),
            rejection_reasons=item.rejection_reasons,
            estimated_cost_usd=item.estimated_cost_usd,
            expected_success=None,
            expected_latency_ms=None,
            uncertainty=1.0,
        )

    def _circuit_breakers(
        self,
        attempts: tuple[Any, ...],
        verifications: tuple[Any, ...],
        budget: BudgetContext,
        alternatives: tuple[ControlledAlternative, ...],
    ) -> CircuitBreakerState:
        settings = self.configuration.get("circuit_breakers")
        limits = settings if isinstance(settings, Mapping) else {}
        failures = sum(
            item.failure_category == "infrastructure_failure" for item in attempts
        )
        rate_limits = sum(
            item.failure_category == "rate_limit" or item.rate_limited
            for item in attempts
        )
        disagreements = sum(
            item.failure_category == "verifier_disagreement" or item.disagreement
            for item in verifications
        )
        durations: list[float] = [
            float(value)
            for item in attempts
            if isinstance((value := getattr(item, "duration_ms", None)), (int, float))
        ]
        failure_rate = failures / len(attempts) if attempts else 0.0
        reasons: list[str] = []
        minimum = int(limits.get("minimum_samples", 1))
        if len(attempts) >= minimum and failure_rate >= float(
            limits.get("provider_failure_rate", 1.1)
        ):
            reasons.append("provider_failure_rate")
        if durations and max(durations) >= float(
            limits.get("latency_ms", float("inf"))
        ):
            reasons.append("latency")
        if rate_limits >= int(limits.get("rate_limit_count", 2**31 - 1)):
            reasons.append("rate_limits")
        if disagreements >= int(limits.get("verifier_disagreement_count", 2**31 - 1)):
            reasons.append("verifier_disagreement")
        eligible_costs = [item for item in alternatives if item.eligible]
        expected = sum(
            item.estimated_cost_usd or 0 for item in eligible_costs[: len(attempts)]
        )
        actual = budget.actual_cost_consumed_usd
        anomaly_factor = float(limits.get("budget_anomaly_factor", float("inf")))
        if actual is not None and expected > 0 and actual > expected * anomaly_factor:
            reasons.append("budget_anomaly")
        if bool(self.configuration.get("emergency_disabled")):
            reasons.append("emergency_global_disable")
        return CircuitBreakerState(
            open=bool(reasons),
            reasons=tuple(reasons),
            metrics={
                "provider_failure_rate": failure_rate,
                "rate_limit_count": rate_limits,
                "verifier_disagreement_count": disagreements,
                "maximum_latency_ms": max(durations) if durations else None,
                "actual_cost_usd": actual,
                "expected_cost_usd": expected,
            },
        )

    def evaluate(
        self,
        *,
        run_id: str,
        sequence: int,
        bootstrap: PolicyDecision,
        attempts: tuple[Any, ...],
        verifications: tuple[Any, ...],
        budget: BudgetContext,
        timestamp: datetime,
        experiment_assignment: Mapping[str, Any] | None = None,
    ) -> tuple[PolicyDecision, GuardedRoutingDecision]:
        mode = self.mode
        if mode not in {"observe", "recommend", "enforce"}:
            raise ValueError(f"unknown routing mode: {mode}")
        policy, source = self._policy()
        alternatives = self._alternatives(policy, bootstrap)
        eligible = [item for item in alternatives if item.eligible]
        recommended = eligible[0] if eligible else None
        if recommended and attempts:
            sequence_names = recommended.route.escalation_sequence
            used = {item.backend_name for item in attempts}
            next_name = next(
                (name for name in sequence_names if name not in used), None
            )
            recommended = next(
                (item for item in eligible if item.route.backend_name == next_name),
                recommended,
            )
        circuit = self._circuit_breakers(attempts, verifications, budget, alternatives)
        permissions = self.configuration.get("permissions")
        allowed = permissions if isinstance(permissions, Mapping) else {}
        fallback = self.configuration.get("emergency_fallback")
        fallback_configured = (
            isinstance(fallback, Mapping)
            and bool(fallback.get("backend_name"))
            and bool(fallback.get("model"))
        )
        enforce_ready = (
            source in {"active_policy", "last_known_good_policy"}
            and bool(policy and policy.get("version"))
            and bool(allowed.get("user_enforce"))
            and bool(allowed.get("workspace_enforce"))
            and fallback_configured
        )
        final = bootstrap
        reason = f"{mode} mode preserved deterministic bootstrap execution"
        marginal: float | None = None
        evidence_summary = {
            "attempt_failure_categories": [item.failure_category for item in attempts],
            "verification_outcomes": [item.outcome for item in verifications],
            "verification_actions": [item.recommended_action for item in verifications],
            "acceptance_eligible_count": sum(
                item.acceptance_eligible for item in verifications
            ),
        }
        if bootstrap.action == "escalate" and recommended is not None:
            value = float(self.configuration.get("value_of_success_usd", 0.0))
            evidence_factor = (
                0.5
                if verifications and verifications[-1].outcome in {"unclear", "error"}
                else 1.0
            )
            conservative_success = (
                (recommended.expected_success or 0.0)
                * (1.0 - recommended.uncertainty)
                * evidence_factor
            )
            marginal = conservative_success * value - (
                recommended.estimated_cost_usd or 0.0
            )
        execution_route = self._bootstrap_execution_route(bootstrap)
        if mode == "enforce":
            if not enforce_ready:
                final = replace(
                    bootstrap,
                    action="fail",
                    chosen_backend=None,
                    chosen_model=None,
                    reason="Enforce mode prerequisites are not satisfied.",
                )
                execution_route = None
                source = "fail_closed"
                reason = final.reason
            elif circuit.open:
                final = replace(
                    bootstrap,
                    action="exhaust",
                    chosen_backend=None,
                    chosen_model=None,
                    reason="Circuit breaker or emergency disable opened before another paid attempt.",
                )
                execution_route = None
                reason = final.reason
            elif recommended is None:
                final = replace(
                    bootstrap,
                    action="exhaust",
                    chosen_backend=None,
                    chosen_model=None,
                    reason="No guarded routing alternative can prove all constraints.",
                )
                execution_route = None
                source = "fail_closed"
                reason = final.reason
            elif (
                budget.actual_stage_attempts_used >= recommended.route.maximum_attempts
            ):
                final = replace(
                    bootstrap,
                    action="exhaust",
                    chosen_backend=None,
                    chosen_model=None,
                    reason="Guarded route maximum-attempt cap reached.",
                )
                execution_route = None
                reason = final.reason
            elif marginal is not None and marginal <= float(
                self.configuration.get("minimum_marginal_value_usd", 0.0)
            ):
                final = replace(
                    bootstrap,
                    action="exhaust",
                    chosen_backend=None,
                    chosen_model=None,
                    reason="Expected marginal value does not justify another paid escalation.",
                )
                execution_route = None
                reason = final.reason
            elif bootstrap.action in {"attempt", "retry", "escalate"}:
                assert policy is not None
                final = replace(
                    bootstrap,
                    chosen_backend=recommended.route.backend_name,
                    chosen_model=recommended.route.model,
                    policy_version=str(policy.get("version")),
                    metadata={
                        **dict(bootstrap.metadata),
                        "guarded_task_route": recommended.route.model_dump(mode="json"),
                    },
                )
                execution_route = recommended.route
                reason = "Enforced active guarded task-level route after all eligibility and safety checks."
        payload = {
            "run_id": run_id,
            "sequence": sequence,
            "mode": mode,
            "policy": policy,
            "source": source,
            "alternatives": [item.model_dump(mode="json") for item in alternatives],
            "bootstrap": {
                "action": bootstrap.action,
                "backend": bootstrap.chosen_backend,
                "model": bootstrap.chosen_model,
                "version": bootstrap.policy_version,
            },
            "budget": budget.__dict__ if hasattr(budget, "__dict__") else str(budget),
            "assignment": dict(experiment_assignment)
            if experiment_assignment
            else None,
            "attempts": [
                item.__dict__ if hasattr(item, "__dict__") else str(item)
                for item in attempts
            ],
            "verifications": [
                item.__dict__ if hasattr(item, "__dict__") else str(item)
                for item in verifications
            ],
        }
        record = GuardedRoutingDecision(
            decision_id=f"guarded_{sequence:03d}",
            run_id=run_id,
            decision_sequence=sequence,
            mode=mode,
            policy_source=source,
            policy_version=str(policy.get("version"))
            if policy
            else bootstrap.policy_version,
            resolved_scope_precedence=self.precedence,
            alternatives=alternatives,
            recommended_route=recommended.route if recommended else None,
            execution_route=execution_route,
            experiment_assignment=dict(experiment_assignment)
            if experiment_assignment
            else None,
            budget_before={
                key: getattr(budget, key) for key in budget.__dataclass_fields__
            },
            evidence_summary=evidence_summary,
            actual_spend_usd=budget.actual_cost_consumed_usd,
            expected_marginal_value_usd=marginal,
            circuit_breakers=circuit,
            final_reason=reason,
            input_digest_sha256=_digest(payload),
            timestamp=timestamp,
            controls_execution=mode == "enforce" and execution_route is not None,
        )
        return final, record

    @staticmethod
    def _bootstrap_execution_route(decision: PolicyDecision) -> TaskRoute | None:
        if (
            decision.action not in {"attempt", "retry", "escalate"}
            or not decision.chosen_backend
        ):
            return None
        return TaskRoute(
            agent_adapter="villani-code",
            backend_name=decision.chosen_backend,
            model=decision.chosen_model or "unknown",
            execution_provider="inherit",
            maximum_attempts=1,
            candidate_strategy="deterministic_evidence_v1",
            verifier_graph_version="villani_ops_verifier_pipeline_v1",
            escalation_sequence=(decision.chosen_backend,),
        )
