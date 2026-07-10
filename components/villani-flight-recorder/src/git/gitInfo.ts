import { execFile } from "node:child_process";
import { promisify } from "node:util";
const exec = promisify(execFile);
async function git(args: string[], cwd = process.cwd()) {
  try {
    return (
      await exec("git", args, { cwd, maxBuffer: 20_000_000 })
    ).stdout.trim();
  } catch {
    return "";
  }
}
export interface GitInfo {
  root: string;
  branch: string;
  head: string;
  dirty: boolean;
  status: string;
  diffStat: string;
  diff: string;
  recentCommits: string;
}
export async function getGitInfo(cwd = process.cwd()): Promise<GitInfo> {
  const root = (await git(["rev-parse", "--show-toplevel"], cwd)) || cwd;
  const status = await git(["status", "--porcelain"], root);
  return {
    root,
    branch: await git(["branch", "--show-current"], root),
    head: await git(["rev-parse", "HEAD"], root),
    dirty: status.length > 0,
    status,
    diffStat: await git(["diff", "--stat"], root),
    diff: await git(["diff"], root),
    recentCommits: await git(
      ["log", "--oneline", "-n", "20", "--decorate"],
      root,
    ),
  };
}
