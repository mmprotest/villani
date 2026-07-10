import { deriveExecutionGraph } from "./deriveGraph.js";
import { deriveMetrics } from "./deriveMetrics.js";
import { deriveTimeline } from "./deriveTimeline.js";
import { deriveReplayStatus } from "./deriveReplayStatus.js";
import { deriveCapturedRunStatus } from "./deriveCapturedRunStatus.js";
import { changedFilesFromEvents, changedFilesFromGit, diffFromEvents, diffFromGit, } from "./deriveDetails.js";
export const fmtTime = (v) => {
    if (!v)
        return "—";
    const d = new Date(v);
    return Number.isNaN(d.getTime())
        ? v
        : d.toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
        });
};
export const fmtDuration = (ms) => ms === undefined
    ? "—"
    : ms < 1000
        ? `${ms}ms`
        : `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}s`;
export function deriveReplayViewModel(session, git) {
    const events = session.events.length
        ? session.events
        : [
            {
                id: "empty",
                provider: session.provider,
                type: "unknown",
                title: "No events captured",
                summary: "Replay data was empty",
            },
        ];
    const normalizedSession = { ...session, events };
    const warnings = [
        ...session.warnings,
        ...events.flatMap((e) => e.warnings ?? []),
    ];
    const unknownEventsCount = events.filter((e) => e.type === "unknown").length;
    const replayStatus = deriveReplayStatus({
        events,
        warnings,
        unknownEventsCount,
        outputWritten: true,
        htmlValidated: true,
    });
    const capturedRunStatus = session.villani?.manifest
        ? session.villani.manifest.final_state === "COMPLETED"
            ? {
                status: "succeeded",
                label: "Completed",
                tone: "success",
                reason: "Canonical controller state COMPLETED",
                failedCommands: 0,
                failedTests: 0,
                totalCommands: session.villani.aggregate?.commands ?? 0,
                totalTests: 0,
                fileEdits: session.villani.aggregate?.fileWrites ?? 0,
                hasFinalAnswer: true,
            }
            : session.villani.manifest.final_state === "FAILED"
                ? {
                    status: "failed",
                    label: "Failed",
                    tone: "error",
                    reason: "Canonical controller state FAILED",
                    failedCommands: 0,
                    failedTests: 0,
                    totalCommands: session.villani.aggregate?.commands ?? 0,
                    totalTests: 0,
                    fileEdits: session.villani.aggregate?.fileWrites ?? 0,
                    hasFinalAnswer: true,
                }
                : session.villani.manifest.final_state === "EXHAUSTED"
                    ? {
                        status: "partial",
                        label: "Exhausted",
                        tone: "warning",
                        reason: "Canonical controller state EXHAUSTED",
                        failedCommands: 0,
                        failedTests: 0,
                        totalCommands: session.villani.aggregate?.commands ?? 0,
                        totalTests: 0,
                        fileEdits: session.villani.aggregate?.fileWrites ?? 0,
                        hasFinalAnswer: true,
                    }
                    : deriveCapturedRunStatus(events)
        : deriveCapturedRunStatus(events);
    const timeline = deriveTimeline(events);
    const eventFiles = changedFilesFromEvents(events);
    const liveFiles = changedFilesFromGit(git);
    const changedFiles = session.villani?.materialization?.changed_files?.length
        ? session.villani.materialization.changed_files
        : session.provider === "git" && eventFiles.length > 0
            ? eventFiles
            : [...new Set([...eventFiles, ...liveFiles])];
    const eventDiff = diffFromEvents(events);
    const liveDiff = diffFromGit(git);
    return {
        brand: { title: "Villani Flight Recorder", mode: "REPLAY" },
        topBar: {
            playbackLabel: "Play",
            speedLabel: "1.0x",
            showProgress: true,
            primaryActionLabel: "Open in Console",
        },
        replayStatus,
        capturedRunStatus,
        metrics: deriveMetrics(normalizedSession, replayStatus, capturedRunStatus),
        timeline,
        graph: deriveExecutionGraph({
            session: normalizedSession,
            git,
            replayStatus,
            capturedRunStatus,
            htmlValid: true,
            outputWritten: true,
        }),
        details: timeline[0]?.detail ?? { title: "No event" },
        warnings,
        rawEvents: events,
        changedFiles,
        diff: session.villani?.attempts.find((attempt) => attempt.snapshot.attempt_id ===
            session.villani?.manifest?.selected_attempt_id)?.patch ||
            eventDiff ||
            liveDiff ||
            "Not captured",
        provider: session.provider,
        villani: session.villani,
        redactionReport: session
            .redactionReport,
    };
}
