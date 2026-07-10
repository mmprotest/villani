import { describe, expect, it } from "vitest";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { scanToIndex } from "../src/index/sessionIndex.js";
import { readIndex } from "../src/index/sessionStore.js";
import { renderReplay } from "../src/render/renderReplay.js";
import { adaptersFor } from "../src/providers/providerAdapter.js";

const fx = (p: string) => path.join(process.cwd(), "test/fixtures", p);

describe("session index flow", () => {
  it("does not duplicate stable sessions across rescans", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-index-"));
    const firstResult = await scanToIndex({
      all: true,
      roots: [fx("claude")],
      indexDir: dir,
    });
    const first = (await readIndex(dir))!.sessions.length;
    expect(firstResult.parsedNew).toBe(first);

    const secondResult = await scanToIndex({
      all: true,
      roots: [fx("claude")],
      indexDir: dir,
    });
    const second = (await readIndex(dir))!.sessions.length;
    expect(second).toBe(first);
    expect(secondResult.skippedUnchanged).toBe(first);
    expect(secondResult.parsedNew + secondResult.parsedChanged).toBe(0);

    const rebuildResult = await scanToIndex({
      all: true,
      roots: [fx("claude")],
      indexDir: dir,
      rebuild: true,
    });
    expect(rebuildResult.skippedUnchanged).toBe(0);
    expect(rebuildResult.parsedChanged).toBe(first);
  });
  it("uses generic only as fallback during --all scans", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-fallback-"));
    const res = await scanToIndex({
      all: true,
      roots: [fx("")],
      indexDir: dir,
    });
    const byPath = new Map<string, Set<string>>();
    for (const s of res.index.sessions) {
      const set = byPath.get(s.sourcePath) ?? new Set<string>();
      set.add(String(s.provider));
      byPath.set(s.sourcePath, set);
    }
    for (const set of byPath.values()) expect(set.size).toBe(1);
    expect(res.index.sessions.some((s) => s.provider === "claude")).toBe(true);
    expect(res.index.sessions.some((s) => s.provider === "codex")).toBe(true);
    expect(res.index.sessions.some((s) => s.provider === "pi")).toBe(true);
    expect(res.index.sessions.some((s) => s.provider === "generic")).toBe(true);
  });
  it("replays an indexed session by id data", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-replay-id-"));
    const res = await scanToIndex({
      all: true,
      roots: [fx("claude")],
      indexDir: dir,
    });
    const rec = res.index.sessions.find((s) => s.provider === "claude")!;
    const ad = adaptersFor(String(rec.provider))[0];
    const session = await ad.parse({
      provider: rec.provider,
      sourcePath: rec.sourcePath,
      sourceKind: "file",
      confidence: rec.confidence,
      reason: "test",
    });
    const out = path.join(dir, "replay.html");
    await renderReplay(session as any, { out });
    const html = await fs.readFile(out, "utf8");
    expect(html).toContain("Claude");
  });
});
