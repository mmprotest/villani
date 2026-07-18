"""Configuration, limits, and filesystem locations for the local daemon."""

from __future__ import annotations

import os
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__


def villani_home() -> Path:
    configured = os.environ.get("VILLANI_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".villani"


@dataclass(frozen=True, slots=True)
class AgentdPaths:
    root: Path

    @classmethod
    def default(cls) -> "AgentdPaths":
        return cls(villani_home() / "agentd")

    @property
    def endpoint(self) -> Path:
        return self.root / "endpoint.json"

    @property
    def token(self) -> Path:
        return self.root / "token"

    @property
    def database(self) -> Path:
        return self.root / "spool.sqlite3"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts" / "sha256"

    @property
    def log(self) -> Path:
        return self.root / "agentd.log"

    @property
    def sync_config(self) -> Path:
        return self.root / "sync.json"

    @property
    def credential_fallback(self) -> Path:
        return self.root / "installation-credential"

    @property
    def remote_work(self) -> Path:
        return self.root / "remote-work"


@dataclass(frozen=True, slots=True)
class Limits:
    stdout_bytes: int = 1_048_576
    stderr_bytes: int = 1_048_576
    event_body_bytes: int = 262_144
    artifact_file_bytes: int = 67_108_864
    total_run_artifact_bytes: int = 268_435_456
    spool_bytes: int = 536_870_912
    otlp_payload_bytes: int = 4_194_304

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value < 1:
                raise ValueError(f"{name} must be positive")

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 0
    limits: Limits = Limits()


@dataclass(frozen=True, slots=True)
class SyncConfig:
    endpoint: str
    installation_id: str
    batch_size: int = 250
    concurrency: int = 2
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 300.0
    poll_seconds: float = 2.0
    remote_execution_enabled: bool = False
    worker_id: str | None = None
    worker_version: str = __version__
    worker_poll_seconds: float = 2.0
    worker_heartbeat_seconds: float = 30.0
    lease_renewal_seconds: float = 15.0
    network_class: str = "local-only"
    data_residency_labels: tuple[str, ...] = ()
    reachable_models: tuple[str, ...] = ()
    reachable_runtimes: tuple[str, ...] = ()
    gpu_metadata: tuple[dict[str, object], ...] = ()
    checkout_secret_commands: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.batch_size < 1 or self.concurrency < 1:
            raise ValueError("sync batch size and concurrency must be positive")
        if self.remote_execution_enabled and (not self.worker_id or not self.data_residency_labels):
            raise ValueError(
                "remote execution requires worker_id and at least one data-residency label"
            )
        if (
            min(
                self.worker_poll_seconds,
                self.worker_heartbeat_seconds,
                self.lease_renewal_seconds,
            )
            <= 0
        ):
            raise ValueError("worker timing values must be positive")

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    @classmethod
    def load(cls, path: Path) -> "SyncConfig | None":
        if not path.is_file():
            return None
        return cls(**json.loads(path.read_text(encoding="utf-8")))
