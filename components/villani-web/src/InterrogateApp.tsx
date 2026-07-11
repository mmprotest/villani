import { FormEvent, useMemo, useState } from "react";
import { InterrogationAnswer, RunClient } from "./api";

const display = (value: unknown) =>
  value == null
    ? "Unknown"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);

export default function InterrogateApp() {
  const client = useMemo(
    () =>
      new RunClient(
        import.meta.env.VITE_API_BASE_URL ?? "",
        sessionStorage.getItem("villani.token") ?? import.meta.env.VITE_API_TOKEN ?? "",
      ),
    [],
  );
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<InterrogationAnswer>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = question.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(undefined);
    try {
      setResult(await client.interrogate(trimmed, result?.conversation.id));
      setQuestion("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Interrogation is unavailable",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="fleet-header">
        <p className="kicker">Villani structured observability</p>
        <h1>Ask authorized run data</h1>
        <p>
          Questions are converted to a bounded, allowlisted query plan. Returned data
          never becomes an unrestricted conversation transcript.
        </p>
        <nav aria-label="Observability pages">
          <a href="/fleet">Fleet search</a>
        </nav>
      </header>
      <main>
        <section aria-labelledby="question-title">
          <h2 id="question-title">Question</h2>
          <form className="ask-form" onSubmit={submit}>
            <label htmlFor="question">
              Ask about metrics, dimensions, and structured run metadata
            </label>
            <textarea
              id="question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              maxLength={2000}
              rows={4}
              required
            />
            <button disabled={loading}>
              {loading ? "Interpreting…" : result ? "Ask follow-up" : "Run query"}
            </button>
          </form>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
        </section>
        {result && (
          <>
            <section aria-labelledby="answer-title">
              <h2 id="answer-title">Answer</h2>
              <p>{result.answer}</p>
              <dl className="run-facts">
                <div>
                  <dt>Data freshness</dt>
                  <dd>{result.data_freshness ?? "Unknown"}</dd>
                </div>
                <div>
                  <dt>Supporting rows</dt>
                  <dd>{result.row_count}</dd>
                </div>
                <div>
                  <dt>Estimated scan</dt>
                  <dd>{result.estimate.scan_rows}</dd>
                </div>
                <div>
                  <dt>Authorization</dt>
                  <dd>
                    {result.authorization.tenant_predicates_injected
                      ? "Tenant predicates injected"
                      : "Denied"}
                  </dd>
                </div>
              </dl>
            </section>
            <section aria-labelledby="plan-title">
              <h2 id="plan-title">Interpreted structured plan</h2>
              <p>{result.interpreted_query}</p>
              <pre aria-label="QueryPlan AST">
                <code>{JSON.stringify(result.query_plan, null, 2)}</code>
              </pre>
              <h3>Metric definitions</h3>
              <dl>
                {Object.entries(result.metric_definitions).map(([name, definition]) => (
                  <div key={name}>
                    <dt>{name}</dt>
                    <dd>{definition}</dd>
                  </div>
                ))}
              </dl>
              <h3>Filters</h3>
              <ul>
                {result.filters.length ? (
                  result.filters.map((filter, index) => (
                    <li key={index}>{display(filter)}</li>
                  ))
                ) : (
                  <li>Only the displayed tenant and time predicates.</li>
                )}
              </ul>
            </section>
            <section aria-labelledby="results-title">
              <h2 id="results-title">Aggregate results</h2>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      {Object.keys(result.rows[0] ?? {}).map((key) => (
                        <th scope="col" key={key}>
                          {key}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((row, index) => (
                      <tr key={index}>
                        {Object.entries(row).map(([key, value]) => (
                          <td key={key}>{display(value)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <h3>Uncertainty and missingness</h3>
              <dl>
                {Object.entries(result.uncertainty).map(([name, count]) => (
                  <div key={name}>
                    <dt>{name}</dt>
                    <dd>{count ?? "Unknown"}</dd>
                  </div>
                ))}
              </dl>
              <h3>Supporting runs</h3>
              <ul>
                {result.supporting_runs.map((run) => (
                  <li key={run.run_id}>
                    <a href={run.url}>{run.run_id}</a>
                  </li>
                ))}
              </ul>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
