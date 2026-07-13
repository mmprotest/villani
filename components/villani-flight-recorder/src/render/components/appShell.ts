import { ReplayDashboardViewModel } from "../viewModel.js";
import { topBar } from "./topBar.js";
import { metricCards } from "./metricCards.js";
import { timeline } from "./timeline.js";
import { detailPanel } from "./detailPanel.js";
import { villaniRunDetails } from "./villaniRunDetails.js";

export const appShell = (vm: ReplayDashboardViewModel) => `
<div class="v-app-shell vfr-shell" data-villani-surface="flight-recorder">
  <aside class="v-sidebar" aria-label="Primary navigation" data-testid="shared-sidebar">
    <div class="v-sidebar__brand">[V] VILLANI</div>
    <nav class="v-sidebar__body">
      <section class="v-sidebar-section">
        <h2 class="v-sidebar-section__title">OBSERVE</h2>
        <a class="v-sidebar-item" aria-current="page" href="#overview"><span class="v-sidebar-item__glyph">◎</span>Replay</a>
        <a class="v-sidebar-item" href="#timelineTitle"><span class="v-sidebar-item__glyph">│</span>Timeline</a>
        <a class="v-sidebar-item" href="#evidence-panel"><span class="v-sidebar-item__glyph">◇</span>Evidence</a>
        <a class="v-sidebar-item" href="#file-activity"><span class="v-sidebar-item__glyph">≡</span>Files</a>
        <a class="v-sidebar-item" href="#candidate-comparison"><span class="v-sidebar-item__glyph">⇄</span>Candidates</a>
      </section>
      <div class="vfr-sidebar-meta">FLIGHT RECORDER<br>READ ONLY / LOCAL</div>
    </nav>
  </aside>
  ${topBar(vm)}
  <div class="v-status-strip">
    <span class="v-status-badge" data-status="${vm.capturedRunStatus.status}">● ${vm.capturedRunStatus.label}</span>
    <span>REPLAY / STATIC</span><span>TRUTH / CANONICAL</span>
  </div>
  <main class="v-canvas" id="main-content">
    <div class="vfr-page app-shell">
      <div id="overview">${metricCards(vm)}</div>
      ${vm.villani ? villaniRunDetails(vm.villani) : ""}
      <div class="investigation-grid-main">
        <div data-testid="replay-timeline" data-surface="event-stream">${timeline(vm)}</div>
        <div data-testid="event-evidence">${detailPanel(vm)}</div>
      </div>
    </div>
  </main>
</div>`;
