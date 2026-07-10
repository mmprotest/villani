import { ReplayDashboardViewModel } from "../viewModel.js";
import { escapeHtml, truncateText } from "../safeHtml.js";
const warningGroups = (warnings: string[]) => {
  const rows = warnings.length
    ? warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")
    : "<li>No warnings captured</li>";
  return `<section><h4>Parse warnings</h4><ul>${rows}</ul></section><section><h4>Replay warnings</h4><ul><li>No replay warnings</li></ul></section><section><h4>Redaction warnings</h4><ul><li>No redaction warnings</li></ul></section><section><h4>Graph/status warnings</h4><ul><li>Minor uncertainty is shown as partial, not failure.</li></ul></section>`;
};
const redactionSummary = (report: unknown) => {
  if (!report || Object.keys(report as Record<string, unknown>).length === 0)
    return `<p>No redactions applied</p>`;
  const values = Object.values(report as Record<string, unknown>);
  if (values.length && values.every((v) => Number(v) === 0))
    return `<p>No redactions applied</p>`;
  return `<pre>${escapeHtml(JSON.stringify(report, null, 2))}</pre>`;
};
export const detailPanel = (vm: ReplayDashboardViewModel) => {
  const tabs = [
    "Event Detail",
    "Changed Files",
    "Diff",
    "Raw JSON",
    "Warnings",
    "Redaction",
  ];
  return `<section class="panel detail-panel"><span class="sr-only">Selected replay event metadata</span><div class="tabs">${tabs.map((t, i) => `<button class="tab ${i === 0 ? "active" : ""}" data-tab="${t}">${t}${t === "Warnings" && vm.warnings.length ? `<span>${vm.warnings.length}</span>` : ""}</button>`).join("")}</div><div id="detailContent" class="detail-content"><div class="detail-placeholder"><span class="sr-only">Selected replay event metadata</span>Initial Event Detail</div></div><template id="changedFiles">${escapeHtml(vm.changedFiles.join("\n") || "Not captured")}</template><template id="gitDiff">${escapeHtml(truncateText(vm.diff))}</template><template id="warningsData">${warningGroups(vm.warnings)}</template><template id="redactionData">${redactionSummary(vm.redactionReport)}</template></section>`;
};
