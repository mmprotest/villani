import { formatTokenCount, sumTokenUsage, } from "../providers/helpers/tokens.js";
import { fmtDuration, fmtTime } from "./viewModel.js";
const task = (s) => s.villani?.task?.instruction ??
    s.events.find((e) => e.type === "user_message")?.summary ??
    s.events.find((e) => e.type === "user_message")?.title ??
    (s.provider === "unknown" ? "Task unavailable" : "Task unavailable");
export const runnerLabel = (p) => ({
    claude: "Claude Code",
    codex: "Codex",
    pi: "Pi",
    villani: "Villani",
    git: "Git Replay",
    unknown: "Generic replay",
})[p] ??
    p ??
    "Generic replay";
export function deriveMetrics(session, replayStatus, capturedRunStatus) {
    const tokenUsage = session.tokenUsage ?? sumTokenUsage(session.events);
    const cacheTokens = tokenUsage &&
        (tokenUsage.cacheCreationTokens !== undefined ||
            tokenUsage.cacheReadTokens !== undefined ||
            tokenUsage.cachedTokens !== undefined)
        ? (tokenUsage.cacheCreationTokens ?? 0) +
            (tokenUsage.cacheReadTokens ?? 0) +
            (tokenUsage.cachedTokens ?? 0)
        : undefined;
    const tokenParts = [
        tokenUsage?.inputTokens !== undefined
            ? `input ${formatTokenCount(tokenUsage.inputTokens)}`
            : undefined,
        tokenUsage?.outputTokens !== undefined
            ? `output ${formatTokenCount(tokenUsage.outputTokens)}`
            : undefined,
        cacheTokens !== undefined
            ? `cache ${formatTokenCount(cacheTokens)}`
            : undefined,
        tokenUsage?.reasoningTokens !== undefined
            ? `reasoning ${formatTokenCount(tokenUsage.reasoningTokens)}`
            : undefined,
    ]
        .filter(Boolean)
        .join(" · ");
    const canonicalDuration = session.villani?.aggregate?.durationMs;
    const dur = canonicalDuration !== undefined && canonicalDuration !== null
        ? fmtDuration(canonicalDuration)
        : session.startedAt && session.endedAt
            ? fmtDuration(new Date(session.endedAt).getTime() -
                new Date(session.startedAt).getTime())
            : "Duration unavailable";
    return [
        {
            id: "task",
            label: "TASK",
            value: task(session),
            subvalue: session.provider,
            icon: "task",
        },
        {
            id: "model",
            label: "MODEL",
            value: session.model ?? "Provider format unknown",
            subvalue: session.model
                ? "Captured model metadata"
                : `${runnerLabel(session.provider)} session`,
            icon: "model",
            empty: !session.model,
        },
        {
            id: "runner",
            label: "RUNNER",
            value: runnerLabel(session.provider),
            empty: !session.provider,
            subvalue: `${session.events.length} events`,
            icon: "runner",
        },
        {
            id: "tokens",
            label: "TOKENS",
            value: tokenUsage?.totalTokens !== undefined
                ? formatTokenCount(tokenUsage.totalTokens)
                : "Not captured",
            subvalue: tokenUsage?.totalTokens !== undefined
                ? tokenParts || "Token telemetry captured"
                : "No token telemetry",
            icon: "tokens",
            telemetryAvailable: tokenUsage?.totalTokens !== undefined,
            empty: tokenUsage?.totalTokens === undefined,
        },
        {
            id: "cost",
            label: "COST (USD)",
            value: session.villani?.aggregate?.costUsd !== undefined &&
                session.villani.aggregate.costUsd !== null
                ? `$${session.villani.aggregate.costUsd.toFixed(2)}`
                : "Unknown",
            subvalue: session.villani
                ? `Accounting: ${session.villani.aggregate?.costAccountingStatus ?? "unknown"}`
                : "No cost telemetry",
            icon: "cost",
            empty: !session.villani ||
                session.villani.aggregate?.costUsd === undefined ||
                session.villani.aggregate.costUsd === null,
        },
        {
            id: "status",
            label: "STATUS",
            value: replayStatus.label,
            subvalue: `Captured: ${capturedRunStatus.label} — ${capturedRunStatus.reason}`,
            icon: replayStatus.tone === "error"
                ? "x"
                : replayStatus.tone === "warning"
                    ? "warn"
                    : "check",
            tone: replayStatus.tone === "error"
                ? "error"
                : replayStatus.tone === "warning"
                    ? "warning"
                    : replayStatus.tone === "success"
                        ? "success"
                        : "info",
        },
        {
            id: "duration",
            label: "DURATION",
            value: dur,
            subvalue: session.startedAt
                ? `Started ${fmtTime(session.startedAt)}`
                : "No duration captured",
            icon: "clock",
            empty: dur === "Duration unavailable",
        },
        {
            id: "runid",
            label: "RUN ID",
            value: session.sessionId ?? "Not captured",
            subvalue: session.sessionId ? "Session identifier" : "No runner metadata",
            icon: "run",
            empty: !session.sessionId,
        },
    ];
}
