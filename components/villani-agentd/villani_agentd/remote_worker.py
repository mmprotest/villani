from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml
from villani_ops.execution_environment.models import SecretRequest
from villani_ops.execution_environment.providers import provider_from_configuration
from villani_ops.execution_environment.secrets import LocalSecretBroker, SecretLease

from .adapters import ADAPTERS
from .config import AgentdPaths, Limits, SyncConfig, villani_home
from .credentials import InstallationCredentialStore
from .process import is_windows, terminate_process_tree
from .uploader import ControlPlaneClient, RemoteError


logger = logging.getLogger(__name__)


def _memory_bytes() -> int:
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
        return 0
    try:
        sysconf = getattr(os, "sysconf", None)
        if sysconf is None:
            return 0
        return int(sysconf("SC_PAGE_SIZE") * sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 0


def discover_worker_capabilities(config: SyncConfig) -> dict[str, Any]:
    config_path = villani_home() / "config.yaml"
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        loaded = None
    configuration = loaded if isinstance(loaded, dict) else {}
    configured = configuration.get("execution_environments")
    selections = list(configured) if isinstance(configured, dict) else [None]
    providers: list[str] = []
    for selection in selections:
        try:
            provider = provider_from_configuration(configuration, selection=selection)
            report = provider.capability_report()
            if report.get("available"):
                providers.append(str(report["provider"]))
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
            continue
    adapters = [
        name
        for name, adapter in ADAPTERS.items()
        if name != "generic" and adapter.detect().available
    ]
    return {
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "execution_providers": sorted(set(providers)),
        "agent_adapters": sorted(adapters),
        "reachable_models": sorted(set(config.reachable_models)),
        "reachable_runtimes": sorted(set(config.reachable_runtimes)),
        "cpu_count": float(os.cpu_count() or 1),
        "memory_bytes": max(1, _memory_bytes()),
        "gpus": list(config.gpu_metadata),
        "concurrency": config.concurrency,
        "network_class": config.network_class,
        "data_residency_labels": sorted(set(config.data_residency_labels)),
        "version": config.worker_version,
    }


class CheckoutCredentialBroker:
    """Resolve a server-provided opaque reference through a local short-lived command."""

    def __init__(self, commands: dict[str, list[str]]) -> None:
        self.commands = commands

    def acquire(self, reference: dict[str, Any], repository_id: str) -> SecretLease:
        if reference.get("scope_repository_id") != repository_id:
            raise RuntimeError("checkout credential repository scope mismatch")
        ttl = int(reference.get("expires_in_seconds", 0))
        if not 1 <= ttl <= 900:
            raise RuntimeError("checkout credential lifetime is outside the allowed range")
        name = str(reference.get("broker_reference") or "")
        command = self.commands.get(name)
        if not command:
            raise RuntimeError("checkout credential broker reference is not configured locally")
        return LocalSecretBroker().acquire(
            [
                SecretRequest(
                    name="VILLANI_CHECKOUT_TOKEN",
                    source="command",
                    command_argv=command,
                    target="environment",
                    required=True,
                )
            ]
        )


@dataclass(frozen=True, slots=True)
class RemoteExecutionResult:
    status: str
    materialized: bool
    finalized: bool
    evidence: dict[str, Any]


class RemoteExecutionWorker:
    def __init__(
        self,
        paths: AgentdPaths,
        config: SyncConfig,
        limits: Limits,
        *,
        client: ControlPlaneClient | None = None,
        capabilities: dict[str, Any] | None = None,
        executor: Callable[[dict[str, Any]], RemoteExecutionResult] | None = None,
    ) -> None:
        if not config.remote_execution_enabled or not config.worker_id:
            raise ValueError("remote execution is not enabled")
        self.paths = paths
        self.config = config
        self.limits = limits
        if client is None:
            credential = InstallationCredentialStore(paths).get(config.installation_id)
            client = ControlPlaneClient(config.endpoint, credential)
        self.client = client
        self.capabilities = capabilities or discover_worker_capabilities(config)
        self.credential_broker = CheckoutCredentialBroker(config.checkout_secret_commands)
        self.executor = executor or self._execute_task

    def heartbeat(self) -> dict[str, Any]:
        return self.client.request(
            "PUT",
            f"/v1/workers/{self.config.worker_id}/heartbeat",
            {"capabilities": self.capabilities, "status": "online"},
        )

    def run_once(self) -> bool:
        self.heartbeat()
        response = self.client.request(
            "POST", f"/v1/workers/{self.config.worker_id}/tasks/claim", {}
        )
        task = response.get("task")
        if not isinstance(task, dict):
            return False
        try:
            result = self.executor(task)
        except Exception as error:
            result = RemoteExecutionResult(
                status="failed",
                materialized=False,
                finalized=False,
                evidence={"failure_class": type(error).__name__},
            )
        completion_key = (
            f"remote-complete:{task['finalization_idempotency_key']}"
            if result.status == "succeeded"
            else f"remote-complete:{task['lease_id']}"
        )
        self.client.request(
            "POST",
            f"/v1/tasks/{task['task_id']}/leases/{task['lease_id']}/complete",
            {
                "idempotency_key": completion_key,
                "finalization_idempotency_key": task["finalization_idempotency_key"],
                "status": result.status,
                "materialized": result.materialized,
                "finalized": result.finalized,
                "result": result.evidence,
            },
        )
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            delay = self.config.worker_poll_seconds
            try:
                worked = self.run_once()
                if worked:
                    delay = 0
            except RemoteError as error:
                delay = error.retry_after or self.config.worker_poll_seconds
            except Exception:
                logger.exception("remote worker iteration failed; retrying")
            stop.wait(delay)

    def _workspace(self, task: dict[str, Any]) -> Path:
        digest = hashlib.sha256(task["finalization_idempotency_key"].encode()).hexdigest()
        root = self.paths.remote_work.resolve()
        workspace = (root / digest).resolve()
        if workspace.parent != root:
            raise RuntimeError("remote workspace escaped the managed root")
        return workspace

    def _checkout(self, task: dict[str, Any], workspace: Path) -> None:
        repository = task["repository"]
        checkout_url = repository.get("checkout_url")
        if not checkout_url:
            raise RuntimeError("remote task repository has no checkout URL")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            shutil.rmtree(workspace)
        environment = dict(os.environ)
        lease: SecretLease | None = None
        try:
            secret_reference = repository.get("checkout_secret")
            if secret_reference:
                lease = self.credential_broker.acquire(
                    secret_reference, str(repository["repository_id"])
                )
                token = lease.environment["VILLANI_CHECKOUT_TOKEN"]
                environment.update(
                    {
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "http.extraHeader",
                        "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
                    }
                )
            clone = subprocess.run(
                ["git", "clone", "--no-checkout", "--", str(checkout_url), str(workspace)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=300,
                check=False,
            )
            if clone.returncode != 0:
                raise RuntimeError("repository checkout failed")
            checkout = subprocess.run(
                ["git", "-C", str(workspace), "checkout", "--detach", str(repository["revision"])],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=120,
                check=False,
            )
            if checkout.returncode != 0:
                raise RuntimeError("repository revision checkout failed")
        finally:
            if lease is not None:
                lease.cleanup()

    def _renew(self, task: dict[str, Any]) -> dict[str, Any]:
        return self.client.request(
            "POST",
            f"/v1/tasks/{task['task_id']}/leases/{task['lease_id']}/renew",
            {},
        )

    def _run_child(
        self, command: list[str], workspace: Path, task: dict[str, Any]
    ) -> RemoteExecutionResult:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=os.environ.copy(),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=not is_windows(),
        )
        totals = {"stdout": 0, "stderr": 0}

        def drain(stream, name: str) -> None:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                totals[name] += len(chunk)

        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=drain, args=(process.stdout, "stdout")),
            threading.Thread(target=drain, args=(process.stderr, "stderr")),
        ]
        for reader in readers:
            reader.start()
        cancelled = False
        next_renewal = time.monotonic() + self.config.lease_renewal_seconds
        try:
            while process.poll() is None:
                if time.monotonic() >= next_renewal:
                    renewal = self._renew(task)
                    next_renewal = time.monotonic() + self.config.lease_renewal_seconds
                    if renewal.get("cancellation_requested"):
                        cancelled = True
                        terminate_process_tree(process)
                        break
                time.sleep(min(0.1, self.config.lease_renewal_seconds / 2))
        except BaseException:
            # Continuing after lease-renewal failure could overlap a reassigned task.
            terminate_process_tree(process)
            raise
        finally:
            process.wait()
            for reader in readers:
                reader.join(timeout=10)
        if cancelled:
            return RemoteExecutionResult(
                status="cancelled",
                materialized=False,
                finalized=False,
                evidence={
                    "child_terminated": True,
                    "exit_code": process.returncode,
                    "stdout_bytes": totals["stdout"],
                    "stderr_bytes": totals["stderr"],
                },
            )
        succeeded = process.returncode == 0
        return RemoteExecutionResult(
            status="succeeded" if succeeded else "failed",
            materialized=succeeded,
            finalized=succeeded,
            evidence={
                "child_terminated": False,
                "exit_code": process.returncode,
                "stdout_bytes": totals["stdout"],
                "stderr_bytes": totals["stderr"],
            },
        )

    def _execute_task(self, task: dict[str, Any]) -> RemoteExecutionResult:
        workspace = self._workspace(task)
        marker = workspace / ".villani-remote-completion.json"
        if marker.is_file():
            value = json.loads(marker.read_text(encoding="utf-8"))
            return RemoteExecutionResult(**value)
        self._checkout(task, workspace)
        task_input = task.get("task_input") or {}
        goal = task_input.get("goal")
        if not isinstance(goal, str) or not goal:
            raise RuntimeError("remote task input requires a non-empty goal")
        command = [
            sys.executable,
            "-m",
            "villani_ops.cli.unified",
            "run",
            goal,
            "--repo",
            str(workspace),
        ]
        criteria = task_input.get("success_criteria")
        if isinstance(criteria, str) and criteria:
            command.extend(["--success-criteria", criteria])
        result = self._run_child(command, workspace, task)
        if result.status == "succeeded":
            marker.write_text(
                json.dumps(
                    {
                        "status": result.status,
                        "materialized": result.materialized,
                        "finalized": result.finalized,
                        "evidence": result.evidence,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        return result
