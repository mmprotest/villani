"""Sequential, resumable paired runner with arm-blind final verification."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import random
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from villani_ops.closed_loop.costs import actual_attempt_cost
from villani_ops.closed_loop.durable_io import write_json_atomic
from villani_ops.core.backend import Backend
from villani_ops.runners.base import RunnerContext
from villani_ops.runners.villani_code import VillaniCodeRunner
from villani_ops.subprocess_utils import resolve_command_prefix

from .models import (
    AccountingAmount,
    AgentSystemIdentity,
    DurationAmount,
    EvaluationSuite,
    EvaluationTask,
    EvaluationTrial,
    SetupCommand,
    ValidationCommand,
)
from .workspace import (
    canonical_digest,
    compact_artifact_path,
    contains_secret,
    load_suite,
    load_task,
    restore_snapshot,
    utc_now,
    validate_suite,
)


@dataclass(frozen=True, slots=True)
class ArmExecutionResult:
    run_id: str | None
    patch: bytes
    agent_system: AgentSystemIdentity
    execution_cost: AccountingAmount
    duration_ms: int
    attempts: int
    escalations: int
    product_proved_acceptable: bool | None
    artifact_references: tuple[str, ...] = ()
    exclusion_reason: str | None = None
    configuration_mode: str = "automatic"


@dataclass(frozen=True, slots=True)
class FinalVerificationResult:
    status: str
    proved_acceptable: bool | None
    duration_ms: int
    changed_files: tuple[str, ...]
    artifact_references: tuple[str, ...]
    failure_reason: str | None = None


class ArmExecutor(Protocol):
    def execute(
        self,
        *,
        arm: str,
        trial_id: str,
        runner_payload: Mapping[str, Any],
        workspace: Path,
        artifact_directory: Path,
    ) -> ArmExecutionResult: ...


class FinalVerifier(Protocol):
    def verify(
        self,
        *,
        suite_directory: Path,
        task: EvaluationTask,
        patch: bytes,
        artifact_directory: Path,
    ) -> FinalVerificationResult: ...


def _version(distribution: str, fallback: str = "unknown") -> str:
    module_name = {
        "villani-ops": "villani_ops",
        "villani-code": "villani_code",
    }.get(distribution)
    if module_name:
        try:
            value = getattr(importlib.import_module(module_name), "__version__", None)
        except ImportError:
            value = None
        if isinstance(value, str) and value:
            return value
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return fallback


def _redact_text(value: str) -> str:
    value = re.sub(
        r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        value,
    )
    return re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", value)


def _unknown_cost(source: str = "not_reported") -> AccountingAmount:
    return AccountingAmount(
        value=None,
        currency=None,
        accounting_status="unknown",
        source=source,
    )


def _not_applicable_cost(source: str) -> AccountingAmount:
    return AccountingAmount(
        value=None,
        currency=None,
        accounting_status="not_applicable",
        source=source,
    )


def _git_text(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


def capture_patch(repository: Path) -> tuple[bytes, tuple[str, ...]]:
    """Capture tracked and untracked changes as one binary-safe Git patch."""

    result = subprocess.run(
        ["git", "add", "-A"], cwd=repository, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError("could not stage the isolated candidate for capture")
    patch = subprocess.run(
        ["git", "diff", "--cached", "--binary", "--no-ext-diff", "HEAD"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if patch.returncode != 0:
        raise RuntimeError("could not capture the isolated candidate patch")
    changed = tuple(
        line
        for line in _git_text(
            repository, "diff", "--cached", "--name-only", "--diff-filter=ACDMRT", "HEAD"
        ).splitlines()
        if line
    )
    return bytes(patch.stdout), changed


def _backend_from_configuration(configuration: Mapping[str, Any]) -> Backend:
    raw_backends = configuration.get("backends")
    if not isinstance(raw_backends, Mapping):
        raise ValueError("no configured coding systems are available")
    candidates: list[Backend] = []
    for name, raw in raw_backends.items():
        if not isinstance(raw, Mapping):
            continue
        backend = Backend.model_validate({"name": str(name), **dict(raw)})
        if backend.enabled and "coding" in backend.roles:
            candidates.append(backend)
    if not candidates:
        raise ValueError("no usable configured coding system is available")
    return max(candidates, key=lambda item: (item.capability_score, item.name))


def _environment_identity(*, execution_provider: str) -> tuple[str, dict[str, Any]]:
    document = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "execution_provider": execution_provider,
        "villani_ops": _version("villani-ops", "1.0.0"),
        "villani_code": _version("villani-code", "1.0.0"),
    }
    return canonical_digest(document), document


def _display_command(argv: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(argv))
    import shlex

    return shlex.join(argv)


class ProductArmExecutor:
    """Use one direct Villani Code call or the normal Villani product loop."""

    def __init__(
        self,
        *,
        configuration: Mapping[str, Any],
        villani_home: Path,
        suite_directory: Path,
    ) -> None:
        self.configuration = dict(configuration)
        self.backend = _backend_from_configuration(configuration)
        self.villani_home = villani_home.resolve()
        self.suite_directory = suite_directory.resolve()

    def execute(
        self,
        *,
        arm: str,
        trial_id: str,
        runner_payload: Mapping[str, Any],
        workspace: Path,
        artifact_directory: Path,
    ) -> ArmExecutionResult:
        if arm == "direct":
            return self._direct(
                trial_id=trial_id,
                runner_payload=runner_payload,
                workspace=workspace,
                artifact_directory=artifact_directory,
            )
        if arm == "villani":
            return self._villani(
                trial_id=trial_id,
                runner_payload=runner_payload,
                workspace=workspace,
                artifact_directory=artifact_directory,
            )
        raise ValueError(f"unsupported evaluation arm: {arm}")

    def _identity(self, *, harness: str, environment_fingerprint: str) -> AgentSystemIdentity:
        metadata = self.backend.metadata if isinstance(self.backend.metadata, dict) else {}
        return AgentSystemIdentity(
            product="Villani" if harness == "villani_product" else "Direct coding system",
            product_version=_version("villani-ops", "1.0.0"),
            harness=harness,
            harness_version="founder_thesis_lab_v1",
            agent=self.backend.command_name or "villani-code",
            agent_version=_version("villani-code", "1.0.0"),
            model=self.backend.model,
            provider=self.backend.provider,
            serving_engine=(str(metadata.get("serving_engine")) if metadata.get("serving_engine") else None),
            serving_engine_version=(
                str(metadata.get("serving_engine_version"))
                if metadata.get("serving_engine_version")
                else None
            ),
            execution_provider=self.backend.execution_environment or "inherit",
            environment_fingerprint=environment_fingerprint,
        )

    def _direct(
        self,
        *,
        trial_id: str,
        runner_payload: Mapping[str, Any],
        workspace: Path,
        artifact_directory: Path,
    ) -> ArmExecutionResult:
        environment_fingerprint, environment_document = _environment_identity(
            execution_provider=self.backend.execution_environment or "inherit"
        )
        write_json_atomic(artifact_directory / "environment.json", environment_document)
        criteria_parts = [
            "Success criteria:",
            *(
                f"- {item}"
                for item in runner_payload["success_criteria"]
            ),
        ]
        visible_validation = runner_payload.get("validation", [])
        if visible_validation:
            criteria_parts.extend(
                [
                    "",
                    "Runner-visible authoritative validation:",
                    *(
                        f"- {_display_command(item['argv'])}"
                        for item in visible_validation
                    ),
                ]
            )
        criteria = "\n".join(str(item) for item in criteria_parts)
        env = {**os.environ, **self.backend.env}
        started = time.monotonic()
        result = VillaniCodeRunner().run(
            RunnerContext(
                attempt_id=trial_id,
                repo_path=str(workspace),
                task_instruction=str(runner_payload["task"]),
                success_criteria=criteria,
                backend=self.backend,
                timeout_seconds=self.backend.timeout_seconds or 1200,
                run_dir=str(artifact_directory),
                env=env,
                inherit_parent_environment=False,
                secure_secret_injection=True,
                candidate_dimensions={
                    "agent": self.backend.command_name or "villani-code",
                    "backend_name": self.backend.name,
                    "model": self.backend.model,
                    "prompt_strategy_id": "direct",
                },
            )
        )
        measured_duration = max(int((time.monotonic() - started) * 1000), 0)
        (artifact_directory / "stdout.txt").write_text(
            _redact_text(result.stdout), encoding="utf-8"
        )
        (artifact_directory / "stderr.txt").write_text(
            _redact_text(result.stderr), encoding="utf-8"
        )
        patch, _changed = capture_patch(workspace)
        patch_has_secret = contains_secret(patch)
        (artifact_directory / "candidate.patch").write_bytes(
            b"" if patch_has_secret else patch
        )
        tokens_known = result.token_accounting_status != "missing"
        breakdown = actual_attempt_cost(
            self.backend,
            input_tokens=result.input_tokens if tokens_known else None,
            output_tokens=result.output_tokens if tokens_known else None,
            duration_seconds=measured_duration / 1000,
        )
        cost = AccountingAmount(
            value=breakdown.total,
            currency=breakdown.currency if breakdown.total is not None else None,
            accounting_status=breakdown.accounting_status,
            source=breakdown.source,
        )
        return ArmExecutionResult(
            run_id=f"direct_{trial_id}",
            patch=b"" if patch_has_secret else patch,
            agent_system=self._identity(
                harness="direct_single_call", environment_fingerprint=environment_fingerprint
            ),
            execution_cost=cost,
            duration_ms=measured_duration,
            attempts=1,
            escalations=0,
            product_proved_acceptable=None,
            artifact_references=("execution/environment.json", "execution/candidate.patch"),
            exclusion_reason=(
                "candidate patch contained possible secret material"
                if patch_has_secret
                else f"direct agent exited with code {result.exit_code}"
                if result.exit_code != 0 and not patch
                else None
            ),
        )

    def _villani(
        self,
        *,
        trial_id: str,
        runner_payload: Mapping[str, Any],
        workspace: Path,
        artifact_directory: Path,
    ) -> ArmExecutionResult:
        prefix = resolve_command_prefix("villani")
        if prefix is None:
            raise RuntimeError("the Villani command is unavailable")
        task_file = artifact_directory / "task.txt"
        task_file.write_text(str(runner_payload["task"]), encoding="utf-8")
        run_id = "eval_" + hashlib.sha256(trial_id.encode()).hexdigest()[:24]
        command = [
            *prefix,
            "run",
            "--task-file",
            str(task_file),
            "--repo",
            str(workspace),
            "--success-criteria",
            "\n".join(str(item) for item in runner_payload["success_criteria"]),
            "--delivery",
            "approve",
            "--preset",
            "performance",
            "--json",
            "--run-id",
            run_id,
        ]
        for validation in runner_payload.get("validation", []):
            command.extend(["--validation-command", _display_command(validation["argv"])])
        environment = {**os.environ, "VILLANI_HOME": str(self.villani_home)}
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        measured_duration = max(int((time.monotonic() - started) * 1000), 0)
        (artifact_directory / "stdout.txt").write_text(
            _redact_text(completed.stdout), encoding="utf-8"
        )
        (artifact_directory / "stderr.txt").write_text(
            _redact_text(completed.stderr), encoding="utf-8"
        )
        run_directory = self.villani_home / "runs" / run_id
        if not run_directory.is_dir():
            raise RuntimeError("Villani did not create the expected canonical run bundle")
        manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
        selected = manifest.get("selected_attempt_id")
        patch_path = run_directory / "final.patch"
        if not patch_path.is_file() and isinstance(selected, str):
            patch_path = run_directory / "attempts" / selected / "patch.diff"
        patch = patch_path.read_bytes() if patch_path.is_file() else b""
        patch_has_secret = contains_secret(patch)
        (artifact_directory / "candidate.patch").write_bytes(
            b"" if patch_has_secret else patch
        )
        events = []
        event_path = run_directory / "events.jsonl"
        if event_path.is_file():
            for line in event_path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        product_path = run_directory / "product-run.json"
        product = (
            json.loads(product_path.read_text(encoding="utf-8"))
            if product_path.is_file()
            else {}
        )
        backend_name = None
        model = None
        if isinstance(selected, str):
            attempt_path = run_directory / "attempts" / selected / "attempt.json"
            if attempt_path.is_file():
                attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
                backend_name = attempt.get("backend_name")
                model = attempt.get("model")
        configured_backend = self.backend
        raw_backends = self.configuration.get("backends")
        if isinstance(raw_backends, Mapping) and isinstance(raw_backends.get(backend_name), Mapping):
            configured_backend = Backend.model_validate(
                {"name": str(backend_name), **dict(raw_backends[backend_name])}
            )
        environment_fingerprint, environment_document = _environment_identity(
            execution_provider=configured_backend.execution_environment or "inherit"
        )
        environment_document["canonical_run_id"] = run_id
        write_json_atomic(artifact_directory / "environment.json", environment_document)
        raw_cost = manifest.get("total_cost_usd")
        status = str(manifest.get("cost_accounting_status") or "unknown")
        cost = AccountingAmount(
            value=float(raw_cost) if isinstance(raw_cost, (int, float)) else None,
            currency=(str(manifest.get("currency") or "USD") if raw_cost is not None else None),
            accounting_status=(status if status in {"complete", "partial", "unknown"} else "unknown"),
            source="canonical_run_manifest",
        )
        identity = AgentSystemIdentity(
            product="Villani",
            product_version=_version("villani-ops", "1.0.0"),
            harness="villani_product",
            harness_version="founder_thesis_lab_v1",
            agent=configured_backend.command_name or "villani-code",
            agent_version=_version("villani-code", "1.0.0"),
            model=str(model) if model else configured_backend.model,
            provider=configured_backend.provider,
            serving_engine=(
                str(configured_backend.metadata.get("serving_engine"))
                if configured_backend.metadata.get("serving_engine")
                else None
            ),
            serving_engine_version=(
                str(configured_backend.metadata.get("serving_engine_version"))
                if configured_backend.metadata.get("serving_engine_version")
                else None
            ),
            execution_provider=configured_backend.execution_environment or "inherit",
            environment_fingerprint=environment_fingerprint,
        )
        return ArmExecutionResult(
            run_id=run_id,
            patch=b"" if patch_has_secret else patch,
            agent_system=identity,
            execution_cost=cost,
            duration_ms=measured_duration,
            attempts=len(manifest.get("attempt_ids") or []),
            escalations=sum(item.get("event_type") == "escalation_selected" for item in events),
            product_proved_acceptable=product.get("final_verdict") == "Ready to apply",
            artifact_references=(
                "execution/environment.json",
                "execution/candidate.patch",
                compact_artifact_path(run_directory, self.suite_directory),
            ),
            exclusion_reason=(
                "candidate patch contained possible secret material"
                if patch_has_secret
                else "Villani service or runner failed before producing a candidate"
                if completed.returncode not in {0, 3, 4} and not patch
                else None
            ),
        )


def _run_command(
    command: SetupCommand | ValidationCommand,
    *,
    workspace: Path,
    evaluator_only_directory: Path | None = None,
) -> dict[str, Any]:
    argv = [
        value.replace("{evaluator_only}", str(evaluator_only_directory or ""))
        for value in command.argv
    ]
    environment = dict(os.environ)
    if evaluator_only_directory is not None:
        environment["VILLANI_EVALUATOR_ONLY_DIR"] = str(evaluator_only_directory)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=command.timeout_seconds,
            check=False,
        )
        return {
            "argv": [_redact_text(value) for value in argv],
            "exit_code": completed.returncode,
            "passed": completed.returncode == 0,
            "duration_ms": max(int((time.monotonic() - started) * 1000), 0),
            "stdout": _redact_text(completed.stdout[-20_000:]),
            "stderr": _redact_text(completed.stderr[-20_000:]),
            "timed_out": False,
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "argv": [_redact_text(value) for value in argv],
            "exit_code": None,
            "passed": False,
            "duration_ms": max(int((time.monotonic() - started) * 1000), 0),
            "stdout": "",
            "stderr": _redact_text(str(error)),
            "timed_out": isinstance(error, subprocess.TimeoutExpired),
        }


def run_setup(commands: Sequence[SetupCommand], workspace: Path, output: Path) -> bool:
    baseline_head = _git_text(workspace, "rev-parse", "HEAD").strip()
    rows = [_run_command(command, workspace=workspace) for command in commands]
    write_json_atomic(
        output,
        {"schema_version": "villani.evaluation_setup.v1", "commands": rows},
    )
    # Setup may create ignored dependencies, but it may not rewrite, stage,
    # commit, or add visible files before either arm begins.
    head_unchanged = _git_text(workspace, "rev-parse", "HEAD", check=False).strip() == baseline_head
    status_clean = not _git_text(
        workspace, "status", "--porcelain", "--untracked-files=normal", check=False
    ).strip()
    return all(row["passed"] for row in rows) and head_unchanged and status_clean


class ArmBlindVerifier:
    """Run identical final verification without any arm or routing context."""

    def verify(
        self,
        *,
        suite_directory: Path,
        task: EvaluationTask,
        patch: bytes,
        artifact_directory: Path,
    ) -> FinalVerificationResult:
        started = time.monotonic()
        if contains_secret(patch):
            raise ValueError("candidate patch contains possible secret material")
        workspace = artifact_directory / "workspace"
        baseline_digest = restore_snapshot(
            suite_directory, task.source_snapshot, workspace, initialize_git=True
        )
        evaluator_material = artifact_directory / "evaluator-only"
        evaluator_material.mkdir(parents=True, exist_ok=True)
        for reference in task.evaluator_only.hidden_check_references:
            source = (suite_directory / reference).resolve()
            if not source.is_relative_to(suite_directory):
                raise ValueError("evaluator-only reference escapes the suite")
            shutil.copy2(source, evaluator_material / source.name)
        setup_ok = run_setup(
            task.allowed_setup, workspace, artifact_directory / "setup.json"
        )
        patch_path = artifact_directory / "candidate.patch"
        patch_path.write_bytes(patch)
        apply_ok = True
        apply_error = ""
        if patch:
            applied = subprocess.run(
                ["git", "apply", "--index", "--binary", str(patch_path)],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            apply_ok = applied.returncode == 0
            apply_error = _redact_text(applied.stderr)
        changed = tuple(
            line
            for line in _git_text(
                workspace,
                "diff",
                "--cached",
                "--name-only",
                "--diff-filter=ACDMRT",
                "HEAD",
                check=False,
            ).splitlines()
            if line
        )
        requirement = task.file_change_requirement
        change_ok = (
            (requirement.behavior == "required" and bool(changed))
            or (requirement.behavior == "forbidden" and not changed)
            or requirement.behavior == "optional"
        )
        allowed_ok = not requirement.allowed_path_prefixes or all(
            any(
                path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
                for prefix in requirement.allowed_path_prefixes
            )
            for path in changed
        )
        forbidden_ok = not any(
            path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
            for path in changed
            for prefix in requirement.forbidden_path_prefixes
        )
        verification_input = {
            "schema_version": "villani.evaluation_verification_input.v1",
            "baseline_digest": baseline_digest,
            "patch_digest": hashlib.sha256(patch).hexdigest(),
            "success_criteria": list(task.success_criteria),
            "file_change_requirement": requirement.model_dump(mode="json"),
            "commands": [
                command.model_dump(mode="json")
                for command in task.authoritative_validation
            ],
            "blind_fields_absent": [
                "arm",
                "harness",
                "route",
                "cost",
                "competing_candidates",
            ],
        }
        write_json_atomic(artifact_directory / "verification-input.json", verification_input)
        rows = [
            {
                "validation_id": command.validation_id,
                "authoritative": command.authoritative,
                **_run_command(
                    command,
                    workspace=workspace,
                    evaluator_only_directory=evaluator_material,
                ),
            }
            for command in task.authoritative_validation
        ] if apply_ok and setup_ok else []
        authoritative = [row for row in rows if row["authoritative"]]
        proved = bool(
            setup_ok
            and apply_ok
            and change_ok
            and allowed_ok
            and forbidden_ok
            and authoritative
            and all(row["passed"] for row in authoritative)
        )
        failure_reason = None
        if not setup_ok:
            failure_reason = "allowed setup failed or changed tracked baseline files"
        elif not apply_ok:
            failure_reason = f"candidate patch did not apply: {apply_error}"
        elif not change_ok or not allowed_ok or not forbidden_ok:
            failure_reason = "candidate did not satisfy the frozen file-change policy"
        elif not authoritative or not all(row["passed"] for row in authoritative):
            failure_reason = "authoritative validation did not all pass"
        write_json_atomic(
            artifact_directory / "verification-result.json",
            {
                "schema_version": "villani.evaluation_verification_result.v1",
                "proved_acceptable": proved,
                "failure_reason": failure_reason,
                "changed_files": list(changed),
                "commands": rows,
                "semantic_verification_blind": True,
            },
        )
        # Hidden checks and restored repositories are execution material, not
        # trial artifacts.  Only the patch, blind input, and bounded results
        # remain inspectable.
        shutil.rmtree(evaluator_material, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)
        return FinalVerificationResult(
            status="complete",
            proved_acceptable=proved,
            duration_ms=max(int((time.monotonic() - started) * 1000), 0),
            changed_files=changed,
            artifact_references=(
                "verification/verification-input.json",
                "verification/verification-result.json",
            ),
            failure_reason=failure_reason,
        )


def _local_compute_cost(
    suite: EvaluationSuite, duration_ms: int
) -> AccountingAmount:
    config = suite.local_compute
    if (
        config.measured_power_watts is None
        or config.electricity_price_per_kwh is None
        or config.currency is None
    ):
        return _unknown_cost("local_compute_measurement_not_configured")
    value = (
        config.measured_power_watts
        / 1000
        * (duration_ms / 3_600_000)
        * config.electricity_price_per_kwh
    )
    return AccountingAmount(
        value=value,
        currency=config.currency,
        accounting_status="complete",
        source="measured_power_runtime_and_electricity_price",
    )


def _total_cost(*amounts: AccountingAmount) -> AccountingAmount:
    known = [item for item in amounts if item.value is not None]
    known_values = [float(item.value) for item in known if item.value is not None]
    currencies = {item.currency for item in known}
    if len(currencies) > 1:
        return _unknown_cost("incompatible_currencies")
    unknown = any(item.accounting_status in {"unknown", "partial"} for item in amounts)
    if unknown:
        return AccountingAmount(
            value=sum(known_values) if known_values else None,
            currency=next(iter(currencies)) if known else None,
            accounting_status="partial" if known else "unknown",
            source="execution_verification_and_local_compute",
        )
    return AccountingAmount(
        value=sum(known_values),
        currency=next(iter(currencies)) if known else "USD",
        accounting_status="complete",
        source="execution_verification_and_local_compute",
    )


def _remove_isolation(path: Path, trial_directory: Path) -> None:
    resolved = path.resolve()
    trial_root = trial_directory.resolve()
    if resolved == trial_root or not resolved.is_relative_to(trial_root):
        raise RuntimeError("refusing cleanup outside the evaluation trial")
    if resolved.exists():

        def make_writable(function, value, _error) -> None:
            os.chmod(value, stat.S_IWRITE | stat.S_IREAD)
            function(value)

        shutil.rmtree(resolved, onerror=make_writable)


def _plan(
    suite: EvaluationSuite,
    *,
    arms: Sequence[str],
    repetitions: int,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for task in sorted(suite.task_versions, key=lambda item: item.task_id):
        for repetition in range(1, repetitions + 1):
            ordered = list(arms)
            rng = random.Random(
                canonical_digest(
                    {
                        "seed": suite.randomization_seed,
                        "task_digest": task.task_digest,
                        "repetition": repetition,
                    }
                )
            )
            rng.shuffle(ordered)
            order_digest = canonical_digest(ordered)
            for order, arm in enumerate(ordered, start=1):
                trial_id = "trial_" + canonical_digest(
                    {
                        "suite": suite.content_digest,
                        "task": task.task_digest,
                        "arm": arm,
                        "repetition": repetition,
                    }
                )[:24]
                entries.append(
                    {
                        "trial_id": trial_id,
                        "task_id": task.task_id,
                        "task_digest": task.task_digest,
                        "arm": arm,
                        "repetition": repetition,
                        "randomized_order": order,
                        "order_digest": order_digest,
                    }
                )
    return {
        "schema_version": "villani.evaluation_run_plan.v1",
        "suite_digest": suite.content_digest,
        "arms": list(arms),
        "repetitions": repetitions,
        "sequential": True,
        "entries": entries,
    }


def _placeholder_identity(arm: str) -> AgentSystemIdentity:
    return AgentSystemIdentity(
        product="Pending evaluation",
        product_version="unknown",
        harness=arm,
        harness_version="founder_thesis_lab_v1",
        agent="pending",
        agent_version="unknown",
        execution_provider="pending",
        environment_fingerprint="pending",
    )


def run_paired_suite(
    suite_directory: str | Path,
    *,
    arms: Sequence[str] = ("direct", "villani"),
    repetitions: int = 1,
    executor: ArmExecutor,
    verifier: FinalVerifier | None = None,
    stop_after: int | None = None,
) -> dict[str, int]:
    """Hold one atomic suite lock while planning and executing trials."""

    root = Path(suite_directory).expanduser().resolve()
    lock_path = root / "evaluation-run.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError(
            "another evaluation process owns this suite; if no process is active, "
            f"remove {lock_path} and rerun the same command"
        ) from error
    try:
        try:
            os.write(
                descriptor,
                json.dumps(
                    {
                        "schema_version": "villani.evaluation_run_lock.v1",
                        "process_id": os.getpid(),
                        "created_at": utc_now().isoformat(),
                    },
                    sort_keys=True,
                ).encode("utf-8"),
            )
        finally:
            os.close(descriptor)
        return _run_paired_suite_locked(
            root,
            arms=arms,
            repetitions=repetitions,
            executor=executor,
            verifier=verifier,
            stop_after=stop_after,
        )
    finally:
        lock_path.unlink(missing_ok=True)


def _run_paired_suite_locked(
    suite_directory: str | Path,
    *,
    arms: Sequence[str] = ("direct", "villani"),
    repetitions: int = 1,
    executor: ArmExecutor,
    verifier: FinalVerifier | None = None,
    stop_after: int | None = None,
) -> dict[str, int]:
    root = Path(suite_directory).expanduser().resolve()
    suite = load_suite(root)
    if suite.status != "frozen" or suite.content_digest is None:
        raise ValueError("evaluation run requires a frozen suite")
    validation = validate_suite(root)
    if not validation["valid"]:
        raise ValueError(f"evaluation suite validation failed: {validation['issues']}")
    selected_arms = tuple(dict.fromkeys(str(item) for item in arms))
    if not selected_arms or set(selected_arms) - {"direct", "villani"}:
        raise ValueError("arms must be direct and/or villani")
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    plan_path = root / "run-plan.json"
    expected_plan = _plan(suite, arms=selected_arms, repetitions=repetitions)
    if plan_path.is_file():
        existing_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if existing_plan != expected_plan:
            raise ValueError("existing randomized run plan differs from the requested run")
    else:
        write_json_atomic(plan_path, expected_plan)
    final_verifier = verifier or ArmBlindVerifier()
    completed = 0
    skipped = 0
    excluded = 0
    started_count = 0
    for entry in expected_plan["entries"]:
        trial_directory = root / "trials" / entry["trial_id"]
        trial_path = trial_directory / "trial.json"
        if trial_path.is_file():
            existing = EvaluationTrial.model_validate_json(
                trial_path.read_text(encoding="utf-8")
            )
            if existing.status in {"completed", "excluded"}:
                skipped += 1
                continue
        if stop_after is not None and started_count >= stop_after:
            break
        started_count += 1
        trial_directory.mkdir(parents=True, exist_ok=True)
        isolation = trial_directory / "isolation"
        if isolation.exists():
            _remove_isolation(isolation, trial_directory)
        isolation.mkdir()
        execution_workspace = isolation / "execution-workspace"
        execution_artifacts = trial_directory / "execution"
        verification_artifacts = trial_directory / "verification"
        execution_artifacts.mkdir(exist_ok=True)
        if verification_artifacts.exists():
            _remove_isolation(verification_artifacts, trial_directory)
        verification_artifacts.mkdir()
        task = load_task(root, entry["task_id"])
        restored = restore_snapshot(
            root, task.source_snapshot, execution_workspace, initialize_git=True
        )
        started_at = utc_now()
        running = EvaluationTrial(
            trial_id=entry["trial_id"],
            suite_id=suite.suite_id,
            suite_digest=suite.content_digest,
            task_id=task.task_id,
            task_digest=entry["task_digest"],
            arm=entry["arm"],
            repetition=entry["repetition"],
            randomized_order=entry["randomized_order"],
            order_digest=entry["order_digest"],
            status="running",
            started_at=started_at,
            agent_system=_placeholder_identity(entry["arm"]),
            baseline_digest=task.immutable_baseline_digest,
            baseline_restore_digest=restored,
            execution_cost=_unknown_cost(),
            verification_cost=_not_applicable_cost("final_verification_uses_local_commands"),
            local_compute_cost=_unknown_cost("duration_not_complete"),
            total_cost=_unknown_cost(),
            duration=DurationAmount(
                value_ms=None, accounting_status="unknown", source="trial_in_progress"
            ),
            verification_status="not_run",
            target_repository_modified=False,
            attempts=0,
            escalations=0,
            configuration_mode="automatic",
            artifact_references=[],
            evidence_eligible=False,
        )
        write_json_atomic(trial_path, running.model_dump(mode="json"))
        trial_started_monotonic = time.monotonic()
        setup_ok = run_setup(
            task.allowed_setup, execution_workspace, execution_artifacts / "setup.json"
        )
        try:
            if not setup_ok:
                execution = ArmExecutionResult(
                    run_id=None,
                    patch=b"",
                    agent_system=_placeholder_identity(entry["arm"]),
                    execution_cost=_not_applicable_cost("execution_not_started"),
                    duration_ms=0,
                    attempts=0,
                    escalations=0,
                    product_proved_acceptable=None,
                    exclusion_reason="allowed setup failed or changed tracked baseline files",
                )
                verification_result = FinalVerificationResult(
                    status="not_run",
                    proved_acceptable=None,
                    duration_ms=0,
                    changed_files=(),
                    artifact_references=(),
                    failure_reason=execution.exclusion_reason,
                )
            else:
                execution = executor.execute(
                    arm=entry["arm"],
                    trial_id=entry["trial_id"],
                    runner_payload=task.runner_payload(),
                    workspace=execution_workspace,
                    artifact_directory=execution_artifacts,
                )
                if contains_secret(execution.patch):
                    candidate_path = execution_artifacts / "candidate.patch"
                    if candidate_path.exists():
                        candidate_path.write_bytes(b"")
                    execution = replace(
                        execution,
                        patch=b"",
                        exclusion_reason="candidate patch contained possible secret material",
                    )
                    verification_result = FinalVerificationResult(
                        status="not_run",
                        proved_acceptable=None,
                        duration_ms=0,
                        changed_files=(),
                        artifact_references=(),
                        failure_reason=execution.exclusion_reason,
                    )
                else:
                    verification_result = final_verifier.verify(
                        suite_directory=root,
                        task=task,
                        patch=execution.patch,
                        artifact_directory=verification_artifacts,
                    )
        except KeyboardInterrupt:
            interrupted = running.model_copy(
                update={"status": "interrupted", "completed_at": utc_now()}
            )
            write_json_atomic(trial_path, interrupted.model_dump(mode="json"))
            raise
        except Exception as error:
            execution = ArmExecutionResult(
                run_id=None,
                patch=b"",
                agent_system=_placeholder_identity(entry["arm"]),
                execution_cost=_unknown_cost("execution_infrastructure_failure"),
                duration_ms=0,
                attempts=0,
                escalations=0,
                product_proved_acceptable=None,
                exclusion_reason=_redact_text(str(error)),
            )
            verification_result = FinalVerificationResult(
                status="infrastructure_failure",
                proved_acceptable=None,
                duration_ms=0,
                changed_files=(),
                artifact_references=(),
                failure_reason=_redact_text(str(error)),
            )
        finally:
            _remove_isolation(isolation, trial_directory)
            for transient in (
                verification_artifacts / "workspace",
                verification_artifacts / "evaluator-only",
            ):
                if transient.exists():
                    _remove_isolation(transient, trial_directory)
        duration_ms = max(
            int((time.monotonic() - trial_started_monotonic) * 1000), 0
        )
        local_compute = _local_compute_cost(suite, duration_ms)
        verification_cost = _not_applicable_cost(
            "arm_blind_authoritative_commands_have_no_reported_model_charge"
        )
        total = _total_cost(execution.execution_cost, verification_cost, local_compute)
        exclusion_reason = execution.exclusion_reason or (
            verification_result.failure_reason
            if verification_result.status in {"infrastructure_failure", "not_run"}
            else None
        )
        status = "excluded" if exclusion_reason else "completed"
        trial = EvaluationTrial(
            trial_id=entry["trial_id"],
            suite_id=suite.suite_id,
            suite_digest=suite.content_digest,
            task_id=task.task_id,
            task_digest=entry["task_digest"],
            arm=entry["arm"],
            repetition=entry["repetition"],
            randomized_order=entry["randomized_order"],
            order_digest=entry["order_digest"],
            status=status,
            started_at=started_at,
            completed_at=utc_now(),
            agent_system=execution.agent_system,
            run_id=execution.run_id,
            baseline_digest=task.immutable_baseline_digest,
            baseline_restore_digest=restored,
            execution_cost=execution.execution_cost,
            verification_cost=verification_cost,
            local_compute_cost=local_compute,
            total_cost=total,
            duration=DurationAmount(
                value_ms=duration_ms,
                accounting_status="complete",
                source="measured_trial_wall_clock",
            ),
            proved_acceptable=verification_result.proved_acceptable,
            verification_status=verification_result.status,
            exclusion_reason=exclusion_reason,
            target_repository_modified=False,
            attempts=execution.attempts,
            escalations=execution.escalations,
            verifier_disagreement=(
                execution.product_proved_acceptable
                != verification_result.proved_acceptable
                if execution.product_proved_acceptable is not None
                and verification_result.proved_acceptable is not None
                else None
            ),
            configuration_mode=(
                "automatic" if execution.configuration_mode == "automatic" else "manual"
            ),
            artifact_references=list(
                dict.fromkeys(
                    (
                        "trial.json",
                        "execution/setup.json",
                        *execution.artifact_references,
                        *verification_result.artifact_references,
                    )
                )
            ),
            evidence_eligible=bool(
                task.evidence_eligible
                and suite.evidence_kind == "real_founder_work"
            ),
        )
        write_json_atomic(trial_path, trial.model_dump(mode="json"))
        if status == "excluded":
            excluded += 1
        else:
            completed += 1
    return {"completed": completed, "skipped": skipped, "excluded": excluded}
