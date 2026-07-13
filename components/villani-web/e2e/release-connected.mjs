import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

import { chromium } from "playwright";

const options = process.argv.slice(2).reduce((result, value, index, values) => {
  if (value.startsWith("--")) result[value.slice(2)] = values[index + 1];
  return result;
}, {});

const baseUrl = String(options.base).replace(/\/$/, "");
const artifacts = resolve(options.artifacts);
const runIds = JSON.parse(await readFile(resolve(options.runs), "utf8"));
const screenshotDirectory = resolve(artifacts, "screenshots");
await mkdir(screenshotDirectory, { recursive: true });

const browser = await chromium.launch({ headless: true });
const errors = [];
const requests = [];
const screenshots = [];
const checks = {};
const viewportCoverage = new Set();

const required = (condition, message) => {
  if (!condition) throw new Error(message);
};

async function api(runId) {
  const response = await fetch(`${baseUrl}/v1/runs/${encodeURIComponent(runId)}`);
  required(response.ok, `API snapshot request failed for ${runId}: ${response.status}`);
  return response.json();
}

function observe(page, label) {
  page.on("console", (message) => {
    if (message.type() === "error")
      errors.push({ page: label, kind: "console", message: message.text() });
  });
  page.on("pageerror", (error) =>
    errors.push({ page: label, kind: "pageerror", message: error.message }),
  );
  page.on("requestfailed", (request) => {
    const failure = request.failure()?.errorText ?? "request failed";
    if (request.url().includes("/stream") && /abort|cancel/i.test(failure)) return;
    requests.push({ page: label, kind: "failed", url: request.url(), failure });
  });
  page.on("response", (response) => {
    if (response.status() >= 400)
      requests.push({
        page: label,
        kind: "http",
        url: response.url(),
        status: response.status(),
      });
  });
}

async function computedTheme(page, surface) {
  const result = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const body = getComputedStyle(document.body);
    const panels = [
      ...document.querySelectorAll(
        ".v-panel,.panel,.run-summary,.fleet-metric,.candidate",
      ),
    ]
      .slice(0, 80)
      .map((element) => getComputedStyle(element).backgroundColor);
    return {
      tokenRoot: root.getPropertyValue("--v-bg-root").trim(),
      focus: root.getPropertyValue("--v-focus").trim(),
      font: root.getPropertyValue("--v-font").trim(),
      sidebarWidth: root.getPropertyValue("--v-sidebar-width").trim(),
      headerHeight: root.getPropertyValue("--v-header-height").trim(),
      bodyBackground: body.backgroundColor,
      panels,
      overflow:
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
      sidebar: Boolean(document.querySelector('[data-testid="shared-sidebar"]')),
      header: Boolean(document.querySelector('[data-testid="shared-header"]')),
    };
  });
  required(
    result.tokenRoot.toLowerCase() === "#050505",
    `${surface}: shared root token missing`,
  );
  required(
    result.focus.toLowerCase() === "#ffffff",
    `${surface}: monochrome focus token is ${result.focus}`,
  );
  required(
    result.sidebarWidth === "232px" && result.headerHeight === "48px",
    `${surface}: shared shell dimensions diverged`,
  );
  required(
    result.bodyBackground === "rgb(5, 5, 5)",
    `${surface}: root background is ${result.bodyBackground}`,
  );
  required(
    result.font.includes("ui-monospace"),
    `${surface}: shared typography missing`,
  );
  required(result.sidebar && result.header, `${surface}: shared shell is incomplete`);
  required(
    result.overflow <= 1,
    `${surface}: horizontal page overflow ${result.overflow}px`,
  );
  for (const background of result.panels) {
    const match = background.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (!match) continue;
    const luminance =
      Number(match[1]) * 0.2126 + Number(match[2]) * 0.7152 + Number(match[3]) * 0.0722;
    required(luminance < 45, `${surface}: light panel surface ${background}`);
  }
  checks[`${surface}_theme`] = result;
}

async function openPage(pathname, label, viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({
    viewport,
    colorScheme: "dark",
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  const actualViewport = page.viewportSize();
  required(
    actualViewport?.width === viewport.width &&
      actualViewport?.height === viewport.height,
    `${label}: requested viewport ${viewport.width}x${viewport.height} resolved to ${actualViewport?.width}x${actualViewport?.height}`,
  );
  viewportCoverage.add(`${actualViewport.width}x${actualViewport.height}`);
  checks[`${label}_viewport`] = actualViewport;
  observe(page, label);
  await page.goto(`${baseUrl}${pathname}`, {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
  await page.waitForSelector('[data-testid="shared-sidebar"]', { timeout: 20_000 });
  await page.waitForTimeout(350);
  await computedTheme(page, label);
  return { context, page };
}

async function capture(page, name, locator = null, fullPage = false) {
  const path = resolve(screenshotDirectory, name);
  if (locator) {
    const target = page.locator(locator).first();
    await target.waitFor({ state: "visible", timeout: 20_000 });
    await target.screenshot({ path });
  } else {
    await page.screenshot({ path, fullPage });
  }
  const contents = await readFile(path);
  required(
    contents.length >= 24 && contents.subarray(1, 4).toString("ascii") === "PNG",
    `${name}: screenshot is not a PNG image`,
  );
  const width = contents.readUInt32BE(16);
  const height = contents.readUInt32BE(20);
  const digest = createHash("sha256").update(contents).digest("hex");
  const result = { name, path, sha256: digest, width, height };
  screenshots.push(result);
  return result;
}

async function assertRunTruth(page, snapshot, label) {
  const body = await page.locator("body").innerText();
  required(body.includes(snapshot.id), `${label}: run ID absent`);
  required(body.includes(snapshot.task_instruction), `${label}: task absent`);
  if (snapshot.selected_attempt_id)
    required(
      body.includes(snapshot.selected_attempt_id),
      `${label}: selected attempt absent`,
    );
  if (snapshot.verification_authority)
    required(
      body.includes(snapshot.verification_authority),
      `${label}: authority absent`,
    );
  for (const file of snapshot.changed_files ?? [])
    required(body.includes(file), `${label}: materialized file ${file} absent`);
  required(
    !/attempt_\d+::|attempt_\d+\|[0-9a-f-]{8,}|scoped_attempt/i.test(body),
    `${label}: internal scoped attempt identity rendered`,
  );
}

try {
  const overview = await openPage("/fleet", "web_overview");
  await overview.page.getByRole("heading", { name: "Fleet control room" }).waitFor();
  await overview.page.locator("#overview").waitFor();
  await capture(overview.page, "01-villani-web-overview.png", null, true);
  await capture(overview.page, "02-runs-list.png", "#runs");
  const candidateIds = await overview.page
    .locator("tbody a[href^='/runs/']")
    .allTextContents();
  required(
    new Set(candidateIds).size === candidateIds.length,
    "Fleet runs list contains duplicate run cards",
  );
  await overview.context.close();

  const easySnapshot = await api(runIds.scenario_a);
  const easy = await openPage(
    `/runs/${encodeURIComponent(runIds.scenario_a)}`,
    "web_easy",
  );
  await easy.page.locator('[data-testid="canonical-run-model"]').waitFor();
  await assertRunTruth(easy.page, easySnapshot, "Villani Web easy run");
  await capture(easy.page, "03-easy-successful-run.png", null, true);
  await easy.context.close();

  const escalatedSnapshot = await api(runIds.scenario_b);
  const escalated = await openPage(
    `/runs/${encodeURIComponent(runIds.scenario_b)}`,
    "web_escalated",
  );
  await escalated.page.locator('[data-testid="candidate-comparison"]').waitFor();
  await assertRunTruth(escalated.page, escalatedSnapshot, "Villani Web escalated run");
  const cards = await escalated.page
    .locator("#candidates .candidate h3")
    .allTextContents();
  required(
    cards.length === new Set(cards).size,
    "Villani Web duplicate candidate cards",
  );
  required(
    cards.length === 2,
    `Villani Web expected 2 escalated candidates, received ${cards.length}`,
  );
  await capture(escalated.page, "04-escalated-run-overview.png", null, true);
  await capture(
    escalated.page,
    "05-candidate-comparison.png",
    '[data-testid="candidate-comparison"]',
  );
  await escalated.context.close();

  const verifierSnapshot = await api(runIds.scenario_f);
  const verifier = await openPage(
    `/runs/${encodeURIComponent(runIds.scenario_f)}`,
    "web_verifier",
  );
  await assertRunTruth(verifier.page, verifierSnapshot, "Villani Web verifier run");
  await capture(
    verifier.page,
    "06-verification-evidence.png",
    '[data-testid="verification-evidence"]',
  );
  await verifier.context.close();

  const classificationSnapshot = await api(runIds.scenario_g);
  const classification = await openPage(
    `/runs/${encodeURIComponent(runIds.scenario_g)}`,
    "web_classification",
  );
  await assertRunTruth(
    classification.page,
    classificationSnapshot,
    "Villani Web classification run",
  );
  const classificationText = await classification.page
    .locator('[data-testid="classification-adjustment"]')
    .innerText();
  required(
    classificationText.includes(
      String(classificationSnapshot.classification_adjustments[0]?.reason),
    ),
    "Classification adjustment reason absent in Web",
  );
  await capture(
    classification.page,
    "07-classification-adjustment.png",
    '[data-testid="classification-adjustment"]',
  );
  await classification.context.close();

  const redactionSnapshot = await api(runIds.scenario_d);
  const redaction = await openPage(
    `/runs/${encodeURIComponent(runIds.scenario_d)}`,
    "web_redaction",
  );
  await assertRunTruth(redaction.page, redactionSnapshot, "Villani Web redaction run");
  await redaction.page
    .locator('[data-testid="redaction-withholding-notice"]')
    .waitFor();
  await capture(
    redaction.page,
    "08-redaction-withheld-artifact.png",
    '[data-testid="redaction-withholding-notice"]',
  );
  await redaction.context.close();

  const heuristicSnapshot = await api(runIds.scenario_e);
  const heuristic = await openPage(
    `/runs/${encodeURIComponent(runIds.scenario_e)}`,
    "web_heuristic",
  );
  await assertRunTruth(
    heuristic.page,
    heuristicSnapshot,
    "Villani Web heuristic-only run",
  );
  required(
    (await heuristic.page.locator("body").innerText())
      .toLowerCase()
      .includes("authoritative"),
    "Heuristic-only terminal authority reason absent",
  );
  await capture(heuristic.page, "09-heuristic-only-failed-run.png", null, true);
  await heuristic.context.close();

  const flightSnapshot = escalatedSnapshot;
  const flight = await openPage(
    `/flight/runs/${encodeURIComponent(runIds.scenario_b)}`,
    "flight_recorder",
  );
  await assertRunTruth(flight.page, flightSnapshot, "Flight Recorder escalated run");
  const flightHtml = await flight.page.content();
  required(
    !/#f8fafc|#f7f3ea|#334155|rgba\(255,\s*255,\s*255,\s*\.(?:3|4|5|6|7|8|9)/i.test(
      flightHtml,
    ),
    "Flight Recorder rendered legacy light CSS",
  );
  const attemptIds = await flight.page
    .locator("[data-attempt-id]")
    .evaluateAll((rows) => rows.map((row) => row.getAttribute("data-attempt-id")));
  required(
    attemptIds.length === new Set(attemptIds).size,
    "Flight Recorder duplicate candidate rows",
  );
  const flightSummaryLayout = await flight.page
    .locator("#overview .summary-facts")
    .evaluate((element) => {
      const columns = getComputedStyle(element)
        .gridTemplateColumns.split(" ")
        .filter(Boolean).length;
      const articles = [...element.querySelectorAll("article")];
      return {
        columns,
        articleCount: articles.length,
        overflow: Math.max(
          0,
          ...articles.map((article) => article.scrollWidth - article.clientWidth),
        ),
        verticallySeparated: articles.every((article) => {
          const children = [...article.querySelectorAll("b,span,small")];
          return children.every((child, index) => {
            if (index === children.length - 1) return true;
            return (
              child.getBoundingClientRect().bottom <=
              children[index + 1].getBoundingClientRect().top + 0.5
            );
          });
        }),
      };
    });
  required(
    flightSummaryLayout.columns === 3 &&
      flightSummaryLayout.articleCount % flightSummaryLayout.columns === 0,
    "Flight Recorder overview leaves an incomplete metric-grid row",
  );
  required(
    flightSummaryLayout.verticallySeparated && flightSummaryLayout.overflow <= 1,
    "Flight Recorder overview metric labels overlap or overflow",
  );
  checks.flight_recorder_summary_layout = flightSummaryLayout;
  await capture(flight.page, "10-flight-recorder-overview.png", "#overview");
  await capture(
    flight.page,
    "11-replay-timeline.png",
    '[data-testid="replay-timeline"]',
  );
  await capture(flight.page, "12-event-stream.png", '[data-testid="event-stream"]');
  await capture(flight.page, "13-evidence-panel.png", '[data-testid="evidence-panel"]');
  await capture(flight.page, "14-file-activity.png", '[data-testid="file-activity"]');
  await capture(
    flight.page,
    "15-flight-candidate-comparison.png",
    '[data-testid="candidate-comparison"]',
  );
  await flight.context.close();

  const responsive1280 = await openPage("/fleet", "web_1280", {
    width: 1280,
    height: 800,
  });
  await responsive1280.page.locator("#overview").waitFor();
  const screenshot1280 = await capture(responsive1280.page, "16-overview-1280x800.png");
  required(
    screenshot1280.width === 1280 && screenshot1280.height === 800,
    `1280 responsive screenshot is ${screenshot1280.width}x${screenshot1280.height}`,
  );
  await responsive1280.context.close();

  const responsive1920 = await openPage("/fleet", "web_1920", {
    width: 1920,
    height: 1080,
  });
  await responsive1920.page.locator("#overview").waitFor();
  const screenshot1920 = await capture(
    responsive1920.page,
    "17-overview-1920x1080.png",
  );
  required(
    screenshot1920.width === 1920 && screenshot1920.height === 1080,
    `1920 responsive screenshot is ${screenshot1920.width}x${screenshot1920.height}`,
  );
  await responsive1920.context.close();
} catch (error) {
  errors.push({
    page: "release",
    kind: "assertion",
    message: error instanceof Error ? (error.stack ?? error.message) : String(error),
  });
} finally {
  await browser.close();
}

const summary = {
  status:
    errors.length || requests.length || screenshots.length !== 17 ? "failed" : "passed",
  browser: "chromium",
  viewport_coverage: [...viewportCoverage].sort(),
  screenshot_count: screenshots.length,
  screenshots,
  errors,
  failed_requests: requests,
  computed_style_checks: checks,
  villani_web_reconciliation: errors.some((item) =>
    String(item.page).startsWith("web_"),
  )
    ? "failed"
    : "passed",
  flight_recorder_reconciliation: errors.some((item) => item.page === "flight_recorder")
    ? "failed"
    : "passed",
};
await writeFile(
  resolve(artifacts, "browser-summary.json"),
  `${JSON.stringify(summary, null, 2)}\n`,
  "utf8",
);
if (summary.status !== "passed") {
  throw new Error(
    `connected browser verification failed: ${errors.length} errors, ${requests.length} failed requests, ${screenshots.length}/17 screenshots`,
  );
}
