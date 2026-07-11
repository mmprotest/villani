"""Cross-platform daemon lifecycle and local diagnostics."""

from __future__ import annotations

import json
import getpass
import os
import secrets
import signal
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Any

from .client import ClientError, LocalClient, is_loopback_host
from .config import AgentdPaths, Limits, ServerConfig, SyncConfig
from .spool import SQLiteSpool
from .adapters import ADAPTERS


def write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        if os.name == "nt":
            identity = getpass.getuser()
            whoami = subprocess.run(
                ["whoami"], text=True, capture_output=True, check=False, timeout=5
            )
            if whoami.returncode == 0 and whoami.stdout.strip():
                identity = whoami.stdout.strip()
            secured = subprocess.run(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"{identity}:(F)",
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            if secured.returncode != 0:
                raise OSError("could not restrict the local token file ACL")
    except BaseException:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise


def run_foreground_service(paths: AgentdPaths, limits: Limits | None = None) -> None:
    """Run the daemon in the foreground for a user-level service manager."""

    from .server import serve

    selected_limits = limits or Limits()
    if paths.endpoint.exists():
        try:
            if LocalClient.from_files(paths).health().get("status") == "ok":
                raise RuntimeError("villani-agentd is already running")
        except ClientError:
            paths.endpoint.unlink(missing_ok=True)
    paths.root.mkdir(parents=True, exist_ok=True)
    if not paths.token.is_file():
        write_token(paths.token, secrets.token_urlsafe(48))
    token = paths.token.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("local daemon token is empty")
    stop = threading.Event()
    sync_config = SyncConfig.load(paths.sync_config)
    worker_threads: list[threading.Thread] = []
    if sync_config is not None:
        from .uploader import SynchronizationWorker

        worker = SynchronizationWorker(paths, sync_config, selected_limits)
        worker_threads.append(threading.Thread(target=worker.run, args=(stop,), daemon=True))
        if sync_config.remote_execution_enabled:
            from .remote_worker import RemoteExecutionWorker

            remote = RemoteExecutionWorker(paths, sync_config, selected_limits)
            worker_threads.append(threading.Thread(target=remote.run, args=(stop,), daemon=True))
        for worker_thread in worker_threads:
            worker_thread.start()
    try:
        serve(ServerConfig(host="127.0.0.1", port=0, limits=selected_limits), paths, token)
    finally:
        stop.set()
        for worker_thread in worker_threads:
            worker_thread.join(timeout=5)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            waited, _status = os.waitpid(pid, getattr(os, "WNOHANG", 1))
            if waited == pid:
                return False
        except ChildProcessError:
            pass
        try:
            stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
            if len(stat_fields) > 2 and stat_fields[2] == "Z":
                return False
        except OSError:
            pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_background(
    config: ServerConfig,
    paths: AgentdPaths,
    *,
    insecure_development: bool = False,
    timeout: float = 10,
) -> dict[str, Any]:
    if not is_loopback_host(config.host) and not insecure_development:
        raise ValueError("non-loopback binding requires --insecure-development")
    if paths.endpoint.exists():
        try:
            existing = LocalClient.from_files(paths)
            existing.status()
            raise RuntimeError("villani-agentd is already running")
        except ClientError:
            paths.endpoint.unlink(missing_ok=True)

    paths.root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(48)
    write_token(paths.token, token)
    command = [
        sys.executable,
        "-m",
        "villani_agentd.daemon_main",
        "--root",
        str(paths.root),
        "--host",
        config.host,
        "--port",
        str(config.port),
    ]
    if insecure_development:
        command.append("--insecure-development")
    for option, value in config.limits.as_dict().items():
        command.extend([f"--{option.replace('_', '-')}", str(value)])
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    log_handle = paths.log.open("ab")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    finally:
        log_handle.close()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            paths.token.unlink(missing_ok=True)
            raise RuntimeError(f"villani-agentd exited during startup with {process.returncode}")
        if paths.endpoint.exists():
            try:
                client = LocalClient.from_files(paths)
                client.health()
                return json.loads(paths.endpoint.read_text(encoding="utf-8"))
            except (ClientError, OSError, json.JSONDecodeError):
                pass
        time.sleep(0.05)
    try:
        os.kill(process.pid, signal.SIGTERM)
    except OSError:
        pass
    paths.token.unlink(missing_ok=True)
    raise RuntimeError("villani-agentd did not become ready before timeout")


def read_endpoint(paths: AgentdPaths) -> dict[str, Any]:
    try:
        value = json.loads(paths.endpoint.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("villani-agentd is not running") from error
    if not isinstance(value, dict):
        raise RuntimeError("agentd endpoint file is invalid")
    return value


def stop_background(paths: AgentdPaths, timeout: float = 10) -> bool:
    endpoint = read_endpoint(paths)
    pid = int(endpoint.get("pid", 0))
    try:
        LocalClient.from_files(paths).status()
    except ClientError as error:
        if not _pid_exists(pid):
            paths.endpoint.unlink(missing_ok=True)
            paths.token.unlink(missing_ok=True)
            return False
        raise RuntimeError("refusing to stop an unverified endpoint process") from error
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        raise RuntimeError(f"cannot stop villani-agentd process {pid}: {error}") from error
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_exists(pid):
        time.sleep(0.05)
    if _pid_exists(pid):
        raise RuntimeError(f"villani-agentd process {pid} did not stop")
    paths.endpoint.unlink(missing_ok=True)
    paths.token.unlink(missing_ok=True)
    return True


def doctor(paths: AgentdPaths) -> tuple[bool, dict[str, Any]]:
    paths.root.mkdir(parents=True, exist_ok=True)
    spool = SQLiteSpool(paths, Limits())
    endpoint: dict[str, Any] | None = None
    endpoint_loopback = True
    running = False
    if paths.endpoint.exists():
        try:
            endpoint = read_endpoint(paths)
            from urllib.parse import urlparse

            endpoint_loopback = is_loopback_host(
                urlparse(str(endpoint.get("endpoint"))).hostname or ""
            )
            running = LocalClient.from_files(paths).health().get("status") == "ok"
        except (ClientError, RuntimeError):
            running = False
    token_permissions = None
    if paths.token.exists() and os.name != "nt":
        token_permissions = oct(paths.token.stat().st_mode & 0o777)
    sync_config = SyncConfig.load(paths.sync_config)
    report = {
        "database_integrity": spool.integrity_check(),
        "endpoint_loopback": endpoint_loopback,
        "running": running,
        "token_permissions": token_permissions,
        "upload_mode": "synchronized" if sync_config else "offline",
        "remote_execution": {
            "enabled": bool(sync_config and sync_config.remote_execution_enabled),
            "worker_id": sync_config.worker_id if sync_config else None,
        },
        "adapters": [
            adapter.detect().as_dict() for name, adapter in ADAPTERS.items() if name != "generic"
        ],
    }
    healthy = report["database_integrity"] == "ok" and endpoint_loopback
    return healthy, report
