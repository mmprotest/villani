import type { VillaniRunData } from "../../providers/villani.js";
import { escapeHtml, truncateText } from "../safeHtml.js";

const text = (value: unknown, missing = "Not captured") =>
  value === null || value === undefined ? missing : String(value);

const number = (value: number | null | undefined, missing = "Unknown") =>
  value === null || value === undefined ? missing : String(value);

const usd = (value: number | null | undefined, currency = "USD") =>
  value === null || value === undefined
    ? "Unknown"
    : `${currency} ${value.toFixed(2)}`;

const duration = (value: number | null | undefined) =>
  value === null || value === undefined
    ? "Not captured"
    : value < 1000
      ? `${value}ms`
      : `${value / 1000}s`;

const json = (value: unknown) =>
  escapeHtml(truncateText(JSON.stringify(value, null, 2) ?? "Not captured"));

const artifactList = (paths: Record<string, string | null>) =>
  `<ul class="artifact-list">${Object.entries(paths)
    .map(
      ([label, artifact]) =>
        `<li><b>${escapeHtml(label)}</b><span class="mono">${escapeHtml(text(artifact))}</span></li>`,
    )
    .join("")}</ul>`;

function policyDetails(run: VillaniRunData) {
  if (!run.policyDecisions.length)
    return `<section class="panel villani-panel"><h2>Policy decisions</h2><p>Not captured</p></section>`;
  return `<section class="panel villani-panel"><div class="panel-head"><div><h2>Policy decisions</h2><p>Every backend alternative and rejection reason, in decision sequence.</p></div></div>${run.policyDecisions
    .map(
      (decision) =>
        `<article class="policy-decision" data-decision-sequence="${decision.decision_sequence}"><h3>#${decision.decision_sequence} ${escapeHtml(decision.action)} · ${escapeHtml(decision.policy_version)}</h3><p>${escapeHtml(decision.reason)}</p><dl class="compact-facts"><div><dt>Required capability</dt><dd>${escapeHtml(number((decision.metadata as Record<string, unknown>).required_capability_score as number | null | undefined))}</dd></div><div><dt>Rule</dt><dd>${escapeHtml(text((decision.metadata as Record<string, unknown>).required_capability_rule))}</dd></div><div><dt>Chosen backend</dt><dd>${escapeHtml(text(decision.chosen_backend))}</dd></div><div><dt>Attempt</dt><dd>${escapeHtml(text(decision.attempt_id))}</dd></div></dl><div class="table-wrap"><table class="evidence-table policy-table"><thead><tr><th>Backend / model</th><th>Capability</th><th>Estimated cost</th><th>Accounting</th><th>Eligible</th><th>Rejection reasons</th></tr></thead><tbody>${decision.considered_backends
          .map(
            (alternative) =>
              `<tr><td><b>${escapeHtml(alternative.backend_name)}</b><br>${escapeHtml(text(alternative.model))}</td><td>${escapeHtml(number(alternative.capability_score))}</td><td>${escapeHtml(usd(alternative.estimated_cost_usd, run.aggregate?.currency))}</td><td>${escapeHtml(alternative.cost_accounting_status)}</td><td>${alternative.eligible ? "Yes" : "No"}</td><td>${escapeHtml(alternative.rejection_reasons.join("; ") || "None")}</td></tr>`,
          )
          .join(
            "",
          )}</tbody></table></div><details><summary>Budget before and projected after</summary><pre>${json({ before: decision.budget_before, after: decision.budget_after })}</pre></details></article>`,
    )
    .join("")}</section>`;
}

function candidateComparison(run: VillaniRunData) {
  const verificationByAttempt = new Map(
    run.verifications.map((verification) => [
      verification.attempt_id,
      verification,
    ]),
  );
  const rankingByAttempt = new Map(
    (run.selection?.rankings ?? []).map((ranking) => [
      ranking.attempt_id,
      ranking,
    ]),
  );
  const selected = run.manifest?.selected_attempt_id;
  return `<section class="panel villani-panel"><div class="panel-head"><div><h2>Candidate comparison</h2><p>Acceptance evidence and deterministic ranking from the canonical bundle.</p></div></div><div class="table-wrap"><table class="evidence-table candidate-table"><thead><tr><th>Attempt</th><th>Backend / model</th><th>Eligible</th><th>Verifier outcome</th><th>Confidence</th><th>Critical coverage</th><th>Tests / evidence</th><th>Risk flags</th><th>Tokens</th><th>Duration</th><th>Cost</th><th>Selection reason</th></tr></thead><tbody>${run.attempts
    .map((attempt) => {
      const snapshot = attempt.snapshot;
      const verification = verificationByAttempt.get(snapshot.attempt_id);
      const ranking = rankingByAttempt.get(snapshot.attempt_id);
      const passed =
        verification?.requirement_results.filter(
          (result) => result.outcome === "passed",
        ).length ?? 0;
      const requirements = verification?.requirement_results.length;
      const evidence = [
        ...(verification?.success_evidence ?? []),
        ...(verification?.failure_evidence ?? []),
        ...(verification?.missing_evidence ?? []),
      ]
        .map((item) => item.summary)
        .join("; ");
      const tokens =
        snapshot.input_tokens !== null && snapshot.output_tokens !== null
          ? snapshot.input_tokens + snapshot.output_tokens
          : null;
      const selectionReason =
        ranking?.reason ??
        (selected === snapshot.attempt_id ? run.selection?.reason : undefined);
      return `<tr data-attempt-id="${escapeHtml(snapshot.attempt_id)}" class="${selected === snapshot.attempt_id ? "selected-candidate" : ""}"><td><b>${escapeHtml(snapshot.attempt_id)}</b>${selected === snapshot.attempt_id ? '<br><span class="selected-label">Selected</span>' : ""}</td><td>${escapeHtml(snapshot.backend_name)}<br>${escapeHtml(text(snapshot.model))}</td><td>${verification ? (verification.acceptance_eligible ? "Yes" : "No") : "Not captured"}</td><td>${escapeHtml(text(verification?.outcome))}</td><td>${escapeHtml(number(verification?.confidence, "Not captured"))}</td><td>${requirements === undefined ? "Not captured" : `${passed}/${requirements}`}</td><td>${escapeHtml(evidence || "Not captured")}</td><td>${escapeHtml(verification ? verification.risk_flags.join("; ") || "None" : "Not captured")}</td><td>${escapeHtml(number(tokens))}</td><td>${escapeHtml(duration(snapshot.duration_ms))}</td><td>${escapeHtml(usd(snapshot.cost_usd, run.aggregate?.currency))}<br>${escapeHtml(snapshot.cost_accounting_status)}</td><td>${escapeHtml(text(selectionReason))}</td></tr>`;
    })
    .join(
      "",
    )}</tbody></table></div><details><summary>Candidate evidence matrix</summary><pre>${json(run.candidateEvidenceMatrix)}</pre></details><details><summary>Deterministic selection</summary><pre>${json(run.selection)}</pre></details></section>`;
}

function attemptDetails(run: VillaniRunData) {
  return `<section class="panel villani-panel"><div class="panel-head"><div><h2>Attempt evidence</h2><p>Captured runner output, patch, trace, telemetry, usage, cost components, and artifact paths.</p></div></div>${run.attempts
    .map((attempt) => {
      const snapshot = attempt.snapshot;
      const verification = run.verifications.find(
        (item) => item.attempt_id === snapshot.attempt_id,
      );
      const traceRecords = [
        ...(attempt.canonicalEvents ?? []),
        ...attempt.traceEvents,
      ]
        .map((item) => item as Record<string, unknown>)
        .filter((item) => item && typeof item === "object");
      const toolCalls = traceRecords.filter((item) =>
        String(item.event_type ?? item.type ?? "").includes("tool"),
      );
      const commands = traceRecords.filter((item) =>
        String(item.event_type ?? item.type ?? "").includes("command"),
      );
      return `<details class="attempt-detail" data-attempt-detail="${escapeHtml(snapshot.attempt_id)}"><summary>${escapeHtml(snapshot.attempt_id)} · ${escapeHtml(snapshot.backend_name)} / ${escapeHtml(text(snapshot.model))} · ${escapeHtml(snapshot.status)}</summary><dl class="compact-facts"><div><dt>Provider</dt><dd>${escapeHtml(text(attempt.provider))}</dd></div><div><dt>Capability score</dt><dd>${escapeHtml(number(attempt.capabilityScore))}</dd></div><div><dt>Tokens</dt><dd>input ${escapeHtml(number(snapshot.input_tokens))}, output ${escapeHtml(number(snapshot.output_tokens))} (${escapeHtml(snapshot.token_accounting_status)})</dd></div><div><dt>Duration</dt><dd>${escapeHtml(duration(snapshot.duration_ms))} (${escapeHtml(snapshot.duration_accounting_status)})</dd></div><div><dt>Cost</dt><dd>${escapeHtml(usd(snapshot.cost_usd, run.aggregate?.currency))} (${escapeHtml(snapshot.cost_accounting_status)})</dd></div><div><dt>Verifier</dt><dd>${escapeHtml(verification ? `${verification.outcome}, eligible ${verification.acceptance_eligible}` : "Not captured")}</dd></div></dl><h4>Cost components</h4><pre>${json(attempt.costComponents)}</pre><h4>Patch</h4><pre>${escapeHtml(truncateText(attempt.patch ?? "Not captured"))}</pre><h4>stdout</h4><pre>${escapeHtml(truncateText(attempt.stdout ?? "Not captured"))}</pre><h4>stderr</h4><pre>${escapeHtml(truncateText(attempt.stderr ?? "Not captured"))}</pre><h4>Tool calls</h4><pre>${json(toolCalls)}</pre><h4>Commands</h4><pre>${json(commands)}</pre><h4>Trace events</h4><pre>${json(attempt.traceEvents)}</pre><h4>Runner telemetry</h4><pre>${json(attempt.runnerTelemetry)}</pre><h4>Artifact paths</h4>${artifactList(attempt.artifactPaths)}</details>`;
    })
    .join("")}</section>`;
}

function aggregateDetails(run: VillaniRunData) {
  const aggregate = run.aggregate;
  return `<section class="panel villani-panel"><div class="panel-head"><div><h2>Run evidence summary</h2><p>Verbatim task, classification, aggregate telemetry, selection, and materialization.</p></div></div><dl class="compact-facts aggregate-facts"><div><dt>Task</dt><dd>${escapeHtml(run.task?.instruction ?? "Not captured")}</dd></div><div><dt>Success criteria</dt><dd>${escapeHtml(run.task?.success_criteria ?? "Not captured")}</dd></div><div><dt>Classification</dt><dd>${escapeHtml(run.classification ? `${run.classification.category}; ${run.classification.difficulty}; ${run.classification.risk}; confidence ${run.classification.confidence}` : "Not captured")}</dd></div><div><dt>Cost</dt><dd>${escapeHtml(usd(aggregate?.costUsd, aggregate?.currency))} (${escapeHtml(aggregate?.costAccountingStatus ?? "unknown")})</dd></div><div><dt>Tokens</dt><dd>input ${escapeHtml(number(aggregate?.inputTokens))}, output ${escapeHtml(number(aggregate?.outputTokens))}</dd></div><div><dt>Duration</dt><dd>${escapeHtml(duration(aggregate?.durationMs))}</dd></div><div><dt>Model calls</dt><dd>${escapeHtml(number(aggregate?.modelCalls, "Not captured"))}</dd></div><div><dt>Tool calls</dt><dd>${escapeHtml(number(aggregate?.toolCalls, "Not captured"))}</dd></div><div><dt>Commands</dt><dd>${escapeHtml(number(aggregate?.commands, "Not captured"))}</dd></div><div><dt>File reads</dt><dd>${escapeHtml(number(aggregate?.fileReads, "Not captured"))}</dd></div><div><dt>File writes</dt><dd>${escapeHtml(number(aggregate?.fileWrites, "Not captured"))}</dd></div><div><dt>Selected attempt</dt><dd>${escapeHtml(run.manifest?.selected_attempt_id ?? "Not selected")}</dd></div><div><dt>Materialization</dt><dd>${escapeHtml(run.materialization?.status ?? "Not captured")}</dd></div></dl><details><summary>Stage metrics</summary><pre>${json(run.manifest?.stage_metrics)}</pre></details><details><summary>Verification results</summary><pre>${json(run.verifications)}</pre></details><details><summary>Materialization</summary><pre>${json(run.materialization)}</pre></details><details><summary>Canonical artifact paths</summary>${artifactList(run.artifactPaths)}</details></section>`;
}

export function villaniRunDetails(run: VillaniRunData): string {
  if (run.corruptReason) return "";
  return `<div class="villani-details">${aggregateDetails(run)}${policyDetails(run)}${candidateComparison(run)}${attemptDetails(run)}</div>`;
}
