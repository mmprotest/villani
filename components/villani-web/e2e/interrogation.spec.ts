import { expect, test } from "@playwright/test";

test("structured query and follow-up expose plans and supporting runs", async ({
  page,
}) => {
  const bodies: unknown[] = [];
  await page.route("**/v1/interrogation/query", async (route) => {
    const body = route.request().postDataJSON();
    bodies.push(body);
    await route.fulfill({
      json: {
        answer: "The authorized query returned one group.",
        interpreted_query: "Compute verified_success_rate grouped by model.",
        query_plan: {
          schema_version: "villani.query_plan.v1",
          metrics: ["verified_success_rate"],
          dimensions: ["model"],
        },
        metric_definitions: {
          verified_success_rate:
            "Accepted / known verdicts; unknown outcomes reported separately.",
        },
        filters: [],
        authorization: {
          permission_version: "tenant_scope.v1",
          tenant_predicates_injected: true,
        },
        estimate: { scan_rows: 100, result_limit: 50, estimated_cells: 200 },
        data_freshness: "2026-07-12T00:00:00Z",
        row_count: 100,
        uncertainty: { unknown_outcomes: 4, unknown_cost: 2 },
        rows: [{ model: "model-a", verified_success_rate: 0.8 }],
        supporting_runs: [
          {
            run_id: "run_123",
            url: "/runs/run_123",
            last_observed_at: "2026-07-12T00:00:00Z",
          },
        ],
        conversation: {
          id: "conversation_1",
          version: bodies.length,
          stored_context: "structured_query_only",
        },
      },
    });
  });
  await page.goto("/ask");
  await page.getByLabel(/Ask about metrics/).fill("success rate by model");
  await page.getByRole("button", { name: "Run query" }).click();
  await expect(
    page.getByText("The authorized query returned one group."),
  ).toBeVisible();
  await expect(page.getByLabel("QueryPlan AST")).toContainText("villani.query_plan.v1");
  await expect(page.getByRole("link", { name: "run_123" })).toHaveAttribute(
    "href",
    "/console/runs/run_123",
  );

  await page.getByLabel(/Ask about metrics/).fill("only provider x");
  await page.getByRole("button", { name: "Ask follow-up" }).click();
  expect(bodies).toEqual([
    { question: "success rate by model", conversation_id: null },
    { question: "only provider x", conversation_id: "conversation_1" },
  ]);
});

test("disabled interrogation fails without exposing partial data", async ({ page }) => {
  await page.route("**/v1/interrogation/query", (route) =>
    route.fulfill({ status: 404, json: { detail: "not found" } }),
  );
  await page.goto("/ask");
  await page.getByLabel(/Ask about metrics/).fill("show everything");
  await page.getByRole("button", { name: "Run query" }).click();
  await expect(page.getByRole("alert")).toContainText("Request failed (404)");
  await expect(page.getByRole("heading", { name: "Answer" })).toHaveCount(0);
});
