import { describe, expect, it } from "vitest";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const exec = promisify(execFile);
const fx = (p: string) => path.resolve("test/fixtures", p);
const build = async () => {
  const npmCli = process.env.npm_execpath;
  if (!npmCli) throw new Error("npm_execpath is required for CLI build tests");
  await exec(process.execPath, [npmCli, "run", "build"]);
};

describe("CLI replay output workflow", () => {
  it("replay --session --out <dir> writes index.html to the requested directory", async () => {
    await build();
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-out-"));
    const out = path.join(dir, "artifact");
    const { stdout } = await exec("node", [
      "dist/cli.js",
      "replay",
      "--session",
      fx("codex/realistic-rollout.jsonl"),
      "--provider",
      "codex",
      "--out",
      out,
    ]);
    const htmlPath = path.join(out, "index.html");
    expect(stdout.trim()).toBe(htmlPath);
    expect(await fs.readFile(htmlPath, "utf8")).toContain(
      "Captured run outcome",
    );
  }, 20000);

  it("replay --latest --root searches the custom root", async () => {
    await build();
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-latest-"));
    const { stdout } = await exec("node", [
      "dist/cli.js",
      "replay",
      "--provider",
      "codex",
      "--latest",
      "--root",
      fx("codex"),
      "--out",
      dir,
    ]);
    expect(stdout).toContain("Provider: codex");
    expect(stdout).toContain(`Root searched: ${fx("codex")}`);
    expect(stdout).toContain("Selected session:");
    expect(stdout).toContain(`Replay written: ${path.join(dir, "index.html")}`);
    expect(await fs.readFile(path.join(dir, "index.html"), "utf8")).toContain(
      "Codex",
    );
  }, 20000);

  it("replay --latest --root strictly filters provider sessions", async () => {
    await build();
    for (const provider of ["codex", "claude", "pi"] as const) {
      const dir = await fs.mkdtemp(
        path.join(os.tmpdir(), `vfr-latest-${provider}-`),
      );
      const { stdout } = await exec("node", [
        "dist/cli.js",
        "replay",
        "--provider",
        provider,
        "--latest",
        "--root",
        fx(""),
        "--out",
        dir,
      ]);
      expect(stdout).toContain(`Provider: ${provider}`);
      const selected =
        stdout
          .split("\n")
          .find((line) => line.startsWith("Selected session:")) ?? "";
      expect(selected).toContain(`${path.sep}${provider}${path.sep}`);
      for (const other of ["codex", "claude", "pi"].filter(
        (p) => p !== provider,
      ))
        expect(selected).not.toContain(`${path.sep}${other}${path.sep}`);
    }
  }, 30000);
});
