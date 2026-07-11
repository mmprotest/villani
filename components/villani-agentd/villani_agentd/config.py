"""Configuration, limits, and filesystem locations for the local daemon."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path


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
