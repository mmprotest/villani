import { execFile } from "node:child_process";
import { promisify } from "node:util";
const exec = promisify(execFile);
async function git(args, cwd = process.cwd()) {
    try {
        return (await exec("git", args, { cwd, maxBuffer: 20_000_000 })).stdout.trim();
    }
    catch {
        return "";
    }
}
export async function getGitInfo(cwd = process.cwd()) {
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
        recentCommits: await git(["log", "--oneline", "-n", "20", "--decorate"], root),
    };
}
