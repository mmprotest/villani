import path from "node:path";
import { createHash } from "node:crypto";
const titleOf = (s, n) => (s || `Task segment ${n}`).replace(/\s+/g, " ").trim().slice(0, 80) ||
    `Task segment ${n}`;
const repoOf = (e) => (e.cwd ? path.resolve(e.cwd) : undefined);
export function segmentSession(sessionId, provider, events, repoIdsByRoot) {
    if (!events.length)
        return [];
    const starts = [
        { i: 0, reason: "session start" },
    ];
    for (let i = 1; i < events.length; i++) {
        const prev = events[i - 1], e = events[i];
        const gap = e.timestamp && prev.timestamp
            ? new Date(e.timestamp).getTime() - new Date(prev.timestamp).getTime()
            : 0;
        if (e.type === "user_message" &&
            events
                .slice(starts.at(-1).i, i)
                .some((x) => x.type === "assistant_message" || x.command))
            starts.push({ i, reason: "new user prompt" });
        else if (repoOf(e) && repoOf(prev) && repoOf(e) !== repoOf(prev))
            starts.push({ i, reason: "repo root change" });
        else if (gap > 20 * 60 * 1000)
            starts.push({ i, reason: "large time gap" });
        else if (prev.type === "git_commit")
            starts.push({ i, reason: "git commit boundary" });
    }
    return starts.map((s, idx) => {
        const end = (starts[idx + 1]?.i ?? events.length) - 1;
        const slice = events.slice(s.i, end + 1);
        const roots = [...new Set(slice.map(repoOf).filter(Boolean))];
        const changed = [
            ...new Set(slice.map((e) => e.path).filter(Boolean)),
        ];
        const firstPrompt = slice.find((e) => e.type === "user_message")?.summary ||
            slice.find((e) => e.type === "user_message")?.title;
        const id = "vfr_task_" +
            createHash("sha1")
                .update(`${sessionId}:${s.i}:${end}:${firstPrompt ?? ""}`)
                .digest("hex")
                .slice(0, 10);
        return {
            id,
            sessionId,
            provider,
            title: titleOf(firstPrompt ?? "", idx + 1),
            startEventIndex: s.i,
            endEventIndex: end,
            firstEventAt: slice.find((e) => e.timestamp)?.timestamp,
            lastEventAt: [...slice].reverse().find((e) => e.timestamp)?.timestamp,
            repoRoots: roots,
            repoIds: roots.map((r) => repoIdsByRoot.get(r)).filter(Boolean),
            eventCount: slice.length,
            commandCount: slice.filter((e) => e.command).length,
            failedCommandCount: slice.filter((e) => (e.exitCode ?? 0) !== 0 || e.type === "error").length,
            changedFiles: changed,
            confidence: firstPrompt && roots.length
                ? "high"
                : firstPrompt || roots.length
                    ? "medium"
                    : "low",
            boundaryReason: s.reason,
            warnings: slice.flatMap((e) => e.warnings ?? []),
        };
    });
}
