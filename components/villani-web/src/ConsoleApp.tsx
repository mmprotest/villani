import { useEffect, useMemo, useState, type ReactNode } from "react";
import type {
  ConsoleBootstrap,
  ConsoleHistoryEntry,
  ConsoleReplaySnapshot,
} from "@villani/run-model";
import {
  DataTable,
  EmptyState,
  ErrorState,
  KeyValueGrid,
  LoadingState,
  MetricCard,
  Panel,
  PanelHeader,
  StatusBadge,
  Timeline,
  TimelineNode,
} from "@villani/ui/react";
import App from "./App";
import FleetApp from "./FleetApp";
import InterrogateApp from "./InterrogateApp";
import { ConsoleClient, type ConsoleHomeDocument } from "./consoleApi";
import {
  ConsoleProvider,
  defaultBootstrap,
  useConsoleEnvironment,
} from "./consoleContext";
import { ProductShell, type Surface } from "./ProductShell";

const decode = (value: string) => {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
};

export function migrateLegacyPath(pathname: string): string {
  if (pathname === "/") return "/console";
  if (pathname === "/flight" || pathname === "/flight/") return "/console/replay";
  const flightRun = pathname.match(/^\/flight\/runs\/([^/]+)(.*)$/);
  if (flightRun) {
    const suffix = flightRun[2];
    return suffix && suffix !== "/"
      ? `/console/runs/${flightRun[1]}${suffix}`
      : `/console/runs/${flightRun[1]}/replay`;
  }
  const flightSession = pathname.match(/^\/flight\/sessions\/([^/]+)(.*)$/);
  if (flightSession) return `/console/sessions/${flightSession[1]}${flightSession[2]}`;
  const run = pathname.match(/^\/runs\/([^/]+)(.*)$/);
  if (run) return `/console/runs/${run[1]}${run[2]}`;
  if (pathname === "/fleet" || pathname.startsWith("/fleet/"))
    return pathname.replace(/^\/fleet/, "/console/fleet");
  if (pathname === "/ask" || pathname.startsWith("/ask/")) return "/console/audit";
  for (const route of ["history", "replay", "models", "policies", "settings"])
    if (pathname === `/${route}`) return `/console/${route}`;
  return pathname;
}

function useLoader<T>(loader: (signal: AbortSignal) => Promise<T>, keys: unknown[]) {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    void loader(controller.signal)
      .then(setValue)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
    // The caller supplies explicit stable dependency keys.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...keys, version]);
  return { value, error, reload: () => setVersion((current) => current + 1) };
}

const money = (value: number | null, currency: string | null = "USD") =>
  value === null ? "Unknown" : `${currency ?? "USD"} ${value.toFixed(4)}`;
const date = (value: string | null) =>
  value ? new Date(value).toLocaleString() : "Not captured";
const syncTone = (state: string) =>
  state === "SYNC FAILED"
    ? "failed"
    : state === "SYNC PENDING"
      ? "running"
      : state === "REDACTED"
        ? "redacted"
        : "selected";

function PageIntro({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <header className="console-page-intro">
      <h1 tabIndex={-1}>{title}</h1>
      {children && <p>{children}</p>}
    </header>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <label className="v-field">
      <span className="v-field__label">{label}</span>
      <select
        className="v-select"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option value={option.value} key={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function HistoryPanel({
  title,
  entries,
}: {
  title: string;
  entries: ConsoleHistoryEntry[];
}) {
  return (
    <Panel>
      <PanelHeader title={title} meta={`${entries.length} shown`} />
      <div className="v-panel__body">
        {entries.length ? (
          <ul className="console-list">
            {entries.map((entry) => (
              <li key={`${entry.kind}:${entry.logical_id}`}>
                <a href={entry.deep_link}>{entry.task ?? entry.id}</a>
                <span className="v-muted">
                  {" "}
                  {entry.source_label} · {entry.status}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="v-muted">No activity recorded yet.</p>
        )}
      </div>
    </Panel>
  );
}

function HomePage({ client }: { client: ConsoleClient }) {
  const environment = useConsoleEnvironment();
  const { value, error, reload } = useLoader<ConsoleHomeDocument>(
    (signal) => client.home(signal),
    [client],
  );
  if (error)
    return (
      <ProductShell surface="home" title="HOME" status="failed">
        <ErrorState title="Home data is unavailable" detail={error}>
          <button className="v-button" type="button" onClick={reload}>
            Try again
          </button>
        </ErrorState>
      </ProductShell>
    );
  if (!value)
    return (
      <ProductShell surface="home" title="HOME">
        <LoadingState title="Loading local activity" />
      </ProductShell>
    );
  const rate =
    value.accepted_task_rate === null
      ? "Not enough data"
      : `${Math.round(value.accepted_task_rate * 100)}%`;
  return (
    <ProductShell surface="home" title="HOME">
      <div className="console-stack">
        <PageIntro title="Home">
          Your local activity and actionable health checks.
        </PageIntro>
        {value.setup_issues.map((issue) => (
          <div className="v-notice" role="alert" key={issue}>
            {issue}
          </div>
        ))}
        <div className="v-grid v-grid--metrics">
          <MetricCard
            label="Service"
            value={value.service.status}
            detail={value.service.last_error ?? "Healthy"}
          />
          <MetricCard
            label="Configured models"
            value={String(value.models.filter((model) => model.configured).length)}
            detail={value.models[0]?.id ?? "Run villani setup"}
          />
          <MetricCard
            label="Accepted-task rate"
            value={rate}
            detail="Finalized local runs"
          />
          <MetricCard
            label="Pending synchronization"
            value={String(value.pending_synchronization)}
            detail={
              environment.workspace.connected ? "Connected workspace" : "Local only"
            }
          />
        </div>
        <div className="v-grid v-grid--2">
          <HistoryPanel title="RECENT RUNS" entries={value.recent_runs} />
          <HistoryPanel title="IMPORTED SESSIONS" entries={value.recent_sessions} />
        </div>
        <Panel>
          <PanelHeader
            title="RECOVERY EVENTS"
            meta={`${value.recent_recovery_events.length} recent`}
          />
          <div className="v-panel__body">
            {value.recent_recovery_events.length ? (
              <ul className="console-list">
                {value.recent_recovery_events.map((event, index) => (
                  <li key={String(event.id ?? index)}>
                    <strong>{String(event.name ?? "Recovery event")}</strong>{" "}
                    <span className="v-muted">{String(event.timestamp ?? "")}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="v-muted">No recovery events recorded.</p>
            )}
          </div>
        </Panel>
      </div>
    </ProductShell>
  );
}

export type HistoryFilters = {
  repository: string;
  source: string;
  status: string;
  model: string;
  date: string;
  synchronization: string;
  cost: string;
  task: string;
};
const initialFilters: HistoryFilters = {
  repository: "",
  source: "",
  status: "",
  model: "",
  date: "",
  synchronization: "",
  cost: "",
  task: "",
};

function choices(entries: ConsoleHistoryEntry[], key: keyof ConsoleHistoryEntry) {
  return [
    ...new Set(
      entries
        .map((entry) => entry[key])
        .filter(Boolean)
        .map(String),
    ),
  ].sort();
}

export function filterHistory(
  entries: ConsoleHistoryEntry[],
  filters: HistoryFilters,
  now = Date.now(),
) {
  const days = filters.date ? Number(filters.date) : 0;
  return entries.filter((entry) => {
    if (filters.repository && entry.repository !== filters.repository) return false;
    if (filters.source && entry.source !== filters.source) return false;
    if (filters.status && entry.status !== filters.status) return false;
    if (filters.model && entry.model !== filters.model) return false;
    if (
      filters.synchronization &&
      entry.synchronization_state !== filters.synchronization
    )
      return false;
    if (filters.cost === "known" && !entry.cost_available) return false;
    if (filters.cost === "unknown" && entry.cost_available) return false;
    if (
      filters.task &&
      !`${entry.task ?? ""} ${entry.id}`
        .toLowerCase()
        .includes(filters.task.toLowerCase())
    )
      return false;
    if (days) {
      const timestamp = Date.parse(entry.updated_at ?? entry.started_at ?? "");
      if (!Number.isFinite(timestamp) || timestamp < now - days * 86_400_000)
        return false;
    }
    return true;
  });
}

function HistoryPage({ client }: { client: ConsoleClient }) {
  const [filters, setFilters] = useState(initialFilters);
  const [refresh, setRefresh] = useState(false);
  const { value, error, reload } = useLoader(
    (signal) => client.history(refresh, signal),
    [client, refresh],
  );
  const entries = useMemo(() => {
    const unique = new Map<string, ConsoleHistoryEntry>();
    for (const entry of value?.entries ?? [])
      unique.set(`${entry.kind}:${entry.logical_id}`, entry);
    return [...unique.values()];
  }, [value]);
  const filtered = useMemo(() => filterHistory(entries, filters), [entries, filters]);
  const update = (name: keyof HistoryFilters, selected: string) =>
    setFilters((current) => ({ ...current, [name]: selected }));
  const select = (name: keyof HistoryFilters, label: string, values: string[]) => (
    <FilterSelect
      label={label}
      value={filters[name]}
      onChange={(value) => update(name, value)}
      options={[
        { value: "", label: `All ${label.toLowerCase()}` },
        ...values.map((item) => ({ value: item, label: item })),
      ]}
    />
  );
  return (
    <ProductShell surface="history" title="HISTORY">
      <div className="console-stack">
        <PageIntro title="History">
          Villani runs and imported coding-agent sessions in one chronology.
        </PageIntro>
        <Panel>
          <PanelHeader
            title="FILTERS"
            actions={
              <button
                className="v-button"
                type="button"
                onClick={() => {
                  setRefresh(true);
                  reload();
                }}
              >
                Refresh sources
              </button>
            }
          />
          <form className="history-filters v-panel__body" aria-label="History filters">
            {select("repository", "Repository", choices(entries, "repository"))}
            {select("source", "Source", choices(entries, "source"))}
            {select("status", "Status", choices(entries, "status"))}
            {select("model", "Model", choices(entries, "model"))}
            <FilterSelect
              label="Date"
              value={filters.date}
              onChange={(value) => update("date", value)}
              options={[
                { value: "", label: "Any date" },
                { value: "1", label: "Past 24 hours" },
                { value: "7", label: "Past 7 days" },
                { value: "30", label: "Past 30 days" },
              ]}
            />
            <FilterSelect
              label="Synchronized state"
              value={filters.synchronization}
              onChange={(value) => update("synchronization", value)}
              options={[
                { value: "", label: "Any sync state" },
                ...[
                  "LOCAL",
                  "SYNC PENDING",
                  "SYNCHRONIZED",
                  "REDACTED",
                  "SYNC FAILED",
                ].map((item) => ({ value: item, label: item })),
              ]}
            />
            <FilterSelect
              label="Cost availability"
              value={filters.cost}
              onChange={(value) => update("cost", value)}
              options={[
                { value: "", label: "Any cost" },
                { value: "known", label: "Known" },
                { value: "unknown", label: "Unknown" },
              ]}
            />
            <label className="v-field">
              <span className="v-field__label">Task text</span>
              <input
                className="v-input"
                value={filters.task}
                onChange={(event) => update("task", event.target.value)}
                placeholder="Search tasks"
              />
            </label>
          </form>
        </Panel>
        {error && <ErrorState title="History is unavailable" detail={error} />}
        {!value && !error && <LoadingState title="Loading history" />}
        {value?.warnings.map((warning) => (
          <div className="v-notice" key={warning}>
            {warning}
          </div>
        ))}
        {value && (
          <Panel data-testid="merged-history">
            <PanelHeader
              title="ALL ACTIVITY"
              meta={`${filtered.length} of ${entries.length} records`}
            />
            <DataTable
              caption="Local and synchronized Villani activity"
              rows={filtered}
              getRowKey={(entry) => `${entry.kind}:${entry.logical_id}`}
              empty="No activity matches these filters."
              columns={[
                {
                  key: "task",
                  header: "Task / session",
                  render: (entry) => (
                    <a href={entry.deep_link}>{entry.task ?? entry.id}</a>
                  ),
                },
                {
                  key: "repository",
                  header: "Repository",
                  render: (entry) => entry.repository ?? "Unknown",
                },
                {
                  key: "source",
                  header: "Source",
                  render: (entry) => entry.source_label,
                },
                { key: "status", header: "Status" },
                {
                  key: "model",
                  header: "Model",
                  render: (entry) => entry.model ?? "Unknown",
                },
                {
                  key: "updated",
                  header: "Updated",
                  render: (entry) => date(entry.updated_at),
                },
                {
                  key: "cost",
                  header: "Cost",
                  render: (entry) => money(entry.cost, entry.currency),
                },
                {
                  key: "synchronization_state",
                  header: "Synchronization",
                  render: (entry) => (
                    <StatusBadge
                      status={syncTone(entry.synchronization_state)}
                      label={entry.synchronization_state}
                    />
                  ),
                },
              ]}
            />
          </Panel>
        )}
      </div>
    </ProductShell>
  );
}

function RunPage() {
  const environment = useConsoleEnvironment();
  return (
    <ProductShell surface="run" title="RUN">
      <div className="console-stack">
        <PageIntro title="Run">Start work with the configured local backend.</PageIntro>
        <Panel>
          <PanelHeader title="READY CHECK" />
          <KeyValueGrid
            items={[
              ["Configuration", environment.setup.valid ? "Ready" : "Needs setup"],
              ["Service", environment.service.status],
              ["Default model", environment.models[0]?.id ?? "Not configured"],
              ["Policy", environment.active_policy ?? "Not configured"],
            ]}
          />
          <div className="v-panel__body">
            {environment.setup.valid ? (
              <p>
                Run <code>villani run --task &quot;your task&quot;</code> in your
                repository. The run will appear in{" "}
                <a href="/console/history">History</a>.
              </p>
            ) : (
              <p>
                No coding backend is configured. Run: <code>villani setup</code>
              </p>
            )}
          </div>
        </Panel>
      </div>
    </ProductShell>
  );
}

function ReplayLanding({ client }: { client: ConsoleClient }) {
  const { value, error } = useLoader(
    (signal) => client.history(false, signal),
    [client],
  );
  return (
    <ProductShell surface="replay" title="REPLAY">
      <div className="console-stack">
        <PageIntro title="Replay">Inspect any local run or imported session.</PageIntro>
        {error && <ErrorState title="Replay index is unavailable" detail={error} />}
        {!value && !error && <LoadingState title="Loading replay index" />}
        {value && (
          <HistoryPanel
            title="AVAILABLE REPLAYS"
            entries={value.entries.slice(0, 25)}
          />
        )}
      </div>
    </ProductShell>
  );
}

function ReplayPage({
  client,
  id,
  kind,
  deepKind,
  deepId,
}: {
  client: ConsoleClient;
  id: string;
  kind: "run" | "session";
  deepKind?: string;
  deepId?: string;
}) {
  const environment = useConsoleEnvironment();
  const { value, error, reload } = useLoader<ConsoleReplaySnapshot>(
    (signal) => client.replay(id, kind, environment.data_source, signal),
    [client, id, kind, environment.data_source],
  );
  if (error)
    return (
      <ProductShell surface="replay" title="REPLAY" detail={id} status="failed">
        <ErrorState title="Unable to load replay" detail={error}>
          <button className="v-button" type="button" onClick={reload}>
            Try again
          </button>
        </ErrorState>
      </ProductShell>
    );
  if (!value)
    return (
      <ProductShell surface="replay" title="REPLAY" detail={id}>
        <LoadingState title="Loading replay" />
      </ProductShell>
    );
  const focused = (type: string, candidate: string) =>
    deepKind === type && deepId === candidate;
  return (
    <ProductShell
      surface="replay"
      title="REPLAY"
      detail={id}
      status={value.summary.status}
      statusText={value.synchronization_state}
    >
      <div className="console-stack replay-page" data-testid="console-replay">
        <PageIntro title={value.summary.task ?? value.id}>
          {value.source_label} · {value.summary.status}
        </PageIntro>
        <Panel id="summary">
          <PanelHeader title="SUMMARY" meta={value.id} />
          <KeyValueGrid
            items={[
              ["Source", value.source_label],
              ["Repository", value.summary.repository ?? "Unknown"],
              ["Model", value.summary.model ?? "Unknown"],
              ["Policy", value.summary.policy ?? "Unknown"],
              ["Started", date(value.summary.started_at)],
              ["Completed", date(value.summary.completed_at)],
              [
                "Duration",
                value.summary.duration_ms === null
                  ? "Unknown"
                  : `${value.summary.duration_ms} ms`,
              ],
              ["Total tokens", value.summary.total_tokens ?? "Unknown"],
              ["Events", value.events.length],
              ["Attempts", value.attempts.length],
              ["Synchronization", value.synchronization_state],
              ["Terminal reason", value.summary.terminal_reason ?? "None"],
            ]}
          />
        </Panel>
        <Panel id="timeline" data-testid="replay-timeline">
          <PanelHeader title="TIMELINE" meta={`${value.events.length} events`} />
          <div className="v-panel__body">
            {value.events.length ? (
              <Timeline aria-label="Replay timeline">
                {value.events.map((event) => (
                  <TimelineNode
                    key={event.id}
                    title={<a href={event.deep_link}>{event.title}</a>}
                    meta={`${date(event.timestamp)} · ${event.source} · ${event.status}`}
                    active={focused("events", event.id)}
                    data-testid={
                      focused("events", event.id) ? "deep-link-target" : undefined
                    }
                  >
                    {event.summary && <p>{event.summary}</p>}
                  </TimelineNode>
                ))}
              </Timeline>
            ) : (
              <EmptyState title="No timeline events" />
            )}
          </div>
        </Panel>
        <Panel id="event-stream" data-testid="event-stream">
          <PanelHeader title="EVENT STREAM" />
          <DataTable
            rows={value.events}
            getRowKey={(event) => event.id}
            empty="No events recorded."
            columns={[
              {
                key: "sequence",
                header: "#",
                render: (event) => event.sequence ?? "-",
              },
              {
                key: "timestamp",
                header: "Time",
                render: (event) => date(event.timestamp),
              },
              {
                key: "title",
                header: "Event",
                render: (event) => <a href={event.deep_link}>{event.title}</a>,
              },
              { key: "kind", header: "Kind" },
              { key: "status", header: "Status" },
              {
                key: "attempt",
                header: "Attempt",
                render: (event) => event.attempt_id ?? "-",
              },
            ]}
          />
        </Panel>
        <Panel id="attempts">
          <PanelHeader title="ATTEMPTS" meta={`${value.attempts.length}`} />
          <DataTable
            rows={value.attempts}
            getRowKey={(attempt) => attempt.id}
            empty="No attempt records are available for this imported session."
            columns={[
              {
                key: "id",
                header: "Attempt",
                render: (attempt) => (
                  <a
                    href={attempt.deep_link}
                    data-testid={
                      focused("attempts", attempt.id) ? "deep-link-target" : undefined
                    }
                  >
                    {attempt.id}
                  </a>
                ),
              },
              {
                key: "status",
                header: "Status",
                render: (attempt) => attempt.status ?? "Unknown",
              },
              {
                key: "backend",
                header: "Backend",
                render: (attempt) => attempt.backend ?? "Unknown",
              },
              {
                key: "model",
                header: "Model",
                render: (attempt) => attempt.model ?? "Unknown",
              },
              {
                key: "eligible",
                header: "Eligible",
                render: (attempt) =>
                  attempt.eligible === null
                    ? "Unknown"
                    : attempt.eligible
                      ? "Yes"
                      : "No",
              },
              {
                key: "selected",
                header: "Selected",
                render: (attempt) => (attempt.selected ? "Yes" : "No"),
              },
            ]}
          />
        </Panel>
        <div className="v-grid v-grid--2">
          <Panel id="evidence" data-testid="evidence-panel">
            <PanelHeader title="EVIDENCE" />
            <pre className="v-code">{JSON.stringify(value.evidence, null, 2)}</pre>
          </Panel>
          <Panel id="verification">
            <PanelHeader title="VERIFICATION" />
            <pre className="v-code">{JSON.stringify(value.verification, null, 2)}</pre>
          </Panel>
        </div>
        <Panel id="candidate-comparison" data-testid="candidate-comparison">
          <PanelHeader title="CANDIDATE COMPARISON" />
          <DataTable
            rows={value.candidate_comparison}
            getRowKey={(attempt) => attempt.id}
            empty="No candidate comparison is available."
            columns={[
              { key: "id", header: "Candidate" },
              {
                key: "verification",
                header: "Verification",
                render: (attempt) => attempt.verification_outcome ?? "Unknown",
              },
              {
                key: "authority",
                header: "Authority",
                render: (attempt) => attempt.verification_authority ?? "Unknown",
              },
              {
                key: "cost",
                header: "Cost",
                render: (attempt) => money(attempt.cost, attempt.currency),
              },
              {
                key: "duration",
                header: "Duration",
                render: (attempt) =>
                  attempt.duration_ms === null
                    ? "Unknown"
                    : `${attempt.duration_ms} ms`,
              },
            ]}
          />
        </Panel>
        <Panel id="files" data-testid="file-activity">
          <PanelHeader title="FILES" meta={`${value.files.length}`} />
          <DataTable
            rows={value.files}
            getRowKey={(file) => `${file.attempt_id}:${file.path}`}
            empty="No changed files recorded."
            columns={[
              {
                key: "path",
                header: "Path",
                render: (file) => (
                  <a
                    href={file.deep_link}
                    data-testid={
                      focused("files", file.path) ? "deep-link-target" : undefined
                    }
                  >
                    {file.path}
                  </a>
                ),
              },
              {
                key: "attempt",
                header: "Attempt",
                render: (file) => file.attempt_id ?? "-",
              },
              {
                key: "materialized",
                header: "Materialized",
                render: (file) => (file.materialized ? "Yes" : "No"),
              },
            ]}
          />
        </Panel>
        <Panel id="cost">
          <PanelHeader title="COST" meta={value.cost.accounting_status} />
          <KeyValueGrid
            items={[
              ["Coding", money(value.cost.coding, value.cost.currency)],
              ["Verification", money(value.cost.verification, value.cost.currency)],
              ["Total", money(value.cost.total, value.cost.currency)],
              ["Accounting", value.cost.accounting_status],
            ]}
          />
        </Panel>
        <Panel id="logs">
          <PanelHeader title="LOGS" meta={`${value.logs.length}`} />
          <div className="v-panel__body replay-logs">
            {value.logs.length ? (
              value.logs.map((log) => (
                <details key={log.id}>
                  <summary>
                    {log.stream} · {log.event_id}
                  </summary>
                  <pre className="v-code">{log.content}</pre>
                </details>
              ))
            ) : (
              <p className="v-muted">No captured logs.</p>
            )}
          </div>
        </Panel>
      </div>
    </ProductShell>
  );
}

function ModelsPage() {
  const environment = useConsoleEnvironment();
  return (
    <ProductShell surface="models" title="MODELS">
      <div className="console-stack">
        <PageIntro title="Models">Configured and detected coding models.</PageIntro>
        <Panel>
          <PanelHeader title="MODEL INVENTORY" meta={`${environment.models.length}`} />
          <DataTable
            rows={environment.models}
            getRowKey={(model) => `${model.provider}:${model.id}`}
            empty="No models are configured. Run: villani setup"
            columns={[
              { key: "id", header: "Model" },
              { key: "provider", header: "Provider" },
              {
                key: "configured",
                header: "Configured",
                render: (model) => (model.configured ? "Yes" : "No"),
              },
              {
                key: "available",
                header: "Available",
                render: (model) =>
                  model.available === null
                    ? "Not checked"
                    : model.available
                      ? "Yes"
                      : "No",
              },
              { key: "capability", header: "Capability" },
              {
                key: "context",
                header: "Context",
                render: (model) => model.context_window ?? "Unknown",
              },
              {
                key: "pricing",
                header: "Pricing",
                render: (model) => model.pricing_status,
              },
            ]}
          />
        </Panel>
      </div>
    </ProductShell>
  );
}

function PoliciesPage({ client }: { client: ConsoleClient }) {
  const { value, error } = useLoader((signal) => client.policies(signal), [client]);
  return (
    <ProductShell surface="policies" title="POLICIES">
      <div className="console-stack">
        <PageIntro title="Policies">
          Simple presets and the active local policy.
        </PageIntro>
        {error && <ErrorState title="Policies are unavailable" detail={error} />}
        {!value && !error && <LoadingState title="Loading policies" />}
        {value && (
          <Panel>
            <PanelHeader
              title="POLICY PRESETS"
              meta={`Active: ${value.active_policy ?? "None"}`}
            />
            <div className="policy-presets v-panel__body">
              {value.presets.map((preset) => (
                <article key={preset.id} className="policy-preset">
                  <h2>{preset.label}</h2>
                  <StatusBadge
                    status={preset.active ? "selected" : "unknown"}
                    label={preset.active ? "ACTIVE" : "AVAILABLE"}
                  />
                </article>
              ))}
            </div>
          </Panel>
        )}
      </div>
    </ProductShell>
  );
}

function SettingsPage() {
  const environment = useConsoleEnvironment();
  return (
    <ProductShell surface="settings" title="SETTINGS">
      <div className="console-stack">
        <PageIntro title="Settings">
          Local configuration, service, privacy, and paths.
        </PageIntro>
        <Panel>
          <PanelHeader title="LOCAL CONFIGURATION" />
          <KeyValueGrid
            items={[
              ["Configuration", environment.setup.valid ? "Valid" : "Needs attention"],
              ["Schema", environment.setup.schema_version ?? "Not configured"],
              ["Service", environment.service.status],
              ["Service log", environment.service.log_path ?? "Unavailable"],
              ["Storage", environment.storage.home || "Hosted workspace"],
              ["Runs", environment.storage.runs || "Hosted workspace"],
              ["Storage writable", environment.storage.writable ? "Yes" : "No"],
              [
                "Workspace",
                environment.workspace.connected
                  ? (environment.workspace.id ?? "Connected")
                  : "Not connected",
              ],
              ["Privacy", "Local-first; secrets are not exposed to the browser"],
              ["Pending synchronization", environment.synchronization.pending],
            ]}
          />
        </Panel>
        {environment.setup.issues.map((issue) => (
          <div key={issue} className="v-notice">
            {issue}
          </div>
        ))}
      </div>
    </ProductShell>
  );
}

function WorkspacePage({
  client,
  surface,
}: {
  client: ConsoleClient;
  surface: Surface;
}) {
  const environment = useConsoleEnvironment();
  const { value, error } = useLoader(
    (signal) => client.workspace(surface, signal),
    [client, surface],
  );
  if (!environment.workspace.connected)
    return (
      <ProductShell surface={surface} title={surface.toUpperCase()}>
        <ErrorState
          title="No workspace is connected"
          detail="Connect a workspace before using Team views."
        />
      </ProductShell>
    );
  return (
    <ProductShell surface={surface} title={surface.toUpperCase()}>
      <div className="console-stack">
        <PageIntro title={surface[0]!.toUpperCase() + surface.slice(1)}>
          Connected workspace data.
        </PageIntro>
        {error && <ErrorState title="Workspace data is unavailable" detail={error} />}
        {!value && !error && <LoadingState title={`Loading ${surface}`} />}
        {value && (
          <Panel>
            <PanelHeader
              title={surface.toUpperCase()}
              meta={value.workspace_id ?? "Connected"}
            />
            <DataTable
              rows={value.items}
              getRowKey={(item, index) => String(item.id ?? index)}
              empty="No records are available."
              columns={[
                {
                  key: "summary",
                  header: "Item",
                  render: (item) =>
                    item.deep_link ? (
                      <a href={String(item.deep_link)}>
                        {String(item.summary ?? item.id ?? "Record")}
                      </a>
                    ) : (
                      String(item.summary ?? item.id ?? "Record")
                    ),
                },
                {
                  key: "status",
                  header: "Status",
                  render: (item) => String(item.status ?? "Unknown"),
                },
                {
                  key: "detail",
                  header: "Details",
                  render: (item) => {
                    if (item.cost === null) return "Cost unknown";
                    if (typeof item.cost === "number")
                      return money(item.cost, String(item.currency ?? "USD"));
                    return String(item.detail ?? "-");
                  },
                },
              ]}
            />
          </Panel>
        )}
      </div>
    </ProductShell>
  );
}

function NotFoundPage() {
  return (
    <ProductShell surface="home" title="NOT FOUND" status="failed">
      <ErrorState
        title="This Console link is not supported"
        detail="Open History to find a run or session."
      >
        <a href="/console/history">Open History</a>
      </ErrorState>
    </ProductShell>
  );
}

function ConsoleRoutes({ client, path }: { client: ConsoleClient; path: string }) {
  const environment = useConsoleEnvironment();
  const replay = path.match(
    /^\/console\/(runs|sessions)\/([^/]+)(?:\/(replay|events|attempts|files)(?:\/(.*))?)?$/,
  );
  if (replay) {
    const kind = replay[1] === "runs" ? "run" : "session";
    const id = decode(replay[2]!);
    const deepKind = replay[3];
    const deepId = replay[4] ? decode(replay[4]) : undefined;
    if (
      deepKind === undefined &&
      kind === "run" &&
      environment.data_source === "workspace"
    )
      return <App />;
    return (
      <ReplayPage
        client={client}
        id={id}
        kind={kind}
        deepKind={deepKind}
        deepId={deepId}
      />
    );
  }
  if (path === "/console" || path === "/console/") return <HomePage client={client} />;
  if (path === "/console/run") return <RunPage />;
  if (path === "/console/history") return <HistoryPage client={client} />;
  if (path === "/console/replay") return <ReplayLanding client={client} />;
  if (path === "/console/models") return <ModelsPage />;
  if (path === "/console/policies") return <PoliciesPage client={client} />;
  if (path === "/console/settings") return <SettingsPage />;
  if (path === "/console/fleet" && environment.data_source === "workspace")
    return <FleetApp />;
  if (path === "/console/audit" && environment.data_source === "workspace")
    return <InterrogateApp />;
  for (const surface of ["fleet", "tasks", "costs", "alerts", "audit"] as Surface[])
    if (path === `/console/${surface}`)
      return <WorkspacePage client={client} surface={surface} />;
  return <NotFoundPage />;
}

export default function ConsoleApp() {
  const original = location.pathname;
  const migrated = migrateLegacyPath(original);
  if (migrated !== original)
    history.replaceState(null, "", `${migrated}${location.search}${location.hash}`);
  const [path, setPath] = useState(migrated);
  const client = useMemo(
    () =>
      new ConsoleClient(
        import.meta.env.VITE_API_BASE_URL ?? "",
        sessionStorage.getItem("villani.token") ?? import.meta.env.VITE_API_TOKEN ?? "",
      ),
    [],
  );
  const [bootstrap, setBootstrap] = useState<ConsoleBootstrap>(defaultBootstrap);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void client.bootstrap(controller.signal).then((value) => {
      setBootstrap(value);
      setLoaded(true);
    });
    const navigate = () => setPath(location.pathname);
    addEventListener("popstate", navigate);
    return () => {
      controller.abort();
      removeEventListener("popstate", navigate);
    };
  }, [client]);
  if (!loaded)
    return (
      <ConsoleProvider value={bootstrap}>
        <ProductShell surface="home" title="VILLANI CONSOLE">
          <LoadingState title="Opening Villani Console" />
        </ProductShell>
      </ConsoleProvider>
    );
  return (
    <ConsoleProvider value={bootstrap}>
      <ConsoleRoutes client={client} path={path} />
    </ConsoleProvider>
  );
}
