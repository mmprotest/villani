import { event, extractPatchTouchedFiles, makeHumanEventTitle, obj, textOf, } from "../normalize/events.js";
import { readJsonl } from "../utils/jsonl.js";
import { timestampOf } from "./helpers/timestamps.js";
import { extractTokenUsage } from "./helpers/tokens.js";
import { classifyTool } from "./helpers/tools.js";
import { finish } from "./generic.js";
import { assertProviderSession } from "./detect.js";
export async function parseCodexSession(sessionPath) {
    await assertProviderSession("codex", sessionPath);
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
            push(event(`codex-${++n}`, "codex", "error", "Invalid JSONL line", { raw: r.raw }, { warnings: [w] }));
            continue;
        }
        const o = obj(r.value);
        const ts = timestampOf(o);
        const sessionId = String(o.session_id ?? "") || undefined;
        const cwd = typeof o.cwd === "string" ? o.cwd : undefined;
        const type = String(o.type ?? "unknown");
        if (o.hook_event_name) {
            const name = String(o.tool_name ?? "hook");
            const c = classifyTool(name, o.tool_input);
            push(event(`codex-${++n}`, "codex", c.type, `Codex hook: ${String(o.hook_event_name)} ${name}`, r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                command: c.command,
                path: c.path,
                summary: textOf(o.transcript_path),
            }));
            continue;
        }
        if (type === "session_meta") {
            push(event(`codex-${++n}`, "codex", "session_start", "Session started", r.value, { timestamp: ts, sessionId, cwd, summary: String(o.model ?? "") }));
            continue;
        }
        if (type === "user_message") {
            push(event(`codex-${++n}`, "codex", "user_message", "User prompt", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                summary: textOf(o.message),
            }));
            continue;
        }
        if (type === "assistant_message") {
            push(event(`codex-${++n}`, "codex", "assistant_message", "Assistant response", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                summary: textOf(o.message),
                tokenUsage: extractTokenUsage(o),
            }));
            continue;
        }
        if (type === "tool_call") {
            const name = String(o.name ?? o.tool_name ?? "tool");
            const c = classifyTool(name, o.arguments ?? o.args ?? o.input);
            lastCommand = c.command ?? lastCommand;
            push(event(`codex-${++n}`, "codex", c.type, name.toLowerCase().includes("mcp") ? "MCP tool call" : "", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                command: c.command,
                path: c.path,
                summary: name,
            }));
            continue;
        }
        if (type === "tool_result") {
            const exitCode = typeof o.exit_code === "number" ? o.exit_code : undefined;
            push(event(`codex-${++n}`, "codex", exitCode && exitCode !== 0 ? "error" : "tool_result", "", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                command: lastCommand,
                exitCode,
                stdout: textOf(o.stdout),
                stderr: textOf(o.stderr),
            }));
            continue;
        }
        if (type === "apply_patch") {
            const patch = String(o.patch ?? "");
            const files = extractPatchTouchedFiles(patch);
            for (const f of files.length ? files : [undefined])
                push(event(`codex-${++n}`, "codex", "file_edit", f ? `Applied patch to ${f}` : "Applied patch", r.value, { timestamp: ts, sessionId, cwd, path: f, diff: patch }));
            continue;
        }
        if (/approval|permission/i.test(type)) {
            push(event(`codex-${++n}`, "codex", "approval", "Approval requested", r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                summary: textOf(o.message ?? o.reason),
            }));
            continue;
        }
        push(event(`codex-${++n}`, "codex", "unknown", `Unknown Codex event: ${type}`, r.value, {
            timestamp: ts,
            sessionId,
            cwd,
            summary: textOf(o),
            tokenUsage: extractTokenUsage(o),
        }));
    }
    return finish("codex", sessionPath, events, warnings, recs.map((r) => r.value));
}
