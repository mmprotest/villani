import { villaniThemeCss } from "@villani/ui";

export const themeCss = () => `${villaniThemeCss}
/* =========================================================
   Design tokens -- aliases reference the shared Villani source.
   ========================================================= */
:root {
  --bg-0: var(--v-bg-root);
  --bg-1: var(--v-bg-panel);
  --panel: var(--v-bg-panel);
  --panel-strong: var(--v-bg-elevated);
  --border: var(--v-border-subtle);
  --border-active: var(--v-border-strong);
  --border-success: var(--v-border-strong);
  --border-warning: var(--v-warning-border);
  --border-muted: var(--v-border-default);
  --text: var(--v-text-primary);
  --text-soft: var(--v-text-secondary);
  --text-muted: var(--v-text-muted);
  --text-dim: var(--v-text-muted);
  --signal: var(--v-text-primary);
  --status-success: var(--v-text-primary);
  --amber: var(--v-warning);
  --red: var(--v-danger);
}

/* =========================================================
   Base
   ========================================================= */
html, body { min-height: 100%; background: var(--v-bg-root); }
body { margin: 0; overflow-x: hidden; color: var(--text); font-family: var(--v-font); }
a { color: var(--v-text-primary); text-underline-offset: 3px; }
svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
button, input, select { font: inherit; }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible { outline: 2px solid var(--v-focus); outline-offset: 2px; }

/* =========================================================
   Investigation report layout
   ========================================================= */
.vfr-shell { min-height: 100vh; }
.vfr-sidebar-meta { margin-top:auto; padding:10px 8px; border-top:1px solid var(--v-border-subtle); color:var(--v-text-muted); font-size:9px; line-height:1.5; }
.vfr-page { display:grid; gap:10px; min-width:0; max-width:1680px; margin:0 auto; }
.app-shell { display:grid; gap:10px; min-width:0; }
.topbar, .brand, .transport, .panel-head, .panel-actions, .tabs { display:flex; align-items:center; }
.topbar { justify-content:space-between; gap:12px; }
.brand { gap:9px; min-width:0; }
.brand-mark { display:grid; width:28px; height:28px; place-items:center; border:1px solid var(--v-border-strong); background:var(--v-bg-root); }
h1 { margin:0; font-size:14px; letter-spacing:.06em; }
.replay-chip, .transport, .provider-badge, .outcome-pill, .status-chip {
  display:inline-flex; align-items:center; min-height:22px; padding:2px 6px;
  border:1px solid var(--v-border-default); border-radius:0;
  background:var(--v-bg-elevated); color:var(--v-text-secondary);
  font-size:10px; font-weight:650; letter-spacing:.045em; text-transform:uppercase;
}
.transport, .transport a { color:var(--v-text-secondary); text-decoration:none; }
.panel, .run-summary { position:relative; min-width:0; border:1px solid var(--v-border-default); border-radius:0; background:var(--v-bg-panel); box-shadow:none; }
.panel::before, .run-summary::before { content:"+"; position:absolute; top:-7px; left:-4px; z-index:2; color:var(--v-text-muted); background:var(--v-bg-root); font-size:10px; }
.panel { padding:10px; }
.panel-head { justify-content:space-between; gap:10px; min-height:31px; margin:-10px -10px 10px; padding:5px 9px; border-bottom:1px solid var(--v-border-subtle); background:var(--v-bg-elevated); }
.panel h2 { margin:0 0 3px; font-size:11px; letter-spacing:.07em; text-transform:uppercase; }
.panel p { margin:0; color:var(--text-muted); font-size:11px; line-height:1.45; }

.run-summary { display:grid; grid-template-columns:minmax(280px,.9fr) minmax(400px,1.4fr); gap:10px; padding:10px; }
.outcome-card { padding:9px; border-left:2px solid var(--v-border-strong); background:var(--v-bg-elevated); }
.run-summary.warning .outcome-card { border-left-color:var(--border-warning); }
.run-summary.error .outcome-card { border-left-color:var(--v-danger-border); }
.outcome-kicker, .metadata-row dt { color:var(--text-muted); font-size:9px; font-weight:650; letter-spacing:.1em; text-transform:uppercase; }
.outcome-card h2 { margin:6px 0; font-size:clamp(20px,3vw,31px); line-height:1.05; }
.outcome-card p { color:var(--text-soft); }
.summary-facts { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px; border:1px solid var(--v-border-subtle); background:var(--v-border-subtle); }
.summary-facts article { min-width:0; padding:7px; background:var(--v-bg-panel); }
.summary-facts b { display:block; overflow:hidden; color:var(--v-text-primary); font-size:13px; text-overflow:ellipsis; white-space:nowrap; }
.summary-facts span { display:block; margin-top:3px; color:var(--text-muted); font-size:9px; text-transform:uppercase; }
.summary-facts small { display:block; margin-top:2px; color:var(--v-text-muted); font-size:9px; line-height:1.3; overflow-wrap:anywhere; }
.metadata-row { grid-column:1/-1; display:grid; grid-template-columns:2fr 1fr 1fr 1fr; gap:0; margin:0; border-top:1px solid var(--v-border-subtle); border-left:1px solid var(--v-border-subtle); }
.metadata-row div { min-width:0; padding:7px; border-right:1px solid var(--v-border-subtle); border-bottom:1px solid var(--v-border-subtle); }
.metadata-row dd { margin:3px 0 0; overflow:hidden; color:var(--text-soft); text-overflow:ellipsis; white-space:nowrap; }
.summary-note { grid-column:1/-1; display:flex; align-items:center; gap:7px; color:var(--text-muted); font-size:10px; }
.summary-note svg { width:14px; }

.investigation-grid-main { display:grid; grid-template-columns:minmax(370px,.8fr) minmax(500px,1.2fr); gap:10px; align-items:start; }
.timeline-list { display:flex; flex-direction:column; gap:0; }
@media (min-width: 901px) { .timeline-panel { display:flex; position:sticky; top:88px; max-height:calc(100vh - 102px); flex-direction:column; } .timeline-panel .panel-head { flex-shrink:0; } .timeline-list { overflow-y:auto; padding-right:3px; } .detail-panel { max-height:calc(100vh - 102px); overflow-y:auto; } }
.timeline-row { display:grid; grid-template-columns:68px 20px minmax(0,1fr); gap:6px; align-items:center; width:100%; padding:0; border:0; border-bottom:1px solid var(--v-border-subtle); border-radius:0; background:transparent; color:inherit; text-align:left; cursor:pointer; }
.timeline-row time { overflow:hidden; color:var(--text-muted); font-size:9px; text-overflow:ellipsis; white-space:nowrap; }
.rail { display:grid; place-items:center; }
.rail i { display:grid; width:17px; height:17px; place-items:center; border:1px solid var(--v-border-default); border-radius:0; background:var(--v-bg-elevated); }
.rail svg { width:11px; }
.completed .rail i { border-color:var(--v-border-strong); color:var(--v-text-primary); }
.warning .rail i, .severity-minor-warning .rail i { border-color:var(--border-warning); color:var(--amber); }
.failed .rail i { border-color:var(--v-danger-border); color:var(--red); }
.timeline-row article { display:grid; grid-template-columns:27px minmax(0,1fr) auto; gap:8px; align-items:center; min-height:48px; padding:7px 8px; border-left:1px solid var(--v-border-subtle); background:var(--v-bg-panel); }
.timeline-row:hover article, .timeline-row.selected article { border-left-color:var(--v-border-strong); background:var(--v-bg-selected); }
.row-icon { display:grid; width:27px; height:27px; place-items:center; border:1px solid var(--v-border-subtle); color:var(--v-text-secondary); background:var(--v-bg-elevated); }
.timeline-row strong, .timeline-row p { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.timeline-row strong { font-size:11px; } .timeline-row p, .timeline-row em { color:var(--text-muted); font-size:9px; font-style:normal; }
.panel-actions { gap:4px; }
.panel-actions button, .inline-filter-reset { display:flex; align-items:center; gap:4px; min-height:27px; padding:4px 7px; border:1px solid var(--v-border-default); border-radius:0; background:var(--v-bg-panel); color:var(--text-soft); cursor:pointer; }
.panel-actions button.active { border-color:var(--v-border-strong); background:var(--v-bg-selected); color:var(--text); }

/* Detail panel */
.detail-panel { min-height:360px; }
.tabs { gap:0; flex-wrap:wrap; border-bottom:1px solid var(--v-border-default); }
.tab { min-height:30px; padding:5px 8px; border:0; border-right:1px solid var(--v-border-subtle); border-radius:0; background:var(--v-bg-panel); color:var(--text-muted); cursor:pointer; }
.tab.active { box-shadow:inset 0 -1px var(--v-text-primary); background:var(--v-bg-selected); color:var(--text); }
.tab span { margin-left:5px; color:var(--v-text-secondary); }
.detail-content { min-height:300px; padding-top:10px; }
.detail-event-layout { display:flex; flex-direction:column; gap:10px; }
.detail-hero h3 { margin:0 0 3px; font-size:16px; }
.detail-hero p { color:var(--text-soft); line-height:1.5; }
.investigation-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:1px; border:1px solid var(--v-border-subtle); background:var(--v-border-subtle); }
.investigation-card, .meta-item { min-width:0; padding:7px; border:0; border-radius:0; background:var(--v-bg-elevated); }
.investigation-label, .meta-grid b { margin-bottom:4px; color:var(--text-dim); font-size:9px; letter-spacing:.08em; text-transform:uppercase; }
.investigation-value, .meta-grid span { overflow:hidden; color:var(--text); text-overflow:ellipsis; white-space:nowrap; }
.metadata-strip .meta-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px; border:1px solid var(--v-border-subtle); background:var(--v-border-subtle); font-size:10px; }
.detail-content pre, .attempt-detail pre, .villani-panel details pre, .copy-command { margin:0; padding:8px; overflow:auto; border:1px solid var(--v-border-subtle); border-radius:0; background:var(--v-bg-root); color:var(--v-text-secondary); font-family:var(--v-font); white-space:pre-wrap; overflow-wrap:anywhere; }
.detail-section { padding:10px 0 0; border:0; border-top:1px solid var(--v-border-subtle); background:transparent; }
.detail-section.primary { padding-top:0; border-top:0; }
.detail-section h4, .evidence-block h4, .villani-panel h4 { margin:0 0 6px; color:var(--text-soft); font-size:10px; letter-spacing:.07em; text-transform:uppercase; }
.detail-section p { color:var(--text-soft); }
.raw-metadata { padding:7px; border:1px solid var(--v-border-subtle); background:var(--v-bg-elevated); }
.raw-metadata summary, .attempt-detail summary, .villani-panel details summary { color:var(--text-soft); font-size:10px; font-weight:650; cursor:pointer; }
.path-list { margin:0; padding-left:18px; color:var(--text-soft); }
.empty-state { padding:14px; border:1px dashed var(--v-border-default); border-radius:0; background:var(--v-bg-elevated); color:var(--text-soft); }
.empty-state h3 { margin:0 0 5px; color:var(--text); }
.timeline-empty { margin-bottom:8px; }
.warning-groups { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; } .warning-groups h4 { margin:0 0 5px; color:var(--text-muted); font-size:9px; text-transform:uppercase; }
.mono { font-family:var(--v-font); }
.mobile-diagnostic-list { display:none; margin:0 0 8px; padding:0; list-style:none; }
.mobile-diagnostic-list li { display:flex; justify-content:space-between; gap:8px; margin-bottom:5px; padding:7px; border:1px solid var(--v-border-subtle); background:var(--v-bg-elevated); }
.mobile-diagnostic-list b { color:var(--text-soft); font-size:10px; }
.full-graph-label, .metadata-label { margin:6px 0; color:var(--text-muted); font-size:9px; font-weight:650; }
.evidence-block section { margin-top:8px; }
.metadata-value { overflow:hidden; color:var(--text); text-overflow:ellipsis; white-space:nowrap; }

.villani-details { display:grid; gap:10px; }
.villani-panel h3 { margin:0 0 4px; font-size:12px; }
.policy-decision { margin-top:10px; padding-top:10px; border-top:1px solid var(--v-border-subtle); }
.policy-decision:first-of-type { padding-top:0; border-top:0; }
.compact-facts { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:1px; margin:8px 0; border:1px solid var(--v-border-subtle); background:var(--v-border-subtle); }
.compact-facts div { min-width:0; padding:7px; background:var(--v-bg-elevated); }
.compact-facts dt { color:var(--text-muted); font-size:9px; letter-spacing:.07em; text-transform:uppercase; }
.compact-facts dd { margin:4px 0 0; overflow-wrap:anywhere; }
.aggregate-facts { grid-template-columns:repeat(3,minmax(0,1fr)); }
.table-wrap { margin:8px 0; overflow-x:auto; }
.evidence-table { width:100%; border-collapse:collapse; font-size:10px; }
.evidence-table th, .evidence-table td { min-width:86px; padding:7px; border:1px solid var(--v-border-subtle); color:var(--v-text-secondary); text-align:left; vertical-align:top; }
.evidence-table th { background:var(--v-bg-elevated); color:var(--text-muted); font-size:9px; letter-spacing:.06em; text-transform:uppercase; }
.selected-candidate { background:var(--v-bg-selected); box-shadow:inset 2px 0 var(--v-text-primary); }
.selected-label { display:inline-block; margin-top:3px; color:var(--v-text-primary); font-size:9px; font-weight:650; text-transform:uppercase; }
.attempt-detail { margin-top:7px; padding:8px; border:1px solid var(--v-border-subtle); border-radius:0; background:var(--v-bg-elevated); }
.attempt-detail pre, .villani-panel details pre { max-height:420px; }
.artifact-list { display:grid; gap:5px; margin:7px 0 0; padding:0; list-style:none; }
.artifact-list li { display:grid; grid-template-columns:minmax(90px,.3fr) minmax(0,1fr); gap:7px; }
.artifact-list span { overflow-wrap:anywhere; }
.classification-audit { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
.adjustment-list { margin:0; padding:0; list-style:none; }
.adjustment-list li { padding:7px; border-top:1px solid var(--v-border-subtle); color:var(--v-text-primary); }
.adjustment-list li span { display:block; margin-top:3px; color:var(--v-text-muted); font-size:9px; }

@media (max-width: 900px) { .run-summary{grid-template-columns:1fr}.metadata-row{grid-template-columns:1fr 1fr}.investigation-grid-main{grid-template-columns:1fr}.detail-panel{min-height:320px}.metadata-strip .meta-grid{grid-template-columns:1fr 1fr}.compact-facts,.aggregate-facts{grid-template-columns:1fr 1fr}.summary-facts{grid-template-columns:repeat(2,minmax(0,1fr))}.classification-audit{grid-template-columns:1fr} }
@media (max-width: 700px) { .mobile-diagnostic-list{display:block} }
@media (max-width: 520px) { .vfr-sidebar-meta{display:none}.run-summary{padding:8px}.outcome-card h2{font-size:20px}.metadata-row,.investigation-grid,.metadata-strip .meta-grid,.warning-groups,.compact-facts,.aggregate-facts{grid-template-columns:1fr}.timeline-row{grid-template-columns:1fr}.timeline-row time,.rail{display:none}.timeline-row article{grid-template-columns:26px minmax(0,1fr)}.timeline-row em{display:none}.artifact-list li{grid-template-columns:1fr} }
`;
