import fg from "fast-glob";
import { parseClaudeSession } from "./claude.js";
import { parseCodexSession } from "./codex.js";
import { parsePiSession } from "./pi.js";
import { parseGeneric } from "./generic.js";
import {
  DiscoveredSession,
  DiscoveryOptions,
  ProviderAdapter,
  ProviderId,
} from "../index/sessionTypes.js";
import { ParsedSession } from "./types.js";
import { defaultRoots } from "../scanners/findSessions.js";
async function discoverFiles(
  provider: ProviderId,
  roots: string[] | undefined,
  patterns: string[],
): Promise<DiscoveredSession[]> {
  const rs = roots?.length
    ? roots
    : defaultRoots(provider as any).map((r) => r.root);
  const out: DiscoveredSession[] = [];
  for (const root of rs) {
    try {
      const files = await fg(patterns, {
        cwd: root,
        absolute: true,
        onlyFiles: true,
        ignore: ["**/node_modules/**", "**/.git/**"],
      });
      out.push(
        ...files.map((f) => ({
          provider,
          sourcePath: f,
          sourceKind: "file" as const,
          confidence: "medium" as const,
          reason: `${provider} session file`,
        })),
      );
    } catch {}
  }
  return out;
}
function wrap(
  provider: ProviderId,
  label: string,
  parser: (p: string) => Promise<ParsedSession>,
  patterns = ["**/*.jsonl"],
): ProviderAdapter {
  return {
    id: provider,
    label,
    discover: (o: DiscoveryOptions) =>
      discoverFiles(provider, o.roots, patterns),
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
export const genericAdapter = wrap(
  "generic",
  "Generic",
  (p) => parseGeneric("unknown", p),
  ["**/*.jsonl", "**/*.json"],
);
export function adaptersFor(agent?: string, all = false) {
  const map = {
    claude: claudeAdapter,
    codex: codexAdapter,
    pi: piAdapter,
    generic: genericAdapter,
  };
  if (agent) return [map[agent as keyof typeof map]].filter(Boolean);
  return all
    ? [claudeAdapter, codexAdapter, piAdapter, genericAdapter]
    : [claudeAdapter, codexAdapter, piAdapter, genericAdapter];
}
