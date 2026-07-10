export function graphNodeTitle(nodeId) {
    return ({
        discover: "Discover",
        parse: "Parse",
        normalize: "Normalize",
        "replay-output": "Replay Output",
        "agent-events": "Agent Events",
        commands: "Commands",
        "file-changes": "File Changes",
        correlate: "Correlate",
        "git-state": "Git State",
        "diff-capture": "Diff Capture",
    }[nodeId] ?? nodeId);
}
export function graphNodeSubtitle(nodeId, context) {
    const { captured, replayStatus } = context;
    switch (nodeId) {
        case "discover":
            return context.hasSource ? "Source found" : "No source found";
        case "parse":
            if (!context.eventCount)
                return "No events";
            if (context.warningCount)
                return "Partial parse";
            return `${context.eventCount} events`;
        case "normalize":
            return context.eventCount ? "Normalized" : "No events";
        case "replay-output":
            if (replayStatus.status === "write_failed")
                return "Write failed";
            if (replayStatus.status === "render_failed")
                return "Render failed";
            if (replayStatus.status === "generated_with_warnings" ||
                replayStatus.status === "partial")
                return "With warnings";
            return "HTML written";
        case "agent-events":
            if (captured.status === "not_applicable")
                return "Not an agent run";
            if (!context.eventCount)
                return "Unknown";
            if (context.unknownCount)
                return `${context.unknownCount} partial`;
            return context.eventCount ? `${context.eventCount} events` : "Captured";
        case "commands":
            if (captured.status === "not_applicable")
                return "No command evidence";
            if (captured.failedTests)
                return `${captured.failedTests} failed ${captured.failedTests === 1 ? "test" : "tests"}`;
            if (captured.failedCommands)
                return `${captured.failedCommands} failed ${captured.failedCommands === 1 ? "cmd" : "cmds"}`;
            if (captured.totalCommands || captured.totalTests)
                return "Tools and tests";
            return "No commands";
        case "file-changes":
            if (captured.status === "not_applicable")
                return context.diffOk ? "From commits" : "No file evidence";
            if (captured.fileEdits)
                return `${captured.fileEdits} ${captured.fileEdits === 1 ? "edit" : "edits"}`;
            return "No edits";
        case "correlate":
            if (captured.status === "not_applicable" && context.hasGit)
                return "Git skipped";
            return context.hasGit ? "Repo linked" : "No repo detected";
        case "git-state":
            if (!context.hasGit)
                return "No repo detected";
            if (!context.gitStatus)
                return "Captured";
            return context.gitStatus.toLowerCase().includes("clean")
                ? "Clean"
                : "Dirty";
        case "diff-capture":
            if (context.diffOk)
                return captured.status === "not_applicable"
                    ? "From commits"
                    : "Captured";
            return context.hasGit ? "No diff captured" : "No repo detected";
        default:
            return "Captured";
    }
}
export function graphNodeBadge(nodeId, context) {
    if (nodeId === "commands") {
        if (context.captured.status === "not_applicable")
            return "Skipped";
        if (context.captured.failedCommands || context.captured.failedTests)
            return "FAILED";
    }
    if (nodeId === "agent-events" && context.captured.status === "not_applicable")
        return "Skipped";
    if (["correlate", "git-state", "diff-capture"].includes(nodeId) &&
        !context.hasGit)
        return "Skipped";
    if (nodeId === "replay-output") {
        return ["render_failed", "write_failed"].includes(context.replayStatus.status)
            ? "FAILED"
            : context.replayStatus.status === "generated_with_warnings" ||
                context.replayStatus.status === "partial"
                ? "WARNING"
                : "COMPLETE";
    }
    return undefined;
}
