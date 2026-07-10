import fs from "node:fs/promises";
import path from "node:path";
import fg from "fast-glob";
import { SessionCandidate, Provider } from "../providers/types.js";
import { expandHome } from "../utils/paths.js";
import { parseClaudeSession } from "../providers/claude.js";
import { parseCodexSession } from "../providers/codex.js";
import { parsePiSession } from "../providers/pi.js";

const parsers = {
  claude: parseClaudeSession,
  codex: parseCodexSession,
  pi: parsePiSession,
} as const;
const real = (p: Provider): p is keyof typeof parsers =>
  p === "claude" || p === "codex" || p === "pi";

export function defaultRoots(
  provider?: Provider,
): { provider: keyof typeof parsers; root: string }[] {
  const codexHome = process.env.CODEX_HOME
    ? path.join(process.env.CODEX_HOME, "sessions")
    : expandHome("~/.codex/sessions");
  const roots = [
    { provider: "claude" as const, root: expandHome("~/.claude") },
    { provider: "claude" as const, root: expandHome("~/.claude/projects") },
    { provider: "codex" as const, root: codexHome },
    { provider: "codex" as const, root: expandHome("~/.codex") },
    { provider: "codex" as const, root: expandHome("~/.codex/sessions") },
    { provider: "pi" as const, root: expandHome("~/.pi") },
    { provider: "pi" as const, root: expandHome("~/.pi/agent/sessions") },
  ];
  const seen = new Set<string>();
  return roots.filter(
    (r) =>
      (!provider || r.provider === provider) &&
      !seen.has(path.resolve(r.root)) &&
      seen.add(path.resolve(r.root)),
  );
}

export async function findSessions(
  opts: { provider?: Provider; roots?: string[]; cwd?: string } = {},
): Promise<SessionCandidate[]> {
  const roots = opts.roots?.length
    ? opts.roots.flatMap((root) =>
        (real(opts.provider ?? "unknown")
          ? [opts.provider as keyof typeof parsers]
          : (["claude", "codex", "pi"] as const)
        ).map((provider) => ({ provider, root: expandHome(root) })),
      )
    : defaultRoots(opts.provider);
  const out: SessionCandidate[] = [];
  for (const { provider, root } of roots) {
    try {
      await fs.access(root);
    } catch {
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
        const sessionPath = path.resolve(f);
        const st = await fs.stat(sessionPath);
        const parsed = await parsers[provider](sessionPath);
        out.push({
          provider,
          path: sessionPath,
          mtimeMs: st.mtimeMs,
          size: st.size,
          cwd: parsed.cwd,
          sessionId: parsed.sessionId,
          model: parsed.model,
          eventCount: parsed.events.length,
          warnings: parsed.warnings,
        });
      } catch {
        // Provider-specific scans are strict: files that cannot be confidently
        // identified by the selected provider parser are omitted rather than
        // reported with a misleading provider label.
      }
    }
  }
  return out.sort((a, b) => b.mtimeMs - a.mtimeMs);
}

export function chooseLatest(s: SessionCandidate[], cwd = process.cwd()) {
  const root = path.resolve(cwd);
  const match = s.find((x) => x.cwd && root.startsWith(path.resolve(x.cwd)));
  return { candidate: match ?? s[0], uncertain: !match };
}
