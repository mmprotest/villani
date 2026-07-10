import fg from "fast-glob";
import { parseClaudeSession } from "./claude.js";
import { parseCodexSession } from "./codex.js";
import { parsePiSession } from "./pi.js";
import { parseGeneric } from "./generic.js";
import { defaultRoots } from "../scanners/findSessions.js";
async function discoverFiles(provider, roots, patterns) {
    const rs = roots?.length
        ? roots
        : defaultRoots(provider).map((r) => r.root);
    const out = [];
    for (const root of rs) {
        try {
            const files = await fg(patterns, {
                cwd: root,
                absolute: true,
                onlyFiles: true,
                ignore: ["**/node_modules/**", "**/.git/**"],
            });
            out.push(...files.map((f) => ({
                provider,
                sourcePath: f,
                sourceKind: "file",
                confidence: "medium",
                reason: `${provider} session file`,
            })));
        }
        catch { }
    }
    return out;
}
function wrap(provider, label, parser, patterns = ["**/*.jsonl"]) {
    return {
        id: provider,
        label,
        discover: (o) => discoverFiles(provider, o.roots, patterns),
        parse: async (d) => {
            const p = await parser(d.sourcePath);
            return { ...p, provider, sourcePath: d.sourcePath };
        },
    };
}
export const claudeAdapter = wrap("claude", "Claude", parseClaudeSession);
export const codexAdapter = wrap("codex", "Codex", parseCodexSession, [
    "**/*.jsonl",
    "**/*.json",
    "**/*.session",
]);
export const piAdapter = wrap("pi", "Pi", parsePiSession, [
    "**/*.jsonl",
    "**/*.json",
]);
export const genericAdapter = wrap("generic", "Generic", (p) => parseGeneric("unknown", p), ["**/*.jsonl", "**/*.json"]);
export function adaptersFor(agent, all = false) {
    const map = {
        claude: claudeAdapter,
        codex: codexAdapter,
        pi: piAdapter,
        generic: genericAdapter,
    };
    if (agent)
        return [map[agent]].filter(Boolean);
    return all
        ? [claudeAdapter, codexAdapter, piAdapter, genericAdapter]
        : [claudeAdapter, codexAdapter, piAdapter, genericAdapter];
}
