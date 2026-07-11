"""Hardened Dev Container CLI provider using the documented up/exec boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import CommandResult, ExecutionEnvironmentConfig, PreparedEnvironment
from .security import check_command, inspect_command_domains, inspect_workspace
from .secrets import LocalSecretBroker, SecretLease


DEVCONTAINER_PROVIDER_VERSION = "devcontainer-v1"
_UNSUPPORTED_KEYS = {
    "dockerComposeFile": "multi-container Compose configurations are unsupported",
    "service": "Compose services are unsupported",
    "runServices": "Compose services are unsupported",
    "mounts": "repository-defined host mounts are unsupported",
    "workspaceMount": "custom workspace mounts are unsupported",
    "runArgs": "repository-defined runtime arguments are unsupported",
    "initializeCommand": "host lifecycle commands are unsupported",
    "onCreateCommand": "lifecycle commands are unsupported",
    "updateContentCommand": "lifecycle commands are unsupported",
    "postCreateCommand": "lifecycle commands are unsupported",
    "postStartCommand": "lifecycle commands are unsupported",
    "postAttachCommand": "lifecycle commands are unsupported",
    "features": "Dev Container Features are unsupported in hardened mode",
    "privileged": "privileged containers are unsupported",
    "capAdd": "additional Linux capabilities are unsupported",
    "securityOpt": "repository-defined security options are unsupported",
    "forwardPorts": "forwarded ports are unsupported",
    "appPort": "published application ports are unsupported",
}


def _jsonc(text: str) -> dict[str, Any]:
    output: list[str] = []
    index = 0
    quoted = False
    escaped = False
    while index < len(text):
        char = text[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            output.append(char)
            index += 1
            continue
        if text[index : index + 2] == "//":
            index = text.find("\n", index)
            if index < 0:
                break
            output.append("\n")
            index += 1
            continue
        if text[index : index + 2] == "/*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated block comment in devcontainer configuration")
            index = end + 2
            continue
        output.append(char)
        index += 1
    normalized = re.sub(r",\s*([}\]])", r"\1", "".join(output))
    value = json.loads(normalized)
    if not isinstance(value, dict):
        raise ValueError("devcontainer configuration must be a JSON object")
    return value


class DevcontainerProvider:
    name = "devcontainer"

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
        self._cli = shutil.which(config.devcontainer.cli)
        self._engine = self._select_engine()
        self._secret_broker = LocalSecretBroker(
            source_environment=self.source_environment
        )
        self._leases: dict[str, SecretLease] = {}
        self._temporary_roots: dict[str, Path] = {}

    def _select_engine(self) -> str | None:
        requested = self.config.devcontainer.engine
        if requested != "auto":
            return shutil.which(requested)
        return shutil.which("docker") or shutil.which("podman")

    def capability_report(self) -> dict[str, Any]:
        if not self._cli:
            return {
                "provider": self.name,
                "available": False,
                "detected_version": None,
                "missing_capabilities": ["devcontainer_cli"],
            }
        try:
            version = subprocess.run(
                [self._cli, "--version"],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "provider": self.name,
                "available": False,
                "detected_version": None,
                "missing_capabilities": [f"probe_error:{error.__class__.__name__}"],
            }
        missing = []
        if version.returncode != 0:
            missing.append("devcontainer_version")
        if not self._engine:
            missing.append("container_engine")
        elif not missing:
            try:
                engine = subprocess.run(
                    [self._engine, "info"],
                    stdin=subprocess.DEVNULL,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                if engine.returncode != 0:
                    missing.append("container_engine_connectivity")
            except (OSError, subprocess.TimeoutExpired):
                missing.append("container_engine_connectivity")
        return {
            "provider": self.name,
            "available": not missing,
            "detected_version": (version.stdout or version.stderr).strip()[:200] or None,
            "engine": Path(self._engine).stem if self._engine else None,
            "missing_capabilities": missing,
            "provider_version": DEVCONTAINER_PROVIDER_VERSION,
            "supported_commands": ["up", "exec"],
        }

    def _config_path(self, worktree: Path) -> Path:
        configured = self.config.devcontainer.config_path
        candidates = (
            [worktree / configured]
            if configured
            else [
                worktree / ".devcontainer" / "devcontainer.json",
                worktree / ".devcontainer.json",
            ]
        )
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(worktree.resolve())
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                return resolved
        raise RuntimeError("devcontainer configuration was not found inside the worktree")

    def _validated_config(self, worktree: Path) -> tuple[Path, dict[str, Any]]:
        path = self._config_path(worktree)
        value = _jsonc(path.read_text(encoding="utf-8"))
        denied = [f"{key}: {_UNSUPPORTED_KEYS[key]}" for key in sorted(value) if key in _UNSUPPORTED_KEYS]
        if denied:
            raise RuntimeError("unsupported devcontainer features: " + "; ".join(denied))
        if not value.get("image") and not isinstance(value.get("build"), dict):
            raise RuntimeError("devcontainer requires an image or single-container build")
        return path, value

    def _hardened_config(
        self, original_path: Path, value: dict[str, Any], lease: SecretLease
    ) -> tuple[Path, Path]:
        hardened = dict(value)
        limits = self.config.limits
        settings = self.config.container
        run_args = [
            "--cpus",
            str(limits.cpu_count),
            "--memory",
            str(limits.memory_bytes),
            "--pids-limit",
            str(limits.process_count),
        ]
        network = settings.network
        if network.mode == "deny":
            run_args.extend(["--network", "none"])
        elif network.mode == "allowlist":
            run_args.extend(["--network", str(network.proxy_network)])
        if settings.read_only_root:
            run_args.append("--read-only")
        if settings.temporary_filesystem:
            run_args.extend(
                [
                    "--tmpfs",
                    f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_bytes}",
                ]
            )
        hardened["runArgs"] = run_args
        hardened["shutdownAction"] = "stopContainer"
        if settings.user:
            hardened["containerUser"] = settings.user
            hardened["remoteUser"] = settings.user
        build = hardened.get("build")
        if isinstance(build, dict):
            build = dict(build)
            dockerfile = build.get("dockerfile")
            if dockerfile:
                build["dockerfile"] = str((original_path.parent / dockerfile).resolve())
            context = build.get("context")
            if context:
                build["context"] = str((original_path.parent / context).resolve())
            hardened["build"] = build
        # Values are deliberately absent. The selected exec receives them on stdin.
        hardened.pop("remoteEnv", None)
        hardened.pop("containerEnv", None)
        root = Path(tempfile.mkdtemp(prefix="villani-devcontainer-"))
        path = root / "devcontainer.json"
        path.write_text(json.dumps(hardened, sort_keys=True), encoding="utf-8")
        return root, path

    def fingerprint(self, repository: Path) -> str:
        config_path, _value = self._validated_config(repository)
        payload = {
            "provider": self.name,
            "provider_version": DEVCONTAINER_PROVIDER_VERSION,
            "cli": self.capability_report().get("detected_version"),
            "config_digest": hashlib.sha256(config_path.read_bytes()).hexdigest(),
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
            raise RuntimeError(f"devcontainer provider unavailable: {missing}")
        original, value = self._validated_config(worktree)
        if self.config.secrets:
            raise RuntimeError(
                "devcontainer secret injection is unsupported: the documented CLI remoteEnv boundary exposes values through configuration or argv; use the container provider"
            )
        workspace = inspect_workspace(worktree, self.config.policy)
        inherited = self._inherit.prepare(repository=repository, worktree=worktree)
        lease = self._secret_broker.acquire(self.config.secrets)
        temporary_root, hardened_config = self._hardened_config(original, value, lease)
        fingerprint = self.fingerprint(repository)
        environment = dict(inherited.environment)
        # Secret values live only in the devcontainer CLI process environment and stdin.
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
        instance = hashlib.sha256(
            f"{worktree.resolve()}:{fingerprint}".encode()
        ).hexdigest()[:24]
        label = f"villani.execution.instance={instance}"
        owner_label = f"villani.execution.owner-pid={os.getpid()}"
        if self._engine:
            try:
                listed = subprocess.run(
                    [self._engine, "ps", "-aq", "--filter", f"label={label}"],
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                for stale_container_id in listed.stdout.split():
                    subprocess.run(
                        [self._engine, "rm", "-f", stale_container_id],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )
            except (OSError, subprocess.TimeoutExpired):
                pass
        up = [
            str(self._cli),
            "up",
            "--workspace-folder",
            str(worktree.resolve()),
            "--config",
            str(hardened_config),
            "--id-label",
            label,
            "--id-label",
            owner_label,
        ]
        if self._engine:
            up.extend(["--docker-path", self._engine])
        try:
            result = subprocess.run(
                up,
                env=environment,
                text=True,
                capture_output=True,
                timeout=self.config.limits.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            lease.cleanup()
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise RuntimeError(
                f"devcontainer up failed: {error.__class__.__name__}"
            ) from None
        if result.returncode != 0:
            lease.cleanup()
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise RuntimeError(
                f"devcontainer up failed with exit code {result.returncode}"
            )
        document: dict[str, Any] = {}
        for line in reversed(result.stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                document = candidate
                break
        container_id = document.get("containerId")
        if not container_id:
            lease.cleanup()
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise RuntimeError("devcontainer up returned no containerId")
        prepared = PreparedEnvironment(
            provider="devcontainer",
            provider_version=DEVCONTAINER_PROVIDER_VERSION,
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
                "container_id": str(container_id),
                "config_path": str(hardened_config),
                "workspace_inspection": workspace,
                "secret_lease": lease.durable_report(),
            },
            policy_decisions=[
                {"policy": "network", "decision": "allow", "mode": network.mode},
                {"policy": "devcontainer_features", "decision": "allow"},
            ],
        )
        instance_key = str(hardened_config)
        self._leases[instance_key] = lease
        self._temporary_roots[instance_key] = temporary_root
        return prepared

    def command_environment(self, prepared: PreparedEnvironment) -> dict[str, str]:
        return dict(prepared.environment)

    def validate_command(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> None:
        prepared.policy_decisions.append(check_command(command, self.config.policy))
        inspect_command_domains(
            command, self.config.container.network, self.config.policy
        )

    def command_prefix(self, prepared: PreparedEnvironment) -> list[str]:
        if not self._cli:
            raise RuntimeError("devcontainer CLI is unavailable")
        prefix = [
            self._cli,
            "exec",
            "--workspace-folder",
            prepared.worktree_path,
            "--config",
            str(prepared.runtime_state["config_path"]),
        ]
        if self._engine:
            prefix.extend(["--docker-path", self._engine])
        return prefix

    def runner_controls(self, prepared: PreparedEnvironment) -> dict[str, Any]:
        return {
            "execution_prefix": self.command_prefix(prepared),
            "workspace_limit_bytes": self.config.limits.disk_bytes,
            "cleanup_command": [
                str(self._engine),
                "rm",
                "-f",
                str(prepared.runtime_state["container_id"]),
            ],
        }

    def wrap_command(
        self, prepared: PreparedEnvironment, command: Sequence[str]
    ) -> list[str]:
        self.validate_command(prepared, command)
        # Environment secrets are inherited by devcontainer exec. File targets are
        # refused because the CLI has no selected-exec-only file mount boundary.
        instance_key = str(prepared.runtime_state["config_path"])
        lease = self._leases.get(instance_key)
        if lease and lease.files:
            raise RuntimeError(
                "devcontainer does not support selected-process-only file secret mounts; use an environment target"
            )
        return [*self.command_prefix(prepared), *command]

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
                text=True,
                capture_output=True,
                timeout=self.config.limits.timeout_seconds,
                check=False,
            )
            timed_out = False
        except subprocess.TimeoutExpired:
            self.cleanup(prepared)
            result = subprocess.CompletedProcess(wrapped, 124, "", "")
            timed_out = True
        stdout = str(result.stdout or "")
        stderr = str(result.stderr or "")
        stdout_bytes = len(stdout.encode())
        stderr_bytes = len(stderr.encode())
        stdout = stdout.encode()[: self.config.limits.stdout_bytes].decode(
            errors="replace"
        )
        stderr = stderr.encode()[: self.config.limits.stderr_bytes].decode(
            errors="replace"
        )
        return CommandResult(
            exit_code=result.returncode,
            duration_ms=max(
                0, int((time.monotonic() - started) * 1000)
            ),
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_bytes > len(stdout.encode()),
            stderr_truncated=stderr_bytes > len(stderr.encode()),
            timed_out=timed_out,
            disk_limit_exceeded=False,
            process_limit_exceeded=False,
            failure_classification="timeout" if timed_out else None,
        )

    def collect(self, prepared: PreparedEnvironment) -> dict[str, Any]:
        return {
            "container_id": prepared.runtime_state.get("container_id"),
            "workspace_size_bytes": sum(
                path.stat().st_size
                for path in Path(prepared.worktree_path).rglob("*")
                if path.is_file()
            ),
        }

    def cleanup(self, prepared: PreparedEnvironment) -> None:
        container_id = prepared.runtime_state.get("container_id")
        if container_id and self._engine:
            try:
                subprocess.run(
                    [self._engine, "rm", "-f", str(container_id)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        instance_key = str(prepared.runtime_state.get("config_path") or "")
        lease = self._leases.pop(instance_key, None)
        if lease:
            lease.cleanup()
            prepared.runtime_state["secret_lease"] = lease.durable_report()
        root = self._temporary_roots.pop(instance_key, None)
        if root:
            shutil.rmtree(root, ignore_errors=True)
