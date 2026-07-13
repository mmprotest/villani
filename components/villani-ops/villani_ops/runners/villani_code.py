from __future__ import annotations
import hashlib
import os
import subprocess
import json
import signal
import time
import threading
from pathlib import Path
from .base import RunnerContext, RunnerResult
from .villani_code_debug import write_runner_telemetry
from villani_ops.subprocess_utils import resolve_command_prefix
from villani_ops.providers import villani_code_provider


def provider_for_villani_code_cli(provider: str) -> str:
    return villani_code_provider(provider)


class VillaniCodeRunner:
    name = "villani_code"

    def build_prompt(self, c: RunnerContext) -> str:
        strategy = str(c.candidate_dimensions.get("prompt_strategy_id") or "direct")
        instructions = {
            "direct": "Solve the objective directly and validate the result.",
            "plan_first": "Inspect the repository and form a concise plan before editing, then validate the result.",
            "test_first": "Reproduce the issue or establish validation evidence before editing, then validate the result after editing.",
        }
        if strategy not in instructions:
            raise ValueError(f"unsupported prompt_strategy_id: {strategy}")
        return f"""Strategy: {strategy}\n{instructions[strategy]}\n\nObjective:\n{c.task_instruction}\n\nSuccess criteria:\n{c.success_criteria or "Not provided"}\n\nAttempt: {c.attempt_id}\nWork only in repo: {c.repo_path}\n"""

    def run(self, context: RunnerContext) -> RunnerResult:
        command_name = context.backend.command_name or "villani-code"
        command_prefix = (
            [command_name]
            if context.execution_prefix
            else resolve_command_prefix(command_name)
        )
        if command_prefix is None:
            return RunnerResult(
                exit_code=127,
                stderr=f"Villani Code command '{command_name}' was not found.",
            )
        api_key = context.backend.resolved_api_key() or ""
        prompt = self.build_prompt(context)
        requested = dict(context.candidate_dimensions)
        applied = {
            "agent": str(requested.get("agent") or "villani-code"),
            "backend_name": str(requested.get("backend_name") or context.backend.name),
            "model": requested.get("model") or context.backend.model,
            "prompt_strategy_id": str(requested.get("prompt_strategy_id") or "direct")
        }
        unsupported = {
            key: value
            for key, value in requested.items()
            if key not in {"agent", "backend_name", "model", "prompt_strategy_id"}
            and value not in {None, "default"}
        }
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        effective_digest = hashlib.sha256(
            json.dumps(applied, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        max_tokens = str(context.backend.max_tokens or 50000)
        cli_provider = provider_for_villani_code_cli(context.backend.provider)
        provider_warning = None
        if (
            (context.backend.provider or "").strip().lower()
            and cli_provider == context.backend.provider
            and cli_provider not in {"openai", "anthropic"}
        ):
            provider_warning = f"Unknown Villani Code CLI provider mapping for '{context.backend.provider}'; passing through unchanged."
        debug_dir = Path(context.run_dir) / "villani_code_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        telemetry_path = Path(context.run_dir) / "runner_telemetry.json"
        prompt_path = Path(context.run_dir) / "villani_code_prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        (Path(context.run_dir) / "effective_candidate_configuration.json").write_text(
            json.dumps(
                {
                    "requested_dimensions": requested,
                    "applied_dimensions": applied,
                    "unsupported_dimensions": unsupported,
                    "effective_prompt_digest": prompt_digest,
                    "effective_configuration_digest": effective_digest,
                    "runner_acknowledged": True,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        safe_inline_limit = int(
            os.environ.get("VILLANI_CODE_INLINE_PROMPT_LIMIT", "12000")
        )
        api_key_args = [] if context.secure_secret_injection else ["--api-key", api_key]
        if len(prompt) > safe_inline_limit:
            cmd = [
                command_name,
                "run",
                "--task-file",
                str(prompt_path),
                "--base-url",
                context.backend.base_url or "",
                "--model",
                context.backend.model,
                "--repo",
                context.repo_path,
                "--provider",
                cli_provider,
                *api_key_args,
                "--auto-approve",
                "--no-stream",
                "--max-tokens",
                max_tokens,
                "--debug",
                "trace",
                "--debug-dir",
                str(debug_dir),
            ]
        else:
            cmd = [
                command_name,
                "run",
                prompt,
                "--base-url",
                context.backend.base_url or "",
                "--model",
                context.backend.model,
                "--repo",
                context.repo_path,
                "--provider",
                cli_provider,
                *api_key_args,
                "--auto-approve",
                "--no-stream",
                "--max-tokens",
                max_tokens,
                "--debug",
                "trace",
                "--debug-dir",
                str(debug_dir),
            ]
        red = [("***REDACTED***" if x == api_key else x) for x in cmd]
        Path(context.run_dir, "villani_code_command.json").write_text(
            json.dumps(red, indent=2)
        )

        def _result(exit_code: int, stdout="", stderr=""):
            tel = write_runner_telemetry(debug_dir, telemetry_path, context.backend)
            warnings = list(tel.token_accounting_warnings)
            telemetry = tel.model_dump(mode="json")
            if provider_warning:
                warnings.append(provider_warning)
                telemetry.setdefault("token_accounting_warnings", []).append(
                    provider_warning
                )
            return RunnerResult(
                exit_code=exit_code,
                stdout=stdout or "",
                stderr=stderr or "",
                input_tokens=tel.input_tokens,
                output_tokens=tel.output_tokens,
                total_tokens=tel.total_tokens,
                total_cost=(
                    context.backend.estimate_cost(tel.input_tokens, tel.output_tokens)
                    if tel.token_accounting_status != "missing"
                    else None
                ),
                debug_artifact_dir=str(debug_dir),
                resolved_trace_dir=tel.resolved_trace_dir,
                telemetry_path=str(telemetry_path),
                duration_ms=tel.duration_ms,
                model_requests=tel.model_requests,
                model_failures=tel.model_failures,
                total_tool_calls=tel.total_tool_calls,
                tool_calls_by_name=tel.tool_calls_by_name,
                total_file_reads=tel.total_file_reads,
                total_file_writes=tel.total_file_writes,
                commands_executed=tel.commands_executed,
                commands_failed=tel.commands_failed,
                first_substantive_file_read_tool_index=tel.first_substantive_file_read_tool_index,
                first_substantive_file_read_seconds=tel.first_substantive_file_read_seconds,
                first_file_mutation_tool_index=tel.first_file_mutation_tool_index,
                first_file_mutation_seconds=tel.first_file_mutation_seconds,
                first_command_tool_index=tel.first_command_tool_index,
                first_command_seconds=tel.first_command_seconds,
                token_accounting_status=tel.token_accounting_status,
                token_accounting_warnings=warnings,
                telemetry=telemetry,
            )

        def _norm(x):
            if x is None:
                return ""
            if isinstance(x, bytes):
                return x.decode(errors="replace")
            return str(x)

        def _close_pipes(p):
            for stream in (
                getattr(p, "stdin", None),
                getattr(p, "stdout", None),
                getattr(p, "stderr", None),
            ):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass

        def _windows_job(p):
            if os.name != "nt":
                return None
            try:
                import ctypes
                from ctypes import wintypes

                class BasicLimits(ctypes.Structure):
                    _fields_ = [
                        ("process_time", ctypes.c_longlong),
                        ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD),
                        ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t),
                        ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD),
                    ]

                class IoCounters(ctypes.Structure):
                    _fields_ = [
                        (name, ctypes.c_ulonglong)
                        for name in (
                            "read_ops",
                            "write_ops",
                            "other_ops",
                            "read_bytes",
                            "write_bytes",
                            "other_bytes",
                        )
                    ]

                class ExtendedLimits(ctypes.Structure):
                    _fields_ = [
                        ("basic", BasicLimits),
                        ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t),
                    ]

                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                job = kernel.CreateJobObjectW(None, None)
                limits = ExtendedLimits()
                limits.basic.flags = 0x00002000
                if (
                    not job
                    or not kernel.SetInformationJobObject(
                        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                    )
                    or not kernel.AssignProcessToJobObject(
                        job, wintypes.HANDLE(int(p._handle))
                    )
                ):
                    if job:
                        kernel.CloseHandle(job)
                    return None
                return job
            except Exception:
                return None

        def _close_windows_job(job, *, terminate=False):
            if not job:
                return False
            import ctypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            if terminate:
                kernel.TerminateJobObject(job, 1)
            kernel.CloseHandle(job)
            return True

        def _terminate_timed_out_process(p):
            if not p:
                return
            if os.name == "posix":
                try:
                    pgid = os.getpgid(p.pid)
                except Exception:
                    pgid = p.pid
                for sig, wait_s in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
                    try:
                        os.killpg(pgid, sig)
                    except ProcessLookupError:
                        pass
                    except Exception:
                        try:
                            os.kill(p.pid, sig)
                        except Exception:
                            pass
                    deadline = time.monotonic() + wait_s
                    while time.monotonic() < deadline and p.poll() is None:
                        time.sleep(0.05)
                    if p.poll() is not None:
                        break
            else:
                job = getattr(p, "_villani_job", None)
                if _close_windows_job(job, terminate=True):
                    try:
                        p.wait(timeout=2)
                    except Exception:
                        pass
                    return
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(p.pid), "/T", "/F"],
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                except Exception:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                try:
                    p.wait(timeout=1)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
                    try:
                        p.wait(timeout=2)
                    except Exception:
                        pass
            _close_pipes(p)
            try:
                p.wait(timeout=0.2)
            except Exception:
                pass

        proc = None
        try:
            environment = (
                {**os.environ, **context.env}
                if context.inherit_parent_environment
                else dict(context.env)
            )
            if context.secure_secret_injection and api_key:
                environment[
                    "ANTHROPIC_API_KEY"
                    if cli_provider == "anthropic"
                    else "OPENAI_API_KEY"
                ] = api_key
            popen_kwargs = {
                "cwd": context.repo_path,
                "text": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "env": environment,
            }
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen(
                [*context.execution_prefix, *command_prefix, *cmd[1:]], **popen_kwargs
            )
            if os.name == "nt":
                proc._villani_job = _windows_job(proc)
            cancellation_stop = threading.Event()
            cancelled = threading.Event()

            def _monitor_cancellation():
                event = context.cancellation_event
                if event is None:
                    return
                while not cancellation_stop.wait(0.05):
                    if event.is_set():
                        cancelled.set()
                        _terminate_timed_out_process(proc)
                        return

            cancellation_monitor = threading.Thread(
                target=_monitor_cancellation, daemon=True
            )
            cancellation_monitor.start()
            monitor_stop = threading.Event()
            disk_exceeded = threading.Event()

            def _monitor_workspace():
                if context.workspace_limit_bytes is None:
                    return
                while not monitor_stop.wait(0.05):
                    total = 0
                    for base, _dirs, files in os.walk(context.repo_path):
                        for name in files:
                            try:
                                total += os.path.getsize(os.path.join(base, name))
                            except OSError:
                                pass
                            if total > context.workspace_limit_bytes:
                                break
                        if total > context.workspace_limit_bytes:
                            break
                    if total > context.workspace_limit_bytes:
                        disk_exceeded.set()
                        if context.cleanup_command:
                            try:
                                subprocess.run(
                                    context.cleanup_command,
                                    text=True,
                                    capture_output=True,
                                    timeout=10,
                                    check=False,
                                )
                            except Exception:
                                pass
                        _terminate_timed_out_process(proc)
                        return

            monitor = threading.Thread(target=_monitor_workspace, daemon=True)
            monitor.start()
            try:
                stdout, stderr = proc.communicate(timeout=context.timeout_seconds)
            finally:
                monitor_stop.set()
                monitor.join(timeout=2)
                cancellation_stop.set()
                cancellation_monitor.join(timeout=2)
            if os.name == "nt":
                _close_windows_job(getattr(proc, "_villani_job", None))
            if cancelled.is_set():
                return _result(
                    130, stdout, (stderr or "") + "\nCandidate execution cancelled"
                )
            if disk_exceeded.is_set():
                return _result(
                    125, stdout, (stderr or "") + "\nWorkspace disk limit exceeded"
                )
            return _result(proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as e:
            _terminate_timed_out_process(proc)
            r = _result(
                124,
                _norm(getattr(e, "stdout", None)),
                _norm(getattr(e, "stderr", None))
                + f"\nCommand timed out after {context.timeout_seconds}s",
            )
            r.token_accounting_warnings.append(
                "Runner timed out; telemetry may be partial."
            )
            r.telemetry.setdefault("token_accounting_warnings", []).append(
                "Runner timed out; telemetry may be partial."
            )
            Path(telemetry_path).write_text(json.dumps(r.telemetry, indent=2))
            return r


class VillaniCodeAdapter(VillaniCodeRunner):
    name = "villani-code"

    def run_task(
        self,
        *,
        repo_path: Path,
        task: str,
        success_criteria: str | None,
        backend_name: str,
        backend_config,
        timeout_seconds: int | None,
        context: dict,
        artifacts_dir: Path,
    ) -> RunnerResult:
        return self.run(
            RunnerContext(
                attempt_id=str(context.get("attempt_id") or "attempt"),
                repo_path=str(repo_path),
                task_instruction=task,
                success_criteria=success_criteria,
                backend=backend_config,
                timeout_seconds=timeout_seconds
                or backend_config.timeout_seconds
                or 1200,
                run_dir=str(artifacts_dir),
            )
        )
