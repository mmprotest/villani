"""Read-only public policy preview and historical route simulation."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from villani_ops.core.backend import Backend

from .capabilities.models import CapabilitySnapshot
from .durable_io import read_jsonl_tolerant
from .interfaces import BudgetContext, PolicyContext, PolicyDecision
from .policy import BootstrapPolicyEngine
from .policy_presets import (
    PUBLIC_POLICY_VERSION,
    apply_policy_preset,
    configured_policy_preset,
)
from .protocol import ClassificationSnapshot
from .verifier_routing import (
    VerifierPolicyEntry,
    VerifierRoutingContext,
    VerifierRoutingPolicy,
    required_capability as required_verifier_capability,
)


POLICY_PREVIEW_SCHEMA = "villani.policy_preview.v1"
POLICY_SIMULATION_SCHEMA = "villani.policy_simulation.v1"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _verifier_route_explanation(
    configuration: Mapping[str, Any],
    backends: Mapping[str, Backend],
    classification: ClassificationSnapshot,
) -> dict[str, Any]:
    graph = configuration.get("verification_graph")
    if isinstance(graph, Mapping):
        return {
            "selected": {
                "route": str(graph.get("graph_id") or "verification-graph"),
                "version": str(graph.get("version") or "unknown"),
                "authority": "acceptance",
                "reason": "Configured authoritative verification graph.",
            },
            "fallbacks": [],
            "excluded": [],
            "repository_validation_required": True,
        }

    verifier = _mapping(configuration.get("verifier"))
    raw_routes = verifier.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        backend_name = verifier.get("backend")
        backend = backends.get(str(backend_name)) if backend_name else None
        no_llm = bool(verifier.get("no_llm", True))
        return {
            "selected": {
                "route": "deterministic-verifier" if no_llm else str(backend_name),
                "model": None if no_llm else (backend.model if backend else None),
                "authority": "acceptance",
                "availability": (
                    "available"
                    if no_llm or (backend is not None and backend.enabled)
                    else "unavailable"
                ),
                "reason": (
                    "Deterministic evidence verification after repository validation."
                    if no_llm
                    else "Configured verifier route; repository validation remains required."
                ),
            },
            "fallbacks": [],
            "excluded": [],
            "repository_validation_required": True,
        }

    policy = VerifierRoutingPolicy.model_validate(_mapping(verifier.get("policy")))
    context = VerifierRoutingContext(
        risk=classification.risk,
        difficulty=classification.difficulty,
        missing_evidence=True,
    )
    minimum, reasons = required_verifier_capability(policy, context)
    authority_rank = {"advisory": 0, "acceptance": 1}
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_routes):
        if not isinstance(raw, Mapping):
            excluded.append(
                {"route": f"route-{index + 1}", "reasons": ["malformed route"]}
            )
            continue
        backend_name = str(raw.get("backend") or "")
        backend = backends.get(backend_name)
        try:
            entry = VerifierPolicyEntry.model_validate(
                {
                    "backend": backend_name,
                    "model": raw.get("model") or (backend.model if backend else None),
                    "capability_score": raw.get(
                        "capability_score", backend.capability_score if backend else 0
                    ),
                    "price_per_call_usd": raw.get(
                        "price_per_call_usd",
                        backend.fixed_cost_per_attempt if backend else None,
                    ),
                    "expected_latency_ms": raw.get("expected_latency_ms"),
                    "authority": raw.get("authority", "acceptance"),
                    "available": raw.get(
                        "available", backend.enabled if backend else False
                    ),
                }
            )
        except ValueError as error:
            excluded.append(
                {"route": backend_name or f"route-{index + 1}", "reasons": [str(error)]}
            )
            continue
        rejection: list[str] = []
        if not entry.available:
            rejection.append("verifier is unavailable")
        if entry.capability_score < minimum:
            rejection.append(
                f"capability {entry.capability_score:g} is below required {minimum:g}"
            )
        if authority_rank[entry.authority] < authority_rank[policy.minimum_authority]:
            rejection.append(
                f"authority {entry.authority} is below required {policy.minimum_authority}"
            )
        row = {
            "route": entry.backend,
            "model": entry.model,
            "capability": entry.capability_score,
            "authority": entry.authority,
            "estimated_cost": entry.price_per_call_usd,
            "estimated_cost_status": (
                "known" if entry.price_per_call_usd is not None else "unknown"
            ),
        }
        if rejection:
            excluded.append({**row, "reasons": rejection})
        else:
            eligible.append(row)
    eligible.sort(
        key=lambda item: (
            item["estimated_cost"] is None,
            item["estimated_cost"]
            if item["estimated_cost"] is not None
            else float("inf"),
            item["capability"],
            item["route"],
        )
    )
    return {
        "selected": eligible[0] if eligible else None,
        "fallbacks": eligible[1:],
        "excluded": excluded,
        "minimum_capability": minimum,
        "selection_reasons": list(reasons),
        "policy_version": policy.version,
        "repository_validation_required": True,
    }


def build_policy_preview_document(
    *,
    raw_classification: ClassificationSnapshot,
    effective_classification: ClassificationSnapshot,
    adjustments: Sequence[Mapping[str, Any]],
    decision: PolicyDecision,
    configuration: Mapping[str, Any],
    backends: Mapping[str, Backend],
) -> dict[str, Any]:
    eligible = [asdict(item) for item in decision.considered_backends if item.eligible]
    excluded = [
        asdict(item) for item in decision.considered_backends if not item.eligible
    ]
    chosen = next(
        (
            item
            for item in decision.considered_backends
            if item.backend_name == decision.chosen_backend
        ),
        None,
    )
    uncertainty: list[str] = []
    if effective_classification.confidence < 0.80:
        uncertainty.append("Classification confidence is below the economy threshold.")
    if chosen is None:
        uncertainty.append("No coding model is currently eligible.")
    elif chosen.estimated_cost_usd is None:
        uncertainty.append("Selected-route cost is unknown.")
    provenance = _mapping(decision.metadata.get("route_provenance"))
    if provenance.get("basis") in {"manual", "bootstrap", "observed"}:
        uncertainty.append(
            "Selected model capability is not qualified empirical evidence."
        )
    stage_projection = _mapping(decision.metadata.get("stage_budget_projection"))
    route_plan = _mapping(decision.metadata.get("route_plan"))
    if stage_projection and not stage_projection.get("reserve_satisfied", False):
        uncertainty.append("Required downstream stage reserves are not satisfied.")
    if not uncertainty:
        uncertainty.append("No unresolved routing uncertainty was recorded.")
    route_unknowns = route_plan.get("unknowns", []) if route_plan else []
    for item in route_unknowns:
        uncertainty.append(f"Route economics input is unknown: {item}.")
    backend_explanations = [
        {
            "backend": item.backend_name,
            "model": item.model,
            "configured_score": item.configured_capability_score,
            "effective_score": item.effective_capability_score,
            "score_provenance": item.capability_provenance,
            "capability_confidence": item.capability_confidence,
            "uncertainty_penalty": item.uncertainty_penalty,
            "empirical_status": item.qualification_status,
            "sample_count": item.empirical_sample_count,
            "wilson_lower_bound": item.empirical_wilson_lower_bound,
            "required_score": decision.required_capability_score,
            "eligibility": item.eligible,
            "rejection_reasons": list(item.rejection_reasons),
            "estimated_cost": item.estimated_cost_usd,
            "cost_accounting_status": item.cost_accounting_status,
            "estimated_duration_ms": item.estimated_duration_ms,
            "duration_accounting_status": item.duration_accounting_status,
            "reserve_impact": dict(item.reserve_impact),
            "override_applied": item.override_applied,
        }
        for item in decision.considered_backends
    ]
    return {
        "schema_version": POLICY_PREVIEW_SCHEMA,
        "raw_classification": raw_classification.model_dump(mode="json"),
        "effective_classification": effective_classification.model_dump(mode="json"),
        "adjustments": [dict(item) for item in adjustments],
        "eligible_models": eligible,
        "excluded_models": excluded,
        "selected_coding_route": {
            "backend": decision.chosen_backend,
            "model": decision.chosen_model,
            "action": decision.action,
            "reason": decision.reason,
            "route_provenance": dict(provenance) if provenance else None,
            "retry_allowed": decision.metadata.get("retry_allowed"),
            "retry_reason_code": decision.metadata.get("policy_reason_code"),
            "credible_progress_assessment": decision.metadata.get(
                "credible_progress_assessment"
            ),
            "next_higher_backend": decision.metadata.get("next_higher_backend"),
            "stage_budget_projection": decision.metadata.get("stage_budget_projection"),
            "empirical_sequence": decision.metadata.get("empirical_optimizer"),
            "override_status": (
                provenance.get("explicit_override") if provenance else False
            ),
            "route_plan": dict(route_plan) if route_plan else None,
        },
        "backend_explanations": backend_explanations,
        "selected_verifier_route": _verifier_route_explanation(
            configuration, backends, effective_classification
        ),
        "estimated_cost": {
            "value": (
                _mapping(route_plan.get("sequence_economics")).get(
                    "expected_accepted_change_cost"
                )
                if route_plan
                else chosen.estimated_cost_usd
                if chosen
                else None
            ),
            "status": (
                _mapping(route_plan.get("sequence_economics")).get(
                    "accounting_status", "unknown"
                )
                if route_plan
                else chosen.cost_accounting_status
                if chosen
                else "unknown"
            ),
            "currency": (
                _mapping(route_plan.get("sequence_economics")).get("currency")
                if route_plan
                else next(
                    (
                        backend.currency
                        for backend in backends.values()
                        if backend.name == decision.chosen_backend
                    ),
                    None,
                )
            ),
        },
        "uncertainty": uncertainty,
        "policy_version": {
            "public": PUBLIC_POLICY_VERSION,
            "preset": configured_policy_preset(configuration),
            "controller": decision.policy_version,
        },
        "coding_attempt_executed": False,
    }


def initial_policy_context(
    classification: ClassificationSnapshot,
    configuration: Mapping[str, Any],
    *,
    run_id: str,
) -> PolicyContext:
    budgets = _mapping(configuration.get("budgets"))
    max_attempts = int(budgets.get("max_attempts", 3))
    raw_cost = budgets.get("max_cost")
    max_cost = float(raw_cost) if raw_cost is not None else None
    raw_wall = budgets.get("max_wall_time")
    wall_ms = int(float(raw_wall) * 1000) if raw_wall is not None else None
    return PolicyContext(
        run_id=run_id,
        trace_id=f"preview:{run_id}",
        state="CLASSIFIED",
        classification=classification,
        attempts=(),
        verifications=(),
        eligible_candidate_ids=(),
        budget=BudgetContext(
            remaining_attempts=max_attempts,
            remaining_cost_usd=max_cost,
            cost_accounting_status=(
                "complete" if max_cost is not None else "not_applicable"
            ),
            remaining_wall_time_ms=wall_ms,
            duration_accounting_status=(
                "complete" if wall_ms is not None else "not_applicable"
            ),
        ),
        policy_configuration=configuration,
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def simulate_historical_runs(
    *,
    runs_root: Path,
    configuration: Mapping[str, Any],
    backends: Mapping[str, Backend],
    snapshot: CapabilitySnapshot | None,
    preset: str,
) -> dict[str, Any]:
    simulated_configuration = apply_policy_preset(configuration, preset)
    engine = BootstrapPolicyEngine(
        backends,
        simulated_configuration,
        capability_snapshot=snapshot,
    )
    rows: list[dict[str, Any]] = []
    ignored = 0
    if runs_root.is_dir():
        directories = sorted(
            (item for item in runs_root.iterdir() if item.is_dir()),
            key=lambda item: item.name,
        )
    else:
        directories = []
    for directory in directories:
        task = _read_json(directory / "task.json")
        raw_classification = _read_json(directory / "classification.json")
        if task is None or raw_classification is None:
            ignored += 1
            continue
        try:
            classification = ClassificationSnapshot.model_validate(raw_classification)
            decisions = read_jsonl_tolerant(directory / "policy_decisions.jsonl")
        except (OSError, ValueError, json.JSONDecodeError):
            ignored += 1
            continue
        actual = next(
            (
                item
                for item in decisions
                if item.get("action") in {"attempt", "retry", "escalate"}
                and item.get("chosen_backend")
            ),
            None,
        )
        if actual is None:
            ignored += 1
            continue
        try:
            simulated = engine.decide(
                initial_policy_context(
                    classification,
                    simulated_configuration,
                    run_id=str(task.get("run_id") or directory.name),
                )
            )
        except (TypeError, ValueError):
            ignored += 1
            continue
        actual_backend = str(actual.get("chosen_backend") or "")
        actual_option = next(
            (
                item
                for item in actual.get("considered_backends", [])
                if isinstance(item, Mapping)
                and item.get("backend_name") == actual_backend
            ),
            {},
        )
        simulated_option = next(
            (
                item
                for item in simulated.considered_backends
                if item.backend_name == simulated.chosen_backend
            ),
            None,
        )
        actual_cost = actual_option.get("estimated_cost_usd")
        actual_cost_value = (
            float(actual_cost) if isinstance(actual_cost, (int, float)) else None
        )
        simulated_cost = (
            simulated_option.estimated_cost_usd if simulated_option else None
        )
        rows.append(
            {
                "run_id": str(task.get("run_id") or directory.name),
                "task": str(task.get("instruction") or ""),
                "actual_backend": actual_backend,
                "simulated_backend": simulated.chosen_backend,
                "route_changed": actual_backend != simulated.chosen_backend,
                "actual_estimated_cost": actual_cost_value,
                "simulated_estimated_cost": simulated_cost,
                "estimated_cost_difference": (
                    simulated_cost - actual_cost_value
                    if simulated_cost is not None and actual_cost_value is not None
                    else None
                ),
                "recorded_outcome_applies_to": actual_backend,
                "counterfactual_outcome_known": False,
            }
        )
    known_differences = [
        float(item["estimated_cost_difference"])
        for item in rows
        if item["estimated_cost_difference"] is not None
    ]
    unknown_differences = len(rows) - len(known_differences)
    return {
        "schema_version": POLICY_SIMULATION_SCHEMA,
        "preset": configured_policy_preset(simulated_configuration),
        "policy_version": PUBLIC_POLICY_VERSION,
        "tasks_evaluated": len(rows),
        "tasks_affected": sum(1 for item in rows if item["route_changed"]),
        "route_changes": [item for item in rows if item["route_changed"]],
        "estimated_cost_differences": {
            "status": (
                "complete"
                if rows and unknown_differences == 0
                else "partial"
                if known_differences
                else "unknown"
            ),
            "simulated_minus_recorded_total": (
                sum(known_differences) if known_differences else None
            ),
            "known_task_count": len(known_differences),
            "unknown_task_count": unknown_differences,
        },
        "outcome_evidence_limitations": [
            "Recorded outcomes apply only to routes that actually executed.",
            "A simulated route was not executed or authoritatively validated.",
            "Unknown price remains unknown and is excluded from numeric differences.",
        ],
        "unsupported_counterfactual_claims": [
            "causal cost savings",
            "counterfactual task success",
            "counterfactual validation outcome",
            "counterfactual latency",
        ],
        "causal_savings_supported": False,
        "live_policy_changed": False,
        "ignored_run_count": ignored,
    }


__all__ = [
    "POLICY_PREVIEW_SCHEMA",
    "POLICY_SIMULATION_SCHEMA",
    "build_policy_preview_document",
    "initial_policy_context",
    "simulate_historical_runs",
]
