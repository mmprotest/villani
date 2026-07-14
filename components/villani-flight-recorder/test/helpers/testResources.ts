import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { JSDOM, type ConstructorOptions } from "jsdom";
import { afterAll, afterEach } from "vitest";

export class TestResourceTracker {
  private readonly directories = new Set<string>();
  private readonly doms = new Set<JSDOM>();

  async temporaryDirectory(prefix: string): Promise<string> {
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
    this.directories.add(directory);
    return directory;
  }

  trackDirectory(directory: string): string {
    this.directories.add(path.resolve(directory));
    return directory;
  }

  dom(html: string, options?: ConstructorOptions): JSDOM {
    const value = new JSDOM(html, options);
    this.doms.add(value);
    return value;
  }

  pending(): { directories: string[]; domWindows: number } {
    return {
      directories: [...this.directories].sort(),
      domWindows: this.doms.size,
    };
  }

  async cleanup(): Promise<void> {
    for (const dom of [...this.doms]) {
      dom.window.close();
      this.doms.delete(dom);
    }
    const directories = [...this.directories].sort(
      (left, right) => right.length - left.length,
    );
    for (const directory of directories) {
      await fs.rm(directory, { recursive: true, force: true });
      this.directories.delete(directory);
    }
  }
}

export const testResources = new TestResourceTracker();

afterEach(async () => {
  await testResources.cleanup();
});

afterAll(() => {
  const pending = testResources.pending();
  if (pending.directories.length || pending.domWindows) {
    throw new Error(
      `Flight Recorder test resources leaked: ${JSON.stringify(pending)}`,
    );
  }
});
