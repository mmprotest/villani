import { event } from "../normalize/events.js";
import { runChildProcess } from "../utils/childProcess.js";
async function git(args, cwd = process.cwd()) {
    return (await runChildProcess("git", args, {
        cwd,
        timeoutMs: 10_000,
        maxOutputBytes: 30_000_000,
    })).stdout.trim();
}
function flags(files, patch) {
    const names = files.split(/\r?\n/).map((l) => l.split(/\s+/).pop() ?? "");
    return [
        names.some((f) => /package.json|lock|Cargo.toml|requirements|pyproject|go.mod|go.sum/.test(f)) && "dependency file changed",
        names.some((f) => /lock/.test(f)) && "lockfile changed",
        names.some((f) => /(test|spec|__tests__)/i.test(f)) && "test file changed",
        patch.length > 20_000 && "large diff",
        names.length > 20 && "many files changed",
        /Binary files/.test(patch) && "binary files changed",
        /^D\s/m.test(files) && "deletions present",
    ]
        .filter(Boolean)
        .join(", ");
}
export async function buildGitReplay(from, to, cwd = process.cwd()) {
    const root = await git(["rev-parse", "--show-toplevel"], cwd);
    const range = `${from}..${to}`;
    const shas = (await git(["rev-list", "--reverse", range], root))
        .split(/\n/)
        .filter(Boolean);
    const events = [];
    let n = 0;
    for (const sha of shas) {
        const meta = await git(["show", "--no-patch", "--format=%H%n%an <%ae>%n%aI%n%s", sha], root);
        const files = await git(["show", "--name-status", "--format=", sha], root);
        const patch = await git(["show", "--format=", "--patch", sha], root);
        const [full, author, timestamp, subject] = meta.split("\n");
        events.push(event(`git-${++n}`, "git", "git_commit", `Commit ${subject}`, { sha: full, author, timestamp, files }, {
            timestamp,
            summary: `${full.slice(0, 8)} ${subject}\n${author}\n${files}\nFlags: ${flags(files, patch)}`,
            diff: patch,
        }));
    }
    events.push(event(`git-${++n}`, "git", "diff", "Final diff/stat", {}, {
        diff: await git(["diff", from, to], root),
        summary: "Git-only replay: reasoning, tool calls, failed attempts, approvals, and shell commands are unknown unless committed.",
    }));
    return {
        provider: "git",
        path: range,
        sessionPath: range,
        cwd: root,
        sessionId: range,
        events,
        warnings: [
            "Git-only replay cannot know agent reasoning, uncommitted failed attempts, tool calls, approvals, or commands unless captured in git history.",
        ],
    };
}
