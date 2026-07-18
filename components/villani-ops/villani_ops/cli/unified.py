"""The single public Villani CLI for canonical closed-loop runs."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from villani_ops.classification import TaskClassifier
from villani_ops.cli.task_input import TaskInputError, resolve_task_input
from villani_ops.llm.client import LLMCallError, LLMCallResult
from villani_ops.diagnostics import (
    RepositoryDiagnosticError,
    build_repository_diagnostics,
    probe_backend,
    resolve_doctor_repository,
)
from villani_ops.closed_loop import (
    BootstrapPolicyEngine,
    ClosedLoopController,
    ClosedLoopRunRequest,
    ClosedLoopRunResult,
    EvidenceSelectorAdapter,
    PatchMaterializerAdapter,
    VillaniVerifierAdapter,
)
from villani_ops.closed_loop.agent_systems.configuration import (
    migrate_agent_system_configuration,
)
from villani_ops.closed_loop.agent_systems.registry import (
    build_agent_system_registry,
)
from villani_ops.closed_loop.durable_io import (
    read_jsonl_tolerant,
    write_json_atomic,
)
from villani_ops.closed_loop.capabilities.report import backend_score_rows
from villani_ops.closed_loop.capabilities.store import CapabilityStore
from villani_ops.closed_loop.qualification import (
    QualificationInvalidation,
    QualificationObservation,
    QualificationStore,
    assess_qualification,
    build_gate_c_report,
    observation_from_evaluation_trial,
    repository_qualification_context,
    task_profile as qualification_task_profile,
)
from villani_ops.closed_loop.qualification.repository import (
    canonical_digest,
    qualification_system_identity,
)
from villani_ops.closed_loop.qualification.store import (
    qualification_policy_from_configuration,
)
from villani_ops.closed_loop.economics import (
    EconomicsStore,
    HistoricalRouteCase,
    RoutePolicy,
    RoutePolicyEvaluation,
    RoutePolicyStore,
    evaluate_route_policy,
    route_policy_from_configuration,
)
from villani_ops.closed_loop.adaptive_verification import (
    BinaryVerificationDecision,
    GateDArm,
    MoneyAccounting,
    append_human_outcome,
    build_supervision_metrics,
    evaluate_gate_d,
    load_decision,
    load_human_outcomes,
    load_plan,
    load_supervision_metrics,
    make_human_outcome,
    persist_supervision_metrics,
)
from villani_ops.closed_loop.offline_evaluation.replay import replay_file
from villani_ops.evaluation_lab.models import (
    FileChangeRequirement,
    SetupCommand,
    ValidationCommand,
)
from villani_ops.evaluation_lab.reporting import (
    build_report,
    load_trials,
    write_reports,
)
from villani_ops.evaluation_lab.reviews import (
    append_review,
    latest_reviews,
    load_reviews,
)
from villani_ops.evaluation_lab.runner import ProductArmExecutor, run_paired_suite
from villani_ops.evaluation_lab.workspace import (
    add_task as evaluation_add_task,
    export_portable_suite,
    freeze_suite,
    import_baseline,
    init_suite,
    validate_suite,
)
from villani_ops.closed_loop.guarded_routing import resolve_routing_configuration
from villani_ops.closed_loop.classification_adjustments import (
    apply_classification_policy,
)
from villani_ops.closed_loop.event_writer import redact_data
from villani_ops.closed_loop.model_management import (
    add_model_to_configuration,
    configured_backends as configured_model_backends,
    default_bootstrap_backend,
    detect_models,
    inventory_document,
    load_model_state,
    remove_model_from_configuration,
    set_bootstrap_default,
    test_models,
    update_detection_state,
    write_configuration_atomic,
    write_model_state,
)
from villani_ops.closed_loop.policy_presets import (
    POLICY_PRESETS,
    PUBLIC_POLICY_VERSION,
    apply_policy_preset,
    configure_policy_preset,
    configured_policy_preset,
    normalize_policy_preset,
    policy_preset_rows,
)
from villani_ops.closed_loop.policy_preview import (
    build_policy_preview_document,
    initial_policy_context,
    simulate_historical_runs,
)
from villani_ops.closed_loop.product_run import (
    build_product_run,
    project_product_stage,
)
from villani_ops.closed_loop.presentation import (
    build_run_presentation,
    failure_experience,
    infer_failure_code,
    progress_lines_for_event,
)
from villani_ops.closed_loop.interfaces import (
    BudgetContext,
    Classification,
    ClassificationContext,
    PolicyContext,
)
from villani_ops.closed_loop.protocol import AccountingStatus, ClassificationSnapshot
from villani_ops.closed_loop.schema_validation import (
    ProtocolValidationError,
    validate_protocol_document,
)
from villani_ops.core.backend import Backend
from villani_ops.closed_loop.costs import actual_attempt_cost
from villani_ops.providers import (
    CANONICAL_PROVIDERS,
    ProviderConfigurationError,
    canonical_provider,
    validate_closed_loop_backend,
    validate_runtime_credentials,
)
from villani_ops.core.task import Task
from villani_ops.subprocess_utils import resolve_command_prefix
from villani_ops.execution_environment import (
    CONFIRMATION_THRESHOLD,
    ExecutionEnvironmentConfig,
    confirmed_command,
    discover_repository_validation,
    display_argv,
    parse_manual_command,
)


console = Console()
app = typer.Typer(
    help="Villani: local-first deterministic coding-agent control loop.",
    no_args_is_help=True,
    add_completion=False,
)
backend_app = typer.Typer(
    help="Manage coding and classification backends.",
    no_args_is_help=True,
    add_completion=False,
)
capability_app = typer.Typer(
    help="Rebuild and inspect local empirical capability profiles.",
    no_args_is_help=True,
    add_completion=False,
)
evaluate_app = typer.Typer(
    help="Offline-only policy replay and evaluation.",
    no_args_is_help=True,
    add_completion=False,
)
eval_app = typer.Typer(
    help="Capture and run paired real-task Founder Thesis Lab evaluations.",
    no_args_is_help=True,
    add_completion=False,
)
policy_app = typer.Typer(
    help="Preview and simulate user-facing policy presets.",
    no_args_is_help=True,
    add_completion=False,
)
models_app = typer.Typer(
    help="Detect, test, add, and remove models.",
    invoke_without_command=True,
    add_completion=False,
)
agents_app = typer.Typer(
    help="List, inspect, and diagnose complete agent systems.",
    no_args_is_help=True,
    add_completion=False,
)
verification_app = typer.Typer(
    help="Inspect verification proof, import explicit outcomes, and evaluate Gate D.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(backend_app, name="backend")
app.add_typer(capability_app, name="capability")
app.add_typer(evaluate_app, name="evaluate")
app.add_typer(eval_app, name="eval")
app.add_typer(policy_app, name="policy")
app.add_typer(models_app, name="models")
app.add_typer(agents_app, name="agents")
app.add_typer(verification_app, name="verification")


def _verification_attempt_id(directory: Path, requested: str | None) -> str:
    if requested:
        if Path(requested).name != requested or requested in {".", ".."}:
            _usage_error("attempt ID must be a single path segment")
        return requested
    manifest = _read_json(directory / "manifest.json") or {}
    selected = manifest.get("selected_attempt_id")
    if isinstance(selected, str) and selected:
        return selected
    attempts = manifest.get("attempt_ids")
    if isinstance(attempts, list) and attempts and isinstance(attempts[-1], str):
        return attempts[-1]
    _usage_error("the run has no recorded attempt to inspect")


def _verification_decisions(directory: Path) -> list[BinaryVerificationDecision]:
    decisions: list[BinaryVerificationDecision] = []
    verification_directory = directory / "verification"
    if not verification_directory.is_dir():
        return decisions
    for path in sorted(verification_directory.glob("*-decision.json")):
        decisions.append(load_decision(path))
    return decisions


def _verification_execution_cost(directory: Path) -> MoneyAccounting:
    manifest = _read_json(directory / "manifest.json") or {}
    status = str(manifest.get("cost_accounting_status") or "unknown")
    value = manifest.get("total_cost_usd")
    if status in {"complete", "partial"} and isinstance(value, (int, float)):
        return MoneyAccounting(
            amount=float(value),
            currency=str(manifest.get("currency") or "USD"),
            accounting_status=status,  # type: ignore[arg-type]
            source="canonical_run_manifest",
        )
    return MoneyAccounting(
        amount=None,
        currency=None,
        accounting_status=("not_applicable" if status == "not_applicable" else "unknown"),
        source="canonical_run_cost_unavailable",
    )


def _verification_selected_identity(
    directory: Path, attempt_id: str
) -> tuple[str, str]:
    attempt = _read_json(directory / "attempts" / attempt_id / "attempt.json") or {}
    system_id = attempt.get("agent_system_id")
    if not isinstance(system_id, str) or not system_id:
        _usage_error(
            "adverse feedback requires the attempt's exact agent-system identity"
        )
    identity_reference = attempt.get("agent_system_identity_path")
    relative = (
        Path(identity_reference)
        if isinstance(identity_reference, str) and identity_reference
        else Path("agent-systems") / f"{system_id}.json"
    )
    identity_path = (directory / relative).resolve()
    if not identity_path.is_relative_to(directory.resolve()):
        _usage_error("agent-system identity path escapes the run bundle")
    identity = _read_json(identity_path) or {}
    if identity.get("system_id") != system_id:
        _usage_error("the selected attempt's exact agent-system identity is unavailable")
    route_name = identity.get("route_name")
    if not isinstance(route_name, str) or not route_name:
        _usage_error("the selected agent-system route identity is unavailable")
    return system_id, route_name


def _quarantine_adverse_outcome(
    *,
    directory: Path,
    run_id: str,
    attempt_id: str,
    outcome_id: str,
    outcome_kind: str,
) -> QualificationInvalidation:
    system_id, route_name = _verification_selected_identity(directory, attempt_id)
    recorded_at = datetime.now(timezone.utc)
    evidence_reference = f"runs/{run_id}/human-outcomes.jsonl#{outcome_id}"
    payload = {
        "recorded_at": recorded_at,
        "system_id": system_id,
        "route_name": route_name,
        "repository_id": None,
        "reason": "false_acceptance",
        "severity": "severe",
        "evidence_reference": evidence_reference,
        "evidence_digest": canonical_digest(
            {"run_id": run_id, "outcome_id": outcome_id, "outcome": outcome_kind}
        ),
        "detail": (
            f"Explicit local outcome {outcome_kind} quarantined automatic use of "
            "the exact agent system."
        ),
    }
    invalidation = QualificationInvalidation(
        **payload,
        invalidation_id=(
            "qinv_"
            + canonical_digest(
                {**payload, "recorded_at": recorded_at.isoformat()}
            ).removeprefix("sha256:")
        ),
    )
    store = QualificationStore(_home() / "qualification")
    store.append_invalidation(invalidation)
    store.rebuild(policy=qualification_policy_from_configuration(_load_config()))
    return invalidation


@verification_app.command("plan")
def verification_plan(
    run_id: str = typer.Argument(..., help="Canonical run ID."),
    attempt_id: str | None = typer.Option(None, "--attempt"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect the deterministic adaptive verification plan without starting work."""

    directory = _run_dir(run_id)
    if not directory.is_dir():
        _usage_error(f"run not found: {run_id}")
    selected = _verification_attempt_id(directory, attempt_id)
    try:
        plan = load_plan(directory / "verification" / f"{selected}-plan.json")
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
        _usage_error(f"adaptive verification plan is unavailable: {error}")
    document = redact_data(plan.model_dump(mode="json"))
    if json_output:
        typer.echo(json.dumps(document, ensure_ascii=False, sort_keys=True))
        return
    console.print(
        f"{plan.risk_tier.title()} risk · {len(plan.nodes)} plan nodes · "
        f"policy {plan.policy_version}"
    )
    for node in plan.nodes:
        console.print(f"- {node.kind}: {node.disposition} — {node.reason}")


@verification_app.command("feedback-import")
def verification_feedback_import(
    run_id: str = typer.Argument(..., help="Canonical run ID."),
    outcome: str | None = typer.Option(None, "--outcome"),
    source_file: Path | None = typer.Option(
        None, "--file", exists=True, dir_okay=False, readable=True
    ),
    attempt_id: str | None = typer.Option(None, "--attempt"),
    review_minutes: float | None = typer.Option(None, "--review-minutes", min=0),
    correction_summary: str | None = typer.Option(None, "--correction-summary"),
    linked_reference: str | None = typer.Option(None, "--linked-reference"),
    notes: str | None = typer.Option(None, "--notes"),
    full_trace_opened: bool | None = typer.Option(
        None, "--opened-full-trace/--did-not-open-full-trace"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Explicitly import a local human outcome; no repository monitoring occurs."""

    directory = _run_dir(run_id)
    if not directory.is_dir():
        _usage_error(f"run not found: {run_id}")
    if (source_file is None and not outcome) or (
        source_file is not None and outcome is not None
    ):
        _usage_error("provide exactly one explicit --outcome or --file")
    imported_from = "explicit_cli"
    if source_file is not None:
        try:
            source = _read_json(source_file)
        except (OSError, json.JSONDecodeError) as error:
            _usage_error(f"feedback file is unreadable: {error}")
        if source is None:
            _usage_error("feedback file must contain one JSON object")
        if source.get("run_id") not in {None, run_id}:
            _usage_error("feedback file run_id does not match the requested run")
        outcome = str(source.get("outcome") or "")
        attempt_id = str(source.get("attempt_id") or attempt_id or "") or None
        review_minutes = cast(float | None, source.get("review_minutes"))
        correction_summary = cast(str | None, source.get("correction_summary"))
        linked_reference = cast(str | None, source.get("linked_reference"))
        notes = cast(str | None, source.get("notes"))
        raw_trace = source.get("full_trace_opened")
        full_trace_opened = raw_trace if isinstance(raw_trace, bool) else None
        imported_from = "explicit_local_file"
    if not outcome:
        _usage_error("feedback file requires an outcome")
    selected = _verification_attempt_id(directory, attempt_id)
    try:
        human_outcome = make_human_outcome(
            run_id=run_id,
            attempt_id=selected,
            outcome=outcome,
            review_minutes=review_minutes,
            full_trace_opened=full_trace_opened,
            correction_summary=correction_summary,
            linked_reference=linked_reference,
            notes=notes,
            imported_from=imported_from,
        )
        appended = append_human_outcome(
            directory / "human-outcomes.jsonl", human_outcome
        )
        adverse = outcome in {"false_acceptance", "reverted", "reopened_defect"}
        invalidation = (
            _quarantine_adverse_outcome(
                directory=directory,
                run_id=run_id,
                attempt_id=selected,
                outcome_id=human_outcome.outcome_id,
                outcome_kind=outcome,
            )
            if adverse
            else None
        )
        configuration = _load_config()
        economics = configuration.get("economics")
        economics_policy = (
            economics.get("policy") if isinstance(economics, Mapping) else None
        )
        review_rate = (
            economics_policy.get("human_review_cost_per_minute")
            if isinstance(economics_policy, Mapping)
            else None
        )
        metrics = build_supervision_metrics(
            run_id=run_id,
            outcomes=load_human_outcomes(directory / "human-outcomes.jsonl"),
            decisions=_verification_decisions(directory),
            evidence_expansion_count=len(
                list((directory / "verification").glob("*-focused-probes.json"))
            ),
            review_cost_per_minute=(
                float(review_rate) if isinstance(review_rate, (int, float)) else None
            ),
            execution_cost=_verification_execution_cost(directory),
        )
        metrics_path = persist_supervision_metrics(directory, metrics)
    except (
        OSError,
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as error:
        _usage_error(f"human outcome was rejected: {error}")
    document = {
        "schema_version": "villani.human_outcome_import_result.v1",
        "appended": appended,
        "outcome": human_outcome.model_dump(mode="json"),
        "metrics_path": metrics_path.relative_to(directory).as_posix(),
        "qualification_invalidation": (
            invalidation.model_dump(mode="json") if invalidation else None
        ),
        "passive_monitoring": False,
    }
    if json_output:
        typer.echo(json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True))
    else:
        console.print(
            f"Recorded {human_outcome.outcome}; review time "
            f"{human_outcome.review_time_accounting_status}."
        )
        if invalidation is not None:
            console.print("The exact agent system was quarantined from automatic use.")


@verification_app.command("feedback")
def verification_feedback(
    run_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show only explicitly imported local outcomes for a run."""

    directory = _run_dir(run_id)
    if not directory.is_dir():
        _usage_error(f"run not found: {run_id}")
    try:
        outcomes = load_human_outcomes(directory / "human-outcomes.jsonl")
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
        _usage_error(f"human outcomes are unavailable: {error}")
    document = [item.model_dump(mode="json") for item in outcomes]
    if json_output:
        typer.echo(json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True))
    else:
        if not outcomes:
            console.print("No explicit human outcome has been imported.")
        for item in outcomes:
            review = (
                f"{item.review_minutes:g} review minutes"
                if item.review_minutes is not None
                else "review time unknown"
            )
            console.print(f"{item.recorded_at.isoformat()} · {item.outcome} · {review}")


@verification_app.command("metrics")
def verification_metrics(
    run_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show persisted local supervision metrics without inventing unknown values."""

    directory = _run_dir(run_id)
    try:
        metrics = load_supervision_metrics(directory / "supervision-metrics.json")
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
        _usage_error(f"supervision metrics are unavailable: {error}")
    document = redact_data(metrics.model_dump(mode="json"))
    if json_output:
        typer.echo(json.dumps(document, ensure_ascii=False, sort_keys=True))
    else:
        minutes = (
            f"{metrics.explicit_review_minutes:g} min"
            if metrics.explicit_review_minutes is not None
            else "unknown"
        )
        console.print(
            f"Review {minutes} · false acceptance {metrics.false_acceptance_count} · "
            f"verification cost {metrics.verification_cost.accounting_status}"
        )


@verification_app.command("gate-d")
def verification_gate_d(
    input_path: Path = typer.Option(
        ..., "--input", exists=True, dir_okay=False, readable=True
    ),
    output: Path | None = typer.Option(None, "--output", dir_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Evaluate matched frozen evidence for Gate D; empty evidence stays insufficient."""

    try:
        source = _read_json(input_path)
        if source is None or not isinstance(source.get("arms"), list):
            raise ValueError("Gate D input requires an arms array")
        arms = [GateDArm.model_validate(item) for item in source["arms"]]
        raw_stamp = source.get("generated_at")
        generated_at = (
            datetime.fromisoformat(str(raw_stamp).replace("Z", "+00:00"))
            if raw_stamp
            else None
        )
        references = source.get("evidence_references")
        report = evaluate_gate_d(
            arms=arms,
            generated_at=generated_at,
            evidence_references=(
                [str(item) for item in references]
                if isinstance(references, list)
                else []
            ),
        )
        if output is not None:
            write_json_atomic(output.expanduser().resolve(), report)
    except (
        OSError,
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as error:
        _usage_error(f"Gate D evidence was rejected: {error}")
    document = report.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(document, ensure_ascii=False, sort_keys=True))
    else:
        console.print(f"Gate D: {report.status} · policy {report.policy_version}")
        for check in report.checks:
            console.print(f"- {check.check}: {check.status} — {check.reason}")
    if report.status == "FAIL":
        raise typer.Exit(1)
    if report.status == "INSUFFICIENT_EVIDENCE":
        raise typer.Exit(2)


@policy_app.command("explain")
def policy_explain(
    task: str | None = typer.Argument(
        None, help="Task instruction. Omit for legacy configuration precedence."
    ),
    repo: Path | None = typer.Option(
        None, "--repo", help="Git repository; defaults to the current repository."
    ),
    success_criteria: str | None = typer.Option(None, "--success-criteria"),
    preset: str | None = typer.Option(None, "--preset"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Explain task classification and routing without a coding attempt."""

    configuration = _load_config()
    if task is not None:
        repository = _resolve_run_repository(repo)
        try:
            explanation = build_policy_preview(
                task=task,
                repository=repository,
                success_criteria=success_criteria or task,
                configuration=configuration,
                preset=preset,
            )
        except (
            OSError,
            TypeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as error:
            message = (
                _validation_message(error)
                if isinstance(error, ValidationError)
                else str(error)
            )
            _usage_error(f"policy preview failed: {message}")
        if json_output:
            typer.echo(
                json.dumps(
                    redact_data(explanation),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        raw = explanation["raw_classification"]
        effective = explanation["effective_classification"]
        route = explanation["selected_coding_route"]
        verifier = explanation["selected_verifier_route"]
        cost = explanation["estimated_cost"]
        console.print(
            f"Raw classification: {raw['difficulty']} difficulty, {raw['risk']} risk, "
            f"confidence {float(raw['confidence']):.2f}"
        )
        console.print(
            f"Effective classification: {effective['difficulty']} difficulty, "
            f"{effective['risk']} risk, confidence {float(effective['confidence']):.2f}"
        )
        adjustments = explanation["adjustments"]
        console.print(
            "Adjustments: "
            + (
                "; ".join(
                    f"{item['field']} {item['before']} -> {item['after']} ({item['rule_id']})"
                    for item in adjustments
                )
                if adjustments
                else "none"
            )
        )
        console.print(
            f"Coding route: {route['backend'] or 'none'} / {route['model'] or 'none'} - "
            f"{route['reason']}"
        )
        for backend in explanation.get("backend_explanations", []):
            reserve = backend.get("reserve_impact") or {}
            wilson = backend.get("wilson_lower_bound")
            console.print(
                f"Backend {backend['backend']}: configured={backend['configured_score']}; "
                f"effective={backend['effective_score']}; "
                f"provenance={backend['score_provenance']}; "
                f"confidence={backend['capability_confidence']}; "
                f"uncertainty_penalty={backend['uncertainty_penalty']}; "
                f"empirical_status={backend['empirical_status']}; "
                f"samples={backend['sample_count']}; "
                f"wilson_lower_bound={wilson if wilson is not None else 'unknown'}; "
                f"required={backend['required_score']}; eligible={backend['eligibility']}; "
                f"cost={backend['estimated_cost'] if backend['estimated_cost'] is not None else 'unknown'} "
                f"({backend['cost_accounting_status']}); "
                f"duration_ms={backend['estimated_duration_ms'] if backend['estimated_duration_ms'] is not None else 'unknown'} "
                f"({backend['duration_accounting_status']}); "
                f"reserve_satisfied={reserve.get('reserve_satisfied', 'unknown')}; "
                "rejections="
                + (
                    ", ".join(backend["rejection_reasons"])
                    if backend["rejection_reasons"]
                    else "none"
                ),
                soft_wrap=True,
            )
        stage = route.get("stage_budget_projection") or {}
        progress = route.get("credible_progress_assessment") or {}
        sequence = route.get("empirical_sequence") or {}
        route_plan = route.get("route_plan") or {}
        route_economics = route_plan.get("sequence_economics") or {}
        console.print(
            f"Decision details: action={route['action']}; "
            f"retry_allowed={route.get('retry_allowed')}; "
            f"retry_reason={route.get('retry_reason_code') or 'not_applicable'}; "
            f"credible_progress={progress.get('credible_progress', 'not_assessed')}; "
            f"progress_reasons={','.join(progress.get('reason_codes', [])) or 'none'}; "
            f"next_higher={route.get('next_higher_backend') or 'none'}; "
            f"override={route.get('override_status', False)}; "
            f"reserve_satisfied={stage.get('reserve_satisfied', 'unknown')}"
        )
        console.print(
            "Stage budget: "
            f"action_cost={stage.get('projected_action_cost', 'unknown')}; "
            f"action_wall_time_ms={stage.get('projected_action_wall_time', 'unknown')}; "
            f"required_reserve_cost={stage.get('required_reserve_cost', 'unknown')}; "
            f"required_reserve_wall_time_ms={stage.get('required_reserve_wall_time', 'unknown')}; "
            f"missing_inputs={','.join(stage.get('missing_inputs', [])) or 'none'}"
        )
        console.print(
            "Empirical sequence: "
            f"status={sequence.get('optimizer_status', 'not_available')}; "
            f"chosen={' -> '.join(sequence.get('chosen_sequence', [])) or 'none'}; "
            f"fallback={sequence.get('fallback_policy_version') or 'none'}; "
            f"missing_inputs={','.join(sequence.get('missing_inputs', [])) or 'none'}"
        )
        if route_plan:
            console.print(route_plan.get("explanation"))
            console.print(
                "Accepted-change route: "
                f"first={route_plan.get('selected_first_system') or 'none'}; "
                f"fallbacks={' -> '.join(route_plan.get('ordered_fallbacks', [])) or 'none'}; "
                f"selection={route_plan.get('selection_mode')}; "
                f"conservative_probability={route_economics.get('conservative_success_probability', 'unknown')}; "
                f"expected_accepted_change_cost={route_economics.get('expected_accepted_change_cost', 'unknown')}; "
                f"accounting={route_economics.get('accounting_status', 'unknown')}; "
                f"policy={route_plan.get('policy_version')}; "
                f"unknowns={','.join(route_plan.get('unknowns', [])) or 'none'}"
            )
        selected_verifier = verifier.get("selected") or {}
        console.print(
            f"Verifier route: {selected_verifier.get('route') or 'none'}; "
            f"authority={selected_verifier.get('authority') or 'none'}"
        )
        console.print(
            f"Estimated cost: {cost['value'] if cost['value'] is not None else 'unknown'} "
            f"({cost['status']})"
        )
        console.print(
            "Uncertainty: "
            + "; ".join(str(item) for item in explanation["uncertainty"])
        )
        return

    resolved, precedence = resolve_routing_configuration(configuration)
    active = resolved.get("active_policy")
    lkg = resolved.get("last_known_good_policy")
    source = (
        "active_policy"
        if isinstance(active, Mapping) and active.get("state") == "active"
        else "last_known_good_policy"
        if isinstance(lkg, Mapping) and lkg.get("state") == "active"
        else "bootstrap_policy"
    )
    explanation = {
        "schema_version": "villani.policy_resolution_explain.v1",
        "mode": resolved.get("mode", "observe"),
        "applied_precedence": list(precedence),
        "policy_fallback_source": source,
        "resolved_configuration": redact_data(resolved),
        "step_level_routing": False,
    }
    if json_output:
        typer.echo(json.dumps(explanation, sort_keys=True))
    else:
        console.print(f"Mode: {explanation['mode']}")
        console.print(f"Precedence: {' -> '.join(precedence)}")
        console.print(f"Policy source: {source}")


@policy_app.command("use")
def policy_use(preset: str = typer.Argument(...)) -> None:
    """Select the default public preset for future runs."""

    configuration = _load_config()
    try:
        selected = normalize_policy_preset(preset)
        updated = configure_policy_preset(configuration, selected)
        _write_config(_config_path(), updated)
    except (OSError, ValueError) as error:
        _usage_error(f"cannot select policy preset: {error}")
    definition = next(item for item in POLICY_PRESETS if item.preset_id == selected)
    console.print(f"Policy preset: {definition.label} - {definition.description}")


@policy_app.command("list")
def policy_list(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the public policy presets without internal routing mode names."""

    rows = policy_preset_rows(_load_config())
    if json_output:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        return
    for row in rows:
        selected = " (selected)" if row["active"] else ""
        console.print(f"{row['label']}{selected}: {row['description']}")


@policy_app.command("simulate")
def policy_simulate(
    preset: str = typer.Option(..., "--preset"),
    runs_root: Path | None = typer.Option(None, "--runs-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Evaluate route changes against recorded runs without changing live policy."""

    configuration = _load_config()
    try:
        selected = normalize_policy_preset(preset)
        backends = _load_backends(configuration)
        snapshot = _capability_snapshot(refresh=True)
        report = simulate_historical_runs(
            runs_root=(runs_root or _runs_root()).expanduser().resolve(),
            configuration=configuration,
            backends=backends,
            snapshot=snapshot,
            preset=selected,
        )
    except (
        OSError,
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"policy simulation failed: {message}")
    if json_output:
        typer.echo(
            json.dumps(
                redact_data(report), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
        return
    difference = report["estimated_cost_differences"]
    console.print(
        f"Historical simulation: evaluated={report['tasks_evaluated']}; "
        f"affected={report['tasks_affected']}; route changes={len(report['route_changes'])}"
    )
    console.print(
        f"Estimated cost difference status: {difference['status']}; "
        f"simulated-minus-recorded={difference['simulated_minus_recorded_total'] if difference['simulated_minus_recorded_total'] is not None else 'unknown'}"
    )
    console.print("Outcome evidence limitation: simulated routes were not executed.")
    console.print("No causal savings or counterfactual success claim is supported.")


def _load_route_policy(path: Path) -> RoutePolicy:
    return RoutePolicy.model_validate_json(path.read_text(encoding="utf-8"))


def _load_historical_route_cases(path: Path) -> list[HistoricalRouteCase]:
    value = json.loads(path.read_text(encoding="utf-8"))
    raw_cases = value.get("cases") if isinstance(value, Mapping) else value
    if not isinstance(raw_cases, list):
        raise ValueError(
            "historical route replay input must be a list or an object with cases"
        )
    return [HistoricalRouteCase.model_validate(item) for item in raw_cases]


@policy_app.command("economics-evaluate")
def policy_economics_evaluate(
    cases: Path = typer.Option(
        ...,
        "--cases",
        exists=True,
        dir_okay=False,
        help="Frozen point-in-time historical route cases.",
    ),
    proposed_policy: Path = typer.Option(
        ...,
        "--proposed-policy",
        exists=True,
        dir_okay=False,
        help="Deterministic proposed route-policy document.",
    ),
    output: Path | None = typer.Option(None, "--output", dir_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Replay a proposed route policy without starting an attempt or activating it."""

    try:
        configured = route_policy_from_configuration(_load_config())
        store = RoutePolicyStore()
        active = store.active_policy(configured)
        proposed = _load_route_policy(proposed_policy)
        evaluation = evaluate_route_policy(
            _load_historical_route_cases(cases),
            active_policy=active,
            proposed_policy=proposed,
        )
        if output is not None:
            write_json_atomic(output.expanduser().resolve(), evaluation)
    except (
        OSError,
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"route-policy evaluation failed: {message}")
    document = redact_data(evaluation.model_dump(mode="json"))
    if json_output:
        typer.echo(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
        return
    console.print(
        f"Route policy evaluation: {evaluation.evaluation_id}; "
        f"cases={evaluation.frozen_case_count}; safe_to_publish={evaluation.safe_to_publish}"
    )
    console.print(
        "Checks: "
        f"reliability_non_decreasing={evaluation.conservative_reliability_non_decreasing}; "
        f"false_acceptance_exposure_non_increasing={evaluation.false_acceptance_exposure_non_increasing}"
    )
    console.print("Rejections: " + ("; ".join(evaluation.rejection_reasons) or "none"))
    if output is not None:
        console.print(f"Evaluation artifact: {output.expanduser().resolve()}")


@policy_app.command("economics-status")
def policy_economics_status(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the exact active accepted-change economics policy."""

    try:
        configured = route_policy_from_configuration(_load_config())
        store = RoutePolicyStore()
        publication = store.active_publication()
        policy = publication.policy if publication is not None else configured
    except (
        OSError,
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"route-policy status failed: {message}")
    document = {
        "schema_version": "villani.route_policy_status.v1",
        "active_policy": policy.model_dump(mode="json"),
        "published": publication is not None,
        "publication": publication.model_dump(mode="json") if publication else None,
    }
    if json_output:
        typer.echo(
            json.dumps(
                redact_data(document), ensure_ascii=False, indent=2, sort_keys=True
            )
        )
        return
    console.print(
        f"Active accepted-change policy: {policy.policy_version}; "
        f"strategy={policy.strategy}; published={publication is not None}"
    )


@policy_app.command("economics-publish")
def policy_economics_publish(
    policy_path: Path = typer.Option(..., "--policy", exists=True, dir_okay=False),
    evaluation_path: Path = typer.Option(
        ..., "--evaluation", exists=True, dir_okay=False
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Publish only an exactly evaluated, reliability-preserving deterministic policy."""

    try:
        policy = _load_route_policy(policy_path)
        evaluation = RoutePolicyEvaluation.model_validate_json(
            evaluation_path.read_text(encoding="utf-8")
        )
        publication = RoutePolicyStore().publish(policy, evaluation)
    except (
        OSError,
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"route-policy publication refused: {message}")
    if json_output:
        typer.echo(
            json.dumps(
                redact_data(publication.model_dump(mode="json")),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    console.print(
        f"Published accepted-change policy {publication.policy.policy_version} "
        f"from evaluation {publication.evaluation_id}."
    )


@policy_app.command("economics-rollback")
def policy_economics_rollback(
    target_version: str | None = typer.Option(None, "--to"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Instantly move the active pointer to a previously published policy."""

    try:
        publication = RoutePolicyStore().rollback(target_version=target_version)
    except (
        OSError,
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"route-policy rollback failed: {message}")
    if json_output:
        typer.echo(
            json.dumps(
                redact_data(publication.model_dump(mode="json")),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    console.print(f"Active accepted-change policy: {publication.policy.policy_version}")


@evaluate_app.command("replay")
def evaluate_replay(
    input_path: Path = typer.Option(..., "--input", exists=True, dir_okay=False),
    json_output: Path = typer.Option(..., "--json-output"),
    markdown_output: Path = typer.Option(..., "--markdown-output"),
    minimum_samples: int = typer.Option(5, "--minimum-samples", min=2),
) -> None:
    """Replay fixture observations without publishing or activating a policy."""

    try:
        report = replay_file(
            input_path,
            json_output=json_output,
            markdown_output=markdown_output,
            minimum_sample_size=minimum_samples,
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        _usage_error(f"offline evaluation failed: {error}")
    evaluation = report["evaluation"]
    console.print(
        f"Offline evaluation: raw={evaluation['raw_count']}; "
        f"observed={evaluation['observed_count']}; censored={evaluation['censored_count']}"
    )
    console.print(f"JSON report: {json_output}")
    console.print(f"Markdown report: {markdown_output}")


@eval_app.command("init")
def evaluation_init(
    suite: Path = typer.Argument(..., help="New evaluation suite directory."),
    title: str = typer.Option(..., "--title"),
    suite_id: str | None = typer.Option(None, "--suite-id"),
    randomization_seed: str | None = typer.Option(None, "--randomization-seed"),
    synthetic_fixture: bool = typer.Option(
        False,
        "--synthetic-fixture",
        help="Mark test-only data that can never count toward Founder Gate evidence.",
    ),
    confidentiality: str = typer.Option("internal", "--confidentiality"),
    measured_power_watts: float | None = typer.Option(
        None, "--measured-power-watts", min=0.001
    ),
    electricity_price_per_kwh: float | None = typer.Option(
        None, "--electricity-price-per-kwh", min=0
    ),
    currency: str | None = typer.Option(None, "--currency"),
) -> None:
    """Initialize a local, non-monitoring evaluation workspace."""

    try:
        created = init_suite(
            suite,
            title=title,
            suite_id=suite_id,
            randomization_seed=randomization_seed,
            evidence_kind=(
                "synthetic_fixture" if synthetic_fixture else "real_founder_work"
            ),
            confidentiality=confidentiality,
            measured_power_watts=measured_power_watts,
            electricity_price_per_kwh=electricity_price_per_kwh,
            currency=currency,
        )
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"evaluation initialization failed: {error}")
    console.print(f"Evaluation suite initialized: {created.suite_id}")
    console.print("Passive monitoring: disabled")


@eval_app.command("import-baseline")
def evaluation_import_baseline(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    repo: Path = typer.Option(..., "--repo", exists=True, file_okay=False),
    commit: str = typer.Option("HEAD", "--commit"),
    include: list[str] | None = typer.Option(None, "--include"),
    exclude: list[str] | None = typer.Option(None, "--exclude"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Capture a secret-screened immutable Git snapshot and prove restoration."""

    try:
        snapshot = import_baseline(
            suite,
            repository=repo,
            commit=commit,
            include_patterns=tuple(include or ()),
            exclude_patterns=tuple(exclude or ()),
        )
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        _usage_error(f"baseline import failed: {error}")
    if json_output:
        typer.echo(
            json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True)
        )
        return
    console.print(f"Baseline digest: {snapshot.baseline_digest}")
    console.print(f"Files captured: {snapshot.file_count}; restore verified: yes")


@eval_app.command("add-task")
def evaluation_add_task_command(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    baseline: str = typer.Option(..., "--baseline"),
    task: str | None = typer.Argument(None, help="Verbatim task instruction."),
    task_file: Path | None = typer.Option(None, "--task-file", dir_okay=False),
    task_id: str | None = typer.Option(None, "--task-id"),
    success_criteria: list[str] | None = typer.Option(None, "--success-criteria"),
    validation_command: list[str] | None = typer.Option(None, "--validation-command"),
    hidden_validation_command: list[str] | None = typer.Option(
        None, "--hidden-validation-command"
    ),
    setup_command: list[str] | None = typer.Option(None, "--setup-command"),
    hidden_check_file: list[Path] | None = typer.Option(None, "--hidden-check-file"),
    future_context_file: list[Path] | None = typer.Option(
        None, "--future-context-file"
    ),
    file_change: str = typer.Option("required", "--file-change"),
    allowed_path: list[str] | None = typer.Option(None, "--allowed-path"),
    forbidden_path: list[str] | None = typer.Option(None, "--forbidden-path"),
    risk: list[str] | None = typer.Option(None, "--risk"),
    category: list[str] | None = typer.Option(None, "--category"),
    confidentiality: str = typer.Option("internal", "--confidentiality"),
    captured_by: str = typer.Option("founder", "--captured-by"),
    source_reference: str = typer.Option("founder_work", "--source-reference"),
) -> None:
    """Add a real task while keeping future and hidden material evaluator-only."""

    try:
        task_text = resolve_task_input(task, task_file)
        criteria = [value for value in (success_criteria or []) if value.strip()]
        if not criteria:
            raise ValueError("at least one --success-criteria is required")
        visible = [
            ValidationCommand(
                validation_id=f"visible_{index:03d}",
                argv=list(parse_manual_command(value)),
                visibility="runner_visible",
            )
            for index, value in enumerate(validation_command or [], start=1)
        ]
        hidden = [
            ValidationCommand(
                validation_id=f"hidden_{index:03d}",
                argv=list(parse_manual_command(value)),
                visibility="evaluator_only",
            )
            for index, value in enumerate(hidden_validation_command or [], start=1)
        ]
        if not visible and not hidden:
            raise ValueError(
                "at least one authoritative validation command is required"
            )
        setup = [
            SetupCommand(
                setup_id=f"setup_{index:03d}",
                argv=list(parse_manual_command(value)),
            )
            for index, value in enumerate(setup_command or [], start=1)
        ]
        created = evaluation_add_task(
            suite,
            baseline_digest=baseline,
            verbatim_task=task_text,
            success_criteria=criteria,
            validation=(*visible, *hidden),
            task_id=task_id,
            allowed_setup=setup,
            file_change_requirement=FileChangeRequirement(
                behavior=cast(Any, file_change),
                allowed_path_prefixes=list(allowed_path or []),
                forbidden_path_prefixes=list(forbidden_path or []),
            ),
            captured_by=captured_by,
            source_reference=source_reference,
            risk_labels=tuple(risk or ()),
            category_labels=tuple(category or ()),
            hidden_check_files=tuple(hidden_check_file or ()),
            future_context_files=tuple(future_context_file or ()),
            confidentiality=confidentiality,
        )
    except (
        OSError,
        TypeError,
        ValueError,
        RuntimeError,
        TaskInputError,
        ValidationError,
    ) as error:
        _usage_error(f"task capture failed: {error}")
    console.print(f"Task captured: {created.task_id}")
    console.print(f"Task digest: {created.content_digest}")


@eval_app.command("validate")
def evaluation_validate(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate snapshot restoration, redaction, and runner non-leakage."""

    try:
        report = validate_suite(suite)
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"suite validation failed: {error}")
    if json_output:
        typer.echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        console.print(
            f"Suite validation: {'PASS' if report['valid'] else 'FAIL'}; tasks={report['task_count']}"
        )
        for issue in report["issues"]:
            console.print(f"- {issue['code']}: {issue['message']}")
    if not report["valid"]:
        raise typer.Exit(1)


@eval_app.command("freeze")
def evaluation_freeze(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    disclosure_complete: bool = typer.Option(
        False,
        "--disclosure-complete",
        help="Attest that unknown and exclusion reporting will be complete.",
    ),
) -> None:
    """Freeze content-addressed task and suite versions."""

    try:
        frozen = freeze_suite(suite, disclosure_complete=disclosure_complete)
    except (OSError, TypeError, ValueError, RuntimeError, ValidationError) as error:
        _usage_error(f"suite freeze failed: {error}")
    console.print(f"Frozen suite digest: {frozen.content_digest}")


@eval_app.command("export")
def evaluation_export(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    output: Path = typer.Option(..., "--output", dir_okay=False),
) -> None:
    """Export runner-safe task payloads and the actual allowed code."""

    try:
        path = export_portable_suite(suite, output)
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        _usage_error(f"evaluation export failed: {error}")
    console.print(f"Portable evaluation bundle: {path}")
    console.print("Evaluator-only material included: no")


@eval_app.command("run")
def evaluation_run(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    arms: str = typer.Option("direct,villani", "--arms"),
    repetitions: int = typer.Option(1, "--repetitions", min=1),
) -> None:
    """Run randomized paired trials sequentially and resume without duplicates."""

    try:
        selected_arms = tuple(
            value.strip() for value in arms.split(",") if value.strip()
        )
        executor = ProductArmExecutor(
            configuration=_load_config(),
            villani_home=_home(),
            suite_directory=suite,
        )
        result = run_paired_suite(
            suite,
            arms=selected_arms,
            repetitions=repetitions,
            executor=executor,
        )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (OSError, TypeError, ValueError, RuntimeError, ValidationError) as error:
        _usage_error(f"paired evaluation failed: {error}")
    console.print(
        f"Paired evaluation: completed={result['completed']}; skipped={result['skipped']}; excluded={result['excluded']}"
    )


@eval_app.command("review-queue")
def evaluation_review_queue(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List unreviewed trial bundles without revealing their arm."""

    try:
        reviewed = latest_reviews(load_reviews(suite))
        rows = [
            {
                "trial_id": trial.trial_id,
                "patch": f"trials/{trial.trial_id}/execution/candidate.patch",
                "verification": f"trials/{trial.trial_id}/verification/verification-result.json",
                "arm_hidden": True,
            }
            for trial in load_trials(suite)
            if trial.status == "completed" and trial.trial_id not in reviewed
        ]
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"review queue failed: {error}")
    if json_output:
        typer.echo(json.dumps(rows, indent=2, sort_keys=True))
        return
    for row in rows:
        console.print(f"{row['trial_id']}: {row['patch']} (arm hidden)")


@eval_app.command("review")
def evaluation_review(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    trial_id: str = typer.Argument(...),
    outcome: str = typer.Option(..., "--outcome"),
    review_minutes: float = typer.Option(..., "--review-minutes", min=0),
    reviewer_id: str = typer.Option(..., "--reviewer"),
    correction_summary: str | None = typer.Option(None, "--correction-summary"),
    severity: str = typer.Option("none", "--severity"),
    unblinded: bool = typer.Option(False, "--unblinded"),
    amend: str | None = typer.Option(None, "--amend"),
    later_rollback: bool | None = typer.Option(
        None, "--later-rollback/--no-later-rollback"
    ),
    reopened_defect: bool | None = typer.Option(
        None, "--reopened-defect/--not-reopened"
    ),
) -> None:
    """Append a human review or amendment; prior labels are never overwritten."""

    try:
        review = append_review(
            suite,
            trial_id=trial_id,
            reviewer_id=reviewer_id,
            outcome=outcome,
            review_minutes=review_minutes,
            blinded=not unblinded,
            arm_revealed_during_review=unblinded,
            correction_summary=correction_summary,
            severity=severity,
            later_rollback=later_rollback,
            reopened_defect=reopened_defect,
            amends_review_id=amend,
        )
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"human review failed: {error}")
    console.print(f"Human review appended: {review.review_id}")


@eval_app.command("report")
def evaluation_report(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    json_output: Path | None = typer.Option(None, "--json-output", dir_okay=False),
    markdown_output: Path | None = typer.Option(
        None, "--markdown-output", dir_okay=False
    ),
    html_output: Path | None = typer.Option(None, "--html-output", dir_okay=False),
) -> None:
    """Generate JSON, Markdown, and HTML evaluation reports."""

    try:
        report, json_path, markdown_path, html_path = write_reports(
            suite,
            json_output=json_output,
            markdown_output=markdown_output,
            html_output=html_output,
        )
    except (OSError, TypeError, ValueError, RuntimeError, ValidationError) as error:
        _usage_error(f"evaluation report failed: {error}")
    console.print(f"Gate B: {report.founder_gate_status}")
    console.print(f"JSON: {json_path}")
    console.print(f"Markdown: {markdown_path}")
    console.print(f"HTML: {html_path}")


@eval_app.command("gate")
def evaluation_gate(
    suite: Path = typer.Argument(..., exists=True, file_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Return PASS, FAIL, or INSUFFICIENT_EVIDENCE for Founder Gate B."""

    try:
        report = build_report(suite)
    except (OSError, TypeError, ValueError, RuntimeError, ValidationError) as error:
        _usage_error(f"Founder Gate failed: {error}")
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "schema_version": "villani.founder_gate.v1",
                    "status": report.founder_gate_status,
                    "checks": [
                        item.model_dump(mode="json")
                        for item in report.founder_gate_checks
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        console.print(report.founder_gate_status)
    if report.founder_gate_status == "FAIL":
        raise typer.Exit(1)
    if report.founder_gate_status == "INSUFFICIENT_EVIDENCE":
        raise typer.Exit(2)


CONFIG_HEADER = """# Villani local-first configuration.
# Add backends with `villani backend add`; store secret values in environment
# variables and put only the variable name in api_key_env.
"""
DEFAULT_CONFIG: dict[str, Any] = {
    "public_policy": {
        "version": PUBLIC_POLICY_VERSION,
        "preset": "balanced",
        "selection_preference": "balanced",
    },
    "model_management": {
        "version": "villani-model-lifecycle-v1",
        "bootstrap_default": None,
    },
    "routing": {
        "mode": "observe",
        "permissions": {"user_enforce": False, "workspace_enforce": False},
        "emergency_disabled": False,
    },
    "policy": {
        "version": "bootstrap_v1",
        "easy_min_capability": 20,
        "medium_min_capability": 50,
        "hard_min_capability": 80,
        "economy_confidence_threshold": 0.80,
        "conservative_confidence_threshold": 0.65,
        "max_same_backend_retries": 1,
        "classifier_retry_limit": 1,
        "verifier_retry_limit": 1,
        "repository_validation_retry_limit": 1,
        "accepted_candidates_required": 1,
        "allow_constraint_violations": False,
        "allow_no_change_retry": False,
        "stage_reserves": {
            "verification_fraction": 0.10,
            "strong_escalation_fraction": 0.30,
            "final_validation_fraction": 0.10,
            "selection_fraction": 0.05,
        },
    },
    "capabilities": {
        "minimum_empirical_samples": 20,
        "target_success_probability": 0.80,
        "minimum_empirical_wilson_lower_bound": None,
        "persisted_sequence_top_n": 100,
        "manual_uncertainty_penalty": 20,
        "bootstrap_uncertainty_penalty": 25,
        "observed_uncertainty_penalty_max": 15,
        "allow_manual_hard_task_qualification": False,
        "allow_bootstrap_threshold_bypass": False,
        "classifier_version": "task_classifier_v1",
        "verifier_version": "villani_ops_verifier_pipeline_v1",
        "scorer_version": "empirical_wilson_v1",
    },
    "qualification": {
        "schema_version": "villani.repository_qualification_configuration.v1",
        "policy": {
            "policy_version": "repository_qualification_v1",
            "minimum_qualified_observations": 20,
            "provisional_maximum_observations": 19,
            "wilson_z": 1.959963984540054,
            "task_wilson_thresholds": {
                "low": 0.60,
                "medium": 0.70,
                "high": 0.80,
            },
            "maximum_evidence_age_days": 180,
            "recent_reliability_window": 5,
            "approved_backoff_levels": [
                "exact_repository_task",
                "repository_category",
                "repository_wide",
            ],
            "compatible_repository_cohorts": {},
            "approved_repository_cohorts": [],
        },
    },
    "economics": {
        "schema_version": "villani.accepted_change_economics_configuration.v1",
        "policy": {
            "schema_version": "villani.route_policy.v1",
            "policy_version": "accepted_change_economics_v1",
            "strategy": "accepted_change_optimizer",
            "objective_version": "total_accepted_change_v1",
            "conservative_cost_statistic": "p90",
            "conservative_duration_statistic": "p90",
            "currency": "USD",
            "human_review_cost_per_minute": None,
            "latency_penalty_per_second": None,
            "allow_provisional_fallback": True,
            "require_complete_objective_for_comparison": True,
            "constraints": {
                "local_only": False,
                "prefer_local": False,
                "allowed_providers": [],
                "preferred_provider": None,
                "excluded_systems": [],
                "forced_system": None,
                "strongest_only": False,
                "maximum_known_cost_usd": None,
                "allowed_permission_profiles": [],
                "allow_experimental_forced": False,
            },
        },
        "constraints": {},
        "availability": {},
        "verification_cost_usd": None,
        "online_update": {"enabled": True},
    },
    "adaptive_verification": {
        "schema_version": "villani.adaptive_verification_configuration.v1",
        "policy_version": "adaptive_verification_v1",
        "standard_patch_line_limit": 200,
        "elevated_patch_line_limit": 600,
        "standard_changed_file_limit": 6,
        "elevated_changed_file_limit": 18,
        "sensitive_paths": [],
        "generated_artifact_paths": [],
        "require_independent_verifier_for_critical": True,
        "require_manual_review_when_proof_impossible": True,
        "minimum_independent_verifier_capability": 80,
        "historical_disagreement_window": 20,
    },
    "budgets": {
        "max_attempts": 3,
        "max_cost": None,
        "max_wall_time": None,
    },
    "delivery": {
        # Preserve the established quickstart: a bare `villani run` applies an
        # accepted low-risk patch. The controller still fails closed when this
        # explicit authority policy is absent or its requirements are unmet.
        "default_mode": "apply",
        "authority_policy": {
            "policy_version": "villani.default_delivery_authority.v1",
            "allow_automatic": True,
            "require_acceptance_eligible": True,
            "allowed_risks": ["low"],
        },
    },
    "verifier": {
        "invocation": "in_process",
        "no_llm": True,
        "backend": None,
        "timeout_seconds": 180,
        "max_tool_calls": 12,
        "base_url": None,
        "model": None,
    },
    "isolation": {
        "include_untracked_attempt_files": False,
        "keep_attempt_worktrees": False,
        "max_file_size_bytes": 52428800,
        "max_total_size_bytes": 524288000,
    },
    "execution_environment": {
        "provider": "inherit",
        "denied_variables": [],
        "sensitive_variables": [],
        "private_paths": [],
        "required": True,
    },
    "backends": {},
    "agent_systems": {
        "schema_version": "villani.agent_system_configuration.v1",
        "systems": {},
    },
}

_controller_builder: (
    Callable[[Mapping[str, Any], Callable[[Any], None] | None], ClosedLoopController]
    | None
) = None


def _home() -> Path:
    configured = os.environ.get("VILLANI_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".villani"
    )


def _config_path() -> Path:
    return _home() / "config.yaml"


def _runs_root() -> Path:
    return _home() / "runs"


def _usage_error(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(2)


def _validation_message(error: ValidationError) -> str:
    details = []
    for issue in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in issue.get("loc", ())) or "value"
        details.append(f"{location}: {issue.get('msg', 'invalid value')}")
    return "; ".join(details) or "invalid configuration"


def _write_config(path: Path, configuration: Mapping[str, Any]) -> None:
    migrated, _report = migrate_agent_system_configuration(configuration)
    write_configuration_atomic(path, migrated, header=CONFIG_HEADER)


def _load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.is_file():
        _usage_error(f"configuration not found at {path}; run `villani init`")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        _usage_error(f"cannot read configuration at {path}: {error}")
    if not isinstance(loaded, dict):
        _usage_error(f"configuration at {path} must be a YAML object")
    try:
        migrated, _report = migrate_agent_system_configuration(loaded)
    except (TypeError, ValueError, ValidationError) as error:
        _usage_error(f"agent-system configuration migration failed: {error}")
    return migrated


def _agent_system_registry():
    configuration = _load_config()
    try:
        return build_agent_system_registry(configuration, _load_backends(configuration))
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"agent-system configuration is invalid: {error}")


@agents_app.command("list")
def agents_list(
    json_output: bool = typer.Option(False, "--json", help="Emit canonical JSON."),
) -> None:
    """List complete systems; disabled systems remain visible but unselectable."""

    registry = _agent_system_registry()
    document = {
        "schema_version": "villani.agent_system_inventory.v1",
        "systems": [item.model_dump(mode="json") for item in registry.list()],
        "harnesses": [item.model_dump(mode="json") for item in registry.discoveries],
        "migration": registry.migration_report,
    }
    if json_output:
        typer.echo(
            json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True)
        )
        return
    for discovery in registry.discoveries:
        readiness = discovery.readiness
        console.print(
            f"detected {discovery.display_name}: "
            f"installed={'yes' if readiness.installed else 'no'}; "
            f"version={readiness.exact_version or 'unknown'}; "
            f"auth={readiness.authentication_status}; "
            f"protocol={readiness.protocol}; "
            f"conformance={readiness.conformance_status}; "
            f"qualification={readiness.qualification_state}; "
            f"repair={readiness.repair_action}",
            soft_wrap=True,
        )
    if not registry.list():
        console.print("No coding agent systems are configured.")
        return
    for identity in registry.list():
        console.print(
            f"{identity.route_name}: {identity.harness.display_name} "
            f"{identity.harness.version}; model={identity.model_provider.model_id}; "
            f"provider={identity.model_provider.provider}; "
            f"qualification={identity.qualification_status}; "
            f"auth={identity.readiness.authentication_status if identity.readiness else 'unknown'}; "
            f"enabled={'yes' if identity.production_enabled else 'no'}; "
            f"id={identity.system_id}",
            soft_wrap=True,
        )


@agents_app.command("inspect")
def agents_inspect(
    reference: str = typer.Argument(..., help="Route name or content-addressed ID."),
    json_output: bool = typer.Option(True, "--json/--no-json"),
) -> None:
    """Print one redacted canonical agent-system identity."""

    try:
        identity = _agent_system_registry().inspect(reference)
    except ValueError as error:
        _usage_error(str(error))
    document = redact_data(identity.model_dump(mode="json"))
    if json_output:
        typer.echo(json.dumps(document, ensure_ascii=False, sort_keys=True))
    else:
        console.print(
            f"{identity.route_name}: {identity.system_id}\n"
            f"  harness={identity.harness.harness_id}@{identity.harness.version}\n"
            f"  model={identity.model_provider.provider}/{identity.model_provider.model_id}\n"
            f"  qualification={identity.qualification_status}; "
            f"unknown={','.join(identity.unknown_fields) or 'none'}",
            soft_wrap=True,
        )


@agents_app.command("doctor")
def agents_doctor(
    reference: str | None = typer.Argument(None, help="Optional route name or ID."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Probe harness availability and protocol compatibility without model use."""

    try:
        reports = _agent_system_registry().doctor(reference)
    except ValueError as error:
        _usage_error(str(error))
    document = {
        "schema_version": "villani.agent_system_doctor_collection.v1",
        "reports": [item.model_dump(mode="json") for item in reports],
    }
    if json_output:
        typer.echo(
            json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True)
        )
    else:
        for report in reports:
            console.print(
                f"{report.system_id}: {'selectable' if report.selectable else 'not selectable'}"
            )
            for check in report.checks:
                console.print(f"- {check.name}: {check.status} - {check.message}")
    if reports and any(not item.selectable for item in reports):
        raise typer.Exit(1)


def _qualification_assessments(
    *,
    repository: Path,
    category: str,
    difficulty: str,
    risk: str,
    required_capabilities: Sequence[str],
    reference: str | None = None,
) -> tuple[
    dict[str, Any],
    Any,
    dict[str, Backend],
    Any,
    tuple[Any, ...],
]:
    configuration = _load_config()
    backends = _load_backends(configuration)
    registry = build_agent_system_registry(configuration, backends)
    identities = (
        (registry.inspect(reference),) if reference is not None else registry.list()
    )
    context = repository_qualification_context(repository)
    requested = qualification_task_profile(
        category, difficulty, risk, required_capabilities
    )
    store = QualificationStore(_home() / "qualification")
    assessments = []
    for identity in identities:
        backend_name = next(
            (
                name
                for name, candidate in registry.by_backend.items()
                if candidate.system_id == identity.system_id
            ),
            None,
        )
        backend = backends.get(backend_name) if backend_name else None
        assessments.append(
            assess_qualification(
                identity=identity,
                repository=context,
                requested_task=requested,
                configuration=configuration,
                store=store,
                backend_execution_selection=(
                    backend.execution_environment if backend is not None else None
                ),
            )
        )
    return configuration, registry, backends, context, tuple(assessments)


@agents_app.command("qualify")
def agents_qualify(
    reference: str = typer.Argument(..., help="Configured route name or system ID."),
    suite: Path | None = typer.Option(
        None, "--suite", help="Frozen evaluation-suite directory."
    ),
    trial: list[str] | None = typer.Option(
        None, "--trial", help="Evaluation trial ID; repeat for multiple trials."
    ),
    evidence: list[Path] | None = typer.Option(
        None,
        "--evidence",
        help="Validated qualification-observation JSON; repeat for multiple files.",
    ),
    category: str | None = typer.Option(None, "--category"),
    difficulty: str | None = typer.Option(None, "--difficulty"),
    risk: str | None = typer.Option(None, "--risk"),
    required_capability: list[str] | None = typer.Option(None, "--required-capability"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Append only evidence that passes the complete PT7 eligibility rules."""

    trial_ids = trial or []
    evidence_paths = evidence or []
    if not trial_ids and not evidence_paths:
        _usage_error("qualify requires at least one --trial or --evidence")
    if trial_ids and suite is None:
        _usage_error("--trial requires --suite")
    configuration = _load_config()
    backends = _load_backends(configuration)
    try:
        registry = build_agent_system_registry(configuration, backends)
        identity = registry.inspect(reference)
    except (TypeError, ValueError, ValidationError) as error:
        _usage_error(f"qualification system is invalid: {error}")
    store = QualificationStore(_home() / "qualification")
    appended: list[QualificationObservation] = []
    unchanged: list[str] = []
    try:
        for trial_id in trial_ids:
            observation = observation_from_evaluation_trial(
                suite,  # type: ignore[arg-type]
                trial_id=trial_id,
                identity=identity,
                category=category,
                difficulty=difficulty,
                risk=risk,
                required_capabilities=required_capability or (),
                runs_root=_runs_root(),
            )
            if store.append_observation(observation):
                appended.append(observation)
            else:
                unchanged.append(observation.observation_id)
        for path in evidence_paths:
            observation = QualificationObservation.model_validate_json(
                path.expanduser().resolve().read_text(encoding="utf-8")
            )
            if observation.eligible:
                raise ValueError(
                    f"{path} claims eligible evidence without a locally verified frozen "
                    "suite; ingest eligible evidence with --suite and --trial"
                )
            if observation.system.system_id != identity.system_id:
                raise ValueError(
                    f"{path} names a different complete agent-system identity"
                )
            expected_system = qualification_system_identity(
                identity,
                environment_fingerprint=(
                    observation.system.execution_environment_fingerprint
                ),
            )
            if observation.system != expected_system:
                raise ValueError(
                    f"{path} does not match the exact configured harness, model, "
                    "provider, protocol, software, and verification-policy identity"
                )
            if store.append_observation(observation):
                appended.append(observation)
            else:
                unchanged.append(observation.observation_id)
        snapshot = store.rebuild(
            policy=qualification_policy_from_configuration(configuration)
        )
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"qualification evidence was rejected: {error}")
    document = {
        "schema_version": "villani.qualification_ingest_result.v1",
        "system_id": identity.system_id,
        "appended": [item.model_dump(mode="json") for item in appended],
        "unchanged_observation_ids": unchanged,
        "eligible_appended": sum(item.eligible for item in appended),
        "excluded_appended": sum(not item.eligible for item in appended),
        "qualification_created_directly": False,
        "snapshot_digest": snapshot.snapshot_digest,
    }
    if json_output:
        typer.echo(
            json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True)
        )
    else:
        console.print(
            f"Recorded {len(appended)} new observation(s), including "
            f"{document['eligible_appended']} eligible and {document['excluded_appended']} excluded."
        )
        console.print(
            "Evidence was recorded; status is derived separately by `villani agents status`."
        )


@agents_app.command("status")
def agents_status(
    reference: str | None = typer.Argument(
        None, help="Optional configured route name or system ID."
    ),
    repo: Path = typer.Option(Path.cwd(), "--repo"),
    category: str = typer.Option("*", "--category"),
    difficulty: str = typer.Option("easy", "--difficulty"),
    risk: str = typer.Option("low", "--risk"),
    required_capability: list[str] | None = typer.Option(None, "--required-capability"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show evidence-backed repository/task qualification state."""

    try:
        _configuration, _registry, _backends, context, assessments = (
            _qualification_assessments(
                repository=repo,
                category=category,
                difficulty=difficulty,
                risk=risk,
                required_capabilities=required_capability or (),
                reference=reference,
            )
        )
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"qualification status is unavailable: {error}")
    document = {
        "schema_version": "villani.qualification_status_collection.v1",
        "repository_id": context.repository_id,
        "repository_head": context.head,
        "assessments": [item.model_dump(mode="json") for item in assessments],
    }
    if json_output:
        typer.echo(
            json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True)
        )
        return
    for assessment in assessments:
        stats = assessment.statistics
        rate = (
            f"{100 * stats.acceptance_rate:.1f}%"
            if stats.acceptance_rate is not None
            else "Unknown"
        )
        console.print(
            f"{assessment.route_name}: {assessment.state.upper()} — "
            f"sample={stats.sample_count}; observed acceptance={rate}; "
            f"last tested={stats.last_evidence_at or 'Unknown'}"
        )
        console.print(f"  {assessment.caveat}")
        console.print(f"  Doctor: {assessment.doctor_action}")
        console.print(f"  View evidence: {assessment.evidence_action}")


@agents_app.command("evidence")
def agents_evidence(
    reference: str = typer.Argument(..., help="Configured route name or system ID."),
    repo: Path = typer.Option(Path.cwd(), "--repo"),
    category: str = typer.Option("*", "--category"),
    difficulty: str = typer.Option("easy", "--difficulty"),
    risk: str = typer.Option("low", "--risk"),
    required_capability: list[str] | None = typer.Option(None, "--required-capability"),
    json_output: bool = typer.Option(True, "--json/--no-json"),
) -> None:
    """Inspect immutable observations, exclusions, drift, and backoff evidence."""

    try:
        _configuration, registry, _backends, context, assessments = (
            _qualification_assessments(
                repository=repo,
                category=category,
                difficulty=difficulty,
                risk=risk,
                required_capabilities=required_capability or (),
                reference=reference,
            )
        )
        identity = registry.inspect(reference)
        store = QualificationStore(_home() / "qualification")
        visible_repository_ids = {
            repository_id
            for backoff in assessments[0].backoff_evidence
            for repository_id in backoff.repository_ids
        } | {context.repository_id}
        observations = [
            item
            for item in store.load_observations()
            if item.system.route_name == identity.route_name
            and item.repository_id in visible_repository_ids
        ]
        invalidations = [
            item
            for item in store.load_invalidations()
            if item.route_name == identity.route_name
            and (
                item.repository_id is None
                or item.repository_id == context.repository_id
            )
        ]
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"qualification evidence is unavailable: {error}")
    document = {
        "schema_version": "villani.qualification_evidence_view.v1",
        "assessment": assessments[0].model_dump(mode="json"),
        "observations": [item.model_dump(mode="json") for item in observations],
        "invalidations": [item.model_dump(mode="json") for item in invalidations],
    }
    if json_output:
        typer.echo(
            json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True)
        )
    else:
        console.print_json(data=redact_data(document))


@agents_app.command("invalidate")
def agents_invalidate(
    reference: str = typer.Argument(..., help="Configured route name or system ID."),
    reason: str = typer.Option(..., "--reason"),
    detail: str = typer.Option(..., "--detail"),
    evidence_reference: str = typer.Option(..., "--evidence-reference"),
    evidence_digest: str | None = typer.Option(None, "--evidence-digest"),
    severity: str = typer.Option("severe", "--severity"),
    repo: Path | None = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Append an invalidation without deleting or rewriting qualification history."""

    configuration = _load_config()
    backends = _load_backends(configuration)
    try:
        registry = build_agent_system_registry(configuration, backends)
        identity = registry.inspect(reference)
        repository_id = (
            repository_qualification_context(repo).repository_id
            if repo is not None
            else None
        )
        now = datetime.now(timezone.utc)
        payload = {
            "recorded_at": now,
            "system_id": identity.system_id,
            "route_name": identity.route_name,
            "repository_id": repository_id,
            "reason": reason,
            "severity": severity,
            "evidence_reference": evidence_reference,
            "evidence_digest": evidence_digest,
            "detail": detail,
        }
        invalidation = QualificationInvalidation(
            **payload,
            invalidation_id=(
                "qinv_"
                + canonical_digest(
                    {
                        **payload,
                        "recorded_at": now.isoformat(),
                    }
                ).removeprefix("sha256:")
            ),
        )
        store = QualificationStore(_home() / "qualification")
        changed = store.append_invalidation(invalidation)
        snapshot = store.rebuild(
            policy=qualification_policy_from_configuration(configuration)
        )
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"qualification invalidation was rejected: {error}")
    document = {
        "schema_version": "villani.qualification_invalidation_result.v1",
        "changed": changed,
        "invalidation": invalidation.model_dump(mode="json"),
        "history_deleted": False,
        "snapshot_digest": snapshot.snapshot_digest,
    }
    if json_output:
        typer.echo(
            json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True)
        )
    else:
        console.print(
            f"Recorded invalidation {invalidation.invalidation_id}; history was preserved."
        )


@agents_app.command("gate-c")
def agents_gate_c(
    repo: Path = typer.Option(Path.cwd(), "--repo"),
    category: str = typer.Option("*", "--category"),
    difficulty: str = typer.Option("easy", "--difficulty"),
    risk: str = typer.Option("low", "--risk"),
    required_capability: list[str] | None = typer.Option(None, "--required-capability"),
    output: Path | None = typer.Option(None, "--output"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Calculate repository-specific Gate C and scorecards without ranking unknowns."""

    configuration = _load_config()
    backends = _load_backends(configuration)
    try:
        registry = build_agent_system_registry(configuration, backends)
        report = build_gate_c_report(
            identities=registry.list(),
            backends=backends,
            repository=repository_qualification_context(repo),
            requested_task=qualification_task_profile(
                category,
                difficulty,
                risk,
                required_capability or (),
            ),
            configuration=configuration,
            store=QualificationStore(_home() / "qualification"),
        )
        document = report.model_dump(mode="json")
        if output is not None:
            write_json_atomic(output.expanduser().resolve(), document)
    except (OSError, TypeError, ValueError, ValidationError) as error:
        _usage_error(f"Gate C could not be calculated: {error}")
    if json_output:
        typer.echo(
            json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True)
        )
    else:
        console.print(f"Gate C: {report.status}")
        for scorecard in report.scorecards:
            console.print(
                f"- {scorecard.system_name}: {scorecard.assessment.state}; "
                f"sample={scorecard.assessment.statistics.sample_count}; "
                f"accepted-as-is={scorecard.accepted_as_is}; failures={scorecard.failures}"
            )
        if report.unmatched_sample_warning:
            console.print(f"Warning: {report.unmatched_sample_warning}")
    if report.status == "FAIL":
        raise typer.Exit(1)
    if report.status == "INSUFFICIENT_EVIDENCE":
        raise typer.Exit(2)


def _load_backends(configuration: Mapping[str, Any]) -> dict[str, Backend]:
    raw = configuration.get("backends")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        _usage_error("config backends must be a mapping keyed by backend name")
    parsed: dict[str, Backend] = {}
    for name, value in raw.items():
        if not isinstance(value, Mapping):
            _usage_error(f"backend {name!r} must be a YAML object")
        try:
            parsed[str(name)] = Backend.model_validate(
                {"name": str(name), **dict(value)}
            )
        except ValidationError as error:
            _usage_error(f"backend {name!r} is invalid: {_validation_message(error)}")
    return parsed


def _model_state_path() -> Path:
    return _home() / "models-state.json"


def _capability_snapshot(*, refresh: bool = False):
    store = CapabilityStore(_home() / "capabilities")
    if refresh:
        capabilities = _capability_configuration(_load_config())
        scorer = str(capabilities.get("scorer_version") or "empirical_wilson_v1")
        return store.rebuild(_runs_root(), scorer_version=scorer).snapshot
    return store.load()


def model_inventory(*, refresh_capabilities: bool = False) -> dict[str, Any]:
    configuration = _load_config()
    try:
        snapshot = _capability_snapshot(refresh=refresh_capabilities)
        state = load_model_state(_model_state_path())
        return inventory_document(configuration, snapshot, state)
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read model inventory: {error}") from error


def _render_model_inventory(document: Mapping[str, Any]) -> None:
    models = document.get("models")
    rows = models if isinstance(models, list) else []
    if not rows:
        console.print("No models configured or detected. Run `villani models detect`.")
        return
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        default = " [DEFAULT]" if raw.get("bootstrap_default") else ""
        manual = " [ADVANCED MANUAL OVERRIDE]" if raw.get("manual_override") else ""
        success = raw.get("observed_success_rate")
        success_text = (
            f"{float(success) * 100:.1f}%"
            if isinstance(success, (int, float))
            else "unknown"
        )
        cost = raw.get("observed_cost_per_accepted_task")
        cost_text = (
            f"{float(cost):.4f}" if isinstance(cost, (int, float)) else "unknown"
        )
        console.print(
            f"{raw.get('backend_name') or '(detected)'}: {raw.get('display_name')}"
            f"{default}{manual}\n"
            f"  provider={raw.get('provider')}; endpoint={raw.get('endpoint') or 'default'}; "
            f"availability={raw.get('availability')}; tools={raw.get('tool_support')}; "
            f"roles={','.join(str(item) for item in raw.get('configured_roles', [])) or 'not configured'}\n"
            f"  capability={raw.get('capability_status')}; observations={raw.get('observed_task_count')}; "
            f"success={success_text}; cost/accepted={cost_text}; "
            f"pricing={raw.get('pricing_status')}; last_tested={raw.get('last_tested_at') or 'never'}",
            soft_wrap=True,
        )


@models_app.callback(invoke_without_command=True)
def models_root(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON inventory."),
) -> None:
    """List configured and recently detected models."""

    if ctx.invoked_subcommand is not None:
        return
    try:
        document = model_inventory(refresh_capabilities=True)
    except ValueError as error:
        _usage_error(str(error))
    if json_output:
        typer.echo(
            json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True)
        )
    else:
        _render_model_inventory(document)


@models_app.command("detect")
def models_detect(
    timeout: float = typer.Option(1.5, "--timeout", min=0.1, max=30.0),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect configured and common local model endpoints without inference."""

    configuration = _load_config()
    try:
        detections = detect_models(configuration, timeout=timeout)
        state = update_detection_state(
            load_model_state(_model_state_path()), detections
        )
        write_model_state(_model_state_path(), state)
        snapshot = _capability_snapshot(refresh=True)
        document = inventory_document(configuration, snapshot, state)
        document["detections"] = [item.as_dict() for item in detections]
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
        _usage_error(f"model detection failed: {error}")
    if json_output:
        typer.echo(
            json.dumps(redact_data(document), ensure_ascii=False, sort_keys=True)
        )
        return
    for detection in detections:
        console.print(
            f"{detection.provider_display_name}: {detection.availability}; "
            f"models={len(detection.models)}; {detection.diagnostic}",
            soft_wrap=True,
        )
    console.print(
        "Detection is advisory. Add a model explicitly before Villani routes to it."
    )


@models_app.command("test")
def models_test(
    backend_name: str | None = typer.Argument(
        None, help="Backend name; omit to test all."
    ),
    timeout: float = typer.Option(3.0, "--timeout", min=0.1, max=60.0),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Test configured availability with zero model-token use."""

    configuration = _load_config()
    try:
        state, results = test_models(
            configuration,
            load_model_state(_model_state_path()),
            backend_names=([backend_name] if backend_name else ()),
            timeout=timeout,
        )
        write_model_state(_model_state_path(), state)
    except (OSError, ValueError, ValidationError) as error:
        _usage_error(f"model test failed: {error}")
    if json_output:
        typer.echo(
            json.dumps(
                {"schema_version": "villani.model_test.v1", "results": results},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    for result in results:
        console.print(
            f"{result['backend_name']}: {result['availability']} - {result['diagnostic']} "
            "(0 model tokens)",
            soft_wrap=True,
        )


@models_app.command("add")
def models_add(
    backend_name: str = typer.Argument(..., help="Short configuration name."),
    model: str = typer.Option(..., "--model", help="Provider model identifier."),
    provider: str = typer.Option(..., "--provider"),
    endpoint: str | None = typer.Option(None, "--endpoint", "--base-url"),
    display_name: str | None = typer.Option(None, "--display-name"),
    role: list[str] | None = typer.Option(None, "--role"),
    api_key_env: str | None = typer.Option(None, "--api-key-env"),
    make_default: bool = typer.Option(
        False, "--default", help="Use as bootstrap default."
    ),
    tool_support: bool | None = typer.Option(None, "--tool-support/--no-tool-support"),
    context_window: int | None = typer.Option(None, "--context-window", min=1),
    manual_capability_score: float | None = typer.Option(
        None,
        "--manual-capability-score",
        min=0,
        max=100,
        help="Advanced manual override; never reported as observed capability.",
    ),
    billing_mode: str = typer.Option("unknown", "--billing-mode"),
    input_cost_per_million: float | None = typer.Option(
        None, "--input-cost-per-million", min=0
    ),
    output_cost_per_million: float | None = typer.Option(
        None, "--output-cost-per-million", min=0
    ),
    fixed_cost_per_attempt: float | None = typer.Option(
        None, "--fixed-cost-per-attempt", min=0
    ),
) -> None:
    """Add or replace a model; capability and pricing remain unknown by default."""

    configuration = _load_config()
    try:
        _validate_billing(
            billing_mode=billing_mode,
            input_price=input_cost_per_million,
            output_price=output_cost_per_million,
            compute_cost=None,
            fixed_cost=fixed_cost_per_attempt,
            estimated_input=None,
            estimated_output=None,
            estimated_duration=None,
        )
        add_model_to_configuration(
            configuration,
            backend_name=backend_name,
            model=model,
            provider=provider,
            endpoint=endpoint,
            display_name=display_name,
            roles=tuple(role or ["coding", "classification"]),
            api_key_env=api_key_env,
            tool_support=tool_support,
            context_window=context_window,
            make_default=make_default,
            manual_capability_score=manual_capability_score,
            billing_mode=billing_mode,
            input_cost_per_million=input_cost_per_million,
            output_cost_per_million=output_cost_per_million,
            fixed_cost_per_attempt=fixed_cost_per_attempt,
        )
        _write_config(_config_path(), configuration)
    except (OSError, ValueError, ValidationError) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"model configuration is invalid: {message}")
    state = "BOOTSTRAP" if make_default else "UNRATED"
    console.print(
        f"Added model {backend_name} as {state}; capability was not fabricated."
    )


@models_app.command("remove")
def models_remove(
    backend_name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Remove one configured model without deleting historical evidence."""

    configuration = _load_config()
    if not yes and not typer.confirm(
        f"Remove configured model {backend_name}? Historical run evidence is retained",
        default=False,
    ):
        console.print("Model was not removed.")
        return
    try:
        remove_model_from_configuration(configuration, backend_name)
        _write_config(_config_path(), configuration)
    except (OSError, ValueError) as error:
        _usage_error(f"model removal failed: {error}")
    console.print(
        f"Removed model {backend_name}; historical observations were retained."
    )


@models_app.command("default")
def models_default(backend_name: str = typer.Argument(...)) -> None:
    """Select the explicit bootstrap model without assigning a capability score."""

    configuration = _load_config()
    try:
        set_bootstrap_default(configuration, backend_name)
        _write_config(_config_path(), configuration)
    except (OSError, ValueError) as error:
        _usage_error(f"cannot select bootstrap default: {error}")
    console.print(
        f"Bootstrap default is {backend_name}; this is not a measured capability rating."
    )


def _validate_run_backends(backends: Mapping[str, Backend]) -> None:
    if not any(
        backend.enabled and "classification" in backend.roles
        for backend in backends.values()
    ):
        _run_usage_error(
            "no_backend",
            "An enabled backend with role 'classification' is required.",
        )
    if not any(
        backend.enabled and "coding" in backend.roles for backend in backends.values()
    ):
        _run_usage_error(
            "no_backend", "An enabled backend with role 'coding' is required."
        )
    active = [
        backend
        for backend in backends.values()
        if backend.enabled and ({"classification", "coding"} & set(backend.roles))
    ]
    try:
        for backend in active:
            validate_closed_loop_backend(backend)
            validate_runtime_credentials(backend)
    except ProviderConfigurationError as error:
        _run_usage_error(infer_failure_code(None, str(error)), str(error))
    currencies = {backend.currency for backend in active}
    if len(currencies) > 1:
        _usage_error(
            "enabled classification/coding backends must use one currency per run; "
            "currency conversion is not performed"
        )


@app.command("init")
def initialize(
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace an existing config template.",
    ),
) -> None:
    """Create the Villani home, canonical runs root, and config template."""

    home = _home()
    runs = _runs_root()
    config = _config_path()
    home.mkdir(parents=True, exist_ok=True)
    runs.mkdir(parents=True, exist_ok=True)
    if config.exists() and not force:
        console.print(
            f"Configuration already exists at {config}; not overwritten.",
            soft_wrap=True,
        )
        return
    _write_config(config, DEFAULT_CONFIG)
    console.print(f"Initialized Villani home at {home}")
    console.print(f"Configuration: {config}")
    console.print(f"Runs: {runs}")


def _doctor_report(
    repository: Path, configuration: Mapping[str, Any]
) -> tuple[bool, dict[str, Any]]:
    return build_repository_diagnostics(repository, configuration)


_probe_backend = probe_backend


@app.command("doctor")
def doctor_command(
    repo: Path | None = typer.Option(
        None, "--repo", help="Repository to inspect without mutation."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit stable machine-readable JSON."
    ),
) -> None:
    """Check configured local capabilities without spending model tokens."""

    configuration = _load_config()
    setup = configuration.get("setup")
    saved = setup.get("repository") if isinstance(setup, Mapping) else None
    try:
        repository, _source = resolve_doctor_repository(
            explicit=repo, saved=saved, cwd=Path.cwd()
        )
        healthy, report = build_repository_diagnostics(
            repository,
            configuration,
            repository_required=repo is not None,
        )
    except RepositoryDiagnosticError as error:
        _usage_error(str(error))
    except (OSError, ValueError, ValidationError, subprocess.SubprocessError) as error:
        _usage_error(f"doctor could not inspect configuration: {error}")
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        console.print(f"Villani doctor: {'ready' if healthy else 'not ready'}")
        console.print(
            f"Repository: {repository}"
            if repository
            else "Repository: unavailable (warning)"
        )
        for name, usable in report["required_capabilities"].items():
            console.print(f"- {name}: {'ok' if usable else 'unavailable'}")
        console.print("Execution providers:")
        for item in report["execution_providers"]:
            console.print(
                f"- {item['provider']}: {'available' if item['available'] else 'unavailable'}"
            )
        console.print("Backend connectivity:")
        for item in report["backend_connectivity"]:
            console.print(f"- {item['name']}: {item['probe_status']} (tokens spent: 0)")
        commands = report["likely_test_commands"]
        console.print(
            "Likely tests: "
            + (
                ", ".join(" ".join(command) for command in commands)
                if commands
                else "none detected"
            )
        )
    if not healthy:
        raise typer.Exit(1)


def _validate_billing(
    *,
    billing_mode: str,
    input_price: float | None,
    output_price: float | None,
    compute_cost: float | None,
    fixed_cost: float | None,
    estimated_input: int | None,
    estimated_output: int | None,
    estimated_duration: float | None,
) -> None:
    allowed = {"token", "compute_time", "fixed", "hybrid", "unknown"}
    if billing_mode not in allowed:
        _usage_error(
            "--billing-mode must be token, compute_time, fixed, hybrid, or unknown"
        )
    has_input = input_price is not None
    has_output = output_price is not None
    if has_input != has_output:
        _usage_error(
            "token billing requires both --input-cost-per-million and "
            "--output-cost-per-million"
        )
    token_component = has_input and has_output
    compute_component = compute_cost is not None
    fixed_component = fixed_cost is not None
    if billing_mode == "token":
        if not token_component:
            _usage_error("--billing-mode token requires both token price options")
        if compute_component or fixed_component:
            _usage_error("token billing cannot include compute-time or fixed costs")
    elif billing_mode == "compute_time":
        if not compute_component:
            _usage_error("--billing-mode compute_time requires --compute-cost-per-hour")
        if token_component or fixed_component:
            _usage_error("compute_time billing cannot include token or fixed costs")
    elif billing_mode == "fixed":
        if not fixed_component:
            _usage_error("--billing-mode fixed requires --fixed-cost-per-attempt")
        if token_component or compute_component:
            _usage_error("fixed billing cannot include token or compute-time costs")
    elif billing_mode == "hybrid":
        component_count = sum((token_component, compute_component, fixed_component))
        if component_count < 2:
            _usage_error(
                "hybrid billing requires at least two configured cost components"
            )
    elif any((token_component, compute_component, fixed_component)):
        _usage_error(
            "unknown billing cannot include token, compute-time, or fixed costs"
        )
    if (
        estimated_input is not None or estimated_output is not None
    ) and not token_component:
        _usage_error("estimated token counts require configured token prices")
    if estimated_duration is not None and not compute_component:
        _usage_error("--estimated-duration-seconds requires compute-time accounting")


@backend_app.command("add")
def backend_add(
    name: str = typer.Argument(..., help="Unique backend name."),
    provider: str = typer.Option(..., "--provider"),
    model: str = typer.Option(..., "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
    role: list[str] | None = typer.Option(None, "--role"),
    capability_score: int | None = typer.Option(None, "--capability-score"),
    capability_score_source: str = typer.Option(
        "user_configured", "--capability-score-source"
    ),
    billing_mode: str = typer.Option("unknown", "--billing-mode"),
    input_cost_per_million: float | None = typer.Option(
        None, "--input-cost-per-million"
    ),
    output_cost_per_million: float | None = typer.Option(
        None, "--output-cost-per-million"
    ),
    compute_cost_per_hour: float | None = typer.Option(None, "--compute-cost-per-hour"),
    fixed_cost_per_attempt: float | None = typer.Option(
        None, "--fixed-cost-per-attempt"
    ),
    estimated_input_tokens: int | None = typer.Option(None, "--estimated-input-tokens"),
    estimated_output_tokens: int | None = typer.Option(
        None, "--estimated-output-tokens"
    ),
    estimated_duration_seconds: float | None = typer.Option(
        None, "--estimated-duration-seconds"
    ),
    api_key_env: str | None = typer.Option(None, "--api-key-env"),
    timeout_seconds: int | None = typer.Option(None, "--timeout-seconds"),
    max_parallel: int = typer.Option(1, "--max-parallel"),
    currency: str = typer.Option("USD", "--currency"),
    execution_environment: str | None = typer.Option(None, "--execution-environment"),
) -> None:
    """Add or replace one backend without resolving its secret."""

    provider = canonical_provider(provider)
    if provider not in CANONICAL_PROVIDERS:
        _usage_error(
            "--provider must be one of: " + ", ".join(sorted(CANONICAL_PROVIDERS))
        )
    missing_requirements: list[str] = []
    roles = list(dict.fromkeys(role or ["coding"]))
    if "coding" in roles and capability_score is None:
        missing_requirements.append(
            "--capability-score is required for a coding backend"
        )
    if provider in {"local", "openai-compatible"} and not str(base_url or "").strip():
        missing_requirements.append(f"--provider {provider} requires --base-url")
    if missing_requirements:
        _usage_error("; ".join(missing_requirements))
    if not re.fullmatch(r"[A-Za-z]{3}", currency):
        _usage_error("--currency must be a three-letter ISO-style code")
    if not capability_score_source.strip():
        _usage_error("--capability-score-source must not be empty")
    if api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        _usage_error("--api-key-env must be a valid environment variable name")
    if timeout_seconds is not None and timeout_seconds <= 0:
        _usage_error("--timeout-seconds must be greater than zero")
    if not 1 <= max_parallel <= 32:
        _usage_error("--max-parallel must be between 1 and 32")
    _validate_billing(
        billing_mode=billing_mode,
        input_price=input_cost_per_million,
        output_price=output_cost_per_million,
        compute_cost=compute_cost_per_hour,
        fixed_cost=fixed_cost_per_attempt,
        estimated_input=estimated_input_tokens,
        estimated_output=estimated_output_tokens,
        estimated_duration=estimated_duration_seconds,
    )
    configuration = _load_config()
    raw_backends = configuration.setdefault("backends", {})
    if not isinstance(raw_backends, dict):
        _usage_error("config backends must be a mapping keyed by backend name")
    payload: dict[str, Any] = {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "api_key_env": api_key_env,
        "roles": roles,
        "capability_score": capability_score or 0,
        "capability_score_source": capability_score_source,
        "billing_mode": billing_mode,
        "input_cost_per_million": input_cost_per_million or 0.0,
        "output_cost_per_million": output_cost_per_million or 0.0,
        "currency": currency.upper(),
        "compute_cost_per_hour": compute_cost_per_hour,
        "fixed_cost_per_attempt": fixed_cost_per_attempt,
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_duration_seconds": estimated_duration_seconds,
        "timeout_seconds": timeout_seconds,
        "max_parallel": max_parallel,
        "enabled": True,
        "metadata": {"allow_dummy_api_key": True} if provider == "local" else {},
        "execution_environment": execution_environment,
    }
    try:
        backend = Backend.model_validate({"name": name, **payload})
    except ValidationError as error:
        _usage_error(f"backend configuration is invalid: {_validation_message(error)}")
    raw_backends[name] = backend.model_dump(mode="json", exclude={"name", "api_key"})
    _write_config(_config_path(), configuration)
    console.print(f"Added backend {name}")


@backend_app.command("list")
def backend_list() -> None:
    """List configured backends without resolving or printing secrets."""

    configuration = _load_config()
    backends = _load_backends(configuration)
    if not backends:
        console.print("No backends configured.")
        return
    for backend in sorted(backends.values(), key=lambda item: item.name):
        credential = (
            f"env:{backend.api_key_env}"
            if backend.api_key_env
            else "configured (redacted)"
            if backend.api_key
            else "not configured"
        )
        console.print(
            f"{backend.name}: provider={backend.provider}; model={backend.model}; "
            f"roles={','.join(backend.roles)}; capability={backend.capability_score:g} "
            f"({backend.capability_score_source}); billing={backend.billing_mode}; "
            f"currency={backend.currency}; "
            f"execution_environment={backend.execution_environment or 'default'}; "
            f"credential={credential}; "
            f"state={'enabled' if backend.enabled else 'disabled'}",
            soft_wrap=True,
        )


def _capability_configuration(configuration: Mapping[str, Any]) -> Mapping[str, Any]:
    value = configuration.get("capabilities")
    return value if isinstance(value, Mapping) else {}


@capability_app.command("rebuild")
def capability_rebuild() -> None:
    """Atomically rebuild profiles from canonical local runs only."""

    configuration = _load_config()
    scorer_version = str(
        _capability_configuration(configuration).get("scorer_version")
        or "empirical_wilson_v1"
    )
    try:
        result = CapabilityStore().rebuild(_runs_root(), scorer_version=scorer_version)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _usage_error(f"capability rebuild failed: {error}")
    snapshot = result.snapshot
    console.print(
        f"Capability snapshot: {'updated' if result.changed else 'unchanged'}; "
        f"runs={snapshot.source_run_count}; attempts={snapshot.source_attempt_count}; "
        f"profiles={len(snapshot.profiles)}"
    )
    console.print(f"Profile digest: {snapshot.profile_digest}")
    exclusions = ", ".join(
        f"{reason}={count}"
        for reason, count in sorted(snapshot.excluded_outcome_counts.items())
    )
    console.print(f"Excluded outcomes: {exclusions or 'none'}")


@capability_app.command("list")
def capability_list() -> None:
    """List configured static scores beside global empirical values."""

    configuration = _load_config()
    backends = _load_backends(configuration)
    capabilities = _capability_configuration(configuration)
    try:
        minimum = int(capabilities.get("minimum_empirical_samples", 20))
        wilson_threshold_value = capabilities.get(
            "minimum_empirical_wilson_lower_bound"
        )
        wilson_threshold = float(
            wilson_threshold_value
            if wilson_threshold_value is not None
            else capabilities.get("target_success_probability", 0.80)
        )
        snapshot = CapabilityStore().load()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _usage_error(f"cannot read capability registry: {error}")
    if not backends:
        console.print("No backends configured.")
        return
    for row in backend_score_rows(
        backends,
        snapshot,
        minimum_empirical_samples=minimum,
        minimum_empirical_wilson_lower_bound=wilson_threshold,
    ):
        empirical = (
            str(row["empirical_capability_score"])
            if row["empirical_capability_score"] is not None
            else "unknown"
        )
        probability = (
            f"{float(row['conservative_success_probability']):.6f}"
            if row["conservative_success_probability"] is not None
            else "unknown"
        )
        console.print(
            f"{row['backend_name']}: provider={row['provider']}; model={row['model']}; "
            f"static={row['static_capability_score']} ({row['static_score_source']}); "
            f"empirical={empirical}; status={row['empirical_status']}; "
            f"samples={row['sample_count']}; conservative_probability={probability}; "
            f"wilson_threshold={row['minimum_wilson_lower_bound']:.6f}",
            soft_wrap=True,
        )


def _classify_for_capability_explain(
    task: str,
    repository: Path,
    success_criteria: str,
    backends: Mapping[str, Backend],
    configuration: Mapping[str, Any],
) -> ClassificationSnapshot:
    eligible = [
        backend
        for backend in backends.values()
        if backend.enabled and "classification" in backend.roles
    ]
    if not eligible:
        raise ValueError("no enabled classification-capable backend is configured")
    default_name = default_bootstrap_backend(configuration)
    classification_backend = next(
        (item for item in eligible if item.name == default_name),
        min(eligible, key=lambda item: (-item.capability_score, item.name)),
    )
    context = ClassificationContext(
        run_id="capability_explain",
        trace_id="capability_explain",
        task_id="capability_explain",
        repository_path=str(repository),
        success_criteria=success_criteria,
        requires_file_changes=True,
        policy_configuration=configuration,
        classification_backend_name=classification_backend.name,
        classification_backend_model=classification_backend.model,
    )
    returned = _ClassifierAdapter(backends, configuration).classify(task, context)
    return ClassificationSnapshot(
        schema_version="villani.classification.v1",
        classification_id="capability_explain",
        run_id="capability_explain",
        task_id="capability_explain",
        classified_at=datetime.now(timezone.utc),
        difficulty=returned.difficulty,
        risk=returned.risk,
        category=returned.category,
        required_capabilities=list(returned.required_capabilities),
        estimated_attempts_needed=returned.estimated_attempts_needed,
        needs_tests=returned.needs_tests,
        confidence=returned.confidence,
        reasoning_summary=returned.reasoning_summary,
        signals=dict(returned.signals),
        metadata=dict(returned.metadata),
    )


def build_policy_preview(
    *,
    task: str,
    repository: Path,
    success_criteria: str,
    configuration: Mapping[str, Any],
    preset: str | None = None,
) -> dict[str, Any]:
    """Classify once and project both coding and verifier routes read-only."""

    effective_configuration = apply_policy_preset(configuration, preset)
    qualification_values = effective_configuration.setdefault("qualification", {})
    if not isinstance(qualification_values, dict):
        raise ValueError("qualification configuration must be an object")
    qualification_values["repository_path"] = str(repository.resolve())
    backends = {
        backend.name: backend
        for backend in configured_model_backends(effective_configuration).values()
    }
    if not any(
        item.enabled and "classification" in item.roles for item in backends.values()
    ):
        raise ValueError("no enabled classification-capable model is configured")
    if not any(item.enabled and "coding" in item.roles for item in backends.values()):
        raise ValueError("no enabled coding model is configured")
    raw = _classify_for_capability_explain(
        task,
        repository,
        success_criteria,
        backends,
        effective_configuration,
    )
    raw_value = Classification(
        difficulty=raw.difficulty,
        risk=raw.risk,
        category=raw.category,
        required_capabilities=tuple(raw.required_capabilities),
        estimated_attempts_needed=raw.estimated_attempts_needed,
        needs_tests=raw.needs_tests,
        confidence=raw.confidence,
        reasoning_summary=raw.reasoning_summary,
        signals=dict(raw.signals),
        metadata=dict(raw.metadata),
    )
    effective_value, adjustment_models, adjustment_version = (
        apply_classification_policy(
            raw_value,
            effective_configuration,
            timestamp=datetime.now(timezone.utc),
        )
    )
    effective = raw.model_copy(
        update={
            "difficulty": effective_value.difficulty,
            "risk": effective_value.risk,
            "confidence": effective_value.confidence,
            "metadata": {
                **dict(raw.metadata),
                "classification_adjustment_policy_version": adjustment_version,
            },
        }
    )
    snapshot = _capability_snapshot(refresh=True)
    qualification_store = QualificationStore()
    registry = build_agent_system_registry(
        effective_configuration,
        backends,
        qualification_store=qualification_store,
    )
    configured_route_policy = route_policy_from_configuration(effective_configuration)
    decision = BootstrapPolicyEngine(
        backends,
        effective_configuration,
        capability_snapshot=snapshot,
        qualification_store=qualification_store,
        agent_system_by_backend=registry.by_backend,
        economics_store=EconomicsStore(),
        route_policy=RoutePolicyStore().active_policy(configured_route_policy),
    ).decide(
        initial_policy_context(
            effective,
            effective_configuration,
            run_id="policy_preview",
        )
    )
    adjustments = [item.model_dump(mode="json") for item in adjustment_models]
    return build_policy_preview_document(
        raw_classification=raw,
        effective_classification=effective,
        adjustments=adjustments,
        decision=decision,
        configuration=effective_configuration,
        backends=backends,
    )


@capability_app.command("explain")
def capability_explain(
    task: str = typer.Option(..., "--task", help="Coding task to classify only."),
    repo: Path = typer.Option(..., "--repo", help="Existing Git repository."),
    success_criteria: str | None = typer.Option(None, "--success-criteria"),
) -> None:
    """Explain bootstrap and empirical routing inputs without coding execution."""

    repository = repo.expanduser().resolve()
    if not repository.exists() or not repository.is_dir():
        _usage_error(f"repository does not exist or is not a directory: {repository}")
    if not _is_git_repository(repository):
        _usage_error(f"repository is not a Git work tree: {repository}")
    configuration = _load_config()
    backends = _load_backends(configuration)
    _validate_run_backends(backends)
    criteria = success_criteria if success_criteria is not None else task
    try:
        classification = _classify_for_capability_explain(
            task, repository, criteria, backends, configuration
        )
        snapshot = CapabilityStore().load()
        engine = BootstrapPolicyEngine(
            backends, configuration, capability_snapshot=snapshot
        )
        budgets = configuration.get("budgets")
        budget_values = budgets if isinstance(budgets, Mapping) else {}
        max_attempts = int(budget_values.get("max_attempts", 3))
        max_cost_value = budget_values.get("max_cost")
        max_cost = float(max_cost_value) if max_cost_value is not None else None
        wall_value = budget_values.get("max_wall_time")
        wall_ms = int(float(wall_value) * 1000) if wall_value is not None else None
        decision = engine.decide(
            PolicyContext(
                run_id="capability_explain",
                trace_id="capability_explain",
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
        )
    except (
        OSError,
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"capability explain failed: {message}")
    explanation = {
        "classification": classification.model_dump(mode="json"),
        "bootstrap": {
            "policy_version": "bootstrap_v1",
            "required_capability_score": decision.required_capability_score,
            "required_capability_rule": decision.required_capability_rule,
            "considered_backends": [
                asdict(item) for item in decision.considered_backends
            ],
        },
        "empirical": {
            "profile_digest": snapshot.profile_digest if snapshot else None,
            "capability_scores": decision.metadata.get("capability_scores", {}),
            "optimizer": decision.metadata.get("empirical_optimizer", {}),
        },
        "path_used": decision.metadata.get("policy_path_used", "bootstrap_v1"),
        "would_choose_backend": decision.chosen_backend,
        "would_choose_model": decision.chosen_model,
        "coding_attempt_executed": False,
    }
    typer.echo(
        json.dumps(
            redact_data(explanation),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


class _ClassifierAdapter:
    def __init__(
        self, backends: Mapping[str, Backend], configuration: Mapping[str, Any]
    ) -> None:
        self._backends = dict(backends)
        self._configuration = dict(configuration)

    @staticmethod
    def _usage(
        backend: Backend,
        result: LLMCallResult | None,
        duration_ms: int,
        failure_state: str,
        error: Exception | None = None,
    ) -> dict[str, Any]:
        has_usage = bool(
            result and (result.usage or result.input_tokens or result.output_tokens)
        )
        input_tokens = result.input_tokens if result is not None and has_usage else None
        output_tokens = (
            result.output_tokens if result is not None and has_usage else None
        )
        cost = actual_attempt_cost(
            backend,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=duration_ms / 1000,
            started=True,
        )
        return {
            "stage": "classification",
            "backend": backend.name,
            "model": backend.model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": (
                input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None
                else None
            ),
            "token_accounting_status": "complete" if has_usage else "unknown",
            "model_calls": 1,
            "model_call_accounting_status": "complete",
            "cost": cost.total,
            "cost_accounting_status": cost.accounting_status,
            "currency": backend.currency,
            "duration_ms": duration_ms,
            "duration_accounting_status": "complete",
            "failure_state": failure_state,
            "error": redact_data(str(error)) if error is not None else None,
        }

    def _fallback_backend_names(self, primary: str) -> list[str]:
        policy = self._configuration.get("policy")
        policy_values = policy if isinstance(policy, Mapping) else self._configuration
        configured = policy_values.get("classifier_fallback_backends")
        if not isinstance(configured, list):
            return []
        return [
            str(name)
            for name in configured
            if str(name) != primary
            and str(name) in self._backends
            and self._backends[str(name)].enabled
            and "classification" in self._backends[str(name)].roles
        ]

    def classify(self, task: str, context: ClassificationContext) -> Classification:
        backend_name = context.classification_backend_name
        if not backend_name or backend_name not in self._backends:
            raise ValueError("classification backend was not resolved")
        task_model = Task(
            task_id=context.task_id,
            repo_path=context.repository_path,
            objective=task,
            instruction=task,
            success_criteria=context.success_criteria,
        )
        policy = self._configuration.get("policy")
        policy_values = policy if isinstance(policy, Mapping) else self._configuration
        retry_limit = max(0, int(policy_values.get("classifier_retry_limit", 1)))
        attempts: list[dict[str, Any]] = []
        candidates = [backend_name, *self._fallback_backend_names(backend_name)]
        for candidate_name in candidates:
            backend = self._backends[candidate_name]
            for _retry in range(retry_limit + 1):
                started = time.monotonic()
                result: LLMCallResult | None = None
                try:
                    classified, result = TaskClassifier().classify(
                        task_model,
                        self._backends,
                        backend_override=backend,
                    )
                    elapsed = max(int((time.monotonic() - started) * 1000), 0)
                    if result.error:
                        error = RuntimeError(result.error)
                        attempts.append(
                            self._usage(backend, result, elapsed, "failed", error)
                        )
                        continue
                    attempts.append(self._usage(backend, result, elapsed, "succeeded"))
                    return Classification(
                        difficulty=classified.difficulty,
                        risk=classified.risk,
                        category=classified.category,
                        required_capabilities=tuple(classified.required_capabilities),
                        estimated_attempts_needed=classified.estimated_attempts_needed,
                        needs_tests=classified.needs_tests,
                        confidence=classified.confidence,
                        reasoning_summary=classified.reasoning_summary,
                        signals=dict(classified.task_shape_signals),
                        metadata={
                            "classifier_version": "task_classifier_v1",
                            "classification_backend": {
                                "name": backend.name,
                                "model": backend.model,
                                "provider": backend.provider,
                            },
                            "classifier_attempts": attempts,
                            "likely_files": list(classified.likely_files),
                            "adjustment_notes": list(classified.adjustment_notes),
                            "relevant_file_paths": list(classified.relevant_file_paths),
                            "original_difficulty": classified.original_difficulty,
                            "original_risk": classified.original_risk,
                            "raw_signals": dict(classified.task_shape_signals),
                            "model_classification": {
                                "difficulty": (
                                    classified.original_difficulty
                                    or classified.difficulty
                                ),
                                "risk": classified.original_risk or classified.risk,
                                "category": classified.category,
                                "required_capabilities": list(
                                    classified.required_capabilities
                                ),
                                "confidence": classified.confidence,
                            },
                        },
                    )
                except Exception as error:
                    elapsed = max(int((time.monotonic() - started) * 1000), 0)
                    partial = error.result if isinstance(error, LLMCallError) else None
                    attempts.append(
                        self._usage(backend, partial, elapsed, "failed", error)
                    )
        # No opaque classifier failure can choose a cheap backend. This route is
        # deliberately conservative and retains every failed model invocation.
        return Classification(
            difficulty="hard",
            risk="high",
            category="unknown",
            required_capabilities=(),
            estimated_attempts_needed=1,
            needs_tests=True,
            confidence=0.0,
            reasoning_summary="Classifier backends failed to produce parseable output; used conservative fallback.",
            signals={},
            metadata={
                "classifier_version": "task_classifier_v1",
                "classification_fallback": True,
                "classification_fallback_reason": "all configured classifier calls failed or returned unparseable output",
                "classifier_attempts": attempts,
            },
        )


def build_controller(
    configuration: Mapping[str, Any],
    on_event: Callable[[Any], None] | None = None,
) -> ClosedLoopController:
    # Discovery is deliberately inert: this parses and digest-validates explicitly
    # configured manifests but does not import or execute any discovered entrypoint.
    from villani_ops.closed_loop.plugins import discover_plugins_from_configuration

    discover_plugins_from_configuration(configuration)
    """Construct only the canonical controller and its M4/M5 dependencies."""

    configured_backends = _load_backends(configuration)
    qualification_store = QualificationStore()
    try:
        agent_registry = build_agent_system_registry(
            configuration,
            configured_backends,
            qualification_store=qualification_store,
        )
    except (TypeError, ValueError, ValidationError) as error:
        _usage_error(f"agent-system configuration is invalid: {error}")
    selectable_coding_backends = {
        name
        for name, identity in agent_registry.by_backend.items()
        if identity.production_enabled
    }
    backends = {
        name: (
            backend
            if "coding" not in backend.roles or name in selectable_coding_backends
            else backend.model_copy(
                update={"roles": [role for role in backend.roles if role != "coding"]}
            )
        )
        for name, backend in configured_backends.items()
    }
    _validate_run_backends(backends)
    for backend_name, identity in agent_registry.by_backend.items():
        backend = configured_backends[backend_name]
        if (
            identity.production_enabled
            and identity.harness.harness_id == "villani-code"
        ):
            execution = ExecutionEnvironmentConfig.from_configuration(
                configuration, backend.execution_environment
            )
            if execution.provider in {"container", "devcontainer"}:
                continue
            command = backend.command_name or "villani-code"
            if resolve_command_prefix(command) is None:
                _usage_error(
                    f"Villani Code command {command!r} is unavailable; install the "
                    "villani-code package before running `villani run`"
                )
    verifier_config = configuration.get("verifier")
    if not isinstance(verifier_config, Mapping):
        verifier_config = {}
    invocation = str(verifier_config.get("invocation") or "in_process")
    if invocation not in {"in_process", "subprocess"}:
        _usage_error("verifier invocation must be 'in_process' or 'subprocess'")
    verifier_backend_name = verifier_config.get("backend")
    verifier_backend = None
    if verifier_backend_name:
        verifier_backend = backends.get(str(verifier_backend_name))
        if verifier_backend is None:
            _usage_error(
                f"verifier backend {verifier_backend_name!r} is not configured"
            )
        if not bool(verifier_config.get("no_llm", True)):
            try:
                validate_closed_loop_backend(verifier_backend)
                validate_runtime_credentials(verifier_backend)
            except ProviderConfigurationError as error:
                _usage_error(f"verifier configuration error: {error}")
            run_currencies = {
                backend.currency
                for backend in backends.values()
                if backend.enabled
                and ("classification" in backend.roles or "coding" in backend.roles)
            }
            if run_currencies and verifier_backend.currency not in run_currencies:
                _usage_error(
                    "enabled classification/coding/verifier backends must use one currency per run; "
                    "currency conversion is not performed"
                )
    capability_snapshot = CapabilityStore().load()
    economics_store = EconomicsStore()
    configured_route_policy = route_policy_from_configuration(configuration)
    active_route_policy = RoutePolicyStore().active_policy(configured_route_policy)
    policy = BootstrapPolicyEngine(
        backends,
        configuration,
        capability_snapshot=capability_snapshot,
        qualification_store=qualification_store,
        agent_system_by_backend=agent_registry.by_backend,
        economics_store=economics_store,
        route_policy=active_route_policy,
    )
    from villani_ops.closed_loop.plugins import (
        BuiltinAgentRunnerPlugin,
        BuiltinMaterializerPlugin,
        BuiltinSelectorPlugin,
        BuiltinVerifierPlugin,
    )

    verifier_impl: Any = VillaniVerifierAdapter(
        invocation=invocation,
        no_llm=bool(verifier_config.get("no_llm", True)),
        backend=str(verifier_backend_name) if verifier_backend_name else None,
        timeout_seconds=int(verifier_config.get("timeout_seconds") or 180),
        max_tool_calls=int(verifier_config.get("max_tool_calls") or 12),
        base_url=(
            str(verifier_config.get("base_url"))
            if verifier_config.get("base_url")
            else verifier_backend.base_url
            if verifier_backend
            else None
        ),
        model=(
            str(verifier_config.get("model"))
            if verifier_config.get("model")
            else verifier_backend.model
            if verifier_backend
            else None
        ),
        backend_config=verifier_backend,
    )
    verifier_routes = verifier_config.get("routes")
    if isinstance(verifier_routes, list) and verifier_routes:
        from villani_ops.closed_loop.verifier_routing import (
            VerifierCascade,
            VerifierPolicyEntry,
            VerifierRoute,
            VerifierRoutingPolicy,
        )

        configured_routes: list[VerifierRoute] = []
        run_currencies = {
            backend.currency
            for backend in backends.values()
            if backend.enabled
            and ("classification" in backend.roles or "coding" in backend.roles)
        }
        for index, route_value in enumerate(verifier_routes):
            if not isinstance(route_value, Mapping):
                _usage_error(f"verifier.routes[{index}] must be a mapping")
            route_backend_name = str(route_value.get("backend") or "")
            route_backend = backends.get(route_backend_name)
            if route_backend is None:
                _usage_error(
                    f"verifier route backend {route_backend_name!r} is not configured"
                )
            route_no_llm = bool(route_value.get("no_llm", False))
            if not route_no_llm:
                try:
                    validate_closed_loop_backend(route_backend)
                    validate_runtime_credentials(route_backend)
                except ProviderConfigurationError as error:
                    _usage_error(f"verifier route configuration error: {error}")
                if run_currencies and route_backend.currency not in run_currencies:
                    _usage_error(
                        "enabled classification/coding/verifier backends must use one currency per run; "
                        "currency conversion is not performed"
                    )
            entry = VerifierPolicyEntry.model_validate(
                {
                    "backend": route_backend_name,
                    "model": route_value.get("model") or route_backend.model,
                    "capability_score": route_value.get(
                        "capability_score", route_backend.capability_score
                    ),
                    "price_per_call_usd": route_value.get(
                        "price_per_call_usd", route_backend.fixed_cost_per_attempt
                    ),
                    "expected_latency_ms": route_value.get("expected_latency_ms"),
                    "authority": route_value.get("authority", "acceptance"),
                    "available": route_value.get("available", route_backend.enabled),
                }
            )
            configured_routes.append(
                VerifierRoute(
                    entry=entry,
                    verifier=VillaniVerifierAdapter(
                        invocation=str(route_value.get("invocation") or invocation),
                        no_llm=route_no_llm,
                        backend=route_backend_name,
                        timeout_seconds=int(
                            route_value.get("timeout_seconds")
                            or verifier_config.get("timeout_seconds")
                            or 180
                        ),
                        max_tool_calls=int(
                            route_value.get("max_tool_calls")
                            or verifier_config.get("max_tool_calls")
                            or 12
                        ),
                        base_url=str(
                            route_value.get("base_url") or route_backend.base_url or ""
                        )
                        or None,
                        model=str(route_value.get("model") or route_backend.model),
                        backend_config=route_backend,
                    ),
                )
            )
        policy_value = verifier_config.get("policy")
        verifier_impl = VerifierCascade(
            configured_routes,
            VerifierRoutingPolicy.model_validate(
                policy_value if isinstance(policy_value, Mapping) else {}
            ),
        )
    graph_value = configuration.get("verification_graph")
    signer = None
    if isinstance(graph_value, Mapping):
        from villani_ops.closed_loop.delivery import ProvenanceSigner
        from villani_ops.closed_loop.verification_graph import (
            VerificationGraph,
            VerificationGraphVerifierAdapter,
        )

        verifier_impl = VerificationGraphVerifierAdapter(
            VerificationGraph.model_validate(graph_value)
        )
        provenance = configuration.get("provenance", {})
        provenance = provenance if isinstance(provenance, Mapping) else {}
        key_env = str(provenance.get("signing_key_env") or "")
        key = os.environ.get(key_env) if key_env else None
        if not key:
            _usage_error(
                "verification graph delivery requires provenance.signing_key_env"
            )
        signer = ProvenanceSigner(
            key.encode(), key_id=str(provenance.get("key_id") or key_env)
        )
    from villani_ops.closed_loop.approvals import ApprovalGuardedMaterializer
    from villani_ops.closed_loop.delivery import (
        DeliveryMaterializerAdapter,
        build_git_host_adapter,
    )

    delivery_configuration = configuration.get("delivery")
    delivery_values = (
        dict(delivery_configuration)
        if isinstance(delivery_configuration, Mapping)
        else {}
    )
    provider_name = str(delivery_values.get("provider") or "auto").lower()
    git_provider = (
        None if provider_name == "auto" else build_git_host_adapter(configuration)
    )

    materializer_impl = ApprovalGuardedMaterializer(
        DeliveryMaterializerAdapter(
            local_apply=PatchMaterializerAdapter(),
            git_provider=git_provider,
            provenance_signer=signer,
        )
    )

    from villani_ops.cli.agentd_sink import build_agentd_event_sink

    return ClosedLoopController(
        classifier=_ClassifierAdapter(backends, configuration),
        policy_engine=policy,
        attempt_runner=BuiltinAgentRunnerPlugin(agent_registry.attempt_runner()),
        verifier=BuiltinVerifierPlugin(verifier_impl),
        selector=BuiltinSelectorPlugin(EvidenceSelectorAdapter()),
        materializer=BuiltinMaterializerPlugin(materializer_impl),
        on_event=on_event,
        event_sink=build_agentd_event_sink(),
        qualification_store=qualification_store,
        economics_store=economics_store,
    )


def _is_git_repository(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_repository_root(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            text=True,
            capture_output=True,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).expanduser().resolve()
    except OSError:
        return None


def _repository_dirty(path: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=path,
            text=True,
            capture_output=True,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _print_failure_experience(
    code: str,
    message: str | None = None,
    *,
    attempts: int = 0,
    patch_preserved: bool = False,
) -> None:
    experience = failure_experience(
        code,
        reason=message,
        attempts=attempts,
        patch_preserved=patch_preserved,
    )
    console.print("FAILED", markup=False)
    console.print(str(experience["what_failed"]), markup=False)
    console.print(f"Villani tried: {experience['what_villani_tried']}", markup=False)
    console.print(f"Missing evidence: {experience['missing_evidence']}", markup=False)
    console.print(f"Patch: {experience['patch_status']}", markup=False)
    console.print(f"Next: {experience['next_action']}", markup=False)


def _run_usage_error(
    code: str,
    message: str,
    *,
    attempts: int = 0,
    patch_preserved: bool = False,
) -> NoReturn:
    _print_failure_experience(
        code,
        message,
        attempts=attempts,
        patch_preserved=patch_preserved,
    )
    raise typer.Exit(2)


def _resolve_run_repository(repo: Path | None) -> Path:
    selected = (repo or Path.cwd()).expanduser().resolve()
    if not selected.exists() or not selected.is_dir():
        _run_usage_error(
            "invalid_repository",
            f"repository does not exist or is not a directory: {selected}",
        )
    root = _git_repository_root(selected)
    if root is None:
        if not _is_git_repository(selected):
            _run_usage_error(
                "invalid_repository", f"repository is not a Git work tree: {selected}"
            )
        root = selected
    dirty = _repository_dirty(root)
    if dirty is None:
        _run_usage_error(
            "invalid_repository", f"repository status could not be inspected: {root}"
        )
    if dirty:
        _run_usage_error(
            "dirty_repository",
            f"repository has uncommitted changes: {root}",
        )
    return root


def _configured_validation_commands(
    configuration: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw = configuration.get("repository_validation_commands")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            continue
        argv = item.get("argv")
        if not (
            isinstance(argv, list)
            and argv
            and all(isinstance(value, str) and value for value in argv)
        ):
            continue
        result.append(
            confirmed_command(
                argv,
                source=str(item.get("source") or "configured_override"),
                confidence=float(item.get("confidence") or 1.0),
                confirmed_by=str(item.get("confirmed_by") or "configuration"),
                validation_id=str(
                    item.get("validation_id") or f"repository_validation_{index:03d}"
                ),
                timeout_seconds=float(item.get("timeout_seconds") or 120),
            )
        )
    return result


def prepare_repository_validation(
    configuration: dict[str, Any],
    repository: Path,
    *,
    manual_commands: Sequence[str] = (),
    confirm_low_confidence: bool = False,
    allow_prompt: bool = True,
    confirmed_by: str = "cli",
    quiet: bool = False,
) -> dict[str, Any]:
    """Attach confirmed validation argv while retaining advisory discovery."""

    if manual_commands:
        commands = [
            confirmed_command(
                parse_manual_command(command),
                source="manual_override",
                confidence=1.0,
                confirmed_by=confirmed_by,
                validation_id=f"repository_validation_{index:03d}",
            )
            for index, command in enumerate(manual_commands, 1)
        ]
        discovery = discover_repository_validation(repository)
        discovery["selection"] = {
            "source": "manual_override",
            "commands": [item["display_command"] for item in commands],
            "confirmed": True,
        }
        if not quiet:
            for item in commands:
                console.print(
                    f"Validation: {item['display_command']} (manual override)"
                )
    else:
        configured = _configured_validation_commands(configuration)
        discovery = discover_repository_validation(repository)
        if configured:
            commands = configured
            discovery["selection"] = {
                "source": "configured_override",
                "commands": [item["display_command"] for item in commands],
                "confirmed": True,
            }
            if not quiet:
                for item in commands:
                    console.print(
                        f"Validation: {item['display_command']} (configured override)",
                        markup=False,
                    )
        else:
            suggestions = discovery.get("suggestions")
            selected = (
                suggestions[0]
                if isinstance(suggestions, list) and suggestions
                else None
            )
            if not isinstance(selected, Mapping):
                commands = []
                discovery["selection"] = {
                    "source": "unavailable",
                    "commands": [],
                    "confirmed": False,
                    "alternative_evidence_required": True,
                }
                if not quiet:
                    console.print(
                        "No repository check was detected. The run can continue, but Villani will require sufficient alternative evidence before it can be ready to apply.",
                        markup=False,
                    )
                configuration["repository_validation_commands"] = commands
                configuration["repository_validation_discovery"] = discovery
                return discovery
            argv = selected.get("argv")
            if not isinstance(argv, list) or not all(
                isinstance(value, str) and value for value in argv
            ):
                _run_usage_error(
                    "no_validation_command",
                    "the discovered repository validation command was malformed",
                )
            confidence = float(selected.get("confidence") or 0)
            command_text = display_argv(argv)
            if not quiet:
                console.print(
                    f"Validation: {command_text} ({selected.get('confidence_label')} confidence)",
                    markup=False,
                )
            if confidence < CONFIRMATION_THRESHOLD and not confirm_low_confidence:
                if not allow_prompt or not typer.confirm(
                    f"Villani is not sure this is the right check. Use '{command_text}'?",
                    default=False,
                ):
                    _run_usage_error(
                        "no_validation_command",
                        "low-confidence validation discovery was not confirmed",
                    )
            commands = [
                confirmed_command(
                    argv,
                    source=str(selected.get("source") or "metadata_discovery"),
                    confidence=confidence,
                    confirmed_by=confirmed_by,
                    validation_id="repository_validation_001",
                )
            ]
            discovery["selection"] = {
                "suggestion_id": selected.get("suggestion_id"),
                "source": selected.get("source"),
                "commands": [command_text],
                "confirmed": True,
                "confirmed_by": confirmed_by,
            }
    configuration["repository_validation_commands"] = commands
    configuration["repository_validation_discovery"] = discovery
    return discovery


_ASCII_PROGRESS_SYMBOLS = {
    "·": ".",
    "●": "*",
    "✓": "+",
    "×": "x",
    "↗": "^",
    "◆": ">",
}


def _display_progress_symbol(symbol: str, encoding: str | None = None) -> str:
    selected_encoding = encoding or console.encoding or "utf-8"
    try:
        symbol.encode(selected_encoding)
    except (LookupError, UnicodeEncodeError):
        return _ASCII_PROGRESS_SYMBOLS.get(symbol, "*")
    return symbol


def _run_progress_listener(
    runs_root: Path,
    *,
    verbose: bool = False,
    debug: bool = False,
) -> Callable[[Any], None]:
    ordinals: dict[str, int] = {}
    current_stage: Literal["Understanding", "Working", "Checking", "Ready"] | None = (
        None
    )
    current_sentence: str | None = None

    def listener(event: Any) -> None:
        nonlocal current_stage, current_sentence
        if event.event_type == "run_created":
            setattr(listener, "run_created", True)
            setattr(listener, "run_id", event.run_id)
            if verbose or debug:
                console.print(f"Run ID: {event.run_id}")
                console.print(f"Run directory: {runs_root / event.run_id}")
        if event.event_type == "attempt_started" and event.attempt_id:
            ordinal = event.payload.get("ordinal")
            if isinstance(ordinal, int):
                ordinals[event.attempt_id] = ordinal
        event_value = event.model_dump(mode="json")
        stage, sentence = project_product_stage(event_value, current_stage)
        if stage != current_stage or (
            sentence != current_sentence
            and event.event_type
            in {
                "retry_selected",
                "escalation_selected",
                "verification_retry_started",
                "repository_validation_retry_started",
                "focused_probe_execution_started",
            }
        ):
            console.print(f"{stage}: {sentence}", markup=False)
            current_stage, current_sentence = stage, sentence
        if verbose or debug:
            for line in progress_lines_for_event(
                event,
                ordinals=ordinals,
                include_raw=True,
            ):
                suffix = f" [{line['raw_event_type']}]" if verbose and not debug else ""
                console.print(
                    f"{_display_progress_symbol(str(line['symbol']))} {line['message']}{suffix}",
                    markup=False,
                )
        if debug:
            console.print(
                json.dumps(redact_data(event_value), sort_keys=True), markup=False
            )

    setattr(listener, "run_created", False)
    setattr(listener, "run_id", None)
    return listener


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _money(value: Any, status: Any, currency: Any = "USD") -> str:
    code = str(currency or "USD").upper()
    return f"{code} {float(value):.2f}" if value is not None else f"Unknown ({status})"


def _count_text(value: Any) -> str:
    return (
        str(value)
        if isinstance(value, int) and not isinstance(value, bool)
        else "Unknown"
    )


def _cost_text(value: Any, status: Any, currency: Any = "USD") -> str:
    """Compatibility formatter for non-run listing commands."""

    return _money(value, status, currency)


def _print_terminal_summary(
    result: ClosedLoopRunResult,
    *,
    verbose: bool = False,
    json_output: bool = False,
) -> None:
    product = build_product_run(result.run_directory)
    if json_output:
        typer.echo(
            json.dumps(
                product.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return

    console.print(str(product.final_verdict or product.current_stage), markup=False)
    if product.verdict_reason:
        console.print(product.verdict_reason, markup=False)
    console.print("\nWhat changed:", markup=False)
    console.print(f"  {product.change_summary}", markup=False)
    console.print("\nFiles changed:", markup=False)
    if product.changed_files:
        for path in product.changed_files:
            console.print(f"  {path}", markup=False)
    else:
        console.print("  No file changes were recorded.", markup=False)
    checks = product.checks_summary
    console.print("\nChecks and tests:", markup=False)
    console.print(
        f"  {_count_text(checks.passed)} passed; {_count_text(checks.failed)} failed; "
        f"{_count_text(checks.not_run)} not run; {_count_text(checks.unavailable)} unavailable "
        f"({checks.accounting_status})",
        markup=False,
    )
    requirements = product.requirement_summary
    console.print("\nRequirement coverage:", markup=False)
    console.print(
        f"  {_count_text(requirements.proved)} proved; "
        f"{_count_text(requirements.not_proved)} not proved "
        f"({requirements.accounting_status})",
        markup=False,
    )
    console.print("\nKnown cost:", markup=False)
    console.print(
        f"  {_money(product.cost.value, product.cost.accounting_status, product.cost.currency)}",
        markup=False,
    )
    console.print("\nElapsed time:", markup=False)
    duration = (
        f"{product.duration.value_ms / 1000:.1f} seconds"
        if product.duration.value_ms is not None
        else f"Unknown ({product.duration.accounting_status})"
    )
    console.print(f"  {duration}", markup=False)
    if product.available_actions:
        primary = product.available_actions[0]
        console.print("\nNext action:", markup=False)
        console.print(f"  {primary.label}: {primary.href}", markup=False)
    console.print("\nEvidence:", markup=False)
    for link in product.evidence_links:
        console.print(f"  {link.label}: {link.href}", markup=False)
    if verbose and product.technical_detail_references:
        console.print("  Technical details:", markup=False)
        for reference in product.technical_detail_references:
            console.print(f"    {reference}", markup=False)
    console.print(f"\nRun ID: {product.run_identity.run_id}", markup=False)


def _print_legacy_terminal_summary(
    result: ClosedLoopRunResult,
    *,
    verbose: bool = False,
) -> None:
    presentation = build_run_presentation(
        result.run_directory, include_raw_events=verbose
    )
    console.print(str(presentation["outcome"]), markup=False)
    console.print("", markup=False)
    console.print(str(presentation["summary"]), markup=False)

    delivery = _mapping(presentation.get("delivery"))
    if delivery:
        console.print("\nDelivery:", markup=False)
        console.print(
            f"  {delivery.get('label') or delivery.get('state')} "
            f"(mode: {delivery.get('mode')})",
            markup=False,
        )
        if delivery.get("target_worktree_modified"):
            console.print("  The target working tree was modified.", markup=False)
        elif delivery.get("repository_modified"):
            console.print(
                "  A separate delivery branch/worktree was created; the original branch was not switched.",
                markup=False,
            )
        else:
            console.print("  The target repository was not modified.", markup=False)
        authority = _mapping(delivery.get("authority"))
        if authority:
            console.print(
                f"  Authority: {'permitted' if authority.get('permitted') else 'not permitted'} "
                f"({authority.get('policy_version') or 'unversioned policy'})",
                markup=False,
            )
        approval_record = _mapping(delivery.get("approval"))
        if approval_record.get("status") == "pending":
            deadline = approval_record.get("deadline") or "no deadline"
            console.print(f"  Approval deadline: {deadline}", markup=False)

    changed = _mapping(presentation.get("changed"))
    console.print("\nChanged:", markup=False)
    files = changed.get("files") if isinstance(changed.get("files"), list) else []
    if files:
        for path in files:
            console.print(f"  {path}", markup=False)
    else:
        console.print("  No files are in the selected patch.", markup=False)
    review = _mapping(delivery.get("review"))
    if review:
        console.print(
            f"  {review.get('insertions', 0)} insertions, "
            f"{review.get('deletions', 0)} deletions",
            markup=False,
        )
        comparison = review.get("candidate_comparison")
        if isinstance(comparison, list) and comparison:
            console.print(
                f"  Compared {len(comparison)} acceptance-eligible candidate"
                f"{'s' if len(comparison) != 1 else ''}.",
                markup=False,
            )
        for warning in review.get("unrelated_change_warnings") or []:
            console.print(f"  Warning: {warning}", markup=False)
        for warning in review.get("sensitive_file_warnings") or []:
            console.print(f"  Warning: {warning}", markup=False)

    confidence = _mapping(presentation.get("confidence"))
    validation = _mapping(presentation.get("validation"))
    console.print("\nConfidence and authority:", markup=False)
    confidence_text = (
        f"{float(confidence['value']):.0%}"
        if confidence.get("value") is not None
        else "Unknown"
    )
    console.print(
        f"  {confidence.get('label')} · {confidence_text} · {confidence.get('authority')}",
        markup=False,
    )
    console.print("\nValidation:", markup=False)
    console.print(
        f"  {_count_text(validation.get('checks_passed'))} repository checks passed; "
        f"{_count_text(validation.get('checks_failed'))} failed; "
        f"{_count_text(validation.get('checks_not_run'))} not run; "
        f"{_count_text(validation.get('checks_unavailable'))} unavailable",
        markup=False,
    )
    console.print(
        f"  {_count_text(validation.get('focused_probes_passed'))} focused probes passed; "
        f"{_count_text(validation.get('focused_probes_failed'))} failed; "
        f"{_count_text(validation.get('focused_probes_not_run'))} not run; "
        f"{_count_text(validation.get('focused_probes_unavailable'))} unavailable",
        markup=False,
    )
    console.print(
        f"  {_count_text(validation.get('requirements_proved'))} task requirements proved; "
        f"{_count_text(validation.get('requirements_not_proved'))} not proved",
        markup=False,
    )
    for command in validation.get("commands") or []:
        console.print(f"  {command.get('command')}", markup=False)

    console.print("\nRemaining risks:", markup=False)
    for risk in presentation.get("remaining_risks") or []:
        console.print(f"  {risk}", markup=False)

    console.print("\nCost:", markup=False)
    cost = _mapping(presentation.get("cost"))
    currency = cost.get("currency", "USD")
    console.print(
        f"  Coding       {_money(cost.get('coding'), cost.get('coding_status'), currency)}",
        markup=False,
    )
    console.print(
        f"  Verification {_money(cost.get('verification'), cost.get('verification_status'), currency)}",
        markup=False,
    )
    console.print(
        f"  Total        {_money(cost.get('total'), cost.get('accounting_status'), currency)}",
        markup=False,
    )

    console.print("\nVillani recovery:", markup=False)
    for item in presentation.get("recovery") or []:
        console.print(f"  {item}", markup=False)

    failure = presentation.get("failure")
    if isinstance(failure, Mapping):
        console.print("\nFailure details:", markup=False)
        console.print(f"  What failed: {failure.get('what_failed')}", markup=False)
        console.print(
            f"  Evidence missing: {failure.get('missing_evidence')}", markup=False
        )
        console.print(f"  Patch: {failure.get('patch_status')}", markup=False)

    console.print("\nNext:", markup=False)
    for item in presentation.get("next_actions") or []:
        console.print(f"  {item.get('label')}: {item.get('action')}", markup=False)


_DELIVERY_MODES = {
    "suggest": "patch_export",
    "approve": "local_patch_apply",
    "apply": "local_patch_apply",
    "branch": "local_branch",
    "pull-request": "pull_request",
}
# Accepted as a compatibility alias, but no longer presented as a public mode.
_LEGACY_DELIVERY_ALIASES = {"patch": "suggest"}
_APPROVAL_MODES = {"automatic", "review"}
_POLICY_SELECTIONS = {"configured", "bootstrap", "active", "last-known-good"}


def configure_run_experience(
    configuration: dict[str, Any],
    *,
    delivery_mode: str,
    approval_mode: str | None,
    policy_selection: str,
    routing_mode: str | None,
) -> None:
    """Apply user-facing choices without changing acceptance eligibility."""

    delivery_mode = _LEGACY_DELIVERY_ALIASES.get(delivery_mode, delivery_mode)
    if delivery_mode not in _DELIVERY_MODES:
        _usage_error(
            "--delivery must be suggest, approve, apply, branch, or pull-request"
        )
    if approval_mode is not None and approval_mode not in _APPROVAL_MODES:
        _usage_error("--approval must be automatic or review")
    if policy_selection not in _POLICY_SELECTIONS:
        _usage_error(
            "--policy must be configured, bootstrap, active, or last-known-good"
        )
    if routing_mode is not None and routing_mode not in {
        "observe",
        "recommend",
        "enforce",
    }:
        _usage_error("--mode must be observe, recommend, or enforce")

    if approval_mode == "review" and delivery_mode == "apply":
        # M3 compatibility: review now means a real persisted approval pause.
        delivery_mode = "approve"
    requested_kind = _DELIVERY_MODES[delivery_mode]
    effective_kind = requested_kind
    delivery = configuration.setdefault("delivery", {})
    if not isinstance(delivery, dict):
        _usage_error("config delivery must be a YAML object")
    delivery["workflow_version"] = "villani.delivery_workflow.v1"
    delivery["mode"] = delivery_mode
    delivery["materialization_type"] = effective_kind
    delivery["requested_materialization_type"] = requested_kind
    delivery["approval_mode"] = (
        "explicit" if delivery_mode == "approve" else "automatic"
    )
    if delivery_mode == "branch":
        # Branch commits are opt-in. Pull-request delivery always commits.
        delivery.setdefault("commit", False)
    approval_configuration = delivery.setdefault("approval", {})
    if not isinstance(approval_configuration, dict):
        _usage_error("config delivery.approval must be a YAML object")
    approval_configuration.setdefault("timeout_seconds", 24 * 60 * 60)
    approval_configuration.setdefault("timeout_policy", "reject")

    routing = configuration.setdefault("routing", {})
    if not isinstance(routing, dict):
        _usage_error("config routing must be a YAML object")
    if routing_mode is not None:
        routing["mode"] = routing_mode
    if policy_selection == "bootstrap":
        for key in ("active_policy", "last_known_good_policy"):
            value = routing.get(key)
            if isinstance(value, Mapping):
                routing[key] = {**dict(value), "state": "paused"}
    elif policy_selection == "active":
        value = routing.get("active_policy")
        if not isinstance(value, Mapping) or value.get("state") != "active":
            _usage_error("no active advanced routing policy is configured")
    elif policy_selection == "last-known-good":
        value = routing.get("last_known_good_policy")
        if not isinstance(value, Mapping) or value.get("state") != "active":
            _usage_error("no active last-known-good routing policy is configured")
        active = routing.get("active_policy")
        if isinstance(active, Mapping):
            routing["active_policy"] = {**dict(active), "state": "paused"}

    configuration["run_experience"] = {
        "mode": "performance",
        "verification_required": True,
        "default_wall_time_budget": None,
        "attempt_policy_visible": False,
        "delivery_mode": delivery_mode,
        "effective_materialization_type": effective_kind,
        "approval_mode": ("explicit" if delivery_mode == "approve" else "automatic"),
        "policy_preset": configured_policy_preset(configuration),
        "policy_selection": policy_selection,
        "routing_mode": routing.get("mode", "observe"),
    }


def resolve_run_budgets(
    configuration: Mapping[str, Any],
    *,
    max_attempts: int | None,
    max_cost: float | None,
    max_wall_time: float | None,
) -> tuple[int, float | None, float | None]:
    budgets = configuration.get("budgets")
    values = budgets if isinstance(budgets, Mapping) else {}
    attempts_value = (
        max_attempts if max_attempts is not None else values.get("max_attempts", 3)
    )
    cost_value = max_cost if max_cost is not None else values.get("max_cost")
    wall_value = (
        max_wall_time if max_wall_time is not None else values.get("max_wall_time")
    )
    try:
        attempts = int(attempts_value)
        cost = float(cost_value) if cost_value is not None else None
        wall = float(wall_value) if wall_value is not None else None
    except (TypeError, ValueError):
        _usage_error("configured budgets must be numeric")
    if attempts < 1:
        _usage_error("--max-attempts must be at least 1")
    if cost is not None and cost < 0:
        _usage_error("--max-cost must not be negative")
    if wall is not None and wall < 0:
        _usage_error("--max-wall-time must not be negative")
    return attempts, cost, wall


def _execute_new_run(
    *,
    task: str,
    repository: Path,
    success_criteria: str,
    configuration: dict[str, Any],
    max_attempts: int,
    max_cost: float | None,
    max_wall_time: float | None,
    requires_file_changes: bool,
    verbose: bool,
    debug: bool,
    json_output: bool = False,
    lineage: Mapping[str, Any] | None = None,
    run_id: str | None = None,
) -> ClosedLoopRunResult:
    runs_root = _runs_root()
    runs_root.mkdir(parents=True, exist_ok=True)
    qualification = configuration.setdefault("qualification", {})
    if not isinstance(qualification, dict):
        _usage_error("config qualification must be a YAML object")
    qualification["repository_path"] = str(repository.resolve())
    builder = _controller_builder or build_controller
    progress_listener = (
        None
        if json_output
        else _run_progress_listener(runs_root, verbose=verbose, debug=debug)
    )
    try:
        controller = builder(configuration, progress_listener)
    except typer.Exit:
        raise
    except (TypeError, ValueError, ValidationError) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _run_usage_error(
            infer_failure_code(None, message), f"Invalid run configuration: {message}"
        )
    cancellation_event = threading.Event()
    request = ClosedLoopRunRequest(
        task=task,
        repository_path=repository,
        success_criteria=success_criteria,
        runs_root=runs_root,
        max_attempts=max_attempts,
        max_cost=max_cost,
        max_wall_time=max_wall_time,
        requires_file_changes=requires_file_changes,
        policy_configuration=configuration,
        lineage=dict(lineage or {}),
        run_id=run_id,
        cancellation_event=cancellation_event,
    )
    try:
        result = controller.run(request)
    except KeyboardInterrupt:
        cancellation_event.set()
        cancelled_projection_printed = False
        cancelled_run_id = getattr(progress_listener, "run_id", None) or run_id
        cancelled_directory = (
            runs_root / str(cancelled_run_id) if cancelled_run_id else None
        )
        if (
            cancelled_run_id
            and cancelled_directory is not None
            and cancelled_directory.is_dir()
        ):
            try:
                cancelled_result = controller.cancel(str(cancelled_run_id), runs_root)
                _print_terminal_summary(
                    cancelled_result,
                    verbose=verbose or debug,
                    json_output=json_output,
                )
                cancelled_projection_printed = True
            except (OSError, TypeError, ValueError):
                pass
        try:
            cancelled_manifest = (
                _read_json(cancelled_directory / "manifest.json")
                if cancelled_directory is not None
                else None
            ) or {}
        except (OSError, json.JSONDecodeError):
            cancelled_manifest = {}
        cancelled_attempt_ids = [
            str(item)
            for item in cancelled_manifest.get("attempt_ids", [])
            if isinstance(item, str)
        ]
        patch_preserved = bool(
            cancelled_directory is not None
            and (
                (cancelled_directory / "final.patch").is_file()
                or any(
                    (
                        cancelled_directory / "attempts" / attempt_id / "patch.diff"
                    ).is_file()
                    for attempt_id in cancelled_attempt_ids
                )
            )
        )
        if not json_output and not cancelled_projection_printed:
            _print_failure_experience(
                "user_cancelled",
                "The run was cancelled by the user.",
                attempts=len(cancelled_attempt_ids),
                patch_preserved=patch_preserved,
            )
        raise typer.Exit(130) from None
    try:
        capabilities = _capability_configuration(configuration)
        scorer = str(capabilities.get("scorer_version") or "empirical_wilson_v1")
        CapabilityStore(_home() / "capabilities").rebuild(
            runs_root,
            scorer_version=scorer,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        if not json_output:
            console.print(
                "Capability profile synchronization pending: "
                f"the run is durable, but observed model statistics could not be refreshed ({error})."
            )
    if not json_output and not bool(getattr(progress_listener, "run_created", False)):
        console.print(f"Run ID: {result.run_id}")
        if verbose or debug:
            console.print(f"Run directory: {result.run_directory}")
    return result


def _finish_run(
    result: ClosedLoopRunResult,
    *,
    verbose: bool,
    open_after: bool,
    json_output: bool = False,
) -> None:
    _print_terminal_summary(result, verbose=verbose, json_output=json_output)
    if open_after:
        _open_flight_recorder(result.run_id)
    if result.terminal_state == "EXHAUSTED":
        raise typer.Exit(3)
    if result.terminal_state == "FAILED":
        raise typer.Exit(4)
    if result.terminal_state == "CANCELLED":
        raise typer.Exit(130)


@app.command("run")
def run_command(
    task: str | None = typer.Argument(
        None,
        help="Task instruction. Omit when using --task-file.",
    ),
    task_file: Path | None = typer.Option(
        None,
        "--task-file",
        help="Read the complete task instruction from a UTF-8 file.",
    ),
    repo: Path | None = typer.Option(
        None,
        "--repo",
        help="Git repository. Defaults to the current repository.",
    ),
    success_criteria: str | None = typer.Option(None, "--success-criteria"),
    validation_command: list[str] | None = typer.Option(
        None,
        "--validation-command",
        help="Exact validation command; repeat for multiple commands.",
    ),
    confirm_validation: bool = typer.Option(
        False,
        "--confirm-validation",
        help="Confirm a low-confidence discovered command non-interactively.",
    ),
    delivery: str | None = typer.Option(
        None,
        "--delivery",
        help="suggest, approve, apply, branch, or pull-request",
    ),
    approval: str | None = typer.Option(None, "--approval", hidden=True),
    max_attempts: int | None = typer.Option(None, "--max-attempts"),
    max_cost: float | None = typer.Option(None, "--max-cost"),
    max_wall_time: float | None = typer.Option(None, "--max-wall-time"),
    accepted_candidates_required: int | None = typer.Option(
        None, "--accepted-candidates-required"
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help="Performance, Reliable, Balanced, Local first, Cheapest acceptable, or Custom.",
    ),
    agent_system: str | None = typer.Option(
        None,
        "--agent-system",
        help="Advanced manual route override by configured route name or system ID.",
    ),
    allow_experimental: bool = typer.Option(
        False,
        "--allow-experimental",
        help="Acknowledge that the manually selected repository route is Experimental.",
    ),
    local_only: bool | None = typer.Option(
        None,
        "--local-only/--allow-remote",
        help="Advanced privacy constraint: allow only qualified local systems.",
        hidden=True,
    ),
    maximum_known_route_cost: float | None = typer.Option(
        None,
        "--maximum-known-route-cost",
        min=0,
        help="Advanced constraint on the known USD route-cost subtotal.",
        hidden=True,
    ),
    preferred_provider: str | None = typer.Option(
        None,
        "--preferred-provider",
        help="Advanced preference applied only among otherwise eligible systems.",
        hidden=True,
    ),
    exclude_system: list[str] | None = typer.Option(
        None,
        "--exclude-system",
        help="Advanced route, backend, or system-ID exclusion; repeat as needed.",
        hidden=True,
    ),
    strongest_only: bool | None = typer.Option(
        None,
        "--strongest-only/--optimize-route",
        help="Advanced control: bypass economics ordering and use strongest evidence.",
        hidden=True,
    ),
    policy_selection: str = typer.Option(
        "configured",
        "--policy",
        hidden=True,
    ),
    mode: str | None = typer.Option(None, "--mode", hidden=True),
    allow_no_file_change: bool = typer.Option(False, "--allow-no-file-change"),
    verbose: bool = typer.Option(False, "--verbose"),
    debug: bool = typer.Option(False, "--debug"),
    open_after: bool = typer.Option(False, "--open"),
    json_output: bool = typer.Option(False, "--json"),
    run_id: str | None = typer.Option(None, "--run-id", hidden=True),
) -> None:
    """Run one canonical deterministic closed loop."""

    try:
        task_text = resolve_task_input(task, task_file)
    except TaskInputError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from None

    repository = _resolve_run_repository(repo)
    try:
        configuration = apply_policy_preset(_load_config(), preset or "performance")
    except ValueError as error:
        _usage_error(str(error))
    selected_delivery = delivery or "approve"
    configure_run_experience(
        configuration,
        delivery_mode=selected_delivery,
        approval_mode=approval,
        policy_selection=policy_selection,
        routing_mode=mode,
    )
    economics = configuration.setdefault("economics", {})
    if not isinstance(economics, dict):
        _usage_error("config economics must be a YAML object")
    constraints = economics.setdefault("constraints", {})
    if not isinstance(constraints, dict):
        _usage_error("config economics.constraints must be a YAML object")
    if local_only is not None:
        constraints["local_only"] = local_only
    if maximum_known_route_cost is not None:
        constraints["maximum_known_cost_usd"] = maximum_known_route_cost
    if preferred_provider is not None:
        constraints["preferred_provider"] = preferred_provider
    if exclude_system:
        constraints["excluded_systems"] = sorted(set(exclude_system))
    if strongest_only is not None:
        constraints["strongest_only"] = strongest_only
    if allow_experimental and agent_system is None:
        _usage_error("--allow-experimental requires --agent-system")
    if agent_system is not None:
        try:
            backends = _load_backends(configuration)
            registry = build_agent_system_registry(configuration, backends)
            identity = registry.inspect(agent_system)
            backend_name = next(
                name
                for name, candidate in registry.by_backend.items()
                if candidate.system_id == identity.system_id
            )
            backend = backends[backend_name]
            assessment = assess_qualification(
                identity=identity,
                repository=repository_qualification_context(repository),
                requested_task=qualification_task_profile(
                    "unknown", "hard", "high", ()
                ),
                configuration=configuration,
                store=QualificationStore(),
                backend_execution_selection=backend.execution_environment,
            )
        except (
            OSError,
            StopIteration,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            _usage_error(f"manual agent-system override is invalid: {error}")
        if assessment.state == "unsupported":
            _usage_error(
                f"{identity.route_name} is Unsupported for this repository: {assessment.caveat}"
            )
        if assessment.state == "experimental" and not allow_experimental:
            _usage_error(
                f"{identity.route_name} is Experimental for this repository; rerun with "
                "--agent-system and --allow-experimental to acknowledge manual-only execution"
            )
        qualification = configuration.setdefault("qualification", {})
        if not isinstance(qualification, dict):
            _usage_error("config qualification must be a YAML object")
        qualification["manual_override"] = {
            "route_name": identity.route_name,
            "system_id": identity.system_id,
            "allow_experimental": allow_experimental,
            "display_state": assessment.state,
            "qualification_created": False,
        }
        constraints["forced_system"] = identity.route_name
        constraints["allow_experimental_forced"] = allow_experimental
        typer.echo(
            f"Manual agent-system override: {identity.route_name} — "
            f"{assessment.state.capitalize()}. The forced choice is excluded from "
            "automatic-policy metrics; qualification still requires every evidence rule.",
            err=True,
        )
    prepare_repository_validation(
        configuration,
        repository,
        manual_commands=validation_command or (),
        confirm_low_confidence=confirm_validation,
        allow_prompt=True,
        confirmed_by="cli",
        quiet=json_output,
    )
    attempts_budget, cost_budget, wall_budget = resolve_run_budgets(
        configuration,
        max_attempts=max_attempts,
        max_cost=max_cost,
        max_wall_time=max_wall_time,
    )
    if max_wall_time is None:
        wall_budget = None
    policy = configuration.setdefault("policy", {})
    if not isinstance(policy, dict):
        _usage_error("config policy must be a YAML object")
    if accepted_candidates_required is not None:
        if accepted_candidates_required < 1:
            _usage_error("--accepted-candidates-required must be at least 1")
        policy["accepted_candidates_required"] = accepted_candidates_required
    result = _execute_new_run(
        task=task_text,
        repository=repository,
        success_criteria=(
            success_criteria if success_criteria is not None else task_text
        ),
        configuration=configuration,
        max_attempts=attempts_budget,
        max_cost=cost_budget,
        max_wall_time=wall_budget,
        requires_file_changes=not allow_no_file_change,
        verbose=verbose,
        debug=debug,
        json_output=json_output,
        run_id=run_id,
    )
    _finish_run(
        result,
        verbose=verbose or debug,
        open_after=open_after,
        json_output=json_output,
    )


def _latest_interrupted_run(root: Path) -> str | None:
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name == ".locks":
            continue
        state = _read_json(directory / "state.json")
        if state and not bool(state.get("terminal")):
            candidates.append(directory)
    return (
        max(candidates, key=lambda item: item.stat().st_mtime).name
        if candidates
        else None
    )


def _resume_materialization_is_safe(run_dir: Path, state: Mapping[str, Any]) -> None:
    """Fail before recovery mutates a repository whose materialization baseline changed."""

    if str(state.get("state")) not in {"SELECTING", "MATERIALIZING", "VERIFIED"}:
        return
    task = _read_json(run_dir / "task.json") or {}
    repository = Path(str(task.get("repository_path") or ""))
    if not repository.is_dir() or not _is_git_repository(repository):
        _run_usage_error(
            "repository_changed_before_materialization",
            "The target repository is missing or is no longer a Git work tree.",
            patch_preserved=(run_dir / "final.patch").is_file(),
        )
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repository,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or result.stdout:
        _run_usage_error(
            "repository_changed_before_materialization",
            "The target repository changed before patch materialization; refusing an unsafe apply.",
            patch_preserved=(run_dir / "final.patch").is_file(),
        )


def _resume_configuration(persisted: Mapping[str, Any]) -> dict[str, Any]:
    """Use the durable policy, but refresh credentials from the current config.

    Run bundles deliberately redact direct API keys.  Keeping the persisted
    policy is important for deterministic recovery, while loading the current
    config supplies a redacted direct credential when one is available.
    """

    configuration = dict(persisted)
    path = _config_path()
    if not path.is_file():
        return configuration
    current = _load_config()
    persisted_backends = configuration.get("backends")
    current_backends = current.get("backends")
    if isinstance(persisted_backends, Mapping) and isinstance(
        current_backends, Mapping
    ):
        # Preserve the policy-selected backend/model/URL from the run bundle;
        # only restore a redacted direct key from the current credential store.
        merged_backends = {
            str(name): dict(value)
            for name, value in persisted_backends.items()
            if isinstance(value, Mapping)
        }
        for name, persisted in merged_backends.items():
            current_value = current_backends.get(name)
            if not isinstance(current_value, Mapping):
                continue
            if persisted.get("api_key") == "***REDACTED***" and current_value.get(
                "api_key"
            ):
                persisted["api_key"] = current_value["api_key"]
        configuration["backends"] = merged_backends
    return configuration


@app.command("resume")
def resume_command(
    run_id: str | None = typer.Argument(None, help="Interrupted canonical run ID."),
    latest: bool = typer.Option(
        False, "--latest", help="Resume the newest interrupted run."
    ),
    verbose: bool = typer.Option(False, "--verbose"),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    """Safely reconcile and continue an interrupted canonical closed-loop run."""

    if bool(run_id) == latest:
        _usage_error("provide exactly one of RUN_ID or --latest")
    root = _runs_root()
    selected = _latest_interrupted_run(root) if latest else run_id
    if not selected:
        _usage_error("no interrupted run was found")
    directory = _run_dir(selected)
    if not directory.is_dir():
        _usage_error(f"run not found: {selected}")
    try:
        manifest = _protocol_document(directory / "manifest.json")
        state = _protocol_document(directory / "state.json")
    except ValueError as error:
        _usage_error(f"recovery error: {error}")
    if bool(state.get("terminal")):
        terminal_state = str(state.get("state") or "FAILED")
        recovered_terminal_state = cast(
            Literal["COMPLETED", "EXHAUSTED", "FAILED", "CANCELLED"],
            terminal_state
            if terminal_state in {"COMPLETED", "EXHAUSTED", "FAILED", "CANCELLED"}
            else "FAILED",
        )
        raw_accounting_status = str(manifest.get("cost_accounting_status") or "unknown")
        accounting_status = cast(
            AccountingStatus,
            raw_accounting_status
            if raw_accounting_status
            in {"complete", "partial", "unknown", "not_applicable"}
            else "unknown",
        )
        result = ClosedLoopRunResult(
            run_id=str(selected),
            terminal_state=recovered_terminal_state,
            selected_attempt_id=(
                str(manifest.get("selected_attempt_id"))
                if manifest.get("selected_attempt_id")
                else None
            ),
            run_directory=directory,
            actual_known_cost_usd=(
                float(manifest["total_cost_usd"])
                if isinstance(manifest.get("total_cost_usd"), (int, float))
                else None
            ),
            accounting_status=accounting_status,
            failure_or_exhaustion_reason=None,
        )
        console.print(
            "This run is already terminal. No recovery action was taken and no cost was duplicated.",
            markup=False,
        )
        _print_terminal_summary(result, verbose=verbose or debug)
        console.print(
            f"To start from the same task with a new identity: villani rerun {selected}",
            markup=False,
        )
        return
    _resume_materialization_is_safe(directory, state)
    persisted_configuration = (manifest.get("metadata") or {}).get(
        "policy_configuration"
    )
    if not isinstance(persisted_configuration, Mapping):
        _usage_error("recovery error: run bundle has no usable persisted configuration")
    try:
        configuration = _resume_configuration(persisted_configuration)
    except typer.Exit:
        raise
    except Exception as error:
        _usage_error(f"recovery error: cannot load current credentials: {error}")
    builder = _controller_builder or build_controller
    try:
        controller = builder(
            dict(configuration),
            _run_progress_listener(root, verbose=verbose, debug=debug),
        )
    except (TypeError, ValueError, ValidationError) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"recovery error: invalid persisted configuration: {message}")
    try:
        result = controller.resume(selected, root)
    except Exception as error:
        _usage_error(f"recovery error: {redact_data(str(error))}")
    _print_terminal_summary(result, verbose=verbose or debug)
    if result.terminal_state == "EXHAUSTED":
        raise typer.Exit(3)
    if result.terminal_state == "FAILED":
        raise typer.Exit(4)
    if result.terminal_state == "CANCELLED":
        raise typer.Exit(130)


def _approval_controller(
    run_id: str, *, verbose: bool, debug: bool
) -> tuple[ClosedLoopController, Path]:
    root = _runs_root()
    directory = _run_dir(run_id)
    if not directory.is_dir():
        _usage_error(f"run not found: {run_id}")
    try:
        manifest = _protocol_document(directory / "manifest.json")
        state = _protocol_document(directory / "state.json")
    except ValueError as error:
        _usage_error(f"approval error: {error}")
    if str(state.get("state")) != "AWAITING_APPROVAL":
        _usage_error("approval action requires a run that is awaiting approval")
    persisted_configuration = _mapping(
        _mapping(manifest.get("metadata")).get("policy_configuration")
    )
    if not persisted_configuration:
        _usage_error("approval error: run bundle has no usable configuration")
    try:
        configuration = _resume_configuration(persisted_configuration)
        builder = _controller_builder or build_controller
        controller = builder(
            configuration,
            _run_progress_listener(root, verbose=verbose, debug=debug),
        )
    except typer.Exit:
        raise
    except (TypeError, ValueError, ValidationError) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"approval error: {message}")
    return controller, root


def _run_approval_action(
    run_id: str,
    *,
    action: str,
    reason: str,
    candidate_id: str | None,
    verbose: bool,
    debug: bool,
) -> None:
    controller, root = _approval_controller(run_id, verbose=verbose, debug=debug)
    actor = (
        os.environ.get("VILLANI_APPROVER") or os.environ.get("USERNAME") or "local-user"
    )
    try:
        result = controller.approval_action(
            run_id,
            root,
            action=action,  # type: ignore[arg-type]
            actor=actor,
            authenticated=False,
            authentication_type="local_cli",
            reason=reason,
            candidate_id=candidate_id,
        )
    except PermissionError as error:
        _usage_error(f"approval denied: {redact_data(str(error))}")
    except (OSError, TypeError, ValueError) as error:
        _usage_error(f"approval error: {redact_data(str(error))}")
    _finish_run(result, verbose=verbose or debug, open_after=False)


@app.command("approve")
def approve_command(
    run_id: str = typer.Argument(..., help="Run awaiting delivery approval."),
    reason: str = typer.Option("Approved after patch review.", "--reason"),
    verbose: bool = typer.Option(False, "--verbose"),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    """Approve and apply the persisted selected patch."""

    _run_approval_action(
        run_id,
        action="approve",
        reason=reason,
        candidate_id=None,
        verbose=verbose,
        debug=debug,
    )


@app.command("reject")
def reject_command(
    run_id: str = typer.Argument(..., help="Run awaiting delivery approval."),
    reason: str = typer.Option("Delivery rejected after patch review.", "--reason"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Reject delivery while preserving the selected patch and evidence."""

    _run_approval_action(
        run_id,
        action="reject",
        reason=reason,
        candidate_id=None,
        verbose=verbose,
        debug=False,
    )


@app.command("request-rerun")
def request_rerun_command(
    run_id: str = typer.Argument(..., help="Run awaiting delivery approval."),
    reason: str = typer.Option("A new coding attempt is required.", "--reason"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Record a rerun request without applying the selected patch."""

    _run_approval_action(
        run_id,
        action="request_rerun",
        reason=reason,
        candidate_id=None,
        verbose=verbose,
        debug=False,
    )


@app.command("choose-candidate")
def choose_candidate_command(
    run_id: str = typer.Argument(..., help="Run awaiting delivery approval."),
    candidate_id: str = typer.Argument(
        ..., help="Another acceptance-eligible candidate ID."
    ),
    reason: str = typer.Option("Selected during patch review.", "--reason"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    """Choose another eligible candidate when the active policy permits it."""

    _run_approval_action(
        run_id,
        action="choose_candidate",
        reason=reason,
        candidate_id=candidate_id,
        verbose=verbose,
        debug=False,
    )


def _source_max_attempts(run_directory: Path, default: int) -> int:
    try:
        for event in read_jsonl_tolerant(run_directory / "events.jsonl"):
            if event.get("event_type") != "run_created":
                continue
            value = _mapping(event.get("payload")).get("max_attempts")
            if isinstance(value, int) and value >= 1:
                return value
            break
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return default


def _delivery_mode_from_configuration(configuration: Mapping[str, Any]) -> str:
    experience = _mapping(configuration.get("run_experience"))
    selected = experience.get("delivery_mode")
    if selected in _DELIVERY_MODES:
        return str(selected)
    delivery = _mapping(configuration.get("delivery"))
    configured_mode = delivery.get("mode")
    if configured_mode in _DELIVERY_MODES:
        return str(configured_mode)
    default_mode = delivery.get("default_mode")
    if default_mode in _DELIVERY_MODES:
        return str(default_mode)
    kind = str(
        delivery.get("requested_materialization_type")
        or delivery.get("materialization_type")
        or "local_patch_apply"
    )
    if kind == "local_patch_apply" and str(delivery.get("approval_mode")) == "explicit":
        return "approve"
    return next(
        (name for name, value in _DELIVERY_MODES.items() if value == kind), "suggest"
    )


@app.command("rerun")
def rerun_command(
    run_id: str = typer.Argument(..., help="Canonical source run ID."),
    repo: Path | None = typer.Option(
        None, "--repo", help="Override the source repository."
    ),
    success_criteria: str | None = typer.Option(None, "--success-criteria"),
    validation_command: list[str] | None = typer.Option(
        None,
        "--validation-command",
        help="Override exact validation command; repeat for multiple commands.",
    ),
    confirm_validation: bool = typer.Option(False, "--confirm-validation"),
    delivery: str | None = typer.Option(None, "--delivery"),
    approval: str | None = typer.Option(None, "--approval", hidden=True),
    max_attempts: int | None = typer.Option(None, "--max-attempts"),
    max_cost: float | None = typer.Option(None, "--max-cost"),
    max_wall_time: float | None = typer.Option(None, "--max-wall-time"),
    accepted_candidates_required: int | None = typer.Option(
        None, "--accepted-candidates-required"
    ),
    preset: str | None = typer.Option(None, "--preset"),
    policy_selection: str | None = typer.Option(None, "--policy", hidden=True),
    mode: str | None = typer.Option(None, "--mode", hidden=True),
    verbose: bool = typer.Option(False, "--verbose"),
    debug: bool = typer.Option(False, "--debug"),
    open_after: bool = typer.Option(False, "--open"),
    new_run_id: str | None = typer.Option(None, "--run-id", hidden=True),
) -> None:
    """Run the same task again under a new canonical run identity."""

    source_directory = _run_dir(run_id)
    if not source_directory.is_dir():
        _usage_error(f"run not found: {run_id}")
    try:
        source_manifest = _protocol_document(source_directory / "manifest.json")
        source_task = _protocol_document(source_directory / "task.json")
    except ValueError as error:
        _usage_error(f"rerun error: {error}")
    source_configuration = _mapping(
        _mapping(source_manifest.get("metadata")).get("policy_configuration")
    )
    try:
        configuration = _resume_configuration(source_configuration)
        configuration = apply_policy_preset(configuration, preset)
    except Exception as error:
        _usage_error(f"rerun error: cannot load current credentials: {error}")

    repository_value = repo or Path(str(source_task.get("repository_path") or ""))
    repository = _resolve_run_repository(repository_value)
    selected_delivery = delivery or _delivery_mode_from_configuration(configuration)
    selected_approval = approval
    selected_policy = policy_selection or "configured"
    configure_run_experience(
        configuration,
        delivery_mode=selected_delivery,
        approval_mode=selected_approval,
        policy_selection=selected_policy,
        routing_mode=mode,
    )
    prepare_repository_validation(
        configuration,
        repository,
        manual_commands=validation_command or (),
        confirm_low_confidence=confirm_validation,
        allow_prompt=True,
        confirmed_by="cli_rerun",
    )
    configured_attempts, configured_cost, configured_wall = resolve_run_budgets(
        configuration,
        max_attempts=max_attempts,
        max_cost=max_cost,
        max_wall_time=max_wall_time,
    )
    attempts_budget = (
        configured_attempts
        if max_attempts is not None
        else _source_max_attempts(source_directory, configured_attempts)
    )
    policy = configuration.setdefault("policy", {})
    if not isinstance(policy, dict):
        _usage_error("config policy must be a YAML object")
    if accepted_candidates_required is not None:
        if accepted_candidates_required < 1:
            _usage_error("--accepted-candidates-required must be at least 1")
        policy["accepted_candidates_required"] = accepted_candidates_required

    source_lineage = _mapping(_mapping(source_task.get("metadata")).get("lineage"))
    root_run_id = str(source_lineage.get("root_run_id") or run_id)
    lineage = {
        "relationship": "rerun",
        "parent_run_id": run_id,
        "root_run_id": root_run_id,
        "source_terminal_state": source_manifest.get("final_state"),
        "cost_accounting": "new_run_only",
    }
    console.print(f"Previous run: {run_id}", markup=False)
    result = _execute_new_run(
        task=str(source_task.get("instruction") or ""),
        repository=repository,
        success_criteria=(
            success_criteria
            if success_criteria is not None
            else str(
                source_task.get("success_criteria") or source_task.get("instruction")
            )
        ),
        configuration=configuration,
        max_attempts=attempts_budget,
        max_cost=configured_cost,
        max_wall_time=configured_wall,
        requires_file_changes=bool(source_task.get("requires_file_changes", True)),
        verbose=verbose,
        debug=debug,
        lineage=lineage,
        run_id=new_run_id,
    )
    console.print(f"Current run: {result.run_id}", markup=False)
    _finish_run(result, verbose=verbose or debug, open_after=open_after)


def _protocol_document(path: Path) -> dict[str, Any]:
    try:
        document = _read_json(path)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path.name}: {error}") from error
    if document is None:
        raise ValueError(f"{path.name}: missing or not a JSON object")
    try:
        validate_protocol_document(document)
    except ProtocolValidationError as error:
        details = "; ".join(
            f"{issue.instance_path or '/'} [{issue.keyword}]"
            for issue in error.issues[:3]
        )
        raise ValueError(
            f"{path.name}: invalid canonical document at {details}"
        ) from error
    return document


def _run_dir(run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        _usage_error("run ID must be a single directory name")
    return _runs_root() / run_id


@app.command("runs")
def list_runs() -> None:
    """List canonical run manifests, isolating corrupt bundles."""

    root = _runs_root()
    directories = (
        sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if root.is_dir()
        else []
    )
    for directory in directories:
        try:
            manifest = _protocol_document(directory / "manifest.json")
            task = _protocol_document(directory / "task.json")
            selected_model = "-"
            selected = manifest.get("selected_attempt_id")
            if selected:
                attempt = _protocol_document(
                    directory / "attempts" / str(selected) / "attempt.json"
                )
                selected_model = str(attempt.get("model") or "unknown")
            cost = _cost_text(
                manifest.get("total_cost_usd"),
                manifest.get("cost_accounting_status"),
                manifest.get("currency", "USD"),
            )
            duration = (
                f"{manifest.get('total_duration_ms')} ms"
                if manifest.get("total_duration_ms") is not None
                else f"unknown ({manifest.get('duration_accounting_status')})"
            )
            console.print(
                f"{directory.name}: created={manifest.get('created_at')}; "
                f"repo={task.get('repository_path')}; "
                f"task={str(task.get('instruction', ''))[:60]}; "
                f"state={manifest.get('final_state')}; model={selected_model}; "
                f"attempts={len(manifest.get('attempt_ids') or [])}; "
                f"cost={cost}; duration={duration}",
                soft_wrap=True,
            )
        except Exception as error:
            console.print(
                f"{directory.name}: state=corrupt; "
                f"reason={str(redact_data(str(error)))[:100]}",
                soft_wrap=True,
            )


def _inspect_bundle(run_id: str) -> dict[str, Any]:
    directory = _run_dir(run_id)
    if not directory.is_dir():
        _usage_error(f"run not found: {run_id}")
    try:
        manifest = _protocol_document(directory / "manifest.json")
        task = _protocol_document(directory / "task.json")
        classification = _protocol_document(directory / "classification.json")
        decisions = read_jsonl_tolerant(directory / "policy_decisions.jsonl")
        for decision in decisions:
            validate_protocol_document(decision)
        attempts = [
            _protocol_document(
                directory / "attempts" / str(attempt_id) / "attempt.json"
            )
            for attempt_id in manifest.get("attempt_ids") or []
        ]
        verifications = [
            _protocol_document(
                directory / "verification" / f"{attempt.get('attempt_id')}.json"
            )
            for attempt in attempts
            if (
                directory / "verification" / f"{attempt.get('attempt_id')}.json"
            ).is_file()
        ]
        selection = (
            _protocol_document(directory / "selection.json")
            if (directory / "selection.json").is_file()
            else None
        )
        materialization = (
            _protocol_document(directory / "materialization.json")
            if (directory / "materialization.json").is_file()
            else None
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        ProtocolValidationError,
    ) as error:
        _usage_error(
            f"cannot inspect canonical run {run_id}: {redact_data(str(error))}"
        )
    artifacts = sorted(
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    )
    cost_components = [
        {
            "attempt_id": attempt.get("attempt_id"),
            "cost": (attempt.get("metadata") or {}).get("cost_breakdown"),
        }
        for attempt in attempts
    ]
    return redact_data(
        {
            "schema_version": "villani.inspect.v1",
            "run_id": run_id,
            "manifest": manifest,
            "task": task,
            "classification": classification,
            "policy_decisions": decisions,
            "attempts": attempts,
            "verifications": verifications,
            "selection": selection,
            "materialization": materialization,
            "cost_components": cost_components,
            "artifact_paths": artifacts,
        }
    )


@app.command("inspect")
def inspect_run(
    run_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect only canonical run-bundle documents."""

    bundle = _inspect_bundle(run_id)
    if json_output:
        typer.echo(json.dumps(bundle, ensure_ascii=False, sort_keys=True))
        return
    manifest = bundle["manifest"]
    classification = bundle["classification"]
    console.print(f"Run: {run_id}")
    console.print(f"State: {manifest.get('final_state')}")
    console.print(
        "Classification: "
        f"{classification.get('difficulty')} / {classification.get('risk')} / "
        f"{classification.get('category')}"
    )
    console.print("Policy decisions:")
    for decision in bundle["policy_decisions"]:
        console.print(
            f"- {decision.get('decision_id')}: {decision.get('action')} "
            f"backend={decision.get('chosen_backend') or '-'}; {decision.get('reason')}"
        )
    console.print("Attempts:")
    for attempt in bundle["attempts"]:
        console.print(
            f"- {attempt.get('attempt_id')}: {attempt.get('backend_name')}/"
            f"{attempt.get('model') or 'unknown'}; status={attempt.get('status')}; "
            f"tokens={attempt.get('input_tokens')}/{attempt.get('output_tokens')}; "
            f"cost={_cost_text(attempt.get('cost_usd'), attempt.get('cost_accounting_status'), manifest.get('currency', 'USD'))}"
        )
        cost = (attempt.get("metadata") or {}).get("cost_breakdown")
        if cost:
            console.print(f"  cost components: {json.dumps(cost, sort_keys=True)}")
    console.print("Verification:")
    for verification in bundle["verifications"]:
        console.print(
            f"- {verification.get('attempt_id')}: {verification.get('outcome')}; "
            f"eligible={verification.get('acceptance_eligible')}; "
            f"{verification.get('reason')}"
        )
    console.print(
        f"Selection: {(bundle.get('selection') or {}).get('selected_candidate_ids') or 'none'}"
    )
    console.print(
        f"Materialization: {(bundle.get('materialization') or {}).get('status') or 'not present'}"
    )
    console.print("Artifacts:")
    for path in bundle["artifact_paths"]:
        console.print(f"- {path}")


@app.command("evidence")
def evidence_command(
    run_id: str = typer.Argument(..., help="Canonical run ID."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect the recorded evidence and technical references for one run."""

    directory = _run_dir(run_id)
    if not directory.is_dir():
        _usage_error(f"run not found: {run_id}")
    try:
        product = build_product_run(directory)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        _usage_error(f"cannot inspect evidence for {run_id}: {redact_data(str(error))}")
    document = {
        "schema_version": "villani.evidence_index.v1",
        "run_id": run_id,
        "evidence_links": [
            item.model_dump(mode="json") for item in product.evidence_links
        ],
        "technical_detail_references": product.technical_detail_references,
    }
    if json_output:
        typer.echo(json.dumps(document, ensure_ascii=False, sort_keys=True))
        return
    console.print(f"Recorded evidence for {run_id}:", markup=False)
    for item in product.evidence_links:
        console.print(f"- {item.label}: {item.href}", markup=False)
    console.print("Technical details:", markup=False)
    for reference in product.technical_detail_references:
        console.print(f"- {reference}", markup=False)


def _split_command(value: str) -> list[str]:
    return [part.strip('"') for part in shlex.split(value, posix=os.name != "nt")]


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _monorepo_vfr_path() -> Path:
    return (
        _repository_root()
        / "components"
        / "villani-flight-recorder"
        / "dist"
        / "cli.js"
    )


def _resolved_prefix(command: str) -> list[str] | None:
    parts = _split_command(command)
    if not parts:
        return None
    prefix = resolve_command_prefix(parts[0])
    return [*prefix, *parts[1:]] if prefix else None


def _resolve_vfr_command() -> list[str] | None:
    configured = os.environ.get("VILLANI_VFR_COMMAND")
    if configured:
        resolved = _resolved_prefix(configured)
        if resolved:
            return resolved
    resolved_vfr = resolve_command_prefix("vfr")
    if resolved_vfr:
        return resolved_vfr
    bundled = _monorepo_vfr_path()
    node = resolve_command_prefix("node")
    if bundled.is_file() and node:
        return [*node, str(bundled)]
    return None


VFR_UNAVAILABLE = (
    "Flight Recorder is unavailable. Install the supported platform Villani "
    "distribution with `pipx install villani`. Monorepo developers may build "
    "with `cd components/villani-flight-recorder && npm install && npm run build` "
    "and set VILLANI_DEVELOPMENT_VFR=1. "
    "The legacy `npm install -g villani-flight-recorder` path is not required "
    "by the packaged product."
)


def _open_flight_recorder(run_id: str | None = None) -> None:
    root = _runs_root()
    root.mkdir(parents=True, exist_ok=True)
    if run_id is not None and not _run_dir(run_id).is_dir():
        _usage_error(f"run not found: {run_id}")
    prefix = _resolve_vfr_command()
    if prefix is None:
        _usage_error(VFR_UNAVAILABLE)
    if run_id is None:
        command = [*prefix, "launch", "--provider", "villani", "--root", str(root)]
    else:
        command = [
            *prefix,
            "replay",
            "--provider",
            "villani",
            "--root",
            str(root),
            "--id",
            run_id,
            "--open",
        ]
    try:
        completed = subprocess.run(command, check=False)
    except OSError as error:
        _usage_error(f"could not start Flight Recorder: {error}. {VFR_UNAVAILABLE}")
    if completed.returncode != 0:
        _usage_error(f"Flight Recorder exited with code {completed.returncode}")


@app.command("open")
def open_run(run_id: str | None = typer.Argument(None)) -> None:
    """Open the canonical run browser or one canonical run."""

    _open_flight_recorder(run_id)


if __name__ == "__main__":
    app()
