import { villaniThemeCss } from "./theme-source.js";

export const villaniTokens = Object.freeze({
  backgroundRoot: "#f6f6f3",
  backgroundPanel: "#ffffff",
  backgroundElevated: "#f0f0ec",
  backgroundSelected: "#e7e7e1",
  textPrimary: "#171717",
  textSecondary: "#454542",
  textMuted: "#62625d",
  borderSubtle: "#ddddd7",
  borderDefault: "#b8b8b0",
  borderStrong: "#686862",
  focus: "#171717",
  disabled: "#777771",
  danger: "#9a1b1b",
  dangerBackground: "#fff3f2",
  dangerBorder: "#c77873",
  warning: "#765600",
  warningBackground: "#fff8df",
  warningBorder: "#b99a42",
  sidebarWidth: "232px",
  headerHeight: "56px",
  statusHeight: "42px",
  radius: "6px",
  font: 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  fontMono:
    'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
});

export const chartTokens = Object.freeze({
  grid: "#ddddd7",
  axis: "#62625d",
  primary: "#171717",
  secondary: "#686862",
  rejected: "#777771",
  selected: "#171717",
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
  pageIntro: "v-page-intro",
  actionableSystemNotice: "v-actionable-notice",
  progressStages: "v-progress-stages",
});

export { villaniThemeCss };

export function statusDescriptor(status) {
  const key = String(status || "unknown").toLowerCase();
  return statusDescriptors[key] || {
    glyph: "?",
    label: key ? key.toUpperCase() : "UNKNOWN",
  };
}
