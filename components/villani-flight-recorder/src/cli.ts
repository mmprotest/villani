#!/usr/bin/env node
import { Command } from "commander";
import { findSessions, chooseLatest } from "./scanners/findSessions.js";
import { parseClaudeSession } from "./providers/claude.js";
import { parseCodexSession } from "./providers/codex.js";
import { parsePiSession } from "./providers/pi.js";
import { parseGeneric } from "./providers/generic.js";
import { parseVillaniRun } from "./providers/villani.js";
import { renderReplay } from "./render/renderReplay.js";
import { openBrowser } from "./utils/openBrowser.js";
import { buildGitReplay } from "./git/gitReplay.js";
import { installHooks, appendHook } from "./hooks/installHooks.js";
import { Provider, ParsedSession } from "./providers/types.js";
import { scanToIndex } from "./index/sessionIndex.js";
import { readIndex, defaultIndexDir } from "./index/sessionStore.js";
import { renderSessionBrowser } from "./render/sessionBrowser.js";
import { formatTokenCount } from "./providers/helpers/tokens.js";
import { adaptersFor } from "./providers/providerAdapter.js";
import fs from "node:fs/promises";
import path from "node:path";
import {
  assertOutsideVillaniRunsRoot,
  launchVillaniRun,
} from "./commands/launchVillani.js";
import { defaultVillaniRunsRoot } from "./scanners/findVillaniRuns.js";

const program = new Command();
program
  .name("villani-flight-recorder")
  .description(
    "Black box recorder for AI coding agents\n\nCommon workflow:\n  vfr scan\n  vfr browse\n  vfr replay --id <session-id>",
  )
  .version("0.1.0");

const RENDERER_VERSION = "0.1.0-no-coverage-v2";
type ReplayManifestEntry = {
  sessionId: string;
  sourcePath: string;
  sourceHash: string;
  replayPath: string;
  generatedAt: string;
  rendererVersion: string;
};
type ReplayManifest = { version: number; entries: ReplayManifestEntry[] };
async function readManifest(replayDir: string): Promise<ReplayManifest> {
  try {
    return JSON.parse(
      await fs.readFile(path.join(replayDir, "manifest.json"), "utf8"),
    );
  } catch {
    return { version: 1, entries: [] };
  }
}
async function writeManifest(replayDir: string, manifest: ReplayManifest) {
  await fs.writeFile(
    path.join(replayDir, "manifest.json"),
    JSON.stringify(manifest, null, 2),
  );
}
async function prepareReplayCache(
  idx: any,
  opts: {
    base: string;
    out: string;
    rebuild?: boolean;
    progress?: (m: string) => void;
  },
) {
  const replayDir = path.join(opts.base, "replays");
  await fs.mkdir(replayDir, { recursive: true });
  const manifest = await readManifest(replayDir);
  const byId = new Map(manifest.entries.map((e) => [e.sessionId, e]));
  let reused = 0,
    generated = 0,
    skipped = 0;
  const next: ReplayManifestEntry[] = [];
  const returnHref =
    path.relative(replayDir, opts.out) || path.basename(opts.out);
  opts.progress?.("Preparing replay cache...");
  for (const s of idx.sessions) {
    const replayPath = path.join(replayDir, `${s.id}.html`);
    const old = byId.get(s.id);
    const reusable =
      !opts.rebuild &&
      old &&
      old.sourceHash === s.sourceHash &&
      old.rendererVersion === RENDERER_VERSION &&
      (await fs
        .stat(replayPath)
        .then(() => true)
        .catch(() => false));
    if (reusable) {
      reused++;
      next.push(old);
      continue;
    }
    const ad = adaptersFor(String(s.provider))[0];
    if (!ad) {
      skipped++;
      continue;
    }
    try {
      const parsed = await ad.parse({
        provider: s.provider,
        sourcePath: s.sourcePath,
        sourceKind: s.sourceKind,
        confidence: s.confidence,
        reason: "browser replay",
      });
      await renderReplay(parsed as unknown as ParsedSession, {
        out: replayPath,
        returnHref,
        returnLabel: "Back to sessions",
      });
      generated++;
      next.push({
        sessionId: s.id,
        sourcePath: s.sourcePath,
        sourceHash: s.sourceHash ?? "",
        replayPath,
        generatedAt: new Date().toISOString(),
        rendererVersion: RENDERER_VERSION,
      });
    } catch {
      skipped++;
    }
  }
  await writeManifest(replayDir, { version: 1, entries: next });
  return { replayDir, reused, generated, skipped };
}
function scanProgress(json?: boolean, quiet?: boolean) {
  return json || quiet ? undefined : (m: string) => console.error(m);
}
async function openFile(file: string) {
  openBrowser(file);
}

program
  .command("scan")
  .description(
    "Scans canonical Villani runs and known Claude, Codex, and Pi session directories by default. Use --root to scan a custom directory.",
  )
  .option("--all")
  .option("--agent <agent>")
  .option("--provider <provider>")
  .option(
    "--root <path>",
    "session root",
    (v, p: string[]) => [...(p ?? []), v],
    [],
  )
  .option("--since <date>")
  .option("--limit <n>")
  .option("--json")
  .option("--index-dir <path>")
  .option("--verbose")
  .option("--quiet")
  .option("--rebuild")
  .action(async (o) => {
    if ((o.agent ?? o.provider) === "villani") {
      for (const root of o.root?.length ? o.root : [defaultVillaniRunsRoot()])
        assertOutsideVillaniRunsRoot(root, o.indexDir);
    }
    const progress = scanProgress(o.json, o.quiet);
    progress?.("Scanning local sessions...");
    const result = await scanToIndex({
      agent: o.agent ?? o.provider,
      all: o.all,
      roots: o.root?.length ? o.root : undefined,
      limit: o.limit ? Number(o.limit) : undefined,
      indexDir: o.indexDir,
      rebuild: o.rebuild,
      progress: (e: any) => {
        if (e.stage === "discover") progress?.(e.message);
        else if (e.stage === "metadata-check" || e.stage === "summary")
          progress?.(e.message);
        else if (e.stage === "parse") {
          if (e.message) progress?.(e.message);
          else progress?.(`Parsed ${e.current} / ${e.total}`);
        } else if (e.stage === "write-index") progress?.(e.message);
      },
    });
    const failedCommands = result.index.sessions.reduce(
      (n, s) => n + s.failedCommandCount,
      0,
    );
    const summary = {
      sessions: result.index.sessions.length,
      taskSegments: result.index.taskSegments.length,
      repos: result.index.repos.length,
      failedCommands,
      warnings:
        result.index.warnings.length +
        result.index.sessions.reduce((n, s) => n + s.warningCount, 0),
      indexPath: result.indexPath,
    };
    if (o.json) return console.log(JSON.stringify(summary, null, 2));
    console.log("Villani Flight Recorder scan complete\n");
    console.log("Providers scanned:");
    for (const [k, v] of Object.entries(result.counts))
      console.log(`- ${k}: ${v} sessions`);
    console.log("\nIndexed:");
    console.log(`- ${summary.sessions} sessions`);
    console.log(`- ${summary.taskSegments} likely task segments`);
    console.log(`- ${summary.repos} repos`);
    console.log(`- ${summary.failedCommands} failed commands`);
    console.log(`- ${summary.warnings} recorder warnings`);
    console.log(
      "\nNext:\n  vfr sessions\n  vfr browse\n  vfr replay --id <session-id>",
    );
  });
async function parse(provider: Provider, file: string) {
  if (provider === "villani") return parseVillaniRun(file);
  if (provider === "claude") return parseClaudeSession(file);
  if (provider === "codex") return parseCodexSession(file);
  if (provider === "pi") return parsePiSession(file);
  return parseGeneric("unknown", file);
}

function projectMatches(s: any, q?: string) {
  if (!q) return true;
  const vals = [
    s.projectDisplayName,
    s.projectName,
    s.projectPath,
    s.projectRoot,
    s.projectId,
    ...(s.repoRoots ?? []),
    ...(s.repoIds ?? []),
  ].filter(Boolean);
  return vals.some((v: string) =>
    v.toLowerCase().includes(String(q).toLowerCase()),
  );
}
function repoMatches(values: string[], q?: string) {
  if (!q) return true;
  const r = path.resolve(q);
  return values.some((v) => v === q || path.resolve(v) === r || v.includes(q));
}
async function requireIndex(dir?: string) {
  const idx = await readIndex(dir);
  if (!idx) {
    console.log("No session index found. Run: vfr scan --all");
    return null;
  }
  return idx;
}
program
  .command("sessions")
  .option("--agent <agent>")
  .option("--provider <provider>")
  .option("--repo <repo>")
  .option("--project <name>")
  .option("--failed")
  .option("--limit <n>")
  .option("--json")
  .option("--index-dir <path>")
  .action(async (o) => {
    const idx = await requireIndex(o.indexDir);
    if (!idx) return;
    let rows = idx.sessions
      .filter(
        (s) =>
          (!o.agent || s.provider === o.agent) &&
          (!o.provider || s.provider === o.provider) &&
          (!o.failed || s.outcome === "failed" || s.failedCommandCount > 0) &&
          (!o.project || projectMatches(s, o.project)) &&
          repoMatches([...s.repoRoots, ...s.repoIds], o.repo),
      )
      .sort((a, b) =>
        String(b.updatedAt ?? b.lastEventAt ?? "").localeCompare(
          String(a.updatedAt ?? a.lastEventAt ?? ""),
        ),
      )
      .slice(0, o.limit ? Number(o.limit) : 20);
    if (o.json) return console.log(JSON.stringify(rows, null, 2));
    if (!rows.length)
      return console.log(
        "No sessions indexed yet. Run `vfr scan` to index local agent sessions.",
      );
    console.log(
      "ID                 Agent   Outcome  Project              Updated               Events  Failed  Tokens   Title / First Prompt",
    );
    for (const s of rows) {
      const project = (
        s.projectDisplayName ??
        s.projectName ??
        idx.repos.find((r) => s.repoIds.includes(r.id))?.name ??
        "-"
      ).slice(0, 20);
      const title = (s.title ?? s.firstPrompt ?? "-")
        .replace(/\s+/g, " ")
        .slice(0, 60);
      console.log(
        `${s.id.padEnd(18)} ${String(s.provider).padEnd(7)} ${String(s.outcome ?? "unknown").padEnd(8)} ${project.padEnd(20)} ${String(s.updatedAt ?? s.lastEventAt ?? "-").padEnd(20)} ${String(s.eventCount).padEnd(7)} ${String(s.failedCommandCount).padEnd(7)} ${formatTokenCount(s.tokenCount).padEnd(8)} ${title}`,
      );
    }
  });
program
  .command("tasks")
  .option("--session <session>")
  .option("--agent <agent>")
  .option("--provider <provider>")
  .option("--repo <repo>")
  .option("--project <name>")
  .option("--failed")
  .option("--limit <n>")
  .option("--json")
  .option("--index-dir <path>")
  .action(async (o) => {
    const idx = await requireIndex(o.indexDir);
    if (!idx) return;
    let rows = idx.taskSegments
      .filter(
        (t) =>
          (!o.session || t.sessionId === o.session) &&
          (!o.agent || t.provider === o.agent) &&
          repoMatches([...t.repoRoots, ...t.repoIds], o.repo),
      )
      .sort((a, b) =>
        String(b.lastEventAt ?? "").localeCompare(String(a.lastEventAt ?? "")),
      )
      .slice(0, o.limit ? Number(o.limit) : 20);
    if (o.json) return console.log(JSON.stringify(rows, null, 2));
    if (!rows.length)
      return console.log("No task segments found. Run: vfr scan --all");
    console.log("Likely task segments\n");
    rows.forEach((t, i) => {
      const repo = idx.repos.find((r) => t.repoIds.includes(r.id));
      console.log(
        `${i + 1}. ${t.id}\n   Title: ${t.title}\n   Agent: ${t.provider}\n   Repo: ${repo?.name ?? "Task unavailable"}\n   Events: ${t.eventCount}\n   Failed commands: ${t.failedCommandCount}\n   Boundary: ${t.boundaryReason}\n   Time: ${t.firstEventAt ?? "Duration unavailable"} to ${t.lastEventAt ?? "Duration unavailable"}\n`,
      );
    });
  });
program
  .command("browse")
  .description(
    "Generates a local HTML session browser from the indexed sessions.",
  )
  .option("--out <path>")
  .option("--index-dir <path>")
  .option("--open")
  .option("--rebuild")
  .option("--quiet")
  .action(async (o) => {
    const idx = await requireIndex(o.indexDir);
    if (!idx) return;
    const base = o.indexDir ?? defaultIndexDir();
    const out = o.out ?? path.join(base, "session-browser.html");
    const progress = o.quiet ? undefined : (m: string) => console.error(m);
    progress?.(`Reading index: ${idx.sessions.length} sessions.`);
    const cache = await prepareReplayCache(idx, {
      base,
      out,
      rebuild: o.rebuild,
      progress,
    });
    progress?.(`Reused ${cache.reused} replay files.`);
    progress?.(`Generated ${cache.generated} replay files.`);
    progress?.(`Skipped ${cache.skipped} failed replay generations.`);
    progress?.("Writing session browser...");
    await fs.mkdir(path.dirname(out), { recursive: true });
    await fs.writeFile(
      out,
      renderSessionBrowser(idx, {
        replayDir: cache.replayDir,
        browserOut: out,
      }),
    );
    console.log(`Session browser written to ${out}`);
    console.log("Open it in your browser.");
    if (o.open) openBrowser(out);
  });

program
  .command("launch")
  .description(
    "Scan sessions, refresh replay cache, generate the session browser, and open it.",
  )
  .option("--provider <provider>")
  .option("--agent <agent>")
  .option("--all")
  .option(
    "--root <path>",
    "session root",
    (v, p: string[]) => [...(p ?? []), v],
    [],
  )
  .option("--index-dir <path>")
  .option("--out <path>")
  .option("--run-id <id>")
  .option("--no-open")
  .option("--rebuild")
  .action(async (o) => {
    if (o.runId) {
      if (o.provider !== "villani")
        throw new Error("--run-id requires --provider villani");
      if (o.root?.length && o.root.length > 1)
        throw new Error("--run-id accepts exactly one --root");
      const file = await launchVillaniRun({
        root: o.root?.[0],
        runId: o.runId,
        out:
          o.out ??
          path.join(
            o.indexDir ?? defaultIndexDir(),
            "replays",
            `${o.runId}.html`,
          ),
        open: o.open,
      });
      console.log(`Villani run replay written to ${file}`);
      return;
    }
    if (o.provider === "villani") {
      for (const root of o.root?.length ? o.root : [defaultVillaniRunsRoot()])
        assertOutsideVillaniRunsRoot(root, o.indexDir, o.out);
    }
    console.error("Villani Flight Recorder launch\n");
    console.error("Scanning local sessions...");
    const result = await scanToIndex({
      agent: o.agent ?? o.provider,
      all: o.all,
      roots: o.root?.length ? o.root : undefined,
      indexDir: o.indexDir,
      rebuild: o.rebuild,
      progress: (e: any) => {
        if (e.stage === "discover") console.error(e.message);
        else if (e.stage === "metadata-check" || e.stage === "summary")
          console.error(e.message);
        else if (e.stage === "parse") {
          if (e.message) console.error(e.message);
          else console.error(`Parsed ${e.current} / ${e.total}`);
        } else if (e.stage === "write-index") console.error(e.message);
      },
    });
    console.error(`Skipped ${result.skippedUnchanged} unchanged sessions.`);
    console.error(
      `Parsed ${result.parsedNew + result.parsedChanged} new or changed sessions.`,
    );
    console.error(`Indexed ${result.index.sessions.length} sessions.`);
    const base = o.indexDir ?? defaultIndexDir();
    const out = o.out ?? path.join(base, "session-browser.html");
    console.error("\nPreparing session browser...");
    const cache = await prepareReplayCache(result.index, {
      base,
      out,
      rebuild: o.rebuild,
      progress: (m) => console.error(m),
    });
    console.error(`Reused ${cache.reused} replay files.`);
    console.error(`Generated ${cache.generated} replay files.`);
    if (cache.skipped)
      console.error(`Skipped ${cache.skipped} failed replay generations.`);
    await fs.mkdir(path.dirname(out), { recursive: true });
    await fs.writeFile(
      out,
      renderSessionBrowser(result.index, {
        replayDir: cache.replayDir,
        browserOut: out,
      }),
    );
    console.log(`Session browser written to ${out}`);
    if (o.open) {
      console.error("Opening browser...");
      await openFile(out);
    }
  });
program
  .command("open")
  .option("--out <path>")
  .option("--index-dir <path>")
  .action(async (o) => {
    const idx = await requireIndex(o.indexDir);
    if (!idx) return;
    const out = o.out ?? path.resolve("villani-flight-recorder-index.html");
    const html = `<!doctype html><meta charset="utf-8"><title>Villani Flight Recorder Index</title><style>body{font-family:system-ui;margin:2rem;line-height:1.45}code{background:#eef;padding:.15rem .3rem;border-radius:4px}</style><h1>Villani Flight Recorder</h1><p>${idx.sessions.length} sessions · ${idx.taskSegments.length} task segments · ${idx.repos.length} repos · ${idx.sessions.reduce((n, s) => n + s.failedCommandCount, 0)} failed commands</p><h2>Recent sessions</h2><ul>${idx.sessions.map((s) => `<li><b>${s.id}</b> ${s.providerLabel} <code>vfr replay --session ${s.id}</code></li>`).join("")}</ul><h2>Likely task segments</h2><ul>${idx.taskSegments.map((t) => `<li><b>${t.title}</b> ${t.id} <code>vfr replay --segment ${t.id}</code></li>`).join("")}</ul><h2>Repos</h2><ul>${idx.repos.map((r) => `<li><b>${r.name}</b> ${r.root} <code>vfr replay --repo ${r.root}</code></li>`).join("")}</ul>`;
    await fs.writeFile(out, html);
    console.log(
      `Session browser generated:\n${out}\n\nOpen this file in your browser.`,
    );
  });
program
  .command("replay")
  .option("--latest")
  .option("--open")
  .option("--provider <provider>")
  .option("--session <path>")
  .option("--id <session-id>")
  .option("--segment <segment>")
  .option("--repo <repo>")
  .option("--index-dir <path>")
  .option("--root <path>")
  .option("--out <path>")
  .option("--no-redact")
  .option("--redact")
  .action(async (o) => {
    if (!o.latest && !o.session && !o.id && !o.segment && !o.repo)
      throw new Error(
        "replay requires --latest, --id <session-id>, --session <path-or-id>, --segment <id>, or --repo <path-or-id>",
      );
    let session: ParsedSession;
    let selectedSessionId: string | undefined;
    let selectedSegmentId: string | undefined;
    if (o.provider === "villani" && o.id && o.root) {
      session = await parseVillaniRun(path.join(path.resolve(o.root), o.id));
      selectedSessionId = o.id;
    } else if (
      o.id ||
      o.segment ||
      o.repo ||
      (o.latest && !o.root) ||
      (o.session &&
        !o.session.includes(path.sep) &&
        !o.session.endsWith(".jsonl") &&
        !o.session.endsWith(".json"))
    ) {
      const idx = await readIndex(o.indexDir);
      if (!idx) {
        console.log("No session index found. Run: vfr scan --all");
        return;
      }
      let seg = o.segment
        ? idx.taskSegments.find((t) => t.id === o.segment)
        : undefined;
      if (!seg && o.repo)
        seg = idx.taskSegments
          .filter((t) => repoMatches([...t.repoRoots, ...t.repoIds], o.repo))
          .sort((a, b) =>
            String(b.lastEventAt ?? "").localeCompare(
              String(a.lastEventAt ?? ""),
            ),
          )[0];
      if (!seg && o.latest)
        seg = idx.taskSegments
          .filter(
            (t) =>
              (!o.provider || t.provider === o.provider) &&
              repoMatches([...t.repoRoots, ...t.repoIds], o.repo),
          )
          .sort((a, b) =>
            String(b.lastEventAt ?? "").localeCompare(
              String(a.lastEventAt ?? ""),
            ),
          )[0];
      const rec = o.id
        ? idx.sessions.find((s) => s.id === o.id)
        : o.session
          ? idx.sessions.find((s) => s.id === o.session)
          : idx.sessions.find((s) => s.id === seg?.sessionId);
      if (!rec && !seg)
        throw new Error(
          "Replay selector did not match any indexed session or segment. Run: vfr sessions or vfr tasks",
        );
      const srec = rec ?? idx.sessions.find((s) => s.id === seg!.sessionId)!;
      const ad = adaptersFor(String(srec.provider))[0];
      session = (await ad.parse({
        provider: srec.provider,
        sourcePath: srec.sourcePath,
        sourceKind: "file",
        confidence: srec.confidence,
        reason: "indexed replay",
      })) as unknown as ParsedSession;
      selectedSessionId = srec.id;
      selectedSegmentId = seg?.id;
      if (seg)
        session.events = session.events.slice(
          seg.startEventIndex,
          seg.endEventIndex + 1,
        );
    } else if (o.session) {
      if (!o.provider) {
        for (const pr of ["claude", "codex", "pi"] as Provider[]) {
          try {
            session = await parse(pr, o.session);
            break;
          } catch {}
        }
        if (!session!)
          console.warn(
            "Warning: Provider could not be confidently detected. Re-run with --provider claude, --provider codex, or --provider pi for best results.",
          );
      }
      session =
        session! ??
        (await parse((o.provider ?? "unknown") as Provider, o.session));
    } else {
      const roots = o.root ? [o.root] : undefined;
      const picked = chooseLatest(
        await findSessions({ provider: o.provider, roots }),
      );
      if (!picked.candidate)
        throw new Error(
          `No ${o.provider ?? "supported"} sessions found under ${o.root ?? "default session roots"}.`,
        );
      if (picked.uncertain)
        console.warn(
          "Warning: repo matching was uncertain; selected most recently modified session.",
        );
      session = await parse(picked.candidate.provider, picked.candidate.path);
    }
    if (session.provider === "villani") {
      const runsRoot =
        o.root ??
        (session.villani?.runDirectory
          ? path.dirname(session.villani.runDirectory)
          : defaultVillaniRunsRoot());
      assertOutsideVillaniRunsRoot(runsRoot, o.indexDir, o.out);
    }
    const file = await renderReplay(session, {
      redact: o.redact !== false,
      out:
        o.out ??
        (selectedSessionId && !selectedSegmentId
          ? path.join(
              o.indexDir ?? defaultIndexDir(),
              "replays",
              `${selectedSessionId}.html`,
            )
          : undefined),
      returnHref: selectedSessionId
        ? path.relative(
            path.dirname(
              o.out ??
                path.join(
                  o.indexDir ?? defaultIndexDir(),
                  "replays",
                  `${selectedSessionId}.html`,
                ),
            ),
            path.join(o.indexDir ?? defaultIndexDir(), "session-browser.html"),
          )
        : undefined,
      returnLabel: selectedSessionId ? "Back to sessions" : undefined,
    });
    if (o.latest) {
      console.log(`Provider: ${session.provider}`);
      console.log(`Root searched: ${o.root ?? "default session roots"}`);
      console.log(
        `Selected session: ${session.path ?? session.sessionPath ?? "unknown"}`,
      );
      console.log(`Replay written: ${file}`);
    } else {
      if (selectedSessionId || selectedSegmentId)
        console.log(`Replay written to ${file}\nOpen it in your browser.`);
      else console.log(file);
    }
    if (o.open) openBrowser(file);
  });
async function assertGitRepo(repo: string) {
  const stat = await fs.stat(repo).catch(() => null);
  if (!stat?.isDirectory())
    throw new Error(`git-replay --repo path is not a directory: ${repo}`);
  const gitDir = await fs.stat(path.join(repo, ".git")).catch(() => null);
  if (!gitDir)
    throw new Error(`git-replay --repo path is not a git repository: ${repo}`);
}
program
  .command("git-replay")
  .requiredOption("--from <ref>")
  .requiredOption("--to <ref>")
  .option("--repo <path>")
  .option("--open")
  .option("--out <path>")
  .option("--no-redact")
  .action(async (o) => {
    const repo = path.resolve(o.repo ?? process.cwd());
    await assertGitRepo(repo);
    const file = await renderReplay(await buildGitReplay(o.from, o.to, repo), {
      cwd: repo,
      redact: o.redact !== false,
      out: o.out,
    });
    console.log(file);
    if (o.open) openBrowser(file);
  });
program
  .command("install-hooks")
  .action(async () =>
    console.log(
      `Hook snippets written. Manual installation is required. No Claude, Codex, or Pi config files were modified.\nSnippet file: ${await installHooks()}`,
    ),
  );
program
  .command("hook")
  .argument("<provider>")
  .action(async (provider) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    for await (const c of process.stdin) data += c;
    console.log(await appendHook(provider, data));
  });
program.parseAsync().catch((e) => {
  console.error(e instanceof Error ? e.message : String(e));
  process.exit(1);
});
