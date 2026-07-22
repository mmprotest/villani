import { useEffect, useState } from "react";
import {
  EmptyState,
  ErrorState,
  KeyValueGrid,
  LoadingState,
  PageIntro,
  Panel,
  PanelHeader,
  ResultVerdict,
  StatusBadge,
} from "@villani/ui/react";
import {
  ConsoleClient,
  type ConsoleAgentSystemsDocument,
  type ConsoleModelInventory,
  type ConsoleRunOptions,
  type ConsoleSettingsDocument,
} from "./consoleApi";
import { useConsoleEnvironment } from "./consoleContext";
import { ProductShell } from "./ProductShell";

function useAsync<T>(load: (signal: AbortSignal) => Promise<T>, keys: unknown[]) {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal)
      .then(setValue)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
    // The caller supplies explicit stable dependency keys.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, keys);
  return { value, error };
}

export function AgentsPage({ client }: { client: ConsoleClient }) {
  const [document, setDocument] = useState<ConsoleAgentSystemsDocument | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void client
      .agentSystems(controller.signal)
      .then(setDocument)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
  }, [client]);

  const runAction = async (
    id: string,
    action: () => Promise<ConsoleAgentSystemsDocument>,
  ) => {
    setBusyAction(id);
    setError(null);
    try {
      setDocument(await action());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(null);
    }
  };

  const activeProfile = document?.profiles.find((profile) => profile.active);
  return (
    <ProductShell surface="agents" title="Agents">
      <div className="console-stack">
        <PageIntro title="Agents">
          Agent systems are complete CLI or API connections. Each role uses a fresh
          invocation with the policy shown here.
        </PageIntro>
        <Panel>
          <PanelHeader
            title="Installed agent systems"
            meta={
              document?.active_profile
                ? `Active profile: ${document.active_profile}`
                : "No active profile"
            }
            actions={
              <button
                type="button"
                disabled={busyAction !== null}
                onClick={() =>
                  void runAction("detect", () => client.detectAgentSystems())
                }
              >
                {busyAction === "detect" ? "Detecting…" : "Detect installed CLIs"}
              </button>
            }
          />
          <div className="v-panel__body">
            <p className="console-muted">
              Detection reads executable capability and authentication status only. It
              does not start login or change provider configuration.
            </p>
          </div>
        </Panel>
        {error && <ErrorState title="Agents are unavailable" detail={error} />}
        {!document && !error && <LoadingState title="Loading agents" />}
        {document && !document.agent_systems.length && (
          <EmptyState
            title="No agent system is configured"
            detail="Continue setup to add an API, Codex CLI, or Claude Code system."
          >
            <a href="/console/onboarding">Continue setup</a>
          </EmptyState>
        )}
        {document?.agent_systems.map((system) => (
          <Panel key={system.id} className="agent-system">
            <PanelHeader
              title={system.display_name}
              meta={system.model ?? system.id}
              actions={
                <StatusBadge
                  status={system.ready ? "selected" : "failed"}
                  label={system.status}
                />
              }
            />
            <div className="v-panel__body console-stack">
              <div className="agent-role-badges" aria-label="Supported roles">
                {system.role_badges.map((role) => (
                  <span className="agent-role-badge" key={role.id}>
                    {role.label}
                  </span>
                ))}
              </div>
              <KeyValueGrid
                items={[
                  ["Version", system.exact_version ?? "Managed by Villani"],
                  ["Authentication", system.authentication_status],
                  ["Model", system.model ?? "Not applicable"],
                  ["Conformance", system.conformance_status],
                  ["Instruction policy", system.instruction_policy],
                  ["Permission policy", system.permission_policy],
                  ["Last doctor", system.last_doctor_time ?? "Not recorded"],
                  ["Repair action", system.exact_next_action],
                ]}
              />
              {system.kind === "cli_agent" && (
                <div>
                  <button
                    type="button"
                    disabled={busyAction !== null}
                    onClick={() =>
                      void runAction(system.id, () =>
                        client.doctorAgentSystem(system.id),
                      )
                    }
                  >
                    {busyAction === system.id ? "Checking…" : "Run doctor"}
                  </button>
                </div>
              )}
              <details className="run-advanced">
                <summary>Evidence and advanced details</summary>
                <KeyValueGrid
                  items={[
                    ["Agent-system ID", system.id],
                    ["Driver", system.driver],
                    ["Executable", system.safe_display_path ?? "Not applicable"],
                    [
                      "Executable digest",
                      system.resolved_path_digest ?? "Not applicable",
                    ],
                    ["Evidence", system.evidence_path ?? "Managed by API doctor"],
                    ["Repository modified", system.repository_modified ? "Yes" : "No"],
                    [
                      "Affected roles",
                      system.affected_roles.length
                        ? system.affected_roles.join(", ")
                        : "None",
                    ],
                    ["Failure", system.what_failed ?? "None"],
                  ]}
                />
              </details>
            </div>
          </Panel>
        ))}
        {activeProfile && (
          <Panel>
            <PanelHeader
              title="Active execution profile"
              meta={activeProfile.profile_type.toUpperCase()}
              actions={
                <StatusBadge
                  status={activeProfile.runnable ? "selected" : "failed"}
                  label={activeProfile.status.toUpperCase()}
                />
              }
            />
            <div className="v-panel__body">
              <KeyValueGrid
                items={activeProfile.role_bindings.map((binding) => [
                  binding.label,
                  binding.agent_system_id ?? "Missing",
                ])}
              />
            </div>
          </Panel>
        )}
        <LegacyAgentsPage client={client} embedded />
      </div>
    </ProductShell>
  );
}

function formatObservedRate(value: number | null | undefined): string {
  return value == null ? "Unknown" : `${(value * 100).toFixed(1)}%`;
}

function formatAcceptedCost(
  distributions:
    Record<string, { median: number | null; known_count: number }> | undefined,
): string {
  if (!distributions) return "Unknown";
  const known = Object.entries(distributions)
    .filter(([, value]) => value.known_count > 0 && value.median != null)
    .map(([currency, value]) => `${currency} ${value.median!.toFixed(4)}`);
  return known.length ? known.join("; ") : "Unknown";
}

function formatDuration(value: number | null | undefined): string {
  return value == null ? "Unknown" : `${(value / 1000).toFixed(1)} s`;
}

/** Existing qualification and economics evidence retained behind an advanced disclosure. */
export function LegacyAgentsPage({
  client,
  embedded = false,
}: {
  client: ConsoleClient;
  embedded?: boolean;
}) {
  const { value, error } = useAsync<ConsoleModelInventory>(
    (signal) => client.models(signal),
    [client],
  );
  const models = value?.models.filter((model) => model.configured) ?? [];
  const systems = value?.agent_systems ?? [];
  const harnesses = value?.agent_harnesses ?? [];
  const configuredHarnessIds = new Set(
    systems.map((system) => system.harness.harness_id),
  );
  const content = (
    <div className="console-stack">
      {!embedded && (
        <PageIntro title="Agents">
          Complete configured agent systems, with connection and evidence details kept
          together.
        </PageIntro>
      )}
      <Panel className="agent-route-explanation">
        <div className="v-panel__body console-stack">
          <strong>
            {value?.economics?.default_explanation ??
              "Villani chose the route most likely to produce a proven change at the lowest total cost."}
          </strong>
          <span className="console-muted">
            {value?.economics?.unknown_accounting_note ??
              "Unknown route inputs remain Unknown and are not treated as zero."}
          </span>
        </div>
      </Panel>
      {error && <ErrorState title="Agents are unavailable" detail={error} />}
      {!value && !error && <LoadingState title="Loading agents" />}
      {value && !systems.length && !models.length && !harnesses.length && (
        <EmptyState
          title="No agent system is configured"
          detail="Continue setup to detect and verify a local agent connection."
        >
          <a href="/console/onboarding">Continue setup</a>
        </EmptyState>
      )}
      {harnesses
        .filter((harness) => !configuredHarnessIds.has(harness.harness_id))
        .map((harness) => (
          <Panel key={harness.harness_id} className="agent-system">
            <PanelHeader
              title={harness.display_name}
              meta="Detected harness"
              actions={
                <StatusBadge
                  status={harness.readiness.installed ? "selected" : "failed"}
                  label={harness.readiness.installed ? "INSTALLED" : "MISSING"}
                />
              }
            />
            <div className="v-panel__body console-stack">
              <KeyValueGrid
                items={[
                  ["Exact version", harness.readiness.exact_version ?? "Unknown"],
                  ["Authentication", harness.readiness.authentication_status],
                  ["Protocol", harness.readiness.protocol],
                  [
                    "Version range",
                    harness.readiness.supported_version_range ?? "Adapter-managed",
                  ],
                  ["Conformance", harness.readiness.conformance_status],
                  ["Qualification", harness.readiness.qualification_state],
                  ["Custom model", harness.readiness.custom_model_capability],
                  ["Custom provider", harness.readiness.custom_provider_capability],
                  ["Local model", harness.readiness.local_model_capability],
                  ["Repair action", harness.readiness.repair_action],
                ]}
              />
            </div>
          </Panel>
        ))}
      {systems.map((system) => {
        const supported = Object.values(system.capabilities).filter(
          (capability) => capability.state === "supported",
        ).length;
        const unknown = Object.values(system.capabilities).filter(
          (capability) => capability.state === "unknown",
        ).length;
        const qualification = system.repository_qualification;
        const repositoryEconomics = system.repository_economics;
        const economics = repositoryEconomics?.profile;
        const qualificationState = qualification?.state ?? "unknown";
        const ready = qualificationState === "qualified";
        const status =
          qualificationState === "qualified"
            ? "selected"
            : qualificationState === "unsupported"
              ? "failed"
              : "unknown";
        const verdict =
          qualificationState === "qualified"
            ? "completed"
            : qualificationState === "unsupported"
              ? "failed"
              : "unknown";
        return (
          <Panel key={system.system_id} className="agent-system">
            <PanelHeader
              title={`${system.harness.display_name} · ${system.model_provider.model_id}`}
              meta={system.route_name}
              actions={
                <StatusBadge status={status} label={qualificationState.toUpperCase()} />
              }
            />
            <div className="v-panel__body console-stack">
              <ResultVerdict
                status={verdict}
                label={
                  ready
                    ? "Eligible for automatic selection"
                    : qualificationState === "provisional"
                      ? "Eligible only as a provisional fallback"
                      : qualificationState === "experimental"
                        ? "Manual override required"
                        : "Not automatically selectable"
                }
                detail={`${system.harness.harness_id}@${system.harness.version} · ${system.model_provider.provider}/${system.model_provider.model_id}`}
              />
              <KeyValueGrid
                items={[
                  ["Repository qualification", qualificationState],
                  [
                    "Observed acceptance",
                    formatObservedRate(qualification?.statistics.acceptance_rate),
                  ],
                  [
                    "Median accepted-change cost",
                    formatAcceptedCost(
                      qualification?.statistics.accepted_change_cost_by_currency,
                    ),
                  ],
                  [
                    "Median duration",
                    formatDuration(
                      qualification?.statistics.duration_distribution.median,
                    ),
                  ],
                  [
                    "Eligible sample",
                    qualification?.statistics.sample_count ?? "Unknown",
                  ],
                  [
                    "Conservative acceptance",
                    qualification?.statistics.wilson_lower_bound == null
                      ? "Unknown"
                      : qualification.statistics.wilson_lower_bound.toFixed(3),
                  ],
                  [
                    "Latest execution cost (median)",
                    formatAcceptedCost(economics?.cost_distributions.execution_cost),
                  ],
                  [
                    "Latest verification cost (median)",
                    formatAcceptedCost(economics?.cost_distributions.verification_cost),
                  ],
                  [
                    "Latest review cost (median)",
                    formatAcceptedCost(economics?.cost_distributions.human_review_cost),
                  ],
                  [
                    "Latest retry/escalation cost (median)",
                    formatAcceptedCost(
                      economics?.cost_distributions.retry_escalation_cost,
                    ),
                  ],
                  [
                    "Economics evidence",
                    economics
                      ? `${economics.sample_count} eligible / ${repositoryEconomics?.matching_profile_count ?? 1} task profile(s)`
                      : "Unknown",
                  ],
                  [
                    "Last tested",
                    qualification?.statistics.last_evidence_at ?? "Unknown",
                  ],
                  [
                    "Evidence level",
                    qualification?.selected_level ?? "No matching evidence",
                  ],
                  [
                    "Caveat",
                    qualification?.caveat ??
                      "Repository context is unavailable; no qualification is implied.",
                  ],
                  ["Harness protocol", system.harness.protocol_version],
                  [
                    "Installed",
                    system.readiness
                      ? system.readiness.installed
                        ? "Yes"
                        : "No"
                      : "Unknown",
                  ],
                  [
                    "Authentication",
                    system.readiness?.authentication_status ?? "Unknown",
                  ],
                  [
                    "Conformance",
                    system.readiness?.conformance_status ?? "Not recorded",
                  ],
                  [
                    "Doctor",
                    qualification?.doctor_action ??
                      system.readiness?.repair_action ??
                      "Run agents doctor",
                  ],
                  ["Execution provider", system.execution.execution_provider],
                  ["Permission profile", system.execution.permission_profile],
                  ["Network policy", system.execution.network_policy],
                  ["Capabilities supported", supported],
                  ["Capabilities unknown", unknown],
                  ["Billing", system.billing.mode],
                ]}
              />
              <details className="run-advanced">
                <summary>View evidence</summary>
                <KeyValueGrid
                  items={[
                    [
                      "Evidence command",
                      qualification?.evidence_action ?? "Unavailable",
                    ],
                    [
                      "Wilson lower bound",
                      qualification?.statistics.wilson_lower_bound == null
                        ? "Unknown"
                        : qualification.statistics.wilson_lower_bound.toFixed(3),
                    ],
                    [
                      "Exclusions",
                      qualification
                        ? JSON.stringify(qualification.statistics.exclusions)
                        : "Unknown",
                    ],
                    [
                      "Economics task profile",
                      economics
                        ? `${economics.key.task_profile.category} / ${economics.key.task_profile.difficulty} / ${economics.key.task_profile.risk}`
                        : "Unknown",
                    ],
                    [
                      "Economics unknown inputs",
                      economics
                        ? JSON.stringify(economics.cost_unknown_counts)
                        : "Unknown",
                    ],
                    [
                      "Economics scope",
                      repositoryEconomics?.scope_note ??
                        "No matching repository economics evidence.",
                    ],
                    [
                      "Drift",
                      qualification?.statistics.drift_flags.length
                        ? qualification.statistics.drift_flags
                            .map((flag) => `${flag.code} (${flag.severity})`)
                            .join(", ")
                        : "None recorded",
                    ],
                  ]}
                />
              </details>
              <details className="run-advanced">
                <summary>Complete system identity</summary>
                <KeyValueGrid
                  items={[
                    ["System ID", system.system_id],
                    [
                      "Adapter",
                      `${system.harness.adapter_id}@${system.harness.adapter_version}`,
                    ],
                    [
                      "Endpoint",
                      system.model_provider.endpoint_identity ?? "Provider default",
                    ],
                    [
                      "Model revision",
                      system.model_provider.model_revision ?? "Unknown",
                    ],
                    [
                      "Serving engine",
                      system.model_provider.serving_engine ?? "Unknown",
                    ],
                    [
                      "Environment fingerprint",
                      system.execution.environment_fingerprint ??
                        "Recorded per attempt",
                    ],
                    ["Verification policy", system.route_profile.verification_policy],
                    ["Redaction", system.redaction_status],
                  ]}
                />
              </details>
            </div>
          </Panel>
        );
      })}
      {!systems.length &&
        models.map((model) => (
          <Panel key={model.id} className="agent-system">
            <PanelHeader
              title={model.display_name || model.model}
              meta={model.bootstrap_default ? "Default" : undefined}
              actions={
                <StatusBadge
                  status={model.available === false ? "failed" : "selected"}
                  label={model.available === true ? "AVAILABLE" : model.availability}
                />
              }
            />
            <div className="v-panel__body console-stack">
              <ResultVerdict
                status={model.available === false ? "failed" : "completed"}
                label={
                  model.available === false
                    ? "Connection needs attention"
                    : "Ready for tasks"
                }
                detail={`${model.provider} · ${model.model}`}
              />
              <KeyValueGrid
                items={[
                  ["Configured roles", model.configured_roles.join(", ") || "None"],
                  ["Tool support", model.tool_support],
                  ["Capability evidence", model.capability_status],
                  ["Observed tasks", model.observed_task_count],
                  [
                    "Observed success rate",
                    model.observed_success_rate === null
                      ? "Unknown"
                      : `${Math.round(model.observed_success_rate * 100)}%`,
                  ],
                  ["Last verified", model.last_tested_at ?? "Not recorded"],
                  ["Pricing", model.pricing_status],
                  ["Context window", model.context_window ?? "Unknown"],
                ]}
              />
              <details className="run-advanced">
                <summary>Advanced connection details</summary>
                <KeyValueGrid
                  items={[
                    ["Backend", model.backend_name ?? "Not recorded"],
                    ["Endpoint", model.endpoint ?? "Provider default"],
                    ["Capability policy", model.capability_policy_version],
                    ["Last diagnostic", model.last_test_diagnostic ?? "Not recorded"],
                  ]}
                />
              </details>
            </div>
          </Panel>
        ))}
    </div>
  );
  if (embedded) {
    return (
      <details className="run-advanced agent-system-legacy-evidence">
        <summary>Recorded qualification and economics evidence</summary>
        {content}
      </details>
    );
  }
  return (
    <ProductShell surface="agents" title="Agents">
      {content}
    </ProductShell>
  );
}

const advancedLinks = [
  [
    "Models",
    "/console/models",
    "Configure model connections and recorded capability evidence.",
  ],
  ["Policies", "/console/policies", "Inspect routing and delivery policy presets."],
  ["Replay", "/console/replay", "Inspect recorded evidence and event timelines."],
  ["Fleet", "/console/fleet", "Open the connected fleet view when available."],
  ["Tasks", "/console/tasks", "Inspect connected task records when available."],
  [
    "Costs",
    "/console/costs",
    "Review connected cost records, preserving unknown values.",
  ],
  ["Alerts", "/console/alerts", "Review connected alerts."],
  [
    "Audit",
    "/console/audit",
    "Open advanced structured interrogation and audit views.",
  ],
] as const;

export function SettingsPage({ client }: { client: ConsoleClient }) {
  const environment = useConsoleEnvironment();
  const [options, setOptions] = useState<ConsoleRunOptions | null>(null);
  const [settings, setSettings] = useState<ConsoleSettingsDocument | null>(null);
  const [agentSystems, setAgentSystems] = useState<ConsoleAgentSystemsDocument | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [profileBusy, setProfileBusy] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      client.settings(controller.signal),
      client.runOptions(controller.signal),
      client.agentSystems(controller.signal),
    ])
      .then(([settingsValue, optionsValue, agentSystemsValue]) => {
        setSettings(settingsValue);
        setOptions(optionsValue);
        setAgentSystems(agentSystemsValue);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
  }, [client]);
  const repositories = options?.repositories.filter((item) => item.valid) ?? [];
  const activeProfile = agentSystems?.profiles.find((profile) => profile.active);

  const applyAgentSystems = (value: ConsoleAgentSystemsDocument) => {
    setAgentSystems(value);
    setSettings((current) =>
      current
        ? {
            ...current,
            active_execution_profile: value.active_profile,
            execution_profiles: value.profiles,
            role_bindings:
              value.profiles.find((profile) => profile.active)?.role_bindings ?? [],
          }
        : current,
    );
  };

  const changeProfile = async (profileId: string) => {
    setProfileBusy(`profile:${profileId}`);
    setError(null);
    try {
      applyAgentSystems(await client.activateProfile(profileId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setProfileBusy(null);
    }
  };

  const changeRole = async (
    role: Parameters<ConsoleClient["setProfileRole"]>[1],
    systemId: string,
  ) => {
    if (!activeProfile) return;
    setProfileBusy(`role:${role}`);
    setError(null);
    try {
      applyAgentSystems(
        await client.setProfileRole(activeProfile.profile_id, role, systemId),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setProfileBusy(null);
    }
  };
  return (
    <ProductShell surface="settings" title="Settings">
      <div className="console-stack settings-page">
        <PageIntro title="Settings">
          Setup, service, repositories, privacy, delivery defaults, updates,
          diagnostics, and advanced tools.
        </PageIntro>
        {error && <ErrorState title="Some settings are unavailable" detail={error} />}
        {!settings && !error && <LoadingState title="Loading settings" />}
        {agentSystems && (
          <Panel id="execution-profile">
            <PanelHeader
              title="Execution profile"
              meta={
                activeProfile
                  ? `${activeProfile.profile_id} · ${activeProfile.profile_type}`
                  : "Not configured"
              }
              actions={
                activeProfile ? (
                  <StatusBadge
                    status={activeProfile.runnable ? "selected" : "failed"}
                    label={activeProfile.status.toUpperCase()}
                  />
                ) : undefined
              }
            />
            <div className="v-panel__body console-stack">
              <div className="profile-choice-list" aria-label="Execution profiles">
                {agentSystems.profiles.map((profile) => (
                  <button
                    type="button"
                    key={profile.profile_id}
                    aria-pressed={profile.active}
                    disabled={profileBusy !== null || profile.active}
                    onClick={() => void changeProfile(profile.profile_id)}
                  >
                    {profile.profile_id} ({profile.profile_type})
                  </button>
                ))}
              </div>
              {activeProfile?.role_bindings.map((binding) => {
                const compatible = agentSystems.agent_systems.filter((system) =>
                  system.configured_roles.includes(binding.role),
                );
                return (
                  <label className="profile-role-binding" key={binding.role}>
                    <span>{binding.label}</span>
                    <select
                      aria-label={`${binding.label} agent system`}
                      value={binding.agent_system_id ?? ""}
                      disabled={profileBusy !== null || !compatible.length}
                      onChange={(event) =>
                        void changeRole(binding.role, event.currentTarget.value)
                      }
                    >
                      {!binding.agent_system_id && <option value="">Missing</option>}
                      {compatible.map((system) => (
                        <option value={system.id} key={system.id}>
                          {system.display_name} — {system.model ?? system.id}
                        </option>
                      ))}
                    </select>
                  </label>
                );
              })}
              <p className="console-muted">
                Profiles are conveniences over these role bindings. Changing a role does
                not create a second controller or reuse another role’s session.
              </p>
            </div>
          </Panel>
        )}
        <div className="v-grid v-grid--2">
          <Panel id="setup">
            <PanelHeader
              title="Setup"
              actions={<a href="/console/onboarding">Review setup</a>}
            />
            <KeyValueGrid
              items={[
                [
                  "Configuration",
                  environment.setup.valid ? "Ready" : "Needs attention",
                ],
                ["Schema", environment.setup.schema_version ?? "Not configured"],
                [
                  "Agent systems",
                  environment.models.filter((model) => model.configured).length,
                ],
                ["Active policy", environment.active_policy ?? "Not recorded"],
              ]}
            />
          </Panel>
          <Panel id="service">
            <PanelHeader title="Service" />
            <KeyValueGrid
              items={[
                ["Status", environment.service.status],
                ["Started", environment.service.started_at ?? "Not recorded"],
                ["Log", environment.service.log_path ?? "Unavailable"],
                ["Mode", environment.workspace.connected ? "Connected" : "Local"],
              ]}
            />
          </Panel>
          <Panel id="repositories">
            <PanelHeader title="Repositories" />
            <div className="v-panel__body">
              {repositories.length ? (
                <ul className="console-list">
                  {repositories.map((repository) => (
                    <li key={repository.path}>
                      <strong>{repository.name}</strong>
                      <span className="v-muted"> {repository.path}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="v-muted">No valid repository is recorded.</p>
              )}
            </div>
          </Panel>
          <Panel id="privacy">
            <PanelHeader title="Privacy" />
            <KeyValueGrid
              items={[
                ["Data location", "Local-first"],
                ["Browser secrets", "Never exposed"],
                ["Run storage", environment.storage.runs || "Hosted workspace"],
                ["Synchronization pending", environment.synchronization.pending],
              ]}
            />
          </Panel>
          <Panel id="delivery">
            <PanelHeader title="Delivery defaults" />
            <KeyValueGrid
              items={[
                ["Outcome", options?.defaults.delivery_mode ?? "Unknown"],
                ["Approval", options?.defaults.approval_mode ?? "Unknown"],
                ["Policy preset", options?.defaults.policy_preset ?? "Unknown"],
                ["Maximum attempts", options?.defaults.max_attempts ?? "Unknown"],
                [
                  "Execution profile",
                  settings?.active_execution_profile ?? "Not configured",
                ],
              ]}
            />
          </Panel>
          <Panel id="updates">
            <PanelHeader title="Updates" />
            <KeyValueGrid
              items={[
                ["Installed version", environment.version],
                ["Update state", environment.update.status],
                ["Available version", environment.update.available_version ?? "None"],
                [
                  "Migration",
                  environment.setup.valid ? "No blocked migration" : "Needs attention",
                ],
                ["Configuration format", environment.setup.schema_version ?? "Unknown"],
                [
                  "Update channel",
                  environment.update.policy.channel === "pinned"
                    ? `pinned (${environment.update.policy.pinned_version})`
                    : environment.update.policy.channel,
                ],
              ]}
            />
            <div className="v-panel__body console-stack">
              <code>villani update status</code>
              <code>villani update check</code>
            </div>
          </Panel>
          <Panel id="entitlement">
            <PanelHeader title="Entitlement" />
            <KeyValueGrid
              items={[
                ["Plan", environment.entitlement.tier.toUpperCase()],
                ["Status", environment.entitlement.status],
                [
                  "Offline grace ends",
                  environment.entitlement.offline_grace_ends_at ?? "Not applicable",
                ],
                [
                  "Evidence remains readable",
                  environment.entitlement.evidence_readable ? "Yes" : "No",
                ],
              ]}
            />
            <div className="v-panel__body console-stack">
              {!!environment.entitlement.locked_features.length && (
                <p className="v-muted">
                  Pro options: {environment.entitlement.locked_features.join(", ")}
                </p>
              )}
              <code>villani license status</code>
            </div>
          </Panel>
          <Panel id="support">
            <PanelHeader title="Support bundle" />
            <div className="v-panel__body console-stack">
              <p>
                Preview a local, default-redacted manifest before explicitly creating an
                archive. Villani never uploads it automatically.
              </p>
              <code>villani support preview</code>
              <code>villani support create --confirm-manifest</code>
            </div>
          </Panel>
        </div>
        <Panel id="diagnostics">
          <PanelHeader title="Diagnostics" />
          <KeyValueGrid
            items={[
              ["Storage writable", environment.storage.writable ? "Yes" : "No"],
              ["Villani home", environment.storage.home || "Hosted workspace"],
              ["Synchronization failures", environment.synchronization.dead_letters],
              ["Last service error", environment.service.last_error ?? "None"],
              ["Exact repair command", "villani doctor"],
              ["Instruction policy", settings?.instruction_policy ?? "Not recorded"],
              ["Later correction import", "villani verification feedback-import"],
            ]}
          />
          {!!environment.setup.issues.length && (
            <div className="v-panel__body" role="alert">
              <ul className="console-list">
                {environment.setup.issues.map((issue) => (
                  <li key={issue}>{issue}</li>
                ))}
              </ul>
            </div>
          )}
        </Panel>
        <Panel id="advanced" data-testid="advanced-navigation">
          <PanelHeader title="Advanced" meta="Deep links remain available" />
          <div className="settings-advanced v-panel__body">
            {advancedLinks.map(([label, href, description]) => (
              <a className="settings-advanced__item" href={href} key={href}>
                <strong>{label}</strong>
                <span>{description}</span>
              </a>
            ))}
          </div>
        </Panel>
      </div>
    </ProductShell>
  );
}
