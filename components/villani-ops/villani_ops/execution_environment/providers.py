"""Local execution providers for inherited and explicit setup environments."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

from .inspection import inspect_repository
from .models import (
    CommandResult,
    EnvironmentRemoval,
    ExecutionEnvironmentConfig,
    PreparedEnvironment,
)

PROVIDER_VERSION = "execution-environment-v1"
DEFAULT_SENSITIVE_VARIABLES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)


class ExecutionEnvironmentProvider(Protocol):
    config: ExecutionEnvironmentConfig

    def prepare(self, *, repository: Path, worktree: Path) -> PreparedEnvironment: ...
    def command_environment(self, prepared: PreparedEnvironment) -> dict[str, str]: ...
    def execute(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> CommandResult: ...
    def collect(self, prepared: PreparedEnvironment) -> dict[str, Any]: ...
    def cleanup(self, prepared: PreparedEnvironment) -> None: ...
    def capability_report(self) -> dict[str, Any]: ...
    def fingerprint(self, repository: Path) -> str: ...
    def wrap_command(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> list[str]: ...


def _normalized(path: Path) -> Path:
    # Lexical normalization avoids touching named pipes, unavailable network drives,
    # or every PATH entry. Explicit roots are not discovered by directory scanning.
    return Path(os.path.normpath(os.path.abspath(str(path.expanduser()))))


def _within(path: Path, root: Path) -> bool:
    try:
        _normalized(path).relative_to(_normalized(root))
        return True
    except ValueError:
        return False


def _tree_size(root: Path, limit: int | None = None) -> int:
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                continue
            if limit is not None and total > limit:
                return total
    return total


class InheritProvider:
    name: Literal["inherit", "setup-command"] = "inherit"

    def __init__(
        self,
        config: ExecutionEnvironmentConfig,
        *,
        source_environment: Mapping[str, str] | None = None,
        cache_root: Path | None = None,
    ) -> None:
        self.config = config
        self.source_environment = dict(
            os.environ if source_environment is None else source_environment
        )
        self.cache_root = cache_root

    def _private_roots(self, repository: Path, worktree: Path) -> tuple[Path, ...]:
        candidates = [
            Path(__file__).resolve().parents[1],
            *(Path(value) for value in self.config.private_paths),
        ]
        for name, value in self.source_environment.items():
            if name.startswith(("VILLANI_", "RUNNER_")) and value:
                candidate = Path(value)
                if candidate.is_absolute():
                    candidates.append(
                        candidate.parent if candidate.suffix else candidate
                    )
        roots: list[Path] = []
        for candidate in candidates:
            root = _normalized(candidate)
            if root not in roots:
                roots.append(root)
        return tuple(roots)

    def _environment(
        self, repository: Path, worktree: Path
    ) -> tuple[dict[str, str], list[EnvironmentRemoval]]:
        environment = dict(self.source_environment)
        removals: list[EnvironmentRemoval] = []
        denied = {name.upper() for name in self.config.denied_variables}
        sensitive = DEFAULT_SENSITIVE_VARIABLES | {
            name.upper() for name in self.config.sensitive_variables
        }
        private_roots = self._private_roots(repository, worktree)

        def private_path(path: Path) -> bool:
            return (
                any(_within(path, root) for root in private_roots)
                and not _within(path, repository)
                and not _within(path, worktree)
            )

        path_list_names = {"PATH", "Path", "PYTHONPATH", "NODE_PATH"}
        for name in list(environment):
            upper = name.upper()
            reason: Literal[
                "sensitive", "denied", "villani_private_variable"
            ] | None = None
            if upper in denied:
                reason = "denied"
            elif upper in sensitive:
                reason = "sensitive"
            elif upper.startswith(("VILLANI_", "RUNNER_")):
                reason = "villani_private_variable"
            if reason:
                environment.pop(name, None)
                removals.append(EnvironmentRemoval(name=name, reason=reason))
                continue
            if name in path_list_names:
                continue
            value_path = Path(environment[name])
            if value_path.is_absolute() and private_path(value_path):
                environment.pop(name, None)
                removals.append(
                    EnvironmentRemoval(name=name, reason="villani_private_path")
                )
        for path_name in path_list_names:
            if path_name not in environment:
                continue
            kept: list[str] = []
            removed = False
            for entry in environment[path_name].split(os.pathsep):
                candidate = Path(entry) if entry else None
                if (
                    candidate is not None
                    and candidate.is_absolute()
                    and private_path(candidate)
                ):
                    removed = True
                else:
                    kept.append(entry)
            environment[path_name] = os.pathsep.join(kept)
            if removed:
                removals.append(
                    EnvironmentRemoval(name=path_name, reason="villani_private_path")
                )
        return environment, removals

    def fingerprint(self, repository: Path) -> str:
        inspection = inspect_repository(repository)
        sanitized, _removals = self._environment(repository, repository)
        payload = {
            "provider": self.name,
            "provider_version": PROVIDER_VERSION,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "lockfiles": inspection["lockfile_digests"],
            "config": self.config.model_dump(mode="json"),
            "path_digest": hashlib.sha256(
                sanitized.get("PATH", sanitized.get("Path", "")).encode()
            ).hexdigest(),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def prepare(self, *, repository: Path, worktree: Path) -> PreparedEnvironment:
        environment, removals = self._environment(repository, worktree)
        return PreparedEnvironment(
            provider=self.name,
            provider_version=PROVIDER_VERSION,
            repository_path=str(repository.resolve()),
            worktree_path=str(worktree.resolve()),
            environment=environment,
            removals=removals,
            fingerprint=self.fingerprint(repository),
            cache_key=None,
            cache_hit=False,
            setup_result=None,
            inspection=inspect_repository(repository),
        )

    def command_environment(self, prepared: PreparedEnvironment) -> dict[str, str]:
        return dict(prepared.environment)

    def execute(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> CommandResult:
        wrapped = self.wrap_command(prepared, command)
        started = time.monotonic()
        try:
            result = subprocess.run(
                wrapped,
                cwd=prepared.worktree_path,
                env=prepared.environment,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.config.limits.timeout_seconds,
                check=False,
            )
            timed_out = False
        except subprocess.TimeoutExpired as error:
            result = subprocess.CompletedProcess(
                wrapped, 124, error.stdout or b"", error.stderr or b""
            )
            timed_out = True
        stdout_raw = bytes(result.stdout or b"")
        stderr_raw = bytes(result.stderr or b"")
        stdout = stdout_raw[: self.config.limits.stdout_bytes]
        stderr = stderr_raw[: self.config.limits.stderr_bytes]
        return CommandResult(
            exit_code=result.returncode,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            stdout_bytes=len(stdout_raw),
            stderr_bytes=len(stderr_raw),
            stdout_truncated=len(stdout_raw) > len(stdout),
            stderr_truncated=len(stderr_raw) > len(stderr),
            timed_out=timed_out,
            disk_limit_exceeded=False,
            process_limit_exceeded=False,
            failure_classification="timeout" if timed_out else None,
        )

    def wrap_command(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> list[str]:
        self.validate_command(prepared, command)
        return list(command)

    def validate_command(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> None:
        from .security import check_command

        prepared.policy_decisions.append(check_command(command, self.config.policy))

    def collect(self, prepared: PreparedEnvironment) -> dict[str, Any]:
        return {"worktree_size_bytes": _tree_size(Path(prepared.worktree_path))}

    def cleanup(self, prepared: PreparedEnvironment) -> None:
        return None

    def capability_report(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": True,
            "provider_version": PROVIDER_VERSION,
            "shell_free": True,
            "limits": None,
        }


class SetupCommandProvider(InheritProvider):
    name: Literal["setup-command"] = "setup-command"

    def _cache_key(self, repository: Path) -> str:
        inspection = inspect_repository(repository)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True, capture_output=True
        ).stdout.strip()
        payload = {
            "repository_head": head or None,
            "lockfile_digests": inspection["lockfile_digests"],
            "provider_version": PROVIDER_VERSION,
            "platform": platform.platform(),
            "setup": self.config.shell_command
            if self.config.shell
            else self.config.setup_argv,
            "shell": self.config.shell,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            try:
                getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
            except OSError:
                process.kill()

    def _assign_windows_job(self, process: subprocess.Popen[bytes]) -> Any:
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMITS),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x00000008 | 0x00002000
        limits.BasicLimitInformation.ActiveProcessLimit = (
            self.config.limits.process_count
        )
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel32.CloseHandle(job)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        if not kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(getattr(process, "_handle")))
        ):
            kernel32.CloseHandle(job)
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        return job

    def _run_setup(self, prepared: PreparedEnvironment) -> CommandResult:
        limits = self.config.limits
        command: Any = (
            self.config.shell_command
            if self.config.shell
            else list(self.config.setup_argv or [])
        )
        policy_command = (
            [str(self.config.shell_command)]
            if self.config.shell
            else list(self.config.setup_argv or [])
        )
        self.wrap_command(prepared, policy_command)
        before = _tree_size(Path(prepared.worktree_path))
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=prepared.worktree_path,
            env=prepared.environment,
            shell=self.config.shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        windows_job = self._assign_windows_job(process)
        captured: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        totals = {"stdout": 0, "stderr": 0}

        def drain(stream: Any, name: str, limit: int) -> None:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                totals[name] += len(chunk)
                remaining = max(0, limit - len(captured[name]))
                if remaining:
                    captured[name].extend(chunk[:remaining])

        assert process.stdout is not None and process.stderr is not None
        threads = [
            threading.Thread(
                target=drain, args=(process.stdout, "stdout", limits.stdout_bytes)
            ),
            threading.Thread(
                target=drain, args=(process.stderr, "stderr", limits.stderr_bytes)
            ),
        ]
        for thread in threads:
            thread.start()
        timed_out = disk_exceeded = process_exceeded = False
        while process.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed > limits.timeout_seconds:
                timed_out = True
            if (
                _tree_size(Path(prepared.worktree_path), before + limits.disk_bytes)
                - before
                > limits.disk_bytes
            ):
                disk_exceeded = True
            # Portable stdlib cannot enumerate a Windows process tree safely; POSIX /proc is checked.
            if os.name != "nt" and Path("/proc").is_dir():
                descendants = 1
                frontier = {process.pid}
                for stat in Path("/proc").glob("[0-9]*/stat"):
                    try:
                        fields = stat.read_text(errors="ignore").split()
                        if len(fields) > 3 and int(fields[3]) in frontier:
                            descendants += 1
                            frontier.add(int(fields[0]))
                    except (OSError, ValueError):
                        pass
                process_exceeded = descendants > limits.process_count
            if timed_out or disk_exceeded or process_exceeded:
                self._terminate(process)
                break
            time.sleep(0.05)
        process.wait()
        for thread in threads:
            thread.join(timeout=10)
        if _tree_size(Path(prepared.worktree_path)) - before > limits.disk_bytes:
            disk_exceeded = True
        if windows_job is not None:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(windows_job)
        stdout_b, stderr_b = bytes(captured["stdout"]), bytes(captured["stderr"])
        stdout_total, stderr_total = totals["stdout"], totals["stderr"]
        return CommandResult(
            exit_code=124
            if timed_out
            else 125
            if disk_exceeded or process_exceeded
            else int(process.returncode or 0),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
            stdout_bytes=stdout_total,
            stderr_bytes=stderr_total,
            stdout_truncated=stdout_total > len(stdout_b),
            stderr_truncated=stderr_total > len(stderr_b),
            timed_out=timed_out,
            disk_limit_exceeded=disk_exceeded,
            process_limit_exceeded=process_exceeded,
        )

    def prepare(self, *, repository: Path, worktree: Path) -> PreparedEnvironment:
        base = super().prepare(repository=repository, worktree=worktree)
        key = self._cache_key(repository)
        cache_root = self.cache_root or (
            Path.home() / ".villani" / "cache" / "execution-environments"
        )
        marker = cache_root / self.name / f"{key}.json"
        hit = self.config.cache and marker.is_file()
        environment = dict(base.environment)
        if self.config.cache:
            # The keyed directory is for dependency/download caches explicitly
            # consumed by setup tooling. Setup still runs in every fresh worktree;
            # a prior success marker can never substitute for worktree-local state.
            environment["VILLANI_SETUP_CACHE"] = str(marker.parent / key)
            Path(environment["VILLANI_SETUP_CACHE"]).mkdir(parents=True, exist_ok=True)
        prepared = base.model_copy(
            update={
                "provider": self.name,
                "cache_key": key,
                "cache_hit": hit,
                "environment": environment,
            }
        )
        result = self._run_setup(prepared)
        if result is not None and result.exit_code != 0:
            raise RuntimeError(
                f"setup command failed with exit code {result.exit_code}: {result.stderr[-1000:]}"
            )
        if self.config.cache:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": "villani.setup_cache.v1",
                        "cache_key": key,
                        "created_at_unix": int(time.time()),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        return prepared.model_copy(update={"setup_result": result})

    def execute(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> CommandResult:
        if self.config.shell:
            raise ValueError(
                "execute argv is unavailable when shell setup is configured"
            )
        clone = self.config.model_copy(update={"setup_argv": list(command)})
        original = self.config
        try:
            self.config = clone
            return self._run_setup(prepared)
        finally:
            self.config = original

    def capability_report(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": True,
            "provider_version": PROVIDER_VERSION,
            "shell_free": not self.config.shell,
            "limits": self.config.limits.model_dump(mode="json"),
        }


def provider_from_configuration(
    configuration: Mapping[str, Any],
    *,
    source_environment: Mapping[str, str] | None = None,
    cache_root: Path | None = None,
    selection: str | None = None,
    pluginized: bool = False,
) -> ExecutionEnvironmentProvider:
    config = ExecutionEnvironmentConfig.from_configuration(configuration, selection)
    if config.provider == "inherit":
        provider: ExecutionEnvironmentProvider = InheritProvider(
            config, source_environment=source_environment, cache_root=cache_root
        )
    elif config.provider == "setup-command":
        provider = SetupCommandProvider(
            config, source_environment=source_environment, cache_root=cache_root
        )
    elif config.provider == "container":
        from .container import ContainerProvider

        provider = ContainerProvider(
            config, source_environment=source_environment, cache_root=cache_root
        )
    else:
        from .devcontainer import DevcontainerProvider

        provider = DevcontainerProvider(
            config, source_environment=source_environment, cache_root=cache_root
        )
    if pluginized:
        from villani_ops.closed_loop.plugins import BuiltinExecutionProviderPlugin

        return BuiltinExecutionProviderPlugin(provider)
    return provider


def preflight_report(
    repository: Path,
    configuration: Mapping[str, Any],
    *,
    selection: str | None = None,
) -> dict[str, Any]:
    provider = provider_from_configuration(configuration, selection=selection)
    fingerprint: str | None = None
    fingerprint_error: str | None = None
    try:
        fingerprint = provider.fingerprint(repository)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        fingerprint_error = error.__class__.__name__
    return {
        "schema_version": "villani.execution_preflight.v1",
        "repository": inspect_repository(repository),
        "provider": provider.capability_report(),
        "execution_environment_fingerprint": fingerprint,
        "fingerprint_error": fingerprint_error,
        "inferred_setup_executed": False,
    }
