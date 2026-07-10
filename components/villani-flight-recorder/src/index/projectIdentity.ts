import path from "node:path";
import { FlightEvent } from "../providers/types.js";

const weak = new Set([
  "repo",
  "sandbox",
  "workspace",
  "tmp",
  "project",
  "projects",
  "runs",
  "tasks",
]);
const splitPath = (p: string) =>
  p.replace(/\\/g, "/").split("/").filter(Boolean);
function decodeClaudeProjectPath(sourcePath?: string) {
  if (!sourcePath) return undefined;
  const marker = ".claude/projects/";
  const normalized = sourcePath.replace(/\\/g, "/");
  const i = normalized.toLowerCase().indexOf(marker);
  if (i < 0) return undefined;
  const encoded = normalized.slice(i + marker.length).split("/")[0];
  if (!encoded) return undefined;
  const raw = encoded.replace(/^([A-Za-z])--/, "$1:/");
  const taskMatch = raw.match(
    /(?:^|-)tasks-([^-]+(?:-[^-]+)*?)(?:-sandbox|-repo|-workspace|-tmp|$)/i,
  );
  if (taskMatch) return `tasks/${taskMatch[1]}`;
  return raw.replace(/-/g, "/");
}
export function meaningfulProjectName(projectPath?: string) {
  if (!projectPath) return undefined;
  const segs = splitPath(projectPath);
  const task = segs.findIndex((s) => s.toLowerCase() === "tasks");
  if (task >= 0 && segs[task + 1]) return segs[task + 1];
  for (let i = segs.length - 1; i >= 0; i--) {
    const s = segs[i];
    if (s && !weak.has(s.toLowerCase())) return s;
  }
  return segs.at(-1);
}
export function deriveProjectIdentity(input: {
  repoRoots?: string[];
  repoIds?: string[];
  cwd?: string;
  events?: FlightEvent[];
  sourcePath?: string;
}) {
  const eventCwd = input.events?.find((e) => e.cwd)?.cwd;
  const decoded = decodeClaudeProjectPath(input.sourcePath);
  const sourceParent = input.sourcePath
    ? path.dirname(input.sourcePath)
    : undefined;
  const candidates = [
    input.repoRoots?.[0],
    input.cwd,
    eventCwd,
    decoded,
    sourceParent,
  ].filter(Boolean) as string[];
  const projectPath = candidates[0];
  const projectName = meaningfulProjectName(
    candidates.find((c) => meaningfulProjectName(c)),
  );
  const projectId =
    input.repoIds?.[0] ??
    (projectPath ? projectPath.toLowerCase() : projectName?.toLowerCase());
  return {
    projectId,
    projectName,
    projectPath,
    projectRoot: input.repoRoots?.[0],
    projectDisplayName: projectName,
  };
}
