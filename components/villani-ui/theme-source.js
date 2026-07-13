export const villaniThemeCss = String.raw`
:root {
  color-scheme: dark;
  --v-bg-root: #050505;
  --v-bg-panel: #090909;
  --v-bg-elevated: #0d0d0d;
  --v-bg-selected: #161616;
  --v-text-primary: #f2f2f2;
  --v-text-secondary: #b8b8b8;
  --v-text-muted: #858585;
  --v-border-subtle: #303030;
  --v-border-default: #555555;
  --v-border-strong: #a3a3a3;
  --v-focus: #ffffff;
  --v-disabled: #626262;
  --v-danger: #ff9b9b;
  --v-warning: #d8c99b;
  --v-sidebar-width: 232px;
  --v-header-height: 48px;
  --v-status-height: 30px;
  --v-font: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  --villani-bg-deepest: var(--v-bg-root);
  --villani-bg-panel: var(--v-bg-panel);
  --villani-bg-elevated: var(--v-bg-elevated);
  --villani-bg-selected: var(--v-bg-selected);
  --villani-text-primary: var(--v-text-primary);
  --villani-text-secondary: var(--v-text-secondary);
  --villani-text-muted: var(--v-text-muted);
  --villani-border-subtle: var(--v-border-subtle);
  --villani-border-standard: var(--v-border-default);
  --villani-border-strong: var(--v-border-strong);
  --villani-focus: var(--v-focus);
  --villani-disabled: var(--v-disabled);
  --villani-font: var(--v-font);
}
*, *::before, *::after { box-sizing: border-box; }
.v-sr-only { position: absolute !important; width: 1px !important; height: 1px !important; padding: 0 !important; margin: -1px !important; overflow: hidden !important; clip: rect(0,0,0,0) !important; white-space: nowrap !important; border: 0 !important; }
html, body, #root { min-height: 100%; }
html { background: var(--v-bg-root); }
body {
  margin: 0;
  background: var(--v-bg-root);
  color: var(--v-text-primary);
  font-family: var(--v-font);
  font-size: 13px;
  line-height: 1.45;
  text-rendering: optimizeLegibility;
}
button, input, select, textarea { font: inherit; }
button, input, select, textarea, dialog { color-scheme: dark; }
a { color: inherit; }
:focus-visible { outline: 2px solid var(--v-focus); outline-offset: 2px; }
::selection { background: var(--v-text-primary); color: var(--v-bg-root); }
.v-sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
.v-app-shell {
  min-height: 100vh;
  display: grid;
  grid-template-columns: var(--v-sidebar-width) minmax(0, 1fr);
  grid-template-rows: var(--v-header-height) var(--v-status-height) minmax(0, 1fr);
  grid-template-areas: "sidebar header" "sidebar status" "sidebar main";
  background: var(--v-bg-root);
}
.v-sidebar {
  grid-area: sidebar;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow: auto;
  border-right: 1px solid var(--v-border-default);
  background: var(--v-bg-panel);
  scrollbar-color: var(--v-border-default) var(--v-bg-panel);
}
.v-sidebar__brand { min-height: var(--v-header-height); display: flex; align-items: center; gap: 9px; padding: 0 14px; border-bottom: 1px solid var(--v-border-default); letter-spacing: .14em; font-weight: 700; }
.v-sidebar__mark { color: var(--v-text-secondary); }
.v-sidebar__body { padding: 12px 8px 20px; }
.v-sidebar-section { margin-bottom: 16px; }
.v-sidebar-section__title { margin: 0 7px 5px; color: var(--v-text-muted); font-size: 10px; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; }
.v-sidebar-item { display: grid; grid-template-columns: 18px minmax(0, 1fr) auto; align-items: center; gap: 7px; min-height: 30px; padding: 5px 8px; border: 1px solid transparent; color: var(--v-text-secondary); text-decoration: none; }
.v-sidebar-item:hover { border-color: var(--v-border-subtle); background: var(--v-bg-elevated); color: var(--v-text-primary); }
.v-sidebar-item[aria-current="page"], .v-sidebar-item[data-active="true"] { border-color: var(--v-border-default); background: var(--v-bg-selected); color: var(--v-text-primary); }
.v-sidebar-item__glyph { text-align: center; color: var(--v-text-muted); }
.v-sidebar-item__meta { color: var(--v-text-muted); font-size: 10px; }
.v-top-header { grid-area: header; position: sticky; top: 0; z-index: 15; display: flex; align-items: center; justify-content: space-between; gap: 16px; min-width: 0; padding: 0 16px; border-bottom: 1px solid var(--v-border-default); background: rgba(5,5,5,.96); }
.v-top-header__identity { min-width: 0; display: flex; align-items: baseline; gap: 10px; }
.v-top-header__title { margin: 0; font-size: 13px; letter-spacing: .09em; text-transform: uppercase; white-space: nowrap; }
.v-top-header__subtitle { overflow: hidden; color: var(--v-text-muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.v-top-header__actions { display: flex; align-items: center; gap: 7px; }
.v-status-strip { grid-area: status; position: sticky; top: var(--v-header-height); z-index: 14; display: flex; align-items: center; gap: 18px; min-width: 0; min-height: var(--v-status-height); padding: 0 16px; overflow: hidden; border-bottom: 1px solid var(--v-border-subtle); background: var(--v-bg-elevated); color: var(--v-text-secondary); font-size: 10px; letter-spacing: .05em; white-space: nowrap; }
.v-status-strip__item { display: inline-flex; align-items: center; gap: 6px; }
.v-canvas { grid-area: main; min-width: 0; padding: 12px; background-color: var(--v-bg-root); background-image: linear-gradient(var(--v-border-subtle) 1px, transparent 1px), linear-gradient(90deg, var(--v-border-subtle) 1px, transparent 1px); background-size: 32px 32px; background-position: -1px -1px; }
.v-grid { display: grid; gap: 10px; min-width: 0; }
.v-grid--metrics { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.v-grid--2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.v-grid--3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.v-stack { display: grid; gap: 10px; }
.v-cluster { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; }
.v-panel { position: relative; min-width: 0; border: 1px solid var(--v-border-default); border-radius: 0; background: var(--v-bg-panel); box-shadow: 0 0 22px rgba(255,255,255,.018); }
.v-panel::before, .v-panel::after { content: ""; position: absolute; pointer-events: none; width: 8px; height: 8px; }
.v-panel::before { top: -1px; left: -1px; border-top: 1px solid var(--v-border-strong); border-left: 1px solid var(--v-border-strong); }
.v-panel::after { right: -1px; bottom: -1px; border-right: 1px solid var(--v-border-strong); border-bottom: 1px solid var(--v-border-strong); }
.v-panel-header { min-height: 34px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 7px 10px; border-bottom: 1px solid var(--v-border-subtle); background: var(--v-bg-elevated); }
.v-panel-header__title { margin: 0; color: var(--v-text-primary); font-size: 11px; font-weight: 650; letter-spacing: .11em; text-transform: uppercase; }
.v-panel-header__meta { color: var(--v-text-muted); font-size: 10px; }
.v-panel__body { min-width: 0; padding: 10px; }
.v-metric-card { min-width: 0; min-height: 86px; padding: 10px; border: 1px solid var(--v-border-subtle); background: var(--v-bg-panel); }
.v-metric-card__label { color: var(--v-text-muted); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; }
.v-metric-card__value { margin-top: 8px; overflow: hidden; color: var(--v-text-primary); font-size: clamp(18px, 2.1vw, 28px); line-height: 1; text-overflow: ellipsis; white-space: nowrap; }
.v-metric-card__detail { margin-top: 8px; color: var(--v-text-secondary); font-size: 10px; }
.v-table-wrap { width: 100%; overflow: auto; scrollbar-color: var(--v-border-default) var(--v-bg-panel); }
.v-data-table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
.v-data-table th { position: sticky; top: 0; z-index: 1; padding: 7px 9px; border-bottom: 1px solid var(--v-border-default); background: var(--v-bg-elevated); color: var(--v-text-muted); font-size: 10px; font-weight: 600; letter-spacing: .08em; text-align: left; text-transform: uppercase; white-space: nowrap; }
.v-data-table td { padding: 7px 9px; border-bottom: 1px solid var(--v-border-subtle); color: var(--v-text-secondary); vertical-align: top; }
.v-data-table tbody tr:hover td { background: var(--v-bg-selected); color: var(--v-text-primary); }
.v-data-table tbody tr:last-child td { border-bottom: 0; }
.v-table-empty { height: 80px; color: var(--v-text-muted) !important; text-align: center; vertical-align: middle !important; }
.v-status-badge { display: inline-flex; align-items: center; gap: 5px; min-height: 22px; padding: 2px 6px; border: 1px solid var(--v-border-default); background: var(--v-bg-elevated); color: var(--v-text-secondary); font-size: 10px; font-weight: 650; letter-spacing: .055em; white-space: nowrap; }
.v-status-badge[data-status="failed"], .v-status-badge[data-status="error"] { border-color: #8c5555; color: var(--v-danger); }
.v-status-badge[data-status="running"] { border-style: dashed; color: var(--v-text-primary); }
.v-status-badge[data-status="selected"] { border-color: var(--v-border-strong); color: var(--v-text-primary); }
.v-status-badge[data-status="redacted"] { border-color: #8b7d55; color: var(--v-warning); }
.v-button, .v-icon-button { appearance: none; min-height: 30px; border: 1px solid var(--v-border-default); border-radius: 0; background: var(--v-bg-elevated); color: var(--v-text-primary); cursor: pointer; }
.v-button { padding: 5px 10px; letter-spacing: .04em; }
.v-icon-button { width: 30px; padding: 0; display: inline-grid; place-items: center; }
.v-button:hover, .v-icon-button:hover { border-color: var(--v-border-strong); background: var(--v-bg-selected); }
.v-button:disabled, .v-icon-button:disabled { border-color: var(--v-border-subtle); color: var(--v-disabled); cursor: not-allowed; }
.v-button[data-variant="primary"] { border-color: var(--v-text-primary); background: var(--v-text-primary); color: var(--v-bg-root); }
.v-field { display: grid; gap: 5px; min-width: 0; }
.v-field__label { color: var(--v-text-muted); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.v-input, .v-select { width: 100%; min-height: 32px; border: 1px solid var(--v-border-default); border-radius: 0; background: var(--v-bg-root); color: var(--v-text-primary); padding: 5px 8px; }
.v-input::placeholder { color: var(--v-text-muted); opacity: 1; }
.v-select { appearance: none; background-image: linear-gradient(45deg,transparent 50%,var(--v-text-secondary) 50%),linear-gradient(135deg,var(--v-text-secondary) 50%,transparent 50%); background-position: calc(100% - 12px) 13px,calc(100% - 8px) 13px; background-size: 4px 4px,4px 4px; background-repeat: no-repeat; padding-right: 24px; }
.v-tabs { display: flex; gap: 0; overflow-x: auto; border-bottom: 1px solid var(--v-border-default); }
.v-tab { appearance: none; min-height: 32px; padding: 6px 10px; border: 0; border-right: 1px solid var(--v-border-subtle); border-radius: 0; background: var(--v-bg-panel); color: var(--v-text-muted); cursor: pointer; }
.v-tab[aria-selected="true"] { background: var(--v-bg-selected); color: var(--v-text-primary); box-shadow: inset 0 -1px var(--v-text-primary); }
.v-tooltip { position: relative; display: inline-flex; }
.v-tooltip__content { position: absolute; z-index: 50; left: 50%; bottom: calc(100% + 7px); width: max-content; max-width: 260px; transform: translateX(-50%); padding: 5px 7px; border: 1px solid var(--v-border-default); background: var(--v-bg-elevated); color: var(--v-text-primary); font-size: 10px; visibility: hidden; opacity: 0; pointer-events: none; }
.v-tooltip:hover .v-tooltip__content, .v-tooltip:focus-within .v-tooltip__content { visibility: visible; opacity: 1; }
.v-dialog-backdrop { position: fixed; inset: 0; z-index: 80; display: grid; place-items: center; padding: 20px; background: rgba(0,0,0,.82); }
.v-dialog { width: min(620px, 100%); max-height: calc(100vh - 40px); overflow: auto; border: 1px solid var(--v-border-strong); background: var(--v-bg-panel); box-shadow: 0 0 50px rgba(255,255,255,.07); }
.v-drawer { position: fixed; z-index: 70; top: 0; right: 0; width: min(520px, 92vw); height: 100vh; overflow: auto; border-left: 1px solid var(--v-border-strong); background: var(--v-bg-panel); box-shadow: -20px 0 50px rgba(0,0,0,.6); }
.v-state { display: grid; place-items: center; min-height: 180px; padding: 24px; color: var(--v-text-secondary); text-align: center; }
.v-state__glyph { font-size: 22px; color: var(--v-text-muted); }
.v-state__title { margin: 8px 0 0; color: var(--v-text-primary); font-size: 13px; }
.v-state__detail { max-width: 620px; margin: 5px 0 0; color: var(--v-text-muted); }
.v-timeline { position: relative; display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
.v-timeline::before { content: ""; position: absolute; top: 12px; bottom: 12px; left: 8px; border-left: 1px solid var(--v-border-default); }
.v-timeline-node { position: relative; display: grid; grid-template-columns: 18px minmax(0,1fr); gap: 8px; padding: 7px 0; }
.v-timeline-node__marker { position: relative; z-index: 1; width: 17px; height: 17px; display: grid; place-items: center; border: 1px solid var(--v-border-default); background: var(--v-bg-panel); color: var(--v-text-secondary); font-size: 8px; }
.v-timeline-node[data-active="true"] .v-timeline-node__marker { border-color: var(--v-border-strong); background: var(--v-bg-selected); color: var(--v-text-primary); box-shadow: 0 0 10px rgba(255,255,255,.1); }
.v-timeline-node__title { color: var(--v-text-primary); }
.v-timeline-node__meta { margin-top: 2px; color: var(--v-text-muted); font-size: 10px; }
.v-key-value-grid { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); margin: 0; border-top: 1px solid var(--v-border-subtle); border-left: 1px solid var(--v-border-subtle); }
.v-key-value-grid__item { min-width: 0; padding: 7px 9px; border-right: 1px solid var(--v-border-subtle); border-bottom: 1px solid var(--v-border-subtle); }
.v-key-value-grid dt { color: var(--v-text-muted); font-size: 10px; letter-spacing: .06em; text-transform: uppercase; }
.v-key-value-grid dd { margin: 4px 0 0; overflow-wrap: anywhere; color: var(--v-text-primary); }
.v-ascii-frame { position: relative; border: 1px solid var(--v-border-subtle); background: var(--v-bg-panel); }
.v-ascii-corner { position: absolute; z-index: 2; color: var(--v-text-muted); font-size: 10px; line-height: 1; pointer-events: none; }
.v-ascii-corner--tl { top: -5px; left: -4px; }.v-ascii-corner--tr { top: -5px; right: -4px; }.v-ascii-corner--bl { bottom: -5px; left: -4px; }.v-ascii-corner--br { right: -4px; bottom: -5px; }
.v-sparkline { display: block; width: 100%; height: 34px; overflow: visible; }
.v-sparkline__grid { stroke: var(--v-border-subtle); stroke-width: 1; }
.v-sparkline__line { fill: none; stroke: var(--v-text-primary); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.v-code { margin: 0; padding: 9px; overflow: auto; border: 1px solid var(--v-border-subtle); background: var(--v-bg-root); color: var(--v-text-secondary); font-family: var(--v-font); font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; }
.v-notice { padding: 8px 10px; border: 1px solid var(--v-border-default); background: var(--v-bg-elevated); color: var(--v-text-secondary); }
.v-notice[data-kind="redaction"] { border-color: #8b7d55; color: var(--v-warning); }
.v-divider { height: 1px; border: 0; background: var(--v-border-subtle); }
.v-muted { color: var(--v-text-muted); }.v-secondary { color: var(--v-text-secondary); }.v-primary { color: var(--v-text-primary); }
.v-mono-number { font-variant-numeric: tabular-nums; }
.v-truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 1180px) { .v-grid--metrics { grid-template-columns: repeat(2,minmax(0,1fr)); } .v-grid--3 { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media (max-width: 900px) {
  :root { --v-sidebar-width: 184px; }
  .v-canvas { padding: 8px; }
  .v-grid--2, .v-grid--3 { grid-template-columns: 1fr; }
  .v-key-value-grid { grid-template-columns: 1fr; }
}
@media (max-width: 680px) {
  .v-app-shell { grid-template-columns: 1fr; grid-template-rows: auto var(--v-header-height) var(--v-status-height) minmax(0,1fr); grid-template-areas: "sidebar" "header" "status" "main"; }
  .v-sidebar { position: static; width: 100%; height: auto; border-right: 0; border-bottom: 1px solid var(--v-border-default); }
  .v-sidebar__brand { min-height: 38px; }
  .v-sidebar__body { display: flex; gap: 4px; overflow-x: auto; padding: 5px; }
  .v-sidebar-section { display: contents; }
  .v-sidebar-section__title { display: none; }
  .v-sidebar-item { min-width: max-content; }
  .v-top-header { top: 0; }
  .v-status-strip { top: var(--v-header-height); }
  .v-grid--metrics { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; } }
`;
