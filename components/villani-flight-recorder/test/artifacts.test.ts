import { describe, expect, it } from "vitest";
import { event } from "../src/normalize/events.js";
import { renderDashboard } from "../src/render/dashboard.js";
import {
  changedFilesFromEvents,
  diffFromEvents,
} from "../src/render/deriveDetails.js";
import { ParsedSession } from "../src/providers/types.js";
import { testResources } from "./helpers/testResources.js";

const session = (events: ParsedSession["events"]): ParsedSession => ({
  provider: "git",
  sessionPath: "test",
  events,
  warnings: [],
});
const textForTab = (html: string, tab: string) => {
  const dom = testResources.dom(html, { runScripts: "dangerously" });
  const doc = dom.window.document;
  doc.querySelector<HTMLElement>(`[data-tab="${tab}"]`)?.click();
  return doc.querySelector("#detailContent")?.textContent ?? "";
};

describe("artifact extraction and detail tabs", () => {
  it("renders event-derived git files and diff instead of Not captured", () => {
    const patch = "diff --git a/README.md b/README.md\n+hello";
    const html = renderDashboard(
      session([
        event("g1", "git", "git_commit", "Commit update", {
          files: ["README.md", "app.js"],
          diff: patch,
        }),
      ]),
      null,
    );
    const changedFiles = textForTab(html, "Changed Files");
    const diff = textForTab(html, "Diff");
    expect(changedFiles).toContain("README.md");
    expect(changedFiles).toContain("app.js");
    expect(diff).toContain("diff --git");
    expect(diff).not.toContain("Not captured");
  });

  it("deduplicates identical event diff blocks", () => {
    const patch = "diff --git a/README.md b/README.md\n+hello";
    const diff = diffFromEvents([
      event("g1", "git", "git_commit", "Commit one", {}, { diff: patch }),
      event("g2", "git", "git_commit", "Commit two", {}, { diff: patch }),
    ]);
    expect(diff.match(/diff --git/g)).toHaveLength(1);
  });

  it("prefers final range diff when available", () => {
    const commitPatch = "diff --git a/app.js b/app.js\n+commit";
    const rangePatch = "diff --git a/README.md b/README.md\n+range";
    expect(
      diffFromEvents([
        event("g1", "git", "git_commit", "Commit", {}, { diff: commitPatch }),
        event("g2", "git", "diff", "Final diff", {}, { diff: rangePatch }),
      ]),
    ).toBe(rangePatch);
  });

  it("extracts changed transcript write/edit paths without treating reads as changes", () => {
    expect(
      changedFilesFromEvents([
        event("r", "claude", "file_read", "Read", {}, { path: "src/read.ts" }),
        event(
          "w",
          "claude",
          "file_write",
          "Write",
          {},
          { path: "src/write.ts" },
        ),
        event("e", "claude", "file_edit", "Edit", {}, { path: "src/edit.ts" }),
      ]),
    ).toEqual(["src/write.ts", "src/edit.ts"]);
  });

  it("shows Not captured when no artifact data exists", () => {
    const html = renderDashboard(
      session([event("m", "git", "user_message", "No artifacts")]),
      null,
    );
    expect(textForTab(html, "Changed Files")).toContain("Not captured");
    expect(textForTab(html, "Diff")).toContain("Not captured");
  });
});
