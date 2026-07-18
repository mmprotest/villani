import { useEffect, useMemo, useState, type FormEvent } from "react";
import type {
  ConsoleBootstrap,
  ConsoleHistoryEntry,
  ConsoleReplaySnapshot,
} from "@villani/run-model";
import {
  DataTable,
  CostDisplay,
  DurationDisplay,
  EmptyState,
  ErrorState,
  EvidenceDisclosure,
  FormField,
  KeyValueGrid,
  LoadingState,
  PageIntro,
  Panel,
  PanelHeader,
  ResultVerdict,
  StatusBadge,
  TaskComposerShell,
  Timeline,
  TimelineNode,
} from "@villani/ui/react";
import App from "./App";
import FleetApp from "./FleetApp";
import InterrogateApp from "./InterrogateApp";
import {
  ConsoleClient,
  type ConsoleRunOptions,
  type ConsoleValidationDiscovery,
  type PolicyPreview,
  type RunFailure,
  type RunPresentation,
} from "./consoleApi";
import {
  ConsoleProvider,
  defaultBootstrap,
  useConsoleEnvironment,
} from "./consoleContext";
import { ProductShell, type Surface } from "./ProductShell";
import { OnboardingPage } from "./OnboardingPage";
import { AgentsPage, SettingsPage } from "./ProductPages";
import { SingleTaskPage } from "./SingleTaskPage";

const decode = (value: string) => {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
};

export function migrateLegacyPath(pathname: string): string {
  if (pathname === "/") return "/console";
  if (["/run", "/console/run", "/console/home"].includes(pathname)) return "/console";
  if (["/history", "/console/history"].includes(pathname)) return "/console/activity";
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
  for (const route of ["replay", "models", "policies", "settings"])
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
const publicLanguage = (value: string) =>
  value
    .replace(/acceptance eligible/gi, "proved acceptable")
    .replace(/canonical truth/gi, "recorded evidence")
    .replace(/verifier authority/gi, "verification")
    .replace(/raw classification/gi, "task assessment")
    .replace(/effective classification/gi, "adjusted task assessment")
    .replace(/materialization/gi, "applying the change")
    .replace(/materialized/gi, "applied")
    .replace(/exhausted/gi, "could not prove");

const publicResult = (status: string) => {
  const key = status.toLowerCase();
  if (key.includes("accept")) return "Proved acceptable";
  if (key.includes("exhaust") || key.includes("reject")) return "Could not prove";
  if (key.includes("fail") || key.includes("error")) return "Could not complete";
  if (key.includes("run") || key.includes("queue")) return "In progress";
  if (key.includes("success") || key.includes("complete")) return "Completed";
  return publicLanguage(status);
};

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
                <a href={entry.deep_link}>{entry.task ?? entry.id}</a>{" "}
                <span className="v-muted">
                  {entry.source_label} · {publicResult(entry.status)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="v-muted">No recorded evidence is available yet.</p>
        )}
      </div>
    </Panel>
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

function ActivityPage({ client }: { client: ConsoleClient }) {
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
    <ProductShell surface="activity" title="Activity">
      <div className="console-stack">
        <PageIntro title="Activity">
          Every Villani task and imported coding session, in one chronological stream.
        </PageIntro>
        <details className="activity-filters run-advanced">
          <summary>Advanced filters</summary>
          <div className="activity-filters__actions">
            <button
              className="v-button"
              type="button"
              onClick={() => {
                setRefresh(true);
                reload();
              }}
            >
              Refresh imported sessions
            </button>
          </div>
          <form className="history-filters" aria-label="Activity filters">
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
        </details>
        {error && <ErrorState title="Activity is unavailable" detail={error} />}
        {!value && !error && <LoadingState title="Loading activity" />}
        {value?.warnings.map((warning) => (
          <div className="v-notice" key={warning}>
            {warning}
          </div>
        ))}
        {value && entries.length === 0 && (
          <EmptyState
            title="No activity yet"
            detail="Start a task and its result will appear here."
          >
            <a href="/console">Open New task</a>
          </EmptyState>
        )}
        {value && entries.length > 0 && (
          <Panel data-testid="merged-history">
            <PanelHeader
              title="Activity"
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
                  header: "Task",
                  render: (entry) => (
                    <div className="activity-task">
                      <a href={entry.deep_link}>{entry.task ?? entry.id}</a>
                      {entry.kind === "session" && (
                        <StatusBadge status="unknown" label="IMPORTED" />
                      )}
                    </div>
                  ),
                },
                {
                  key: "result",
                  header: "Result",
                  render: (entry) => publicResult(entry.status),
                },
                {
                  key: "repository",
                  header: "Repository",
                  render: (entry) => entry.repository ?? "Unknown",
                },
                {
                  key: "duration",
                  header: "Elapsed time",
                  render: (entry) => (
                    <DurationDisplay milliseconds={entry.duration_ms} />
                  ),
                },
                {
                  key: "cost",
                  header: "Known cost",
                  render: (entry) => (
                    <CostDisplay
                      value={entry.cost}
                      currency={entry.currency}
                      accountingStatus={entry.cost_available ? "complete" : "unknown"}
                    />
                  ),
                },
                {
                  key: "agent",
                  header: "Agent system",
                  render: (entry) => entry.model ?? entry.source_label ?? "Unknown",
                },
                {
                  key: "next",
                  header: "Next action",
                  render: (entry) => (
                    <a href={entry.deep_link}>
                      {entry.kind === "session"
                        ? "Review session"
                        : /fail|reject|exhaust/i.test(entry.status)
                          ? "Review evidence"
                          : "Open task"}
                    </a>
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

function RunFailureDetails({ failure }: { failure: RunFailure }) {
  return (
    <div className="run-failure" role="alert">
      <h3>{publicLanguage(failure.what_failed)}</h3>
      <dl className="run-result-list">
        <div>
          <dt>What Villani tried</dt>
          <dd>{publicLanguage(failure.what_villani_tried)}</dd>
        </div>
        <div>
          <dt>Evidence missing</dt>
          <dd>{publicLanguage(failure.missing_evidence)}</dd>
        </div>
        <div>
          <dt>Patch</dt>
          <dd>{publicLanguage(failure.patch_status)}</dd>
        </div>
        <div>
          <dt>Next</dt>
          <dd>{publicLanguage(failure.next_action)}</dd>
        </div>
      </dl>
    </div>
  );
}

const serviceOfflineFailure: RunFailure = {
  code: "service_offline",
  what_failed: "Villani Service is offline, so Console cannot submit or observe a run.",
  what_villani_tried: "Console attempted the authenticated local service boundary.",
  missing_evidence: "No live local service connection is available.",
  patch_preserved: false,
  patch_status: "No run was started, so no patch was created.",
  next_action: "Run `villani service start`, then retry.",
};

function LegacyRunResult({
  value,
  client,
  onUpdate,
}: {
  value: RunPresentation;
  client: ConsoleClient;
  onUpdate: (value: RunPresentation) => void;
}) {
  const terminal = value.outcome !== "RUNNING";
  const confidence = value.confidence;
  const cost = value.cost;
  const delivery = value.delivery;
  const review = delivery?.review;
  const [approvalReason, setApprovalReason] = useState("");
  const [candidateId, setCandidateId] = useState(value.selected_attempt_id ?? "");
  const [approvalPending, setApprovalPending] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);

  const takeApprovalAction = async (
    action: "approve" | "reject" | "request_rerun" | "choose_candidate",
  ) => {
    setApprovalPending(true);
    setApprovalError(null);
    try {
      onUpdate(
        await client.approvalActionLegacy(value.run_id, {
          action,
          reason: approvalReason.trim() || undefined,
          candidate_id: action === "choose_candidate" ? candidateId : undefined,
        }),
      );
    } catch (reason) {
      setApprovalError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setApprovalPending(false);
    }
  };
  return (
    <div className="console-stack run-result" data-testid="run-presentation">
      <ResultVerdict
        status={value.outcome}
        label={publicResult(value.outcome)}
        detail={publicLanguage(value.summary)}
      />
      <div className="run-result-context v-muted">
        <span>
          Task record: <a href={`/console/runs/${value.run_id}`}>{value.run_id}</a>
        </span>
        {value.lineage?.parent_run_id && (
          <span>
            Previous task:{" "}
            <a href={`/console/runs/${value.lineage.parent_run_id}`}>
              {value.lineage.parent_run_id}
            </a>
          </span>
        )}
      </div>

      {delivery && (
        <Panel>
          <PanelHeader title="CHANGE DELIVERY" meta={publicLanguage(delivery.label)} />
          <div className="v-panel__body console-stack">
            <KeyValueGrid
              items={[
                ["Mode", publicLanguage(delivery.mode)],
                ["State", publicLanguage(delivery.label)],
                [
                  "Target working tree",
                  delivery.target_worktree_modified ? "Modified" : "Not modified",
                ],
                [
                  "Permission",
                  delivery.authority.permitted
                    ? `Permitted · ${delivery.authority.policy_version ?? "policy"}`
                    : `Not permitted · ${delivery.authority.policy_version ?? "policy"}`,
                ],
                ["Approval", delivery.approval.status ?? "Not required"],
                ["Approval deadline", delivery.approval.deadline ?? "None"],
              ]}
            />
            {!!delivery.authority.reasons?.length && (
              <ul className="console-list">
                {delivery.authority.reasons.map((reason) => (
                  <li key={reason}>{publicLanguage(reason)}</li>
                ))}
              </ul>
            )}
          </div>
        </Panel>
      )}

      {value.outcome === "AWAITING APPROVAL" && review && delivery && (
        <Panel>
          <PanelHeader title="PATCH REVIEW" meta="Decision required" />
          <div className="v-panel__body console-stack">
            <KeyValueGrid
              items={[
                ["Files changed", review.files_changed.length],
                ["Insertions", review.insertions],
                ["Deletions", review.deletions],
                ["Verification", publicLanguage(review.verifier_authority)],
                ["Candidates compared", review.candidate_comparison.length],
                [
                  "Cost",
                  review.cost.value === null
                    ? `Unknown (${review.cost.accounting_status})`
                    : money(review.cost.value, review.cost.currency),
                ],
              ]}
            />
            <div className="v-grid v-grid--2 run-review-grid">
              <div>
                <h3>Files</h3>
                <ul className="console-list">
                  {review.files_changed.map((file) => (
                    <li key={file}>{file}</li>
                  ))}
                </ul>
              </div>
              <div>
                <h3>Recorded evidence</h3>
                <ul className="console-list">
                  {review.validation_evidence.map((item, index) => (
                    <li key={item.evidence_id ?? `${item.kind}-${index}`}>
                      {item.summary ?? item.kind ?? "Recorded evidence"}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
            {!!review.candidate_comparison.length && (
              <div>
                <h3>Candidate comparison</h3>
                <ol className="console-list">
                  {review.candidate_comparison.map((item, index) => (
                    <li key={String(item.attempt_id ?? index)}>
                      {String(item.attempt_id ?? "Candidate")}
                      {item.rank === undefined ? "" : ` · rank ${String(item.rank)}`}
                      {item.reason === undefined ? "" : ` · ${String(item.reason)}`}
                    </li>
                  ))}
                </ol>
              </div>
            )}
            {!![
              ...review.remaining_risks,
              ...review.unrelated_change_warnings,
              ...review.sensitive_file_warnings,
            ].length && (
              <div className="run-review-warnings" role="note">
                <strong>Risks and warnings</strong>
                <ul className="console-list">
                  {[
                    ...review.remaining_risks,
                    ...review.unrelated_change_warnings,
                    ...review.sensitive_file_warnings,
                  ].map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}
            <label className="v-field">
              <span className="v-field__label">Decision reason (optional)</span>
              <input
                className="v-input"
                value={approvalReason}
                onChange={(event) => setApprovalReason(event.target.value)}
                placeholder="Record why you made this decision"
              />
            </label>
            {delivery.approval.allow_candidate_change &&
              delivery.eligible_candidate_ids.length > 1 && (
                <div className="run-approval-candidate">
                  <label className="v-field">
                    <span className="v-field__label">Proved candidate</span>
                    <select
                      className="v-select"
                      value={candidateId}
                      onChange={(event) => setCandidateId(event.target.value)}
                    >
                      <option value="">Choose a candidate</option>
                      {delivery.eligible_candidate_ids.map((id) => (
                        <option value={id} key={id}>
                          {id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="v-button v-button--secondary"
                    type="button"
                    disabled={!candidateId || approvalPending}
                    onClick={() => void takeApprovalAction("choose_candidate")}
                  >
                    Select candidate
                  </button>
                </div>
              )}
            <div className="run-approval-actions">
              <button
                className="v-button"
                type="button"
                disabled={approvalPending}
                onClick={() => void takeApprovalAction("approve")}
              >
                {approvalPending ? "Recording decision…" : "Approve and apply change"}
              </button>
              <button
                className="v-button v-button--secondary"
                type="button"
                disabled={approvalPending}
                onClick={() => void takeApprovalAction("reject")}
              >
                Reject delivery
              </button>
              <button
                className="v-button v-button--secondary"
                type="button"
                disabled={approvalPending}
                onClick={() => void takeApprovalAction("request_rerun")}
              >
                Request rerun
              </button>
            </div>
            {approvalError && (
              <p className="v-danger" role="alert">
                {approvalError}
              </p>
            )}
          </div>
        </Panel>
      )}

      <Panel>
        <PanelHeader title={terminal ? "WHAT CHANGED" : "LIVE PROGRESS"} />
        <div className="v-panel__body">
          {!terminal && !value.progress.length && <p>Waiting for recorded progress.</p>}
          {!!value.progress.length && (
            <ol className="run-progress" aria-label="Run progress">
              {value.progress.map((line, index) => (
                <li
                  className={`run-progress--${line.tone}`}
                  key={`${index}:${line.message}`}
                >
                  <span aria-hidden="true">{line.symbol}</span>
                  <span>{publicLanguage(line.message)}</span>
                </li>
              ))}
            </ol>
          )}
          {terminal && (
            <>
              {value.changed?.files.length ? (
                <ul className="console-list">
                  {value.changed.files.map((file) => (
                    <li key={file}>{file}</li>
                  ))}
                </ul>
              ) : (
                <p>No files were changed.</p>
              )}
            </>
          )}
        </div>
      </Panel>

      {terminal && (
        <>
          <div className="v-grid v-grid--2">
            <Panel>
              <PanelHeader title="RESULT" />
              <KeyValueGrid
                items={[
                  [
                    "Outcome",
                    confidence?.label
                      ? publicLanguage(confidence.label)
                      : "Not established",
                  ],
                  [
                    "Confidence",
                    confidence?.value === null || confidence?.value === undefined
                      ? "Unknown"
                      : `${Math.round(confidence.value * 100)}%`,
                  ],
                  ["Verification", confidence?.authority ?? "None"],
                  ["Synchronization", value.synchronization_state ?? "LOCAL"],
                ]}
              />
            </Panel>
            <Panel>
              <PanelHeader title="VERIFICATION" />
              <KeyValueGrid
                items={[
                  [
                    "Repository checks passed",
                    value.validation.checks_passed ?? "Unknown",
                  ],
                  [
                    "Repository checks failed",
                    value.validation.checks_failed ?? "Unknown",
                  ],
                  [
                    "Repository checks not run",
                    value.validation.checks_not_run ?? "Unknown",
                  ],
                  [
                    "Repository checks unavailable",
                    value.validation.checks_unavailable ?? "Unknown",
                  ],
                  [
                    "Focused probes passed",
                    value.validation.focused_probes_passed ?? "Unknown",
                  ],
                  [
                    "Focused probes failed",
                    value.validation.focused_probes_failed ?? "Unknown",
                  ],
                  [
                    "Focused probes not run",
                    value.validation.focused_probes_not_run ?? "Unknown",
                  ],
                  [
                    "Focused probes unavailable",
                    value.validation.focused_probes_unavailable ?? "Unknown",
                  ],
                  [
                    "Task requirements proved",
                    value.validation.requirements_proved ?? "Unknown",
                  ],
                  [
                    "Task requirements not proved",
                    value.validation.requirements_not_proved ?? "Unknown",
                  ],
                  [
                    "Accounting",
                    value.canonical_summary?.accounting.known
                      ? `${value.canonical_summary.accounting.total_cost} ${value.canonical_summary.accounting.currency}`
                      : `Unknown (${value.canonical_summary?.accounting.accounting_status ?? value.cost?.accounting_status ?? "unknown"})`,
                  ],
                  ["Verification", value.validation.authority],
                ]}
              />
              <div className="v-panel__body">
                {value.validation.commands.map((command) => (
                  <code className="run-command" key={command.command}>
                    {command.command}
                  </code>
                ))}
              </div>
            </Panel>
          </div>
          <div className="v-grid v-grid--2">
            <Panel>
              <PanelHeader title="REMAINING RISKS" />
              <div className="v-panel__body">
                <ul className="console-list">
                  {(value.remaining_risks ?? ["No risk statement was recorded."]).map(
                    (risk) => (
                      <li key={risk}>{risk}</li>
                    ),
                  )}
                </ul>
              </div>
            </Panel>
            <Panel>
              <PanelHeader title="COST" meta={cost?.accounting_status ?? "unknown"} />
              <KeyValueGrid
                items={[
                  ["Coding", money(cost?.coding ?? null, cost?.currency ?? "USD")],
                  [
                    "Verification",
                    money(cost?.verification ?? null, cost?.currency ?? "USD"),
                  ],
                  ["Total", money(cost?.total ?? null, cost?.currency ?? "USD")],
                ]}
              />
            </Panel>
          </div>
          <Panel>
            <PanelHeader title="VILLANI RECOVERY" />
            <div className="v-panel__body">
              <ul className="console-list">
                {(value.recovery ?? []).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
              {value.failure && <RunFailureDetails failure={value.failure} />}
              {value.synchronization_failure && (
                <RunFailureDetails failure={value.synchronization_failure} />
              )}
            </div>
          </Panel>
          <Panel>
            <PanelHeader title="NEXT ACTION" />
            <div className="v-panel__body run-next-actions">
              {(value.next_actions ?? []).map((item) => (
                <div key={`${item.label}:${item.action}`}>
                  <strong>{publicLanguage(item.label)}</strong>
                  <code>{publicLanguage(item.action)}</code>
                </div>
              ))}
              <a className="v-button" href={`/console/runs/${value.run_id}/replay`}>
                Open replay
              </a>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}

function LegacyRunPage({ client }: { client: ConsoleClient }) {
  const environment = useConsoleEnvironment();
  const { value: options, error: optionsError } = useLoader<ConsoleRunOptions>(
    (signal) => client.runOptions(signal),
    [client],
  );
  const [repository, setRepository] = useState("");
  const [task, setTask] = useState("");
  const [successCriteria, setSuccessCriteria] = useState("");
  const [discovery, setDiscovery] = useState<ConsoleValidationDiscovery | null>(null);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  const [selectedSuggestion, setSelectedSuggestion] = useState("");
  const [manualValidation, setManualValidation] = useState("");
  const [validationConfirmed, setValidationConfirmed] = useState(false);
  const [deliveryMode, setDeliveryMode] = useState("suggest");
  const [maxCost, setMaxCost] = useState("");
  const [maxWallTime, setMaxWallTime] = useState("");
  const [maxAttempts, setMaxAttempts] = useState("3");
  const [policyPreset, setPolicyPreset] = useState("balanced");
  const [policySelection, setPolicySelection] = useState("configured");
  const [routingMode, setRoutingMode] = useState("observe");
  const [preview, setPreview] = useState<PolicyPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [requiresFileChanges, setRequiresFileChanges] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [submissionError, setSubmissionError] = useState<string | null>(null);
  const [submissionFailure, setSubmissionFailure] = useState<RunFailure | null>(null);
  const [runId, setRunId] = useState(
    () => new URLSearchParams(location.search).get("run") ?? "",
  );
  const [presentation, setPresentation] = useState<RunPresentation | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  useEffect(() => {
    if (!options) return;
    const requestedRepository = new URLSearchParams(location.search).get("repository");
    setRepository(
      (current) => current || requestedRepository || options.default_repository || "",
    );
    setDeliveryMode(options.defaults.delivery_mode);
    setPolicyPreset(options.defaults.policy_preset ?? "balanced");
    setPolicySelection(options.defaults.policy_selection);
    setRoutingMode(options.defaults.routing_mode);
    setMaxAttempts(String(options.defaults.max_attempts));
    setMaxCost(
      options.defaults.max_cost === null ? "" : String(options.defaults.max_cost),
    );
    setMaxWallTime(
      options.defaults.max_wall_time === null
        ? ""
        : String(options.defaults.max_wall_time),
    );
  }, [options]);

  useEffect(() => {
    setPreview(null);
    setPreviewError(null);
  }, [repository, task, successCriteria, policyPreset]);

  useEffect(() => {
    if (!repository) {
      setDiscovery(null);
      return;
    }
    const controller = new AbortController();
    setDiscoveryError(null);
    void client
      .discoverValidation(repository, controller.signal)
      .then((value) => {
        setDiscovery(value);
        setSelectedSuggestion(value.selected_suggestion_id ?? "");
        setValidationConfirmed(false);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setDiscoveryError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
  }, [client, repository]);

  useEffect(() => {
    if (!runId) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const load = () => {
      void client
        .runStatusLegacy(runId, controller.signal)
        .then((value) => {
          setPresentation(value);
          setStatusError(null);
          if (value.outcome === "RUNNING" && !controller.signal.aborted)
            timer = setTimeout(load, 750);
        })
        .catch((reason: unknown) => {
          if (!controller.signal.aborted) {
            setStatusError(reason instanceof Error ? reason.message : String(reason));
            timer = setTimeout(load, 1500);
          }
        });
    };
    load();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [client, runId]);

  const suggestion = discovery?.suggestions.find(
    (item) => item.suggestion_id === selectedSuggestion,
  );
  const exactValidation = manualValidation.trim() || suggestion?.display_command || "";
  const lowConfidence = !manualValidation.trim() && !!suggestion?.requires_confirmation;
  const repositoryStatus = options?.repositories.find(
    (item) => item.path === repository,
  );
  const formReady =
    environment.setup.valid &&
    !!task.trim() &&
    !!repository &&
    repositoryStatus?.dirty !== true &&
    !!exactValidation &&
    (!lowConfidence || validationConfirmed) &&
    !submitting;

  const previewReady =
    environment.setup.valid && !!task.trim() && !!repository && !previewing;

  const previewPolicy = async () => {
    if (!previewReady) return;
    setPreviewing(true);
    setPreviewError(null);
    try {
      setPreview(
        await client.previewPolicy({
          repository,
          task,
          success_criteria: successCriteria,
          preset: policyPreset,
        }),
      );
    } catch (reason) {
      setPreviewError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPreviewing(false);
    }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!formReady) return;
    setSubmitting(true);
    setSubmissionError(null);
    setSubmissionFailure(null);
    try {
      const result = await client.startRun({
        repository,
        task,
        success_criteria: successCriteria,
        validation_command: manualValidation.trim() || undefined,
        validation_argv: manualValidation.trim() ? undefined : suggestion?.argv,
        validation_confirmed: validationConfirmed,
        delivery_mode: deliveryMode,
        max_cost: maxCost,
        max_wall_time: maxWallTime,
        max_attempts: maxAttempts,
        policy_preset: policyPreset,
        policy_selection: policySelection,
        routing_mode: routingMode,
        requires_file_changes: requiresFileChanges,
      });
      if (result.failure) {
        setSubmissionFailure(result.failure);
      } else if (result.run_id) {
        setRunId(result.run_id);
        history.replaceState(
          null,
          "",
          `/console?run=${encodeURIComponent(result.run_id)}`,
        );
      }
    } catch (reason) {
      setSubmissionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSubmitting(false);
    }
  };

  const previewVerifier = preview?.selected_verifier_route.selected;
  const previewVerifierName = previewVerifier
    ? String(
        previewVerifier.route ?? previewVerifier.model ?? "Configured verification",
      )
    : "No verification route is available";
  const previewEligible =
    preview?.eligible_models
      .map((item) => String(item.backend_name ?? ""))
      .filter(Boolean)
      .join(", ") || "None";

  return (
    <ProductShell
      surface="new-task"
      title="New task"
      status={optionsError ? "failed" : "running"}
      statusText={optionsError ? "Villani service is unavailable" : undefined}
    >
      <div className="console-stack">
        <PageIntro title="What would you like Villani to change?">
          Choose a repository, describe the outcome, and review the recorded result
          before any change is applied.
        </PageIntro>
        {optionsError && (
          <>
            <ErrorState
              title="Villani Service is unavailable"
              detail={`${optionsError}. Run: villani service start`}
            />
            <Panel>
              <PanelHeader title="RECOVERY" />
              <div className="v-panel__body">
                <RunFailureDetails failure={serviceOfflineFailure} />
              </div>
            </Panel>
          </>
        )}
        {!options && !optionsError && <LoadingState title="Preparing run options" />}
        {options && (
          <TaskComposerShell
            title="New task"
            meta={environment.setup.valid ? undefined : "Setup required"}
          >
            <form className="run-form" onSubmit={submit} noValidate>
              <FormField
                className="run-form--wide"
                id="task-repository"
                label="Repository"
                required
                error={
                  repositoryStatus?.dirty
                    ? "Commit or stash existing changes before starting a task."
                    : repository && repositoryStatus && !repositoryStatus.valid
                      ? "Choose a valid Git repository."
                      : undefined
                }
              >
                <input
                  className="v-input"
                  list="villani-repositories"
                  value={repository}
                  onChange={(event) => setRepository(event.target.value)}
                  placeholder="Select a Git repository"
                  required
                />
              </FormField>
              <datalist id="villani-repositories">
                {options.repositories.map((item) => (
                  <option value={item.path} key={item.path}>
                    {item.name}
                  </option>
                ))}
              </datalist>
              <FormField
                className="run-form--wide"
                id="task-instruction"
                label="Task"
                required
              >
                <textarea
                  className="v-textarea run-textarea"
                  value={task}
                  onChange={(event) => setTask(event.target.value)}
                  placeholder="What should Villani change?"
                  required
                />
              </FormField>
              <FormField
                className="run-form--wide"
                id="task-success-criteria"
                label="Success criteria (optional)"
              >
                <textarea
                  className="v-textarea run-textarea run-textarea--small"
                  value={successCriteria}
                  onChange={(event) => setSuccessCriteria(event.target.value)}
                  placeholder="Observable conditions that must be true"
                />
              </FormField>
              <details className="run-advanced run-form--wide task-settings">
                <summary>Task settings</summary>
                <div className="run-form run-form--nested">
                  <fieldset className="run-validation run-form--wide">
                    <legend>Repository validation</legend>
                    {discoveryError && <p className="v-danger">{discoveryError}</p>}
                    {discovery?.failure && (
                      <RunFailureDetails failure={discovery.failure} />
                    )}
                    {discovery?.suggestions.length ? (
                      <label className="v-field">
                        <span className="v-field__label">Detected validation</span>
                        <select
                          className="v-select"
                          value={selectedSuggestion}
                          onChange={(event) => {
                            setSelectedSuggestion(event.target.value);
                            setValidationConfirmed(false);
                          }}
                        >
                          {discovery.suggestions.map((item) => (
                            <option value={item.suggestion_id} key={item.suggestion_id}>
                              {item.display_command} · {item.confidence_label}{" "}
                              confidence
                            </option>
                          ))}
                        </select>
                      </label>
                    ) : (
                      <p className="v-muted">No validation command was detected.</p>
                    )}
                    <label className="v-field">
                      <span className="v-field__label">Manual override (optional)</span>
                      <input
                        className="v-input"
                        value={manualValidation}
                        onChange={(event) => setManualValidation(event.target.value)}
                        placeholder="Example: python -m pytest -q"
                      />
                    </label>
                    <div className="run-command-preview">
                      <span>Exactly what will run</span>
                      <code>
                        {exactValidation || "Choose or enter a validation command"}
                      </code>
                      <small>
                        Detection is advisory. Verification begins only when this
                        confirmed command runs against the candidate change.
                      </small>
                    </div>
                    {lowConfidence && (
                      <label className="run-checkbox">
                        <input
                          type="checkbox"
                          checked={validationConfirmed}
                          onChange={(event) =>
                            setValidationConfirmed(event.target.checked)
                          }
                        />
                        Confirm this low-confidence command
                      </label>
                    )}
                  </fieldset>
                  <FilterSelect
                    label="Delivery mode"
                    value={deliveryMode}
                    onChange={setDeliveryMode}
                    options={options.delivery_modes.map((item) => ({
                      value: item.id,
                      label: item.label,
                    }))}
                  />
                  <div className="v-field run-approval-mode">
                    <span className="v-field__label">Approval mode</span>
                    <strong>
                      {deliveryMode === "approve"
                        ? "Explicit approval after selection"
                        : deliveryMode === "suggest"
                          ? "No change applied"
                          : "Configured delivery permission"}
                    </strong>
                    <span className="v-field__help">
                      Apply automatically, Branch, and Pull request fail closed when
                      permission is insufficient.
                    </span>
                  </div>
                  <FilterSelect
                    label="Policy preset"
                    value={policyPreset}
                    onChange={setPolicyPreset}
                    options={(options.policy_presets ?? options.policies).map(
                      (item) => ({
                        value: item.id,
                        label: item.label,
                      }),
                    )}
                  />
                  <label className="v-field">
                    <span className="v-field__label">Budget (optional, USD)</span>
                    <input
                      className="v-input"
                      type="number"
                      min="0"
                      step="0.01"
                      value={maxCost}
                      onChange={(event) => setMaxCost(event.target.value)}
                    />
                  </label>
                  <label className="v-field">
                    <span className="v-field__label">
                      Time limit (optional, seconds)
                    </span>
                    <input
                      className="v-input"
                      type="number"
                      min="0"
                      step="1"
                      value={maxWallTime}
                      onChange={(event) => setMaxWallTime(event.target.value)}
                    />
                  </label>
                  <details className="run-advanced run-form--wide">
                    <summary>Advanced policy selection</summary>
                    <div className="run-form run-form--nested">
                      <FilterSelect
                        label="Advanced policy source"
                        value={policySelection}
                        onChange={setPolicySelection}
                        options={(options.advanced_policies ?? []).map((item) => ({
                          value: item.id,
                          label: item.label,
                        }))}
                      />
                      <FilterSelect
                        label="Routing mode"
                        value={routingMode}
                        onChange={setRoutingMode}
                        options={options.routing_modes.map((item) => ({
                          value: item,
                          label: item,
                        }))}
                      />
                      <label className="v-field">
                        <span className="v-field__label">Maximum attempts</span>
                        <input
                          className="v-input"
                          type="number"
                          min="1"
                          step="1"
                          value={maxAttempts}
                          onChange={(event) => setMaxAttempts(event.target.value)}
                        />
                      </label>
                      <label className="run-checkbox">
                        <input
                          type="checkbox"
                          checked={requiresFileChanges}
                          onChange={(event) =>
                            setRequiresFileChanges(event.target.checked)
                          }
                        />
                        Require a file change
                      </label>
                    </div>
                  </details>
                </div>
              </details>
              {!!options.setup_issues.length && (
                <div className="v-notice run-form--wide">
                  {options.setup_issues.join(" ")}
                </div>
              )}
              <div className="run-policy-actions run-form--wide">
                <button
                  className="v-button v-button--secondary"
                  type="button"
                  disabled={!previewReady}
                  onClick={() => void previewPolicy()}
                >
                  {previewing ? "Assessing…" : "Preview task assessment"}
                </button>
                <span className="v-muted">
                  Assessment only; no coding attempt is started.
                </span>
              </div>
              {previewError && (
                <div className="v-notice v-danger run-form--wide">{previewError}</div>
              )}
              {preview && (
                <EvidenceDisclosure
                  className="run-form--wide"
                  summary="Advanced task assessment"
                >
                  <section className="policy-preview" aria-label="Task assessment">
                    <div className="policy-preview__header">
                      <strong>Task assessment</strong>
                      <StatusBadge
                        status="selected"
                        label={preview.policy_version.preset}
                      />
                    </div>
                    <KeyValueGrid
                      items={[
                        [
                          "Task assessment",
                          `${preview.raw_classification.difficulty} difficulty, ${preview.raw_classification.risk} risk (${Math.round(preview.raw_classification.confidence * 100)}%)`,
                        ],
                        [
                          "Adjusted task assessment",
                          `${preview.effective_classification.difficulty} difficulty, ${preview.effective_classification.risk} risk (${Math.round(preview.effective_classification.confidence * 100)}%)`,
                        ],
                        [
                          "Adjustments",
                          preview.adjustments.length
                            ? preview.adjustments
                                .map(
                                  (item) =>
                                    `${item.field}: ${item.before} -> ${item.after} (${item.rule_id})`,
                                )
                                .join("; ")
                            : "None",
                        ],
                        ["Available agent systems", previewEligible],
                        [
                          "Selected agent system",
                          `${preview.selected_coding_route.backend ?? "None"} / ${preview.selected_coding_route.model ?? "None"}`,
                        ],
                        [
                          "Selection basis",
                          preview.selected_coding_route.route_provenance?.basis ??
                            "Unknown",
                        ],
                        ["Verification", previewVerifierName],
                        [
                          "Estimated cost",
                          preview.estimated_cost.value === null
                            ? `Unknown (${preview.estimated_cost.status})`
                            : money(
                                preview.estimated_cost.value,
                                preview.estimated_cost.currency,
                              ),
                        ],
                        [
                          "Policy version",
                          `${preview.policy_version.public} / ${preview.policy_version.controller}`,
                        ],
                      ]}
                    />
                    {!!preview.excluded_models.length && (
                      <div className="policy-preview__section">
                        <strong>Excluded models</strong>
                        <ul>
                          {preview.excluded_models.map((item, index) => (
                            <li
                              key={`${String(item.backend_name ?? "model")}-${index}`}
                            >
                              {String(item.backend_name ?? "Unknown model")}:{" "}
                              {Array.isArray(item.reasons)
                                ? item.reasons.join("; ")
                                : "Not available under this policy"}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    <div className="policy-preview__section">
                      <strong>Uncertainty</strong>
                      <ul>
                        {preview.uncertainty.map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  </section>
                </EvidenceDisclosure>
              )}
              <button
                className="v-button run-submit"
                type="submit"
                disabled={!formReady}
              >
                {submitting ? "Starting…" : "Start task"}
              </button>
            </form>
          </TaskComposerShell>
        )}
        {submissionError && (
          <ErrorState
            title="Run could not be submitted"
            detail={`${submissionError}. If the service is offline, run: villani service start`}
          />
        )}
        {submissionFailure && (
          <Panel>
            <PanelHeader title="FAILED" />
            <div className="v-panel__body">
              <RunFailureDetails failure={submissionFailure} />
            </div>
          </Panel>
        )}
        {statusError && (
          <ErrorState
            title="Live status is temporarily unavailable"
            detail={statusError}
          />
        )}
        {presentation && (
          <LegacyRunResult
            value={presentation}
            client={client}
            onUpdate={setPresentation}
          />
        )}
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
                header: "Proved acceptable",
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
                header: "Verification",
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
                header: "Applied change",
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

function ModelsPage({ client }: { client: ConsoleClient }) {
  const { value, error, reload } = useLoader(
    (signal) => client.models(signal),
    [client],
  );
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [backendName, setBackendName] = useState("");
  const [modelName, setModelName] = useState("");
  const [provider, setProvider] = useState("local");
  const [endpoint, setEndpoint] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [makeDefault, setMakeDefault] = useState(false);
  const [toolSupport, setToolSupport] = useState("unknown");
  const [contextWindow, setContextWindow] = useState("");
  const [manualScore, setManualScore] = useState("");
  const [billingMode, setBillingMode] = useState("unknown");
  const [inputPrice, setInputPrice] = useState("");
  const [outputPrice, setOutputPrice] = useState("");
  const [fixedPrice, setFixedPrice] = useState("");

  const act = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    setActionError(null);
    setNotice(null);
    try {
      await action();
      setNotice(label);
      reload();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy("");
    }
  };

  const add = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await act(`Added ${backendName}`, () =>
      client.addModel({
        backend_name: backendName,
        model: modelName,
        provider,
        endpoint: endpoint || undefined,
        display_name: displayName || undefined,
        roles: ["coding", "classification"],
        make_default: makeDefault,
        tool_support:
          toolSupport === "unknown" ? undefined : toolSupport === "supported",
        context_window: contextWindow || undefined,
        manual_capability_score: manualScore || undefined,
        billing_mode: billingMode,
        input_cost_per_million: inputPrice || undefined,
        output_cost_per_million: outputPrice || undefined,
        fixed_cost_per_attempt: fixedPrice || undefined,
      }),
    );
  };

  const configureDetected = (model: NonNullable<typeof value>["models"][number]) => {
    const generated = model.model
      .toLowerCase()
      .replace(/[^a-z0-9_.-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 64);
    setBackendName(generated || "local-model");
    setModelName(model.model);
    setDisplayName(model.display_name);
    setEndpoint(model.endpoint ?? "");
    setProvider(
      model.endpoint?.includes("127.0.0.1") || model.endpoint?.includes("localhost")
        ? "local"
        : "openai-compatible",
    );
  };

  return (
    <ProductShell surface="models" title="MODELS">
      <div className="console-stack">
        <PageIntro title="Models">
          Detect, configure, test, and choose models without entering capability scores.
        </PageIntro>
        {error && <ErrorState title="Models are unavailable" detail={error} />}
        {!value && !error && <LoadingState title="Loading models" />}
        <Panel>
          <PanelHeader title="MODEL OPERATIONS" />
          <div className="model-actions v-panel__body">
            <button
              className="v-button"
              type="button"
              disabled={!!busy}
              onClick={() =>
                void act("Detection complete", () => client.detectModels())
              }
            >
              Detect models
            </button>
            <button
              className="v-button v-button--secondary"
              type="button"
              disabled={!!busy}
              onClick={() =>
                void act("Model tests complete", () => client.testModels())
              }
            >
              Test configured models
            </button>
            <span className="v-muted">
              Detection and testing inspect model-list endpoints and use zero model
              tokens.
            </span>
          </div>
          {notice && <div className="v-notice v-panel__body">{notice}</div>}
          {actionError && (
            <div className="v-notice v-danger v-panel__body">{actionError}</div>
          )}
        </Panel>
        {value && (
          <Panel>
            <PanelHeader
              title="MODEL INVENTORY"
              meta={`${value.models.length} · default: ${value.bootstrap_default ?? "not selected"}`}
            />
            <DataTable
              rows={value.models}
              getRowKey={(model) =>
                `${model.backend_name ?? model.endpoint}:${model.provider}:${model.id}`
              }
              empty="No models are configured or detected. Select Detect models."
              columns={[
                {
                  key: "display_name",
                  header: "Model",
                  render: (model) => (
                    <div>
                      <strong>{model.display_name}</strong>
                      <div className="v-muted">
                        {model.backend_name ?? "Detected only"}
                      </div>
                    </div>
                  ),
                },
                { key: "provider", header: "Provider" },
                {
                  key: "endpoint",
                  header: "Endpoint",
                  render: (model) => model.endpoint ?? "Provider default",
                },
                {
                  key: "availability",
                  header: "Availability",
                  render: (model) => model.availability,
                },
                { key: "tool_support", header: "Tools" },
                {
                  key: "context_window",
                  header: "Context",
                  render: (model) => model.context_window ?? "Unknown",
                },
                {
                  key: "configured_roles",
                  header: "Roles",
                  render: (model) =>
                    model.configured_roles.join(", ") || "Not configured",
                },
                { key: "pricing_status", header: "Pricing" },
                {
                  key: "observed_task_count",
                  header: "Observed",
                  render: (model) => `${model.observed_task_count} tasks`,
                },
                {
                  key: "observed_success_rate",
                  header: "Success",
                  render: (model) =>
                    model.observed_success_rate === null
                      ? "Unknown"
                      : `${(model.observed_success_rate * 100).toFixed(1)}%`,
                },
                {
                  key: "observed_cost_per_accepted_task",
                  header: "Cost / accepted",
                  render: (model) =>
                    money(model.observed_cost_per_accepted_task, model.currency),
                },
                {
                  key: "capability_status",
                  header: "Capability",
                  render: (model) => (
                    <div>
                      <StatusBadge status={model.capability_status.toLowerCase()} />
                      {model.manual_override && (
                        <div className="v-muted">Advanced manual override</div>
                      )}
                    </div>
                  ),
                },
                {
                  key: "last_tested_at",
                  header: "Last tested",
                  render: (model) => date(model.last_tested_at),
                },
                {
                  key: "actions",
                  header: "Actions",
                  render: (model) => (
                    <div className="model-row-actions">
                      {model.configured && model.backend_name ? (
                        <>
                          <button
                            className="v-button v-button--small"
                            type="button"
                            disabled={!!busy}
                            onClick={() =>
                              void act(`Tested ${model.backend_name}`, () =>
                                client.testModels(model.backend_name ?? undefined),
                              )
                            }
                          >
                            Test
                          </button>
                          {!model.bootstrap_default && (
                            <button
                              className="v-button v-button--small"
                              type="button"
                              disabled={!!busy}
                              onClick={() =>
                                void act(`Default is ${model.backend_name}`, () =>
                                  client.setDefaultModel(model.backend_name ?? ""),
                                )
                              }
                            >
                              Make default
                            </button>
                          )}
                          <button
                            className="v-button v-button--small"
                            type="button"
                            disabled={!!busy}
                            onClick={() => {
                              if (
                                model.backend_name &&
                                window.confirm(
                                  `Remove ${model.backend_name}? Historical evidence will be retained.`,
                                )
                              )
                                void act(`Removed ${model.backend_name}`, () =>
                                  client.removeModel(model.backend_name ?? ""),
                                );
                            }}
                          >
                            Remove
                          </button>
                        </>
                      ) : (
                        <button
                          className="v-button v-button--small"
                          type="button"
                          onClick={() => configureDetected(model)}
                        >
                          Configure
                        </button>
                      )}
                    </div>
                  ),
                },
              ]}
            />
          </Panel>
        )}
        <Panel>
          <PanelHeader title="ADD MODEL" meta="New models begin UNRATED" />
          <form className="run-form v-panel__body" onSubmit={add}>
            <label className="v-field">
              <span className="v-field__label">Configuration name</span>
              <input
                className="v-input"
                value={backendName}
                onChange={(event) => setBackendName(event.target.value)}
                required
              />
            </label>
            <label className="v-field">
              <span className="v-field__label">Model identifier</span>
              <input
                className="v-input"
                value={modelName}
                onChange={(event) => setModelName(event.target.value)}
                required
              />
            </label>
            <FilterSelect
              label="Provider"
              value={provider}
              onChange={setProvider}
              options={[
                { value: "local", label: "Local" },
                { value: "openai-compatible", label: "OpenAI compatible" },
                { value: "openai", label: "OpenAI" },
                { value: "anthropic", label: "Anthropic" },
                { value: "villani-code", label: "Villani Code" },
                { value: "custom", label: "Custom" },
              ]}
            />
            <label className="v-field">
              <span className="v-field__label">Endpoint</span>
              <input
                className="v-input"
                type="url"
                value={endpoint}
                onChange={(event) => setEndpoint(event.target.value)}
                placeholder="http://127.0.0.1:1234/v1"
              />
            </label>
            <label className="v-field">
              <span className="v-field__label">Display name (optional)</span>
              <input
                className="v-input"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
            <label className="run-checkbox">
              <input
                type="checkbox"
                checked={makeDefault}
                onChange={(event) => setMakeDefault(event.target.checked)}
              />
              Use as bootstrap default
            </label>
            <details className="run-advanced run-form--wide">
              <summary>Advanced metadata and manual overrides</summary>
              <div className="run-form run-form--nested">
                <FilterSelect
                  label="Tool support"
                  value={toolSupport}
                  onChange={setToolSupport}
                  options={[
                    { value: "unknown", label: "Unknown" },
                    { value: "supported", label: "Supported" },
                    { value: "unsupported", label: "Unsupported" },
                  ]}
                />
                <label className="v-field">
                  <span className="v-field__label">Context window</span>
                  <input
                    className="v-input"
                    type="number"
                    min="1"
                    value={contextWindow}
                    onChange={(event) => setContextWindow(event.target.value)}
                  />
                </label>
                <label className="v-field">
                  <span className="v-field__label">
                    Manual capability score (Advanced override)
                  </span>
                  <input
                    className="v-input"
                    type="number"
                    min="0"
                    max="100"
                    value={manualScore}
                    onChange={(event) => setManualScore(event.target.value)}
                  />
                  <span className="v-field__help">
                    This is labelled manual and is never presented as observed
                    capability.
                  </span>
                </label>
                <FilterSelect
                  label="Pricing"
                  value={billingMode}
                  onChange={setBillingMode}
                  options={[
                    { value: "unknown", label: "Unknown" },
                    { value: "token", label: "Token pricing" },
                    { value: "fixed", label: "Fixed per attempt" },
                  ]}
                />
                {billingMode === "token" && (
                  <>
                    <label className="v-field">
                      <span className="v-field__label">Input / million (USD)</span>
                      <input
                        className="v-input"
                        type="number"
                        min="0"
                        step="0.0001"
                        value={inputPrice}
                        onChange={(event) => setInputPrice(event.target.value)}
                      />
                    </label>
                    <label className="v-field">
                      <span className="v-field__label">Output / million (USD)</span>
                      <input
                        className="v-input"
                        type="number"
                        min="0"
                        step="0.0001"
                        value={outputPrice}
                        onChange={(event) => setOutputPrice(event.target.value)}
                      />
                    </label>
                  </>
                )}
                {billingMode === "fixed" && (
                  <label className="v-field">
                    <span className="v-field__label">Fixed / attempt (USD)</span>
                    <input
                      className="v-input"
                      type="number"
                      min="0"
                      step="0.0001"
                      value={fixedPrice}
                      onChange={(event) => setFixedPrice(event.target.value)}
                    />
                  </label>
                )}
              </div>
            </details>
            <button className="v-button run-submit" type="submit" disabled={!!busy}>
              Add model
            </button>
          </form>
        </Panel>
      </div>
    </ProductShell>
  );
}

function PoliciesPage({ client }: { client: ConsoleClient }) {
  const { value, error, reload } = useLoader(
    (signal) => client.policies(signal),
    [client],
  );
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [simulationPreset, setSimulationPreset] = useState("balanced");
  const [simulation, setSimulation] = useState<Awaited<
    ReturnType<ConsoleClient["simulatePolicy"]>
  > | null>(null);

  useEffect(() => {
    if (value) setSimulationPreset(value.active_preset);
  }, [value]);

  const select = async (preset: string) => {
    setBusy(preset);
    setActionError(null);
    try {
      await client.selectPolicy(preset);
      reload();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy("");
    }
  };

  const simulate = async () => {
    setBusy("simulate");
    setActionError(null);
    try {
      setSimulation(await client.simulatePolicy(simulationPreset));
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy("");
    }
  };
  return (
    <ProductShell surface="policies" title="POLICIES">
      <div className="console-stack">
        <PageIntro title="Policies">
          Simple presets and the active local policy.
        </PageIntro>
        {error && <ErrorState title="Policies are unavailable" detail={error} />}
        {actionError && <div className="v-notice v-danger">{actionError}</div>}
        {!value && !error && <LoadingState title="Loading policies" />}
        {value && (
          <Panel>
            <PanelHeader
              title="POLICY PRESETS"
              meta={`Active: ${value.active_preset}`}
            />
            <div className="policy-presets v-panel__body">
              {value.presets.map((preset) => (
                <article key={preset.id} className="policy-preset">
                  <div>
                    <h2>{preset.label}</h2>
                    <p>{preset.description}</p>
                    {preset.advanced && <small>Exposes Advanced controls.</small>}
                  </div>
                  <div className="policy-preset__actions">
                    <StatusBadge
                      status={preset.active ? "selected" : "unknown"}
                      label={preset.active ? "ACTIVE" : "AVAILABLE"}
                    />
                    {!preset.active && (
                      <button
                        className="v-button v-button--small"
                        type="button"
                        disabled={!!busy}
                        onClick={() => void select(preset.id)}
                      >
                        Select
                      </button>
                    )}
                  </div>
                </article>
              ))}
            </div>
          </Panel>
        )}
        {value && (
          <Panel>
            <PanelHeader title="HISTORICAL SIMULATION" meta="Read-only" />
            <div className="policy-simulation v-panel__body">
              <FilterSelect
                label="Evaluate preset"
                value={simulationPreset}
                onChange={setSimulationPreset}
                options={value.presets.map((preset) => ({
                  value: preset.id,
                  label: preset.label,
                }))}
              />
              <button
                className="v-button"
                type="button"
                disabled={!!busy}
                onClick={() => void simulate()}
              >
                Evaluate recorded runs
              </button>
              <p className="v-muted">
                Simulation never changes the live policy and cannot establish causal
                savings.
              </p>
              {simulation && (
                <div className="policy-simulation__result">
                  <KeyValueGrid
                    items={[
                      ["Tasks evaluated", simulation.tasks_evaluated],
                      ["Tasks affected", simulation.tasks_affected],
                      ["Route changes", simulation.route_changes.length],
                      [
                        "Estimated cost difference",
                        simulation.estimated_cost_differences
                          .simulated_minus_recorded_total === null
                          ? `Unknown (${simulation.estimated_cost_differences.status})`
                          : money(
                              simulation.estimated_cost_differences
                                .simulated_minus_recorded_total,
                            ),
                      ],
                    ]}
                  />
                  <div>
                    <strong>Evidence limitations</strong>
                    <ul>
                      {simulation.outcome_evidence_limitations.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                    <p>
                      Unsupported counterfactual claims:{" "}
                      {simulation.unsupported_counterfactual_claims.join(", ")}.
                    </p>
                  </div>
                </div>
              )}
            </div>
          </Panel>
        )}
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
    <ProductShell surface="new-task" title="Not found" status="failed">
      <ErrorState
        title="This Console link is not supported"
        detail="Open Activity to find a task or imported session."
      >
        <a href="/console/activity">Open Activity</a>
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
  if (path === "/console" || path === "/console/" || path === "/console/run")
    return <SingleTaskPage client={client} />;
  if (path === "/console/activity" || path === "/console/history")
    return <ActivityPage client={client} />;
  if (path === "/console/agents") return <AgentsPage client={client} />;
  if (path === "/console/onboarding") return <OnboardingPage client={client} />;
  if (path === "/console/replay") return <ReplayLanding client={client} />;
  if (path === "/console/models") return <ModelsPage client={client} />;
  if (path === "/console/policies") return <PoliciesPage client={client} />;
  if (path === "/console/settings" || path === "/console/advanced")
    return <SettingsPage client={client} />;
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
        <ProductShell surface="new-task" title="Villani">
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
