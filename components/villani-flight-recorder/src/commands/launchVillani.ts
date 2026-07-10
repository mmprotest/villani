import path from "node:path";

import { defaultIndexDir } from "../index/sessionStore.js";
import { parseVillaniRun } from "../providers/villani.js";
import { renderReplay } from "../render/renderReplay.js";
import { defaultVillaniRunsRoot } from "../scanners/findVillaniRuns.js";
import { openBrowser } from "../utils/openBrowser.js";

export function assertOutsideVillaniRunsRoot(
  runsRoot: string,
  ...outputs: (string | undefined)[]
) {
  const root = path.resolve(runsRoot);
  for (const output of outputs) {
    if (!output) continue;
    const candidate = path.resolve(output);
    if (candidate === root || candidate.startsWith(`${root}${path.sep}`))
      throw new Error(
        `Flight Recorder output must be outside the canonical runs root: ${candidate}`,
      );
  }
}

export async function launchVillaniRun(
  options: {
    root?: string;
    runId: string;
    out?: string;
    open?: boolean;
  },
  dependencies: {
    open?: (file: string) => void | Promise<void>;
  } = {},
) {
  const root = path.resolve(options.root ?? defaultVillaniRunsRoot());
  const runDirectory = path.join(root, options.runId);
  const session = await parseVillaniRun(runDirectory);
  const out =
    options.out ??
    path.join(defaultIndexDir(), "replays", `${options.runId}.html`);
  assertOutsideVillaniRunsRoot(root, out);
  const file = await renderReplay(session, {
    out,
    returnLabel: "Back to runs",
  });
  if (options.open !== false) await (dependencies.open ?? openBrowser)(file);
  return file;
}
