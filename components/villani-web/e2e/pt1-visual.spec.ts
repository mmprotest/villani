import { expect, test, type Locator, type Page } from "@playwright/test";

import { mixedActivity, mockPt1Console } from "./pt1-fixtures";

async function settle(page: Page) {
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready);
}

async function capture(page: Page, name: string) {
  await settle(page);
  await expect(page).toHaveScreenshot(name, {
    animations: "disabled",
    caret: "hide",
    fullPage: true,
  });
}

async function captureElement(locator: Locator, name: string) {
  await expect(locator).toHaveScreenshot(name, {
    animations: "disabled",
    caret: "hide",
  });
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
});

test("PT1 visual contract: onboarding start", async ({ page }) => {
  await mockPt1Console(page, { setupValid: false });
  await page.goto("/console/onboarding");
  await expect(
    page.getByRole("heading", {
      name: "Which repository should Villani work in?",
      level: 2,
    }),
  ).toBeVisible();
  await capture(page, "onboarding-start.png");
});

test("PT1 visual contract: detected agent", async ({ page }) => {
  await mockPt1Console(page, { setupValid: false });
  await page.goto("/console/onboarding");
  await page.getByRole("button", { name: "Use this repository" }).click();
  await expect(
    page.getByRole("heading", {
      name: "Which agent system should Villani use?",
      level: 2,
    }),
  ).toBeVisible();
  await capture(page, "onboarding-detected-agent.png");
});

test("PT1 visual contract: setup error", async ({ page }) => {
  await mockPt1Console(page, { setupValid: false, modelTestAvailable: false });
  await page.goto("/console/onboarding");
  await page.getByRole("button", { name: "Use this repository" }).click();
  await page.getByRole("button", { name: "Use this agent" }).click();
  await page.getByRole("button", { name: "Verify connection" }).click();
  await expect(
    page.getByText("The local agent endpoint did not respond."),
  ).toBeVisible();
  await capture(page, "onboarding-setup-error.png");
});

test("PT1 visual contract: setup complete", async ({ page }) => {
  await mockPt1Console(page);
  await page.goto("/console/onboarding");
  await expect(page.getByTestId("setup-complete")).toBeVisible();
  await capture(page, "onboarding-complete.png");
});

test("PT1 visual contract: empty New task", async ({ page }) => {
  await mockPt1Console(page);
  await page.goto("/console");
  await expect(page.locator("#task-instruction")).toBeVisible();
  await capture(page, "new-task-empty.png");
});

test("PT1 visual contract: populated New task", async ({ page }) => {
  await mockPt1Console(page);
  await page.goto("/console");
  await page
    .locator("#task-instruction")
    .fill("Make onboarding and the product share one clear visual system.");
  await page.getByText("Details (optional)").click();
  await page
    .locator("#task-success-criteria")
    .fill("The four default destinations work from keyboard and mobile layouts.");
  await capture(page, "new-task-populated.png");
});

test("PT1 visual contract: mixed Activity", async ({ page }) => {
  await mockPt1Console(page, { activity: mixedActivity });
  await page.goto("/console/activity");
  await expect(page.locator(".v-status-badge", { hasText: "IMPORTED" })).toBeVisible();
  await capture(page, "activity-mixed.png");
});

test("PT1 visual contract: healthy Settings", async ({ page }) => {
  await mockPt1Console(page);
  await page.goto("/console/settings");
  await expect(page.getByTestId("actionable-system-notice")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Diagnostics" })).toBeVisible();
  await capture(page, "settings-healthy.png");
});

test("PT1 visual contract: actionable service failure", async ({ page }) => {
  await mockPt1Console(page, {
    serviceStatus: "stopped",
    serviceError: "The local service stopped unexpectedly.",
  });
  await page.goto("/console");
  await expect(page.getByTestId("actionable-system-notice")).toBeVisible();
  await capture(page, "service-failure-actionable.png");
});

test("PT1 visual contract: Advanced navigation", async ({ page }) => {
  await mockPt1Console(page);
  await page.goto("/console/settings#advanced");
  const advanced = page.getByTestId("advanced-navigation");
  await advanced.scrollIntoViewIfNeeded();
  await captureElement(advanced, "advanced-navigation.png");
});

test("PT1 visual contract: mobile New task has no page overflow", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 760 });
  await mockPt1Console(page);
  await page.goto("/console");
  await expect(page.locator("#task-instruction")).toBeVisible();
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
  await capture(page, "new-task-mobile-320.png");
});

test("PT1 visual contract: keyboard focus", async ({ page }) => {
  await mockPt1Console(page);
  await page.goto("/console");
  await page.locator("body").press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await capture(page, "keyboard-focus.png");
});
