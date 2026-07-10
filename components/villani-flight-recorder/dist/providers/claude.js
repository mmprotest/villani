import { event, isTestCommand, makeHumanEventTitle, obj, textOf, } from "../normalize/events.js";
import { readJsonl } from "../utils/jsonl.js";
import { blocks, contentText } from "./helpers/content.js";
import { timestampOf } from "./helpers/timestamps.js";
import { extractTokenUsage } from "./helpers/tokens.js";
import { classifyTool } from "./helpers/tools.js";
import { finish } from "./generic.js";
import { assertProviderSession } from "./detect.js";
export async function parseClaudeSession(sessionPath) {
    await assertProviderSession("claude", sessionPath);
    const recs = await readJsonl(sessionPath);
    const events = [];
    const warnings = [];
    let n = 0;
    const toolUsesById = new Map();
    const push = (e) => events.push({ ...e, title: e.title || makeHumanEventTitle(e) });
    for (const r of recs) {
        if (r.error) {
            const w = `Line ${r.line}: ${r.error}`;
            warnings.push(w);
            push(event(`claude-${++n}`, "claude", "error", "Invalid JSONL line", { raw: r.raw }, { warnings: [w] }));
            continue;
        }
        const o = obj(r.value);
        const ts = timestampOf(o);
        const sessionId = String(o.session_id ?? "") || undefined;
        const cwd = typeof o.cwd === "string" ? o.cwd : undefined;
        if (o.hook_event_name) {
            const name = String(o.tool_name ?? "hook");
            const c = classifyTool(name, o.tool_input);
            const exitCode = typeof obj(o.tool_response).exit_code === "number"
                ? obj(o.tool_response).exit_code
                : undefined;
            push(event(`claude-${++n}`, "claude", exitCode && exitCode !== 0 ? "error" : c.type, `Claude hook: ${String(o.hook_event_name)} ${name}`, r.value, {
                timestamp: ts,
                sessionId,
                cwd,
                command: c.command,
                path: c.path,
                exitCode,
                stdout: textOf(obj(o.tool_response).stdout),
                stderr: textOf(obj(o.tool_response).stderr),
                summary: textOf(o.transcript_path),
            }));
            continue;
        }
        const msg = obj(o.message);
        const content = msg.content ?? o.content;
        const role = String(msg.role ?? o.type ?? "");
        const model = typeof msg.model === "string" ? msg.model : undefined;
        if (role === "user" || o.type === "user") {
            const text = contentText(content);
            if (text)
                push(event(`claude-${++n}`, "claude", "user_message", "User prompt", r.value, { timestamp: ts, sessionId, cwd, summary: text }));
            for (const b of blocks(content).filter((b) => b.type === "tool_result")) {
                const toolUseId = typeof b.tool_use_id === "string" ? b.tool_use_id : undefined;
                const remembered = toolUseId ? toolUsesById.get(toolUseId) : undefined;
                const failed = b.is_error === true;
                const summary = textOf(b.content);
                if (remembered) {
                    const isBash = remembered.name.toLowerCase() === "bash" ||
                        Boolean(remembered.command);
                    const isTest = Boolean(remembered.command && isTestCommand(remembered.command));
                    const type = isBash
                        ? isTest
                            ? "test_run"
                            : "bash_command"
                        : "tool_result";
                    const title = failed
                        ? isTest
                            ? `Test failed: ${remembered.command}`
                            : remembered.command
                                ? `Command failed: ${remembered.command}`
                                : `${remembered.name} tool failed`
                        : remembered.command
                            ? `Command completed: ${remembered.command}`
                            : `${remembered.name} result`;
                    push(event(`claude-${++n}`, "claude", type, title, {
                        ...b,
                        tool_name: remembered.name,
                        tool_input: remembered.input,
                    }, {
                        timestamp: ts,
                        sessionId,
                        cwd,
                        command: remembered.command,
                        path: remembered.path,
                        exitCode: failed && isBash ? 1 : undefined,
                        summary,
                        warnings: failed && !isBash
                            ? ["Tool result marked as error"]
                            : undefined,
                    }));
                }
                else {
                    push(event(`claude-${++n}`, "claude", failed ? "tool_result" : "tool_result", failed ? "Tool result failed" : "Tool result", b, {
                        timestamp: ts,
                        sessionId,
                        cwd,
                        summary,
                        warnings: failed
                            ? ["Unmatched tool_result marked as error"]
                            : undefined,
                    }));
                }
            }
            continue;
        }
        if (role === "assistant" || o.type === "assistant") {
            const text = contentText(content);
            const tokenUsage = extractTokenUsage(o);
            let tokenUsageAttached = false;
            if (text) {
                tokenUsageAttached = Boolean(tokenUsage);
                push(event(`claude-${++n}`, "claude", "assistant_message", "Assistant response", r.value, {
                    timestamp: ts,
                    sessionId,
                    cwd,
                    summary: text,
                    raw: { ...o, model },
                    tokenUsage,
                }));
            }
            for (const b of blocks(content).filter((b) => b.type === "tool_use")) {
                const name = String(b.name ?? "tool");
                const c = classifyTool(name, b.input);
                const eventId = `claude-${++n}`;
                if (typeof b.id === "string")
                    toolUsesById.set(b.id, {
                        id: b.id,
                        name,
                        command: c.command,
                        path: c.path,
                        input: b.input,
                        eventId,
                    });
                push(event(eventId, "claude", c.type, c.title ?? "", { ...obj(r.value), tool_use_id: b.id }, {
                    timestamp: ts,
                    sessionId,
                    cwd,
                    command: c.command,
                    path: c.path,
                    summary: name,
                    tokenUsage: !tokenUsageAttached ? tokenUsage : undefined,
                }));
                tokenUsageAttached = tokenUsageAttached || Boolean(tokenUsage);
            }
            continue;
        }
        const original = String(o.type ?? "unknown");
        push(event(`claude-${++n}`, "claude", "unknown", `Unknown Claude event: ${original}`, r.value, { timestamp: ts, sessionId, cwd, summary: textOf(o) }));
    }
    const out = finish("claude", sessionPath, events, warnings, recs.map((r) => r.value));
    const model = recs
        .map((r) => obj(obj(r.value).message).model ?? obj(r.value).model)
        .find((x) => typeof x === "string");
    if (typeof model === "string")
        out.model = model;
    return out;
}
