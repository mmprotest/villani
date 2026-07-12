"""Hardened Docker/Podman execution provider."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from .models import CommandResult, ExecutionEnvironmentConfig, PreparedEnvironment
from .security import check_command, inspect_command_domains, inspect_workspace
from .secrets import LocalSecretBroker, SecretLease


CONTAINER_PROVIDER_VERSION = "container-v1"


def _tree_size(root: Path, limit: int | None = None) -> int:
    total = 0
    for base, _directories, files in os.walk(root):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                continue
            if limit is not None and total > limit:
                return total
    return total


class ContainerProvider:
    name = "container"

    def __init__(
        self,
        config: ExecutionEnvironmentConfig,
        *,
        source_environment: Mapping[str, str] | None = None,
        cache_root: Path | None = None,
    ) -> None:
        from .providers import InheritProvider

        self.config = config
        self.source_environment = dict(
            os.environ if source_environment is None else source_environment
        )
        self.cache_root = cache_root
        self._inherit = InheritProvider(
            config, source_environment=self.source_environment, cache_root=cache_root
        )
        self._engine = self._select_engine()
        self._secret_broker = LocalSecretBroker(
            source_environment=self.source_environment
        )
        self._leases: dict[str, SecretLease] = {}

    def _select_engine(self) -> str | None:
        requested = self.config.container.engine
        if requested != "auto":
            return shutil.which(requested)
        for candidate in ("docker", "podman"):
            found = shutil.which(candidate)
            if found:
                return found
        return None

    @property
    def engine_name(self) -> str | None:
        return Path(self._engine).stem.lower() if self._engine else None

    def _probe(self) -> dict[str, Any]:
        if not self._engine:
            return {
                "provider": self.name,
                "available": False,
                "engine": self.config.container.engine,
                "detected_version": None,
                "missing_capabilities": ["container_cli"],
            }
        try:
            version = subprocess.run(
                [self._engine, "--version"],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            daemon = subprocess.run(
                [self._engine, "info"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "provider": self.name,
                "available": False,
                "engine": self.engine_name,
                "detected_version": None,
                "missing_capabilities": [f"probe_error:{error.__class__.__name__}"],
            }
        missing = []
        if version.returncode != 0:
            missing.append("container_version")
        if daemon.returncode != 0:
            missing.append("daemon_connectivity")
        image_identity = self._image_identity() if daemon.returncode == 0 else None
        if daemon.returncode == 0 and not image_identity:
            missing.append("container_image")
        return {
            "provider": self.name,
            "available": not missing,
            "engine": self.engine_name,
            "detected_version": (version.stdout or version.stderr).strip()[:200]
            or None,
            "missing_capabilities": missing,
            "provider_version": CONTAINER_PROVIDER_VERSION,
            "image_available": image_identity is not None,
            "limits": self.config.limits.model_dump(mode="json"),
            "network_mode": self.config.container.network.mode,
        }

    def capability_report(self) -> dict[str, Any]:
        return self._probe()

    def _image_identity(self) -> str | None:
        if not self._engine or not self.config.container.image:
            return None
        try:
            result = subprocess.run(
                [
                    self._engine,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    self.config.container.image,
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def fingerprint(self, repository: Path) -> str:
        payload = {
            "provider": self.name,
            "provider_version": CONTAINER_PROVIDER_VERSION,
            "engine": self.engine_name,
            "image": self.config.container.image,
            "image_identity": self._image_identity(),
            "config": self.config.model_dump(mode="json"),
            "repository": self._inherit.fingerprint(repository),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def prepare(self, *, repository: Path, worktree: Path) -> PreparedEnvironment:
        capability = self.capability_report()
        if not capability.get("available"):
            missing = ", ".join(capability.get("missing_capabilities") or [])
            raise RuntimeError(
                f"container provider unavailable ({self.engine_name or self.config.container.engine}): {missing}"
            )
        inherited = self._inherit.prepare(repository=repository, worktree=worktree)
        workspace = inspect_workspace(worktree, self.config.policy)
        fingerprint = self.fingerprint(repository)
        name_seed = hashlib.sha256(
            f"{worktree.resolve()}:{fingerprint}".encode()
        ).hexdigest()[:20]
        container_name = f"villani-{name_seed}"
        try:
            subprocess.run(
                [str(self._engine), "rm", "-f", container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        secret_policy_decisions = []
        for request in self.config.secrets:
            if request.source == "command":
                secret_policy_decisions.append(
                    check_command(request.command_argv or [], self.config.policy)
                )
        lease = self._secret_broker.acquire(self.config.secrets)
        environment = dict(inherited.environment)
        environment.update(lease.environment)
        network = self.config.container.network
        if network.mode == "allowlist":
            environment.update(
                {
                    "HTTP_PROXY": str(network.proxy_url),
                    "HTTPS_PROXY": str(network.proxy_url),
                    "ALL_PROXY": str(network.proxy_url),
                    "NO_PROXY": "",
                }
            )
        prepared = PreparedEnvironment(
            provider="container",
            provider_version=CONTAINER_PROVIDER_VERSION,
            repository_path=str(repository.resolve()),
            worktree_path=str(worktree.resolve()),
            environment=environment,
            removals=inherited.removals,
            fingerprint=fingerprint,
            cache_key=None,
            cache_hit=False,
            setup_result=None,
            inspection=inherited.inspection,
            runtime_state={
                "engine": self.engine_name,
                "container_name": container_name,
                "workspace_target": self.config.container.workspace_target,
                "workspace_inspection": workspace,
                "secret_lease": lease.durable_report(),
            },
            policy_decisions=[
                {
                    "policy": "network",
                    "decision": "allow",
                    "mode": network.mode,
                    "allowlist_count": len(network.allowed_domains)
                    + len(network.allowed_hosts),
                },
                {"policy": "workspace", "decision": "allow"},
                *secret_policy_decisions,
            ],
        )
        self._leases[container_name] = lease
        return prepared

    def command_environment(self, prepared: PreparedEnvironment) -> dict[str, str]:
        return dict(prepared.environment)

    def _network_args(self) -> list[str]:
        network = self.config.container.network
        if network.mode == "deny":
            return ["--network", "none"]
        if network.mode == "allowlist":
            return ["--network", str(network.proxy_network)]
        return []

    def command_prefix(self, prepared: PreparedEnvironment) -> list[str]:
        if not self._engine:
            raise RuntimeError("container CLI is unavailable")
        limits = self.config.limits
        settings = self.config.container
        name = str(prepared.runtime_state["container_name"])
        mount_source = str(Path(prepared.worktree_path).resolve())
        command = [
            self._engine,
            "run",
            "--name",
            name,
            "--label",
            "villani.execution.managed=true",
            "--label",
            f"villani.execution.fingerprint={prepared.fingerprint[:24]}",
            "--label",
            f"villani.execution.owner-pid={os.getpid()}",
            "--cpus",
            str(limits.cpu_count),
            "--memory",
            str(limits.memory_bytes),
            "--pids-limit",
            str(limits.process_count),
            "--workdir",
            settings.workspace_target,
            "--mount",
            f"type=bind,source={mount_source},target={settings.workspace_target}",
            *self._network_args(),
        ]
        if settings.read_only_root:
            command.append("--read-only")
        if settings.temporary_filesystem:
            command.extend(
                [
                    "--tmpfs",
                    f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes}",
                ]
            )
        if settings.user:
            command.extend(["--user", settings.user])
        if settings.storage_opt_size:
            command.extend(["--storage-opt", f"size={limits.disk_bytes}"])
        lease = self._leases.get(str(prepared.runtime_state["container_name"]))
        if lease is not None:
            for name in sorted(lease.environment):
                command.extend(["--env", name])
            for name in sorted(
                prepared.runtime_state.get("injected_environment_names") or []
            ):
                if name not in lease.environment:
                    command.extend(["--env", str(name)])
            for target_name, source in sorted(lease.files.items()):
                command.extend(
                    [
                        "--mount",
                        f"type=bind,source={source},target=/run/secrets/{target_name},readonly",
                    ]
                )
        if self.config.container.network.mode == "allowlist":
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
                command.extend(["--env", name])
        command.append(str(settings.image))
        return command

    def runner_controls(self, prepared: PreparedEnvironment) -> dict[str, Any]:
        return {
            "execution_prefix": self.command_prefix(prepared),
            "workspace_limit_bytes": self.config.limits.disk_bytes,
            "cleanup_command": [
                str(self._engine),
                "rm",
                "-f",
                str(prepared.runtime_state["container_name"]),
            ],
        }

    def validate_command(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> None:
        decision = check_command(command, self.config.policy)
        hosts = inspect_command_domains(
            command, self.config.container.network, self.config.policy
        )
        prepared.policy_decisions.append(decision)
        if hosts:
            prepared.policy_decisions.append(
                {
                    "policy": "domain",
                    "decision": "allow",
                    "host_count": len(hosts),
                }
            )

    def wrap_command(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> list[str]:
        self.validate_command(prepared, command)
        return [*self.command_prefix(prepared), *command]

    def _remove_container(self, prepared: PreparedEnvironment) -> None:
        if not self._engine:
            return
        name = prepared.runtime_state.get("container_name")
        if not name:
            return
        try:
            subprocess.run(
                [self._engine, "rm", "-f", str(name)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def execute(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> CommandResult:
        wrapped = self.wrap_command(prepared, command)
        limits = self.config.limits
        before = _tree_size(Path(prepared.worktree_path))
        started = time.monotonic()
        process = subprocess.Popen(
            wrapped,
            cwd=prepared.worktree_path,
            env=prepared.environment,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        totals = {"stdout": 0, "stderr": 0}

        def drain(stream: Any, key: str, limit: int) -> None:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                totals[key] += len(chunk)
                remaining = max(0, limit - len(captured[key]))
                if remaining:
                    captured[key].extend(chunk[:remaining])

        assert process.stdout is not None and process.stderr is not None
        threads = [
            threading.Thread(
                target=drain,
                args=(process.stdout, "stdout", limits.stdout_bytes),
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, "stderr", limits.stderr_bytes),
            ),
        ]
        for thread in threads:
            thread.start()
        timed_out = disk_exceeded = False
        while process.poll() is None:
            if time.monotonic() - started > limits.timeout_seconds:
                timed_out = True
            if (
                _tree_size(Path(prepared.worktree_path), before + limits.disk_bytes)
                - before
                > limits.disk_bytes
            ):
                disk_exceeded = True
            if timed_out or disk_exceeded:
                self._remove_container(prepared)
                try:
                    process.kill()
                except OSError:
                    pass
                break
            time.sleep(0.05)
        process.wait()
        for thread in threads:
            thread.join(timeout=10)
        stdout = bytes(captured["stdout"])
        stderr = bytes(captured["stderr"])
        exit_code = (
            124 if timed_out else 125 if disk_exceeded else int(process.returncode or 0)
        )
        classification: (
            Literal["timeout", "disk_limit", "process_limit", "memory_limit"] | None
        ) = (
            "timeout"
            if timed_out
            else "disk_limit"
            if disk_exceeded
            else "process_limit"
            if "resource temporarily unavailable"
            in stderr.decode(errors="ignore").lower()
            or "fork" in stderr.decode(errors="ignore").lower()
            else "memory_limit"
            if exit_code in {137, -9}
            else None
        )
        return CommandResult(
            exit_code=exit_code,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            stdout_bytes=totals["stdout"],
            stderr_bytes=totals["stderr"],
            stdout_truncated=totals["stdout"] > len(stdout),
            stderr_truncated=totals["stderr"] > len(stderr),
            timed_out=timed_out,
            disk_limit_exceeded=disk_exceeded,
            process_limit_exceeded=classification == "process_limit",
            failure_classification=classification,
        )

    def collect(self, prepared: PreparedEnvironment) -> dict[str, Any]:
        return {
            "worktree_size_bytes": _tree_size(Path(prepared.worktree_path)),
            "engine": self.engine_name,
            "container_name": prepared.runtime_state.get("container_name"),
        }

    def cleanup(self, prepared: PreparedEnvironment) -> None:
        self._remove_container(prepared)
        lease = self._leases.pop(
            str(prepared.runtime_state.get("container_name") or ""), None
        )
        if lease is not None:
            lease.cleanup()
            prepared.runtime_state["secret_lease"] = lease.durable_report()
