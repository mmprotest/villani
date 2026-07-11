import {
  artifactMayRender,
  maskSensitive,
  type ArtifactDescriptor,
  type DerivedRun,
  type RunDetail,
  type RunEvent,
  type RunSpan,
} from "@villani/run-model";

export interface StaticRunBundle {
  detail: RunDetail;
  events: RunEvent[];
  spans: RunSpan[];
  artifacts: ArtifactDescriptor[];
  derived: DerivedRun;
  exportedAt: string;
}
const escape = (value: unknown) =>
  String(value ?? "—").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!,
  );
const json = (value: unknown) => escape(JSON.stringify(maskSensitive(value), null, 2));

export function buildStaticExport(bundle: StaticRunBundle): string {
  const safeArtifacts = bundle.artifacts.filter((artifact) =>
    artifactMayRender(artifact.sensitivity),
  );
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Villani run ${escape(bundle.detail.id)}</title><style>body{font:15px system-ui;background:#0b0e12;color:#edf2f7;margin:0}main{max-width:1100px;margin:auto;padding:32px}section{background:#141922;border:1px solid #2b3442;border-radius:12px;padding:20px;margin:16px 0}h1,h2{margin-top:0}.state{display:inline-block;border:1px solid currentColor;border-radius:99px;padding:4px 10px}pre{white-space:pre-wrap;overflow-wrap:anywhere}li{margin:.45rem 0}.muted{color:#aab4c2}</style></head><body><main><header><p class="muted">Villani static run export · ${escape(bundle.exportedAt)}</p><h1>${escape(bundle.derived.task)}</h1><p class="state">${escape(bundle.derived.status.label)} — ${escape(bundle.derived.status.reason)}</p><p>${escape(bundle.detail.id)} · ${escape(bundle.derived.repository)} · ${escape(bundle.derived.model)}</p></header><section><h2>Timeline</h2><ol>${bundle.events.map((event) => `<li><strong>${escape(event.name ?? event.title ?? event.type)}</strong> <span class="muted">${escape(event.occurred_at ?? event.timestamp)}</span></li>`).join("")}</ol></section><section><h2>Execution graph</h2><ul>${bundle.spans.map((span) => `<li>${escape(span.kind)}: ${escape(span.name)} (${escape(span.status)}) · parent ${escape(span.parent_span_id)}</li>`).join("")}</ul></section><section><h2>Candidates and evidence</h2><pre>${json(bundle.derived.candidates)}</pre></section><section><h2>Cost and tokens</h2><pre>${json(bundle.derived.metrics)}</pre></section><section><h2>Files and patches</h2><pre>${json({ changedFiles: bundle.derived.changedFiles, patchEvolution: bundle.derived.patchEvolution, artifacts: safeArtifacts })}</pre></section><section><h2>Policy</h2><pre>${json(bundle.derived.policyDecisions)}</pre></section>${bundle.derived.failure ? `<section><h2>Failure</h2><pre>${json(bundle.derived.failure)}</pre></section>` : ""}</main></body></html>`;
}

export function downloadStaticExport(bundle: StaticRunBundle) {
  const url = URL.createObjectURL(
    new Blob([buildStaticExport(bundle)], { type: "text/html" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `villani-run-${bundle.detail.id}.html`;
  link.click();
  URL.revokeObjectURL(url);
}
