#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "../components/villani-web/node_modules/@playwright/test/index.mjs";

function argument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || !process.argv[index + 1]) {
    throw new Error(`missing ${name}`);
  }
  return process.argv[index + 1];
}

const transcript = path.resolve(argument("--transcript"));
const consoleUrl = argument("--console-url");
const runId = argument("--run-id");
const output = path.resolve(argument("--output"), "screenshots");
fs.mkdirSync(output, { recursive: true });

async function waitForVisible(page, locator, label) {
  try {
    await locator.waitFor();
  } catch (error) {
    const root = page.locator("#root");
    const diagnostic = {
      label,
      url: page.url(),
      title: await page.title(),
      root_children: await root.locator(":scope > *").count(),
      root_html_length: (await root.innerHTML()).length,
      body_text_length: (await page.locator("body").innerText()).length,
    };
    console.error(
      `Browser rendering diagnostic: ${JSON.stringify(diagnostic)}`,
    );
    await page.screenshot({
      path: path.join(output, "browser-failure.png"),
      fullPage: true,
    });
    throw error;
  }
}

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 1,
  });
  page.on("console", (message) =>
    console.error(`Browser console ${message.type()}: ${message.text()}`),
  );
  page.on("pageerror", (error) =>
    console.error(`Browser page error: ${error.name}: ${error.message}`),
  );
  page.on("requestfailed", (request) =>
    console.error(
      `Browser request failed: ${request.method()} ${request.url()} (${request.failure()?.errorText ?? "unknown"})`,
    ),
  );
  await page.goto(pathToFileURL(transcript).href, { waitUntil: "load" });
  await page
    .locator("#setup")
    .screenshot({ path: path.join(output, "01-setup-flow.png") });
  await page
    .locator("#doctor")
    .screenshot({ path: path.join(output, "02-doctor.png") });

  await page.goto(consoleUrl, { waitUntil: "networkidle" });
  await waitForVisible(
    page,
    page.locator('[data-testid="shared-app-shell"]'),
    "Villani Console shell",
  );
  await page.screenshot({
    path: path.join(output, "03-villani-console.png"),
    fullPage: true,
  });

  const consoleBase = new URL(consoleUrl).origin;
  await page.goto(`${consoleBase}/console/runs/${encodeURIComponent(runId)}`, {
    waitUntil: "networkidle",
  });
  await waitForVisible(
    page,
    page.getByText(runId, { exact: false }).first(),
    "sample run detail",
  );
  await page.locator("#summary").screenshot({
    path: path.join(output, "04-sample-run.png"),
  });

  await page.goto(
    `${consoleBase}/console/runs/${encodeURIComponent(runId)}/replay`,
    {
      waitUntil: "networkidle",
    },
  );
  await waitForVisible(
    page,
    page.locator('[data-testid="console-replay"]'),
    "sample replay",
  );
  await page.screenshot({
    path: path.join(output, "05-sample-replay.png"),
    fullPage: true,
  });
  if (
    fs
      .readFileSync(path.join(output, "04-sample-run.png"))
      .equals(fs.readFileSync(path.join(output, "05-sample-replay.png")))
  ) {
    throw new Error("sample run and replay screenshots are identical");
  }
} finally {
  await browser.close();
}
