import { describe, it, expect } from "vitest";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { execFile } from "node:child_process";
import vm from "node:vm";
import { JSDOM } from "jsdom";
import { promisify } from "node:util";
import { readJsonl } from "../src/utils/jsonl.js";
import { parseClaudeSession } from "../src/providers/claude.js";
import { parseCodexSession } from "../src/providers/codex.js";
import { parsePiSession } from "../src/providers/pi.js";
import { parseGeneric } from "../src/providers/generic.js";
import { redactString } from "../src/redaction/redact.js";
import { buildGitReplay } from "../src/git/gitReplay.js";
import { renderReplay } from "../src/render/renderReplay.js";
import { defaultRoots, findSessions } from "../src/scanners/findSessions.js";
import { appendHook } from "../src/hooks/installHooks.js";
const exec = promisify(execFile);
const fx = (p: string) => path.resolve("test/fixtures", p);

describe("provider parsers", () => {
  it("jsonl invalid lines warn and unknown valid records are preserved", async () => {
    const s = await parseGeneric("unknown", fx("generic/mixed-invalid.jsonl"));
    expect(s.warnings[0]).toContain("Line 2");
    expect(s.events.some((e) => e.type === "unknown")).toBe(true);
  });
  it("claude extracts messages, tools, results, hooks", async () => {
    const s = await parseClaudeSession(fx("claude/realistic-transcript.jsonl"));
    expect(s.sessionId).toBe("claude-realistic-1");
    expect(s.model).toContain("claude");
    expect(
      s.events.some(
        (e) => e.type === "user_message" && e.summary === "Fix the tests",
      ),
    ).toBe(true);
    expect(
      s.events.some(
        (e) => e.type === "assistant_message" && e.summary?.includes("inspect"),
      ),
    ).toBe(true);
    expect(
      s.events.some((e) => e.type === "test_run" && e.command === "npm test"),
    ).toBe(true);
    expect(
      s.events.some((e) => e.type === "file_read" && e.path === "src/index.ts"),
    ).toBe(true);
    expect(
      s.events.some((e) => e.type === "file_edit" && e.path === "src/index.ts"),
    ).toBe(true);
    expect(
      s.events.some((e) => e.type === "file_write" && e.path === "src/new.ts"),
    ).toBe(true);
    expect(
      s.events.some(
        (e) =>
          e.type === "test_run" &&
          e.command === "npm test" &&
          e.exitCode === 1 &&
          e.summary === "Tests failed",
      ),
    ).toBe(true);
    expect(s.events.some((e) => e.title === "Searched with Grep")).toBe(true);
    expect(
      s.events.some((e) => e.title === "Unknown Claude event: message_delta"),
    ).toBe(true);
    const h = await parseClaudeSession(fx("claude/hook-payload.jsonl"));
    expect(h.events[0].title).toBe("Claude hook: PostToolUse Bash");
  });
  it("codex extracts metadata, shell, apply_patch, approvals, hooks", async () => {
    const s = await parseCodexSession(fx("codex/realistic-rollout.jsonl"));
    expect(s.sessionId).toBe("sess_123");
    expect(s.model).toBe("gpt-5-codex");
    expect(
      s.events.some((e) => e.type === "test_run" && e.command === "npm test"),
    ).toBe(true);
    expect(
      s.events.some((e) => e.type === "file_edit" && e.path === "src/index.ts"),
    ).toBe(true);
    expect(s.events.some((e) => e.type === "approval")).toBe(true);
    expect(
      s.events.some(
        (e) => e.title === "Unknown Codex event: response_item_done",
      ),
    ).toBe(true);
    const h = await parseCodexSession(fx("codex/hook-payload.jsonl"));
    expect(h.events[0].title).toBe("Codex hook: PreToolUse bash");
  });
  it("pi extracts execution lifecycle, failures, summaries, model changes", async () => {
    const s = await parsePiSession(fx("pi/realistic-session.jsonl"));
    expect(s.sessionId).toBe("pi_123");
    expect(s.model).toBe("qwen3.6-27b");
    expect(
      s.events.some(
        (e) => e.type === "user_message" && e.summary === "Fix the repo",
      ),
    ).toBe(true);
    expect(s.events.some((e) => e.title === "Pi tool started: bash")).toBe(
      true,
    );
    expect(
      s.events.some((e) => e.type === "error" && e.command === "npm test"),
    ).toBe(true);
    expect(s.events.some((e) => e.title === "Branch summary")).toBe(true);
    expect(
      s.events.some(
        (e) => e.title === "Model changed: qwen3.5-9b to qwen3.6-27b",
      ),
    ).toBe(true);
    expect(
      s.events.some((e) => e.title === "Unknown Pi event: compaction"),
    ).toBe(true);
  });
});
