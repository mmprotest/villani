/** PT10 self-service read models. Public safety and evidence fields are explicit. */
export type UpdateChannel = "stable" | "beta" | "pinned";
export interface UpdatePolicy {
    schema_version: "villani.update_policy.v1";
    channel: UpdateChannel;
    pinned_version: string | null;
    feed_url: string | null;
    checks_enabled: boolean;
}
export interface UpdateArtifact {
    operating_system: "windows" | "macos" | "linux";
    architecture: string;
    url: string;
    sha256: string;
}
export interface UpdateRelease {
    version: string;
    channel: "stable" | "beta";
    released_at: string;
    release_notes: string;
    minimum_config_version: number;
    maximum_config_version: number;
    artifacts: UpdateArtifact[];
}
export interface UpdateFeed {
    schema_version: "villani.update_feed.v1";
    releases: UpdateRelease[];
    source_upload_required: false;
}
export interface MigrationPreview {
    configuration_version: number | null;
    spool_version_before: number | null;
    spool_version_after: number | null;
    checked_run_bundles: number;
    protocol_majors: number[];
    actions: string[];
    configuration_backup_required: boolean;
    destructive: false;
    repositories_modified: false;
}
export interface UpdateState {
    schema_version: "villani.update_state.v1";
    installed_version: string;
    policy: UpdatePolicy;
    status: "idle" | "current" | "available" | "downloaded" | "installing" | "verified" | "rolled_back" | "failed";
    available_version: string | null;
    last_checked_at: string | null;
    release_notes: string | null;
    artifact_url: string | null;
    artifact_sha256: string | null;
    migration_preview: MigrationPreview | null;
    active_installation: string | null;
    previous_installation: string | null;
    configuration_backup: string | null;
    evidence_path: string | null;
    error: string | null;
    repositories_modified: false;
    source_uploaded: false;
    forced: false;
}
export interface EntitlementState {
    schema_version: "villani.entitlement_state.v1";
    tier: "free" | "pro";
    status: "free" | "active" | "offline_grace" | "expired" | "invalid";
    license_id: string | null;
    issuer: string | null;
    issued_at: string | null;
    expires_at: string | null;
    offline_grace_ends_at: string | null;
    effective_features: string[];
    locked_features: string[];
    core_safety_features: string[];
    evidence_readable: true;
    accepted_runs_verifiable: true;
    licensing_network_used: false;
    source_data_shared: false;
    repair_action: string | null;
    evidence_path: string;
}
export interface SupportBundleItem {
    logical_name: string;
    source_class: "versions" | "schemas" | "logs" | "doctor" | "failure_codes" | "run_evidence";
    included: boolean;
    reason: string;
    size_bytes: number | null;
    sha256: string | null;
}
export interface SupportBundleManifest {
    schema_version: "villani.support_bundle_manifest.v1";
    generated_at: string;
    preview: boolean;
    explicit_run_ids: string[];
    items: SupportBundleItem[];
    redactions: string[];
    archive_name: string | null;
    archive_sha256: string | null;
    uploaded: false;
    repositories_modified: false;
    prompts_included: false;
    source_included: false;
    diffs_included: false;
    terminal_content_included: false;
}
export interface PackageManifestItem {
    path: string;
    sha256: string;
    size_bytes: number;
    executable: boolean;
}
export interface PackageManifest {
    schema_version: "villani.package_manifest.v1";
    version: string;
    operating_system: "windows" | "macos" | "linux";
    architecture: string;
    files: PackageManifestItem[];
    sbom_path: string;
    release_notes_path: string;
    source_checkout_required: false;
    sibling_node_modules_required: false;
}
export interface DoctorCheck {
    identifier: string;
    status: "pass" | "warn" | "fail";
    message: string;
    recovery_action: string | null;
    details: Record<string, unknown>;
    repositories_modified: false;
    evidence_path: string;
}
export interface DoctorReport {
    schema_version: "villani.doctor.v1";
    generated_at: string;
    healthy: boolean;
    ok: boolean;
    summary: {
        passed: number;
        warnings: number;
        failed: number;
    };
    checks: DoctorCheck[];
    repositories_modified: false;
    evidence_path: string;
    [key: string]: unknown;
}
