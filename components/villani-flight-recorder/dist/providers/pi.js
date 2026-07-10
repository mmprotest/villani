import { event, makeHumanEventTitle, obj, textOf, } from "../normalize/events.js";
import { readJsonl } from "../utils/jsonl.js";
import { timestampOf } from "./helpers/timestamps.js";
import { extractTokenUsage } from "./helpers/tokens.js";
import { classifyTool } from "./helpers/tools.js";
import { finish } from "./generic.js";
import { assertProviderSession } from "./detect.js";
export async function parsePiSession(sessionPath) {
    await assertProviderSession("pi", sessionPath);
    const recs = await readJsonl(sessionPath);
    const events = [];
    const warnings = [];
    let n = 0;
    let lastCommand;
    const push = (e) => events.push({ ...e, title: e.title || makeHumanEventTitle(e) });
    for (const r of recs) {
        if (r.error) {
            const w = `Line ${r.line}: ${r.error}`;
            warnings.push(w);
            push(event(`pi-${++n}`, "pi", "error", "Invalid JSONL line", { raw: r.raw }, { warnings: [w] }));
            continue;
        }
        const o = obj(r.value);
        const ts = timestampOf(o);
        const sessionId = String(o.session_id ?? "") || undefined;
        const cwd = typeof o.cwd === "string" ? o.cwd : undefined;
        const type = String(o.type ?? "unknown");
        if (type === "session_start") {
            push(event(`pi-${++n}`, "pi", "session_start", "Session started", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                summary: textOf(o.model),
            }));
            continue;
        }
        if (type === "session_end") {
            push(event(`pi-${++n}`, "pi", "session_end", "Session ended", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
            }));
            continue;
        }
        if (type === "message") {
            const role = String(o.role);
            push(event(`pi-${++n}`, "pi", role === "user" ? "user_message" : "assistant_message", role === "user" ? "User prompt" : "Assistant response", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                summary: textOf(o.content),
                tokenUsage: role === "user" ? undefined : extractTokenUsage(o),
            }));
            continue;
        }
        if (type === "tool_call" || type === "tool_execution_start") {
            const name = String(o.tool_name ?? o.name ?? "tool");
            const c = classifyTool(name, o.args ?? o.input);
            lastCommand = c.command ?? lastCommand;
            push(event(`pi-${++n}`, "pi", c.type, type === "tool_execution_start" ? `Pi tool started: ${name}` : "", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                command: c.command,
                path: c.path,
                summary: name,
            }));
            continue;
        }
        if (type === "tool_execution_update") {
            push(event(`pi-${++n}`, "pi", "tool_result", `Pi tool update: ${String(o.tool_name ?? "tool")}`, r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                summary: textOf(o.update ?? o.message),
            }));
            continue;
        }
        if (type === "tool_execution_end") {
            const res = obj(o.result);
            const code = typeof res.exit_code === "number" ? res.exit_code : undefined;
            push(event(`pi-${++n}`, "pi", code && code !== 0 ? "error" : "tool_result", code && code !== 0
                ? `Command failed: ${lastCommand ?? String(o.tool_name ?? "tool")}`
                : `Pi tool finished: ${String(o.tool_name ?? "tool")}`, r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                command: lastCommand,
                exitCode: code,
                stdout: textOf(res.stdout),
                stderr: textOf(res.stderr),
            }));
            continue;
        }
        if (type === "branch_summary") {
            push(event(`pi-${++n}`, "pi", "assistant_message", "Branch summary", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                summary: textOf(o.summary),
                tokenUsage: extractTokenUsage(o),
            }));
            continue;
        }
        if (type === "model_change") {
            push(event(`pi-${++n}`, "pi", "tool_call", `Model changed: ${String(o.from)} to ${String(o.to)}`, r.value, { timestamp: ts, sessionId, cwd }));
            continue;
        }
        push(event(`pi-${++n}`, "pi", "unknown", `Unknown Pi event: ${type}`, r.value, {
            timestamp: ts,
            sessionId,
            cwd,
            summary: textOf(o.summary ?? o.content),
        }));
    }
    return finish("pi", sessionPath, events, warnings, recs.map((r) => r.value));
}
