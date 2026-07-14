import {
  spawn,
  spawnSync,
  type ChildProcessWithoutNullStreams,
} from "node:child_process";
import { performance } from "node:perf_hooks";

export interface ChildProcessDiagnostics {
  command: string;
  args: string[];
  cwd: string;
  pid: number | null;
  stdout: string;
  stderr: string;
  exitStatus: number | null;
  signal: NodeJS.Signals | null;
  elapsedMs: number;
  processState: "exited" | "terminated" | "spawn_failed";
  timedOut: boolean;
  outputTruncated: boolean;
}

export interface ChildProcessResult extends ChildProcessDiagnostics {
  exitStatus: number;
  processState: "exited";
  timedOut: false;
}

export class ChildProcessFailure extends Error {
  constructor(public readonly diagnostics: ChildProcessDiagnostics) {
    super(`child process failed: ${JSON.stringify(diagnostics, null, 2)}`);
    this.name = "ChildProcessFailure";
  }
}

interface RunChildProcessOptions {
  cwd: string;
  timeoutMs?: number;
  maxOutputBytes?: number;
  env?: NodeJS.ProcessEnv;
}

const activeChildren = new Set<ChildProcessWithoutNullStreams>();

export function activeChildProcessCount(): number {
  return activeChildren.size;
}

function waitForClose(
  child: ChildProcessWithoutNullStreams,
  timeoutMs: number,
): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null)
    return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, timeoutMs);
    child.once("close", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

async function terminateChild(
  child: ChildProcessWithoutNullStreams,
): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill("SIGTERM");
  await waitForClose(child, 500);
  if (child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32" && child.pid !== undefined) {
    spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "pipe",
    });
  } else {
    child.kill("SIGKILL");
  }
  await waitForClose(child, 2_000);
}

export async function runChildProcess(
  command: string,
  args: string[],
  options: RunChildProcessOptions,
): Promise<ChildProcessResult> {
  const started = performance.now();
  const timeoutMs = options.timeoutMs ?? 10_000;
  const maxOutputBytes = options.maxOutputBytes ?? 30_000_000;
  let stdout = "";
  let stderr = "";
  let capturedBytes = 0;
  let outputTruncated = false;
  let spawnError: Error | undefined;
  let timedOut = false;

  const child = spawn(command, args, {
    cwd: options.cwd,
    env: options.env,
    shell: false,
    windowsHide: true,
    stdio: "pipe",
  });
  child.stdin.end();
  activeChildren.add(child);

  const capture = (stream: "stdout" | "stderr", chunk: Buffer) => {
    const remaining = Math.max(0, maxOutputBytes - capturedBytes);
    const value = chunk.subarray(0, remaining).toString("utf8");
    if (stream === "stdout") stdout += value;
    else stderr += value;
    capturedBytes += chunk.length;
    if (chunk.length > remaining) outputTruncated = true;
  };
  child.stdout.on("data", (chunk: Buffer) => capture("stdout", chunk));
  child.stderr.on("data", (chunk: Buffer) => capture("stderr", chunk));

  let exitStatus: number | null = null;
  let signal: NodeJS.Signals | null = null;
  try {
    const outcome = await new Promise<"closed" | "error" | "timeout">(
      (resolve) => {
        const timer = setTimeout(() => {
          timedOut = true;
          resolve("timeout");
        }, timeoutMs);
        child.once("error", (error) => {
          spawnError = error;
          clearTimeout(timer);
          resolve("error");
        });
        child.once("close", (code, closeSignal) => {
          exitStatus = code;
          signal = closeSignal;
          clearTimeout(timer);
          resolve("closed");
        });
      },
    );
    if (outcome !== "closed") await terminateChild(child);
  } finally {
    await terminateChild(child);
    exitStatus = child.exitCode;
    signal = child.signalCode;
    activeChildren.delete(child);
  }

  const processState = spawnError
    ? "spawn_failed"
    : exitStatus !== null || signal !== null
      ? "exited"
      : "terminated";
  const diagnostics: ChildProcessDiagnostics = {
    command,
    args: [...args],
    cwd: options.cwd,
    pid: child.pid ?? null,
    stdout,
    stderr,
    exitStatus,
    signal,
    elapsedMs: Math.round(performance.now() - started),
    processState,
    timedOut,
    outputTruncated,
  };
  if (
    spawnError ||
    timedOut ||
    outputTruncated ||
    exitStatus !== 0 ||
    processState !== "exited"
  )
    throw new ChildProcessFailure(diagnostics);
  return diagnostics as ChildProcessResult;
}
