import { FlightEvent } from "../providers/types.js";
import { ReplayStatusSummary } from "./statusTypes.js";

export function deriveReplayStatus(input: {
  events: FlightEvent[];
  warnings: string[];
  unknownEventsCount: number;
  outputWritten?: boolean;
  htmlValidated?: boolean;
  parseFailed?: boolean;
  renderFailed?: boolean;
  writeFailed?: boolean;
}): ReplayStatusSummary {
  const outputWritten = input.outputWritten !== false && !input.writeFailed;
  const base = {
    warningsCount: input.warnings.length,
    unknownEventsCount: input.unknownEventsCount,
    outputWritten,
    htmlValidated: input.htmlValidated,
  };
  if (input.writeFailed || input.outputWritten === false)
    return {
      ...base,
      status: "write_failed",
      label: "Write failed",
      tone: "error",
      reason: "Replay HTML was not written",
    };
  if (input.renderFailed || input.htmlValidated === false)
    return {
      ...base,
      status: "render_failed",
      label: "Render failed",
      tone: "error",
      reason: "Replay HTML or inline JavaScript failed validation",
    };
  if (input.parseFailed || input.events.length === 0)
    return {
      ...base,
      status: "parse_failed",
      label: "Parse failed",
      tone: "error",
      reason: "No events were parsed from the session",
    };
  if (
    input.unknownEventsCount >
    Math.max(1, input.events.length - input.unknownEventsCount)
  )
    return {
      ...base,
      status: "partial",
      label: "Partial",
      tone: "warning",
      reason: "Replay generated from mostly unknown events",
    };
  if (input.warnings.length || input.unknownEventsCount)
    return {
      ...base,
      status: "generated_with_warnings",
      label: "Generated with warnings",
      tone: "warning",
      reason: "Replay HTML written with recorder warnings",
    };
  return {
    ...base,
    status: "generated",
    label: "Generated",
    tone: "info",
    reason: "Replay HTML written successfully",
  };
}
