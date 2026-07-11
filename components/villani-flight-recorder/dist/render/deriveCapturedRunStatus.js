import { deriveRunStatus } from "@villani/run-model";
export function deriveCapturedRunStatus(events, controllerState) {
    const hasAgent = events.some((event) => !["git_commit", "git_status", "diff"].includes(event.type));
    if (!hasAgent && !controllerState) {
        return {
            ...deriveRunStatus([], undefined),
            status: "not_applicable",
            label: "N/A",
            tone: "muted",
            reason: "Git-only replay",
        };
    }
    return deriveRunStatus(events.map((event) => ({
        id: event.id,
        type: event.type,
        title: event.title,
        command: event.command,
        exit_code: event.exitCode,
        path: event.path,
        sequence: event.sequence,
        timestamp: event.timestamp,
        attempt_id: event.attemptId,
        raw: event.raw,
    })), controllerState);
}
