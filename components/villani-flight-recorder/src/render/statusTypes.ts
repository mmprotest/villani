export type ReplayProcessingStatus =
  | "generated"
  | "generated_with_warnings"
  | "partial"
  | "parse_failed"
  | "render_failed"
  | "write_failed";

export type CapturedRunStatus =
  "succeeded" | "failed" | "partial" | "unknown" | "not_applicable";

export interface ReplayStatusSummary {
  status: ReplayProcessingStatus;
  label: string;
  tone: "success" | "warning" | "error" | "info" | "muted";
  reason: string;
  warningsCount: number;
  unknownEventsCount: number;
  outputWritten: boolean;
  htmlValidated?: boolean;
}

export interface CapturedRunStatusSummary {
  status: CapturedRunStatus;
  label: string;
  tone: "success" | "warning" | "error" | "info" | "muted";
  reason: string;
  failedCommands: number;
  failedTests: number;
  totalCommands: number;
  totalTests: number;
  fileEdits: number;
  hasFinalAnswer?: boolean;
}
