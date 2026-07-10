import os from "node:os";
import path from "node:path";
export const home = () => os.homedir();
export const expandHome = (p: string) =>
  p.startsWith("~") ? path.join(home(), p.slice(1)) : p;
export const replayRoot = (cwd = process.cwd()) =>
  path.join(cwd, ".villani-flight-recorder", "replays");
export const safeSegment = (s: string) =>
  s.replace(/[^a-zA-Z0-9._-]+/g, "-").slice(0, 80) || "replay";
