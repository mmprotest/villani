import { createElement as h, useId } from "react";

import { statusDescriptor } from "./index.js";

const classes = (...values) => values.filter(Boolean).join(" ");

export function AppShell({ sidebar, header, statusStrip, children, className, ...props }) {
  return h("div", { ...props, className: classes("v-app-shell", className) }, sidebar, header, statusStrip,
    h("main", { className: "v-canvas", id: "main-content" }, children));
}

export function Sidebar({ brand = "VILLANI", children, className, ...props }) {
  return h("aside", { ...props, className: classes("v-sidebar", className), "aria-label": props["aria-label"] || "Primary navigation" },
    h("div", { className: "v-sidebar__brand" }, brand), h("nav", { className: "v-sidebar__body" }, children));
}

export function SidebarSection({ title, children, className, ...props }) {
  return h("section", { ...props, className: classes("v-sidebar-section", className) },
    title ? h("h2", { className: "v-sidebar-section__title" }, title) : null, children);
}

export function SidebarItem({ active = false, glyph, children, className, ...props }) {
  return h("a", { ...props, className: classes("v-sidebar-item", className), "aria-current": active ? "page" : undefined },
    glyph ? h("span", { className: "v-sidebar-item__glyph", "aria-hidden": "true" }, glyph) : null,
    h("span", null, children));
}

export function TopHeader({ title, detail, actions, children, className, ...props }) {
  return h("header", { ...props, className: classes("v-top-header", className) },
    h("div", { className: "v-top-header__identity" },
      title ? h("strong", { className: "v-top-header__title" }, title) : null,
      detail ? h("span", { className: "v-top-header__detail" }, detail) : null, children),
    actions ? h("div", { className: "v-top-header__actions" }, actions) : null);
}

export function StatusStrip({ children, className, ...props }) {
  return h("div", { ...props, className: classes("v-status-strip", className), role: props.role || "status" }, children);
}

export function Panel({ children, className, ...props }) {
  return h("section", { ...props, className: classes("v-panel", className) }, children);
}

export function PanelHeader({ title, meta, actions, children, className, ...props }) {
  return h("header", { ...props, className: classes("v-panel-header", className) },
    h("div", { className: "v-panel-header__identity" },
      title ? h("h2", { className: "v-panel-header__title" }, title) : null,
      meta ? h("span", { className: "v-panel-header__meta" }, meta) : null, children),
    actions ? h("div", { className: "v-panel-header__actions" }, actions) : null);
}

export function MetricCard({ label, value, detail, sparkline, className, ...props }) {
  return h("article", { ...props, className: classes("v-metric-card", className) },
    h("span", { className: "v-metric-card__label" }, label),
    h("strong", { className: "v-metric-card__value" }, value ?? "—"),
    detail ? h("span", { className: "v-metric-card__detail" }, detail) : null,
    sparkline ? h("div", { className: "v-metric-card__sparkline" }, sparkline) : null);
}

export function DataTable({ columns, rows, caption, getRowKey = (_row, index) => index, empty = "No records", className, ...props }) {
  const head = h("thead", null, h("tr", null, columns.map((column) =>
    h("th", { key: column.key, scope: "col", className: column.className }, column.header))));
  const bodyRows = rows.length ? rows.map((row, rowIndex) =>
    h("tr", { key: getRowKey(row, rowIndex) }, columns.map((column) =>
      h("td", { key: column.key, className: column.className },
        column.render ? column.render(row, rowIndex) : row[column.key])))) :
    h("tr", null, h("td", { colSpan: columns.length, className: "v-table-empty" }, empty));
  const table = h("table", { ...props, className: classes("v-data-table", className) },
    caption ? h("caption", { className: "v-sr-only" }, caption) : null,
    head,
    h("tbody", null, bodyRows));
  return h("div", { className: "v-table-wrap" }, table);
}

export function StatusBadge({ status = "unknown", label, className, ...props }) {
  const descriptor = statusDescriptor(status);
  const key = String(status || "unknown").toLowerCase();
  return h("span", { ...props, className: classes("v-status-badge", className), "data-status": key },
    h("span", { "aria-hidden": "true" }, descriptor.glyph), label || descriptor.label);
}

export function Button({ variant = "default", className, type = "button", ...props }) {
  return h("button", { ...props, type, className: classes("v-button", className), "data-variant": variant });
}

export function IconButton({ label, className, type = "button", ...props }) {
  return h("button", { ...props, type, className: classes("v-icon-button", className), "aria-label": label || props["aria-label"] });
}

function Field({ label, id, children }) {
  return h("label", { className: "v-field", htmlFor: id },
    label ? h("span", { className: "v-field__label" }, label) : null, children);
}

export function TextInput({ label, id, className, ...props }) {
  const inputId = id || useId();
  return h(Field, { label, id: inputId }, h("input", { ...props, id: inputId, className: classes("v-input", className) }));
}

export function Select({ label, id, options = [], children, className, ...props }) {
  const selectId = id || useId();
  return h(Field, { label, id: selectId }, h("select", { ...props, id: selectId, className: classes("v-select", className) },
    children || options.map((option) => h("option", { key: option.value, value: option.value, disabled: option.disabled }, option.label))));
}

export function Tabs({ tabs, activeId, onChange, label = "Sections", className, ...props }) {
  return h("div", { ...props, className: classes("v-tabs", className), role: "tablist", "aria-label": label },
    tabs.map((tab) => h("button", { key: tab.id, type: "button", className: "v-tab", role: "tab",
      "aria-selected": activeId === tab.id, "aria-controls": tab.controls, disabled: tab.disabled,
      onClick: () => onChange?.(tab.id) }, tab.label)));
}

export function Tooltip({ content, children, className, ...props }) {
  const id = useId();
  return h("span", { ...props, className: classes("v-tooltip", className), tabIndex: 0, "aria-describedby": id },
    children, h("span", { id, className: "v-tooltip__content", role: "tooltip" }, content));
}

export function Dialog({ open, title, onClose, children, className, ...props }) {
  if (!open) return null;
  return h("div", { className: "v-dialog-backdrop", onMouseDown: (event) => event.target === event.currentTarget && onClose?.() },
    h("section", { ...props, className: classes("v-dialog", className), role: "dialog", "aria-modal": "true", "aria-label": title },
      h(PanelHeader, { title, actions: h(IconButton, { label: "Close dialog", onClick: onClose }, "×") }),
      h("div", { className: "v-panel__body" }, children)));
}

export function Drawer({ open, title, onClose, children, className, ...props }) {
  if (!open) return null;
  return h("aside", { ...props, className: classes("v-drawer", className), "aria-label": title },
    h(PanelHeader, { title, actions: h(IconButton, { label: "Close drawer", onClick: onClose }, "×") }),
    h("div", { className: "v-panel__body" }, children));
}

function State({ kind, glyph, title, detail, children, className, ...props }) {
  return h("div", { ...props, className: classes("v-state", className), "data-state": kind }, h("div", null,
    h("div", { className: "v-state__glyph", "aria-hidden": "true" }, glyph),
    h("h2", { className: "v-state__title" }, title),
    detail ? h("p", { className: "v-state__detail" }, detail) : null, children));
}

export function EmptyState({ title = "No data", detail, ...props }) {
  return h(State, { ...props, kind: "empty", glyph: "○", title, detail });
}
export function ErrorState({ title = "Unable to load", detail, ...props }) {
  return h(State, { ...props, kind: "error", glyph: "×", title, detail, role: "alert" });
}
export function LoadingState({ title = "Loading", detail, ...props }) {
  return h(State, { ...props, kind: "loading", glyph: "◉", title, detail, "aria-live": "polite" });
}

export function Timeline({ children, className, ...props }) {
  return h("ol", { ...props, className: classes("v-timeline", className) }, children);
}
export function TimelineNode({ title, meta, marker = "·", active = false, children, className, ...props }) {
  return h("li", { ...props, className: classes("v-timeline-node", className), "data-active": active },
    h("span", { className: "v-timeline-node__marker", "aria-hidden": "true" }, marker),
    h("div", { className: "v-timeline-node__content" }, h("div", { className: "v-timeline-node__title" }, title),
      meta ? h("div", { className: "v-timeline-node__meta" }, meta) : null, children));
}

export function EventTable(props) {
  return h(DataTable, { ...props, className: classes("v-event-table", props.className) });
}

export function KeyValueGrid({ items, className, ...props }) {
  return h("dl", { ...props, className: classes("v-key-value-grid", className) }, items.map((item, index) => {
    const label = Array.isArray(item) ? item[0] : item.label;
    const value = Array.isArray(item) ? item[1] : item.value;
    return h("div", { className: "v-key-value-grid__item", key: `${label}-${index}` },
      h("dt", null, label), h("dd", null, value ?? "—"));
  }));
}

export function AsciiCorners() {
  return h("span", { "aria-hidden": "true" },
    h("span", { className: "v-ascii-corner v-ascii-corner--tl" }, "+"),
    h("span", { className: "v-ascii-corner v-ascii-corner--tr" }, "+"),
    h("span", { className: "v-ascii-corner v-ascii-corner--bl" }, "+"),
    h("span", { className: "v-ascii-corner v-ascii-corner--br" }, "+"));
}
export function AsciiFrame({ children, className, ...props }) {
  return h("div", { ...props, className: classes("v-ascii-frame", className) }, h(AsciiCorners), children);
}

export function Sparkline({ values = [], label = "Trend", className, ...props }) {
  const width = 100;
  const height = 30;
  const numeric = values.map(Number).filter(Number.isFinite);
  const min = Math.min(...numeric, 0);
  const max = Math.max(...numeric, 1);
  const span = max - min || 1;
  const points = numeric.map((value, index) => {
    const x = numeric.length === 1 ? width / 2 : (index / (numeric.length - 1)) * width;
    const y = height - ((value - min) / span) * height;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  return h("svg", { ...props, className: classes("v-sparkline", className), viewBox: `0 0 ${width} ${height}`,
    role: "img", "aria-label": label, preserveAspectRatio: "none" },
    h("line", { className: "v-sparkline__grid", x1: 0, x2: width, y1: height - 0.5, y2: height - 0.5 }),
    points ? h("polyline", { className: "v-sparkline__line", points }) : null);
}
