#!/usr/bin/env python3
"""Recorded clean-user setup, service, doctor, console, and sample-task gate."""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml
from villani_ops.executables import (
    discover_interpreter_scripts_directory,
    resolve_installed_executable,
    resolved_executable_prefix,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTS = ROOT / "onboarding-verification" / "artifacts" / "latest"
MODEL_FIXTURE = ROOT / "release-verification" / "fixtures" / "model_service.py"
SCREENSHOT_SCRIPT = ROOT / "onboarding-verification" / "capture_screenshots.mjs"
_PACKAGE_IDENTITY_QUERY = """
import importlib.metadata
import json
import platform

names = ('villani', 'villani-code', 'villani-ops', 'villani-agentd')
print(json.dumps({
    'python_version': platform.python_version(),
    'packages': {name: importlib.metadata.version(name) for name in names},
}, sort_keys=True))
"""


class GateFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommandRecord:
    command: list[str]
    cwd: str
    exit_code: int
    elapsed_seconds: float
    stdout_path: str
    stderr_path: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_artifacts(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    allowed = (ROOT / "onboarding-verification").resolve()
    if resolved != allowed and allowed not in resolved.parents:
        # Pytest and CI may explicitly choose an external temporary directory.
        if "pytest-" not in str(resolved).lower() and not os.environ.get(
            "VILLANI_ONBOARDING_ALLOW_EXTERNAL_ARTIFACTS"
        ):
            raise GateFailure(
                f"refusing artifact path outside onboarding-verification: {resolved}"
            )
    return resolved


def _literal_absolute(path: Path) -> Path:
    """Return an absolute path without following an interpreter symlink."""

    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _selected_installation(
    python: Path, parent_environment: dict[str, str]
) -> tuple[Path, Path, dict[str, str], dict[str, Any]]:
    selected = _literal_absolute(python)
    discovery = discover_interpreter_scripts_directory(
        selected, environ=parent_environment
    )
    if discovery.path is None:
        raise GateFailure(
            f"could not discover the selected interpreter scripts directory: "
            f"{discovery.diagnostic}"
        )
    scripts = discovery.path
    original_path = parent_environment.get("PATH", "")
    child_environment = dict(parent_environment)
    child_environment["PATH"] = str(scripts) + (
        os.pathsep + original_path if original_path else ""
    )
    resolutions = {
        name: resolve_installed_executable(
            name,
            interpreter=selected,
            environ=child_environment,
        )
        for name in ("villani", "villani-code", "villani-agentd", "vfr")
    }
    selected_directories = {
        os.path.normcase(os.path.abspath(str(scripts))),
        os.path.normcase(os.path.abspath(str(selected.parent))),
    }
    invalid = [
        item
        for item in resolutions.values()
        if item.path is None
        or item.source not in {"interpreter_scripts", "interpreter_parent"}
        or os.path.normcase(os.path.abspath(str(item.path.parent)))
        not in selected_directories
    ]
    if invalid:
        details = "; ".join(item.diagnostic for item in invalid)
        raise GateFailure(
            "required entry points were not all resolved from the selected "
            f"installation at {scripts}: {details}"
        )
    report = {
        name: {
            "path": str(item.path),
            "source": item.source,
            "candidates": [str(candidate) for candidate in item.candidates],
            "diagnostic": item.diagnostic,
        }
        for name, item in resolutions.items()
    }
    report["_prefixes"] = {
        name: list(
            resolved_executable_prefix(
                item, interpreter=selected, environ=child_environment
            )
        )
        for name, item in resolutions.items()
    }
    try:
        identity = subprocess.run(
            [str(selected), "-I", "-c", _PACKAGE_IDENTITY_QUERY],
            env=child_environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateFailure(
            f"could not record selected installation package identity: "
            f"{type(error).__name__}"
        ) from error
    if identity.returncode != 0:
        raise GateFailure(
            "selected installation package identity query failed with exit code "
            f"{identity.returncode}"
        )
    try:
        identity_document = json.loads(identity.stdout)
    except json.JSONDecodeError as error:
        raise GateFailure(
            "selected installation package identity query returned malformed output"
        ) from error
    if not isinstance(identity_document, dict):
        raise GateFailure("selected installation package identity is not an object")
    report["_runtime_identity"] = identity_document
    return selected, scripts, child_environment, report


def _run(
    records: list[CommandRecord],
    artifacts: Path,
    label: str,
    command: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path = ROOT,
    timeout: float = 300,
    require_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            shell=False,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        stdout = str(error.stdout or "")
        stderr = str(error.stderr or "") + f"\nTimed out after {timeout} seconds.\n"
        completed = subprocess.CompletedProcess(list(command), 124, stdout, stderr)
    stdout_path = artifacts / f"{label}.stdout.log"
    stderr_path = artifacts / f"{label}.stderr.log"
    stdout_path.write_text(completed.stdout or "", encoding="utf-8")
    stderr_path.write_text(completed.stderr or "", encoding="utf-8")
    records.append(
        CommandRecord(
            list(command),
            str(cwd.resolve()),
            int(completed.returncode),
            round(time.monotonic() - started, 3),
            str(stdout_path),
            str(stderr_path),
        )
    )
    if require_success and completed.returncode != 0:
        raise GateFailure(
            f"{label} failed with exit code {completed.returncode}; see {stdout_path} and {stderr_path}"
        )
    return completed


def _wait_for_endpoint(process: subprocess.Popen[bytes], path: Path) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise GateFailure(f"fixture model service exited with {process.returncode}")
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            endpoint = value.get("base_url") if isinstance(value, dict) else None
            if isinstance(endpoint, str) and endpoint:
                return endpoint
        time.sleep(0.05)
    raise GateFailure("fixture model service did not publish its endpoint")


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise GateFailure(
            f"git {' '.join(arguments)} failed in {repository}: {completed.stderr.strip()}"
        )
    return completed


def _delivery_repository(root: Path, name: str) -> Path:
    repository = root / name
    repository.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Villani Design Partner Gate")
    _git(repository, "config", "user.email", "gate@villani.invalid")
    (repository / "calculator.py").write_text(
        '"""Tiny disposable Villani delivery sample."""\n\n'
        "\ndef add(left: int, right: int) -> int:\n"
        "    return left + right\n",
        encoding="utf-8",
    )
    (repository / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import add\n\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    _git(repository, "add", "calculator.py", "test_calculator.py")
    _git(repository, "commit", "-m", "delivery fixture baseline")
    return repository.resolve()


def _repository_snapshot(repository: Path) -> dict[str, str]:
    return {
        "head": _git(repository, "rev-parse", "HEAD").stdout.strip(),
        "branch": _git(repository, "symbolic-ref", "--short", "HEAD").stdout.strip(),
        "status": _git(
            repository, "status", "--porcelain", "--untracked-files=all"
        ).stdout,
    }


def _atomic_delivery_configuration(
    path: Path, *, allow_automatic: bool, provider: str = "fixture"
) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise GateFailure("setup configuration is not a YAML object")
    delivery = document.setdefault("delivery", {})
    if not isinstance(delivery, dict):
        raise GateFailure("setup delivery configuration is not a YAML object")
    delivery["provider"] = provider
    authority = delivery.setdefault("authority_policy", {})
    if not isinstance(authority, dict):
        raise GateFailure("setup delivery authority is not a YAML object")
    authority["allow_automatic"] = allow_automatic
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    os.replace(temporary, path)


def _new_run_directory(home: Path, previous: set[str]) -> Path:
    runs_root = home / "runs"
    current = {
        item.name
        for item in runs_root.glob("run_*")
        if item.is_dir() and item.name != ".locks"
    }
    created = sorted(current - previous)
    if len(created) != 1:
        raise GateFailure(f"expected one new run directory, found {created}")
    return runs_root / created[0]


def _assert_terminal_explanation(output: str) -> None:
    for marker in (
        "ACCEPTED",
        "Delivery:",
        "Changed:",
        "Confidence and authority:",
        "Validation:",
        "Cost:",
        "Villani recovery:",
        "Next:",
    ):
        if marker not in output:
            raise GateFailure(f"terminal result omitted required section: {marker}")


def _prove_delivery_modes(
    *,
    records: list[CommandRecord],
    artifacts: Path,
    prefix: list[str],
    env: dict[str, str],
    home: Path,
    work: Path,
) -> dict[str, Any]:
    repositories = work / "delivery-repositories"
    repositories.mkdir(parents=True)
    configuration_path = home / "config.yaml"
    _atomic_delivery_configuration(configuration_path, allow_automatic=True)
    env["VILLANI_APPROVER"] = "design-partner-gate"
    task = "Add a typed subtract(left, right) function and a passing unittest."
    validation = "python -m unittest -q"
    results: dict[str, Any] = {}
    sequence = 20

    def execute(
        name: str,
        mode: str,
        *,
        require_success: bool = True,
    ) -> tuple[Path, Path, subprocess.CompletedProcess[str], dict[str, str]]:
        nonlocal sequence
        repository = _delivery_repository(repositories, name)
        before = _repository_snapshot(repository)
        previous = {
            item.name for item in (home / "runs").glob("run_*") if item.is_dir()
        }
        completed = _run(
            records,
            artifacts,
            f"{sequence:02d}-delivery-{name}",
            [
                *prefix,
                "run",
                task,
                "--repo",
                str(repository),
                "--success-criteria",
                "subtract(8, 3) returns 5 and repository tests pass",
                "--validation-command",
                validation,
                "--delivery",
                mode,
                "--max-attempts",
                "1",
            ],
            env=env,
            timeout=180,
            require_success=require_success,
        )
        sequence += 1
        return repository, _new_run_directory(home, previous), completed, before

    suggest_repo, suggest_run, suggest, suggest_before = execute("suggest", "suggest")
    suggest_delivery = json.loads(
        (suggest_run / "delivery.json").read_text(encoding="utf-8")
    )
    if _repository_snapshot(suggest_repo) != suggest_before:
        raise GateFailure("suggest delivery changed its target repository")
    if suggest_delivery.get("state") != "suggested":
        raise GateFailure("suggest delivery did not persist the suggested state")
    if not (suggest_run / "delivery" / "selected.patch").is_file():
        raise GateFailure("suggest delivery did not preserve the selected patch")
    if not (suggest_run / "verification" / "attempt_001.json").is_file():
        raise GateFailure("suggest delivery did not preserve verification evidence")
    _assert_terminal_explanation(suggest.stdout)
    results["suggest"] = {
        "status": "passed",
        "run_id": suggest_run.name,
        "repository_unchanged": True,
        "selected_patch_preserved": True,
        "evidence_preserved": True,
    }

    approve_repo, approve_run, initial_approval, approve_before = execute(
        "approve", "approve"
    )
    initial_state = json.loads((approve_run / "state.json").read_text(encoding="utf-8"))
    if initial_state.get("state") != "AWAITING_APPROVAL":
        raise GateFailure("approve delivery did not reach AWAITING_APPROVAL")
    if _repository_snapshot(approve_repo) != approve_before:
        raise GateFailure("approve delivery mutated the repository before approval")
    approved = _run(
        records,
        artifacts,
        f"{sequence:02d}-delivery-approve-restarted",
        [*prefix, "approve", approve_run.name, "--reason", "Gate evidence reviewed."],
        env=env,
        timeout=120,
    )
    sequence += 1
    approve_delivery = json.loads(
        (approve_run / "delivery.json").read_text(encoding="utf-8")
    )
    approval_audit = (approve_run / "approval-audit.jsonl").read_text(encoding="utf-8")
    if approve_delivery.get("state") != "applied" or "def subtract" not in (
        approve_repo / "calculator.py"
    ).read_text(encoding="utf-8"):
        raise GateFailure("restarted approval did not apply the selected patch")
    if (
        "design-partner-gate" not in approval_audit
        or '"action":"approve"' not in approval_audit
    ):
        raise GateFailure("approval audit did not record the actor and action")
    _assert_terminal_explanation(approved.stdout)
    results["approve"] = {
        "status": "passed",
        "run_id": approve_run.name,
        "initial_cli_exit_code": initial_approval.returncode,
        "persisted_across_process_restart": True,
        "actor": "design-partner-gate",
        "applied_after_approval": True,
    }

    reject_repo, reject_run, _pending_rejection, reject_before = execute(
        "reject", "approve"
    )
    _run(
        records,
        artifacts,
        f"{sequence:02d}-delivery-reject-restarted",
        [*prefix, "reject", reject_run.name, "--reason", "Gate rejection proof."],
        env=env,
        timeout=120,
    )
    sequence += 1
    reject_delivery = json.loads(
        (reject_run / "delivery.json").read_text(encoding="utf-8")
    )
    if _repository_snapshot(reject_repo) != reject_before:
        raise GateFailure("rejected delivery changed its target repository")
    if (
        reject_delivery.get("state") != "rejected"
        or not (reject_run / "delivery" / "selected.patch").is_file()
    ):
        raise GateFailure("rejection did not preserve the selected patch")
    results["reject"] = {
        "status": "passed",
        "run_id": reject_run.name,
        "repository_unchanged": True,
        "selected_patch_preserved": True,
    }

    _atomic_delivery_configuration(configuration_path, allow_automatic=False)
    denied_repo, denied_run, denied, denied_before = execute(
        "apply-denied", "apply", require_success=False
    )
    denied_delivery = json.loads(
        (denied_run / "delivery.json").read_text(encoding="utf-8")
    )
    if denied.returncode != 4:
        raise GateFailure(
            f"authority-denied apply exited {denied.returncode}, expected 4"
        )
    if _repository_snapshot(denied_repo) != denied_before:
        raise GateFailure("authority-denied apply changed its target repository")
    if (denied_delivery.get("failure") or {}).get(
        "code"
    ) != "delivery_authority_insufficient":
        raise GateFailure("authority-denied apply did not fail closed")
    if not (denied_run / "delivery" / "selected.patch").is_file():
        raise GateFailure("authority-denied apply lost its selected patch")

    _atomic_delivery_configuration(configuration_path, allow_automatic=True)
    apply_repo, apply_run, applied, _apply_before = execute("apply", "apply")
    apply_delivery = json.loads(
        (apply_run / "delivery.json").read_text(encoding="utf-8")
    )
    if apply_delivery.get("state") != "applied" or not (
        (apply_delivery.get("authority") or {}).get("permitted")
    ):
        raise GateFailure("authority-permitted apply did not apply")
    if "def subtract" not in (apply_repo / "calculator.py").read_text(encoding="utf-8"):
        raise GateFailure("authority-permitted apply omitted the selected patch")
    _assert_terminal_explanation(applied.stdout)
    results["apply"] = {
        "status": "passed",
        "run_id": apply_run.name,
        "insufficient_authority_failed_closed": True,
        "denied_run_id": denied_run.name,
        "permitted_authority_applied": True,
        "repository_identity_validated": bool(
            ((apply_delivery.get("result") or {}).get("delivery_receipt") or {})
            .get("metadata", {})
            .get("repository_identity_validated")
        ),
    }

    branch_repo, branch_run, branched, branch_before = execute("branch", "branch")
    branch_delivery = json.loads(
        (branch_run / "delivery.json").read_text(encoding="utf-8")
    )
    branch_metadata = (
        (branch_delivery.get("result") or {}).get("delivery_receipt") or {}
    ).get("metadata") or {}
    branch_worktree = Path(str(branch_metadata.get("delivery_worktree") or ""))
    if _repository_snapshot(branch_repo) != branch_before:
        raise GateFailure("branch delivery changed the original branch or working tree")
    if not branch_worktree.is_dir() or "def subtract" not in (
        branch_worktree / "calculator.py"
    ).read_text(encoding="utf-8"):
        raise GateFailure(
            "branch delivery worktree does not contain the selected patch"
        )
    if (
        not branch_metadata.get("branch")
        or not (branch_run / "delivery" / "selected.patch").is_file()
    ):
        raise GateFailure("branch delivery omitted durable branch or patch metadata")
    _assert_terminal_explanation(branched.stdout)
    results["branch"] = {
        "status": "passed",
        "run_id": branch_run.name,
        "original_repository_unchanged": True,
        "branch": branch_metadata["branch"],
        "worktree": str(branch_worktree),
        "selected_patch_preserved": True,
    }

    pr_repo, pr_run, pull_request, pr_before = execute("pull-request", "pull-request")
    pr_delivery = json.loads((pr_run / "delivery.json").read_text(encoding="utf-8"))
    pr_metadata = ((pr_delivery.get("result") or {}).get("delivery_receipt") or {}).get(
        "metadata"
    ) or {}
    pr_body = (pr_run / "delivery" / "pull-request-body.md").read_text(encoding="utf-8")
    required_body = (
        "## Task",
        "## Summary",
        "## Changed files",
        "## Validation",
        "## Verifier authority",
        "## Attempts and recovery",
        "## Cost",
        "generated by an agent",
    )
    if _repository_snapshot(pr_repo) != pr_before:
        raise GateFailure("pull-request delivery changed the original working tree")
    if not pr_metadata.get("commit") or not str(
        (pr_metadata.get("pull_request") or {}).get("url") or ""
    ).startswith("fixture://pull/"):
        raise GateFailure(
            "pull-request fixture did not record commit, push, and PR creation"
        )
    if any(marker not in pr_body for marker in required_body):
        raise GateFailure("pull-request body omitted required design-partner content")
    if "agentd.sqlite" in pr_body.lower() or "database_id" in pr_body.lower():
        raise GateFailure("pull-request body exposed an internal database identifier")
    _assert_terminal_explanation(pull_request.stdout)
    results["pull_request"] = {
        "status": "passed",
        "run_id": pr_run.name,
        "branch": pr_metadata.get("branch"),
        "commit": pr_metadata.get("commit"),
        "push": pr_metadata.get("push"),
        "pull_request": pr_metadata.get("pull_request"),
        "body_path": str(pr_run / "delivery" / "pull-request-body.md"),
        "original_repository_unchanged": True,
        "sensitive_content_absent": not any(
            marker.lower() in pr_body.lower()
            for marker in ("authorization: bearer", "api_key=", "sk-villani-")
        ),
    }
    return results


def _transcript_html(setup: str, doctor: str, open_output: str) -> str:
    def panel(identifier: str, title: str, command: str, body: str) -> str:
        return (
            f'<section id="{identifier}" class="panel"><header><i></i><b>{html.escape(title)}</b>'
            f"<span>{html.escape(command)}</span></header><pre>{html.escape(body)}</pre></section>"
        )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Villani guided setup recording</title><style>
*{{box-sizing:border-box}}body{{margin:0;padding:42px;background:#090d19;color:#e7ecfb;font:15px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}}
main{{width:1120px;margin:auto}}h1{{font:700 34px/1.2 Inter,system-ui,sans-serif;margin:0 0 8px}}.lede{{font:16px Inter,system-ui,sans-serif;color:#9caccf;margin:0 0 30px}}
.panel{{margin:0 0 28px;border:1px solid #2b3858;border-radius:14px;overflow:hidden;background:#11182a;box-shadow:0 22px 55px #0007}}
header{{height:52px;padding:0 18px;display:flex;align-items:center;gap:12px;background:#182238;border-bottom:1px solid #2b3858;font-family:Inter,system-ui,sans-serif}}
header i{{width:11px;height:11px;border-radius:50%;background:#45dfa7;box-shadow:0 0 14px #45dfa7}}header b{{font-size:15px}}header span{{margin-left:auto;color:#8fa0c2;font:13px ui-monospace,monospace}}
pre{{white-space:pre-wrap;word-break:break-word;margin:0;padding:22px;color:#dce6ff}}.stamp{{color:#56e0ae;font:700 14px Inter,system-ui,sans-serif;margin-bottom:12px}}
</style></head><body><main><div class="stamp">RECORDED INTEGRATION · {html.escape(utc_now())}</div><h1>Villani first-run setup</h1>
<p class="lede">Detected model → atomic configuration → service → diagnostic → sample task.</p>
{panel("setup", "Guided setup and sample task", "villani setup", setup)}
{panel("doctor", "Environment diagnostics", "villani doctor", doctor)}
{panel("open", "Console handoff", "villani open", open_output)}
</main></body></html>"""


def run_gate(
    *, artifacts: Path, python: Path, skip_screenshots: bool = False
) -> dict[str, Any]:
    artifacts = _safe_artifacts(artifacts)
    if artifacts.exists():
        shutil.rmtree(artifacts)
    artifacts.mkdir(parents=True)
    work = artifacts / "work"
    home = work / "home"
    temporary = work / "tmp"
    temporary.mkdir(parents=True)
    endpoint_file = work / "model-endpoint.json"
    model_requests = artifacts / "model-requests.jsonl"
    records: list[CommandRecord] = []
    parent_environment = dict(os.environ)
    try:
        selected_python, scripts_directory, env, executable_report = (
            _selected_installation(python, parent_environment)
        )
    except BaseException as error:
        selected_python = _literal_absolute(python)
        discovery = discover_interpreter_scripts_directory(
            selected_python, environ=parent_environment
        )
        failure_report = {
            "schema_version": "villani.onboarding_gate.v1",
            "started_at": utc_now(),
            "completed_at": utc_now(),
            "verdict": "ONBOARDING GATE FAILED",
            "python": str(selected_python),
            "selected_interpreter": str(selected_python),
            "scripts_directory": (
                str(discovery.path) if discovery.path is not None else None
            ),
            "scripts_directory_diagnostic": discovery.diagnostic,
            "entry_points": {},
            "commands": [],
            "screenshots": [],
            "failure": f"{type(error).__name__}: {error}",
        }
        (artifacts / "onboarding-report.json").write_text(
            json.dumps(failure_report, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts / "ONBOARDING_REPORT.txt").write_text(
            "ONBOARDING GATE FAILED\n", encoding="utf-8"
        )
        raise
    prefixes = executable_report.pop("_prefixes")
    runtime_identity = executable_report.pop("_runtime_identity")
    report: dict[str, Any] = {
        "schema_version": "villani.onboarding_gate.v1",
        "started_at": utc_now(),
        "verdict": "ONBOARDING GATE FAILED",
        "python": str(selected_python),
        "selected_interpreter": str(selected_python),
        "scripts_directory": str(scripts_directory),
        "caller_path_contained_scripts_directory": any(
            os.path.normcase(os.path.abspath(part))
            == os.path.normcase(str(scripts_directory))
            for part in parent_environment.get("PATH", "").split(os.pathsep)
            if part
        ),
        "entry_points": executable_report,
        "runtime_identity": runtime_identity,
        "certification_identity": {
            "git_commit_sha": os.environ.get("GITHUB_SHA"),
            "branch": os.environ.get("GITHUB_HEAD_REF")
            or os.environ.get("GITHUB_REF_NAME"),
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
            "operating_system": os.name,
        },
        "villani_home": str(home.resolve()),
        "temporary_directory": str(temporary.resolve()),
        "commands": [],
        "screenshots": [],
    }
    env.update(
        {
            "VILLANI_HOME": str(home.resolve()),
            "TEMP": str(temporary.resolve()),
            "TMP": str(temporary.resolve()),
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    for secret_name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "VILLANI_MODEL_API_KEY_ENV",
    ):
        env.pop(secret_name, None)
    model_process_log = (artifacts / "model-service.log").open("wb")
    fixture = subprocess.Popen(
        [
            str(selected_python),
            str(MODEL_FIXTURE),
            "--log",
            str(model_requests),
            "--endpoint-file",
            str(endpoint_file),
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=model_process_log,
        stderr=subprocess.STDOUT,
        shell=False,
    )
    prefix = list(prefixes["villani"])
    try:
        for index, name in enumerate(
            ("villani", "villani-code", "villani-agentd", "vfr")
        ):
            _run(
                records,
                artifacts,
                f"00-entrypoint-{index + 1}-{name}",
                [*prefixes[name], "--help"],
                env=env,
                timeout=60,
            )
        endpoint = _wait_for_endpoint(fixture, endpoint_file)
        setup = _run(
            records,
            artifacts,
            "01-setup",
            [
                *prefix,
                "setup",
                "--endpoint",
                endpoint,
                "--model",
                "fixture-onboarding",
                "--yes",
                "--start",
                "--no-automatic",
                "--no-open",
                "--sample",
            ],
            env=env,
        )
        if "Sample task completed successfully." not in setup.stdout:
            raise GateFailure("setup did not record a successful sample task")
        setup_record_path = home / "setup-record.json"
        setup_record = json.loads(setup_record_path.read_text(encoding="utf-8"))
        if setup_record.get("sample_exit_code") != 0:
            raise GateFailure("setup record does not prove sample task success")
        sample_path = Path(str(setup_record["sample"]["path"])).resolve()
        sample_validation = _run(
            records,
            artifacts,
            "02-sample-validation",
            [str(selected_python), "-m", "unittest", "-q"],
            env=env,
            cwd=sample_path,
        )
        sample_diff = _run(
            records,
            artifacts,
            "03-sample-diff",
            ["git", "diff", "--check"],
            env=env,
            cwd=sample_path,
        )
        if "def subtract" not in (sample_path / "calculator.py").read_text(
            encoding="utf-8"
        ):
            raise GateFailure("materialized sample patch does not contain subtract")
        sample_run_roots = sorted((home / "runs").glob("run_*"))
        if len(sample_run_roots) != 1:
            raise GateFailure(
                f"expected one recorded sample run, found {len(sample_run_roots)}"
            )
        sample_run_root = sample_run_roots[0]
        manifest = json.loads(
            (sample_run_root / "manifest.json").read_text(encoding="utf-8")
        )
        if manifest.get("final_state") != "COMPLETED" or not manifest.get(
            "selected_attempt_id"
        ):
            raise GateFailure("sample run did not reach a selected COMPLETED result")
        doctor_json = _run(
            records,
            artifacts,
            "04-doctor-json",
            [*prefix, "doctor", "--repo", str(sample_path), "--json"],
            env=env,
        )
        doctor_document = json.loads(doctor_json.stdout)
        if (
            not doctor_document.get("healthy")
            or not doctor_document.get("ok")
            or doctor_document.get("summary", {}).get("failed") != 0
        ):
            raise GateFailure("doctor did not report a healthy configured installation")
        if doctor_document.get("inferred_commands_executed") is not False:
            raise GateFailure(
                "doctor executed or misreported inferred validation commands"
            )
        connectivity = doctor_document.get("backend_connectivity") or []
        if not connectivity or any(
            item.get("model_tokens_spent") != 0
            for item in connectivity
            if isinstance(item, dict)
        ):
            raise GateFailure("doctor did not prove zero model-token spending")
        doctor_human = _run(
            records,
            artifacts,
            "05-doctor-human",
            [*prefix, "doctor", "--repo", str(sample_path)],
            env=env,
        )
        opened = _run(
            records,
            artifacts,
            "06-open",
            [*prefix, "open", "--print-only"],
            env=env,
        )
        service = _run(
            records,
            artifacts,
            "07-service-status",
            [*prefix, "service", "status", "--json"],
            env=env,
        )
        service_document = json.loads(service.stdout)
        if not service_document.get("running") or not service_document.get(
            "console_url"
        ):
            raise GateFailure("service did not report a running console")
        console_url = str(service_document["console_url"])
        if console_url not in opened.stdout:
            raise GateFailure("villani open did not return the running console URL")
        delivery_modes = _prove_delivery_modes(
            records=records,
            artifacts=artifacts,
            prefix=prefix,
            env=env,
            home=home,
            work=work,
        )
        transcript = artifacts / "setup-flow.html"
        transcript.write_text(
            _transcript_html(setup.stdout, doctor_human.stdout, opened.stdout),
            encoding="utf-8",
        )
        screenshots: list[str] = []
        if not skip_screenshots:
            node = shutil.which("node")
            if not node:
                raise GateFailure(
                    "Node.js is required to capture onboarding screenshots"
                )
            _run(
                records,
                artifacts,
                "08-screenshots",
                [
                    node,
                    str(SCREENSHOT_SCRIPT),
                    "--transcript",
                    str(transcript),
                    "--console-url",
                    console_url,
                    "--run-id",
                    manifest["run_id"],
                    "--output",
                    str(artifacts),
                ],
                env=env,
            )
            expected = (
                artifacts / "screenshots" / "01-setup-flow.png",
                artifacts / "screenshots" / "02-doctor.png",
                artifacts / "screenshots" / "03-villani-console.png",
                artifacts / "screenshots" / "04-sample-run.png",
                artifacts / "screenshots" / "05-sample-replay.png",
            )
            for path in expected:
                if not path.is_file() or path.stat().st_size < 1_000:
                    raise GateFailure(
                        f"required screenshot is missing or empty: {path}"
                    )
                screenshots.append(str(path))
        report.update(
            {
                "verdict": "ONBOARDING GATE PASSED",
                "configured_model": setup_record["selected_model"],
                "capability_status": "unrated",
                "configuration_path": str(home / "config.yaml"),
                "service_running": True,
                "console_url": console_url,
                "doctor": doctor_document,
                "sample_repository": str(sample_path),
                "sample_validation_exit_code": sample_validation.returncode,
                "sample_diff_check_exit_code": sample_diff.returncode,
                "sample_run_id": manifest["run_id"],
                "sample_final_state": manifest["final_state"],
                "sample_selected_attempt": manifest["selected_attempt_id"],
                "delivery_modes": delivery_modes,
                "screenshots": screenshots,
            }
        )
    except BaseException as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            stopped = _run(
                records,
                artifacts,
                "99-service-stop",
                [*prefix, "service", "stop", "--json"],
                env=env,
                timeout=30,
                require_success=False,
            )
            stopped_document = (
                json.loads(stopped.stdout) if stopped.stdout.strip() else {}
            )
            report["service_stopped"] = (
                stopped.returncode == 0 and not stopped_document.get("running", True)
            )
        except Exception as stop_error:
            report["service_stopped"] = False
            report["service_stop_error"] = f"{type(stop_error).__name__}: {stop_error}"
        if fixture.poll() is None:
            fixture.terminate()
            try:
                fixture.wait(timeout=5)
            except subprocess.TimeoutExpired:
                fixture.kill()
                fixture.wait(timeout=5)
        model_process_log.close()
        report["fixture_exit_code"] = fixture.returncode
        report["commands"] = [asdict(item) for item in records]
        report["completed_at"] = utc_now()
        if not report.get("service_stopped"):
            report["verdict"] = "ONBOARDING GATE FAILED"
        (artifacts / "onboarding-report.json").write_text(
            json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        (artifacts / "ONBOARDING_REPORT.txt").write_text(
            report["verdict"] + "\n", encoding="utf-8"
        )
    if not report.get("service_stopped"):
        raise GateFailure("Villani Service remained running after the recorded gate")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--skip-screenshots", action="store_true")
    args = parser.parse_args()
    try:
        report = run_gate(
            artifacts=args.artifacts,
            python=args.python,
            skip_screenshots=args.skip_screenshots,
        )
    except BaseException as error:
        print(f"ONBOARDING GATE FAILED: {error}", file=sys.stderr)
        return 1
    print(report["verdict"])
    print(f"Report: {args.artifacts.resolve() / 'onboarding-report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
