import { cloneElement, createElement as h, isValidElement, useId } from "react";

import { statusDescriptor } from "./index.js";

const classes = (...values) => values.filter(Boolean).join(" ");

export function AppShell({ sidebar, header, statusStrip, children, className, ...props }) {
  return h("div", { ...props, className: classes("v-app-shell", className), "data-has-notice": statusStrip ? "true" : "false" }, sidebar, header, statusStrip,
    h("main", { className: "v-canvas", id: "main-content" }, children));
}

export function Sidebar({ brand = "VILLANI", children, className, ...props }) {
  const label = props["aria-label"] || "Primary navigation";
  const asideProps = { ...props };
  delete asideProps["aria-label"];
  return h("aside", { ...asideProps, className: classes("v-sidebar", className) },
    h("div", { className: "v-sidebar__brand" }, brand), h("nav", { className: "v-sidebar__body", "aria-label": label }, children));
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

export function PrimaryNavigation({ primary = [], secondary = [], activeId, className, ...props }) {
  const items = (group) => group.map((item) => h(SidebarItem, {
    key: item.id,
    href: item.href,
    glyph: item.glyph,
    active: item.id === activeId,
  }, item.label));
  return h("div", { ...props, className: classes("v-primary-navigation", className) },
    h(SidebarSection, { title: "Primary" }, items(primary)),
    secondary.length ? h(SidebarSection, { title: "Secondary" }, items(secondary)) : null);
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

export function ActionableSystemNotice({ title, detail, actionHref, actionLabel = "Open settings", kind = "warning", className, ...props }) {
  return h("div", { ...props, className: classes("v-actionable-notice", className), "data-kind": kind },
    h("div", { className: "v-actionable-notice__content" },
      h("strong", { className: "v-actionable-notice__title" }, title),
      detail ? h("span", { className: "v-actionable-notice__detail" }, detail) : null),
    actionHref ? h("a", { className: "v-actionable-notice__action", href: actionHref }, actionLabel) : null);
}

export function PageIntro({ title, eyebrow, actions, children, className, ...props }) {
  return h("header", { ...props, className: classes("v-page-intro", className) },
    eyebrow ? h("span", { className: "v-page-intro__eyebrow" }, eyebrow) : null,
    h("h1", { tabIndex: -1 }, title),
    children ? h("p", null, children) : null,
    actions ? h("div", { className: "v-cluster v-page-intro__actions" }, actions) : null);
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

export function TaskComposerShell({ title = "New task", meta, children, className, ...props }) {
  return h(Panel, { ...props, className: classes("v-task-composer", className) },
    h(PanelHeader, { title, meta }), h("div", { className: "v-panel__body" }, children));
}

export function ProgressStages({ stages, current = 0, className, ...props }) {
  return h("ol", { ...props, className: classes("v-progress-stages", className),
    style: { ...(props.style || {}), "--v-stage-count": stages.length } }, stages.map((stage, index) =>
    h("li", { className: "v-progress-stage", key: stage.id || stage.label || index,
      "data-state": index < current ? "complete" : index === current ? "current" : "upcoming",
      "aria-current": index === current ? "step" : undefined }, stage.label || stage)));
}

const publicVerdicts = {
  "ready to apply": "Ready to apply",
  "needs review": "Needs review",
  "could not prove": "Could not prove",
  cancelled: "Cancelled",
  accepted: "Proved acceptable",
  exhausted: "Could not prove",
  rejected: "Could not prove",
  failed: "Could not complete",
  running: "In progress",
  completed: "Completed",
  succeeded: "Completed",
};

export function ResultVerdict({ status = "unknown", label, detail, className, ...props }) {
  const key = String(status).toLowerCase();
  const tone = ["failed", "error", "exhausted", "rejected", "could not prove", "cancelled"].includes(key) ? "error" : "neutral";
  return h("section", { ...props, className: classes("v-result-verdict", className), "data-tone": tone },
    h("strong", { className: "v-result-verdict__label" }, label || publicVerdicts[key] || statusDescriptor(key).label),
    detail ? h("p", { className: "v-result-verdict__detail" }, detail) : null);
}

export function EvidenceDisclosure({ summary = "Recorded evidence", children, className, ...props }) {
  return h("details", { ...props, className: classes("v-evidence-disclosure", className) },
    h("summary", null, summary), h("div", { className: "v-evidence-disclosure__body" }, children));
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

export function PrimaryAction(props) {
  return h(Button, { ...props, variant: "primary" });
}

export function SecondaryAction(props) {
  return h(Button, { ...props, variant: "secondary" });
}

export function IconButton({ label, className, type = "button", ...props }) {
  return h("button", { ...props, type, className: classes("v-icon-button", className), "aria-label": label || props["aria-label"] });
}

export function FormField({ label, id, help, error, required = false, children, className, ...props }) {
  const generatedId = useId();
  const controlId = id || generatedId;
  const helpId = `${controlId}-help`;
  const errorId = `${controlId}-error`;
  const describedBy = [help ? helpId : null, error ? errorId : null].filter(Boolean).join(" ") || undefined;
  const control = isValidElement(children) ? cloneElement(children, {
    id: controlId,
    required: children.props.required ?? required,
    "aria-describedby": [children.props["aria-describedby"], describedBy].filter(Boolean).join(" ") || undefined,
    "aria-invalid": error ? true : children.props["aria-invalid"],
  }) : children;
  return h("div", { ...props, className: classes("v-field", className) },
    label ? h("label", { className: "v-field__label", htmlFor: controlId }, label,
      required ? h("span", { className: "v-field__required", "aria-hidden": "true" }, " *") : null) : null,
    control,
    help ? h("span", { className: "v-field__help", id: helpId }, help) : null,
    error ? h("span", { className: "v-field__error", id: errorId, role: "alert" }, error) : null);
}

export function TextInput({ label, help, error, id, className, ...props }) {
  const inputId = id || useId();
  return h(FormField, { label, help, error, id: inputId, required: props.required }, h("input", { ...props, className: classes("v-input", className) }));
}

export function Select({ label, help, error, id, options = [], children, className, ...props }) {
  const selectId = id || useId();
  return h(FormField, { label, help, error, id: selectId, required: props.required }, h("select", { ...props, className: classes("v-select", className) },
    children || options.map((option) => h("option", { key: option.value, value: option.value, disabled: option.disabled }, option.label))));
}

export function TextArea({ label, help, error, id, className, ...props }) {
  const textareaId = id || useId();
  return h(FormField, { label, help, error, id: textareaId, required: props.required },
    h("textarea", { ...props, className: classes("v-textarea", className) }));
}

export function CostDisplay({ value, currency = "USD", accountingStatus = "unknown", className, ...props }) {
  const known = typeof value === "number" && Number.isFinite(value);
  return h("span", { ...props, className: classes("v-value", className), "data-known": String(known) },
    known ? `${currency || "USD"} ${value.toFixed(4)}` : `Unknown (${accountingStatus || "unknown"})`);
}

export function DurationDisplay({ milliseconds, className, ...props }) {
  const known = typeof milliseconds === "number" && Number.isFinite(milliseconds);
  let value = "Unknown";
  if (known && milliseconds < 1000) value = `${Math.round(milliseconds)} ms`;
  else if (known && milliseconds < 60000) value = `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 1 : 0)} s`;
  else if (known) value = `${(milliseconds / 60000).toFixed(1)} min`;
  return h("span", { ...props, className: classes("v-value", className), "data-known": String(known) }, value);
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
