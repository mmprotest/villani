export const AGENT_SYSTEM_SCHEMA_VERSION = "villani.agent_system.v1";
export const AGENT_SYSTEM_CONFIG_SCHEMA_VERSION = "villani.agent_system_config.v1";
export const ROLE_BINDINGS_SCHEMA_VERSION = "villani.role_bindings.v1";
export const AGENT_INVOCATION_IDENTITY_SCHEMA_VERSION = "villani.agent_invocation_identity.v1";
export const CLI_INVOCATION_SCHEMA_VERSION = "villani.cli_invocation.v1";
export const CLI_PROCESS_RESULT_SCHEMA_VERSION = "villani.cli_process_result.v1";
export const CLI_OUTPUT_TAIL_SCHEMA_VERSION = "villani.cli_output_tail.v1";
export const CODEX_CODER_RESULT_SCHEMA_VERSION = "villani.codex_coder_result.v1";
export const CLAUDE_CODER_RESULT_SCHEMA_VERSION = "villani.claude_coder_result.v1";
export const HARNESS_RESULT_SCHEMA_VERSION = "villani.harness_result.v1";
export const HARNESS_CONFORMANCE_SCHEMA_VERSION = "villani.harness_conformance_report.v1";
export const HARNESS_DISCOVERY_SCHEMA_VERSION = "villani.harness_discovery.v1";
export const REQUIRED_HARNESS_CONFORMANCE_CHECKS = [
    "manifest",
    "protocol_negotiation",
    "version_capture",
    "worktree_enforcement",
    "path_safety",
    "event_ordering",
    "cancellation",
    "timeout",
    "malformed_output",
    "oversized_output",
    "process_crash",
    "missing_executable",
    "permissions",
    "artifacts",
    "patch_correctness",
    "cleanup",
    "secret_redaction",
    "unknown_cost",
    "cross_platform_paths",
    "successful_patch",
    "no_patch",
    "command_recovery",
    "permission_request",
    "rate_limit_retry",
    "unsupported_version",
    "schema_change",
    "missing_final_result",
    "partial_patch_on_crash",
    "known_cost",
    "non_ascii_spaced_paths",
    "large_output",
    "outside_isolation_mutation",
];
