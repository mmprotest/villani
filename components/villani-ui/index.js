import { villaniThemeCss } from "./theme-source.js";

export const villaniTokens = Object.freeze({
  backgroundRoot: "#050505",
  backgroundPanel: "#090909",
  backgroundElevated: "#0d0d0d",
  backgroundSelected: "#161616",
  textPrimary: "#f2f2f2",
  textSecondary: "#b8b8b8",
  textMuted: "#858585",
  borderSubtle: "#303030",
  borderDefault: "#555555",
  borderStrong: "#a3a3a3",
  focus: "#ffffff",
  disabled: "#626262",
  sidebarWidth: "232px",
  headerHeight: "48px",
  statusHeight: "30px",
  font: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
});

export const chartTokens = Object.freeze({
  grid: "#303030",
  axis: "#858585",
  primary: "#f2f2f2",
  secondary: "#a3a3a3",
  rejected: "#6f6f6f",
  selected: "#ffffff",
});

export const statusDescriptors = Object.freeze({
  succeeded: { glyph: "●", label: "SUCCEEDED" },
  completed: { glyph: "●", label: "COMPLETED" },
  running: { glyph: "◉", label: "RUNNING" },
  queued: { glyph: "○", label: "QUEUED" },
  failed: { glyph: "×", label: "FAILED" },
  exhausted: { glyph: "⊘", label: "EXHAUSTED" },
  rejected: { glyph: "⊘", label: "REJECTED" },
  selected: { glyph: "◆", label: "SELECTED" },
  redacted: { glyph: "!", label: "REDACTED" },
  unknown: { glyph: "?", label: "UNKNOWN" },
});

export const uiClassNames = Object.freeze({
  appShell: "v-app-shell",
  sidebar: "v-sidebar",
  topHeader: "v-top-header",
  statusStrip: "v-status-strip",
  canvas: "v-canvas",
  panel: "v-panel",
  panelHeader: "v-panel-header",
  metricCard: "v-metric-card",
  dataTable: "v-data-table",
  statusBadge: "v-status-badge",
});

export { villaniThemeCss };

export function statusDescriptor(status) {
  const key = String(status || "unknown").toLowerCase();
  return statusDescriptors[key] || {
    glyph: "?",
    label: key ? key.toUpperCase() : "UNKNOWN",
  };
}
