import { describe, expect, it } from "vitest";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  extractTokenUsage,
  sumTokenUsage,
} from "../src/providers/helpers/tokens.js";
import { parseClaudeSession } from "../src/providers/claude.js";
import { parseGeneric } from "../src/providers/generic.js";
import { clientScript } from "../src/render/clientScript.js";
import { renderSessionBrowser } from "../src/render/sessionBrowser.js";
import { deriveMetrics } from "../src/render/deriveMetrics.js";
import { SessionIndex } from "../src/index/sessionTypes.js";

async function fixture(lines: unknown[]) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-tokens-"));
  const file = path.join(dir, "session.jsonl");
  await fs.writeFile(file, lines.map((l) => JSON.stringify(l)).join("\n"));
  return file;
}

describe("token telemetry", () => {
  it("extracts Claude and OpenAI/Codex style usage", () => {
    expect(
      extractTokenUsage({
        message: { usage: { input_tokens: 10, output_tokens: 5 } },
      })?.totalTokens,
    ).toBe(15);
    expect(
      extractTokenUsage({
        usage: {
          prompt_tokens: "7",
          completion_tokens: 3,
          reasoning_tokens: 2,
        },
      }),
    ).toMatchObject({
      inputTokens: 7,
      outputTokens: 3,
      reasoningTokens: 2,
      totalTokens: 12,
    });
  });

  it("attaches Claude usage once when assistant text and tool_use split children exist", async () => {
    const file = await fixture([
      {
        type: "assistant",
        session_id: "s",
        message: {
          role: "assistant",
          model: "claude-3",
          usage: { input_tokens: 100, output_tokens: 20 },
          content: [
            { type: "text", text: "I'll inspect." },
            {
              type: "tool_use",
              id: "toolu_1",
              name: "Bash",
              input: { command: "pwd" },
            },
          ],
        },
      },
    ]);
    const s = await parseClaudeSession(file);
    expect(
      s.events.find((e) => e.type === "assistant_message")?.tokenUsage
        ?.totalTokens,
    ).toBe(120);
    expect(sumTokenUsage(s.events)?.totalTokens).toBe(120);
  });

  it("attaches Claude usage to first tool child when no text event is emitted", async () => {
    const file = await fixture([
      {
        type: "assistant",
        session_id: "s",
        message: {
          role: "assistant",
          model: "claude-3",
          usage: { input_tokens: 9, output_tokens: 1 },
          content: [
            {
              type: "tool_use",
              id: "toolu_1",
              name: "Bash",
              input: { command: "pwd" },
            },
          ],
        },
      },
    ]);
    const s = await parseClaudeSession(file);
    expect(s.events).toHaveLength(1);
    expect(s.events[0].tokenUsage?.totalTokens).toBe(10);
  });

  it("attaches generic usage and renders replay/event token states", async () => {
    const file = await fixture([
      { type: "assistant", content: "hello", usage: { total_tokens: 42 } },
      { type: "user", content: "no usage" },
    ]);
    const s = await parseGeneric("unknown", file);
    expect(s.events[0].tokenUsage?.totalTokens).toBe(42);
    expect(
      deriveMetrics(
        s,
        { label: "ok", tone: "success", reason: "ok" },
        { label: "ok", tone: "success", reason: "ok" },
      ).find((m) => m.id === "tokens")?.value,
    ).toBe("42");
    const script = clientScript();
    expect(script).toContain("Token usage");
    expect(script).toContain("Not captured for this event.");
  });

  it("renders session browser token count", () => {
    const index: SessionIndex = {
      version: 1,
      generatedAt: "now",
      warnings: [],
      repos: [],
      taskSegments: [],
      sessions: [
        {
          id: "s",
          provider: "generic",
          providerLabel: "Generic",
          sourcePath: "/tmp/s.jsonl",
          sourceKind: "file",
          eventCount: 2,
          repoRoots: [],
          repoIds: [],
          taskSegmentIds: [],
          commandCount: 0,
          failedCommandCount: 0,
          fileEventCount: 0,
          warningCount: 0,
          fingerprint: {},
          confidence: "high",
          warnings: [],
          tokenCount: 193000,
        },
      ],
    };
    expect(renderSessionBrowser(index)).toContain("193k");
  });
});
