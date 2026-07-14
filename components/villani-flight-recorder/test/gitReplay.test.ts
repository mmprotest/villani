import { afterEach, describe, expect, it } from "vitest";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { buildGitReplay } from "../src/git/gitReplay.js";
import {
  activeChildProcessCount,
  ChildProcessFailure,
  runChildProcess,
} from "../src/utils/childProcess.js";

afterEach(() => {
  expect(activeChildProcessCount()).toBe(0);
});

describe("git replay", () => {
  it.each([1, 2, 3])(
    "works repeatedly in isolated git repo %i and CLI invalid input exits nonzero",
    async (iteration) => {
      const d = await fs.mkdtemp(
        path.join(os.tmpdir(), `vfr-git-replay-${process.pid}-${iteration}-`),
      );
      const boundedExec = (file: string, args: string[]) =>
        runChildProcess(file, args, { cwd: d, timeoutMs: 10_000 });
      try {
        await boundedExec("git", ["init"]);
        await fs.writeFile(path.join(d, "a.test.ts"), "a");
        await boundedExec("git", ["add", "."]);
        await boundedExec("git", [
          "-c",
          "user.email=a@b.c",
          "-c",
          "user.name=A",
          "commit",
          "-m",
          "first",
        ]);
        await fs.writeFile(path.join(d, "package.json"), "{}");
        await boundedExec("git", ["add", "."]);
        await boundedExec("git", [
          "-c",
          "user.email=a@b.c",
          "-c",
          "user.name=A",
          "commit",
          "-m",
          "second",
        ]);
        const replay = await buildGitReplay("HEAD~1", "HEAD", d);
        expect(
          replay.events.some((event) =>
            event.summary?.includes("dependency file changed"),
          ),
        ).toBe(true);
        const failure = await runChildProcess(
          process.execPath,
          [path.resolve("dist/cli.js"), "replay"],
          { cwd: process.cwd(), timeoutMs: 5_000 },
        ).catch((error: unknown) => error);
        expect(failure).toBeInstanceOf(ChildProcessFailure);
        const diagnostics = (failure as ChildProcessFailure).diagnostics;
        expect(diagnostics).toMatchObject({
          exitStatus: 1,
          processState: "exited",
          timedOut: false,
        });
        expect(diagnostics.stderr).toContain("replay requires");
        expect(diagnostics.elapsedMs).toBeGreaterThanOrEqual(0);
      } finally {
        await fs.rm(d, {
          recursive: true,
          force: true,
          maxRetries: 5,
          retryDelay: 100,
        });
      }
    },
    20_000,
  );
});
