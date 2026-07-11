import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs/promises";

const detail = (
  status = "RUNNING",
  attempts = [{ id: "attempt_001", status: "running" }],
) => ({
  id: "run_001",
  workspace_id: "workspace",
  project_id: "project",
  repository_id: "repo",
  trace_id: "0123456789abcdef0123456789abcdef",
  status,
  first_occurred_at: "2026-07-11T00:00:00Z",
  first_observed_at: "2026-07-11T00:00:00Z",
  last_observed_at: "2026-07-11T00:00:02Z",
  attempts,
  outcomes: [],
  artifact_count: 0,
});
const event = (
  id: string,
  name: string,
  status = "ok",
  attempt_id: string | null = null,
  body: Record<string, unknown> = {},
) => ({
  schema_version: "villani.telemetry_envelope.v2",
  id,
  event_id: id,
  idempotency_key: id,
  occurred_at: `2026-07-11T00:00:0${id.slice(-1)}Z`,
  observed_at: `2026-07-11T00:00:0${id.slice(-1)}Z`,
  sequence: Number(id.slice(-1)),
  run_id: "run_001",
  trace_id: "0123456789abcdef0123456789abcdef",
  span_id: `0123456789abcde${id.slice(-1)}`,
  parent_span_id: null,
  attempt_id,
  source: "controller",
  kind: name.includes("verification") ? "verifier" : "controller",
  name,
  status,
  attributes: {},
  body,
});

async function mockRun(
  page: Page,
  options: {
    status?: string;
    attempts?: { id: string; status: string }[];
    events?: ReturnType<typeof event>[];
    artifacts?: Record<string, unknown>[];
    streamBodies?: string[];
    deny?: boolean;
  } = {},
) {
  let stream = 0;
  await page.route("**/v1/runs/run_001/stream", async (route) => {
    const body = options.streamBodies?.[stream++] ?? "";
    await route.fulfill({ status: 200, contentType: "text/event-stream", body });
  });
  await page.route("**/v1/runs/run_001/events?**", async (route) =>
    route.fulfill({
      json: {
        events: options.events ?? [event("evt1", "run_created", "running")],
        next_cursor: null,
        cursor: "cursor-1",
      },
    }),
  );
  await page.route("**/v1/runs/run_001/spans?**", async (route) =>
    route.fulfill({
      json: {
        spans: [
          {
            schema_version: "villani.span.v2",
            trace_id: "0123456789abcdef0123456789abcdef",
            span_id: "0123456789abcdef",
            parent_span_id: null,
            run_id: "run_001",
            attempt_id: "attempt_001",
            kind: "agent",
            name: "coding",
            status: "ok",
            started_at: null,
            ended_at: null,
            attributes: {},
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/v1/runs/run_001/artifacts?**", async (route) =>
    route.fulfill({ json: { artifacts: options.artifacts ?? [], next_cursor: null } }),
  );
  await page.route("**/v1/runs/run_001", async (route) =>
    options.deny
      ? route.fulfill({ status: 404, json: { detail: "run not found" } })
      : route.fulfill({ json: detail(options.status, options.attempts) }),
  );
}

test("live progression appears from the subscription", async ({ page }) => {
  const live = event("evt2", "verification_completed", "ok", "attempt_001", {
    acceptance_eligible: true,
  });
  await mockRun(page, {
    streamBodies: [
      `id: live\nevent: telemetry.ingested\ndata: ${JSON.stringify({ topic: "telemetry.ingested", payload: { run_id: "run_001", event: live } })}\n\n`,
    ],
  });
  await page.goto("/runs/run_001");
  await expect(page.getByText("verification_completed")).toBeVisible();
  await expect(page.getByText("live").first()).toBeVisible();
});

test("reconnect catches up without duplicating events", async ({ page }) => {
  const live = event("evt2", "materialization_completed", "ok", "attempt_001");
  await mockRun(page, {
    streamBodies: ["", `data: ${JSON.stringify({ payload: { event: live } })}\n\n`],
  });
  await page.goto("/runs/run_001");
  await expect(page.getByText("materialization_completed")).toBeVisible({
    timeout: 5000,
  });
  await expect(page.getByText("run_created")).toHaveCount(1);
});

test("failed run shows classified failure and authorized actions only", async ({
  page,
}) => {
  await mockRun(page, {
    status: "FAILED",
    events: [
      event("evt1", "run_failed", "error", null, {
        root_cause: "repository_conflict",
        evidence: ["patch did not apply"],
        next_safe_action: "Rebase then resume",
        resume_url: "/resume/run_001",
      }),
    ],
  });
  await page.goto("/runs/run_001");
  await expect(page.getByRole("heading", { name: "Failure" })).toBeVisible();
  await expect(page.getByText("repository_conflict")).toBeVisible();
  await expect(page.getByRole("link", { name: "Resume run" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Cancel run" })).toHaveCount(0);
});

test("multiple candidates preserve eligibility and selected branch", async ({
  page,
}) => {
  await mockRun(page, {
    status: "COMPLETED",
    attempts: [
      { id: "attempt_001", status: "rejected" },
      { id: "attempt_002", status: "completed" },
    ],
    events: [
      event("evt1", "verification_completed", "rejected", "attempt_001", {
        acceptance_eligible: false,
      }),
      event("evt2", "verification_completed", "ok", "attempt_002", {
        acceptance_eligible: true,
      }),
      event("evt3", "candidate_selected", "ok", null, {
        selected_attempt_id: "attempt_002",
      }),
    ],
  });
  await page.goto("/runs/run_001");
  await expect(
    page.getByRole("heading", { name: "attempt_001", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "attempt_002", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Eligible", { exact: true })).toBeVisible();
  await expect(page.getByText("Ineligible", { exact: true })).toBeVisible();
});

test("secret artifact stays redacted and cannot request content", async ({ page }) => {
  let contentRequests = 0;
  await page.route("**/v1/artifacts/**", async (route) => {
    contentRequests++;
    await route.fulfill({ status: 500 });
  });
  await mockRun(page, {
    artifacts: [
      {
        artifact_id: "secret",
        logical_role: "command-log",
        media_type: "text/plain",
        size_bytes: 9,
        sensitivity: "secret",
        status: "redacted",
      },
    ],
  });
  await page.goto("/runs/run_001");
  const artifact = page.getByRole("button", {
    name: /command-log, secret, content redacted/,
  });
  await expect(artifact).toBeDisabled();
  await expect(page.getByText("supersecret")).toHaveCount(0);
  expect(contentRequests).toBe(0);
});

test("authorization failure is non-enumerating", async ({ page }) => {
  await mockRun(page, { deny: true });
  await page.goto("/runs/run_001");
  await expect(page.getByRole("alert")).toContainText(
    "not found or you are not authorized",
  );
});

test("static export downloads a no-server HTML run", async ({ page }) => {
  await mockRun(page, { status: "COMPLETED" });
  await page.goto("/runs/run_001");
  const downloadPromise = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Export this run for offline viewing" })
    .click();
  const download = await downloadPromise;
  const body = await fs.readFile(await download.path()!, "utf8");
  expect(body).toContain("<!doctype html>");
  expect(body).toContain("Villani static run export");
  expect(body).not.toContain("/src/main.tsx");
});
