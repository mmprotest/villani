import {
  artifactMayRender,
  maskSensitive,
  type ArtifactDescriptor,
  type DerivedRun,
  type RunDetail,
  type RunEvent,
  type RunSpan,
} from "@villani/run-model";
import { villaniThemeCss } from "@villani/ui";

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
  const section = (title: string, body: string) =>
    `<section class="v-panel"><header class="v-panel-header"><h2 class="v-panel-header__title">${escape(title)}</h2></header><div class="v-panel__body">${body}</div></section>`;
  const exportCss = `.static-page{display:grid;gap:10px;max-width:1440px;margin:0 auto}.static-title{margin:0;font-size:22px}.static-meta{color:var(--v-text-muted)}.static-list{margin:0;padding-left:22px}.static-list li{padding:5px;border-bottom:1px solid var(--v-border-subtle)}.static-json{margin:0}.static-sidebar-note{margin-top:auto;padding:10px;color:var(--v-text-muted);font-size:9px}.static-status{margin-left:auto}`;
  const timeline = `<ol class="static-list">${bundle.events
    .map(
      (event) =>
        `<li><strong>${escape(event.name ?? event.title ?? event.type)}</strong> <span class="static-meta">${escape(event.occurred_at ?? event.timestamp)}</span></li>`,
    )
    .join("")}</ol>`;
  const graph = `<ul class="static-list">${bundle.spans
    .map(
      (span) =>
        `<li>${escape(span.kind)}: ${escape(span.name)} (${escape(span.status)}) · parent ${escape(span.parent_span_id)}</li>`,
    )
    .join("")}</ul>`;
  const content = [
    section("TIMELINE", timeline),
    section("EXECUTION GRAPH", graph),
    section(
      "CANDIDATES / EVIDENCE",
      `<pre class="v-code static-json">${json(bundle.derived.candidates)}</pre>`,
    ),
    section(
      "COST / TOKENS",
      `<pre class="v-code static-json">${json(bundle.derived.metrics)}</pre>`,
    ),
    section(
      "FILES / PATCHES",
      `<pre class="v-code static-json">${json({
        changedFiles: bundle.derived.changedFiles,
        patchEvolution: bundle.derived.patchEvolution,
        artifacts: safeArtifacts,
      })}</pre>`,
    ),
    section(
      "POLICY",
      `<pre class="v-code static-json">${json(bundle.derived.policyDecisions)}</pre>`,
    ),
    bundle.derived.failure
      ? section(
          "FAILURE",
          `<pre class="v-code static-json">${json(bundle.derived.failure)}</pre>`,
        )
      : "",
  ].join("");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Villani run ${escape(bundle.detail.id)}</title><style>${villaniThemeCss}${exportCss}</style></head><body data-villani-theme="shared"><div class="v-app-shell"><aside class="v-sidebar" aria-label="Export navigation"><div class="v-sidebar__brand">[V] VILLANI</div><nav class="v-sidebar__body"><section class="v-sidebar-section"><h2 class="v-sidebar-section__title">OBSERVE</h2><span class="v-sidebar-item" aria-current="page"><span class="v-sidebar-item__glyph">◎</span>Replay export</span></section><div class="static-sidebar-note">OFFLINE / READ ONLY<br>SHARED UI TOKENS</div></nav></aside><header class="v-top-header"><strong class="v-top-header__title">STATIC RUN EXPORT</strong><span class="v-top-header__detail">${escape(bundle.detail.id)}</span></header><div class="v-status-strip"><span class="v-status-badge" data-status="${escape(bundle.derived.status.status)}">● ${escape(bundle.derived.status.label)}</span><span class="static-status">EXPORTED ${escape(bundle.exportedAt)}</span></div><main class="v-canvas" id="main-content"><div class="static-page"><header class="v-panel"><div class="v-panel__body"><p class="static-meta">Villani static run export</p><h1 class="static-title">${escape(bundle.derived.task)}</h1><p>${escape(bundle.derived.status.reason)}</p><p class="static-meta">${escape(bundle.detail.id)} · ${escape(bundle.derived.repository)} · ${escape(bundle.derived.model)}</p></div></header>${content}</div></main></div></body></html>`;
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
