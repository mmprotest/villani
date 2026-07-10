import { readJsonl } from "../utils/jsonl.js";
import {
  event,
  extractCommand,
  extractPath,
  isTestCommand,
  makeHumanEventTitle,
  obj,
  textOf,
} from "../normalize/events.js";
import {
  FlightEvent,
  FlightEventType,
  ParsedSession,
  Provider,
} from "./types.js";
import { timestampOf } from "./helpers/timestamps.js";
import { extractTokenUsage } from "./helpers/tokens.js";

export async function parseGeneric(
  provider: Provider,
  sessionPath: string,
): Promise<ParsedSession> {
  const recs = await readJsonl(sessionPath);
  const events: FlightEvent[] = [];
  const warnings: string[] = [];
  let n = 0;
  for (const r of recs) {
    if (r.error) {
      const w = `Line ${r.line}: ${r.error}`;
      warnings.push(w);
      events.push(
        event(
          `${provider}-${++n}`,
          provider,
          "error",
          "Invalid JSONL line",
          { raw: r.raw },
          { warnings: [w] },
        ),
      );
      continue;
    }
    const o = obj(r.value);
    const typeName = String(o.type ?? o.event ?? o.kind ?? o.role ?? "unknown");
    const input = o.input ?? o.args ?? o.arguments ?? o.parameters ?? o;
    const command = extractCommand(input);
    const path = extractPath(input);
    let type: FlightEventType = "unknown";
    if (/user/i.test(typeName)) type = "user_message";
    else if (/assistant/i.test(typeName)) type = "assistant_message";
    else if (command)
      type = isTestCommand(command) ? "test_run" : "bash_command";
    else if (path && /read/i.test(typeName)) type = "file_read";
    else if (path && /(edit|patch)/i.test(typeName)) type = "file_edit";
    else if (path && /write/i.test(typeName)) type = "file_write";
    const e: FlightEvent = event(
      `${provider}-${++n}`,
      provider,
      type,
      "",
      r.value,
      {
        sessionId: typeof o.session_id === "string" ? o.session_id : undefined,
        cwd: typeof o.cwd === "string" ? o.cwd : undefined,
        timestamp: timestampOf(o),
        summary: textOf(o.message ?? o.content ?? o.summary ?? o.result),
        command,
        path,
        exitCode:
          typeof o.exitCode === "number"
            ? o.exitCode
            : typeof o.exit_code === "number"
              ? o.exit_code
              : undefined,
        durationMs:
          typeof o.durationMs === "number"
            ? o.durationMs
            : typeof o.duration_ms === "number"
              ? o.duration_ms
              : undefined,
        tokenUsage: extractTokenUsage(r.value),
        title: "",
      },
    );
    e.title =
      type === "unknown"
        ? ["generic", "unknown"].includes(String(provider))
          ? `Unknown event: ${typeName}`
          : `Unknown ${provider[0].toUpperCase()}${provider.slice(1)} event: ${typeName}`
        : makeHumanEventTitle(e);
    events.push(e);
  }
  return finish(
    provider,
    sessionPath,
    events,
    warnings,
    recs.map((r) => r.value),
  );
}

export function finish(
  provider: Provider,
  sessionPath: string,
  events: FlightEvent[],
  warnings: string[],
  records: unknown[],
): ParsedSession {
  const meta =
    records
      .map(obj)
      .find((o) => o.session_id || o.sessionId || o.cwd || o.model) ?? {};
  return {
    provider,
    path: sessionPath,
    sessionPath,
    sessionId:
      events.find((e) => e.sessionId)?.sessionId ??
      (typeof meta.session_id === "string" ? meta.session_id : undefined),
    cwd:
      events.find((e) => e.cwd)?.cwd ??
      (typeof meta.cwd === "string" ? meta.cwd : undefined),
    model: typeof meta.model === "string" ? meta.model : undefined,
    startedAt: events.find((e) => e.timestamp)?.timestamp,
    endedAt: [...events].reverse().find((e) => e.timestamp)?.timestamp,
    events,
    warnings,
  };
}
