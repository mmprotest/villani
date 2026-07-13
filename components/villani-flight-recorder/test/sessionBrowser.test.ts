import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";
import { renderSessionBrowser } from "../src/render/sessionBrowser.js";
import { SessionIndex } from "../src/index/sessionTypes.js";

function idx(sessions: SessionIndex["sessions"]): SessionIndex {
  return {
    version: 1,
    generatedAt: "now",
    sessions,
    taskSegments: [],
    repos: [],
    warnings: [],
  };
}
const base = {
  sourceKind: "file" as const,
  sourceType: "transcript" as const,
  eventCount: 2,
  repoRoots: [],
  repoIds: [],
  taskSegmentIds: [],
  commandCount: 0,
  failedCommandCount: 0,
  fileEventCount: 0,
  warningCount: 0,
  fingerprint: {},
  confidence: "high" as const,
  warnings: [],
  indexedAt: "now",
  sourcePath: "/tmp/a.jsonl",
};

describe("session browser", () => {
  it("renders sessions", () => {
    const html = renderSessionBrowser(
      idx([
        {
          ...base,
          id: "c1",
          provider: "claude",
          providerLabel: "Claude",
          outcome: "success",
          title: "Claude run",
        },
        {
          ...base,
          id: "x1",
          provider: "codex",
          providerLabel: "Codex",
          outcome: "failed",
          title: "Codex run",
        },
      ]),
    );
    expect(html).toContain("Villani Flight Recorder");
    expect(html).toContain("Local coding-agent sessions");
    expect(html).toContain("Claude");
    expect(html).toContain("Codex");
    expect(html).toContain("Open Replay");
  });
  it("includes filters", () => {
    const html = renderSessionBrowser(idx([]));
    for (const text of [
      "All Agents",
      "Claude",
      "Codex",
      "Pi",
      "Generic",
      "Failed",
      "Success",
      "Unknown",
    ])
      expect(html).toContain(text);
  });
  it("escapes session content", () => {
    const html = renderSessionBrowser(
      idx([
        {
          ...base,
          id: "bad",
          provider: "generic",
          providerLabel: "Generic",
          title: '<script>alert("x")</script>',
        },
      ]),
    );
    expect(html).toContain("\\u003cscript");
    expect(html).not.toContain('<script>alert("x")</script>');
  });
  it("uses the shared monochrome Villani theme and browser interactions", () => {
    const html = renderSessionBrowser(
      idx([
        {
          ...base,
          id: "c1",
          provider: "claude",
          providerLabel: "Claude",
          outcome: "success",
          title: "Claude run",
          updatedAt: "2026-01-02T00:00:00.000Z",
          changedFiles: ["src/a.ts"],
        },
        {
          ...base,
          id: "x1",
          provider: "codex",
          providerLabel: "Codex",
          outcome: "failed",
          title: "Codex run",
          updatedAt: "2026-01-01T00:00:00.000Z",
          failedCommandCount: 2,
        },
      ]),
    );
    expect(html).toContain("Local investigation index");
    expect(html).toContain("--villani-bg-deepest:#050505");
    expect(html).toContain("background:var(--villani-bg-deepest)");
    expect(html).toContain("color-scheme:dark");
    expect(html).not.toContain("background: linear-gradient(180deg, #eef1f3");
    expect(html).not.toContain("#0f172a");
    expect(html).not.toContain("#071020");
    expect(html).not.toContain("linear-gradient(135deg,#09111f,#17213a)");
    expect(html).not.toContain("color-scheme:light dark");
    expect(html).toContain("provider-badge");
    expect(html).toContain("outcome-pill");
  });

  it("includes clear filters, active summary, freshness, pagination, and empty-state copy", () => {
    const html = renderSessionBrowser(
      idx([
        {
          ...base,
          id: "safe",
          provider: "claude",
          providerLabel: "Claude",
          outcome: "success",
          title: "Safe run",
          changedFiles: ["src/safe.ts"],
          model: "claude-test",
        },
        {
          ...base,
          id: "bad",
          provider: "generic",
          providerLabel: "Generic",
          outcome: "failed",
          title: '<img src=x onerror="alert(1)">',
        },
      ]),
    );
    expect(html).toContain('id="preview"');
    expect(html).toContain("Session preview");
    expect(html).toContain("Open Replay");
    expect(html).toContain("replayCommand");
    expect(html).toContain("Showing ");
    expect(html).toContain(" of ");
    expect(html).toContain(" sessions");
    expect(html).toContain("Clear filters");
    expect(html).toContain('id="activeFilterSummary"');
    expect(html).toContain("No filters active");
    expect(html).toContain("No sessions match the current filters.");
    expect(html).toContain("Clear filters to show all indexed sessions.");
    expect(html).toContain("Show more");
    expect(html).toContain("matching sessions");
    expect(html).toContain(
      "Open Replay links point to replay HTML generated for this browser snapshot",
    );
    expect(html).toContain("Replay generated for this browser snapshot");
    expect(html).toContain("Updated newest first");
    expect(html).toContain("Failed commands");
    expect(html).toContain("src/safe.ts");
    expect(html).not.toContain('<img src=x onerror="alert(1)">');
  });

  it("resets filters and pagination in the browser script", async () => {
    const sessions = Array.from({ length: 105 }, (_, i) => ({
      ...base,
      id: `s${i}`,
      provider: i === 104 ? ("codex" as const) : ("claude" as const),
      providerLabel: i === 104 ? "Codex" : "Claude",
      outcome: i === 104 ? ("failed" as const) : ("success" as const),
      title: `Run ${i}`,
      updatedAt: `2026-01-${String((i % 28) + 1).padStart(2, "0")}T00:00:00.000Z`,
    }));
    const dom = new JSDOM(renderSessionBrowser(idx(sessions)), {
      runScripts: "dangerously",
      url: "file:///tmp/sessions.html",
    });
    const doc = dom.window.document;
    expect(doc.querySelectorAll(".session")).toHaveLength(100);
    expect(doc.getElementById("resultCount")?.textContent).toContain(
      "Showing 100 of 105 matching sessions · 105 indexed",
    );
    doc
      .getElementById("showMore")
      ?.dispatchEvent(new dom.window.Event("click"));
    expect(doc.querySelectorAll(".session")).toHaveLength(105);
    (doc.getElementById("search") as HTMLInputElement).value = "no-match";
    doc.getElementById("search")?.dispatchEvent(new dom.window.Event("input"));
    expect(doc.getElementById("activeFilterSummary")?.textContent).toContain(
      "Search “no-match”",
    );
    expect(doc.getElementById("emptyFilter")?.classList.contains("show")).toBe(
      true,
    );
    doc
      .getElementById("clearFilters")
      ?.dispatchEvent(new dom.window.Event("click"));
    expect((doc.getElementById("search") as HTMLInputElement).value).toBe("");
    expect(
      (doc.getElementById("outcomeFilter") as HTMLSelectElement).value,
    ).toBe("all");
    expect((doc.getElementById("sort") as HTMLSelectElement).value).toBe(
      "updated-desc",
    );
    expect(doc.getElementById("activeFilterSummary")?.textContent).toBe(
      "No filters active",
    );
    expect(doc.querySelectorAll(".session")).toHaveLength(100);
  });

  it("uses dedicated layout fields for updated time and counts", () => {
    const html = renderSessionBrowser(
      idx([
        {
          ...base,
          id: "long-layout",
          provider: "claude",
          providerLabel: "Claude",
          outcome: "failed",
          title:
            "Investigate a very long replay title that should stay readable without pushing the timestamp into the event counts",
          updatedAt: "2026-07-02T18:41:00.000Z",
          eventCount: 123,
          failedCommandCount: 4,
          changedFileCount: 9,
          failureSummary: "npm test failed with exit code 1",
          projectPath:
            "/tmp/a/very/long/source/path/that/should/truncate/safely/without/expanding/the/session/row",
          sourcePath:
            "/tmp/a/very/long/source/path/that/should/truncate/safely/without/crowding/events/session.jsonl",
        },
      ]),
    );
    expect(html).toContain("session-updated");
    expect(html).toContain("session-events");
    expect(html).toContain("session-failed");
    expect(html).toContain("session-files");
    expect(html).toContain("session-action");
    expect(html).toContain("grid-template-columns:minmax(92px,112px)");
    expect(html).toContain("minmax(158px,176px)");
    expect(html).toContain("minmax(72px,84px)");
    expect(html).toContain("session-project");
    expect(html).toContain("session-source");
  });

  it("does not render duplicated generic unknown titles", () => {
    const html = renderSessionBrowser(
      idx([
        {
          ...base,
          id: "generic",
          provider: "generic",
          providerLabel: "Generic",
          title: "Unknown event: custom",
        },
      ]),
    );
    expect(html).not.toContain("Unknown Unknown");
    expect(html).toContain("Unknown event: custom");
  });

  it("renders empty state", () => {
    const html = renderSessionBrowser(idx([]));
    expect(html).toContain("No sessions indexed yet");
    expect(html).toContain("Run vfr scan");
  });
});

it("renders robust session rows instead of the fragile column layout", () => {
  const html = renderSessionBrowser(
    idx([
      {
        ...base,
        id: "robust",
        provider: "claude",
        providerLabel: "Claude",
        outcome: "failed",
        title: "Long title",
      } as any,
    ]),
  );
  expect(html).toContain("session-row-main");
  expect(html).toContain("session-row-meta");
  expect(html).toContain("session-row-action");
  expect(html).toContain("session-row-title");
  expect(html).toContain("session-row-source");
  expect(html).toContain("overflow-wrap:anywhere");
});
