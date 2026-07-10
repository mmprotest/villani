function rawObj(v) {
    return v && typeof v === "object" && !Array.isArray(v)
        ? v
        : {};
}
const asStrings = (v) => Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
const statusFileName = (line) => line.trim().split(/\s+/).pop() ?? "";
export function changedFilesFromEvents(events) {
    const files = new Set();
    for (const e of events) {
        if (["file_write", "file_edit", "file_delete"].includes(e.type) && e.path)
            files.add(e.path);
        const raw = rawObj(e.raw);
        for (const name of asStrings(raw.files))
            files.add(name);
        const text = typeof raw.files === "string"
            ? raw.files
            : typeof raw.file === "string"
                ? raw.file
                : "";
        for (const line of text.split(/\r?\n/)) {
            const name = statusFileName(line);
            if (name)
                files.add(name);
        }
        const diff = e.diff ?? String(raw.diff ?? raw.patch ?? "");
        for (const line of diff.split(/\r?\n/)) {
            const m = line.match(/^diff --git a\/(.*?) b\/(.*?)$/);
            if (m)
                files.add(m[2] ?? m[1]);
        }
    }
    return [...files].filter(Boolean);
}
export const changedFilesFromGit = (git) => (git?.status ?? "")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
const normalizeDiffBlock = (s) => s.trim().replace(/\r\n/g, "\n");
function dedupeDiffBlocks(blocks) {
    const seen = new Set();
    const out = [];
    for (const block of blocks.map(normalizeDiffBlock).filter(Boolean)) {
        if (seen.has(block))
            continue;
        seen.add(block);
        out.push(block);
    }
    return out;
}
export function diffFromEvents(events) {
    const diffEvents = events
        .filter((e) => e.type === "diff")
        .map((e) => e.diff ?? String(rawObj(e.raw).diff ?? rawObj(e.raw).patch ?? ""));
    const primary = dedupeDiffBlocks(diffEvents);
    if (primary.length)
        return primary.join("\n\n");
    return dedupeDiffBlocks(events
        .map((e) => e.diff ?? String(rawObj(e.raw).diff ?? rawObj(e.raw).patch ?? ""))
        .filter(Boolean)).join("\n\n");
}
export const diffFromGit = (git) => git?.diff || git?.diffStat || "";
