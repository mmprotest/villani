import { expect, test, type Page } from "@playwright/test";

const rows = (start: number) =>
  Array.from({ length: 100 }, (_, offset) => ({
    id: `run_${String(start - offset).padStart(6, "0")}`,
    state: offset % 5 ? "completed" : "failed",
    repository_id: "repo",
    agent: "codex",
    model: "fleet-model",
    provider: "openai",
    verification: offset % 3 ? "accepted" : "unknown",
    cost_usd: offset % 4 ? 1.25 : null,
    cost_accounting_status: offset % 4 ? "complete" : "unknown",
    total_tokens: 1000 + offset,
    duration_ms: 5000 + offset,
    last_observed_at: "2026-07-11T00:00:00Z",
    tags: [],
  }));

async function mockFleet(page: Page) {
  let searchCalls = 0;
  await page.route("**/v1/fleet/runs/search", async (route) => {
    searchCalls++;
    const request = route.request().postDataJSON() as { cursor?: string | null };
    const second = Boolean(request.cursor);
    await route.fulfill({
      json: {
        runs: rows(second ? 99_899 : 99_999),
        next_cursor: second ? null : "cursor-2",
      },
    });
  });
  await page.route("**/v1/fleet/metrics/definitions", async (route) =>
    route.fulfill({
      json: {
        version: "villani.fleet_metrics.v1",
        metrics: {
          verified_success_rate: {
            numerator: "accepted",
            denominator: "accepted or rejected",
            unknown_rule: "reported separately",
          },
        },
      },
    }),
  );
  await page.route("**/v1/fleet/metrics", async (route) =>
    route.fulfill({
      json: {
        definition_version: "villani.fleet_metrics.v1",
        metrics: {
          run_count: 100_000,
          verified_success_rate: {
            value: 0.72,
            numerator: 72_000,
            denominator: 100_000,
            unknown_outcome_count: 4_000,
          },
          cost_per_accepted_change: { value: 1.5, unknown_cost_count: 2_000 },
          duration_ms: { average: 5000, unknown_count: 100 },
          queue_time_ms: { average: 300, unknown_count: 20 },
          attempts: 130_000,
          escalations: 4_000,
          rejected_wasted_spend_usd: { known_total: 800, unknown_count: 500 },
        },
        comparisons: {
          "fleet-model": {
            verified_success_rate: { value: 0.72 },
            cost_per_accepted_change: { value: 1.5 },
            attempts: 130_000,
            escalations: 4_000,
          },
        },
      },
    }),
  );
  await page.route("**/v1/fleet/saved-views", async (route) =>
    route.fulfill({ json: { views: [] } }),
  );
  await page.route("**/v1/fleet/alerts", async (route) =>
    route.fulfill({ json: { rules: [], events: [] } }),
  );
  await page.route("**/v1/fleet/review-queue", async (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.route("**/v1/fleet/failure-clusters", async (route) =>
    route.fulfill({ json: { clusters: [] } }),
  );
  return () => searchCalls;
}

test("100,000-run fleet stays server-paginated and exposes unknown denominators", async ({
  page,
}) => {
  const calls = await mockFleet(page);
  await page.goto("/fleet");
  await expect(page.getByRole("heading", { name: "Fleet control room" })).toBeVisible();
  await expect(page.getByText("100000 filtered runs")).toBeVisible();
  await expect(page.getByText("4000 unknown outcomes")).toBeVisible();
  await expect(page.locator("tbody tr")).toHaveCount(100);
  await expect(page.getByRole("link", { name: "run_099999" })).toBeVisible();
  await page.getByRole("button", { name: "Next page" }).click();
  await expect(page.getByRole("link", { name: "run_099899" })).toBeVisible();
  await expect(page.locator("tbody tr")).toHaveCount(100);
  expect(calls()).toBeLessThanOrEqual(3); // React development strict mode may replay the initial request.
});
