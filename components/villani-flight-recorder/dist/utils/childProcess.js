import { spawn, spawnSync, } from "node:child_process";
import { performance } from "node:perf_hooks";
export class ChildProcessFailure extends Error {
    diagnostics;
    constructor(diagnostics) {
        super(`child process failed: ${JSON.stringify(diagnostics, null, 2)}`);
        this.diagnostics = diagnostics;
        this.name = "ChildProcessFailure";
    }
}
const activeChildren = new Set();
export function activeChildProcessCount() {
    return activeChildren.size;
}
function waitForClose(child, timeoutMs) {
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
async function terminateChild(child) {
    if (child.exitCode !== null || child.signalCode !== null)
        return;
    child.kill("SIGTERM");
    await waitForClose(child, 500);
    if (child.exitCode !== null || child.signalCode !== null)
        return;
    if (process.platform === "win32" && child.pid !== undefined) {
        spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], {
            windowsHide: true,
            stdio: "pipe",
        });
    }
    else {
        child.kill("SIGKILL");
    }
    await waitForClose(child, 2_000);
}
export async function runChildProcess(command, args, options) {
    const started = performance.now();
    const timeoutMs = options.timeoutMs ?? 10_000;
    const maxOutputBytes = options.maxOutputBytes ?? 30_000_000;
    let stdout = "";
    let stderr = "";
    let capturedBytes = 0;
    let outputTruncated = false;
    let spawnError;
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
    const capture = (stream, chunk) => {
        const remaining = Math.max(0, maxOutputBytes - capturedBytes);
        const value = chunk.subarray(0, remaining).toString("utf8");
        if (stream === "stdout")
            stdout += value;
        else
            stderr += value;
        capturedBytes += chunk.length;
        if (chunk.length > remaining)
            outputTruncated = true;
    };
    child.stdout.on("data", (chunk) => capture("stdout", chunk));
    child.stderr.on("data", (chunk) => capture("stderr", chunk));
    let exitStatus = null;
    let signal = null;
    try {
        const outcome = await new Promise((resolve) => {
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
        });
        if (outcome !== "closed")
            await terminateChild(child);
    }
    finally {
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
    const diagnostics = {
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
    if (spawnError ||
        timedOut ||
        outputTruncated ||
        exitStatus !== 0 ||
        processState !== "exited")
        throw new ChildProcessFailure(diagnostics);
    return diagnostics;
}
