import {
  canonicalRunSnapshot,
  type ArtifactDescriptor,
  type CanonicalRunSnapshot,
  type RunDetail,
  type RunEvent,
} from "@villani/run-model";

import { escapeHtml } from "./safeHtml.js";
import { themeCss } from "./theme.js";

const value = (input: unknown, missing = "Unknown") =>
  input === null || input === undefined || input === ""
    ? missing
    : String(input);

const json = (input: unknown) =>
  escapeHtml(JSON.stringify(input, null, 2) ?? "Unknown");

const money = (input: number | null) =>
  input === null ? "Unknown" : `USD ${input.toFixed(5)}`;

const tokens = (input: number | null) =>
  input === null ? "Unknown" : input.toLocaleString("en-US");

const statusGlyph = (status: string | null) => {
  const normalized = String(status ?? "unknown").toLowerCase();
  if (/fail|error/.test(normalized)) return "×";
  if (/reject|exhaust/.test(normalized)) return "⊘";
  if (/run|start|pending/.test(normalized)) return "◉";
  if (/complete|succeed|accept|selected/.test(normalized)) return "●";
  return "○";
};

const eventName = (event: RunEvent) =>
  event.name ?? event.title ?? event.type ?? "unknown_event";

const eventRole = (event: RunEvent) =>
  event.body?.command_role ??
  event.attributes?.command_role ??
  event.body?.role ??
  "—";

const eventAuthority = (event: RunEvent) =>
  event.body?.verification_authority ??
  event.attributes?.verification_authority ??
  event.body?.authority_source ??
  "—";

function metrics(snapshot: CanonicalRunSnapshot) {
  const rows = [
    ["Coding cost", money(snapshot.coding_cost_usd), "candidate execution"],
    [
      "Verifier cost",
      money(snapshot.verifier_cost_usd),
      snapshot.verifier_identity ?? "no LLM verifier",
    ],
    ["Total cost", money(snapshot.total_cost_usd), "coding + verifier"],
    [
      "Tokens",
      tokens(snapshot.total_tokens),
      `${tokens(snapshot.input_tokens)} in / ${tokens(snapshot.output_tokens)} out`,
    ],
    [
      "Duration",
      snapshot.duration_ms === null ? "Unknown" : `${snapshot.duration_ms} ms`,
      `${snapshot.attempts.length} attempts`,
    ],
    [
      "Files",
      value(snapshot.file_write_count),
      `${snapshot.selected_materialized_files.length} materialized`,
    ],
  ];
  return `<section id="overview" class="run-summary" aria-label="Connected run overview"><div class="outcome-card"><div class="outcome-kicker">Canonical connected run</div><h2>${escapeHtml(value(snapshot.status).toUpperCase())}</h2><p>${escapeHtml(value(snapshot.task, "Task not captured"))}</p></div><div class="summary-facts">${rows.map(([label, metric, detail]) => `<article><b>${escapeHtml(metric)}</b><span>${escapeHtml(label)}</span><small>${escapeHtml(detail)}</small></article>`).join("")}</div><dl class="metadata-row"><div><dt>Run ID</dt><dd class="mono">${escapeHtml(snapshot.run_id)}</dd></div><div><dt>Repository</dt><dd>${escapeHtml(value(snapshot.repository))}</dd></div><div><dt>Backend / model</dt><dd>${escapeHtml(`${value(snapshot.selected_backend)} / ${value(snapshot.selected_model)}`)}</dd></div><div><dt>Selected attempt</dt><dd>${escapeHtml(value(snapshot.selected_attempt_id, "None"))}</dd></div></dl></section>`;
}

function classification(snapshot: CanonicalRunSnapshot) {
  const adjustments = snapshot.classification_adjustments;
  return `<section class="panel villani-panel" id="classification-adjustment" data-testid="classification-adjustment"><div class="panel-head"><div><h2>Classification / raw → effective</h2><p>Raw classifier output is immutable; routing consumes the policy-derived effective value.</p></div><span class="replay-chip">${adjustments.length} adjustment${adjustments.length === 1 ? "" : "s"}</span></div><div class="classification-audit"><div><h4>Raw / immutable</h4><pre>${json(snapshot.raw_classification)}</pre></div><div><h4>Effective / routing</h4><pre>${json(snapshot.effective_classification)}</pre></div></div><h4>Adjustment records</h4>${adjustments.length ? `<ol class="adjustment-list">${adjustments.map((adjustment) => `<li><strong>${escapeHtml(value(adjustment.field))}</strong>: ${escapeHtml(value(adjustment.before))} → ${escapeHtml(value(adjustment.after))}<span>${escapeHtml(value(adjustment.rule_id))} / ${escapeHtml(value(adjustment.reason))} / ${escapeHtml(value(adjustment.policy_version))} / ${escapeHtml(value(adjustment.authority))}</span></li>`).join("")}</ol>` : `<p>No semantic adjustment applied.</p>`}</section>`;
}

function lifecycle(events: RunEvent[]) {
  return `<section class="panel timeline-panel" id="replay-timeline" data-testid="replay-timeline"><div class="panel-head"><div><h2>Replay lifecycle</h2><p>Structured controller transitions in canonical sequence.</p></div><span class="replay-chip">${events.length} events</span></div><ol class="connected-lifecycle">${events.map((event) => `<li data-event-name="${escapeHtml(eventName(event))}"><span class="rail"><i>${statusGlyph(event.status ?? null)}</i></span><div><strong>${escapeHtml(eventName(event))}</strong><p>${escapeHtml(value(event.source, "unknown source"))} / ${escapeHtml(value(event.attempt_id, "run"))}</p></div><time>${escapeHtml(value(event.occurred_at ?? event.timestamp, "time unknown"))}</time></li>`).join("")}</ol></section>`;
}

function eventStream(events: RunEvent[]) {
  return `<section class="panel villani-panel" id="event-stream" data-testid="event-stream"><div class="panel-head"><div><h2>Event stream</h2><p>Source, attempt, command role, authority, and redaction metadata.</p></div></div><div class="table-wrap"><table class="evidence-table"><thead><tr><th>Time</th><th>Event</th><th>Source</th><th>Attempt</th><th>Command role</th><th>Authority</th><th>Redaction</th></tr></thead><tbody>${events.map((event) => `<tr><td>${escapeHtml(value(event.occurred_at ?? event.timestamp))}</td><td>${escapeHtml(eventName(event))}</td><td>${escapeHtml(value(event.source))}</td><td>${escapeHtml(value(event.attempt_id, "run"))}</td><td>${escapeHtml(value(eventRole(event), "—"))}</td><td>${escapeHtml(value(eventAuthority(event), "—"))}</td><td>${escapeHtml(value(event.body?.redaction_applied ?? event.attributes?.redaction_applied, "none"))}</td></tr>`).join("")}</tbody></table></div></section>`;
}

function evidence(snapshot: CanonicalRunSnapshot) {
  const facts = [
    ["Outcome", snapshot.verification_outcome],
    ["Authority", snapshot.verification_authority],
    ["Verifier", snapshot.verifier_identity],
    ["Selected attempt", snapshot.selected_attempt_id],
    ["Materialization", snapshot.materialization_status],
    ["Selection reason", snapshot.selection_reason],
    ["Failure category", snapshot.failure_category ?? "None"],
    ["Terminal reason", snapshot.terminal_reason],
  ];
  return `<section class="panel villani-panel" id="evidence-panel" data-testid="evidence-panel"><div class="panel-head"><div><h2>Verification evidence</h2><p>Acceptance authority, eligibility, blockers, and selection truth.</p></div><span class="v-status-badge" data-status="${escapeHtml(value(snapshot.verification_outcome))}">${statusGlyph(snapshot.verification_outcome)} ${escapeHtml(value(snapshot.verification_outcome).toUpperCase())}</span></div><dl class="compact-facts">${facts.map(([label, fact]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value(fact))}</dd></div>`).join("")}</dl><details><summary>Candidate rankings</summary><pre>${json(snapshot.candidate_rankings)}</pre></details></section>`;
}

function files(
  snapshot: CanonicalRunSnapshot,
  artifacts: ArtifactDescriptor[],
) {
  return `<section class="panel villani-panel" id="file-activity" data-testid="file-activity"><div class="panel-head"><div><h2>File activity</h2><p>Writes by attempt and the files materialized from the selected patch only.</p></div></div><div class="file-activity-grid"><div><h4>Writes by attempt</h4>${Object.entries(
    snapshot.attempt_changed_files,
  )
    .map(
      ([attempt, paths]) =>
        `<article class="attempt-file-row"><strong>${escapeHtml(attempt)}</strong><span>${paths.map(escapeHtml).join(", ") || "No changed files"}</span></article>`,
    )
    .join(
      "",
    )}</div><div><h4>Selected materialized files</h4><ul class="path-list">${snapshot.selected_materialized_files.map((path) => `<li>${escapeHtml(path)}</li>`).join("") || "<li>None</li>"}</ul><h4>Safe synchronized artifacts</h4><ul class="artifact-list">${artifacts.map((artifact) => `<li><b>${escapeHtml(artifact.logical_role)}</b><span>${escapeHtml(artifact.status ?? "available")} / ${escapeHtml(artifact.sensitivity)}</span></li>`).join("") || "<li>No safe artifacts synchronized</li>"}</ul></div></div></section>`;
}

function candidates(snapshot: CanonicalRunSnapshot) {
  return `<section class="panel villani-panel" id="candidate-comparison" data-testid="candidate-comparison"><div class="panel-head"><div><h2>Candidate comparison</h2><p>Unique public attempt IDs with eligibility, authority, cost, tokens, files, and selection.</p></div></div><div class="table-wrap"><table class="evidence-table"><thead><tr><th>Candidate</th><th>Backend / model</th><th>Status</th><th>Eligibility</th><th>Authority</th><th>Outcome</th><th>Cost</th><th>Tokens</th><th>Duration</th><th>Files</th></tr></thead><tbody>${snapshot.attempts.map((attempt) => `<tr data-attempt-id="${escapeHtml(attempt.attempt_id)}" class="${attempt.selected ? "selected-candidate" : ""}"><td><b>${escapeHtml(attempt.attempt_id)}</b>${attempt.selected ? '<br><span class="selected-label">◆ Selected</span>' : ""}</td><td>${escapeHtml(value(attempt.backend))}<br>${escapeHtml(value(attempt.model))}</td><td>${escapeHtml(value(attempt.status))}</td><td>${attempt.eligible === null ? "Unknown" : attempt.eligible ? "Eligible" : "Ineligible"}</td><td>${escapeHtml(value(attempt.verification_authority, "None"))}</td><td>${escapeHtml(value(attempt.verification_outcome))}</td><td>${escapeHtml(money(attempt.cost_usd))}</td><td>${escapeHtml(tokens(attempt.total_tokens))}</td><td>${escapeHtml(attempt.duration_ms === null ? "Unknown" : `${attempt.duration_ms} ms`)}</td><td>${attempt.changed_files.length}</td></tr>`).join("")}</tbody></table></div><p class="selection-copy">${escapeHtml(value(snapshot.selection_reason))}</p></section>`;
}

function redaction(snapshot: CanonicalRunSnapshot) {
  const visible =
    snapshot.redaction_status !== null ||
    (snapshot.redacted_field_count ?? 0) > 0 ||
    (snapshot.withheld_artifact_count ?? 0) > 0;
  if (!visible) return "";
  return `<div class="v-notice connected-redaction" data-kind="redaction" data-testid="redaction-withholding-notice" role="status"><span class="v-status-badge" data-status="redacted">! REDACTED</span><span>Safe run metadata remains visible. ${snapshot.redacted_field_count ?? 0} field(s) redacted; ${snapshot.withheld_artifact_count ?? 0} unsafe artifact(s) withheld${snapshot.withheld_artifact_categories?.length ? ` (${escapeHtml(snapshot.withheld_artifact_categories.join(", "))})` : ""}.</span></div>`;
}

export function renderConnectedReplay(
  detail: RunDetail,
  events: RunEvent[] = [],
  artifacts: ArtifactDescriptor[] = [],
): string {
  const snapshot = canonicalRunSnapshot(detail);
  const sortedEvents = [...events].sort(
    (left, right) => (left.sequence ?? 0) - (right.sequence ?? 0),
  );
  const css = `${themeCss()}
.connected-page{display:grid;gap:10px}.connected-redaction{display:flex;align-items:center;gap:8px}.connected-lifecycle{position:relative;margin:0;padding:0;list-style:none}.connected-lifecycle::before{content:"";position:absolute;top:9px;bottom:9px;left:9px;border-left:1px solid var(--v-border-default)}.connected-lifecycle li{position:relative;display:grid;grid-template-columns:20px minmax(0,1fr) minmax(120px,auto);gap:7px;padding:6px 0;border-bottom:1px solid var(--v-border-subtle)}.connected-lifecycle .rail{z-index:1}.connected-lifecycle strong{font-size:10px}.connected-lifecycle p,.connected-lifecycle time{color:var(--v-text-muted);font-size:9px}.connected-lifecycle time{text-align:right}.file-activity-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.attempt-file-row{display:grid;grid-template-columns:120px minmax(0,1fr);gap:8px;padding:6px;border-bottom:1px solid var(--v-border-subtle)}.selection-copy{padding:7px;border-left:2px solid var(--v-border-strong);background:var(--v-bg-elevated)}@media(max-width:900px){.file-activity-grid{grid-template-columns:1fr}.connected-lifecycle li{grid-template-columns:20px minmax(0,1fr)}.connected-lifecycle time{grid-column:2;text-align:left}}`;
  const body = `${metrics(snapshot)}${redaction(snapshot)}${classification(snapshot)}<div class="investigation-grid-main">${lifecycle(sortedEvents)}${evidence(snapshot)}</div>${eventStream(sortedEvents)}${files(snapshot, artifacts)}${candidates(snapshot)}`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Villani Flight Recorder / ${escapeHtml(snapshot.run_id)}</title><style>${css}</style></head><body><div class="v-app-shell vfr-shell" data-villani-surface="flight-recorder-connected"><aside class="v-sidebar" aria-label="Primary navigation" data-testid="shared-sidebar"><div class="v-sidebar__brand">[V] VILLANI</div><nav class="v-sidebar__body"><section class="v-sidebar-section"><h2 class="v-sidebar-section__title">OBSERVE</h2><a class="v-sidebar-item" aria-current="page" href="#overview"><span class="v-sidebar-item__glyph">◎</span>Replay</a><a class="v-sidebar-item" href="#replay-timeline"><span class="v-sidebar-item__glyph">│</span>Timeline</a><a class="v-sidebar-item" href="#event-stream"><span class="v-sidebar-item__glyph">≡</span>Events</a><a class="v-sidebar-item" href="#evidence-panel"><span class="v-sidebar-item__glyph">◇</span>Evidence</a><a class="v-sidebar-item" href="#file-activity"><span class="v-sidebar-item__glyph">▤</span>Files</a><a class="v-sidebar-item" href="#candidate-comparison"><span class="v-sidebar-item__glyph">⇄</span>Candidates</a></section><div class="vfr-sidebar-meta">FLIGHT RECORDER<br>CONNECTED / READ ONLY</div></nav></aside><header class="v-top-header topbar" data-testid="shared-header"><div class="brand"><div class="brand-mark">V</div><h1>Villani Flight Recorder</h1><span class="replay-chip">REPLAY</span></div><span class="transport">${escapeHtml(snapshot.run_id)}</span></header><div class="v-status-strip"><span class="v-status-badge" data-status="${escapeHtml(value(snapshot.status))}">${statusGlyph(snapshot.status)} ${escapeHtml(value(snapshot.status).toUpperCase())}</span><span>API / CONNECTED</span><span>TRUTH / SYNCHRONIZED</span></div><main class="v-canvas" id="main-content"><div class="connected-page vfr-page">${body}</div></main></div></body></html>`;
}
