#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

import { parseVillaniRun } from "../dist/providers/villani.js";
import { defaultVillaniSchemaValidator } from "../dist/providers/villaniSchemaValidation.js";

const componentRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const repositoryRoot = path.resolve(componentRoot, "..", "..");
const fixtureRoot = path.join(
  repositoryRoot,
  "integration",
  "fixtures",
  "protocol",
  "v1",
  "valid_run",
);
const outputRoot = path.resolve(
  process.env.VFR_CONCURRENCY_DIAGNOSTICS_DIR ??
    path.join(componentRoot, "test-results", "concurrency-stress"),
);
const workerCount = 20;
const iterationTimeoutMs = Number(
  process.env.VFR_CONCURRENCY_STRESS_ITERATION_TIMEOUT_MS ?? "4500",
);
const trackedRoots = new Set();
const trackedChildren = new Set();
const trackedTimers = new Set();

let fixtureFilesPromise;

function activeHandles() {
  const getHandles = process._getActiveHandles;
  if (typeof getHandles !== "function") return [];
  return getHandles
    .call(process)
    .map((handle) => handle?.constructor?.name ?? typeof handle);
}

async function fixtureFiles() {
  if (fixtureFilesPromise) return fixtureFilesPromise;
  fixtureFilesPromise = (async () => {
    const files = [];
    const visit = async (directory) => {
      const entries = await fs.readdir(directory, { withFileTypes: true });
      for (const entry of entries.sort((left, right) =>
        left.name.localeCompare(right.name),
      )) {
        const child = path.join(directory, entry.name);
        if (entry.isDirectory()) await visit(child);
        else if (entry.isFile())
          files.push({
            relativePath: path.relative(fixtureRoot, child),
            content: await fs.readFile(child),
          });
      }
    };
    await visit(fixtureRoot);
    return files;
  })();
  return fixtureFilesPromise;
}

function protocolDocuments(files) {
  const documents = [];
  const add = (value) => {
    if (
      value !== null &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      "schema_version" in value
    )
      documents.push(value);
  };
  for (const file of files) {
    const text = file.content.toString("utf8");
    if (file.relativePath.endsWith(".json")) add(JSON.parse(text));
    else if (file.relativePath.endsWith(".jsonl"))
      for (const line of text.split(/\r?\n/))
        if (line.trim()) add(JSON.parse(line));
  }
  return documents;
}

function expectedInventory(files) {
  return files
    .map(
      (file) =>
        `${file.relativePath.replaceAll(path.sep, "/")}:${file.content.length}:${crypto
          .createHash("sha256")
          .update(file.content)
          .digest("hex")}`,
    )
    .sort();
}

async function digestRun(run) {
  const rows = [];
  const visit = async (directory) => {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      const child = path.join(directory, entry.name);
      if (entry.isDirectory()) await visit(child);
      else if (entry.isFile()) {
        const content = await fs.readFile(child);
        rows.push(
          `${path.relative(run, child).replaceAll(path.sep, "/")}:${content.length}:${crypto
            .createHash("sha256")
            .update(content)
            .digest("hex")}`,
        );
      }
    }
  };
  await visit(run);
  return rows.sort();
}

async function materialize(run, files) {
  for (const file of files) {
    const destination = path.join(run, file.relativePath);
    await fs.mkdir(path.dirname(destination), { recursive: true });
    await fs.writeFile(destination, file.content);
  }
}

class IterationDiagnostics {
  records = [];
  active = new Map();

  constructor(label, iteration) {
    this.label = label;
    this.iteration = iteration;
    this.startedAt = new Date().toISOString();
  }

  async phase(workerIndex, phase, sourcePath, destinationPath, action) {
    const key = `${workerIndex}:${phase}`;
    const started = performance.now();
    const record = {
      workerIndex,
      phase,
      sourcePath,
      destinationPath,
      startedAt: new Date().toISOString(),
    };
    this.active.set(key, { ...record, started });
    try {
      const value = await action();
      this.records.push({
        ...record,
        finishedAt: new Date().toISOString(),
        elapsedMs: Math.round((performance.now() - started) * 1000) / 1000,
        status: "passed",
      });
      return value;
    } catch (error) {
      this.records.push({
        ...record,
        finishedAt: new Date().toISOString(),
        elapsedMs: Math.round((performance.now() - started) * 1000) / 1000,
        status: "failed",
        error:
          error instanceof Error
            ? `${error.name}: ${error.message}`
            : String(error),
      });
      throw error;
    } finally {
      this.active.delete(key);
    }
  }

  async write(status, error = null) {
    await fs.mkdir(outputRoot, { recursive: true });
    const now = performance.now();
    const document = {
      schemaVersion: "villani.flight_recorder.concurrency_stress.v1",
      label: this.label,
      iteration: this.iteration,
      pid: process.pid,
      nodeVersion: process.version,
      platform: process.platform,
      startedAt: this.startedAt,
      finishedAt: new Date().toISOString(),
      status,
      error:
        error === null
          ? null
          : error instanceof Error
            ? `${error.name}: ${error.message}`
            : String(error),
      phases: this.records,
      activePhases: [...this.active.values()].map((item) => ({
        workerIndex: item.workerIndex,
        phase: item.phase,
        sourcePath: item.sourcePath,
        destinationPath: item.destinationPath,
        startedAt: item.startedAt,
        elapsedMs: Math.round((now - item.started) * 1000) / 1000,
      })),
      activeHandles: activeHandles(),
      trackedResources: resourceState(),
    };
    const file = path.join(
      outputRoot,
      `${this.label}-iteration-${String(this.iteration).padStart(3, "0")}-pid-${process.pid}.json`,
    );
    await fs.writeFile(file, `${JSON.stringify(document, null, 2)}\n`, "utf8");
  }
}

function resourceState() {
  return {
    temporaryDirectories: trackedRoots.size,
    childProcesses: trackedChildren.size,
    timers: trackedTimers.size,
    jsdomWindows: 0,
    servers: 0,
    watchers: 0,
  };
}

function inventoriesEqual(left, right) {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

async function runIteration(label, iteration) {
  const diagnostics = new IterationDiagnostics(label, iteration);
  const files = await fixtureFiles();
  const expected = expectedInventory(files);
  const documents = protocolDocuments(files);
  const roots = [];
  let watchdog;
  const operation = (async () => {
    try {
      const validators = new Set();
      await Promise.all(
        Array.from({ length: workerCount }, async (_value, workerIndex) => {
          const root = await diagnostics.phase(
            workerIndex,
            "fixture_directory_creation",
            fixtureRoot,
            "pending",
            async () => {
              const value = await fs.mkdtemp(
                path.join(os.tmpdir(), "vfr-stress-"),
              );
              trackedRoots.add(value);
              roots.push(value);
              return value;
            },
          );
          const run = path.join(root, "run_protocol_fixture");
          await diagnostics.phase(
            workerIndex,
            "fixture_recursive_copy",
            fixtureRoot,
            run,
            () => materialize(run, files),
          );
          await diagnostics.phase(
            workerIndex,
            "fixture_integrity_check",
            fixtureRoot,
            run,
            async () => {
              if (!inventoriesEqual(await digestRun(run), expected))
                throw new Error("fixture integrity mismatch");
            },
          );
          const validator = await diagnostics.phase(
            workerIndex,
            "schema_validator_acquisition",
            fixtureRoot,
            run,
            () => defaultVillaniSchemaValidator(),
          );
          validators.add(validator);
          const parsedDocuments = await diagnostics.phase(
            workerIndex,
            "json_parsing",
            fixtureRoot,
            run,
            () => protocolDocuments(files),
          );
          await diagnostics.phase(
            workerIndex,
            "schema_validation",
            fixtureRoot,
            run,
            () => {
              for (const document of parsedDocuments)
                if (!validator.validate(document).valid)
                  throw new Error("schema validation failed");
            },
          );
          const session = await diagnostics.phase(
            workerIndex,
            "canonical_run_parsing",
            fixtureRoot,
            run,
            () => parseVillaniRun(run, validator),
          );
          const inventory = await diagnostics.phase(
            workerIndex,
            "digest_calculation",
            fixtureRoot,
            run,
            () => digestRun(run),
          );
          await diagnostics.phase(
            workerIndex,
            "assertion_completion",
            fixtureRoot,
            run,
            () => {
              if (
                session.sessionId !== "run_protocol_fixture" ||
                session.events.length !== 24
              )
                throw new Error("canonical parser assertion failed");
              if (!inventoriesEqual(inventory, expected))
                throw new Error("post-parse fixture digest changed");
            },
          );
        }),
      );
      if (validators.size !== 1)
        throw new Error("schema validator was initialized more than once");
      if (documents.length === 0)
        throw new Error("canonical protocol documents were not loaded");
    } finally {
      await Promise.all(
        roots.map((root, workerIndex) =>
          diagnostics.phase(
            workerIndex,
            "temporary_directory_cleanup",
            fixtureRoot,
            root,
            async () => {
              await fs.rm(root, { recursive: true, force: true });
              trackedRoots.delete(root);
            },
          ),
        ),
      );
    }
  })();
  try {
    await Promise.race([
      operation,
      new Promise((_, reject) => {
        watchdog = setTimeout(
          () => reject(new Error(`iteration exceeded ${iterationTimeoutMs}ms`)),
          iterationTimeoutMs,
        );
        trackedTimers.add(watchdog);
      }),
    ]);
    clearTimeout(watchdog);
    trackedTimers.delete(watchdog);
    await diagnostics.write("passed");
  } catch (error) {
    if (watchdog) {
      clearTimeout(watchdog);
      trackedTimers.delete(watchdog);
    }
    await diagnostics.write("failed", error);
    throw error;
  }
}

async function runFreshProcess(label, iteration, iterations) {
  const child = spawn(
    process.execPath,
    [fileURLToPath(import.meta.url), "--worker", label, String(iterations)],
    {
      cwd: componentRoot,
      env: { ...process.env, VFR_CONCURRENCY_DIAGNOSTICS_DIR: outputRoot },
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  trackedChildren.add(child);
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += chunk.toString("utf8");
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString("utf8");
  });
  let timer;
  let timedOut = false;
  let spawnError;
  const code = await new Promise((resolve) => {
    timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, 60_000);
    trackedTimers.add(timer);
    child.once("error", (error) => {
      spawnError = error;
      child.kill("SIGKILL");
    });
    child.once("close", resolve);
  }).finally(() => {
    clearTimeout(timer);
    trackedTimers.delete(timer);
    trackedChildren.delete(child);
  });
  if (timedOut)
    throw new Error(`fresh process ${iteration} exceeded 60 seconds`);
  if (spawnError) throw spawnError;
  if (code !== 0)
    throw new Error(
      `fresh process ${iteration} failed with ${code}\nstdout:\n${stdout}\nstderr:\n${stderr}`,
    );
}

async function writeSummary(mode, iterations, freshProcesses) {
  await fs.mkdir(outputRoot, { recursive: true });
  const resources = resourceState();
  const leaked = Object.values(resources).some((value) => value !== 0);
  const summary = {
    schemaVersion: "villani.flight_recorder.concurrency_stress_summary.v1",
    status: leaked ? "failed" : "passed",
    mode,
    nodeVersion: process.version,
    platform: process.platform,
    workerCount,
    inProcessIterations: iterations,
    freshProcesses,
    iterationTimeoutMs,
    activeHandles: activeHandles(),
    trackedResources: resources,
    completedAt: new Date().toISOString(),
  };
  await fs.writeFile(
    path.join(outputRoot, `summary-${mode}-pid-${process.pid}.json`),
    `${JSON.stringify(summary, null, 2)}\n`,
    "utf8",
  );
  if (leaked)
    throw new Error(`tracked resources leaked: ${JSON.stringify(resources)}`);
}

async function main() {
  await fs.mkdir(outputRoot, { recursive: true });
  if (process.argv[2] === "--worker") {
    const label = process.argv[3] ?? "fresh-worker";
    const iterations = Number(process.argv[4] ?? "1");
    for (let index = 1; index <= iterations; index++)
      await runIteration(label, index);
    await writeSummary("fresh-worker", iterations, 0);
    return;
  }
  const inProcessIterations = Number(
    process.env.VFR_CONCURRENCY_STRESS_IN_PROCESS_ITERATIONS ?? "10",
  );
  const freshProcesses = Number(
    process.env.VFR_CONCURRENCY_STRESS_FRESH_PROCESSES ?? "4",
  );
  const freshIterations = Number(
    process.env.VFR_CONCURRENCY_STRESS_FRESH_ITERATIONS ?? "2",
  );
  for (let index = 1; index <= inProcessIterations; index++)
    await runIteration("in-process", index);
  for (let index = 1; index <= freshProcesses; index++)
    await runFreshProcess(`fresh-${index}`, index, freshIterations);
  await writeSummary("orchestrator", inProcessIterations, freshProcesses);
}

main().catch(async (error) => {
  try {
    await fs.mkdir(outputRoot, { recursive: true });
    await fs.writeFile(
      path.join(outputRoot, `fatal-pid-${process.pid}.json`),
      `${JSON.stringify(
        {
          status: "failed",
          error:
            error instanceof Error
              ? `${error.name}: ${error.message}`
              : String(error),
          nodeVersion: process.version,
          activeHandles: activeHandles(),
          trackedResources: resourceState(),
          failedAt: new Date().toISOString(),
        },
        null,
        2,
      )}\n`,
      "utf8",
    );
  } finally {
    console.error(error);
    process.exit(1);
  }
});
