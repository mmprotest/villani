"""Shared verifier trace resolution and one-shot execution services."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from .deterministic import deterministic_result
from .errors import VerifierSchemaError
from .llm import llm_result
from .load_debug_run import load_debug_run
from .trace import redact


VerifierInvocation = Literal["in_process", "subprocess"]


@dataclass(frozen=True, slots=True)
class VerifierExecution:
    result: dict[str, Any]
    debug_dir: Path | None
    resolution_status: str
    resolution_reason: str
    invocation_status: str
    subprocess_exit_code: int | None = None


def is_verifier_debug_dir(path: Path) -> bool:
    return path.is_dir() and (path / "session_meta.json").exists()


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _debug_dir_score(path: Path) -> tuple[int, float, str]:
    score = 0
    if (path / "session_meta.json").exists():
        score += 10
    if (path / "final_summary.json").exists():
        score += 5
    if (path / "commands.jsonl").exists():
        score += 2
    if (path / "tool_calls.jsonl").exists():
        score += 2
    mtimes = [
        _safe_mtime(path / name)
        for name in ("final_summary.json", "session_meta.json", "summary.json")
        if (path / name).exists()
    ]
    if not mtimes:
        mtimes.append(_safe_mtime(path))
    return score, max(mtimes), path.name


def resolve_verifier_debug_dir(
    debug_root: Path | None,
    resolved_trace_dir: Path | None = None,
) -> Path | None:
    if resolved_trace_dir and is_verifier_debug_dir(resolved_trace_dir):
        return resolved_trace_dir
    candidates: list[Path] = []
    if debug_root and is_verifier_debug_dir(debug_root):
        candidates.append(debug_root)
    if debug_root and debug_root.exists():
        children = [child for child in debug_root.iterdir() if child.is_dir()]
        candidates.extend(child for child in children if is_verifier_debug_dir(child))
        for child in children:
            candidates.extend(
                grandchild
                for grandchild in child.iterdir()
                if grandchild.is_dir() and is_verifier_debug_dir(grandchild)
            )
    if not candidates:
        return None
    return max(set(candidates), key=_debug_dir_score)


def debug_resolution(
    debug_root: Path | None,
    resolved_trace_dir: Path | None,
) -> tuple[Path | None, str, str]:
    resolved = resolve_verifier_debug_dir(debug_root, resolved_trace_dir)
    if resolved is None:
        root = debug_root or resolved_trace_dir
        return (
            None,
            "missing",
            "No verifier-compatible Villani Code debug trace found. "
            f"Expected session_meta.json under {root} or one of its child trace "
            "directories.",
        )
    if resolved_trace_dir and resolved == resolved_trace_dir:
        return (
            resolved,
            "resolved",
            "selected runner resolved_trace_dir containing session_meta.json",
        )
    if debug_root and resolved == debug_root:
        return (
            resolved,
            "resolved",
            "selected debug root containing session_meta.json",
        )
    return (
        resolved,
        "resolved",
        "selected nested trace directory containing session_meta.json",
    )


def _error_result(reason: str) -> dict[str, Any]:
    return {
        "result": None,
        "verdict": "error",
        "confidence": 0.0,
        "recommendedAction": "inspect_manually",
        "reason": reason,
        "traceDir": None,
    }


def _write_result(path: Path, result: dict[str, Any]) -> None:
    safe_result = redact(result)
    result.clear()
    result.update(safe_result)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _read_result(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _subprocess_invocation_status(
    result: dict[str, Any], returncode: int | None
) -> str:
    """Preserve a nested verifier's fail-closed status across its CLI boundary."""

    declared = result.get("invocationStatus", result.get("invocation_status"))
    if declared in {
        "completed",
        "malformed_output",
        "timeout",
        "subprocess_failure",
    }:
        return str(declared)
    # A valid rejecting verifier exits one by CLI convention.  That is a
    # completed invocation, not an infrastructure failure.
    if result.get("verdict") in {"success", "failure"} and result.get("result") in {
        0,
        1,
    }:
        return "completed"
    if returncode in {None, 0}:
        return "completed"
    reason = str(result.get("reason") or "").lower()
    if "malformed" in reason:
        return "malformed_output"
    if "timeout" in reason or "timed out" in reason:
        return "timeout"
    return "subprocess_failure"


def execute_verifier(
    *,
    debug_root: Path | None,
    resolved_trace_dir: Path | None,
    repo_dir: Path,
    workspace: Path,
    out: Path,
    trace_dir: Path,
    backend: str | None = None,
    timeout_seconds: int = 180,
    max_tool_calls: int = 12,
    verifier: Callable[..., Any] | None = None,
    invocation: VerifierInvocation = "subprocess",
    no_llm: bool = False,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> VerifierExecution:
    """Resolve one trace and invoke the verifier exactly once without retries."""

    resolved, resolution_status, resolution_reason = debug_resolution(
        debug_root, resolved_trace_dir
    )
    if resolved is None:
        result = _error_result(resolution_reason)
        result = redact(result)
        _write_result(out, result)
        return VerifierExecution(
            result,
            None,
            resolution_status,
            resolution_reason,
            "missing_trace",
        )

    subprocess_exit_code: int | None = None
    try:
        if verifier is not None:
            returned = verifier(
                debug_dir=resolved,
                repo_dir=repo_dir,
                workspace=workspace,
                backend=backend,
                out=out,
                trace_dir=trace_dir,
            )
            if isinstance(returned, dict):
                result = returned
            else:
                result = _read_result(out)
                if result is None:
                    raise ValueError("malformed verifier output: expected an object")
        elif invocation == "in_process":
            run = load_debug_run(resolved)
            trace_writer = None
            if not no_llm:
                from .trace import VerifierTraceWriter

                trace_writer = VerifierTraceWriter(
                    workspace,
                    resolved,
                    trace_dir,
                    True,
                    "full",
                )
                trace_writer.start(
                    {
                        "debugDir": str(resolved),
                        "repoDir": str(repo_dir),
                        "backend": backend,
                        "model": model,
                    }
                )
            result = deterministic_result(
                run,
                repo_dir=str(repo_dir),
                mode="deterministic" if no_llm else "llm_tool_loop",
                model=model,
                base_url=base_url,
            )
            if not no_llm:
                result = llm_result(
                    run,
                    result,
                    workspace=str(workspace),
                    backend=backend,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    timeout_seconds=timeout_seconds,
                    max_tool_calls=max_tool_calls,
                    trace=trace_writer,
                )
                if trace_writer is not None:
                    trace_writer.finish(result)
        else:
            command = [
                sys.executable,
                "-m",
                "villani_ops.cli.main",
                "verifier",
                "--debug-dir",
                str(resolved),
                "--repo-dir",
                str(repo_dir),
                "--workspace",
                str(workspace),
                "--json",
                "--out",
                str(out),
                "--verifier-timeout-seconds",
                str(timeout_seconds),
                "--max-verifier-tool-calls",
                str(max_tool_calls),
                "--trace-dir",
                str(trace_dir),
            ]
            if backend:
                command += ["--backend", backend]
            if base_url:
                command += ["--base-url", base_url]
            if model:
                command += ["--model", model]
            process_env = None
            if api_key:
                process_env = {
                    **os.environ,
                    "VILLANI_OPS_VERIFIER_API_KEY": api_key,
                }
            process = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=timeout_seconds + 30,
                env=process_env,
            )
            subprocess_exit_code = process.returncode
            if stdout_path is not None:
                stdout_path.write_text(process.stdout or "", encoding="utf-8")
            if stderr_path is not None:
                stderr_path.write_text(process.stderr or "", encoding="utf-8")
            try:
                parsed = json.loads(process.stdout) if process.stdout.strip() else None
            except json.JSONDecodeError:
                parsed = None
            result = parsed if isinstance(parsed, dict) else _read_result(out)
            if result is None:
                raise ValueError(
                    "verifier subprocess failed with malformed output "
                    f"(exit {process.returncode})"
                )

        if not isinstance(result, dict):
            raise ValueError("malformed verifier output: expected an object")
        _write_result(out, result)
        return VerifierExecution(
            result,
            resolved,
            resolution_status,
            resolution_reason,
            _subprocess_invocation_status(result, subprocess_exit_code),
            subprocess_exit_code,
        )
    except subprocess.TimeoutExpired as error:
        reason = f"verifier timeout after {error.timeout} seconds"
        result = _error_result(reason)
        _write_result(out, result)
        return VerifierExecution(
            result,
            resolved,
            resolution_status,
            resolution_reason,
            "timeout",
            subprocess_exit_code,
        )
    except (json.JSONDecodeError, ValueError, TypeError, VerifierSchemaError) as error:
        reason = f"malformed verifier output: {error}"
        result = _error_result(reason)
        _write_result(out, result)
        return VerifierExecution(
            result,
            resolved,
            resolution_status,
            resolution_reason,
            "malformed_output",
            subprocess_exit_code,
        )
    except Exception as error:
        reason = f"verifier subprocess failed: {error}"
        result = _error_result(reason)
        _write_result(out, result)
        return VerifierExecution(
            result,
            resolved,
            resolution_status,
            resolution_reason,
            "subprocess_failure",
            subprocess_exit_code,
        )
