import { describe, expect, it } from "vitest";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { buildGitReplay } from "../src/git/gitReplay.js";
const exec = promisify(execFile);

describe("git replay", () => {
  it("works in a temporary git repo and CLI invalid input exits nonzero", async () => {
    const d = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-"));
    await exec("git", ["init"], { cwd: d });
    await exec("git", ["config", "user.email", "a@b.c"], { cwd: d });
    await exec("git", ["config", "user.name", "A"], { cwd: d });
    await fs.writeFile(path.join(d, "a.test.ts"), "a");
    await exec("git", ["add", "."], { cwd: d });
    await exec("git", ["commit", "-m", "first"], { cwd: d });
    await fs.writeFile(path.join(d, "package.json"), "{}");
    await exec("git", ["add", "."], { cwd: d });
    await exec("git", ["commit", "-m", "second"], { cwd: d });
    const s = await buildGitReplay("HEAD~1", "HEAD", d);
    expect(
      s.events.some((e) => e.summary?.includes("dependency file changed")),
    ).toBe(true);
    await expect(
      exec("node", ["dist/cli.js", "replay"], { cwd: process.cwd() }),
    ).rejects.toBeTruthy();
  });
});
