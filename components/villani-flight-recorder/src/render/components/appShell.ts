import { ReplayDashboardViewModel } from "../viewModel.js";
import { topBar } from "./topBar.js";
import { metricCards } from "./metricCards.js";
import { timeline } from "./timeline.js";
import { detailPanel } from "./detailPanel.js";
import { villaniRunDetails } from "./villaniRunDetails.js";
export const appShell = (vm: ReplayDashboardViewModel) =>
  `<div class="app-shell">${topBar(vm)}${metricCards(vm)}${vm.villani ? villaniRunDetails(vm.villani) : ""}<main class="investigation-grid-main">${timeline(vm)}${detailPanel(vm)}</main></div>`;
