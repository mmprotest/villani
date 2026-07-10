import type { VillaniRunData } from "./villani.js";

export type Provider =
  "claude" | "codex" | "pi" | "villani" | "git" | "unknown";

export type FlightEventType =
  | "session_start"
  | "session_end"
  | "user_message"
  | "assistant_message"
  | "tool_call"
  | "tool_result"
  | "file_read"
  | "file_write"
  | "file_edit"
  | "file_delete"
  | "bash_command"
  | "test_run"
  | "approval"
  | "error"
  | "diff"
  | "git_commit"
  | "git_status"
  | "unknown";

export interface TokenUsage {
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  cacheCreationTokens?: number;
  cacheReadTokens?: number;
  cachedTokens?: number;
  reasoningTokens?: number;
  source?: string;
}

export interface FlightEvent {
  id: string;
  provider: Provider;
  sessionId?: string;
  runId?: string;
  traceId?: string;
  attemptId?: string | null;
  eventId?: string;
  parentEventId?: string | null;
  sequence?: number;
  timestamp?: string;
  cwd?: string;
  type: FlightEventType;
  title: string;
  summary?: string;
  path?: string;
  command?: string;
  exitCode?: number;
  durationMs?: number;
  stdout?: string;
  stderr?: string;
  diff?: string;
  raw?: unknown;
  warnings?: string[];
  tokenUsage?: TokenUsage;
}

export interface ParseSessionInput {
  provider: Provider;
  sessionPath: string;
  repoRoot?: string;
}

export interface ParseSessionResult {
  provider: Provider;
  sessionPath: string;
  sessionId?: string;
  cwd?: string;
  model?: string;
  startedAt?: string;
  endedAt?: string;
  events: FlightEvent[];
  warnings: string[];
  tokenUsage?: TokenUsage;
  villani?: VillaniRunData;
}

export type ParsedSession = ParseSessionResult & { path?: string };

export interface SessionCandidate {
  provider: Provider;
  path: string;
  mtimeMs: number;
  size: number;
  cwd?: string;
  sessionId?: string;
  model?: string;
  eventCount?: number;
  warnings: string[];
}
