import fs from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { scanToIndex } from "../src/index/sessionIndex.js";
import { readIndex } from "../src/index/sessionStore.js";
import { renderReplay } from "../src/render/renderReplay.js";
import { testResources } from "./helpers/testResources.js";

describe("session index launch flow", () => {
  it("scans fixture roots, stores sessions, tasks, and replays an indexed segment", async () => {
    const dir = await testResources.temporaryDirectory("vfr-index-");
    const result = await scanToIndex({
      agent: "claude",
      roots: ["test/fixtures/claude"],
      indexDir: dir,
    });
    expect(result.index.sessions.length).toBeGreaterThan(0);
    expect(result.index.taskSegments.length).toBeGreaterThan(0);
    expect((await readIndex(dir))?.sessions.length).toBe(
      result.index.sessions.length,
    );
    const rec = result.index.sessions[0];
    const seg = result.index.taskSegments.find((t) => t.sessionId === rec.id)!;
    const { claudeAdapter } =
      await import("../src/providers/providerAdapter.js");
    const parsed = await claudeAdapter.parse({
      provider: rec.provider,
      sourcePath: rec.sourcePath,
      sourceKind: "file",
      confidence: rec.confidence,
      reason: "test",
    });
    parsed.events = parsed.events.slice(
      seg.startEventIndex,
      seg.endEventIndex + 1,
    );
    const out = await renderReplay(
      { ...parsed, sessionPath: rec.sourcePath, path: rec.sourcePath },
      { out: path.join(dir, "replay.html") },
    );
    expect(await fs.readFile(out, "utf8")).toContain("Villani Flight Recorder");
  });
  it("extracts clean title, write-only changed files, duration, and failure summary", async () => {
    const dir = await testResources.temporaryDirectory("vfr-index-meta-");
    const root = await testResources.temporaryDirectory("vfr-project-");
    const fixtureDir = path.join(dir, "sessions");
    await fs.mkdir(fixtureDir);
    const file = path.join(fixtureDir, "generic.jsonl");
    await fs.writeFile(
      file,
      [
        JSON.stringify({
          type: "user",
          message: "  Fix the bug   now  ",
          cwd: root,
          timestamp: "2026-01-01T00:00:00.000Z",
        }),
        JSON.stringify({ type: "read", path: "src/read-only.ts", cwd: root }),
        JSON.stringify({ type: "write", path: "src/changed.ts", cwd: root }),
        JSON.stringify({
          type: "command",
          command: "npm test",
          exitCode: 1,
          cwd: root,
          durationMs: 25,
        }),
      ].join("\n"),
    );
    const result = await scanToIndex({
      agent: "generic",
      roots: [fixtureDir],
      indexDir: dir,
    });
    const rec = result.index.sessions[0];
    expect(rec.title).toBe("Fix the bug now");
    expect(rec.projectName).toBe(path.basename(root));
    expect(rec.changedFiles).toEqual(["src/changed.ts"]);
    expect(rec.failureSummary).toBe("npm test failed with exit code 1");
    expect(rec.durationMs).toBe(25);
  });
});
