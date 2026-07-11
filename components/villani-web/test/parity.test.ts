import path from "node:path";
import { describe, expect, it } from "vitest";
import { deriveRunStatus } from "@villani/run-model";
import { parseVillaniRun } from "../../villani-flight-recorder/src/providers/villani";
import { deriveReplayViewModel } from "../../villani-flight-recorder/src/render/viewModel";

describe("Flight Recorder golden parity", () => {
  it("derives identical status and captured metrics for the same canonical bundle", async () => {
    const fixture = path.resolve("../../integration/fixtures/protocol/v1/valid_run");
    const session = await parseVillaniRun(fixture);
    const recorder = deriveReplayViewModel(session, null).capturedRunStatus;
    const shared = deriveRunStatus(
      session.events.map((event) => ({
        id: event.id,
        type: event.type,
        title: event.title,
        command: event.command,
        exit_code: event.exitCode,
        path: event.path,
        raw: event.raw,
      })),
      session.villani?.manifest?.final_state,
    );
    expect(shared).toEqual(recorder);
    expect(shared.totalCommands).toBe(recorder.totalCommands);
    expect(shared.fileEdits).toBe(recorder.fileEdits);
  });
});
