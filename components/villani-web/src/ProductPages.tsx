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
  type ConsoleModelInventory,
  type ConsoleRunOptions,
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
  const { value, error } = useAsync<ConsoleModelInventory>(
    (signal) => client.models(signal),
    [client],
  );
  const models = value?.models.filter((model) => model.configured) ?? [];
  const systems = value?.agent_systems ?? [];
  return (
    <ProductShell surface="agents" title="Agents">
      <div className="console-stack">
        <PageIntro title="Agents">
          Complete configured agent systems, with connection and evidence details kept
          together.
        </PageIntro>
        {error && <ErrorState title="Agents are unavailable" detail={error} />}
        {!value && !error && <LoadingState title="Loading agents" />}
        {value && !systems.length && !models.length && (
          <EmptyState
            title="No agent system is configured"
            detail="Continue setup to detect and verify a local agent connection."
          >
            <a href="/console/onboarding">Continue setup</a>
          </EmptyState>
        )}
        {systems.map((system) => {
          const supported = Object.values(system.capabilities).filter(
            (capability) => capability.state === "supported",
          ).length;
          const unknown = Object.values(system.capabilities).filter(
            (capability) => capability.state === "unknown",
          ).length;
          const ready =
            system.production_enabled &&
            ["qualified", "bootstrap"].includes(system.qualification_status);
          return (
            <Panel key={system.system_id} className="agent-system">
              <PanelHeader
                title={`${system.harness.display_name} · ${system.model_provider.model_id}`}
                meta={system.route_name}
                actions={
                  <StatusBadge
                    status={ready ? "selected" : "failed"}
                    label={ready ? "SELECTABLE" : "DISABLED"}
                  />
                }
              />
              <div className="v-panel__body console-stack">
                <ResultVerdict
                  status={ready ? "completed" : "failed"}
                  label={ready ? "Ready for tasks" : "Not selectable"}
                  detail={`${system.harness.harness_id}@${system.harness.version} · ${system.model_provider.provider}/${system.model_provider.model_id}`}
                />
                <KeyValueGrid
                  items={[
                    ["Qualification", system.qualification_status],
                    ["Harness protocol", system.harness.protocol_version],
                    ["Execution provider", system.execution.execution_provider],
                    ["Permission profile", system.execution.permission_profile],
                    ["Network policy", system.execution.network_policy],
                    ["Capabilities supported", supported],
                    ["Capabilities unknown", unknown],
                    ["Billing", system.billing.mode],
                  ]}
                />
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
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      client.settings(controller.signal),
      client.runOptions(controller.signal),
    ])
      .then(([settingsValue, optionsValue]) => {
        setSettings(settingsValue);
        setOptions(optionsValue);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
  }, [client]);
  const repositories = options?.repositories.filter((item) => item.valid) ?? [];
  return (
    <ProductShell surface="settings" title="Settings">
      <div className="console-stack settings-page">
        <PageIntro title="Settings">
          Setup, service, repositories, privacy, delivery defaults, updates,
          diagnostics, and advanced tools.
        </PageIntro>
        {error && <ErrorState title="Some settings are unavailable" detail={error} />}
        {!settings && !error && <LoadingState title="Loading settings" />}
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
              ]}
            />
          </Panel>
          <Panel id="updates">
            <PanelHeader title="Updates" />
            <KeyValueGrid
              items={[
                ["Installed version", environment.version],
                [
                  "Migration",
                  environment.setup.valid ? "No blocked migration" : "Needs attention",
                ],
                ["Configuration format", environment.setup.schema_version ?? "Unknown"],
                ["Update channel", "Not configured"],
              ]}
            />
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
