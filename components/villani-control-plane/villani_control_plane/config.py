from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VILLANI_CONTROL_PLANE_", extra="ignore")

    database_url: str = "postgresql+psycopg://villani:villani@localhost:5432/villani"
    build_version: str = "dev"
    expected_migration: str = "head"
    dev_organization_id: str = "org_dev"
    dev_organization_name: str = "Development"
    dev_workspace_id: str = "workspace_dev"
    dev_workspace_name: str = "Development"
    dev_api_token: str | None = Field(default=None, min_length=24)
    dev_enrollment_token: str | None = Field(default=None, min_length=24)
    dev_user_email: str | None = None
    dev_user_password: str | None = Field(default=None, min_length=12)
    object_store_backend: str = "filesystem"
    object_store_path: Path = Path(".object-store")
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    artifact_upload_ttl_seconds: int = 900
    max_artifact_size_bytes: int = 268_435_456
    allowed_artifact_sensitivities: str = "public,internal,confidential,restricted"
    allowed_artifact_retention_classes: str = "ephemeral,run,project,compliance"
    max_installation_batch_events: int = 1000
    max_installation_events_per_minute: int = 10000
    subscription_queue_size: int = 256
    outbox_poll_seconds: float = 0.1
    outbox_lease_seconds: int = 30
    remote_task_lease_seconds: int = 60
    remote_task_cancellation_grace_seconds: int = 15
    remote_task_retry_delay_seconds: int = 5
    remote_task_claim_candidates: int = 100
    worker_heartbeat_stale_seconds: int = 120
    natural_language_query_enabled: bool = True
    natural_language_query_max_scan_rows: int = 100_000
    natural_language_query_max_result_rows: int = 200
    natural_language_query_default_days: int = 30
    session_ttl_seconds: int = 28_800
    authorization_cache_bound_seconds: int = 0
    authentication_rate_limit_per_minute: int = 30
    api_rate_limit_per_minute: int = 600
    secure_cookies: bool = True
    session_cookie_name: str = "villani_session"
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    deployment_mode: str = "local-only"
    deployment_region: str = "local"
    air_gapped: bool = False
    metadata_only: bool = False
    metrics_enabled: bool = True
    otlp_endpoint: str | None = None
    graceful_shutdown_seconds: int = 30
    development_encryption_key: str = "development-only-key-change-me"
    development_encryption_key_id: str = "dev-key-v1"

    @property
    def sensitivity_policy(self) -> frozenset[str]:
        return frozenset(value.strip() for value in self.allowed_artifact_sensitivities.split(","))

    @property
    def retention_policy(self) -> frozenset[str]:
        return frozenset(
            value.strip() for value in self.allowed_artifact_retention_classes.split(",")
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
