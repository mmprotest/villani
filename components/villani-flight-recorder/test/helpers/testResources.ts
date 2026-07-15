import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { JSDOM, type ConstructorOptions } from "jsdom";
import { afterAll, afterEach, vi } from "vitest";

import { activeChildProcessCount } from "../../src/utils/childProcess.js";

type CloseResource = () => void | Promise<void>;

export class TestResourceTracker {
  private readonly directories = new Set<string>();
  private readonly doms = new Set<JSDOM>();
  private readonly directoryCleanup = new Map<string, Promise<void>>();
  private readonly servers = new Set<CloseResource>();
  private readonly watchers = new Set<CloseResource>();
  private readonly timers = new Set<NodeJS.Timeout>();

  async temporaryDirectory(prefix: string): Promise<string> {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
    this.directories.add(directory);
    return directory;
  }

  trackDirectory(directory: string): string {
    this.directories.add(path.resolve(directory));
    return directory;
  }

  async removeDirectory(directory: string): Promise<void> {
    const resolved = path.resolve(directory);
    const pending = this.directoryCleanup.get(resolved);
    if (pending) return pending;
    if (!this.directories.has(resolved)) return;
    const cleanup = fs
      .rm(resolved, { recursive: true, force: true })
      .finally(() => {
        this.directories.delete(resolved);
        this.directoryCleanup.delete(resolved);
      });
    this.directoryCleanup.set(resolved, cleanup);
    return cleanup;
  }

  dom(html: string, options?: ConstructorOptions): JSDOM {
    const value = new JSDOM(html, options);
    this.doms.add(value);
    return value;
  }

  trackServer(close: CloseResource): CloseResource {
    this.servers.add(close);
    return close;
  }

  releaseServer(close: CloseResource): void {
    this.servers.delete(close);
  }

  trackWatcher(close: CloseResource): CloseResource {
    this.watchers.add(close);
    return close;
  }

  releaseWatcher(close: CloseResource): void {
    this.watchers.delete(close);
  }

  timer(callback: () => void, timeoutMs: number): NodeJS.Timeout {
    const timer = setTimeout(() => {
      this.timers.delete(timer);
      callback();
    }, timeoutMs);
    this.timers.add(timer);
    return timer;
  }

  clearTimer(timer: NodeJS.Timeout): void {
    clearTimeout(timer);
    this.timers.delete(timer);
  }

  pending(): {
    directories: string[];
    domWindows: number;
    servers: number;
    watchers: number;
    timers: number;
    childProcesses: number;
  } {
    return {
      directories: [...this.directories].sort(),
      domWindows: this.doms.size,
      servers: this.servers.size,
      watchers: this.watchers.size,
      timers: this.timers.size,
      childProcesses: activeChildProcessCount(),
    };
  }

  async cleanup(): Promise<void> {
    for (const dom of [...this.doms]) {
      dom.window.close();
      this.doms.delete(dom);
    }
    for (const timer of [...this.timers]) this.clearTimer(timer);
    for (const close of [...this.watchers]) {
      await close();
      this.watchers.delete(close);
    }
    for (const close of [...this.servers]) {
      await close();
      this.servers.delete(close);
    }
    const directories = [...this.directories].sort(
      (left, right) => right.length - left.length,
    );
    for (const directory of directories) {
      await this.removeDirectory(directory);
    }
  }
}

export const testResources = new TestResourceTracker();

afterEach(async () => {
  await testResources.cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

afterAll(() => {
  const pending = testResources.pending();
  if (
    pending.directories.length ||
    pending.domWindows ||
    pending.servers ||
    pending.watchers ||
    pending.timers ||
    pending.childProcesses
  ) {
    throw new Error(
      `Flight Recorder test resources leaked: ${JSON.stringify(pending)}`,
    );
  }
});
