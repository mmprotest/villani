import { createContext, useContext, type ReactNode } from "react";
import type { ConsoleBootstrap } from "@villani/run-model";

const defaultBootstrap: ConsoleBootstrap = {
  schema_version: "villani.console.bootstrap.v1",
  mode: "local",
  data_source: "local-service",
  version: "unknown",
  workspace: { connected: false, id: null, endpoint: null },
  service: { status: "loading", started_at: null, log_path: null, last_error: null },
  setup: { configured: false, valid: false, schema_version: null, issues: [] },
  synchronization: { pending: 0, dead_letters: 0 },
  storage: { home: "", runs: "", spool: "", writable: false },
  models: [],
  active_policy: null,
  entitlement: {
    schema_version: "villani.entitlement_state.v1",
    tier: "free",
    status: "free",
    license_id: null,
    issuer: null,
    issued_at: null,
    expires_at: null,
    offline_grace_ends_at: null,
    effective_features: [],
    locked_features: [],
    core_safety_features: [],
    evidence_readable: true,
    accepted_runs_verifiable: true,
    licensing_network_used: false,
    source_data_shared: false,
    repair_action: null,
    evidence_path: "",
  },
  update: {
    schema_version: "villani.update_state.v1",
    installed_version: "unknown",
    policy: {
      schema_version: "villani.update_policy.v1",
      channel: "stable",
      pinned_version: null,
      feed_url: null,
      checks_enabled: true,
    },
    status: "idle",
    available_version: null,
    last_checked_at: null,
    release_notes: null,
    artifact_url: null,
    artifact_sha256: null,
    migration_preview: null,
    active_installation: null,
    previous_installation: null,
    configuration_backup: null,
    evidence_path: null,
    error: null,
    repositories_modified: false,
    source_uploaded: false,
    forced: false,
  },
};

const ConsoleContext = createContext<ConsoleBootstrap>(defaultBootstrap);

export function ConsoleProvider({
  value,
  children,
}: {
  value: ConsoleBootstrap;
  children: ReactNode;
}) {
  return <ConsoleContext.Provider value={value}>{children}</ConsoleContext.Provider>;
}

export function useConsoleEnvironment() {
  return useContext(ConsoleContext);
}

export { defaultBootstrap };
