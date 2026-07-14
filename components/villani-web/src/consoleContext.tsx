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
