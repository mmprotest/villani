import fg from "fast-glob";
import { parseClaudeSession } from "./claude.js";
import { parseCodexSession } from "./codex.js";
import { parsePiSession } from "./pi.js";
import { parseGeneric } from "./generic.js";
import { corruptVillaniRun, parseVillaniRun } from "./villani.js";
import {
  DiscoveredSession,
  DiscoveryOptions,
  ProviderAdapter,
  ProviderId,
} from "../index/sessionTypes.js";
import { ParsedSession } from "./types.js";
import { defaultRoots } from "../scanners/findSessions.js";
import {
  defaultVillaniRunsRoot,
  findVillaniRuns,
} from "../scanners/findVillaniRuns.js";
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
export const villaniAdapter: ProviderAdapter = {
  id: "villani",
  label: "Villani",
  discover: async (options) =>
    (await findVillaniRuns(options.roots ?? [defaultVillaniRunsRoot()])).map(
      (run) => ({
        provider: "villani",
        sourcePath: run.runPath,
        sourceKind: "directory" as const,
        confidence: "high" as const,
        reason: run.error
          ? `Villani canonical run directory: ${run.error}`
          : "Villani canonical run directory",
      }),
    ),
  parse: async (discovered) => {
    if (discovered.sourceKind !== "directory")
      throw new Error(
        `Villani provider requires a canonical run directory: ${discovered.sourcePath}`,
      );
    try {
      return {
        ...(await parseVillaniRun(discovered.sourcePath)),
        provider: "villani" as const,
        sourcePath: discovered.sourcePath,
      };
    } catch (error) {
      return {
        ...corruptVillaniRun(discovered.sourcePath, error),
        provider: "villani" as const,
        sourcePath: discovered.sourcePath,
      };
    }
  },
};
export function adaptersFor(agent?: string, all = false) {
  const map = {
    villani: villaniAdapter,
    claude: claudeAdapter,
    codex: codexAdapter,
    pi: piAdapter,
    generic: genericAdapter,
  };
  if (agent) return [map[agent as keyof typeof map]].filter(Boolean);
  return all
    ? [villaniAdapter, claudeAdapter, codexAdapter, piAdapter, genericAdapter]
    : [villaniAdapter, claudeAdapter, codexAdapter, piAdapter, genericAdapter];
}
