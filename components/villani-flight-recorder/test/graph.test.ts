import { describe, expect, it } from "vitest";
import path from "node:path";
import { parseClaudeSession } from "../src/providers/claude.js";
import { parsePiSession } from "../src/providers/pi.js";
import { deriveExecutionGraph } from "../src/render/deriveGraph.js";
const fx = (p: string) => path.resolve("test/fixtures", p);

describe("execution graph", () => {
  it("contains lane graph nodes and labels without old phases", async () => {
    const s = await parseClaudeSession(fx("claude/realistic-transcript.jsonl"));
    const graph = deriveExecutionGraph({
      session: s,
      git: null,
      outputWritten: true,
      htmlValid: true,
    });
    expect(graph.laneLabels).toEqual([
      "Recorder Pipeline",
      "Captured Run",
      "Repository",
    ]);
    expect(graph.nodes.map((n) => n.title)).toEqual([
      "Discover",
      "Parse",
      "Normalize",
      "Agent Events",
      "Commands",
      "File Changes",
      "Correlate",
      "Git State",
      "Diff Capture",
      "Replay Output",
    ]);
    expect(graph.links.map((l) => `${l.from}:${l.to}`)).toContain(
      "normalize:replay-output",
    );
    expect(graph.links.map((l) => `${l.from}:${l.to}`)).not.toContain(
      "correlate:replay-output",
    );
    expect(graph.nodes.find((n) => n.id === "commands")?.subtitle).toBe(
      "1 failed test",
    );
    expect(graph.nodes.find((n) => n.title === "Validate")).toBeUndefined();
    expect(graph.nodes.find((n) => n.title === "Review")).toBeUndefined();
    expect(graph.nodes.find((n) => n.title === "Finalize")).toBeUndefined();
  });

  it("keeps graph visible copy compact", async () => {
    const s = await parseClaudeSession(fx("claude/realistic-transcript.jsonl"));
    const graph = deriveExecutionGraph({
      session: s,
      git: null,
      outputWritten: true,
      htmlValid: true,
    });
    const maxSubtitleLength = 24;
    for (const node of graph.nodes) {
      expect(node.title.length).toBeLessThanOrEqual(18);
      if (node.subtitle) {
        expect(node.subtitle.length).toBeLessThanOrEqual(maxSubtitleLength);
      }
    }
    const commandsNode = graph.nodes.find((n) => n.id === "commands");
    expect(commandsNode?.title).toBe("Commands");
    expect(commandsNode?.subtitle).toMatch(
      /Tools and tests|failed test|failed cmd|No commands|N\/A/,
    );
    expect(commandsNode?.title).not.toContain("/");
    expect(commandsNode?.subtitle).not.toContain("/");
  });

  it("does not spread captured command failures to unrelated nodes or links", async () => {
    const s = await parsePiSession(fx("pi/realistic-session.jsonl"));
    const graph = deriveExecutionGraph({
      session: s,
      git: null,
      outputWritten: true,
      htmlValid: true,
    });
    const node = (id: string) => graph.nodes.find((n) => n.id === id);
    const link = (id: string) => graph.links.find((l) => l.id === id);
    expect(node("commands")?.status).toBe("failed");
    expect(node("replay-output")?.status).not.toBe("failed");
    expect(node("file-changes")?.status).not.toBe("failed");
    expect(node("git-state")?.status).not.toBe("failed");
    expect(link("commands-file-changes")?.status).not.toBe("failed");
    expect(link("commands-correlate")?.status).not.toBe("failed");
    expect(link("correlate-git-state")?.status).not.toBe("failed");
  });

  it("marks missing git repository data unavailable without blocking replay output", async () => {
    const s = await parseClaudeSession(fx("claude/realistic-transcript.jsonl"));
    const graph = deriveExecutionGraph({
      session: s,
      git: null,
      outputWritten: true,
      htmlValid: true,
    });
    expect(graph.nodes.find((n) => n.id === "git-state")?.severity).toBe(
      "unavailable",
    );
    expect(graph.nodes.find((n) => n.id === "diff-capture")?.severity).toBe(
      "unavailable",
    );
    expect(
      graph.nodes.find((n) => n.id === "replay-output")?.status,
    ).not.toMatch(/failed|pending/);
    expect(
      graph.links.find((l) => l.id === "normalize-replay-output")?.status,
    ).toMatch(/completed|warning/);
  });

  it("dims captured-run nodes for git-only replay", async () => {
    const graph = deriveExecutionGraph({
      session: {
        provider: "git",
        events: [
          {
            id: "g1",
            type: "git_commit",
            title: "Commit",
            summary: "Repository reconstruction",
          },
        ],
        warnings: [],
      },
      git: {
        head: "abcdef123456",
        status: "clean",
        diff: "diff --git a/a b/a",
        diffStat: "1 file changed",
      },
      outputWritten: true,
      htmlValid: true,
    });
    expect(graph.nodes.find((n) => n.id === "agent-events")?.laneTone).toBe(
      "dimmed",
    );
    expect(graph.nodes.find((n) => n.id === "commands")?.laneTone).toBe(
      "dimmed",
    );
    expect(graph.nodes.find((n) => n.id === "git-state")?.laneTone).toBe(
      "emphasis",
    );
  });
});
