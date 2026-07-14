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
const output = path.resolve(argument("--output"), "screenshots");
fs.mkdirSync(output, { recursive: true });

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
  await page.goto(pathToFileURL(transcript).href, { waitUntil: "load" });
  await page.locator("#setup").screenshot({ path: path.join(output, "01-setup-flow.png") });
  await page.locator("#doctor").screenshot({ path: path.join(output, "02-doctor.png") });

  await page.goto(consoleUrl, { waitUntil: "networkidle" });
  await page.getByText("Villani Console", { exact: true }).waitFor();
  await page.screenshot({ path: path.join(output, "03-villani-console.png"), fullPage: true });
} finally {
  await browser.close();
}
