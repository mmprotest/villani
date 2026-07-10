import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
async function filesUnder(directory) {
    const files = [];
    const visit = async (current) => {
        const entries = await fs.readdir(current, { withFileTypes: true });
        for (const entry of entries) {
            const child = path.join(current, entry.name);
            if (entry.isDirectory())
                await visit(child);
            else if (entry.isFile())
                files.push(child);
        }
    };
    await visit(directory);
    return files.sort();
}
export async function fingerprintVillaniRun(runDirectory) {
    const root = path.resolve(runDirectory);
    const digest = createHash("sha1");
    let sizeBytes = 0;
    let mtimeMs = 0;
    for (const file of await filesUnder(root)) {
        const stat = await fs.stat(file);
        const relative = path.relative(root, file).replaceAll(path.sep, "/");
        sizeBytes += stat.size;
        mtimeMs = Math.max(mtimeMs, stat.mtimeMs);
        digest.update(`${relative}\0${stat.size}\0${stat.mtimeMs}\n`);
    }
    return {
        sizeBytes,
        modifiedAt: new Date(mtimeMs || 0).toISOString(),
        mtimeMs,
        hash: digest.digest("hex").slice(0, 12),
    };
}
export function villaniSessionFields(parsed) {
    const run = parsed.villani;
    if (!run)
        return {};
    if (run.corruptReason) {
        return {
            title: `Corrupt run: ${parsed.sessionId ?? path.basename(run.runDirectory)}`,
            outcome: "failed",
            failureSummary: run.corruptReason,
            corruptReason: run.corruptReason,
            state: "corrupt",
            attemptCount: undefined,
            repositoryPath: undefined,
            costAccountingStatus: "unknown",
        };
    }
    const manifest = run.manifest;
    const selected = run.attempts.find((attempt) => attempt.snapshot.attempt_id === manifest?.selected_attempt_id);
    const changedFiles = run.materialization?.changed_files ?? [];
    return {
        title: run.task?.instruction,
        firstPrompt: run.task?.instruction,
        outcome: manifest?.final_state === "COMPLETED"
            ? "success"
            : manifest?.final_state === "FAILED" ||
                manifest?.final_state === "EXHAUSTED"
                ? "failed"
                : "unknown",
        failureSummary: manifest?.final_state === "EXHAUSTED"
            ? "Run exhausted without an accepted candidate"
            : run.state?.failure?.message,
        model: selected?.snapshot.model ?? undefined,
        selectedModel: selected?.snapshot.model ?? undefined,
        durationMs: manifest?.total_duration_ms ?? undefined,
        tokenCount: manifest?.total_input_tokens !== null &&
            manifest?.total_input_tokens !== undefined &&
            manifest?.total_output_tokens !== null &&
            manifest?.total_output_tokens !== undefined
            ? manifest.total_input_tokens + manifest.total_output_tokens
            : undefined,
        inputTokenCount: manifest?.total_input_tokens ?? undefined,
        outputTokenCount: manifest?.total_output_tokens ?? undefined,
        costUsd: manifest?.total_cost_usd ?? undefined,
        costAccountingStatus: manifest?.cost_accounting_status ?? "unknown",
        state: manifest?.final_state,
        attemptCount: manifest?.attempt_ids.length ?? run.attempts.length,
        repositoryPath: run.task?.repository_path,
        projectPath: run.task?.repository_path,
        projectRoot: run.task?.repository_path,
        changedFiles,
        changedFileCount: changedFiles.length,
    };
}
