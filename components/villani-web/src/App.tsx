import { useMemo, useState } from "react";
import {
  artifactMayRender,
  deriveRun,
  maskSensitive,
  type ArtifactDescriptor,
  type DerivedRun,
  type RunDetail,
  type RunEvent,
  type RunSpan,
} from "@villani/run-model";
import { RunClient } from "./api";
import { downloadStaticExport } from "./staticExport";
import { useRun } from "./useRun";

const fmtTime = (value?: string | null) =>
  value ? new Date(value).toLocaleString() : "Not captured";
const fmtDuration = (start: string, end: string) => {
  const ms = Math.max(0, new Date(end).getTime() - new Date(start).getTime());
  return ms < 60_000
    ? `${(ms / 1000).toFixed(1)}s`
    : `${Math.floor(ms / 60_000)}m ${Math.floor((ms % 60_000) / 1000)}s`;
};
const fmtMoney = (value: number | null | undefined) =>
  value == null ? "Unknown" : `USD ${value.toFixed(4)}`;
const fmtTokens = (value: number | null | undefined) =>
  value == null ? "Unknown" : value.toLocaleString();
const nameOf = (event: RunEvent) =>
  event.name ?? event.title ?? event.type ?? "Unknown event";
const tone = (status: string) =>
  /fail|error|cancel/.test(status.toLowerCase())
    ? "error"
    : /run|start|queue|pending/.test(status.toLowerCase())
      ? "info"
      : /reject|exhaust|warn/.test(status.toLowerCase())
        ? "warning"
        : "success";

function Header({
  detail,
  derived,
  connection,
  onExport,
}: {
  detail: RunDetail;
  derived: DerivedRun;
  connection: string;
  onExport: () => Promise<void>;
}) {
  const aggregate = derived.aggregate;
  const totalCost =
    aggregate?.totalCostUsd ??
    derived.metrics.reduce<number | null>(
      (sum, row) => (row.costUsd == null ? sum : (sum ?? 0) + row.costUsd),
      null,
    );
  const tokens =
    aggregate?.totalTokens ??
    derived.metrics.reduce(
      (sum, row) => sum + (row.inputTokens ?? 0) + (row.outputTokens ?? 0),
      0,
    );
  return (
    <header className="run-header">
      <div className="eyebrow">
        <span className={`status ${derived.status.tone}`}>
          <span aria-hidden="true">●</span> {derived.status.label}
        </span>
        <span className={`connection ${connection}`} aria-live="polite">
          {connection}
        </span>
      </div>
      <div className="title-row">
        <div>
          <p className="kicker">Individual run · {detail.id}</p>
          <h1>{derived.task}</h1>
        </div>
        <button
          onClick={() => void onExport()}
          aria-label="Export this run for offline viewing"
        >
          Export offline
        </button>
      </div>
      <dl className="run-facts">
        <div>
          <dt>Repository</dt>
          <dd>{derived.repository}</dd>
        </div>
        <div>
          <dt>Policy</dt>
          <dd>{derived.policy}</dd>
        </div>
        <div>
          <dt>Agent / model</dt>
          <dd>
            {derived.agent} / {derived.model}
          </dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{fmtTime(detail.first_occurred_at)}</dd>
        </div>
        <div>
          <dt>Elapsed</dt>
          <dd>
            {aggregate?.durationMs == null
              ? fmtDuration(detail.first_occurred_at, detail.last_observed_at)
              : `${aggregate.durationMs.toLocaleString()} ms`}
          </dd>
        </div>
        <div>
          <dt>Cost</dt>
          <dd>{fmtMoney(totalCost)}</dd>
        </div>
        <div>
          <dt>Tokens</dt>
          <dd>{tokens ? tokens.toLocaleString() : "Unknown"}</dd>
        </div>
        <div>
          <dt>Selected candidate</dt>
          <dd>{derived.selectedCandidate ?? "None"}</dd>
        </div>
        <div>
          <dt>Terminal reason</dt>
          <dd>{derived.terminalReason ?? derived.status.reason}</dd>
        </div>
        <div>
          <dt>File writes</dt>
          <dd>{aggregate?.fileWriteCount ?? derived.status.fileEdits}</dd>
        </div>
        <div>
          <dt>Attempts / escalations</dt>
          <dd>
            {aggregate?.attemptCount ?? detail.attempts.length} /{" "}
            {aggregate?.escalationCount ?? 0}
          </dd>
        </div>
        {derived.redaction && (
          <div>
            <dt>Redaction</dt>
            <dd>Remote data redacted</dd>
          </div>
        )}
      </dl>
    </header>
  );
}

const category = (event: RunEvent) => {
  const value = `${event.kind ?? ""} ${nameOf(event)}`.toLowerCase();
  return (
    (
      [
        "controller",
        "model",
        "tool",
        "command",
        "file",
        "verifier",
        "policy",
        "queue",
        "materialization",
      ] as const
    ).find((key) => value.includes(key)) ?? "controller"
  );
};

function Timeline({ events }: { events: RunEvent[] }) {
  const [limit, setLimit] = useState(100);
  return (
    <section id="timeline" aria-labelledby="timeline-title">
      <div className="section-title">
        <div>
          <p className="kicker">Live chronology</p>
          <h2 id="timeline-title">Timeline</h2>
        </div>
        <span>{events.length} events</span>
      </div>
      <ol className="timeline" aria-label="Run events">
        {events.slice(-limit).map((event) => (
          <li key={event.event_id ?? event.id} tabIndex={0}>
            <span
              className={`event-mark ${tone(event.status ?? nameOf(event))}`}
              aria-hidden="true"
            />
            <div>
              <div className="event-heading">
                <span className="category">{category(event)}</span>
                <strong>{nameOf(event)}</strong>
                <time>{fmtTime(event.occurred_at ?? event.timestamp)}</time>
              </div>
              <p>
                {String(
                  event.body?.message ??
                    event.attributes?.message ??
                    event.status ??
                    "Recorded",
                )}
              </p>
            </div>
          </li>
        ))}
      </ol>
      {events.length > limit && (
        <button className="secondary" onClick={() => setLimit((value) => value + 100)}>
          Show 100 earlier events
        </button>
      )}
    </section>
  );
}

export function Graph({
  spans,
  hasMore,
  onMore,
}: {
  spans: RunSpan[];
  hasMore: boolean;
  onMore: () => void;
}) {
  const branches = new Map<string, RunSpan[]>();
  for (const span of spans) {
    const key = span.attempt_id ?? "controller";
    branches.set(key, [...(branches.get(key) ?? []), span]);
  }
  return (
    <section id="graph" aria-labelledby="graph-title">
      <div className="section-title">
        <div>
          <p className="kicker">Causal execution</p>
          <h2 id="graph-title">Execution graph</h2>
        </div>
        <span>{spans.length} spans</span>
      </div>
      <div className="graph" role="list" aria-label="Causal span graph">
        {[...branches].map(([branch, values]) => (
          <div className="branch" key={branch} role="listitem">
            <h3>{branch === "controller" ? "Controller" : `Candidate ${branch}`}</h3>
            {values.map((span) => (
              <article
                key={span.span_id}
                tabIndex={0}
                className={`node ${tone(span.status)}`}
                aria-label={`${span.kind} ${span.name}, ${span.status}`}
              >
                <div>
                  <span className="category">{span.kind}</span>
                  <strong>{span.name}</strong>
                </div>
                <p>
                  <span className="state-word">{span.status}</span> · parent{" "}
                  {span.parent_span_id ?? "root"}
                </p>
              </article>
            ))}
          </div>
        ))}
      </div>
      {!spans.length && <p className="empty">No spans captured.</p>}
      {hasMore && (
        <button className="secondary" onClick={onMore}>
          Load more spans
        </button>
      )}
    </section>
  );
}

export function Candidates({ derived }: { derived: DerivedRun }) {
  return (
    <section id="candidates" aria-labelledby="candidate-title">
      <div className="section-title">
        <div>
          <p className="kicker">Acceptance-grade evidence</p>
          <h2 id="candidate-title">Candidates</h2>
        </div>
      </div>
      <div className="candidate-grid">
        {derived.candidates.map((candidate) => (
          <article
            key={candidate.attemptId}
            className={`candidate ${candidate.selected ? "selected" : ""}`}
          >
            <div className="candidate-head">
              <h3>{candidate.attemptId}</h3>
              <span className={`status ${candidate.eligible ? "success" : "warning"}`}>
                {candidate.eligible ? "Eligible" : "Ineligible"}
              </span>
            </div>
            <p>
              {candidate.status}
              {candidate.selected ? " · Selected" : ""}
            </p>
            <dl>
              <dt>Requirements</dt>
              <dd>
                {candidate.requirementResults.length
                  ? JSON.stringify(maskSensitive(candidate.requirementResults))
                  : "Not captured"}
              </dd>
              <dt>Evidence grades</dt>
              <dd>{candidate.evidenceGrades.join(", ") || "Not captured"}</dd>
              <dt>Risks</dt>
              <dd>{candidate.risks.join(", ") || "None recorded"}</dd>
              <dt>Patch digest</dt>
              <dd className="digest">{candidate.patchDigest ?? "Not captured"}</dd>
              <dt>Selection explanation</dt>
              <dd>
                {candidate.explanation ?? "Not selected or explanation unavailable"}
              </dd>
            </dl>
          </article>
        ))}
      </div>
      {!derived.candidates.length && <p className="empty">No candidates recorded.</p>}
    </section>
  );
}

function Cost({ derived }: { derived: DerivedRun }) {
  const selectedCost = derived.metrics
    .filter((row) => row.selected)
    .reduce((sum, row) => sum + (row.costUsd ?? 0), 0);
  const rejectedCost = derived.metrics
    .filter((row) => row.attemptId && !row.selected)
    .reduce((sum, row) => sum + (row.costUsd ?? 0), 0);
  return (
    <section id="cost" aria-labelledby="cost-title">
      <div className="section-title">
        <div>
          <p className="kicker">Accounting provenance</p>
          <h2 id="cost-title">Cost and tokens</h2>
        </div>
        <span>
          Selected {fmtMoney(selectedCost)} · Rejected {fmtMoney(rejectedCost)}
        </span>
      </div>
      <div className="table-scroll" tabIndex={0}>
        <table>
          <caption className="sr-only">
            Cost and token usage by stage and attempt
          </caption>
          <thead>
            <tr>
              <th>Stage</th>
              <th>Attempt / model</th>
              <th>Work</th>
              <th>Cost</th>
              <th>Input</th>
              <th>Output</th>
            </tr>
          </thead>
          <tbody>
            {derived.metrics.map((row) => (
              <tr key={row.key}>
                <td>{row.stage}</td>
                <td>
                  {row.attemptId ?? "Run"}
                  {row.model ? ` / ${row.model}` : ""}
                </td>
                <td>
                  {row.selected
                    ? "Selected"
                    : row.retry
                      ? "Retry"
                      : row.attemptId
                        ? "Rejected / active"
                        : "Shared"}
                </td>
                <td>{fmtMoney(row.costUsd)}</td>
                <td>{fmtTokens(row.inputTokens)}</td>
                <td>{fmtTokens(row.outputTokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!derived.metrics.length && (
        <p className="empty">Cost and token accounting were not captured.</p>
      )}
    </section>
  );
}

function Files({
  derived,
  artifacts,
  hasMore,
  onMore,
  client,
}: {
  derived: DerivedRun;
  artifacts: ArtifactDescriptor[];
  hasMore: boolean;
  onMore: () => void;
  client: RunClient;
}) {
  const [content, setContent] = useState<string>();
  const [artifactError, setArtifactError] = useState<string>();
  async function open(artifact: ArtifactDescriptor) {
    if (!artifactMayRender(artifact.sensitivity)) return;
    try {
      setArtifactError(undefined);
      setContent(await client.artifactContent(artifact.artifact_id));
    } catch (error) {
      setArtifactError(error instanceof Error ? error.message : "Artifact unavailable");
    }
  }
  return (
    <section id="files" aria-labelledby="files-title">
      <div className="section-title">
        <div>
          <p className="kicker">Patch evolution</p>
          <h2 id="files-title">Files and patches</h2>
        </div>
      </div>
      <div className="split">
        <div>
          <h3>Changed files</h3>
          <ul className="file-list">
            {derived.changedFiles.map((file) => (
              <li key={file}>{file}</li>
            ))}
          </ul>
          {!derived.changedFiles.length && (
            <p className="empty">No file list captured.</p>
          )}
        </div>
        <div>
          <h3>Artifacts</h3>
          <ul className="artifact-list">
            {artifacts.map((artifact) => (
              <li key={artifact.artifact_id}>
                <button
                  disabled={
                    !artifactMayRender(artifact.sensitivity) ||
                    artifact.status !== "available"
                  }
                  onClick={() => void open(artifact)}
                  aria-label={`${artifact.logical_role}, ${artifact.sensitivity}${artifact.sensitivity === "secret" ? ", content redacted" : ""}`}
                >
                  <span>{artifact.logical_role}</span>
                  <small>
                    {artifact.sensitivity} · {artifact.status ?? "recorded"}
                  </small>
                </button>
              </li>
            ))}
          </ul>
          {hasMore && (
            <button className="secondary" onClick={onMore}>
              Load more artifacts
            </button>
          )}
        </div>
      </div>
      <h3>Patch snapshots</h3>
      <ol className="patch-list">
        {derived.patchEvolution.map((patch) => (
          <li key={patch.id}>
            <strong>{patch.attemptId ?? "run"}</strong> ·{" "}
            {patch.digest ?? "digest unavailable"}
            <br />
            <small>{patch.files.join(", ") || "Files not captured"}</small>
          </li>
        ))}
      </ol>
      {artifactError && (
        <p role="alert" className="error-box">
          {artifactError}
        </p>
      )}
      {content !== undefined && (
        <pre aria-label="Authorized artifact content">{content}</pre>
      )}
    </section>
  );
}

function Policy({ derived }: { derived: DerivedRun }) {
  return (
    <section id="policy" aria-labelledby="policy-title">
      <div className="section-title">
        <div>
          <p className="kicker">Deterministic decisions</p>
          <h2 id="policy-title">Policy</h2>
        </div>
      </div>
      {derived.policyDecisions.map((decision, index) => (
        <details key={index}>
          <summary>
            {String(
              decision.action ??
                decision.name ??
                decision.decision ??
                `Decision ${index + 1}`,
            )}
          </summary>
          <dl className="policy-grid">
            <dt>Alternatives</dt>
            <dd>
              <pre>
                {JSON.stringify(
                  maskSensitive(
                    decision.alternatives ?? decision.considered_backends ?? [],
                  ),
                  null,
                  2,
                )}
              </pre>
            </dd>
            <dt>Rejection reasons</dt>
            <dd>{JSON.stringify(maskSensitive(decision.rejection_reasons ?? []))}</dd>
            <dt>Budgets</dt>
            <dd>
              {JSON.stringify(
                maskSensitive(decision.budget ?? decision.budgets ?? "Not captured"),
              )}
            </dd>
            <dt>Experiment</dt>
            <dd>{String(decision.experiment_assignment ?? "Not assigned")}</dd>
            <dt>Escalation</dt>
            <dd>
              {String(
                decision.escalation_reason ?? decision.reason ?? "Not applicable",
              )}
            </dd>
          </dl>
        </details>
      ))}
      {!derived.policyDecisions.length && (
        <p className="empty">No policy decisions captured.</p>
      )}
    </section>
  );
}

function Failure({ derived }: { derived: DerivedRun }) {
  if (!derived.failure) return null;
  return (
    <section id="failure" aria-labelledby="failure-title" className="failure">
      <div className="section-title">
        <div>
          <p className="kicker">Safe recovery</p>
          <h2 id="failure-title">Failure</h2>
        </div>
      </div>
      <dl>
        <dt>Classified root cause</dt>
        <dd>{derived.failure.rootCause}</dd>
        <dt>Relevant evidence</dt>
        <dd>{derived.failure.evidence.join(" · ") || "No evidence captured"}</dd>
        <dt>Next safe action</dt>
        <dd>{derived.failure.nextSafeAction}</dd>
      </dl>
      <div className="actions">
        {derived.failure.resumeUrl && (
          <a href={derived.failure.resumeUrl}>Resume run</a>
        )}
        {derived.failure.cancelUrl && (
          <a href={derived.failure.cancelUrl}>Cancel run</a>
        )}
      </div>
    </section>
  );
}

export default function App() {
  const runId = decodeURIComponent(
    location.pathname.match(/\/runs\/([^/]+)/)?.[1] ??
      new URLSearchParams(location.search).get("run") ??
      "",
  );
  const client = useMemo(
    () =>
      new RunClient(
        import.meta.env.VITE_API_BASE_URL ?? "",
        sessionStorage.getItem("villani.token") ?? import.meta.env.VITE_API_TOKEN ?? "",
      ),
    [],
  );
  const run = useRun(runId, client);
  if (!runId)
    return (
      <main id="main" className="center">
        <h1>Run ID required</h1>
        <p>
          Open <code>/runs/&lt;run_id&gt;</code>.
        </p>
      </main>
    );
  if (run.error)
    return (
      <main id="main" className="center">
        <h1>Unable to open run</h1>
        <p role="alert">{run.error}</p>
      </main>
    );
  if (!run.detail || !run.derived)
    return (
      <main id="main" className="center" aria-busy="true">
        <h1>Loading run…</h1>
      </main>
    );
  const exportRun = async () => {
    const events: RunEvent[] = [];
    let eventCursor: string | null = null;
    do {
      const page = await client.events(runId, eventCursor);
      events.push(...page.events);
      eventCursor = page.next_cursor;
    } while (eventCursor);
    const spans = [...run.spans];
    let spanCursor = run.spanCursor;
    while (spanCursor) {
      const page = await client.spans(runId, spanCursor);
      spans.push(...page.values);
      spanCursor = page.nextCursor;
    }
    const artifacts = [...run.artifacts];
    let artifactCursor = run.artifactCursor;
    while (artifactCursor) {
      const page = await client.artifacts(runId, artifactCursor);
      artifacts.push(...page.values);
      artifactCursor = page.nextCursor;
    }
    downloadStaticExport({
      detail: run.detail!,
      events,
      spans,
      artifacts,
      derived: deriveRun(run.detail!, events),
      exportedAt: new Date().toISOString(),
    });
  };
  return (
    <>
      <nav aria-label="Run sections">
        <a href="#timeline">Timeline</a>
        <a href="#graph">Graph</a>
        <a href="#candidates">Candidates</a>
        <a href="#cost">Cost</a>
        <a href="#files">Files</a>
        <a href="#policy">Policy</a>
        {run.derived.failure && <a href="#failure">Failure</a>}
      </nav>
      <main id="main">
        <Header
          detail={run.detail}
          derived={run.derived}
          connection={run.connection}
          onExport={exportRun}
        />
        <Timeline events={run.events} />
        <Graph
          spans={run.spans}
          hasMore={Boolean(run.spanCursor)}
          onMore={() => void run.loadMoreSpans()}
        />
        <Candidates derived={run.derived} />
        <Cost derived={run.derived} />
        <Files
          derived={run.derived}
          artifacts={run.artifacts}
          hasMore={Boolean(run.artifactCursor)}
          onMore={() => void run.loadMoreArtifacts()}
          client={client}
        />
        <Policy derived={run.derived} />
        <Failure derived={run.derived} />
      </main>
    </>
  );
}
