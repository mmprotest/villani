import { GitInfo } from "../git/gitInfo.js";
import { ParsedSession } from "../providers/types.js";
import { IconName } from "./components/icons.js";
import { GRAPH_COORDS } from "./graphGeometry.js";
import {
  ExecutionGraphViewModel,
  GraphLinkViewModel,
  GraphNodeViewModel,
  Severity,
  Status,
} from "./viewModel.js";
import {
  CapturedRunStatusSummary,
  ReplayStatusSummary,
} from "./statusTypes.js";
import { deriveCapturedRunStatus } from "./deriveCapturedRunStatus.js";
import { deriveReplayStatus } from "./deriveReplayStatus.js";
import {
  graphNodeBadge,
  graphNodeSubtitle,
  graphNodeTitle,
  GraphCopyContext,
} from "./graphCopy.js";

export interface GraphDerivationInput {
  session: ParsedSession;
  git: GitInfo | null;
  htmlValid?: boolean;
  outputWritten?: boolean;
  replayStatus?: ReplayStatusSummary;
  capturedRunStatus?: CapturedRunStatusSummary;
}

export type GraphLinkKind =
  | "recorder_pipeline"
  | "recorder_to_captured"
  | "captured_execution"
  | "captured_artifact"
  | "repo_correlation"
  | "repo_artifact"
  | "replay_output";

export interface GraphLinkDefinition {
  id: string;
  from: string;
  to: string;
  kind: GraphLinkKind;
}

export const GRAPH_LINKS: GraphLinkDefinition[] = [
  {
    id: "discover-parse",
    from: "discover",
    to: "parse",
    kind: "recorder_pipeline",
  },
  {
    id: "parse-normalize",
    from: "parse",
    to: "normalize",
    kind: "recorder_pipeline",
  },
  {
    id: "normalize-replay-output",
    from: "normalize",
    to: "replay-output",
    kind: "replay_output",
  },
  {
    id: "normalize-agent-events",
    from: "normalize",
    to: "agent-events",
    kind: "recorder_to_captured",
  },
  {
    id: "agent-events-commands",
    from: "agent-events",
    to: "commands",
    kind: "captured_execution",
  },
  {
    id: "commands-file-changes",
    from: "commands",
    to: "file-changes",
    kind: "captured_artifact",
  },
  {
    id: "commands-correlate",
    from: "commands",
    to: "correlate",
    kind: "repo_correlation",
  },
  {
    id: "correlate-git-state",
    from: "correlate",
    to: "git-state",
    kind: "repo_correlation",
  },
  {
    id: "git-state-diff-capture",
    from: "git-state",
    to: "diff-capture",
    kind: "repo_artifact",
  },
];

const gitAvailable = (git: GitInfo | null) =>
  Boolean(git?.head || git?.status || git?.diff || git?.diffStat);
const statusFromSeverity = (s: Severity): Status =>
  s === "failed"
    ? "failed"
    : s === "warning"
      ? "warning"
      : s === "skipped" || s === "unavailable"
        ? "skipped"
        : "completed";
const node = (
  id: string,
  title: string,
  severity: Severity,
  subtitle: string,
  icon: IconName,
  badgeLabel?: string,
  laneTone?: GraphNodeViewModel["laneTone"],
): GraphNodeViewModel => ({
  id,
  title,
  severity,
  status: statusFromSeverity(severity),
  subtitle,
  icon,
  badgeLabel,
  laneTone,
  badgeTone:
    severity === "minor-warning"
      ? "minor-warning"
      : severity === "unavailable" || severity === "skipped"
        ? "muted"
        : severity === "failed"
          ? "error"
          : severity === "warning"
            ? "warning"
            : "success",
  ...GRAPH_COORDS[id],
  detail: { title, summary: subtitle },
});

export function deriveGraphLinkStatus(input: {
  link: GraphLinkDefinition;
  nodesById: Map<string, GraphNodeViewModel>;
  replayStatus: ReplayStatusSummary;
  capturedRunStatus: CapturedRunStatusSummary;
  hasGitInfo: boolean;
  hasDiffInfo: boolean;
}): GraphLinkViewModel["status"] {
  const {
    link,
    nodesById,
    replayStatus,
    capturedRunStatus,
    hasGitInfo,
    hasDiffInfo,
  } = input;
  const to = nodesById.get(link.to);
  switch (link.kind) {
    case "recorder_pipeline":
      if (replayStatus.status === "parse_failed") return "failed";
      return to?.severity === "warning" || to?.severity === "minor-warning"
        ? "warning"
        : "completed";
    case "replay_output":
      if (["render_failed", "write_failed"].includes(replayStatus.status))
        return "failed";
      if (
        replayStatus.status === "generated_with_warnings" ||
        replayStatus.status === "partial"
      )
        return "warning";
      return "completed";
    case "recorder_to_captured":
      if (
        capturedRunStatus.status === "not_applicable" ||
        to?.severity === "unavailable"
      )
        return "pending";
      return to?.severity === "warning" || to?.severity === "minor-warning"
        ? "warning"
        : "completed";
    case "captured_execution":
      if (capturedRunStatus.failedCommands || capturedRunStatus.failedTests)
        return "failed";
      return capturedRunStatus.totalCommands || capturedRunStatus.totalTests
        ? "completed"
        : "pending";
    case "captured_artifact":
      return capturedRunStatus.fileEdits ? "completed" : "pending";
    case "repo_correlation":
      return hasGitInfo ? "completed" : "pending";
    case "repo_artifact":
      if (!hasGitInfo) return "pending";
      return hasDiffInfo ? "completed" : "warning";
  }
}

export function deriveExecutionGraph(
  input: GraphDerivationInput,
): ExecutionGraphViewModel {
  const { session, git } = input;
  const events = session.events;
  const warnings = [
    ...session.warnings,
    ...events.flatMap((e) => e.warnings ?? []),
  ];
  const unknown = events.filter((e) => e.type === "unknown").length;
  const known = events.length - unknown;
  const hasGit = gitAvailable(git);
  const diffOk = Boolean(
    git?.diff || git?.diffStat || events.some((e) => e.diff),
  );
  const replayStatus =
    input.replayStatus ??
    deriveReplayStatus({
      events,
      warnings,
      unknownEventsCount: unknown,
      outputWritten: input.outputWritten,
      htmlValidated: input.htmlValid,
    });
  const captured = input.capturedRunStatus ?? deriveCapturedRunStatus(events);
  const parseSeverity: Severity =
    replayStatus.status === "parse_failed"
      ? "failed"
      : warnings.length === 0
        ? "none"
        : warnings.length <= 2
          ? "minor-warning"
          : "warning";
  const agentSeverity: Severity =
    captured.status === "not_applicable"
      ? "skipped"
      : events.length === 0
        ? "unavailable"
        : unknown > known
          ? "warning"
          : unknown > 0
            ? "minor-warning"
            : "none";
  const commandSeverity: Severity =
    captured.status === "not_applicable"
      ? "skipped"
      : captured.failedCommands || captured.failedTests
        ? "failed"
        : captured.totalCommands || captured.totalTests
          ? "none"
          : "unavailable";
  const fileSeverity: Severity =
    captured.status === "not_applicable" && diffOk
      ? "none"
      : captured.status === "not_applicable"
        ? "unavailable"
        : captured.fileEdits
          ? "none"
          : "unavailable";
  const outputSeverity: Severity = ["render_failed", "write_failed"].includes(
    replayStatus.status,
  )
    ? "failed"
    : replayStatus.status === "generated_with_warnings" ||
        replayStatus.status === "partial"
      ? "minor-warning"
      : "none";
  const isGitReplay = captured.status === "not_applicable";
  const repositoryLaneTone: GraphNodeViewModel["laneTone"] = !hasGit
    ? "dimmed"
    : isGitReplay
      ? "emphasis"
      : "normal";
  const capturedLaneTone: GraphNodeViewModel["laneTone"] = isGitReplay
    ? "dimmed"
    : "normal";
  const copyContext: GraphCopyContext = {
    eventCount: events.length,
    unknownCount: unknown,
    warningCount: warnings.length,
    hasSource: Boolean(session.path || session.sessionPath || session.provider),
    hasGit,
    diffOk,
    gitStatus: git?.status,
    captured,
    replayStatus,
  };
  const makeNode = (
    id: string,
    severity: Severity,
    icon: IconName,
    laneTone?: GraphNodeViewModel["laneTone"],
  ) =>
    node(
      id,
      graphNodeTitle(id),
      severity,
      graphNodeSubtitle(id, copyContext),
      icon,
      graphNodeBadge(id, copyContext),
      laneTone,
    );
  const nodes = [
    makeNode(
      "discover",
      session.path || session.sessionPath || session.provider
        ? "none"
        : "failed",
      "discover",
    ),
    makeNode("parse", parseSeverity, "parse"),
    makeNode("normalize", events.length ? "none" : "failed", "normalize"),
    makeNode("agent-events", agentSeverity, "terminal", capturedLaneTone),
    makeNode("commands", commandSeverity, "terminal", capturedLaneTone),
    makeNode(
      "file-changes",
      fileSeverity,
      "edit",
      isGitReplay && diffOk ? "emphasis" : capturedLaneTone,
    ),
    makeNode(
      "correlate",
      hasGit ? "none" : "unavailable",
      "correlate",
      repositoryLaneTone,
    ),
    makeNode(
      "git-state",
      hasGit ? "none" : "unavailable",
      "branch",
      repositoryLaneTone,
    ),
    makeNode(
      "diff-capture",
      diffOk ? "none" : hasGit ? "minor-warning" : "unavailable",
      "edit",
      repositoryLaneTone,
    ),
    makeNode("replay-output", outputSeverity, "flag"),
  ];
  const nodesById = new Map(nodes.map((n) => [n.id, n]));
  const links = GRAPH_LINKS.map((link) => ({
    ...link,
    laneTone:
      link.kind === "repo_correlation" || link.kind === "repo_artifact"
        ? repositoryLaneTone
        : link.kind === "captured_execution" ||
            link.kind === "captured_artifact"
          ? capturedLaneTone
          : undefined,
    status: deriveGraphLinkStatus({
      link,
      nodesById,
      replayStatus,
      capturedRunStatus: captured,
      hasGitInfo: hasGit,
      hasDiffInfo: diffOk,
    }),
  }));
  return {
    nodes,
    links,
    laneLabels: ["Recorder Pipeline", "Captured Run", "Repository"],
    legend: [
      { label: "Completed", status: "completed" },
      { label: "Minor", status: "completed" },
      { label: "Warning", status: "warning" },
      { label: "Failed", status: "failed" },
      { label: "Skipped", status: "skipped" },
    ],
  } as ExecutionGraphViewModel;
}
