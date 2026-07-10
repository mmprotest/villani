import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { parseClaudeSession } from "../src/providers/claude.js";
import { deriveCapturedRunStatus } from "../src/render/deriveCapturedRunStatus.js";

async function parseLines(lines: unknown[]) {
  const dir = await mkdtemp(path.join(os.tmpdir(), "vfr-claude-"));
  const file = path.join(dir, "session.jsonl");
  await writeFile(file, lines.map((l) => JSON.stringify(l)).join("\n"));
  return parseClaudeSession(file);
}

describe("Claude tool_result correlation", () => {
  it("correlates failed Bash tool results back to test commands", async () => {
    const session = await parseLines([
      {
        type: "assistant",
        message: {
          role: "assistant",
          content: [
            {
              type: "tool_use",
              id: "toolu_test_1",
              name: "Bash",
              input: { command: "npm test" },
            },
          ],
        },
        timestamp: "2026-07-01T01:00:00Z",
        session_id: "claude-correlation-1",
      },
      {
        type: "user",
        message: {
          role: "user",
          content: [
            {
              type: "tool_result",
              tool_use_id: "toolu_test_1",
              content: "Tests failed",
              is_error: true,
            },
          ],
        },
        timestamp: "2026-07-01T01:00:01Z",
        session_id: "claude-correlation-1",
      },
    ]);
    expect(
      session.events.some(
        (e) =>
          e.type === "test_run" &&
          e.command === "npm test" &&
          e.exitCode === 1 &&
          /failed/i.test(e.title),
      ),
    ).toBe(true);
    const captured = deriveCapturedRunStatus(session.events);
    expect(captured.status).toBe("failed");
    expect(captured.failedCommands).toBe(1);
    expect(captured.failedTests).toBe(1);
    expect(captured.totalCommands).toBe(1);
    expect(captured.totalTests).toBe(1);
  });

  it("correlates non-error Bash results without marking failure", async () => {
    const session = await parseLines([
      {
        type: "assistant",
        message: {
          role: "assistant",
          content: [
            {
              type: "tool_use",
              id: "toolu_test_2",
              name: "Bash",
              input: { command: "npm test" },
            },
          ],
        },
      },
      {
        type: "user",
        message: {
          role: "user",
          content: [
            {
              type: "tool_result",
              tool_use_id: "toolu_test_2",
              content: "Tests passed",
              is_error: false,
            },
          ],
        },
      },
    ]);
    expect(
      session.events.some((e) => e.command === "npm test" && e.exitCode === 1),
    ).toBe(false);
  });

  it("preserves unmatched failed tool results with warnings", async () => {
    const session = await parseLines([
      {
        type: "user",
        message: {
          role: "user",
          content: [
            {
              type: "tool_result",
              tool_use_id: "missing_from_map",
              content: "Tool failed",
              is_error: true,
            },
          ],
        },
      },
    ]);
    const event = session.events.find((e) => e.type === "tool_result");
    expect(event).toBeTruthy();
    expect(event?.warnings?.join(" ")).toMatch(/unmatched|error/i);
  });
});
