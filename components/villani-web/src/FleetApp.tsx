import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { maskSensitive } from "@villani/run-model";
import { RunClient } from "./api";
import { ProductShell } from "./ProductShell";

type Filters = Record<string, string | number | string[] | undefined>;
type RunRow = Record<string, unknown>;
const value = (row: RunRow, key: string) =>
  row[key] == null ? "Unknown" : String(row[key]);
const numberValue = (input: FormDataEntryValue | null) =>
  input ? Number(input) : undefined;
const formatRate = (metric: any) =>
  metric?.value == null ? "Unknown" : `${(metric.value * 100).toFixed(1)}%`;
const formatMoney = (metric: any) =>
  metric?.value == null ? "Unknown" : `USD ${metric.value.toFixed(3)}`;

function MetricCard({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <article className="fleet-metric" tabIndex={0}>
      <p className="kicker">{label}</p>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

export default function FleetApp() {
  const client = useMemo(
    () =>
      new RunClient(
        import.meta.env.VITE_API_BASE_URL ?? "",
        sessionStorage.getItem("villani.token") ?? import.meta.env.VITE_API_TOKEN ?? "",
      ),
    [],
  );
  const [filters, setFilters] = useState<Filters>({});
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<(string | null)[]>([]);
  const [metrics, setMetrics] = useState<Record<string, any>>({});
  const [comparisons, setComparisons] = useState<Record<string, any>>({});
  const [groupBy, setGroupBy] = useState("model");
  const [definitions, setDefinitions] = useState<
    Record<string, Record<string, string>>
  >({});
  const [views, setViews] = useState<Record<string, any>[]>([]);
  const [alerts, setAlerts] = useState<Record<string, any>[]>([]);
  const [alertEvents, setAlertEvents] = useState<Record<string, any>[]>([]);
  const [queue, setQueue] = useState<Record<string, any>[]>([]);
  const [clusters, setClusters] = useState<Record<string, any>[]>([]);
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);

  const load = useCallback(
    async (targetCursor: string | null, activeFilters: Filters, grouping = groupBy) => {
      setLoading(true);
      setError(undefined);
      try {
        const [page, summary] = await Promise.all([
          client.fleetSearch(activeFilters, targetCursor, 100),
          client.fleetMetrics(activeFilters, grouping),
        ]);
        setRuns(page.runs);
        setNextCursor(page.next_cursor);
        setMetrics(summary.metrics);
        setComparisons(summary.comparisons);
      } catch (reason) {
        setError(
          reason instanceof Error ? reason.message : "Unable to load fleet data",
        );
      } finally {
        setLoading(false);
      }
    },
    [client, groupBy],
  );

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      client.metricDefinitions(controller.signal),
      client.savedViews(controller.signal),
      client.alertRules(controller.signal),
      client.reviewQueue(controller.signal),
      client.failureClusters(controller.signal),
    ])
      .then(([defs, saved, ruleData, review, clusterData]) => {
        setDefinitions(defs.metrics);
        setViews(saved.views);
        setAlerts(ruleData.rules);
        setAlertEvents(ruleData.events);
        setQueue(review.items);
        setClusters(clusterData.clusters);
      })
      .catch(() => undefined);
    void load(null, {});
    return () => controller.abort();
  }, [client, load]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const next: Filters = {};
    for (const key of [
      "project_id",
      "repository_id",
      "agent",
      "model",
      "provider",
      "policy_version",
      "task_category",
      "state",
      "verification",
      "failure_category",
      "started_after",
      "started_before",
    ]) {
      const item = String(data.get(key) ?? "").trim();
      if (item) next[key] = item;
    }
    for (const key of [
      "min_cost_usd",
      "max_cost_usd",
      "min_tokens",
      "max_tokens",
      "min_duration_ms",
      "max_duration_ms",
    ]) {
      const item = numberValue(data.get(key));
      if (item !== undefined && !Number.isNaN(item)) next[key] = item;
    }
    const tags = String(data.get("tags") ?? "")
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean);
    if (tags.length) next.tags = tags;
    setFilters(next);
    setCursor(null);
    setHistory([]);
    void load(null, next);
  }
  function nextPage() {
    if (!nextCursor) return;
    setHistory((items) => [...items, cursor]);
    setCursor(nextCursor);
    void load(nextCursor, filters);
  }
  function previousPage() {
    const prior = history.at(-1) ?? null;
    setHistory((items) => items.slice(0, -1));
    setCursor(prior);
    void load(prior, filters);
  }
  async function saveView() {
    const name = window.prompt("Saved view name");
    if (!name) return;
    await client.createSavedView({
      name,
      visibility: "private",
      filter_ast: filters,
      columns: [
        "state",
        "repository",
        "agent",
        "model",
        "verification",
        "cost",
        "tokens",
        "duration",
      ],
      sort: [{ field: "last_observed_at", direction: "desc" }],
      version: 1,
    });
    setViews((await client.savedViews()).views);
  }
  async function addAlert() {
    const name = window.prompt("Alert name", "Fleet spend guard");
    if (!name) return;
    await client.createAlertRule({
      name,
      rule_type: "spend",
      filter_ast: filters,
      threshold: { operator: "gte", value: 10 },
      cooldown_seconds: 300,
      destination: { type: "test_webhook" },
      enabled: true,
    });
    setAlerts((await client.alertRules()).rules);
  }
  async function exportData(format: "csv" | "json") {
    const blob = await client.fleetExport(filters, format);
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `villani-fleet.${format}`;
    link.click();
    URL.revokeObjectURL(url);
  }
  const known = metrics.verified_success_rate ?? {};
  return (
    <ProductShell
      surface="fleet"
      title="FLEET CONTROL ROOM"
      detail={loading ? "QUERY / ACTIVE" : `${runs.length} ROWS / PAGE`}
      status={error ? "failed" : loading ? "running" : "succeeded"}
      statusText={
        error ? "API / ERROR" : loading ? "FLEET / LOADING" : "FLEET / SYNCHRONIZED"
      }
    >
      <div className="fleet-page">
        <header className="fleet-header">
          <p className="kicker">Structured fleet observability</p>
          <div className="title-row">
            <div>
              <h1>Fleet control room</h1>
              <p>
                Verified outcomes, spend, latency, policy behavior, and review signals.
              </p>
            </div>
            <div className="actions">
              <button onClick={() => void saveView()}>Save view</button>
              <button className="secondary" onClick={() => void exportData("csv")}>
                Export CSV
              </button>
              <button className="secondary" onClick={() => void exportData("json")}>
                Export JSON
              </button>
            </div>
          </div>
        </header>
        {error && (
          <p role="alert" className="error-box">
            {error}
          </p>
        )}
        <section id="overview" aria-labelledby="overview-title">
          <div className="section-title">
            <div>
              <p className="kicker">Exact denominator rules</p>
              <h2 id="overview-title">Fleet overview</h2>
            </div>
            <span>
              {metrics.run_count ?? 0} filtered runs ·{" "}
              {known.unknown_outcome_count ?? 0} unknown outcomes
            </span>
          </div>
          <div className="metric-grid">
            <MetricCard
              label="Verified success"
              value={formatRate(metrics.verified_success_rate)}
              detail={`${known.numerator ?? 0} / ${known.denominator ?? 0}; unknown shown separately`}
            />
            <MetricCard
              label="Cost / accepted"
              value={formatMoney(metrics.cost_per_accepted_change)}
              detail={`${metrics.cost_per_accepted_change?.unknown_cost_count ?? 0} accepted changes have unknown cost`}
            />
            <MetricCard
              label="Duration"
              value={
                metrics.duration_ms?.average == null
                  ? "Unknown"
                  : `${Math.round(metrics.duration_ms.average)} ms`
              }
              detail={`${metrics.duration_ms?.unknown_count ?? 0} unknown`}
            />
            <MetricCard
              label="Queue time"
              value={
                metrics.queue_time_ms?.average == null
                  ? "Unknown"
                  : `${Math.round(metrics.queue_time_ms.average)} ms`
              }
              detail={`${metrics.queue_time_ms?.unknown_count ?? 0} unknown`}
            />
            <MetricCard
              label="Attempts / escalations"
              value={`${metrics.attempts ?? 0} / ${metrics.escalations ?? 0}`}
              detail="Filtered run total"
            />
            <MetricCard
              label="Rejected spend"
              value={`USD ${(metrics.rejected_wasted_spend_usd?.known_total ?? 0).toFixed(3)}`}
              detail={`${metrics.rejected_wasted_spend_usd?.unknown_count ?? 0} unknown`}
            />
          </div>
          <details>
            <summary>Metric definitions</summary>
            <div className="definition-grid">
              {Object.entries(definitions).map(([name, definition]) => (
                <article key={name}>
                  <h3>{name.replaceAll("_", " ")}</h3>
                  <p>
                    <strong>Numerator:</strong> {definition.numerator}
                  </p>
                  <p>
                    <strong>Denominator:</strong> {definition.denominator}
                  </p>
                  <p>
                    <strong>Unknowns:</strong> {definition.unknown_rule}
                  </p>
                </article>
              ))}
            </div>
          </details>
        </section>
        <section id="runs" aria-labelledby="runs-title">
          <div className="section-title">
            <div>
              <p className="kicker">Cursor-paginated search</p>
              <h2 id="runs-title">Runs</h2>
            </div>
            <span aria-live="polite">
              {loading ? "Loading…" : `${runs.length} rows on this page`}
            </span>
          </div>
          <form className="filter-grid" onSubmit={submit}>
            {[
              "repository_id",
              "agent",
              "model",
              "provider",
              "policy_version",
              "task_category",
              "state",
              "verification",
              "failure_category",
              "tags",
            ].map((name) => (
              <label key={name}>
                {name.replaceAll("_", " ")}
                <input name={name} />
              </label>
            ))}
            {[
              "min_cost_usd",
              "max_cost_usd",
              "min_tokens",
              "max_tokens",
              "min_duration_ms",
              "max_duration_ms",
            ].map((name) => (
              <label key={name}>
                {name.replaceAll("_", " ")}
                <input name={name} type="number" min="0" />
              </label>
            ))}
            <label>
              started after
              <input name="started_after" type="datetime-local" />
            </label>
            <label>
              started before
              <input name="started_before" type="datetime-local" />
            </label>
            <button type="submit">Apply filters</button>
          </form>
          <div className="saved-views" aria-label="Saved views">
            {views.map((view) => (
              <button
                className="secondary"
                key={String(view.id)}
                onClick={() => {
                  const next = view.filter_ast as Filters;
                  setFilters(next);
                  setCursor(null);
                  setHistory([]);
                  void load(null, next);
                }}
              >
                {String(view.name)} · v{String(view.version)}
              </button>
            ))}
          </div>
          <div className="table-scroll" tabIndex={0}>
            <table>
              <caption className="sr-only">Cursor-paginated fleet runs</caption>
              <thead>
                <tr>
                  <th>Run</th>
                  <th>State</th>
                  <th>Repository</th>
                  <th>Agent / model</th>
                  <th>Verification</th>
                  <th>Cost</th>
                  <th>Tokens</th>
                  <th>Duration</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={value(run, "id")}>
                    <td>
                      <a href={`/runs/${encodeURIComponent(value(run, "id"))}`}>
                        {value(run, "id")}
                      </a>
                    </td>
                    <td>
                      <span className="status">{value(run, "state")}</span>
                    </td>
                    <td>{value(run, "repository_id")}</td>
                    <td>
                      {value(run, "agent")} / {value(run, "model")}
                    </td>
                    <td>{value(run, "verification")}</td>
                    <td>
                      {run.cost_usd == null
                        ? "Unknown"
                        : `USD ${Number(run.cost_usd).toFixed(3)}`}
                    </td>
                    <td>{value(run, "total_tokens")}</td>
                    <td>
                      {run.duration_ms == null
                        ? "Unknown"
                        : `${value(run, "duration_ms")} ms`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pagination">
            <button
              className="secondary"
              disabled={!history.length}
              onClick={previousPage}
            >
              Previous page
            </button>
            <button disabled={!nextCursor} onClick={nextPage}>
              Next page
            </button>
          </div>
        </section>
        <section id="comparisons" aria-labelledby="comparison-title">
          <div className="section-title">
            <div>
              <p className="kicker">Like-for-like groups</p>
              <h2 id="comparison-title">Comparisons</h2>
            </div>
            <label>
              Group by{" "}
              <select
                value={groupBy}
                onChange={(event) => {
                  setGroupBy(event.target.value);
                  void load(cursor, filters, event.target.value);
                }}
              >
                <option value="model">Model</option>
                <option value="agent">Agent</option>
                <option value="provider">Provider</option>
                <option value="policy_version">Policy</option>
              </select>
            </label>
          </div>
          <div className="candidate-grid">
            {Object.entries(comparisons).map(([name, result]) => (
              <article className="candidate" key={name}>
                <h3>{name}</h3>
                <p>Success {formatRate(result.verified_success_rate)}</p>
                <p>Cost / accepted {formatMoney(result.cost_per_accepted_change)}</p>
                <p>
                  {result.attempts} attempts · {result.escalations} escalations
                </p>
              </article>
            ))}
          </div>
        </section>
        <section id="alerts" aria-labelledby="alerts-title">
          <div className="section-title">
            <div>
              <p className="kicker">Outbox-evaluated · test destinations only</p>
              <h2 id="alerts-title">Alerts</h2>
            </div>
            <button onClick={() => void addAlert()}>Add test alert</button>
          </div>
          <div className="split">
            <div>
              <h3>Rules</h3>
              <ul>
                {alerts.map((rule) => (
                  <li key={String(rule.id)}>
                    {String(rule.name)} · {String(rule.rule_type)} ·{" "}
                    {rule.enabled ? "enabled" : "disabled"}
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h3>Events</h3>
              <ul>
                {alertEvents.map((event) => (
                  <li key={String(event.id)}>
                    <span className="status">{String(event.event_type)}</span>{" "}
                    {String(event.rule_id)} · not sent
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>
        <section id="review" aria-labelledby="review-title">
          <div className="section-title">
            <div>
              <p className="kicker">Human feedback</p>
              <h2 id="review-title">Review queues</h2>
            </div>
            <span>{queue.length} items</span>
          </div>
          <ul>
            {queue.map((item) => (
              <li key={String(item.id)}>
                <a href={`/runs/${encodeURIComponent(String(item.run_id))}`}>
                  {String(item.run_id)}
                </a>{" "}
                · {String(item.queue)} · priority {String(item.priority)} ·{" "}
                {String(item.reason)}
              </li>
            ))}
          </ul>
        </section>
        <section id="clusters" aria-labelledby="clusters-title">
          <div className="section-title">
            <div>
              <p className="kicker">Deterministic signatures</p>
              <h2 id="clusters-title">Recurring failures</h2>
            </div>
            <span>{clusters.length} clusters</span>
          </div>
          <div className="candidate-grid">
            {clusters.map((cluster) => (
              <article className="candidate" key={String(cluster.signature)}>
                <h3>{String(cluster.label)}</h3>
                <p>
                  {String(cluster.occurrence_count)} occurrences ·{" "}
                  {String(cluster.failure_category)}
                </p>
                <p>
                  Advisory label:{" "}
                  {cluster.advisory_label
                    ? `${String(cluster.advisory_label)} (${String(cluster.advisory_label_version)})`
                    : "None"}
                </p>
                <small className="digest">{String(cluster.signature)}</small>
              </article>
            ))}
          </div>
        </section>
        <footer>
          <pre>{JSON.stringify(maskSensitive({ filters }), null, 2)}</pre>
        </footer>
      </div>
    </ProductShell>
  );
}
