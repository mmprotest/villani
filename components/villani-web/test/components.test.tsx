import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Candidates, Graph } from "../src/App";
import type { DerivedRun } from "@villani/run-model";
import InterrogateApp from "../src/InterrogateApp";

const derived: DerivedRun = {
  status: {
    status: "succeeded",
    label: "Completed",
    tone: "success",
    reason: "Controller state COMPLETED",
    failedCommands: 0,
    failedTests: 0,
    totalCommands: 1,
    totalTests: 1,
    fileEdits: 1,
  },
  task: "Fix the parser",
  repository: "repo",
  policy: "bootstrap_v1",
  agent: "codex",
  model: "gpt",
  selectedCandidate: "attempt_002",
  candidates: [
    {
      attemptId: "attempt_002",
      status: "accepted",
      eligible: true,
      selected: true,
      requirementResults: [{ result: "passed" }],
      evidenceGrades: ["acceptance"],
      risks: [],
      patchDigest: "abc",
      explanation: "Strongest evidence",
      costUsd: 1,
    },
  ],
  metrics: [],
  changedFiles: [],
  patchEvolution: [],
  policyDecisions: [],
};

describe("individual run components", () => {
  it("renders candidate eligibility and selection without HTML injection", () => {
    render(
      <Candidates
        derived={{
          ...derived,
          candidates: [
            { ...derived.candidates[0]!, explanation: "<img src=x onerror=alert(1)>" },
          ],
        }}
      />,
    );
    expect(screen.getByText("Eligible")).toBeInTheDocument();
    expect(screen.getAllByText("<img src=x onerror=alert(1)>").length).toBeGreaterThan(
      0,
    );
    expect(document.querySelector("img")).toBeNull();
  });

  it("exposes causal parent links and candidate branches to assistive tech", () => {
    render(
      <Graph
        spans={[
          {
            span_id: "child",
            parent_span_id: "parent",
            attempt_id: "attempt_002",
            kind: "verifier",
            name: "requirements",
            status: "ok",
            started_at: null,
            ended_at: null,
            attributes: {},
          },
        ]}
        hasMore
        onMore={vi.fn()}
      />,
    );
    expect(screen.getByRole("list", { name: "Causal span graph" })).toBeInTheDocument();
    expect(screen.getByText("Candidate attempt_002")).toBeInTheDocument();
    expect(screen.getByText(/parent parent/)).toBeInTheDocument();
  });
});

describe("structured interrogation", () => {
  it("renders the plan, missingness, and safe supporting links", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          answer: "One aggregate row.",
          interpreted_query: "Compute run_count grouped by model.",
          query_plan: {
            schema_version: "villani.query_plan.v1",
            metrics: ["run_count"],
          },
          metric_definitions: { run_count: "Count of authorized runs." },
          filters: [],
          authorization: {
            permission_version: "tenant_scope.v1",
            tenant_predicates_injected: true,
          },
          estimate: { scan_rows: 2, result_limit: 50, estimated_cells: 2 },
          data_freshness: "2026-07-12T00:00:00Z",
          row_count: 2,
          uncertainty: { unknown_cost: 1 },
          rows: [{ model: "<img src=x onerror=alert(1)>", run_count: 2 }],
          supporting_runs: [
            {
              run_id: "run_1",
              url: "/runs/run_1",
              last_observed_at: "2026-07-12T00:00:00Z",
            },
          ],
          conversation: {
            id: "conversation_1",
            version: 1,
            stored_context: "structured_query_only",
          },
        }),
      }),
    );
    render(<InterrogateApp />);
    fireEvent.change(screen.getByLabelText(/Ask about metrics/), {
      target: { value: "runs by model" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run query" }));

    await waitFor(() =>
      expect(screen.getByText("One aggregate row.")).toBeInTheDocument(),
    );
    expect(screen.getByLabelText("QueryPlan AST")).toHaveTextContent(
      "villani.query_plan.v1",
    );
    expect(screen.getByText("unknown_cost")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "run_1" })).toHaveAttribute(
      "href",
      "/runs/run_1",
    );
    expect(screen.getAllByText("<img src=x onerror=alert(1)>").length).toBeGreaterThan(
      0,
    );
    expect(document.querySelector("img")).toBeNull();
  });
});
