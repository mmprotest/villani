import { escapeHtml } from "../safeHtml.js";
import { icon } from "./icons.js";
const metric = (metrics, id) => metrics.find((m) => m.id === id);
const capturedToneClass = (vm) => vm.capturedRunStatus.tone === "error"
    ? "error"
    : vm.capturedRunStatus.tone === "warning"
        ? "warning"
        : vm.capturedRunStatus.status === "not_applicable"
            ? "info"
            : "success";
const value = (m) => escapeHtml(m?.value ?? "Not captured");
const subvalue = (m) => escapeHtml(m?.subvalue ?? "");
const present = (value, unknown = "Unknown") => value === null || value === undefined ? unknown : String(value);
const villaniCards = (vm) => {
    const run = vm.villani;
    if (run.corruptReason) {
        return `<section class="run-summary error" aria-label="Villani run summary"><div class="outcome-card"><div class="outcome-kicker">Canonical run bundle</div><h2>Corrupt</h2><p>${escapeHtml(run.corruptReason)}</p></div><div class="summary-facts"><article><b>${escapeHtml(run.runDirectory)}</b><span>run directory</span></article></div></section>`;
    }
    const manifest = run.manifest;
    const classification = run.classification;
    const aggregate = run.aggregate;
    const selected = run.attempts.find((attempt) => attempt.snapshot.attempt_id === manifest?.selected_attempt_id);
    const totalTokens = aggregate?.inputTokens !== null &&
        aggregate?.inputTokens !== undefined &&
        aggregate.outputTokens !== null &&
        aggregate.outputTokens !== undefined
        ? aggregate.inputTokens + aggregate.outputTokens
        : null;
    const cost = aggregate?.costUsd === null || aggregate?.costUsd === undefined
        ? "Unknown"
        : `${aggregate.currency} ${aggregate.costUsd.toFixed(2)}`;
    const duration = aggregate?.durationMs === null || aggregate?.durationMs === undefined
        ? "Unknown"
        : aggregate.durationMs < 1000
            ? `${aggregate.durationMs}ms`
            : `${aggregate.durationMs / 1000}s`;
    const state = manifest?.final_state ?? run.state?.state ?? "Unknown";
    const tone = state === "COMPLETED"
        ? "success"
        : state === "FAILED"
            ? "error"
            : "warning";
    const policy = [
        ...new Set(run.policyDecisions.map((decision) => decision.policy_version)),
    ].join(", ") || "Not captured";
    return `<section class="run-summary ${tone}" aria-label="Villani run summary"><div class="outcome-card"><div class="outcome-kicker">Canonical controller result</div><h2>${escapeHtml(state)}</h2><p>${escapeHtml(run.task?.instruction ?? "Task not captured")}</p></div><div class="summary-facts"><article><b>${escapeHtml(classification ? `${classification.difficulty} / ${classification.risk}` : "Not captured")}</b><span>classification</span></article><article><b>${escapeHtml(policy)}</b><span>policy</span></article><article><b>${escapeHtml(manifest?.selected_attempt_id ?? "Not selected")}</b><span>selected attempt</span></article><article><b>${escapeHtml(selected?.snapshot.model ?? "Not captured")}</b><span>selected model</span></article><article><b>${escapeHtml(present(totalTokens))}</b><span>total tokens</span></article><article><b>${escapeHtml(duration)}</b><span>total duration</span></article><article><b>${escapeHtml(cost)}</b><span>known cost</span></article><article><b>${escapeHtml(aggregate?.costAccountingStatus ?? "unknown")}</b><span>cost accounting</span></article></div><dl class="metadata-row"><div><dt>Run ID</dt><dd class="mono">${escapeHtml(manifest?.run_id ?? "Not captured")}</dd></div><div><dt>Confidence</dt><dd>${escapeHtml(classification ? String(classification.confidence) : "Not captured")}</dd></div><div><dt>Attempts</dt><dd>${escapeHtml(String(run.attempts.length))}</dd></div><div><dt>Repository</dt><dd>${escapeHtml(run.task?.repository_path ?? "Not captured")}</dd></div></dl></section>`;
};
export const metricCards = (vm) => {
    if (vm.villani)
        return villaniCards(vm);
    const task = metric(vm.metrics, "task");
    const model = metric(vm.metrics, "model");
    const runner = metric(vm.metrics, "runner");
    const tokens = metric(vm.metrics, "tokens");
    const cost = metric(vm.metrics, "cost");
    const duration = metric(vm.metrics, "duration");
    const runId = metric(vm.metrics, "runid");
    const optionalFacts = [tokens, cost]
        .filter((m) => Boolean(m && !m.empty))
        .map((m) => `<article><b>${value(m)}</b><span>${escapeHtml(m.label.toLowerCase())}</span></article>`)
        .join("");
    const isGitReplay = vm.provider === "git";
    const isGenericReplay = vm.provider === "unknown";
    const metadata = [
        ["Task", isGitReplay || task?.empty ? "" : value(task), false],
        ["Run ID", runId?.empty ? "" : value(runId), true],
    ]
        .filter(([, v]) => Boolean(v))
        .map(([label, v, mono]) => `<div><dt>${label}</dt><dd class="${mono ? "mono" : ""}">${v}</dd></div>`)
        .join("");
    const captured = vm.capturedRunStatus;
    const outcomeText = captured.status === "not_applicable"
        ? captured.label
        : `${captured.label}${captured.reason ? `: ${captured.reason}` : ""}`;
    const factCards = isGitReplay
        ? `<article><b>${escapeHtml(String(vm.rawEvents.length))}</b><span>repository events</span></article><article><b>Git replay</b><span>repository changes</span></article>${duration?.empty ? "" : `<article><b>${value(duration)}</b><span>duration</span></article>`}${optionalFacts}`
        : isGenericReplay
            ? `<article><b>${escapeHtml(String(vm.rawEvents.length))}</b><span>events captured</span></article><article><b>Generic replay</b><span>provider</span></article><article><b>Provider format unknown</b><span>source format</span></article>${optionalFacts}`
            : `<article><b>${escapeHtml(String(vm.rawEvents.length))}</b><span>events captured</span></article><article><b>${value(runner)}</b><span>provider</span></article><article><b>${value(model)}</b><span>${subvalue(model) || "model"}</span></article>${duration?.empty ? "" : `<article><b>${value(duration)}</b><span>duration</span></article>`}${optionalFacts}`;
    return `<section class="run-summary ${capturedToneClass(vm)}" aria-label="Captured run summary"><div class="outcome-card"><div class="outcome-kicker">Captured run outcome</div><h2>${escapeHtml(outcomeText)}</h2><p>${escapeHtml(isGitReplay ? "Repository changes replayed" : vm.replayStatus.label)}${vm.warnings.length ? ` with ${vm.warnings.length} recorder warning${vm.warnings.length === 1 ? "" : "s"}` : ""}</p></div><div class="summary-facts">${factCards}</div>${metadata ? `<dl class="metadata-row">${metadata}</dl>` : ""}${captured.reason ? `<div class="summary-note">${icon(capturedToneClass(vm) === "error" ? "x" : capturedToneClass(vm) === "warning" ? "warn" : "check")}<span>${escapeHtml(captured.reason)}</span></div>` : ""}</section>`;
};
