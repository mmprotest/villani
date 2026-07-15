import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";

export type ConcurrencyPhase =
  | "fixture_directory_creation"
  | "fixture_recursive_copy"
  | "fixture_integrity_check"
  | "schema_validator_acquisition"
  | "json_parsing"
  | "schema_validation"
  | "canonical_run_parsing"
  | "digest_calculation"
  | "assertion_completion"
  | "temporary_directory_cleanup";

interface PhaseRecord {
  workerIndex: number;
  phase: ConcurrencyPhase;
  sourcePath: string;
  destinationPath: string;
  startedAt: string;
  finishedAt: string;
  elapsedMs: number;
  status: "passed" | "failed";
  error?: string;
}

interface ActivePhase {
  workerIndex: number;
  phase: ConcurrencyPhase;
  sourcePath: string;
  destinationPath: string;
  startedAt: string;
  elapsedMs: number;
}

function activeHandles(): string[] {
  const getHandles = (
    process as typeof process & { _getActiveHandles?: () => unknown[] }
  )._getActiveHandles;
  if (!getHandles) return [];
  return getHandles.call(process).map((handle) => {
    const constructor = (handle as { constructor?: { name?: string } })
      .constructor;
    return constructor?.name ?? typeof handle;
  });
}

export class ConcurrencyDiagnostics {
  private readonly records: PhaseRecord[] = [];
  private readonly active = new Map<
    string,
    Omit<ActivePhase, "elapsedMs"> & {
      started: number;
    }
  >();
  private readonly startedAt = new Date().toISOString();
  private resourceState: unknown = null;

  constructor(
    private readonly label: string,
    private readonly outputDirectory = process.env
      .VFR_CONCURRENCY_DIAGNOSTICS_DIR ??
      path.join(os.tmpdir(), "villani-flight-recorder-concurrency"),
  ) {}

  async phase<T>(
    workerIndex: number,
    phase: ConcurrencyPhase,
    sourcePath: string,
    destinationPath: string,
    action: () => Promise<T> | T,
  ): Promise<T> {
    const key = `${workerIndex}:${phase}`;
    const started = performance.now();
    const startedAt = new Date().toISOString();
    this.active.set(key, {
      workerIndex,
      phase,
      sourcePath,
      destinationPath,
      startedAt,
      started,
    });
    try {
      const value = await action();
      this.records.push({
        workerIndex,
        phase,
        sourcePath,
        destinationPath,
        startedAt,
        finishedAt: new Date().toISOString(),
        elapsedMs: Math.round((performance.now() - started) * 1000) / 1000,
        status: "passed",
      });
      return value;
    } catch (error) {
      this.records.push({
        workerIndex,
        phase,
        sourcePath,
        destinationPath,
        startedAt,
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

  recordResourceState(value: unknown): void {
    this.resourceState = value;
  }

  private document(status: "passed" | "failed", error?: unknown) {
    const now = performance.now();
    return {
      schemaVersion: "villani.flight_recorder.concurrency_diagnostics.v1",
      label: this.label,
      pid: process.pid,
      nodeVersion: process.version,
      platform: process.platform,
      startedAt: this.startedAt,
      finishedAt: new Date().toISOString(),
      status,
      error:
        error === undefined
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
      trackedResources: this.resourceState,
    };
  }

  async write(status: "passed" | "failed", error?: unknown): Promise<string> {
    await fs.mkdir(this.outputDirectory, { recursive: true });
    const file = path.join(
      this.outputDirectory,
      `${this.label}-${process.pid}-${Date.now()}.json`,
    );
    await fs.writeFile(
      file,
      `${JSON.stringify(this.document(status, error), null, 2)}\n`,
      "utf8",
    );
    return file;
  }

  async writeWhenRequested(): Promise<void> {
    if (process.env.VFR_CONCURRENCY_DEBUG === "1") await this.write("passed");
  }
}

export async function withDiagnosticDeadline<T>(
  operation: Promise<T>,
  timeoutMs: number,
): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(
          () =>
            reject(
              new Error(
                `concurrency diagnostic deadline exceeded after ${timeoutMs}ms`,
              ),
            ),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
