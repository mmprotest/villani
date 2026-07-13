import { afterAll } from "vitest";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import type { ParsedSession } from "../src/providers/types.js";
import { renderReplay } from "../src/render/renderReplay.js";

let temporaryRoot: string | undefined;
let sequence = 0;

type RenderOptions = Parameters<typeof renderReplay>[1];

/** Render without leaving replay artifacts in the source checkout. */
export async function renderReplayForTest(
  session: ParsedSession,
  options: RenderOptions = {},
) {
  temporaryRoot ??= await fs.mkdtemp(
    path.join(os.tmpdir(), "vfr-render-test-"),
  );
  sequence += 1;
  return renderReplay(session, {
    cwd: process.cwd(),
    ...options,
    out: options.out ?? path.join(temporaryRoot, `replay-${sequence}.html`),
  });
}

afterAll(async () => {
  if (temporaryRoot) {
    await fs.rm(temporaryRoot, { recursive: true, force: true });
    temporaryRoot = undefined;
  }
});
