export const villaniTokens = Object.freeze({
  backgroundDeepest: "#050505",
  backgroundPanel: "#090909",
  backgroundElevated: "#0d0d0d",
  backgroundSelected: "#161616",
  textPrimary: "#f2f2f2",
  textSecondary: "#b8b8b8",
  textMuted: "#858585",
  borderSubtle: "#303030",
  borderStandard: "#555555",
  borderStrong: "#a3a3a3",
  focus: "#ffffff",
  disabled: "#626262"
});

export const villaniThemeCss = `
:root{--villani-bg-deepest:#050505;--villani-bg-panel:#090909;--villani-bg-elevated:#0d0d0d;--villani-bg-selected:#161616;--villani-text-primary:#f2f2f2;--villani-text-secondary:#b8b8b8;--villani-text-muted:#858585;--villani-border-subtle:#303030;--villani-border-standard:#555555;--villani-border-strong:#a3a3a3;--villani-focus:#ffffff;--villani-disabled:#626262;--villani-font:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono",monospace;color-scheme:dark}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--villani-bg-deepest);color:var(--villani-text-primary);font-family:var(--villani-font)}
:focus-visible{outline:2px solid var(--villani-focus);outline-offset:3px}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
`;
