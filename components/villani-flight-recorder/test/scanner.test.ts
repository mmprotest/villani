import { describe, expect, it } from "vitest";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { defaultRoots, findSessions } from "../src/scanners/findSessions.js";
import { appendHook } from "../src/hooks/installHooks.js";
const fx = (p: string) => path.resolve("test/fixtures", p);

describe("scanner and hooks", () => {
  it("scan --root auto-detects providers and respects custom CODEX_HOME", async () => {
    const auto = await findSessions({ roots: [fx("claude")] });
    expect(auto.some((x) => x.provider === "claude")).toBe(true);
    const s = await findSessions({ roots: [fx("claude")], provider: "claude" });
    expect(s.every((x) => x.provider === "claude")).toBe(true);
    const old = process.env.CODEX_HOME;
    process.env.CODEX_HOME = path.join(os.tmpdir(), "not_named_home");
    expect(defaultRoots("codex")[0].root).toContain("not_named_home");
    process.env.CODEX_HOME = old;
  });

  it("hook ingestion writes jsonl safely and rejects invalid json", async () => {
    const d = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-hook-"));
    const file = await appendHook("claude", '{"session_id":"abc","x":1}\n', d);
    expect(file).toContain(".villani-flight-recorder");
    expect(await fs.readFile(file, "utf8")).toContain('"provider":"claude"');
    await expect(appendHook("claude", "not-json", d)).rejects.toThrow(
      /Invalid JSON/,
    );
  });
});

const providers = ["codex", "claude", "pi"] as const;

describe("strict provider scanning", () => {
  for (const provider of providers) {
    it(`scan --provider ${provider} only returns confident ${provider} sessions`, async () => {
      const sessions = await findSessions({ roots: [fx("")], provider });
      expect(sessions.length).toBeGreaterThan(0);
      expect(sessions.every((s) => s.provider === provider)).toBe(true);
      expect(
        sessions.every((s) =>
          s.path.includes(`${path.sep}${provider}${path.sep}`),
        ),
      ).toBe(true);
      for (const other of providers.filter((p) => p !== provider)) {
        expect(
          sessions.some((s) =>
            s.path.includes(`${path.sep}${other}${path.sep}`),
          ),
        ).toBe(false);
      }
    });
  }
});
