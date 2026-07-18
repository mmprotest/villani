import { expect, test, type Page } from "@playwright/test";

import { mockPt2Console, productRun, pt2Action } from "./pt2-fixtures";

async function capture(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await expect(page).toHaveScreenshot(name, {
    animations: "disabled",
    caret: "hide",
    fullPage: true,
  });
}

async function expectResultOrder(result: ReturnType<Page["getByTestId"]>) {
  const text = (await result.textContent()) ?? "";
  const labels = [
    "WHAT CHANGED",
    "FILES CHANGED",
    "CHECKS AND TESTS",
    "REQUIREMENT COVERAGE",
    "KNOWN COST",
    "ELAPSED TIME",
    "Evidence",
  ];
  let previous = -1;
  for (const label of labels) {
    const position = text.indexOf(label);
    expect(
      position,
      `${label} must be present after the previous result section`,
    ).toBeGreaterThan(previous);
    previous = position;
  }
}

async function startTask(page: Page) {
  await page.goto("/console");
  await page
    .getByLabel(/^Task/)
    .fill("Fix repeated separators in the parser.\nPreserve multiline text.");
  await page.getByRole("button", { name: "Run safely" }).click();
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
});

test("PT2 clean success uses four canonical stages and safe defaults", async ({
  page,
}) => {
  const products = [
    productRun({ stage: "Understanding", verdict: null }),
    productRun({ stage: "Working", verdict: null }),
    productRun({ stage: "Checking", verdict: null }),
    productRun(),
  ];
  const fixture = await mockPt2Console(page, { products });
  await startTask(page);

  await expect(page.getByRole("heading", { name: "UNDERSTANDING" })).toBeVisible();
  await capture(page, "stage-understanding.png");

  fixture.advance();
  await expect(page.getByRole("heading", { name: "WORKING" })).toBeVisible();
  await capture(page, "stage-working.png");

  fixture.advance();
  await expect(page.getByRole("heading", { name: "CHECKING" })).toBeVisible();
  await capture(page, "stage-checking.png");

  fixture.advance();
  const result = page.getByTestId("run-presentation");
  await expect(result.getByText("Ready to apply")).toBeVisible();
  await expectResultOrder(result);
  await capture(page, "stage-ready-verdict-ready-to-apply.png");

  expect(fixture.submissions).toHaveLength(1);
  expect(fixture.submissions[0]).toMatchObject({
    repository: "C:/work/sample-app",
    task: "Fix repeated separators in the parser.\nPreserve multiline text.",
    policy_preset: "performance",
    delivery_mode: "approve",
    verification_required: true,
  });
  expect(fixture.submissions[0]).not.toHaveProperty("max_wall_time");
  await expect(result).not.toContainText(/\d+%/);
});

test("PT2 applies only the selected proved patch after the result", async ({
  page,
}) => {
  const applied = productRun({
    targetModified: true,
    actions: [
      pt2Action(
        "review_evidence",
        "Review evidence",
        "GET",
        "/console/runs/run_pt2_fixture/replay",
      ),
    ],
  });
  const fixture = await mockPt2Console(page, { deliveryResult: applied });
  await page.goto("/console?run=run_pt2_fixture");
  await page.getByRole("button", { name: "Apply change" }).click();

  await expect(page.getByText("The target repository was modified.")).toBeVisible();
  expect(fixture.approvals).toEqual([
    {
      action: "approve",
      reason: "Apply change selected from the product result.",
    },
  ]);
  await capture(page, "delivery-apply-selected-patch.png");
});

test("PT2 creates a branch without changing the original working tree", async ({
  page,
}) => {
  const branchReady = productRun({
    actions: [
      pt2Action(
        "create_branch",
        "Create branch",
        "POST",
        "/v1/console/runs/run_pt2_fixture/approval",
      ),
      pt2Action(
        "review_evidence",
        "Review evidence",
        "GET",
        "/console/runs/run_pt2_fixture/replay",
      ),
    ],
  });
  const branched = productRun({
    actions: [
      pt2Action(
        "review_evidence",
        "Review evidence",
        "GET",
        "/console/runs/run_pt2_fixture/replay",
      ),
    ],
    targetStatement:
      "A separate branch was created. The original working tree was not modified.",
  });
  await mockPt2Console(page, { products: [branchReady], deliveryResult: branched });
  await page.goto("/console?run=run_pt2_fixture");
  await page.getByRole("button", { name: "Create branch" }).click();

  await expect(
    page.getByText(
      "A separate branch was created. The original working tree was not modified.",
    ),
  ).toBeVisible();
  await capture(page, "delivery-create-branch.png");
});

test("PT2 explains validation retry and then proves the result", async ({ page }) => {
  const products = [
    productRun({ stage: "Understanding", verdict: null }),
    productRun({
      stage: "Working",
      verdict: null,
      sentence: "The first route could not prove the change. Retrying.",
    }),
    productRun({
      stage: "Checking",
      verdict: null,
      sentence: "Verification needs another check.",
    }),
    productRun(),
  ];
  const fixture = await mockPt2Console(page, { products });
  await startTask(page);
  fixture.advance();
  await expect(
    page.getByText("The first route could not prove the change. Retrying."),
  ).toBeVisible();
  await capture(page, "validation-failure-retrying.png");
  fixture.advance();
  await expect(page.getByText("Verification needs another check.")).toBeVisible();
  fixture.advance();
  await expect(page.getByText("Ready to apply")).toBeVisible();
});

test("PT2 missing evidence is Could not prove and cannot be delivered", async ({
  page,
}) => {
  const failed = productRun({
    verdict: "Could not prove",
    reason: "Verification evidence was missing.",
    actions: [
      pt2Action("retry", "Start again", "GET", "/console"),
      pt2Action(
        "review_evidence",
        "Review evidence",
        "GET",
        "/console/runs/run_pt2_fixture/replay",
      ),
    ],
  });
  await mockPt2Console(page, { products: [failed] });
  await page.goto("/console?run=run_pt2_fixture");

  const result = page.getByTestId("run-presentation");
  await expect(result.getByText("Could not prove")).toBeVisible();
  for (const label of ["Apply change", "Create branch", "Open pull request"])
    await expect(result.getByRole("button", { name: label })).toHaveCount(0);
  await expectResultOrder(result);
  await capture(page, "verdict-could-not-prove-ordered.png");
});

test("PT2 cancellation stops the task and preserves evidence", async ({ page }) => {
  const running = productRun({ stage: "Working", verdict: null });
  const cancelled = productRun({
    verdict: "Cancelled",
    reason: "The task was cancelled safely and recorded evidence was preserved.",
    actions: [
      pt2Action("retry", "Start again", "GET", "/console"),
      pt2Action(
        "review_evidence",
        "Review evidence",
        "GET",
        "/console/runs/run_pt2_fixture/replay",
      ),
    ],
  });
  await mockPt2Console(page, { products: [running], cancelResult: cancelled });
  await page.goto("/console?run=run_pt2_fixture");
  await page.getByRole("button", { name: "Cancel" }).click();

  await expect(page.getByText("Cancelled", { exact: true })).toBeVisible();
  await expect(page.getByText("The target repository was not modified.")).toBeVisible();
  await page.getByText("Evidence", { exact: true }).click();
  await expect(page.getByText("Recorded evidence", { exact: true })).toBeVisible();
  await capture(page, "verdict-cancelled.png");
});

test("PT2 browser refresh reconnects without duplicate submission", async ({
  page,
}) => {
  const fixture = await mockPt2Console(page, {
    products: [productRun({ stage: "Working", verdict: null })],
  });
  await page.goto("/console?run=run_pt2_fixture");
  await expect(page.getByRole("heading", { name: "WORKING" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "WORKING" })).toBeVisible();

  expect(fixture.submissions).toHaveLength(0);
  await capture(page, "browser-refresh-reconnected.png");
});

test("PT2 dirty target explains the safe action before any run", async ({ page }) => {
  const fixture = await mockPt2Console(page, { dirtyRepository: true });
  await page.goto("/console");

  await expect(
    page.getByText("Commit or stash existing changes before starting a task."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Run safely" })).toBeDisabled();
  expect(fixture.submissions).toHaveLength(0);
  await capture(page, "dirty-target-safe-action.png");
});

test("PT2 preserves unknown cost instead of fabricating zero", async ({ page }) => {
  await mockPt2Console(page, {
    products: [productRun({ cost: null, costStatus: "unknown" })],
  });
  await page.goto("/console?run=run_pt2_fixture");

  const result = page.getByTestId("run-presentation");
  await expect(result.getByText("Unknown (unknown)")).toBeVisible();
  await expect(result.getByText(/USD 0\.0000/)).toHaveCount(0);
  await capture(page, "unknown-cost.png");
});

test("PT2 unavailable agent gives one exact recovery action", async ({ page }) => {
  await mockPt2Console(page, { unavailableAgent: true });
  await startTask(page);

  await expect(page.getByText("No usable agent system is available.")).toBeVisible();
  await expect(
    page.getByText(
      "Open Settings > Agents, connect one usable agent system, then try again.",
    ),
  ).toBeVisible();
  await expect(
    page.getByText("No run was started. The target repository was not modified."),
  ).toBeVisible();
  await capture(page, "unavailable-agent.png");
});

test("PT2 target drift becomes Needs review and delivery remains denied", async ({
  page,
}) => {
  const conflict = productRun({
    verdict: "Needs review",
    reason: "The target repository changed after verification.",
    actions: [
      pt2Action("retry", "Start again", "GET", "/console"),
      pt2Action(
        "review_evidence",
        "Review evidence",
        "GET",
        "/console/runs/run_pt2_fixture/replay",
      ),
    ],
    targetStatement:
      "Delivery stopped before applying the patch. The target repository was not modified.",
  });
  await mockPt2Console(page, { deliveryResult: conflict });
  await page.goto("/console?run=run_pt2_fixture");
  await page.getByRole("button", { name: "Apply change" }).click();

  const result = page.getByTestId("run-presentation");
  await expect(result.getByText("Needs review")).toBeVisible();
  await expect(
    result.getByText("The target repository changed after verification."),
  ).toBeVisible();
  await expect(result.getByRole("button", { name: "Apply change" })).toHaveCount(0);
  await capture(page, "verdict-needs-review-target-drift.png");
});
