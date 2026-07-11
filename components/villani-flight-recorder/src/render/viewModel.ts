import { GitInfo } from "../git/gitInfo.js";
import { FlightEvent, ParsedSession } from "../providers/types.js";
import { IconName } from "./components/icons.js";
import { deriveExecutionGraph } from "./deriveGraph.js";
import { deriveMetrics } from "./deriveMetrics.js";
import { deriveTimeline } from "./deriveTimeline.js";
import { deriveReplayStatus } from "./deriveReplayStatus.js";
import { deriveCapturedRunStatus } from "./deriveCapturedRunStatus.js";
import {
  CapturedRunStatusSummary,
  ReplayStatusSummary,
} from "./statusTypes.js";
import {
  changedFilesFromEvents,
  changedFilesFromGit,
  diffFromEvents,
  diffFromGit,
} from "./deriveDetails.js";
import type { VillaniRunData } from "../providers/villani.js";

export type Status =
  "completed" | "running" | "warning" | "failed" | "pending" | "skipped";
export type Severity =
  | "none"
  | "info"
  | "minor-warning"
  | "warning"
  | "failed"
  | "unavailable"
  | "skipped";
export interface DetailViewModel {
  title: string;
  summary?: string;
  sourceLabel?: string;
  impactLabel?: string;
  replayImpactLabel?: string;
  capturedImpactLabel?: string;
  meta?: Record<string, string>;
  raw?: unknown;
}
export interface MetricCardViewModel {
  id: string;
  label: string;
  value: string;
  subvalue?: string;
  icon: IconName;
  tone?: "default" | "success" | "warning" | "error" | "info";
  telemetryAvailable?: boolean;
  empty?: boolean;
}
export interface TimelineEventViewModel {
  id: string;
  timestamp?: string;
  title: string;
  subtitle?: string;
  durationLabel?: string;
  status: Status;
  severity?: Severity;
  icon: IconName;
  eventType: string;
  detail: DetailViewModel;
  raw: FlightEvent;
}
export interface GraphNodeViewModel {
  id: string;
  title: string;
  subtitle?: string;
  durationLabel?: string;
  status: Status;
  severity?: Severity;
  badgeLabel?: string;
  badgeTone?:
    "success" | "info" | "minor-warning" | "warning" | "error" | "muted";
  icon: IconName;
  x: number;
  y: number;
  width: number;
  height: number;
  detail: DetailViewModel;
  laneTone?: "normal" | "dimmed" | "emphasis";
}
export interface GraphLinkViewModel {
  id: string;
  from: string;
  to: string;
  kind?: string;
  laneTone?: "normal" | "dimmed" | "emphasis";
  status: Exclude<Status, "skipped">;
}
export interface ExecutionGraphViewModel {
  nodes: GraphNodeViewModel[];
  links: GraphLinkViewModel[];
  laneLabels?: string[];
  legend: { label: string; status: Status }[];
}
export interface ReplayDashboardViewModel {
  brand: { title: string; mode: "REPLAY" | "LIVE" };
  topBar: {
    playbackLabel: string;
    speedLabel: string;
    showProgress: boolean;
    primaryActionLabel: string;
  };
  replayStatus: ReplayStatusSummary;
  capturedRunStatus: CapturedRunStatusSummary;
  metrics: MetricCardViewModel[];
  timeline: TimelineEventViewModel[];
  graph: ExecutionGraphViewModel;
  details: DetailViewModel;
  redactionReport?: unknown;
  warnings: string[];
  rawEvents: FlightEvent[];
  changedFiles: string[];
  diff: string;
  provider: string;
  villani?: VillaniRunData;
}

export const fmtTime = (v?: string) => {
  if (!v) return "—";
  const d = new Date(v);
  return Number.isNaN(d.getTime())
    ? v
    : d.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
};
export const fmtDuration = (ms?: number) =>
  ms === undefined
    ? "—"
    : ms < 1000
      ? `${ms}ms`
      : `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}s`;
export function deriveReplayViewModel(
  session: ParsedSession,
  git: GitInfo | null,
): ReplayDashboardViewModel {
  const events = session.events.length
    ? session.events
    : [
        {
          id: "empty",
          provider: session.provider,
          type: "unknown",
          title: "No events captured",
          summary: "Replay data was empty",
        } as FlightEvent,
      ];
  const normalizedSession = { ...session, events };
  const warnings = [
    ...session.warnings,
    ...events.flatMap((e) => e.warnings ?? []),
  ];
  const unknownEventsCount = events.filter((e) => e.type === "unknown").length;
  const replayStatus = deriveReplayStatus({
    events,
    warnings,
    unknownEventsCount,
    outputWritten: true,
    htmlValidated: true,
  });
  const capturedRunStatus = deriveCapturedRunStatus(
    events,
    session.villani?.manifest?.final_state,
  );
  const timeline = deriveTimeline(events);
  const eventFiles = changedFilesFromEvents(events);
  const liveFiles = changedFilesFromGit(git);
  const changedFiles = session.villani?.materialization?.changed_files?.length
    ? session.villani.materialization.changed_files
    : session.provider === "git" && eventFiles.length > 0
      ? eventFiles
      : [...new Set([...eventFiles, ...liveFiles])];
  const eventDiff = diffFromEvents(events);
  const liveDiff = diffFromGit(git);
  return {
    brand: { title: "Villani Flight Recorder", mode: "REPLAY" },
    topBar: {
      playbackLabel: "Play",
      speedLabel: "1.0x",
      showProgress: true,
      primaryActionLabel: "Open in Console",
    },
    replayStatus,
    capturedRunStatus,
    metrics: deriveMetrics(normalizedSession, replayStatus, capturedRunStatus),
    timeline,
    graph: deriveExecutionGraph({
      session: normalizedSession,
      git,
      replayStatus,
      capturedRunStatus,
      htmlValid: true,
      outputWritten: true,
    }),
    details: timeline[0]?.detail ?? { title: "No event" },
    warnings,
    rawEvents: events,
    changedFiles,
    diff:
      session.villani?.attempts.find(
        (attempt) =>
          attempt.snapshot.attempt_id ===
          session.villani?.manifest?.selected_attempt_id,
      )?.patch ||
      eventDiff ||
      liveDiff ||
      "Not captured",
    provider: session.provider,
    villani: session.villani,
    redactionReport: (session as ParsedSession & { redactionReport?: unknown })
      .redactionReport,
  };
}
