import { describe, expect, it } from "vitest";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { buildGitReplay } from "../src/git/gitReplay.js";
const exec = promisify(execFile);

describe("git replay", () => {
  it.each([1, 2, 3])(
    "works repeatedly in isolated git repo %i and CLI invalid input exits nonzero",
    async () => {
      const d = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-"));
      const boundedExec = (file: string, args: string[]) =>
        exec(file, args, { cwd: d, timeout: 10_000, windowsHide: true });
      try {
        await boundedExec("git", ["init"]);
        await boundedExec("git", ["config", "user.email", "a@b.c"]);
        await boundedExec("git", ["config", "user.name", "A"]);
        await fs.writeFile(path.join(d, "a.test.ts"), "a");
        await boundedExec("git", ["add", "."]);
        await boundedExec("git", ["commit", "-m", "first"]);
        await fs.writeFile(path.join(d, "package.json"), "{}");
        await boundedExec("git", ["add", "."]);
        await boundedExec("git", ["commit", "-m", "second"]);
        const replay = await buildGitReplay("HEAD~1", "HEAD", d);
        expect(
          replay.events.some((event) =>
            event.summary?.includes("dependency file changed"),
          ),
        ).toBe(true);
        await expect(
          exec("node", ["dist/cli.js", "replay"], {
            cwd: process.cwd(),
            timeout: 10_000,
            windowsHide: true,
          }),
        ).rejects.toBeTruthy();
      } finally {
        await fs.rm(d, { recursive: true, force: true });
      }
    },
  );
});
