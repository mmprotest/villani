import path from "node:path";
import type { ParsedSession } from "../src/providers/types.js";
import { renderReplay } from "../src/render/renderReplay.js";
import { testResources } from "./helpers/testResources.js";

type RenderOptions = Parameters<typeof renderReplay>[1];

/** Render without leaving replay artifacts in the source checkout. */
export async function renderReplayForTest(
  session: ParsedSession,
  options: RenderOptions = {},
) {
  const temporaryRoot =
    await testResources.temporaryDirectory("vfr-render-test-");
  return renderReplay(session, {
    cwd: process.cwd(),
    ...options,
    out: options.out ?? path.join(temporaryRoot, "replay.html"),
  });
}
