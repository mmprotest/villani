"""The single public Villani CLI for canonical closed-loop runs."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from villani_ops.classification import TaskClassifier
from villani_ops.closed_loop import (
    BootstrapPolicyEngine,
    ClosedLoopController,
    ClosedLoopRunRequest,
    ClosedLoopRunResult,
    EvidenceSelectorAdapter,
    PatchMaterializerAdapter,
    VillaniCodeAttemptAdapter,
    VillaniVerifierAdapter,
)
from villani_ops.closed_loop.durable_io import read_jsonl_tolerant
from villani_ops.closed_loop.capabilities.report import backend_score_rows
from villani_ops.closed_loop.capabilities.store import CapabilityStore
from villani_ops.closed_loop.event_writer import redact_data
from villani_ops.closed_loop.interfaces import (
    BudgetContext,
    Classification,
    ClassificationContext,
    PolicyContext,
)
from villani_ops.closed_loop.protocol import ClassificationSnapshot
from villani_ops.closed_loop.schema_validation import (
    ProtocolValidationError,
    validate_protocol_document,
)
from villani_ops.core.backend import Backend
from villani_ops.core.task import Task
from villani_ops.subprocess_utils import resolve_command_prefix


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
app.add_typer(backend_app, name="backend")
app.add_typer(capability_app, name="capability")


CONFIG_HEADER = """# Villani local-first configuration.
# Add backends with `villani backend add`; store secret values in environment
# variables and put only the variable name in api_key_env.
"""
DEFAULT_CONFIG: dict[str, Any] = {
    "policy": {
        "version": "bootstrap_v1",
        "easy_min_capability": 20,
        "medium_min_capability": 50,
        "hard_min_capability": 80,
        "economy_confidence_threshold": 0.80,
        "conservative_confidence_threshold": 0.65,
        "max_same_backend_retries": 1,
        "verifier_retry_limit": 1,
        "accepted_candidates_required": 1,
        "allow_constraint_violations": False,
        "allow_no_change_retry": False,
    },
    "capabilities": {
        "minimum_empirical_samples": 20,
        "target_success_probability": 0.80,
        "persisted_sequence_top_n": 100,
        "classifier_version": "task_classifier_v1",
        "verifier_version": "villani_ops_verifier_pipeline_v1",
        "scorer_version": "empirical_wilson_v1",
    },
    "budgets": {
        "max_attempts": 3,
        "max_cost": None,
        "max_wall_time": None,
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
    "backends": {},
}

_controller_builder: Callable[
    [Mapping[str, Any], Callable[[Any], None] | None], ClosedLoopController
] | None = None


def _home() -> Path:
    configured = os.environ.get("VILLANI_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".villani"


def _config_path() -> Path:
    return _home() / "config.yaml"


def _runs_root() -> Path:
    return _home() / "runs"


def _usage_error(message: str) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(2)


def _validation_message(error: ValidationError) -> str:
    details = []
    for issue in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in issue.get("loc", ())) or "value"
        details.append(f"{location}: {issue.get('msg', 'invalid value')}")
    return "; ".join(details) or "invalid configuration"


def _write_config(path: Path, configuration: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        dict(configuration),
        sort_keys=False,
        allow_unicode=True,
    )
    path.write_text(CONFIG_HEADER + payload, encoding="utf-8")


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
    return loaded


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


def _validate_run_backends(backends: Mapping[str, Backend]) -> None:
    if not any(
        backend.enabled and "classification" in backend.roles
        for backend in backends.values()
    ):
        _usage_error("an enabled backend with role 'classification' is required")
    if not any(
        backend.enabled and "coding" in backend.roles
        for backend in backends.values()
    ):
        _usage_error("an enabled backend with role 'coding' is required")


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
        _usage_error("--billing-mode must be token, compute_time, fixed, hybrid, or unknown")
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
            _usage_error(
                "--billing-mode token requires both token price options"
            )
        if compute_component or fixed_component:
            _usage_error("token billing cannot include compute-time or fixed costs")
    elif billing_mode == "compute_time":
        if not compute_component:
            _usage_error(
                "--billing-mode compute_time requires --compute-cost-per-hour"
            )
        if token_component or fixed_component:
            _usage_error("compute_time billing cannot include token or fixed costs")
    elif billing_mode == "fixed":
        if not fixed_component:
            _usage_error(
                "--billing-mode fixed requires --fixed-cost-per-attempt"
            )
        if token_component or compute_component:
            _usage_error("fixed billing cannot include token or compute-time costs")
    elif billing_mode == "hybrid":
        component_count = sum((token_component, compute_component, fixed_component))
        if component_count < 2:
            _usage_error("hybrid billing requires at least two configured cost components")
    elif any((token_component, compute_component, fixed_component)):
        _usage_error("unknown billing cannot include token, compute-time, or fixed costs")
    if (estimated_input is not None or estimated_output is not None) and not token_component:
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
    compute_cost_per_hour: float | None = typer.Option(
        None, "--compute-cost-per-hour"
    ),
    fixed_cost_per_attempt: float | None = typer.Option(
        None, "--fixed-cost-per-attempt"
    ),
    estimated_input_tokens: int | None = typer.Option(
        None, "--estimated-input-tokens"
    ),
    estimated_output_tokens: int | None = typer.Option(
        None, "--estimated-output-tokens"
    ),
    estimated_duration_seconds: float | None = typer.Option(
        None, "--estimated-duration-seconds"
    ),
    api_key_env: str | None = typer.Option(None, "--api-key-env"),
    timeout_seconds: int | None = typer.Option(None, "--timeout-seconds"),
    max_parallel: int = typer.Option(1, "--max-parallel"),
) -> None:
    """Add or replace one backend without resolving its secret."""

    roles = list(dict.fromkeys(role or ["coding"]))
    if "coding" in roles and capability_score is None:
        _usage_error("--capability-score is required for a coding backend")
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
        "compute_cost_per_hour": compute_cost_per_hour,
        "fixed_cost_per_attempt": fixed_cost_per_attempt,
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_duration_seconds": estimated_duration_seconds,
        "timeout_seconds": timeout_seconds,
        "max_parallel": max_parallel,
        "enabled": True,
        "metadata": {"allow_dummy_api_key": True} if provider == "local" else {},
    }
    try:
        backend = Backend.model_validate({"name": name, **payload})
    except ValidationError as error:
        _usage_error(f"backend configuration is invalid: {_validation_message(error)}")
    raw_backends[name] = backend.model_dump(
        mode="json", exclude={"name", "api_key"}
    )
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
        result = CapabilityStore().rebuild(
            _runs_root(), scorer_version=scorer_version
        )
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
        snapshot = CapabilityStore().load()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _usage_error(f"cannot read capability registry: {error}")
    if not backends:
        console.print("No backends configured.")
        return
    for row in backend_score_rows(
        backends, snapshot, minimum_empirical_samples=minimum
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
            f"samples={row['sample_count']}; conservative_probability={probability}",
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
    classification_backend = min(
        eligible, key=lambda item: (-item.capability_score, item.name)
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
    returned = _ClassifierAdapter(backends).classify(task, context)
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
    except (OSError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
        message = _validation_message(error) if isinstance(error, ValidationError) else str(error)
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
    def __init__(self, backends: Mapping[str, Backend]) -> None:
        self._backends = dict(backends)

    def classify(
        self, task: str, context: ClassificationContext
    ) -> Classification:
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
        classified, _ = TaskClassifier().classify(
            task_model,
            self._backends,
            backend_override=self._backends[backend_name],
        )
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
                "likely_files": list(classified.likely_files),
                "adjustment_notes": list(classified.adjustment_notes),
                "relevant_file_paths": list(classified.relevant_file_paths),
                "original_difficulty": classified.original_difficulty,
                "original_risk": classified.original_risk,
            },
        )


def build_controller(
    configuration: Mapping[str, Any],
    on_event: Callable[[Any], None] | None = None,
) -> ClosedLoopController:
    """Construct only the canonical controller and its M4/M5 dependencies."""

    backends = _load_backends(configuration)
    _validate_run_backends(backends)
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
    capability_snapshot = CapabilityStore().load()
    policy = BootstrapPolicyEngine(
        backends,
        configuration,
        capability_snapshot=capability_snapshot,
    )
    return ClosedLoopController(
        classifier=_ClassifierAdapter(backends),
        policy_engine=policy,
        attempt_runner=VillaniCodeAttemptAdapter(backends=backends),
        verifier=VillaniVerifierAdapter(
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
        ),
        selector=EvidenceSelectorAdapter(),
        materializer=PatchMaterializerAdapter(),
        on_event=on_event,
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


def _run_progress_listener(runs_root: Path) -> Callable[[Any], None]:
    shown: set[str] = set()
    state_events = {
        "classification_started": "CLASSIFYING",
        "classification_completed": "CLASSIFIED",
        "policy_selected": "POLICY_SELECTED",
        "attempt_started": "ATTEMPT_RUNNING",
        "attempt_completed": "ATTEMPT_COMPLETED",
        "verification_started": "VERIFYING",
        "verification_completed": "VERIFIED",
        "candidate_rejected": "REJECTED",
        "candidate_selected": "SELECTING",
        "materialization_started": "MATERIALIZING",
        "run_completed": "COMPLETED",
        "run_exhausted": "EXHAUSTED",
        "run_failed": "FAILED",
    }

    def listener(event: Any) -> None:
        if event.event_type == "run_created":
            setattr(listener, "run_created", True)
            console.print(f"Run ID: {event.run_id}")
            console.print(f"Run directory: {runs_root / event.run_id}")
            return
        state = state_events.get(event.event_type)
        if state and state not in shown:
            shown.add(state)
            console.print(f"State: {state}")

    setattr(listener, "run_created", False)
    return listener


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _cost_text(value: Any, status: Any) -> str:
    return f"USD {float(value):.6f}" if value is not None else f"unknown ({status})"


def _print_terminal_summary(
    result: ClosedLoopRunResult,
) -> None:
    run_dir = Path(result.run_directory)
    classification = _read_json(run_dir / "classification.json") or {}
    manifest = _read_json(run_dir / "manifest.json") or {}
    attempts = []
    for attempt_id in manifest.get("attempt_ids") or []:
        attempt = _read_json(run_dir / "attempts" / str(attempt_id) / "attempt.json")
        if attempt:
            attempts.append(attempt)
    verifications = []
    for attempt in attempts:
        verification = _read_json(
            run_dir / "verification" / f"{attempt.get('attempt_id')}.json"
        )
        if verification:
            verifications.append(verification)
    materialization = _read_json(run_dir / "materialization.json") or {}
    console.print(f"Terminal state: {result.terminal_state}")
    if classification:
        console.print(
            "Classification: "
            f"{classification.get('difficulty')} / {classification.get('risk')} / "
            f"{classification.get('category')} "
            f"(confidence {classification.get('confidence')})"
        )
    sequence = " -> ".join(
        f"{item.get('backend_name')}/{item.get('model') or 'unknown-model'}"
        for item in attempts
    )
    console.print(f"Attempts: {sequence or 'none'}")
    verifier_text = ", ".join(
        f"{item.get('attempt_id')}={item.get('outcome')}"
        for item in verifications
    )
    console.print(f"Verifier outcomes: {verifier_text or 'none'}")
    console.print(f"Selected attempt: {result.selected_attempt_id or 'none'}")
    console.print(
        "Cost: "
        + _cost_text(
            manifest.get("total_cost_usd"),
            manifest.get("cost_accounting_status", result.accounting_status),
        )
    )
    tokens = (
        f"{manifest.get('total_input_tokens')} input / "
        f"{manifest.get('total_output_tokens')} output"
        if manifest.get("total_input_tokens") is not None
        and manifest.get("total_output_tokens") is not None
        else f"unknown ({manifest.get('token_accounting_status', 'unknown')})"
    )
    console.print(f"Tokens: {tokens}")
    duration = (
        f"{manifest.get('total_duration_ms')} ms"
        if manifest.get("total_duration_ms") is not None
        else f"unknown ({manifest.get('duration_accounting_status', 'unknown')})"
    )
    console.print(f"Duration: {duration}")
    patch_status = materialization.get("status") or (
        "recorded" if (run_dir / "final.patch").is_file() else "not materialized"
    )
    console.print(f"Final patch: {patch_status}")
    if result.failure_or_exhaustion_reason:
        console.print(f"Reason: {result.failure_or_exhaustion_reason}")


@app.command("run")
def run_command(
    task: str = typer.Argument(..., help="Coding task, preserved verbatim."),
    repo: Path = typer.Option(..., "--repo", help="Existing Git repository."),
    success_criteria: str | None = typer.Option(None, "--success-criteria"),
    max_attempts: int | None = typer.Option(None, "--max-attempts"),
    max_cost: float | None = typer.Option(None, "--max-cost"),
    max_wall_time: float | None = typer.Option(None, "--max-wall-time"),
    accepted_candidates_required: int | None = typer.Option(
        None, "--accepted-candidates-required"
    ),
    open_after: bool = typer.Option(False, "--open"),
) -> None:
    """Run one canonical deterministic closed loop."""

    repository = repo.expanduser().resolve()
    if not repository.exists() or not repository.is_dir():
        _usage_error(f"repository does not exist or is not a directory: {repository}")
    if not _is_git_repository(repository):
        _usage_error(f"repository is not a Git work tree: {repository}")
    configuration = _load_config()
    budgets = configuration.get("budgets")
    if not isinstance(budgets, Mapping):
        budgets = {}
    attempts_budget = max_attempts if max_attempts is not None else budgets.get("max_attempts", 3)
    cost_budget = max_cost if max_cost is not None else budgets.get("max_cost")
    wall_budget = (
        max_wall_time
        if max_wall_time is not None
        else budgets.get("max_wall_time")
    )
    try:
        attempts_budget = int(attempts_budget)
        cost_budget = float(cost_budget) if cost_budget is not None else None
        wall_budget = float(wall_budget) if wall_budget is not None else None
    except (TypeError, ValueError):
        _usage_error("configured budgets must be numeric")
    if attempts_budget < 1:
        _usage_error("--max-attempts must be at least 1")
    if cost_budget is not None and cost_budget < 0:
        _usage_error("--max-cost must not be negative")
    if wall_budget is not None and wall_budget < 0:
        _usage_error("--max-wall-time must not be negative")
    policy = configuration.setdefault("policy", {})
    if not isinstance(policy, dict):
        _usage_error("config policy must be a YAML object")
    if accepted_candidates_required is not None:
        if accepted_candidates_required < 1:
            _usage_error("--accepted-candidates-required must be at least 1")
        policy["accepted_candidates_required"] = accepted_candidates_required
    runs_root = _runs_root()
    runs_root.mkdir(parents=True, exist_ok=True)
    builder = _controller_builder or build_controller
    progress_listener = _run_progress_listener(runs_root)
    try:
        controller = builder(configuration, progress_listener)
    except (TypeError, ValueError, ValidationError) as error:
        message = (
            _validation_message(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        _usage_error(f"invalid run configuration: {message}")
    request = ClosedLoopRunRequest(
        task=task,
        repository_path=repository,
        success_criteria=success_criteria if success_criteria is not None else task,
        runs_root=runs_root,
        max_attempts=attempts_budget,
        max_cost=cost_budget,
        max_wall_time=wall_budget,
        policy_configuration=configuration,
    )
    result = controller.run(request)
    if not bool(getattr(progress_listener, "run_created", False)):
        console.print(f"Run ID: {result.run_id}")
        console.print(f"Run directory: {result.run_directory}")
    _print_terminal_summary(result)
    if open_after:
        _open_flight_recorder(result.run_id)
    if result.terminal_state == "EXHAUSTED":
        raise typer.Exit(3)
    if result.terminal_state == "FAILED":
        raise typer.Exit(4)


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
        raise ValueError(f"{path.name}: invalid canonical document at {details}") from error
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
                directory
                / "verification"
                / f"{attempt.get('attempt_id')}.json"
            )
            for attempt in attempts
            if (directory / "verification" / f"{attempt.get('attempt_id')}.json").is_file()
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
    except (OSError, ValueError, json.JSONDecodeError, ProtocolValidationError) as error:
        _usage_error(f"cannot inspect canonical run {run_id}: {redact_data(str(error))}")
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
            f"cost={_cost_text(attempt.get('cost_usd'), attempt.get('cost_accounting_status'))}"
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


def _split_command(value: str) -> list[str]:
    return [part.strip('"') for part in shlex.split(value, posix=os.name != "nt")]


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _monorepo_vfr_path() -> Path:
    return _repository_root() / "components" / "villani-flight-recorder" / "dist" / "cli.js"


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
    "Flight Recorder is unavailable. Install it with "
    "`npm install -g villani-flight-recorder`, or build this monorepo with "
    "`cd components/villani-flight-recorder && npm install && npm run build`."
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
