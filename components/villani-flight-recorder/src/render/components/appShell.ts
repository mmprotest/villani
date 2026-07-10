import { ReplayDashboardViewModel } from "../viewModel.js";
import { topBar } from "./topBar.js";
import { metricCards } from "./metricCards.js";
import { timeline } from "./timeline.js";
import { detailPanel } from "./detailPanel.js";
export const appShell = (vm: ReplayDashboardViewModel) =>
  `<div class="app-shell">${topBar(vm)}${metricCards(vm)}<main class="investigation-grid-main">${timeline(vm)}${detailPanel(vm)}</main></div>`;
