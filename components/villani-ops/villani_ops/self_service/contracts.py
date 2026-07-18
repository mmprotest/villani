"""Public PT10 contracts shared by CLI, service, Console, and release tooling."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UpdatePolicy(StrictModel):
    schema_version: Literal["villani.update_policy.v1"] = "villani.update_policy.v1"
    channel: Literal["stable", "beta", "pinned"] = "stable"
    pinned_version: str | None = Field(default=None, min_length=1)
    feed_url: str | None = None
    checks_enabled: bool = True

    @model_validator(mode="after")
    def validate_pin(self) -> "UpdatePolicy":
        if self.channel == "pinned" and not self.pinned_version:
            raise ValueError("the pinned update channel requires pinned_version")
        if self.channel != "pinned" and self.pinned_version is not None:
            raise ValueError("pinned_version is valid only for the pinned channel")
        return self


class UpdateArtifact(StrictModel):
    operating_system: Literal["windows", "macos", "linux"]
    architecture: str
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UpdateRelease(StrictModel):
    version: str
    channel: Literal["stable", "beta"]
    released_at: datetime
    release_notes: str
    minimum_config_version: int = Field(default=1, ge=1)
    maximum_config_version: int = Field(default=1, ge=1)
    artifacts: list[UpdateArtifact]


class UpdateFeed(StrictModel):
    schema_version: Literal["villani.update_feed.v1"] = "villani.update_feed.v1"
    releases: list[UpdateRelease]
    source_upload_required: Literal[False] = False


class MigrationPreview(StrictModel):
    configuration_version: int | None = None
    spool_version_before: int | None = None
    spool_version_after: int | None = None
    checked_run_bundles: int = Field(default=0, ge=0)
    protocol_majors: list[int] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    configuration_backup_required: bool = False
    destructive: Literal[False] = False
    repositories_modified: Literal[False] = False


class UpdateState(StrictModel):
    schema_version: Literal["villani.update_state.v1"] = "villani.update_state.v1"
    installed_version: str
    policy: UpdatePolicy
    status: Literal[
        "idle",
        "current",
        "available",
        "downloaded",
        "installing",
        "verified",
        "rolled_back",
        "failed",
    ] = "idle"
    available_version: str | None = None
    last_checked_at: datetime | None = None
    release_notes: str | None = None
    artifact_url: str | None = None
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    migration_preview: MigrationPreview | None = None
    active_installation: str | None = None
    previous_installation: str | None = None
    configuration_backup: str | None = None
    evidence_path: str | None = None
    error: str | None = None
    repositories_modified: Literal[False] = False
    source_uploaded: Literal[False] = False
    forced: Literal[False] = False


class EntitlementState(StrictModel):
    schema_version: Literal["villani.entitlement_state.v1"] = (
        "villani.entitlement_state.v1"
    )
    tier: Literal["free", "pro"]
    status: Literal["free", "active", "offline_grace", "expired", "invalid"]
    license_id: str | None = None
    issuer: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    offline_grace_ends_at: datetime | None = None
    effective_features: list[str]
    locked_features: list[str]
    core_safety_features: list[str]
    evidence_readable: Literal[True] = True
    accepted_runs_verifiable: Literal[True] = True
    licensing_network_used: Literal[False] = False
    source_data_shared: Literal[False] = False
    repair_action: str | None = None
    evidence_path: str


class SupportBundleItem(StrictModel):
    logical_name: str
    source_class: Literal[
        "versions", "schemas", "logs", "doctor", "failure_codes", "run_evidence"
    ]
    included: bool
    reason: str
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SupportBundleManifest(StrictModel):
    schema_version: Literal["villani.support_bundle_manifest.v1"] = (
        "villani.support_bundle_manifest.v1"
    )
    generated_at: datetime
    preview: bool
    explicit_run_ids: list[str]
    items: list[SupportBundleItem]
    redactions: list[str]
    archive_name: str | None = None
    archive_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    uploaded: Literal[False] = False
    repositories_modified: Literal[False] = False
    prompts_included: Literal[False] = False
    source_included: Literal[False] = False
    diffs_included: Literal[False] = False
    terminal_content_included: Literal[False] = False


class PackageManifestItem(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    executable: bool = False


class PackageManifest(StrictModel):
    schema_version: Literal["villani.package_manifest.v1"] = (
        "villani.package_manifest.v1"
    )
    version: str
    operating_system: Literal["windows", "macos", "linux"]
    architecture: str
    files: list[PackageManifestItem]
    sbom_path: str
    release_notes_path: str
    source_checkout_required: Literal[False] = False
    sibling_node_modules_required: Literal[False] = False


def public_state_models() -> tuple[type[BaseModel], ...]:
    """Return the public models for contract parity tests and local consumers."""

    return (
        UpdatePolicy,
        UpdateFeed,
        UpdateState,
        EntitlementState,
        SupportBundleManifest,
        PackageManifest,
    )


__all__ = [
    "EntitlementState",
    "MigrationPreview",
    "PackageManifest",
    "PackageManifestItem",
    "SupportBundleItem",
    "SupportBundleManifest",
    "UpdatePolicy",
    "UpdateArtifact",
    "UpdateFeed",
    "UpdateRelease",
    "UpdateState",
    "public_state_models",
]
