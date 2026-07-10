import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
const REQUIRED_RUN_FILES = ["manifest.json", "state.json", "events.jsonl"];
export function defaultVillaniRunsRoot() {
    const home = process.env.VILLANI_HOME || path.join(os.homedir(), ".villani");
    return path.join(home, "runs");
}
async function isFile(file) {
    return fs
        .stat(file)
        .then((stat) => stat.isFile())
        .catch(() => false);
}
export async function findVillaniRuns(roots = [defaultVillaniRunsRoot()]) {
    const runs = [];
    for (const configuredRoot of roots) {
        const root = path.resolve(configuredRoot);
        const entries = await fs
            .readdir(root, { withFileTypes: true })
            .catch(() => []);
        for (const entry of entries) {
            if (!entry.isDirectory())
                continue;
            const runPath = path.join(root, entry.name);
            const required = await Promise.all(REQUIRED_RUN_FILES.map((name) => isFile(path.join(runPath, name))));
            if (!required.every(Boolean))
                continue;
            try {
                const stat = await fs.stat(runPath);
                runs.push({
                    runId: entry.name,
                    runPath,
                    mtimeMs: stat.mtimeMs,
                });
            }
            catch (error) {
                runs.push({
                    runId: entry.name,
                    runPath,
                    mtimeMs: 0,
                    error: error instanceof Error ? error.message : String(error),
                });
            }
        }
    }
    return runs.sort((left, right) => right.mtimeMs - left.mtimeMs || left.runId.localeCompare(right.runId));
}
