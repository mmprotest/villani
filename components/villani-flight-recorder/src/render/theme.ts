export const themeCss = () => `
/* =========================================================
   Design tokens
   ========================================================= */
:root {
  --bg-0: #101820;
  --bg-1: #f3f0e8;
  --panel: rgba(255, 252, 246, 0.96);
  --panel-strong: rgba(248, 244, 236, 0.98);
  --border: rgba(77, 91, 105, 0.18);
  --border-active: rgba(105, 157, 214, 0.75);
  --border-success: rgba(126, 226, 139, 0.62);
  --border-warning: rgba(255, 204, 77, 0.72);
  --border-muted: rgba(155, 177, 202, 0.26);
  --text: #17202a;
  --text-soft: #334155;
  --text-muted: #64748b;
  --text-dim: #7a8796;
  --blue: #58748f;
  --green: #91d39a;
  --amber: #e8c66b;
  --red: #f08080;
  --glow-blue: 0 0 28px rgba(59, 153, 255, 0.34);
  --glow-green: 0 0 20px rgba(126, 226, 139, 0.20);
  --glow-amber: 0 0 20px rgba(255, 210, 77, 0.20);
}

/* =========================================================
   Base
   ========================================================= */
* {
  box-sizing: border-box;
}

html,
body {
  margin: 0;
  min-height: 100vh;
}

body {
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
  background: linear-gradient(180deg, #eef1f3, #f7f3ea 56%, #efe9dc);
}

body::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  background: radial-gradient(circle at 20% 0%, rgba(88, 116, 143, 0.08), transparent 34%);
}

svg {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

/* =========================================================
   Investigation report layout
   ========================================================= */
.app-shell {
  max-width: 1440px;
  margin: 0 auto;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0;} .topbar, .brand, .transport, .panel-head, .panel-actions, .tabs { display:flex; align-items:center; }
.topbar { position: sticky; top: 0; z-index: 10; justify-content: space-between; gap: 16px; padding: 10px 0; backdrop-filter: blur(14px); }
.brand { gap: 12px; min-width: 0; }
.brand-mark { width: 34px; height: 34px; border: 1px solid var(--border-active); border-radius: 10px; display: grid; place-items: center; }
h1 { margin: 0; font-size: clamp(20px, 3vw, 26px); }
.replay-chip { font-size: 11px; font-weight: 800; color: #334155; border: 1px solid rgba(88,116,143,.22); border-radius: 999px; padding: 5px 9px; background: rgba(255,255,255,.38); }
.transport, .transport a { color: #334155; text-decoration:none; }
.transport { color: #334155; border: 1px solid rgba(88,116,143,.22); border-radius: 999px; padding: 6px 10px; font-size: 12px; background: rgba(255,255,255,.46); }
.panel, .run-summary { background: linear-gradient(180deg, rgba(255,252,246,.96), rgba(248,244,236,.98)); border: 1px solid rgba(154,178,205,.14); border-radius: 18px; box-shadow: 0 6px 18px rgba(31,41,55,.06); }
.panel { padding: 18px; }
.panel-head { justify-content: space-between; gap: 14px; margin-bottom: 14px; }
.panel h2 { margin: 0 0 4px; font-size: 19px; }
.panel p { margin: 0; color: var(--text-muted); font-size: 13px; line-height: 1.45; }
.run-summary { padding: 22px; display: grid; grid-template-columns: minmax(280px, 1.1fr) minmax(320px, 1fr); gap: 18px 24px; }
.outcome-card { border-left: 5px solid var(--blue); padding-left: 18px; }
.run-summary.success .outcome-card { border-left-color: var(--green); }
.run-summary.warning .outcome-card { border-left-color: var(--amber); }
.run-summary.error .outcome-card { border-left-color: var(--red); }
.outcome-kicker, .metadata-row dt { color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; font-size: 11px; font-weight: 800; }
.outcome-card h2 { margin: 8px 0; font-size: clamp(26px, 4vw, 42px); line-height: 1.05; letter-spacing: -0.035em; }
.outcome-card p { margin: 0; color: var(--text-soft); font-size: 14px; }
.summary-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.summary-facts article { border: 0; border-left: 1px solid rgba(154,178,205,.22); padding: 8px 0 8px 12px; background: transparent; min-width:0; }
.summary-facts b { display:block; font-size: 18px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.summary-facts span { color: var(--text-muted); font-size: 12px; }
.metadata-row { grid-column: 1 / -1; display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: 10px; margin: 0; }
.metadata-row div { min-width: 0; }
.metadata-row dd { margin: 4px 0 0; color: var(--text-soft); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.summary-note { grid-column: 1 / -1; display:flex; gap:10px; align-items:center; color: var(--text-muted); font-size: 13px; }
.summary-note svg { width: 16px; }
.investigation-grid-main { display:grid; grid-template-columns: minmax(360px,.8fr) minmax(520px,1.2fr); gap:18px; align-items:start; }
.timeline-list { display:flex; flex-direction:column; gap:8px; }
@media (min-width: 901px) { .timeline-panel { max-height: calc(100vh - 160px); display:flex; flex-direction:column; position:sticky; top:84px; } .timeline-panel .panel-head { flex-shrink:0; } .timeline-list { overflow-y:auto; padding-right:6px; } .detail-panel { max-height: calc(100vh - 160px); overflow-y:auto; } }
.timeline-row { display:grid; grid-template-columns: 72px 26px minmax(0,1fr); gap: 8px; align-items:center; width:100%; border:0; background:transparent; color:inherit; text-align:left; padding:0; cursor:pointer; }
.timeline-row time { color: var(--text-muted); font-size: 11px; white-space: nowrap; overflow:hidden; text-overflow:ellipsis; }
.rail { display:grid; place-items:center; }
.rail i { width:24px; height:24px; border-radius:8px; border:1px solid var(--border-muted); display:grid; place-items:center; background:#f7f3ea; }
.rail svg { width:14px; }
.completed .rail i { color:#06130d; background:var(--green); }
.warning .rail i, .severity-minor-warning .rail i { color:var(--amber); border-color:var(--border-warning); }
.failed .rail i { color:var(--red); border-color:rgba(255,107,107,.7); }
.timeline-row article { min-height:56px; display:grid; grid-template-columns:32px minmax(0,1fr) auto; gap:12px; align-items:center; padding:13px 14px; border:1px solid rgba(154,178,205,.10); border-radius:12px; background:rgba(255,255,255,.36); }
.timeline-row:hover article, .timeline-row.selected article { border-color: rgba(118,169,224,.62); background:rgba(88,116,143,.08); }
.row-icon { width:32px; height:32px; border-radius:9px; display:grid; place-items:center; color:#58748f; background:rgba(88,116,143,.08); }
.timeline-row strong, .timeline-row p { display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.timeline-row strong { font-size:13px; } .timeline-row p, .timeline-row em { color:var(--text-muted); font-size:12px; font-style:normal; }
.panel-actions { gap:8px; } .panel-actions button { border:1px solid rgba(154,178,205,.18); border-radius:8px; background:rgba(255,255,255,.52); color:var(--text-soft); padding:7px 10px; font:inherit; font-size:12px; cursor:pointer; display:flex; gap:6px; align-items:center; } .panel-actions button.active { color:var(--text); border-color:rgba(232,198,107,.78); background:rgba(232,198,107,.13); }
/* Detail panel */
.detail-panel { min-height: 360px; }
.tabs { gap: 6px; flex-wrap: wrap; border-bottom: 1px solid rgba(124,170,220,.12); padding-bottom: 10px; }
.tab { background: transparent; color: var(--text-muted); border: 0; border-bottom: 2px solid transparent; padding: 7px 9px; font: inherit; font-size: 12px; cursor: pointer; }
.tab.active { color: var(--text); border-bottom-color: var(--blue); background: rgba(59,153,255,.07); }
.tab span { margin-left:6px; color:#58748f; }
.detail-content { padding-top: 14px; min-height: 300px; }
.detail-event-layout { display:flex; flex-direction:column; gap:14px; }
.detail-hero { display:block; }
.detail-hero h3 { margin:0 0 4px; font-size:22px; line-height:1.15; }
.detail-hero p { color:var(--text-soft); line-height:1.55; }

.status-chip { display:inline-flex; margin:3px 0 7px; padding:4px 8px; border-radius:999px; border:1px solid var(--border); font-size:10px; text-transform:uppercase; color:var(--text-soft); }
.investigation-grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:10px; }
.investigation-card, .meta-item { border:1px solid rgba(154,178,205,.14); background:rgba(255,255,255,.52); border-radius:12px; padding:10px; min-width:0; }
.investigation-label, .meta-grid b { font-size:10px; text-transform:uppercase; letter-spacing:.1em; color:var(--text-dim); margin-bottom:5px; }
.investigation-value, .meta-grid span { color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.metadata-strip .meta-grid { display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:10px; font-size:12px; }
.detail-content pre { white-space: pre-wrap; overflow:auto; margin:0; background:#f8fafc; border:1px solid rgba(154,178,205,.16); border-radius:12px; padding:12px; color:#1f2937; max-height: none; }

.detail-section { border:0; border-top:1px solid rgba(154,178,205,.16); border-radius:0; padding:13px 0 0; background:transparent; }
.detail-section.primary { border-top:0; padding-top:0; background:transparent; }
.detail-section h4 { margin:0 0 8px; color:var(--text-soft); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
.detail-section p { color:var(--text-soft); }
.raw-metadata { border:1px solid rgba(154,178,205,.12); border-radius:12px; padding:10px; background:rgba(255,255,255,.018); }
.raw-metadata summary { cursor:pointer; color:var(--text-soft); font-weight:700; font-size:12px; }
.path-list { margin:0; padding-left:18px; color:var(--text-soft); }
.empty-state { border:1px dashed rgba(154,178,205,.24); border-radius:14px; padding:18px; background:rgba(255,255,255,.02); color:var(--text-soft); }
.empty-state h3 { margin:0 0 6px; color:var(--text); }
.inline-filter-reset { margin-top:10px; border:1px solid rgba(118,169,224,.45); border-radius:999px; background:rgba(88,116,143,.08); color:var(--text); padding:8px 12px; cursor:pointer; }
.timeline-empty { margin-bottom:12px; }
.warning-groups { display:grid; grid-template-columns: repeat(2,1fr); gap:14px; } .warning-groups h4 { margin:0 0 6px; font-size:11px; color:var(--text-muted); text-transform:uppercase; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.mobile-diagnostic-list { display:none; list-style:none; padding:0; margin:0 0 12px; }
.mobile-diagnostic-list li { display:flex; justify-content:space-between; gap:10px; border:1px solid rgba(124,170,220,.12); border-radius:10px; padding:10px; margin-bottom:8px; background:rgba(255,255,255,.34); }
.mobile-diagnostic-list b { color:var(--text-soft); font-size:12px; }
.full-graph-label { color:var(--text-muted); font-size:12px; font-weight:700; margin:8px 0; }
.evidence-block section { margin-top:10px; }
.evidence-block h4 { margin:0 0 6px; color:var(--text-soft); font-size:12px; }
.metadata-label { color:var(--text-dim); font-size:11px; font-weight:750; margin-bottom:5px; }
.metadata-value { color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.villani-details { display:grid; gap:18px; }
.villani-panel h3 { margin:0 0 5px; font-size:16px; }
.villani-panel h4 { margin:16px 0 7px; color:var(--text-soft); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
.policy-decision { border-top:1px solid rgba(154,178,205,.16); padding-top:15px; margin-top:15px; }
.policy-decision:first-of-type { border-top:0; padding-top:0; }
.compact-facts { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:9px; margin:12px 0; }
.compact-facts div { border:1px solid rgba(154,178,205,.14); border-radius:11px; padding:9px; background:rgba(255,255,255,.45); min-width:0; }
.compact-facts dt { color:var(--text-muted); font-size:10px; text-transform:uppercase; letter-spacing:.08em; }
.compact-facts dd { margin:5px 0 0; overflow-wrap:anywhere; }
.aggregate-facts { grid-template-columns:repeat(3,minmax(0,1fr)); }
.table-wrap { overflow-x:auto; margin:12px 0; }
.evidence-table { width:100%; border-collapse:collapse; font-size:12px; }
.evidence-table th,.evidence-table td { border:1px solid rgba(154,178,205,.18); padding:9px; text-align:left; vertical-align:top; min-width:92px; }
.evidence-table th { background:rgba(88,116,143,.08); color:var(--text-soft); }
.selected-candidate { background:rgba(145,211,154,.12); }
.selected-label { display:inline-block; margin-top:4px; color:#315f37; font-size:10px; font-weight:800; text-transform:uppercase; }
.attempt-detail { border:1px solid rgba(154,178,205,.16); border-radius:13px; padding:12px; margin-top:10px; }
.attempt-detail summary,.villani-panel details summary { cursor:pointer; color:var(--text-soft); font-weight:750; }
.attempt-detail pre,.villani-panel details pre { white-space:pre-wrap; overflow:auto; max-height:420px; background:#f8fafc; border:1px solid rgba(154,178,205,.16); border-radius:11px; padding:11px; }
.artifact-list { display:grid; gap:6px; list-style:none; margin:10px 0 0; padding:0; }
.artifact-list li { display:grid; grid-template-columns:minmax(90px,.3fr) minmax(0,1fr); gap:9px; }
.artifact-list span { overflow-wrap:anywhere; }
@media (max-width: 700px) { .mobile-diagnostic-list { display:block; } }

@media (max-width: 900px) { .app-shell{padding:14px; gap:14px;} .topbar{position:static; align-items:flex-start; flex-direction:column;} .run-summary{grid-template-columns:1fr; padding:18px;} .metadata-row{grid-template-columns:1fr 1fr;} .investigation-grid-main{grid-template-columns:1fr;} .detail-panel{min-height:320px;} .metadata-strip .meta-grid{grid-template-columns:1fr 1fr;} .compact-facts,.aggregate-facts{grid-template-columns:1fr 1fr;} }
@media (max-width: 520px) { .app-shell{padding:10px; gap:10px;} .topbar{gap:8px; padding:6px 0;} .brand-mark{width:28px; height:28px; border-radius:8px;} .replay-chip{padding:3px 7px;} .transport{display:none;} .run-summary{padding:12px; gap:10px;} .outcome-card{padding-left:10px; border-left-width:4px;} .outcome-card h2{font-size:24px; margin:4px 0;} .outcome-card p,.summary-note{font-size:12px;} .summary-facts{grid-template-columns:repeat(2,minmax(0,1fr)); gap:6px;} .summary-facts article{padding:5px 0 5px 8px;} .summary-facts b{font-size:13px;} .summary-facts span{font-size:10px;} .metadata-row{grid-template-columns:1fr 1fr; gap:6px; font-size:11px;} .summary-note{display:none;} .investigation-grid,.metadata-strip .meta-grid,.warning-groups,.compact-facts,.aggregate-facts{grid-template-columns:1fr;} .timeline-row{grid-template-columns:1fr;} .timeline-row time,.rail{display:none;} .timeline-row article{grid-template-columns:30px minmax(0,1fr); min-height:48px; padding:10px;} .timeline-row em{display:none;} .panel{padding:12px;} .artifact-list li{grid-template-columns:1fr;} }
`;
