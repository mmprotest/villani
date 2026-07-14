import { describe, expect, it } from "vitest";
import fs from "node:fs/promises";
import path from "node:path";
import { parseClaudeSession } from "../src/providers/claude.js";
import { renderReplayForTest } from "./renderReplayForTest.js";
import { testResources } from "./helpers/testResources.js";
const fx = (p: string) => path.resolve("test/fixtures", p);

describe("client interactions", () => {
  it("supports timeline clicks, Raw JSON, and Redaction tabs", async () => {
    const s = await parseClaudeSession(fx("claude/realistic-transcript.jsonl"));
    const html = await fs.readFile(await renderReplayForTest(s), "utf8");
    const dom = testResources.dom(html, { runScripts: "dangerously" });
    dom.window.document
      .querySelectorAll<HTMLElement>(".timeline-row")[1]
      .click();
    expect(
      dom.window.document.querySelector("#detailContent")?.textContent,
    ).toContain("Source");
    expect(dom.window.document.querySelector(".coverage-row")).toBeFalsy();
    dom.window.document
      .querySelector<HTMLElement>('[data-tab="Raw JSON"]')
      ?.click();
    expect(
      dom.window.document.querySelector("#detailContent pre")?.textContent,
    ).toContain("title");
    dom.window.document
      .querySelector<HTMLElement>('[data-tab="Redaction"]')
      ?.click();
    expect(
      dom.window.document.querySelector("#detailContent")?.textContent,
    ).toMatch(/No redactions applied|redact/i);
  });
});
