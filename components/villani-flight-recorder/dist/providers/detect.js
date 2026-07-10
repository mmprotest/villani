import { obj } from "../normalize/events.js";
import { readJsonl } from "../utils/jsonl.js";
export class ProviderMismatchError extends Error {
    constructor(provider, sessionPath) {
        super(`File is not a confident ${provider} session: ${sessionPath}`);
        this.name = "ProviderMismatchError";
    }
}
export async function assertProviderSession(provider, sessionPath) {
    const recs = await readJsonl(sessionPath);
    let confidence = 0;
    for (const r of recs) {
        if (r.error)
            continue;
        const o = obj(r.value);
        const type = String(o.type ?? "");
        const msg = obj(o.message);
        const content = Array.isArray(msg.content) ? msg.content : [];
        if (provider === "codex") {
            if (type === "session_meta")
                confidence += 4;
            if (type === "tool_call" && "arguments" in o)
                confidence += 2;
            if (type === "tool_result" &&
                ("stdout" in o || "stderr" in o || "exit_code" in o))
                confidence += 2;
            if (typeof o.model === "string" && /codex|gpt-.*codex/i.test(o.model))
                confidence += 3;
            if (o.hook_event_name &&
                !("tool_response" in o) &&
                /codex/i.test(String(o.model ?? o.transcript_path ?? "")))
                confidence += 3;
        }
        else if (provider === "claude") {
            if (msg.role === "assistant" &&
                typeof msg.model === "string" &&
                /claude/i.test(msg.model))
                confidence += 4;
            if (content.some((b) => obj(b).type === "tool_use" &&
                typeof obj(b).id === "string" &&
                String(obj(b).id).startsWith("toolu_")))
                confidence += 3;
            if (content.some((b) => obj(b).type === "tool_result" &&
                typeof obj(b).tool_use_id === "string"))
                confidence += 3;
            if (o.hook_event_name && "tool_response" in o)
                confidence += 3;
        }
        else if (provider === "pi") {
            if (type === "session_start" &&
                typeof o.model === "string" &&
                !/claude|codex/i.test(o.model))
                confidence += 3;
            if (type === "tool_execution_start" ||
                type === "tool_execution_update" ||
                type === "tool_execution_end")
                confidence += 3;
            if (type === "branch_summary" || type === "model_change")
                confidence += 2;
            if (typeof o.session_id === "string" && o.session_id.startsWith("pi_"))
                confidence += 2;
        }
    }
    if (confidence < 3)
        throw new ProviderMismatchError(provider, sessionPath);
}
