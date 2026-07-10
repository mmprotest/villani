import { ReplayDashboardViewModel, MetricCardViewModel } from "../viewModel.js";
import { escapeHtml } from "../safeHtml.js";
import { icon } from "./icons.js";

const metric = (metrics: MetricCardViewModel[], id: string) =>
  metrics.find((m) => m.id === id);

const capturedToneClass = (vm: ReplayDashboardViewModel) =>
  vm.capturedRunStatus.tone === "error"
    ? "error"
    : vm.capturedRunStatus.tone === "warning"
      ? "warning"
      : vm.capturedRunStatus.status === "not_applicable"
        ? "info"
        : "success";

const value = (m?: MetricCardViewModel) =>
  escapeHtml(m?.value ?? "Not captured");
const subvalue = (m?: MetricCardViewModel) => escapeHtml(m?.subvalue ?? "");

export const metricCards = (vm: ReplayDashboardViewModel) => {
  const task = metric(vm.metrics, "task");
  const model = metric(vm.metrics, "model");
  const runner = metric(vm.metrics, "runner");
  const tokens = metric(vm.metrics, "tokens");
  const cost = metric(vm.metrics, "cost");
  const duration = metric(vm.metrics, "duration");
  const runId = metric(vm.metrics, "runid");
  const optionalFacts = [tokens, cost]
    .filter((m): m is MetricCardViewModel => Boolean(m && !m.empty))
    .map(
      (m) =>
        `<article><b>${value(m)}</b><span>${escapeHtml(m.label.toLowerCase())}</span></article>`,
    )
    .join("");
  const isGitReplay = vm.provider === "git";
  const isGenericReplay = vm.provider === "unknown";
  const metadata = [
    ["Task", isGitReplay || task?.empty ? "" : value(task), false],
    ["Run ID", runId?.empty ? "" : value(runId), true],
  ]
    .filter(([, v]) => Boolean(v))
    .map(
      ([label, v, mono]) =>
        `<div><dt>${label}</dt><dd class="${mono ? "mono" : ""}">${v}</dd></div>`,
    )
    .join("");
  const captured = vm.capturedRunStatus;
  const outcomeText =
    captured.status === "not_applicable"
      ? captured.label
      : `${captured.label}${captured.reason ? `: ${captured.reason}` : ""}`;
  const factCards = isGitReplay
    ? `<article><b>${escapeHtml(String(vm.rawEvents.length))}</b><span>repository events</span></article><article><b>Git replay</b><span>repository changes</span></article>${duration?.empty ? "" : `<article><b>${value(duration)}</b><span>duration</span></article>`}${optionalFacts}`
    : isGenericReplay
      ? `<article><b>${escapeHtml(String(vm.rawEvents.length))}</b><span>events captured</span></article><article><b>Generic replay</b><span>provider</span></article><article><b>Provider format unknown</b><span>source format</span></article>${optionalFacts}`
      : `<article><b>${escapeHtml(String(vm.rawEvents.length))}</b><span>events captured</span></article><article><b>${value(runner)}</b><span>provider</span></article><article><b>${value(model)}</b><span>${subvalue(model) || "model"}</span></article>${duration?.empty ? "" : `<article><b>${value(duration)}</b><span>duration</span></article>`}${optionalFacts}`;
  return `<section class="run-summary ${capturedToneClass(vm)}" aria-label="Captured run summary"><div class="outcome-card"><div class="outcome-kicker">Captured run outcome</div><h2>${escapeHtml(outcomeText)}</h2><p>${escapeHtml(isGitReplay ? "Repository changes replayed" : vm.replayStatus.label)}${vm.warnings.length ? ` with ${vm.warnings.length} recorder warning${vm.warnings.length === 1 ? "" : "s"}` : ""}</p></div><div class="summary-facts">${factCards}</div>${metadata ? `<dl class="metadata-row">${metadata}</dl>` : ""}${captured.reason ? `<div class="summary-note">${icon(capturedToneClass(vm) === "error" ? "x" : capturedToneClass(vm) === "warning" ? "warn" : "check")}<span>${escapeHtml(captured.reason)}</span></div>` : ""}</section>`;
};
