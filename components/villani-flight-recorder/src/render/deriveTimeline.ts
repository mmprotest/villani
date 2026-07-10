import { FlightEvent } from "../providers/types.js";
import { IconName } from "./components/icons.js";
import {
  fmtDuration,
  Severity,
  Status,
  TimelineEventViewModel,
} from "./viewModel.js";

const rawIsError = (e: FlightEvent) =>
  Boolean(
    e.raw &&
    typeof e.raw === "object" &&
    "is_error" in e.raw &&
    (e.raw as { is_error?: unknown }).is_error === true,
  );
export const eventStatus = (e: FlightEvent): Status =>
  e.type === "error" || (e.exitCode ?? 0) !== 0 || rawIsError(e)
    ? "failed"
    : "completed";
export const eventSeverity = (e: FlightEvent): Severity =>
  e.type === "error" || (e.exitCode ?? 0) !== 0 || rawIsError(e)
    ? "failed"
    : e.type === "unknown" || (e.warnings?.length ?? 0) > 0
      ? "minor-warning"
      : "none";
function timelineCopy(e: FlightEvent): { title: string; subtitle: string } {
  if (e.type === "test_run" && e.command && (e.exitCode ?? 0) !== 0) {
    return {
      title: `Command failed: ${e.command}`,
      subtitle: "Captured test command failed",
    };
  }
  if (e.command && (e.exitCode ?? 0) !== 0) {
    return {
      title: `Command failed: ${e.command}`,
      subtitle: "Captured command exited non-zero",
    };
  }
  if (e.type === "error" && e.command) {
    return {
      title: `Command failed: ${e.command}`,
      subtitle: e.summary || "Captured command failed",
    };
  }
  if (e.type === "tool_result" && rawIsError(e)) {
    return {
      title: "Tool result failed",
      subtitle: "Captured tool returned an error",
    };
  }
  if (e.command) {
    return { title: e.title, subtitle: "Captured command recorded" };
  }
  if (e.type.includes("file")) {
    return {
      title: e.title || "Captured file edit",
      subtitle: "Captured file edit",
    };
  }
  if (["git_commit", "git_status", "diff"].includes(e.type)) {
    return { title: e.title, subtitle: "Repository reconstruction" };
  }
  if (e.type === "unknown" || (e.warnings?.length ?? 0) > 0) {
    return {
      title: "Transcript parsed with warnings",
      subtitle: "Recorder recovered partial session data",
    };
  }
  if (e.type.includes("message")) {
    return { title: e.title, subtitle: "Captured agent run" };
  }
  return {
    title: e.title,
    subtitle: e.summary || e.path || "Recorder pipeline",
  };
}

const commandHasResult = (e: FlightEvent) =>
  e.exitCode !== undefined ||
  e.stdout !== undefined ||
  e.stderr !== undefined ||
  e.type === "error" ||
  rawIsError(e);
const commandIsStart = (e: FlightEvent) =>
  Boolean(e.command) && !commandHasResult(e);
const commandIsResult = (e: FlightEvent) =>
  Boolean(e.command) && commandHasResult(e);

const rawObj = (e: FlightEvent): Record<string, unknown> =>
  e.raw && typeof e.raw === "object" && !Array.isArray(e.raw)
    ? (e.raw as Record<string, unknown>)
    : {};

const commandLifecycleKey = (e: FlightEvent): string | undefined => {
  const raw = rawObj(e);
  const candidates = [
    raw.tool_use_id,
    raw.tool_call_id,
    raw.call_id,
    raw.parent_id,
    raw.command_id,
    raw.provider_command_id,
  ];
  const value = candidates.find(
    (v) => typeof v === "string" && v.trim().length > 0,
  );
  return typeof value === "string" ? value : undefined;
};

type PendingCommand = { event: FlightEvent; index: number; key?: string };

function groupedCommandEvent(
  start: FlightEvent,
  result: FlightEvent,
): FlightEvent {
  const started = start.timestamp
    ? new Date(start.timestamp).getTime()
    : undefined;
  const ended = result.timestamp
    ? new Date(result.timestamp).getTime()
    : undefined;
  const durationMs =
    result.durationMs ??
    start.durationMs ??
    (started !== undefined && ended !== undefined
      ? Math.max(0, ended - started)
      : undefined);
  return {
    ...start,
    ...result,
    id: `${start.id}+${result.id}`,
    type: result.type,
    title: result.title || start.title,
    summary: result.summary || start.summary,
    command: start.command || result.command,
    durationMs,
    raw: {
      kind: "grouped_command_lifecycle",
      status:
        result.type === "error" ||
        (result.exitCode ?? 0) !== 0 ||
        rawIsError(result)
          ? "failed"
          : result.exitCode === 0
            ? "succeeded"
            : "unknown",
      command: start.command || result.command,
      exitCode: result.exitCode,
      stdout: result.stdout,
      stderr: result.stderr,
      start,
      result,
      startTime: start.timestamp,
      endTime: result.timestamp,
      lifecycleKey: commandLifecycleKey(start) ?? commandLifecycleKey(result),
    },
  };
}

function groupCommandLifecycle(events: FlightEvent[]): FlightEvent[] {
  const grouped: FlightEvent[] = [];
  const pending: PendingCommand[] = [];
  const flushStale = (index: number) => {
    while (
      pending.length &&
      !pending[0]!.key &&
      index - pending[0]!.index > 3
    ) {
      grouped.push(pending.shift()!.event);
    }
  };
  for (let index = 0; index < events.length; index++) {
    const event = events[index]!;
    if (commandIsResult(event)) {
      const key = commandLifecycleKey(event);
      let match = key ? pending.findIndex((p) => p.key === key) : -1;
      if (match < 0) {
        match = pending.findIndex(
          (p) => p.event.command === event.command && index - p.index <= 3,
        );
      }
      if (match >= 0) {
        const earlier = pending.splice(0, match);
        grouped.push(...earlier.map((p) => p.event));
        const [start] = pending.splice(0, 1);
        grouped.push(groupedCommandEvent(start!.event, event));
        continue;
      }
    }
    flushStale(index);
    if (commandIsStart(event)) {
      pending.push({ event, index, key: commandLifecycleKey(event) });
      continue;
    }
    grouped.push(event);
  }
  while (pending.length) grouped.push(pending.shift()!.event);
  return grouped;
}

export function deriveTimeline(
  events: FlightEvent[],
): TimelineEventViewModel[] {
  return groupCommandLifecycle(events).map((e): TimelineEventViewModel => ({
    id: e.id,
    timestamp: e.timestamp,
    title: timelineCopy(e).title,
    subtitle: timelineCopy(e).subtitle,
    durationLabel: fmtDuration(e.durationMs),
    status: eventStatus(e),
    severity: eventSeverity(e),
    icon: (e.command
      ? "terminal"
      : e.type.includes("file")
        ? "edit"
        : e.type.includes("message")
          ? "review"
          : "parse") as IconName,
    eventType: e.type,
    detail: {
      title: timelineCopy(e).title,
      summary: timelineCopy(e).subtitle,
      sourceLabel:
        e.command || e.type.includes("message") || e.type.includes("file")
          ? "Captured agent run"
          : ["git_commit", "git_status", "diff"].includes(e.type)
            ? "Repository reconstruction"
            : "Recorder pipeline",
      impactLabel:
        (e.exitCode ?? 0) !== 0 || e.type === "error" || rawIsError(e)
          ? "Captured run failed"
          : ["git_commit", "git_status", "diff"].includes(e.type)
            ? "Git diff captured"
            : "Replay event recorded",
      replayImpactLabel:
        (e.exitCode ?? 0) !== 0 || rawIsError(e)
          ? "The replay was generated successfully, but the captured agent command failed. Investigate this command before trusting the run outcome."
          : "Generated",
      capturedImpactLabel:
        (e.exitCode ?? 0) !== 0 || rawIsError(e)
          ? "Failed command/test"
          : e.command
            ? "Command/tool recorded"
            : ["git_commit", "git_status", "diff"].includes(e.type)
              ? "N/A"
              : "Captured run preserved",
      raw: e,
    },
    raw: e,
  }));
}
