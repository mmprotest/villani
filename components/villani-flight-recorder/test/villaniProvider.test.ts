import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { JSDOM } from "jsdom";
import { describe, expect, it, vi } from "vitest";

import { launchVillaniRun } from "../src/commands/launchVillani.js";
import { scanToIndex } from "../src/index/sessionIndex.js";
import { parseVillaniRun, readVillaniJsonl } from "../src/providers/villani.js";
import { renderDashboard } from "../src/render/dashboard.js";
import { deriveReplayViewModel } from "../src/render/viewModel.js";
import { renderReplay } from "../src/render/renderReplay.js";
import { renderSessionBrowser } from "../src/render/sessionBrowser.js";
import { findVillaniRuns } from "../src/scanners/findVillaniRuns.js";
import {
  copyVillaniFixture,
  snapshotRunFiles,
} from "./helpers/villaniFixture.js";

async function updateJson(
  file: string,
  update: (value: Record<string, unknown>) => void,
) {
  const value = JSON.parse(await fs.readFile(file, "utf8")) as Record<
    string,
    unknown
  >;
  update(value);
  await fs.writeFile(file, `${JSON.stringify(value, null, 2)}\n`);
}

describe("native Villani provider", () => {
  it("accepts a valid final JSONL object without a trailing newline", async () => {
    const file = path.join(
      await fs.mkdtemp(path.join(os.tmpdir(), "vfr-jsonl-")),
      "events.jsonl",
    );
    await fs.writeFile(file, '{"ok":true}');
    await expect(readVillaniJsonl(file)).resolves.toMatchObject({
      values: [{ ok: true }],
      warnings: [],
    });
  });

  it("ignores only a genuinely truncated final JSONL object", async () => {
    const file = path.join(
      await fs.mkdtemp(path.join(os.tmpdir(), "vfr-jsonl-")),
      "events.jsonl",
    );
    await fs.writeFile(file, '{"ok":true}\n{"partial":');
    await expect(readVillaniJsonl(file)).resolves.toMatchObject({
      values: [{ ok: true }],
      warnings: [expect.stringContaining("truncated")],
    });
  });

  it("reports a complete malformed final JSON object", async () => {
    const file = path.join(
      await fs.mkdtemp(path.join(os.tmpdir(), "vfr-jsonl-")),
      "events.jsonl",
    );
    await fs.writeFile(file, '{"ok":true}\n{"broken":}');
    await expect(readVillaniJsonl(file)).rejects.toThrow(/malformed JSONL/);
  });

  it("detects and parses the complete canonical fixture without writing to it", async () => {
    const fixture = await copyVillaniFixture();
    const before = await snapshotRunFiles(fixture.run);
    const found = await findVillaniRuns([fixture.root]);
    expect(found).toHaveLength(1);
    expect(found[0]).toMatchObject({ runId: "run_protocol_fixture" });

    const session = await parseVillaniRun(fixture.run);
    expect(session.sessionId).toBe("run_protocol_fixture");
    expect(session.events).toHaveLength(24);
    expect(session.villani?.attempts).toHaveLength(2);
    expect(session.villani?.verifications).toHaveLength(2);
    expect(session.villani?.selection?.selected_candidate_ids).toEqual([
      "attempt_002",
    ]);
    expect(await snapshotRunFiles(fixture.run)).toEqual(before);
  });

  it("uses VILLANI_HOME for the default canonical runs root", async () => {
    const fixture = await copyVillaniFixture();
    const home = await fs.mkdtemp(path.join(os.tmpdir(), "villani-home-"));
    const runs = path.join(home, "runs");
    await fs.mkdir(runs);
    await fs.cp(fixture.run, path.join(runs, "run_protocol_fixture"), {
      recursive: true,
    });
    vi.stubEnv("VILLANI_HOME", home);
    try {
      expect(await findVillaniRuns()).toEqual([
        expect.objectContaining({
          runId: "run_protocol_fixture",
          runPath: path.join(runs, "run_protocol_fixture"),
        }),
      ]);
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("preserves canonical event identity and parent correlation", async () => {
    const { run } = await copyVillaniFixture();
    const event = (await parseVillaniRun(run)).events[2]!;
    expect(event).toMatchObject({
      id: "evt_003",
      eventId: "evt_003",
      runId: "run_protocol_fixture",
      traceId: "trace_protocol_fixture",
      attemptId: null,
      parentEventId: "evt_002",
      sequence: 3,
    });
  });

  it("renders controller policy timeline in canonical sequence order", async () => {
    const { run } = await copyVillaniFixture();
    const session = await parseVillaniRun(run);
    session.events.reverse();
    const timeline = deriveReplayViewModel(session, null).timeline;
    expect(timeline.map((event) => event.raw.sequence)).toEqual(
      Array.from({ length: 24 }, (_, index) => index + 1),
    );
    expect(timeline.map((event) => event.title)).toEqual(
      expect.arrayContaining([
        "Classification completed",
        "Policy decision recorded",
        "Escalation selected",
        "Candidate selected",
        "Materialization completed",
      ]),
    );
  });

  it("renders two candidate rows with exact eligibility and selected ID", async () => {
    const { run } = await copyVillaniFixture();
    const html = renderDashboard(await parseVillaniRun(run), null);
    const document = new JSDOM(html).window.document;
    const rows = [...document.querySelectorAll(".candidate-table tbody tr")];
    expect(rows).toHaveLength(2);
    expect(rows[0]?.getAttribute("data-attempt-id")).toBe("attempt_001");
    expect(rows[0]?.textContent).toContain("No");
    expect(rows[0]?.textContent).toContain("rejected");
    expect(rows[1]?.getAttribute("data-attempt-id")).toBe("attempt_002");
    expect(rows[1]?.textContent).toContain("Selected");
    expect(rows[1]?.textContent).toContain("accepted");
  });

  it("renders exact canonical token, duration, and cost values", async () => {
    const { run } = await copyVillaniFixture();
    const view = deriveReplayViewModel(await parseVillaniRun(run), null);
    expect(view.villani?.aggregate).toMatchObject({
      inputTokens: 180,
      outputTokens: 95,
      durationMs: 9000,
      costUsd: 0.05,
      costAccountingStatus: "complete",
      fileReads: null,
    });
    expect(view.metrics.find((metric) => metric.id === "tokens")?.value).toBe(
      "275",
    );
    expect(view.metrics.find((metric) => metric.id === "duration")?.value).toBe(
      "9.00s",
    );
    expect(view.metrics.find((metric) => metric.id === "cost")?.value).toBe(
      "USD 0.05",
    );
  });

  it("renders canonical run browser row values without metric inference", async () => {
    const fixture = await copyVillaniFixture();
    const indexDir = await fs.mkdtemp(
      path.join(os.tmpdir(), "vfr-browser-index-"),
    );
    const result = await scanToIndex({
      agent: "villani",
      roots: [fixture.root],
      indexDir,
      rebuild: true,
    });
    const document = new JSDOM(renderSessionBrowser(result.index), {
      runScripts: "dangerously",
      url: "file:///tmp/villani-runs.html",
    }).window.document;
    const row = document.querySelector("[data-id='run_protocol_fixture']");
    expect(row?.textContent).toContain("COMPLETED");
    expect(row?.textContent).toContain(
      "Correct the calculator add function and preserve the existing API.",
    );
    expect(row?.textContent).toContain("/fixture/repository");
    expect(row?.textContent).toContain("2 attempts");
    expect(row?.textContent).toContain("fixture-large");
    expect(row?.textContent).toContain("275 tokens");
    expect(row?.textContent).toContain("9s duration");
    expect(row?.textContent).toContain("USD 0.05 cost");
  });

  it("renders null cost as unknown rather than zero", async () => {
    const { run } = await copyVillaniFixture();
    await updateJson(path.join(run, "manifest.json"), (manifest) => {
      manifest.total_cost_usd = null;
      manifest.cost_accounting_status = "unknown";
    });
    const view = deriveReplayViewModel(await parseVillaniRun(run), null);
    const cost = view.metrics.find((metric) => metric.id === "cost");
    expect(cost).toMatchObject({ value: "Unknown", empty: true });
    expect(cost?.value).not.toBe("$0.00");
  });

  it("continues indexing and attaches a readable error after a corrupt run", async () => {
    const fixture = await copyVillaniFixture();
    const corrupt = path.join(fixture.root, "run_corrupt");
    await fs.mkdir(corrupt);
    await fs.writeFile(path.join(corrupt, "manifest.json"), "{bad json\n");
    await fs.writeFile(path.join(corrupt, "state.json"), "{}\n");
    await fs.writeFile(path.join(corrupt, "events.jsonl"), "");
    const indexDir = await fs.mkdtemp(path.join(os.tmpdir(), "vfr-index-"));
    const result = await scanToIndex({
      agent: "villani",
      roots: [fixture.root],
      indexDir,
      rebuild: true,
    });
    expect(result.index.sessions).toHaveLength(2);
    expect(
      result.index.sessions.find(
        (session) => session.id === "run_protocol_fixture",
      ),
    ).toMatchObject({ provider: "villani", outcome: "success" });
    const broken = result.index.sessions.find(
      (session) => session.id === "run_corrupt",
    );
    expect(broken).toMatchObject({
      provider: "villani",
      state: "corrupt",
      outcome: "failed",
    });
    expect(broken?.failureSummary).toContain("manifest.json could not be read");
  });

  it("renders an unknown future event generically and keeps it inspectable", async () => {
    const { run } = await copyVillaniFixture();
    const future = {
      schema_version: "villani.event.v1",
      event_id: "evt_025",
      sequence: 25,
      timestamp: "2026-07-10T00:00:25Z",
      trace_id: "trace_protocol_fixture",
      run_id: "run_protocol_fixture",
      attempt_id: null,
      parent_event_id: "evt_024",
      source: "controller",
      event_type: "future_controller_signal",
      payload: { future_value: 7 },
    };
    await fs.appendFile(
      path.join(run, "events.jsonl"),
      `${JSON.stringify(future)}\n`,
    );
    const session = await parseVillaniRun(run);
    expect(session.events.at(-1)).toMatchObject({
      type: "unknown",
      title: "Unknown Villani event: future_controller_signal",
      eventId: "evt_025",
      sequence: 25,
      raw: future,
    });
    expect(deriveReplayViewModel(session, null).timeline.at(-1)?.title).toBe(
      "Unknown Villani event: future_controller_signal",
    );
  });

  it("redacts a fake API key from canonical events and artifacts", async () => {
    const { run } = await copyVillaniFixture();
    const secret = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890";
    await fs.appendFile(
      path.join(run, "attempts", "attempt_002", "stdout.log"),
      `\nOPENAI_API_KEY=${secret}\n`,
    );
    const events = path.join(run, "events.jsonl");
    const content = await fs.readFile(events, "utf8");
    await fs.writeFile(
      events,
      content.replace('"exit_code":0}', `"exit_code":0,"api_key":"${secret}"}`),
    );
    const out = path.join(
      await fs.mkdtemp(path.join(os.tmpdir(), "vfr-redacted-")),
      "run.html",
    );
    await renderReplay(await parseVillaniRun(run), { out });
    const html = await fs.readFile(out, "utf8");
    expect(html).not.toContain(secret);
    expect(html).toContain("REDACTED");
  });

  it("launches a requested Villani run with injected browser opening", async () => {
    const fixture = await copyVillaniFixture();
    const before = await snapshotRunFiles(fixture.run);
    const out = path.join(
      await fs.mkdtemp(path.join(os.tmpdir(), "vfr-launch-")),
      "requested.html",
    );
    const open = vi.fn();
    const file = await launchVillaniRun(
      {
        root: fixture.root,
        runId: "run_protocol_fixture",
        out,
      },
      { open },
    );
    expect(file).toBe(out);
    expect(open).toHaveBeenCalledOnce();
    expect(open).toHaveBeenCalledWith(out);
    expect(await fs.readFile(out, "utf8")).toContain("Candidate comparison");
    expect(await snapshotRunFiles(fixture.run)).toEqual(before);
    await expect(
      launchVillaniRun(
        {
          root: fixture.root,
          runId: "run_protocol_fixture",
          out: path.join(fixture.root, "forbidden.html"),
        },
        { open },
      ),
    ).rejects.toThrow("outside the canonical runs root");
    await expect(
      fs.stat(path.join(fixture.root, "forbidden.html")),
    ).rejects.toThrow();
  });

  it("uses protocol-tolerant JSONL rules for a truncated final record only", async () => {
    const { run } = await copyVillaniFixture();
    const events = path.join(run, "events.jsonl");
    await fs.appendFile(events, '{"schema_version":"villani.event.v1"');
    const tolerant = await parseVillaniRun(run);
    expect(tolerant.events).toHaveLength(24);
    expect(tolerant.warnings[0]).toContain("truncated final JSONL line");

    const second = await copyVillaniFixture();
    const original = await fs.readFile(
      path.join(second.run, "events.jsonl"),
      "utf8",
    );
    await fs.writeFile(
      path.join(second.run, "events.jsonl"),
      `${original.split(/\r?\n/)[0]}\n{bad}\n${original.split(/\r?\n/).slice(1).join("\n")}`,
    );
    await expect(parseVillaniRun(second.run)).rejects.toThrow(
      "malformed JSONL at line 2",
    );
  });
});
