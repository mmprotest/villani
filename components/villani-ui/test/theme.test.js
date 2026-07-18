import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
  statusDescriptor,
  uiClassNames,
  villaniThemeCss,
  villaniTokens,
} from "../index.js";

const channel = (value) => {
  const normalized = value / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
};

const luminance = (hex) => {
  const value = Number.parseInt(hex.slice(1), 16);
  return (
    0.2126 * channel((value >> 16) & 255) +
    0.7152 * channel((value >> 8) & 255) +
    0.0722 * channel(value & 255)
  );
};

const contrast = (foreground, background) => {
  const values = [luminance(foreground), luminance(background)].sort(
    (left, right) => right - left,
  );
  return (values[0] + 0.05) / (values[1] + 0.05);
};

test("shared theme is monochrome, light, and accessible", () => {
  assert.equal(villaniTokens.backgroundRoot, "#f6f6f3");
  assert.equal(villaniTokens.backgroundPanel, "#ffffff");
  assert.equal(villaniTokens.textPrimary, "#171717");
  assert.match(villaniThemeCss, /color-scheme:\s*light/);
  assert.match(villaniThemeCss, /\.v-app-shell/);
  assert.match(villaniThemeCss, /\.v-panel/);
  assert.match(villaniThemeCss, /:focus-visible/);
  assert.match(villaniThemeCss, /prefers-reduced-motion:\s*reduce/);
  assert.doesNotMatch(
    villaniThemeCss,
    /green|blue|#0f0|#00ff00|#22c55e|#10b981|#2563eb|#3b82f6|#090d19|#11182a|#45dfa7/i,
  );
  assert.equal(villaniTokens.focus, "#171717");
  for (const [foreground, background] of [
    [villaniTokens.textPrimary, villaniTokens.backgroundPanel],
    [villaniTokens.textSecondary, villaniTokens.backgroundPanel],
    [villaniTokens.textMuted, villaniTokens.backgroundPanel],
    [villaniTokens.danger, villaniTokens.dangerBackground],
    [villaniTokens.warning, villaniTokens.warningBackground],
  ])
    assert.ok(
      contrast(foreground, background) >= 4.5,
      `${foreground} on ${background} must meet WCAG AA for normal text`,
    );
});

test("shared component contracts include shell and textual status", () => {
  assert.equal(uiClassNames.appShell, "v-app-shell");
  assert.deepEqual(statusDescriptor("failed"), { glyph: "×", label: "FAILED" });
  assert.deepEqual(statusDescriptor("selected"), { glyph: "◆", label: "SELECTED" });
});

test("shared package exports the complete control-plane component set", async () => {
  const source = await readFile(new URL("../react.js", import.meta.url), "utf8");
  const required = [
    "AppShell", "Sidebar", "SidebarSection", "SidebarItem", "TopHeader", "StatusStrip",
    "Panel", "PanelHeader", "MetricCard", "DataTable", "StatusBadge", "Button", "IconButton",
    "TextInput", "Select", "Tabs", "Tooltip", "Dialog", "Drawer", "EmptyState", "ErrorState",
    "LoadingState", "Timeline", "TimelineNode", "EventTable", "KeyValueGrid", "AsciiFrame",
    "AsciiCorners", "Sparkline", "PrimaryNavigation", "ActionableSystemNotice", "PageIntro",
    "TaskComposerShell", "ProgressStages", "ResultVerdict", "EvidenceDisclosure", "FormField",
    "TextArea", "PrimaryAction", "SecondaryAction", "CostDisplay", "DurationDisplay",
  ];
  for (const name of required) assert.match(source, new RegExp(`export function ${name}\\b`), name);
});

test("recorded onboarding consumes the shared theme without a standalone palette", async () => {
  const source = await readFile(
    new URL("../../../onboarding-verification/run_onboarding_gate.py", import.meta.url),
    "utf8",
  );
  const transcript =
    source.match(/def _transcript_html[\s\S]+?(?=\r?\ndef run_gate)/)?.[0] ?? "";
  assert.match(transcript, /components" \/ "villani-ui" \/ "theme\.css/);
  assert.doesNotMatch(transcript, /#[0-9a-f]{3,8}|green|blue|glow/i);
});
