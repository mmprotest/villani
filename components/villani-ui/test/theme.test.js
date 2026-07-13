import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
  statusDescriptor,
  uiClassNames,
  villaniThemeCss,
  villaniTokens,
} from "../index.js";

test("shared theme is monochrome and dark", () => {
  assert.equal(villaniTokens.backgroundRoot, "#050505");
  assert.equal(villaniTokens.textPrimary, "#f2f2f2");
  assert.match(villaniThemeCss, /color-scheme:\s*dark/);
  assert.match(villaniThemeCss, /\.v-app-shell/);
  assert.match(villaniThemeCss, /\.v-panel/);
  assert.doesNotMatch(
    villaniThemeCss,
    /green|blue|#0f0|#00ff00|#22c55e|#10b981|#2563eb|#3b82f6|#f8fafc|#f7f3ea/i,
  );
  assert.equal(villaniTokens.focus, "#ffffff");
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
    "AsciiCorners", "Sparkline",
  ];
  for (const name of required) assert.match(source, new RegExp(`export function ${name}\\b`), name);
});
