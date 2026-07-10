import fs from "node:fs/promises";
import path from "node:path";
import fg from "fast-glob";
import { expandHome } from "../utils/paths.js";
import { parseClaudeSession } from "../providers/claude.js";
import { parseCodexSession } from "../providers/codex.js";
import { parsePiSession } from "../providers/pi.js";
const parsers = {
    claude: parseClaudeSession,
    codex: parseCodexSession,
    pi: parsePiSession,
};
const real = (p) => p === "claude" || p === "codex" || p === "pi";
export function defaultRoots(provider) {
    const codexHome = process.env.CODEX_HOME
        ? path.join(process.env.CODEX_HOME, "sessions")
        : expandHome("~/.codex/sessions");
    const roots = [
        { provider: "claude", root: expandHome("~/.claude") },
        { provider: "claude", root: expandHome("~/.claude/projects") },
        { provider: "codex", root: codexHome },
        { provider: "codex", root: expandHome("~/.codex") },
        { provider: "codex", root: expandHome("~/.codex/sessions") },
        { provider: "pi", root: expandHome("~/.pi") },
        { provider: "pi", root: expandHome("~/.pi/agent/sessions") },
    ];
    const seen = new Set();
    return roots.filter((r) => (!provider || r.provider === provider) &&
        !seen.has(path.resolve(r.root)) &&
        seen.add(path.resolve(r.root)));
}
export async function findSessions(opts = {}) {
    const roots = opts.roots?.length
        ? opts.roots.flatMap((root) => (real(opts.provider ?? "unknown")
            ? [opts.provider]
            : ["claude", "codex", "pi"]).map((provider) => ({ provider, root: expandHome(root) })))
        : defaultRoots(opts.provider);
    const out = [];
    for (const { provider, root } of roots) {
        try {
            await fs.access(root);
        }
        catch {
            continue;
        }
        const files = await fg("**/*.jsonl", {
            cwd: root,
            absolute: true,
            onlyFiles: true,
            suppressErrors: true,
        });
        for (const f of files) {
            try {
                const st = await fs.stat(f);
                const parsed = await parsers[provider](f);
                out.push({
                    provider,
                    path: f,
                    mtimeMs: st.mtimeMs,
                    size: st.size,
                    cwd: parsed.cwd,
                    sessionId: parsed.sessionId,
                    model: parsed.model,
                    eventCount: parsed.events.length,
                    warnings: parsed.warnings,
                });
            }
            catch {
                // Provider-specific scans are strict: files that cannot be confidently
                // identified by the selected provider parser are omitted rather than
                // reported with a misleading provider label.
            }
        }
    }
    return out.sort((a, b) => b.mtimeMs - a.mtimeMs);
}
export function chooseLatest(s, cwd = process.cwd()) {
    const root = path.resolve(cwd);
    const match = s.find((x) => x.cwd && root.startsWith(path.resolve(x.cwd)));
    return { candidate: match ?? s[0], uncertain: !match };
}
