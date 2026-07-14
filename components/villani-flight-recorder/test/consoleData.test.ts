import fs from "node:fs/promises";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { consoleIndex, consoleReplay } from "../src/console/consoleData.js";
import {
  consoleRedirectHtml,
  villaniConsoleUrl,
} from "../src/console/consoleUrl.js";
import { scanToIndex } from "../src/index/sessionIndex.js";
import {
  activeChildProcessCount,
  runChildProcess,
} from "../src/utils/childProcess.js";
import { copyVillaniFixture } from "./helpers/villaniFixture.js";
import { testResources } from "./helpers/testResources.js";

afterEach(() => vi.unstubAllEnvs());

describe("Villani Console data adapter", () => {
  it("indexes canonical runs without exposing parser filesystem paths", async () => {
    const fixture = await copyVillaniFixture();
    const indexDir =
      await testResources.temporaryDirectory("vfr-console-index-");
    const document = await consoleIndex({
      indexDir,
      roots: [fixture.root],
      refresh: true,
    });
    expect(document.schema_version).toBe("villani.console.history.v1");
    expect(document.entries).toEqual([
      expect.objectContaining({
        id: "run_protocol_fixture",
        logical_id: "run_protocol_fixture",
        kind: "run",
        source: "villani",
        synchronization_state: "LOCAL",
        deep_link: "/console/runs/run_protocol_fixture",
      }),
    ]);
    expect(JSON.stringify(document)).not.toContain(fixture.root);
    expect(JSON.stringify(document)).not.toContain("sourcePath");
  });

  it("projects canonical replay into every Console panel and stable deep links", async () => {
    const fixture = await copyVillaniFixture();
    const indexDir = await testResources.temporaryDirectory(
      "vfr-console-replay-",
    );
    await scanToIndex({
      agent: "villani",
      roots: [fixture.root],
      indexDir,
      rebuild: true,
    });
    const replay = await consoleReplay({
      id: "run_protocol_fixture",
      kind: "run",
      indexDir,
      runsRoot: fixture.root,
    });
    expect(replay).toMatchObject({
      schema_version: "villani.console.replay.v1",
      id: "run_protocol_fixture",
      kind: "run",
      synchronization_state: "LOCAL",
      summary: {
        model: "fixture-large",
        total_tokens: 275,
        total_cost: 0.05,
      },
      verification: { outcome: "accepted" },
      deep_links: { self: "/console/runs/run_protocol_fixture" },
    });
    expect(replay.events).toHaveLength(24);
    expect(replay.attempts).toHaveLength(2);
    expect(replay.candidate_comparison).toHaveLength(2);
    expect(replay.files.length).toBeGreaterThan(0);
    expect(replay.logs.length).toBeGreaterThan(0);
    expect(replay.events[0]?.deep_link).toMatch(
      /^\/console\/runs\/run_protocol_fixture\/events\//,
    );
    expect(replay.attempts[0]?.deep_link).toBe(
      "/console/runs/run_protocol_fixture/attempts/attempt_001",
    );
  });

  it("projects imported provider sessions through the same replay contract", async () => {
    const indexDir = await testResources.temporaryDirectory(
      "vfr-console-import-",
    );
    await scanToIndex({
      agent: "claude",
      roots: [path.resolve("test/fixtures/claude")],
      indexDir,
      rebuild: true,
    });
    const history = await consoleIndex({ indexDir });
    const entry = history.entries.find((item) => item.source === "claude");
    expect(entry).toBeDefined();
    const replay = await consoleReplay({
      id: entry!.id,
      kind: "session",
      indexDir,
    });
    expect(replay.kind).toBe("session");
    expect(replay.source).toBe("claude");
    expect(replay.verification).toEqual(
      expect.objectContaining({ outcome: "not_applicable" }),
    );
    expect(JSON.stringify(replay)).not.toContain("sourcePath");
  });
});

describe("Flight Recorder compatibility links", () => {
  it("resolve only to the running loopback Villani Console", async () => {
    const home = await testResources.temporaryDirectory("vfr-console-url-");
    await fs.mkdir(path.join(home, "agentd"));
    await fs.writeFile(
      path.join(home, "agentd", "endpoint.json"),
      JSON.stringify({ endpoint: "http://127.0.0.1:7411" }),
    );
    vi.stubEnv("VILLANI_HOME", home);
    const url = await villaniConsoleUrl("/console/history");
    expect(url).toBe("http://127.0.0.1:7411/console/history");
    const html = consoleRedirectHtml(url);
    expect(html).toContain("Villani Console compatibility link");
    expect(html).not.toContain("Villani Flight Recorder");
  });

  it("refuses a non-loopback service endpoint", async () => {
    const home = await testResources.temporaryDirectory("vfr-console-unsafe-");
    await fs.mkdir(path.join(home, "agentd"));
    await fs.writeFile(
      path.join(home, "agentd", "endpoint.json"),
      JSON.stringify({ endpoint: "https://example.com" }),
    );
    vi.stubEnv("VILLANI_HOME", home);
    await expect(villaniConsoleUrl()).rejects.toThrow("safe loopback");
  });

  it("routes browse and run launch commands into the single Console", async () => {
    const home = await testResources.temporaryDirectory("vfr-console-cli-");
    await fs.mkdir(path.join(home, "agentd"));
    await fs.writeFile(
      path.join(home, "agentd", "endpoint.json"),
      JSON.stringify({ endpoint: "http://127.0.0.1:7411" }),
    );
    const env = { ...process.env, VILLANI_HOME: home };
    const browse = await runChildProcess(
      process.execPath,
      ["dist/cli.js", "browse"],
      { cwd: path.resolve("."), env },
    );
    expect(browse.stdout).toContain("http://127.0.0.1:7411/console/history");
    const replay = await runChildProcess(
      process.execPath,
      [
        "dist/cli.js",
        "launch",
        "--provider",
        "villani",
        "--run-id",
        "run_1",
        "--no-open",
      ],
      { cwd: path.resolve("."), env },
    );
    expect(replay.stdout).toContain(
      "http://127.0.0.1:7411/console/runs/run_1/replay",
    );
    expect(activeChildProcessCount()).toBe(0);
  });
});
