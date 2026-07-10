import { describe, expect, it } from "vitest";
import { parseClaudeSession } from "../src/providers/claude.js";
import { buildGitReplay } from "../src/git/gitReplay.js";
import { deriveCapturedRunStatus } from "../src/render/deriveCapturedRunStatus.js";
import { deriveReplayStatus } from "../src/render/deriveReplayStatus.js";
import { deriveExecutionGraph } from "../src/render/deriveGraph.js";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const exec = promisify(execFile);
const fx = (p: string) => path.resolve("test/fixtures", p);

describe("status derivation", () => {
  it("keeps replay status separate from captured failures", async () => {
    const s = await parseClaudeSession(fx("claude/realistic-transcript.jsonl"));
    const captured = deriveCapturedRunStatus(s.events);
    const replay = deriveReplayStatus({
      events: s.events,
      warnings: [],
      unknownEventsCount: 0,
      outputWritten: true,
      htmlValidated: true,
    });
    expect(captured.status).toBe("failed");
    expect(replay.status).toBe("generated");
    const graph = deriveExecutionGraph({
      session: s,
      git: null,
      replayStatus: replay,
      capturedRunStatus: captured,
    });
    expect(graph.nodes.find((n) => n.id === "commands")?.status).toBe("failed");
    expect(graph.nodes.find((n) => n.id === "replay-output")?.status).not.toBe(
      "failed",
    );
  });

  it("git-only captured status is not applicable", async () => {
    const d = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-status-"));
    await exec("git", ["init"], { cwd: d });
    await exec("git", ["config", "user.email", "a@b.c"], { cwd: d });
    await exec("git", ["config", "user.name", "A"], { cwd: d });
    await fs.writeFile(path.join(d, "a.txt"), "a");
    await exec("git", ["add", "."], { cwd: d });
    await exec("git", ["commit", "-m", "first"], { cwd: d });
    await fs.writeFile(path.join(d, "a.txt"), "b");
    await exec("git", ["commit", "-am", "second"], { cwd: d });
    const s = await buildGitReplay("HEAD~1", "HEAD", d);
    expect(deriveCapturedRunStatus(s.events).status).toBe("not_applicable");
  });
});
